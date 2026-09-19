"""Read the audit trail (`events`, ADR 0018): span trees, an instance's journey, how a rule
was produced, a process's aggregates, and the three monitoring planes."""

import asyncio
import json
import statistics
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import Numeric, String, case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.common.exceptions import NotFoundError
from app.core.config import settings
from app.core.database import session_factory
from app.core.events import Event
from app.features.agents import decision_reviewer
from app.features.agents.llm import TRUNCATED
from app.features.alerts import service as alerts
from app.features.decisions import service as decisions
from app.features.decisions.model import ENGINE, Decision
from app.features.decisions.runs import reason_codes
from app.features.decisions.schemas import DecisionOut
from app.features.ingestion.model import File, Instance
from app.features.processes.model import Process
from app.features.processes.service import get as get_process
from app.features.rules.model import NormRule, Rule
from app.features.sources import service as sources
from app.features.traces.schemas import (
    AgentsMetrics,
    CompileStats,
    DecisionTrace,
    ExecutionMetrics,
    FileOut,
    IngestionMetrics,
    InstanceTrace,
    LlmStats,
    NormStats,
    Plane,
    PlaneHealth,
    ProcessMetrics,
    ProviderStats,
    RuleResultOut,
    RuleRunStats,
    RuleRuntime,
    RuleTrace,
    SourceRead,
    SourceStats,
    SpanNode,
    SpanOut,
    StepStats,
    TokenBucket,
    TokenStats,
)

FAILURES = (
    "MISSING_DATA",
    "RULE_ERROR",
    "RULE_NEEDS_DATA",
    "RULE_COMPILE_FAILED",
    "RULE_CONFLICT",
)
LIFECYCLE = ("save_rule", "compile_rule", "activate_rule", "retire_rule", "impact_check")
SYNC_STATS = ("requests", "retries", "rate_limited", "timeouts")  # of a `sync_source` span

# Every span name, in exactly one monitoring plane. `test_planes.py` fails when the code
# emits a span that is not here. An `llm_run` is always `agents`, whichever step called it.
_INGESTION, _AGENTS, _EXECUTION = Plane.ingestion, Plane.agents, Plane.execution
PLANES: dict[str, Plane] = {
    # Reading documents and sources into instances and snapshots.
    "upload_document": _INGESTION,
    "store_file": _INGESTION,
    "extraction": _INGESTION,
    "native_text": _INGESTION,
    "ocr": _INGESTION,
    "vision": _INGESTION,
    "text_judge": _INGESTION,
    "schema_fields": _INGESTION,
    "provider_call": _INGESTION,
    "focused_read": _INGESTION,
    "ingest_document": _INGESTION,
    "configure_mail_gathering": _INGESTION,
    "mail_operator": _INGESTION,
    "reextract_document": _INGESTION,
    "extract_document": _INGESTION,
    "upload_workbook": _INGESTION,
    "load_workbook": _INGESTION,
    "sync_source": _INGESTION,
    "discover_source": _INGESTION,
    # Agents writing the rules' code, and what defines and gates it.
    "load_use_case": _AGENTS,
    "load_definition": _AGENTS,
    "configure_agent": _AGENTS,
    "activate_agent_config": _AGENTS,
    "save_rule": _AGENTS,
    "norm": _AGENTS,
    "norm_preview": _AGENTS,
    "normalize_norm": _AGENTS,
    "discover_process": _AGENTS,
    "discuss_process": _AGENTS,
    "compile_process_draft": _AGENTS,
    "publish_process_draft": _AGENTS,
    "compile_rules": _AGENTS,
    "compile_rule": _AGENTS,
    "coder_attempt": _AGENTS,
    "run_tests": _AGENTS,
    "impact_check": _AGENTS,
    "activate_rule": _AGENTS,
    "publish_process_version": _AGENTS,
    "retire_rule": _AGENTS,
    "llm_run": _AGENTS,
    "demo_llm_down": _AGENTS,
    "learn_norms": _AGENTS,  # norms proposed from past cases, then validated and adopted
    "validate_norm": _AGENTS,
    "adopt_norm": _AGENTS,
    "reject_norm": _AGENTS,
    "accept_proposal": _AGENTS,  # a manager settles any proposal (docs/api.md, Proposals)
    "reject_proposal": _AGENTS,
    "suggest_escalation": _AGENTS,  # the assistant's LLM suggestions, not the engine's run
    "propose_decision": _AGENTS,
    "suggest_rule": _AGENTS,  # an amendment of the escalating rule, after a resolution
    "expire_proposal": _AGENTS,  # open proposals closed unsettled: `superseded` + cause
    # Running compiled code over instances, and the people acting on its decisions.
    "run_process": _EXECUTION,
    "evaluate_rule": _EXECUTION,
    "decision": _EXECUTION,
    "reprocess": _EXECUTION,
    "review_decision": _EXECUTION,
    "resolution": _EXECUTION,
    "export_outcomes": _EXECUTION,
    "detect_stale_decisions": _EXECUTION,  # ADR 0026
    "ack_alert": _EXECUTION,
}
STREAM_POLL_S = 1.0


def _nodes(rows: Iterable[Event]) -> dict[str, SpanNode]:
    """Every span by id, each holding its children in start order."""
    nodes = {r.span_id: SpanNode.model_validate(r, from_attributes=True) for r in rows}
    for node in sorted(nodes.values(), key=lambda n: (n.started_at, n.id)):
        if node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)
    return nodes


def _roots(nodes: dict[str, SpanNode]) -> list[SpanNode]:
    return sorted(
        (n for n in nodes.values() if n.parent_id not in nodes), key=lambda n: (n.started_at, n.id)
    )


_AGENT = func.coalesce(func.nullif(Event.data["agent"].astext, ""), Event.data["role"].astext)


async def list_spans(
    session: AsyncSession,
    process_id: int | None,
    step: str | None,
    status: str | None,
    limit: int,
    plane: Plane | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    rule_id: int | None = None,
    norm_rule_id: int | None = None,
    use_case_id: int | None = None,
    data: dict[str, str | None] | None = None,
) -> list[SpanOut]:
    """`data` filters span attributes: `model`, `role`, `agent` (the `by_role` key: the
    agent's name, else its role), `provider`, `operation`, `source`."""
    query = select(Event).where(*_scope(process_id, since))
    if step is not None:
        query = query.where(Event.step == step)
    if status is not None:
        query = query.where(Event.status == status)
    if plane is not None:
        query = query.where(Event.step.in_(steps_of(plane)))
    if until is not None:
        query = query.where(Event.started_at < until)
    if rule_id is not None:
        query = query.where(Event.rule_id == rule_id)
    if norm_rule_id is not None:
        query = query.where(Event.norm_rule_id == norm_rule_id)
    if use_case_id is not None:
        processes = select(Process.id).where(Process.use_case_id == use_case_id)
        query = query.where(Event.process_id.in_(processes))
    for key, value in (data or {}).items():
        if value is not None:
            query = query.where((_AGENT if key == "agent" else Event.data[key].astext) == value)
    rows = await session.scalars(query.order_by(Event.id.desc()).limit(limit))
    return [SpanOut.model_validate(r, from_attributes=True) for r in rows]


async def get_trace(session: AsyncSession, trace_id: str) -> list[SpanNode]:
    rows = list(await session.scalars(select(Event).where(Event.trace_id == trace_id)))
    if not rows:
        raise NotFoundError(f"Trace {trace_id} does not exist")
    return _roots(_nodes(rows))


async def instance_trace(session: AsyncSession, instance_id: int) -> InstanceTrace:
    instance = await session.get(Instance, instance_id)
    if instance is None:
        raise NotFoundError(f"Instance {instance_id} does not exist")
    file = (
        await session.execute(
            select(File.hash, File.name, func.length(File.content), File.ingested_at).where(
                File.hash == instance.file_hash
            )
        )
    ).first()
    history = list(
        await session.scalars(
            select(Decision).where(Decision.instance_id == instance_id).order_by(Decision.id)
        )
    )
    rule_ids = {r["rule_id"] for d in history for r in d.results}
    rules = {r.id: r for r in await session.scalars(select(Rule).where(Rule.id.in_(rule_ids)))}
    # Its spans, and in the same traces the steps of no particular instance (the upload's
    # reading steps, the run and its per-rule spans), never another instance's.
    traces = select(Event.trace_id).where(Event.instance_id == instance_id)
    query = select(Event).where(
        Event.trace_id.in_(traces),
        or_(Event.instance_id == instance_id, Event.instance_id.is_(None)),
    )
    rows = list(await session.scalars(query))
    if history:
        exports = (
            select(Event)
            .where(
                Event.process_id == instance.process_id,
                Event.step == "export_outcomes",
                Event.started_at >= history[0].created_at,
            )
            .order_by(Event.id.desc())
            .limit(10)
        )
        rows += list(await session.scalars(exports))
    engine = [d for d in history if d.author == ENGINE]
    automatic = engine[-1] if engine else None
    human = next((d for d in reversed(history) if d.author != ENGINE), None)
    exported = automatic or human
    if automatic:
        review = (await decision_reviewer.for_decisions(session, [automatic])).get(automatic.id)
        if review:
            if human and human.id > automatic.id:
                exported = human
            elif review.requires_human:
                exported = None
    if instance.status == "PENDING":
        exported = None
    read: list[Event] = []
    if history:
        name = Event.data["source"].astext
        read = list(
            await session.scalars(
                select(Event)
                .where(
                    Event.step == "sync_source",
                    Event.process_id == instance.process_id,
                    Event.started_at <= history[-1].created_at,
                )
                .distinct(name)
                .order_by(name, Event.id.desc())
            )
        )
    return InstanceTrace(
        id=instance.id,
        process_id=instance.process_id,
        name=instance.name,
        status=instance.status,
        file=FileOut(hash=file[0], name=file[1], size_bytes=file[2], ingested_at=file[3])
        if file
        else None,
        symbols=instance.symbols,
        decisions=[
            DecisionTrace(
                **DecisionOut.model_validate(d, from_attributes=True).model_dump(),
                rule_results=[
                    RuleResultOut(
                        **r,
                        rule_text=rules[r["rule_id"]].text if r["rule_id"] in rules else None,
                        rule_summary=rules[r["rule_id"]].summary if r["rule_id"] in rules else None,
                        norm_rule_id=rules[r["rule_id"]].norm_rule_id
                        if r["rule_id"] in rules
                        else None,
                    )
                    for r in d.results
                ],
            )
            for d in history
        ],
        exported_decision=exported.decision if exported else None,
        spans=_roots(_nodes(rows)),
        sources_read=[
            SourceRead(
                source=e.data["source"],
                status=e.status,
                started_at=e.started_at,
                duration_ms=e.duration_ms,
                trace_id=e.trace_id,
                **{k: e.data.get(k) or 0 for k in SYNC_STATS},
            )
            for e in read
        ],
    )


def _p(q: float):
    return func.percentile_cont(q).within_group(Event.duration_ms)


async def rule_trace(session: AsyncSession, rule_id: int) -> RuleTrace:
    rule = await session.get(Rule, rule_id)
    if rule is None:
        raise NotFoundError(f"Rule {rule_id} does not exist")
    norm_rule = await session.get(NormRule, rule.norm_rule_id) if rule.norm_rule_id else None
    traces = select(Event.trace_id).where(Event.rule_id == rule_id, Event.step == "compile_rule")
    nodes = _nodes(await session.scalars(select(Event).where(Event.trace_id.in_(traces))))
    compilations = sorted(
        (n for n in nodes.values() if n.step == "compile_rule" and n.rule_id == rule_id),
        key=lambda n: n.started_at,
        reverse=True,
    )
    normalizations = [n for n in nodes.values() if n.step == "normalize_norm"]
    runs = select(Event.span_id).where(
        Event.process_id == rule.process_id, Event.step == "run_process"
    )
    evaluated = (
        await session.execute(
            select(
                func.count(),
                func.sum(Event.data["instances"].as_integer()),
                func.sum(Event.data["fired"].as_integer()),
                func.sum(Event.data["errors"].as_integer()),
                _p(0.5),
                _p(0.95),
            ).where(
                Event.step == "evaluate_rule",
                Event.rule_id == rule_id,
                Event.parent_id.in_(runs),
            )
        )
    ).one()
    lifecycle = await session.scalars(
        select(Event)
        .where(Event.rule_id == rule_id, Event.step.in_(LIFECYCLE))
        .order_by(Event.started_at)
    )
    return RuleTrace(
        id=rule.id,
        process_id=rule.process_id,
        text=rule.text,
        status=rule.status,
        norm_rule_id=rule.norm_rule_id,
        norm_rule_text=norm_rule.text if norm_rule else None,
        activation=(rule.report or {}).get("activation"),
        normalization=SpanOut.model_validate(
            max(normalizations, key=lambda n: n.started_at).model_dump(exclude={"children"})
        )
        if normalizations
        else None,
        compilations=compilations,
        lifecycle=[SpanOut.model_validate(e, from_attributes=True) for e in lifecycle],
        runtime=RuleRuntime(
            runs=evaluated[0],
            instances=evaluated[1] or 0,
            fired=evaluated[2] or 0,
            errors=evaluated[3] or 0,
            p50_ms=evaluated[4],
            p95_ms=evaluated[5],
        ),
    )


def _scope(process_id: int | None, since: datetime | None) -> list:
    """The spans of one process (or all) from `since` on."""
    where = []
    if process_id is not None:
        where.append(Event.process_id == process_id)
    if since is not None:
        where.append(Event.started_at >= since)
    return where


def _link(scope: dict, **filters) -> str | None:
    """`GET /traces` for the spans behind one aggregate row, in the metrics' scope (`process_id`
    and `since` of `scope`). None when a row's key is null: no filter selects exactly it."""
    if any(v is None for v in filters.values()):
        return None
    since = scope.get("since")
    query = {
        "process_id": scope.get("process_id"),
        "since": since.isoformat() if since else None,
        **filters,
        "limit": 1000,
    }
    return "/traces?" + urlencode({k: v for k, v in query.items() if v is not None})


_ERRORS = func.count().filter(Event.status == "error")


def _total(key: str):
    return func.coalesce(func.sum(Event.data[key].as_integer()), 0)


async def _steps(session: AsyncSession, where: list, scope: dict) -> list[StepStats]:
    rows = await session.execute(
        select(Event.step, func.count(), _ERRORS, _p(0.5), _p(0.95))
        .where(*where)
        .group_by(Event.step)
        .order_by(Event.step)
    )
    return [
        StepStats(step=s, count=c, errors=e, p50_ms=p50, p95_ms=p95, traces=_link(scope, name=s))
        for s, c, e, p50, p95 in rows
    ]


_TOKENS = ("retries", "requests", "input_tokens", "output_tokens", "cached_tokens")


async def _llm(session: AsyncSession, where: list, *keys, join=None) -> list[tuple]:
    """`llm_run` spans grouped by `keys`: (*keys, calls, errors, fallbacks, truncations,
    retries, requests, input, output and cached tokens, known cost, unpriced requests)."""
    failed = Event.data["failed_attempts"]
    cost_status = Event.data["cost_status"].astext
    priced = cost_status.in_(("known", "included"))
    query = select(
        *keys,
        func.count(),
        _ERRORS,
        func.count().filter(Event.status == "ok", failed.contains([{}])),
        func.count().filter(failed.contains([{"error": TRUNCATED}])),
        *map(_total, _TOKENS),
        func.coalesce(func.sum(cast(Event.data["cost_usd"].astext, Numeric)).filter(priced), 0),
        func.coalesce(
            func.sum(Event.data["requests"].as_integer()).filter(
                or_(cost_status.is_(None), ~priced)
            ),
            0,
        ),
    ).select_from(Event)
    if join is not None:
        query = query.outerjoin(*join)
    return list(
        await session.execute(
            query.where(*where, Event.step == "llm_run").group_by(*keys).order_by(*keys)
        )
    )


_LLM_FIELDS = ("calls", "errors", "fallbacks", "truncations", *_TOKENS, "known_cost_usd")
_LLM_FIELDS += ("unpriced_requests",)


def _token_stats(rows: list[tuple], link) -> list[TokenStats]:
    """One row per key; `link(key)` is its drill-down."""
    return [
        TokenStats(
            key=None if k is None else str(k),
            **dict(zip(_LLM_FIELDS, rest, strict=True)),
            traces=link(k),
        )
        for k, *rest in rows
    ]


async def _runs(session: AsyncSession, where: list) -> tuple[int, int, float | None]:
    """Completed runs, the instances they decided, and instances per second of run time."""
    runs, instances, run_ms = (
        await session.execute(
            select(
                func.count(),
                func.sum(Event.data["instances"].as_integer()),
                func.sum(Event.duration_ms),
            ).where(*where, Event.step == "run_process", Event.status == "ok")
        )
    ).one()
    instances, run_ms = instances or 0, run_ms or 0
    return runs, instances, round(instances / run_ms * 1000, 1) if run_ms else None


async def _providers(session: AsyncSession, where: list, scope: dict) -> list[ProviderStats]:
    """Group journal calls; replayed response tokens never count as new network usage."""
    provider = Event.data["provider"].astext
    model = Event.data["model"].astext
    operation = Event.data["operation"].astext
    network = Event.data["network_attempted"].as_boolean().is_(True)
    replay = Event.data["outcome"].astext == "replay"
    blocked = Event.data["outcome"].astext == "blocked_uncertain"
    known = Event.data["cost_status"].astext == "known"
    included = Event.data["cost_status"].astext == "included"
    unpriced = or_(
        Event.data["cost_status"].astext.is_(None),
        Event.data["cost_status"].astext.not_in(("known", "included")),
    )
    latency = func.coalesce(Event.data["network_latency_ms"].as_integer(), Event.duration_ms)

    def network_sum(key):
        return func.coalesce(func.sum(Event.data[key].as_integer()).filter(network), 0)

    rows = await session.execute(
        select(
            provider,
            model,
            operation,
            func.count(),
            func.count().filter(network),
            func.count().filter(replay),
            _ERRORS,
            network_sum("input_tokens"),
            network_sum("output_tokens"),
            func.count().filter(blocked),
            func.count().filter(network, Event.data["fallback"].as_boolean().is_(True)),
            network_sum("total_tokens"),
            network_sum("cached_tokens"),
            network_sum("reasoning_tokens"),
            func.coalesce(func.sum(latency).filter(network), 0),
            func.percentile_cont(0.5).within_group(latency).filter(network),
            func.percentile_cont(0.95).within_group(latency).filter(network),
            func.coalesce(
                func.sum(cast(Event.data["cost_usd"].astext, Numeric)).filter(network), 0
            ),
            func.count().filter(network, known),
            func.count().filter(network, included),
            func.count().filter(network, unpriced),
            func.count().filter(Event.data["http_status_code"].as_integer() == 429),
        )
        .where(*where, Event.step == "provider_call")
        .group_by(provider, model, operation)
        .order_by(provider, model, operation)
    )
    return [
        ProviderStats(
            provider=p,
            model=m,
            operation=o,
            attempts=c,
            network_requests=n,
            replays=replays,
            errors=e,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            blocked=b,
            fallbacks=f,
            total_tokens=total,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            network_latency_ms=latency_ms,
            network_p50_ms=p50,
            network_p95_ms=p95,
            known_cost_usd=float(cost),
            priced_requests=priced,
            included_requests=included_count,
            unpriced_requests=unpriced,
            rate_limited=limited,
            traces=_link(scope, name="provider_call", provider=p, model=m, operation=o),
        )
        for (
            p,
            m,
            o,
            c,
            n,
            replays,
            e,
            tokens_in,
            tokens_out,
            b,
            f,
            total,
            cached,
            reasoning,
            latency_ms,
            p50,
            p95,
            cost,
            priced,
            included_count,
            unpriced,
            limited,
        ) in rows
    ]


def _decided(process_id: int | None, since: datetime | None) -> list:
    where = []
    if process_id is not None:
        where.append(Instance.process_id == process_id)
    if since is not None:
        where.append(Decision.created_at >= since)
    return where


async def _outcomes(session: AsyncSession, process_id: int | None, since: datetime | None):
    """Each instance's latest decision by outcome and failure, the human queue with each
    case's first reason code (`OTHER` if none), and the undecided."""
    latest = select(func.max(Decision.id)).group_by(Decision.instance_id)
    decided = [*_decided(process_id, since), Decision.id.in_(latest)]
    by_outcome = await session.execute(
        select(Decision.decision, func.count())
        .join(Instance, Decision.instance_id == Instance.id)
        .where(*decided)
        .group_by(Decision.decision)
    )
    failures = (
        await session.execute(
            select(*(func.count().filter(Decision.reason.contains(code)) for code in FAILURES))
            .join(Instance, Decision.instance_id == Instance.id)
            .where(*decided)
        )
    ).one()
    undecided = [Instance.status == "PENDING"]
    if process_id is not None:
        undecided.append(Instance.process_id == process_id)
    ids = [process_id] if process_id is not None else await session.scalars(select(Process.id))
    queue = [i for p in ids for i in await decisions.queue(session, p, None)]
    reasons = Counter((reason_codes(i.reason) or ["OTHER"])[0] for i in queue)
    return (
        dict(by_outcome.all()),
        dict(zip(FAILURES, failures, strict=True)),
        len(queue),
        dict(reasons),
        await session.scalar(select(func.count()).where(*undecided)),
    )


async def metrics(session: AsyncSession, process_id: int, since: datetime | None) -> ProcessMetrics:
    await get_process(session, process_id)
    spans = _scope(process_id, since)
    scope = {"process_id": process_id, "since": since}
    runs, instances, per_second = await _runs(session, spans)
    by_outcome, failures, escalated, reasons, pending = await _outcomes(session, process_id, since)
    return ProcessMetrics(
        since=since,
        runs=runs,
        instances_decided=instances,
        instances_per_second=per_second,
        steps=await _steps(session, spans, scope),
        llm=await _llm_stats(session, spans, scope),
        providers=await _providers(session, spans, scope),
        decisions_by_outcome=by_outcome,
        failures=failures,
        escalated=escalated,
        escalation_reasons=reasons,
        pending=pending,
    )


async def _llm_stats(session: AsyncSession, where: list, scope: dict) -> list[LlmStats]:
    """By model and role."""
    model, role = Event.data["model"].astext, Event.data["role"].astext
    return [
        LlmStats(
            model=m,
            role=r,
            **dict(zip(_LLM_FIELDS, rest, strict=True)),
            traces=_link(scope, name="llm_run", model=m, role=r),
        )
        for m, r, *rest in await _llm(session, where, model, role)
    ]


# The monitoring planes: the same `events`, cut by `PLANES`.


def steps_of(plane: Plane) -> list[str]:
    return [step for step, p in PLANES.items() if p == plane]


async def plane_metrics(
    session: AsyncSession, plane: Plane, process_id: int | None, since: datetime | None
) -> IngestionMetrics | AgentsMetrics | ExecutionMetrics:
    if process_id is not None:
        await get_process(session, process_id)
    where = _scope(process_id, since)
    scope = {"process_id": process_id, "since": since}
    steps = await _steps(session, [*where, Event.step.in_(steps_of(plane))], scope)
    base = {
        "plane": plane,
        "process_id": process_id,
        "since": since,
        "spans": sum(s.count for s in steps),
        "errors": sum(s.errors for s in steps),
        "steps": steps,
    }
    build = {
        Plane.ingestion: _ingestion,
        Plane.agents: _agents,
        Plane.execution: _execution,
    }[plane]
    return await build(session, where, base)


async def _ingestion(session: AsyncSession, where: list, base: dict) -> IngestionMetrics:
    def count(step: str, *extra):
        return func.count().filter(Event.step == step, *extra)

    epoch = func.extract("epoch", Event.started_at) * 1000
    read = Event.step.in_(("ingest_document", "extract_document"))
    row = (
        await session.execute(
            select(
                func.count().filter(read),
                func.min(epoch).filter(read),
                func.max(epoch + func.coalesce(Event.duration_ms, 0)).filter(read),
                func.coalesce(
                    func.sum(Event.data["pages"].as_integer()).filter(Event.step == "native_text"),
                    0,
                ),
                count("ocr"),
                count("vision"),
                count("text_judge"),
                count("focused_read"),
                count("extraction", Event.data["cache_hit"].as_boolean()),
            ).where(*where)
        )
    ).one()
    files, first, last, *calls = row
    # A declared symbol read as null (the process API stores `{name: {value, origin}}`).
    null = '$.symbols ? (@.type() == "object").keyvalue() ? (@.value.value == null).key'
    field = cast(func.jsonb_path_query(Event.data, cast(null, JSONPATH)), String)  # quoted
    nulls = await session.execute(
        select(field, func.count()).where(*where, read).group_by(field).order_by(field)
    )
    abstentions = {json.loads(name): n for name, n in nulls}
    seconds = (float(last) - float(first)) / 1000 if files else 0
    return IngestionMetrics(
        **base,
        files=files,
        files_per_second=round(files / seconds, 2) if seconds > 0 else None,
        pages=calls[0],
        ocr_calls=calls[1],
        vision_calls=calls[2],
        judge_calls=calls[3],
        focused_reads=calls[4],
        providers=await _providers(session, where, base),
        cache_hits=calls[5],
        abstentions=sum(abstentions.values()),
        abstentions_by_field=abstentions,
        sources=await _sources(session, where, base),
    )


async def _sources(session: AsyncSession, where: list, scope: dict) -> list[SourceStats]:
    """`sync_source` spans by source: the connector's requests, retries and 429s."""
    name = Event.data["source"].astext
    rows = await session.execute(
        select(
            name,
            func.count(),
            _ERRORS,
            *map(_total, ("requests", "retries", "rate_limited")),
            _p(0.95),
        )
        .where(*where, Event.step == "sync_source")
        .group_by(name)
        .order_by(name)
    )
    return [
        SourceStats(
            source=s,
            syncs=n,
            errors=e,
            requests=requests,
            retries=retries,
            rate_limited=limited,
            p95_ms=p95,
            traces=_link(scope, name="sync_source", source=s),
        )
        for s, n, e, requests, retries, limited, p95 in rows
    ]


async def _agents(session: AsyncSession, where: list, base: dict) -> AgentsMetrics:
    model = Event.data["model"].astext
    hour = func.date_trunc("hour", Event.started_at)
    per_hour = await session.execute(
        select(hour, func.count(), *map(_total, _TOKENS[2:]))
        .where(*where, Event.step == "llm_run")
        .group_by(hour)
        .order_by(hour)
    )
    compiled = (
        await session.execute(
            select(
                func.count().filter(Event.step == "compile_rule"),
                func.count().filter(Event.step == "compile_rule", Event.data["valid"].as_boolean()),
                func.count().filter(Event.step == "coder_attempt"),
                func.coalesce(
                    func.max(Event.data["attempt"].as_integer()).filter(
                        Event.step == "coder_attempt"
                    ),
                    0,
                ),
            ).where(*where)
        )
    ).one()
    compilations, valid, attempts, max_attempts = compiled

    by_model = _token_stats(
        await _llm(session, where, model), lambda k: _link(base, name="llm_run", model=k)
    )
    total = TokenStats(
        key=None,
        **{f: sum(getattr(r, f) for r in by_model) for f in _LLM_FIELDS},
        traces=_link(base, name="llm_run"),
    )
    return AgentsMetrics(
        **base,
        total=total,
        llm=await _llm_stats(session, where, base),
        by_model=by_model,
        by_role=_token_stats(
            await _llm(session, where, _AGENT), lambda k: _link(base, name="llm_run", agent=k)
        ),
        by_rule=_token_stats(
            await _llm(session, where, Event.rule_id),
            lambda k: _link(base, name="llm_run", rule_id=k),
        ),
        by_norm_rule=_token_stats(
            await _llm(session, where, Event.norm_rule_id),
            lambda k: _link(base, name="llm_run", norm_rule_id=k),
        ),
        by_use_case=_token_stats(
            await _llm(
                session, where, Process.use_case_id, join=(Process, Process.id == Event.process_id)
            ),
            lambda k: _link(base, name="llm_run", use_case_id=k),
        ),
        per_hour=[
            TokenBucket(
                hour=h,
                calls=c,
                input_tokens=i,
                output_tokens=o,
                cached_tokens=k,
                traces=_link(
                    {**base, "since": max(h, base["since"]) if base["since"] else h},
                    name="llm_run",
                    until=(h + timedelta(hours=1)).isoformat(),
                ),
            )
            for h, c, i, o, k in per_hour
        ],
        compile=CompileStats(
            compilations=compilations,
            valid=valid,
            success_rate=round(valid / compilations, 3) if compilations else None,
            attempts=attempts,
            attempts_per_compilation=round(attempts / compilations, 2) if compilations else None,
            max_attempts=max_attempts,
        ),
        norms=await _norms(session, where),
    )


async def _norms(session: AsyncSession, where: list, limit: int = 50) -> list[NormStats]:
    """Each norm is one trace: the normalizer, then its checks compiled in the background."""
    norms = list(
        await session.scalars(
            select(Event)
            .where(*where, Event.step == "norm")
            .order_by(Event.started_at.desc())
            .limit(limit)
        )
    )
    traces = [n.trace_id for n in norms]
    tokens = {
        t: rest
        for t, *rest in await session.execute(
            select(Event.trace_id, func.count(), _total("input_tokens"), _total("output_tokens"))
            .where(Event.trace_id.in_(traces), Event.step == "llm_run")
            .group_by(Event.trace_id)
        )
    }
    activated: dict[str, list[datetime]] = {}
    for t, started, ms in await session.execute(
        select(Event.trace_id, Event.started_at, Event.duration_ms).where(
            Event.trace_id.in_(traces),
            Event.step == "activate_rule",
            Event.status == "ok",
            Event.data["after"].astext == "active",
        )
    ):
        activated.setdefault(t, []).append(started + timedelta(milliseconds=ms or 0))
    out = []
    for n in norms:
        calls, tokens_in, tokens_out = tokens.get(n.trace_id, (0, 0, 0))
        done = activated.get(n.trace_id, [])
        out.append(
            NormStats(
                trace_id=n.trace_id,
                started_at=n.started_at,
                author=(n.data or {}).get("author"),
                calls=calls,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                rules_activated=len(done),
                seconds_to_active=round((max(done) - n.started_at).total_seconds(), 1)
                if done
                else None,
                traces=f"/traces/{n.trace_id}",
            )
        )
    return out


async def _execution(session: AsyncSession, where: list, base: dict) -> ExecutionMetrics:
    process_id, since = base["process_id"], base["since"]
    runs, instances, per_second = await _runs(session, where)
    rules = await session.execute(
        select(
            Event.rule_id,
            func.count(),
            _total("instances"),
            _total("fired"),
            _total("errors"),
            _p(0.5),
            _p(0.95),
        )
        .where(*where, Event.step == "evaluate_rule")
        .group_by(Event.rule_id)
        .order_by(Event.rule_id)
    )
    by_outcome, failures, escalated, reasons, pending = await _outcomes(session, process_id, since)
    # A person's decision, and how long after the engine's last decision before it.
    by_engine = aliased(Decision)
    engine = (
        select(func.max(by_engine.created_at))
        .where(
            by_engine.instance_id == Decision.instance_id,
            by_engine.author == ENGINE,
            by_engine.created_at < Decision.created_at,
        )
        .scalar_subquery()
    )
    resolved = list(
        await session.execute(
            select(Decision.author, func.extract("epoch", Decision.created_at - engine))
            .join(Instance, Decision.instance_id == Instance.id)
            .where(*_decided(process_id, since), Decision.author != ENGINE)
        )
    )
    waits = sorted(float(s) for _, s in resolved if s is not None and s >= 0)
    authors: dict[str, int] = {}
    for author, _ in resolved:
        authors[author] = authors.get(author, 0) + 1
    return ExecutionMetrics(
        **base,
        runs=runs,
        instances_decided=instances,
        instances_per_second=per_second,
        rules=[
            RuleRunStats(
                rule_id=r,
                evaluations=c,
                instances=i,
                fired=f,
                errors=e,
                p50_ms=p50,
                p95_ms=p95,
                traces=_link(base, name="evaluate_rule", rule_id=r),
            )
            for r, c, i, f, e, p50, p95 in rules
        ],
        decisions_by_outcome=by_outcome,
        failures=failures,
        escalated=escalated,
        escalation_reasons=reasons,
        pending=pending,
        resolutions=len(resolved),
        resolutions_by_author=authors,
        resolution_p50_s=round(statistics.median(waits), 1) if waits else None,
        resolution_p95_s=round(_quantile(waits, 0.95), 1) if waits else None,
        open_alerts=await alerts.open_count(session, process_id),
    )


def _quantile(values: list[float], q: float) -> float:
    """Linear interpolation, as Postgres's `percentile_cont`."""
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


# Live view: which plane is healthy now, and every new span as it is written.


async def health(session: AsyncSession) -> list[PlaneHealth]:
    since = datetime.now(UTC) - timedelta(minutes=settings.health_window_minutes)
    plane = case({s: p.value for s, p in PLANES.items()}, value=Event.step)
    rows = {
        p: (n, e, p95)
        for p, n, e, p95 in await session.execute(
            select(plane, func.count(), _ERRORS, _p(0.95))
            .where(Event.started_at >= since, Event.step.in_(list(PLANES)))
            .group_by(plane)
        )
    }
    out = []
    for p in Plane:
        spans, errors, p95 = rows.get(p.value, (0, 0, None))
        rate = errors / spans if spans else None
        limit = settings.health_p95_ms.get(p)
        status, reason = "ok", None
        if 0 < spans < settings.health_min_spans:
            reason = f"not enough data: {spans} spans, {settings.health_min_spans} needed"
        elif rate is not None and rate >= settings.health_down_error_rate:
            status, reason = "down", f"{errors}/{spans} spans failed"
        elif rate is not None and rate >= settings.health_degraded_error_rate:
            status, reason = "degraded", f"{errors}/{spans} spans failed"
        elif p95 is not None and limit is not None and p95 > limit:
            status, reason = "degraded", f"p95 {p95:.0f} ms over {limit} ms"
        # A known outage needs no sample size (ADR 0028).
        if (
            status == "ok"
            and p == Plane.ingestion
            and (down := await sources.down_sources(session, since))
        ):
            status, reason = "degraded", f"sources down: {', '.join(down)}"
        out.append(
            PlaneHealth(
                plane=p,
                status=status,
                spans=spans,
                errors=errors,
                error_rate=None if rate is None else round(rate, 3),
                p95_ms=p95,
                reason=reason,
            )
        )
    return out


async def stream(
    plane: Plane | None, process_id: int | None, after: int | None
) -> AsyncIterator[str]:
    """Server-sent events: each new span as `event: <plane>`, `id: <events.id>`, `data:
    <SpanOut>`; a `: ping` comment when nothing new came. Polls `events` by id."""
    where = []
    if plane is not None:
        where.append(Event.step.in_(steps_of(plane)))
    if process_id is not None:
        where.append(Event.process_id == process_id)
    while True:
        async with session_factory() as session:
            if after is None:
                after = await session.scalar(select(func.coalesce(func.max(Event.id), 0)))
            rows = list(
                await session.scalars(
                    select(Event).where(Event.id > after, *where).order_by(Event.id).limit(500)
                )
            )
        for row in rows:
            span = SpanOut.model_validate(row, from_attributes=True)
            after = row.id
            yield (
                f"id: {row.id}\nevent: {PLANES.get(row.step, 'other')}\n"
                f"data: {span.model_dump_json()}\n\n"
            )
        if not rows:
            yield ": ping\n\n"
        await asyncio.sleep(STREAM_POLL_S)
