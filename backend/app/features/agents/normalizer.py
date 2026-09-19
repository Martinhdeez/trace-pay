"""Norm normalizer: turns a norm in natural language into rules (ADR 0017).

A non-technical person pastes the norm (any language). Each sentence stays one norm rule,
kept exactly as written: the unit the client owns. The normalizer splits it into atomic
checks, in English, each with the decision the norm implies and how it was read; each check
is an ordinary `Rule` (one code, one decision) linked to its norm rule. The console first
asks for a preview, which writes nothing and can be revised with the manager's feedback;
accepting it saves the checks like any other rule, they compile in the background (ADR
0004), and every interpretation is kept in the check's `report["norm"]`.

A check's decision is the one the norm names for that failure (`decision_source:
"explicit"`, with the words that name it). When the norm does not name it (`"policy"`),
code decides from the check's `kind`, whatever the model proposed: a `violation` (the
failure proves the case breaks the norm) gets the use case's `failed_check_decision`; a
`doubt` (the failure means the system cannot tell the right outcome by itself, e.g. several
instances claim the same order) gets the escalation type.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, NotFoundError
from app.core import events
from app.features.agents import compiler, llm
from app.features.processes.model import DecisionType, Process, Symbol
from app.features.rules import service as rules
from app.features.rules.model import NormRule, Rule
from app.features.rules.schemas import RuleIn

MAX_REPAIRS = 2


class Check(BaseModel):
    """One checkable condition of a norm sentence: becomes one `Rule`."""

    text: str  # English, precise, naming symbols and source columns
    summary: str = ""  # the same check in one plain line for a non-technical reader
    type: Literal["requirement", "prohibition"]
    decision: str
    # explicit: the norm names the outcome of this failure (`quote` holds those words);
    # policy: it does not, and the use case's `failed_check_decision` decides.
    decision_source: Literal["explicit", "policy"]
    quote: str = ""
    # violation: its failure proves non-compliance; doubt: a person must resolve it.
    kind: Literal["violation", "doubt"] = "violation"
    kind_reason: str = ""  # why this kind, in one sentence
    interpretation: str  # what was decided and why


class Sentence(BaseModel):
    """One sentence of the norm and what the normalizer made of it."""

    number: int
    text: str  # exactly as the client wrote it
    checks: list[Check] = []
    policies: list[str] = []  # statements that are not checkable conditions
    covered: list[int] = []  # ids of active rules that already implement part of it


class Normalization(BaseModel):
    norm_rules: list[Sentence]


class NormIn(BaseModel):
    text: str


class NormPreviewIn(BaseModel):
    text: str
    feedback: str = ""  # the manager's answer to `previous`, to revise it
    previous: Normalization | None = None


class ExistingRule(BaseModel):
    """A rule a sentence's `covered` names: what already implements part of it."""

    id: int
    text: str
    summary: str | None
    status: str
    decision: str


class NormPreview(Normalization):
    existing: list[ExistingRule] = []


class CreatedCheck(Check):
    rule_id: int


class CreatedNormRule(Sentence):
    id: int
    checks: list[CreatedCheck]


class NormOut(BaseModel):
    norm_rules: list[CreatedNormRule]


@dataclass(frozen=True)
class Deps:
    decisions: set[str]
    default: str
    rules: dict[int, str]  # existing rules, any status but retired: id -> text
    policy: str | None = None  # failed_check_decision: decides every `policy` violation
    escalate: str | None = None  # the escalation type: decides every `policy` doubt


normalizer = Agent(
    None, output_type=Normalization, deps_type=Deps, name="normalizer", retries=MAX_REPAIRS
)


@normalizer.output_validator
def _consistent(ctx: RunContext[Deps], output: Normalization) -> Normalization:
    problems = []
    for s in output.norm_rules:
        for c in s.checks:
            if c.decision_source == "policy" and c.kind == "doubt" and ctx.deps.escalate:
                c.decision = ctx.deps.escalate
            elif c.decision_source == "policy" and c.kind == "violation" and ctx.deps.policy:
                c.decision = ctx.deps.policy
            elif c.decision_source == "explicit" and not _quoted(c.quote, s.text):
                problems.append(
                    f"check {c.text!r} is `explicit` but its `quote` {c.quote!r} is not words "
                    "of its sentence naming the outcome; quote them, or mark it `policy`"
                )
    checks = [c for s in output.norm_rules for c in s.checks]
    for c in checks:
        if c.decision not in ctx.deps.decisions:
            known = sorted(ctx.deps.decisions)
            problems.append(f"{c.decision!r} is not a decision type ({known})")
        elif c.decision == ctx.deps.default:
            problems.append(
                f"check {c.text!r} decides the default {c.decision!r}: a rule fires to "
                "prevent the default, choose another decision"
            )
    texts = [c.text.strip() for c in checks]
    if len(set(texts)) < len(texts):
        problems.append("check texts must be unique")
    if repeated := set(texts) & {t.strip() for t in ctx.deps.rules.values()}:
        problems.append(f"already existing rules, list their ids in `covered`: {sorted(repeated)}")
    if unknown := {i for s in output.norm_rules for i in s.covered} - set(ctx.deps.rules):
        problems.append(f"`covered` names rules that do not exist: {sorted(unknown)}")
    if empty := [s.number for s in output.norm_rules if not (s.checks or s.policies or s.covered)]:
        problems.append(f"norm rules {empty} have no check, policy or covered rule")
    if problems:
        raise ModelRetry("Fix your answer: " + "; ".join(problems))
    return output


def _quoted(quote: str, sentence: str) -> bool:
    def words(text: str) -> str:
        return " ".join(text.casefold().split())

    return bool(quote.strip()) and words(quote) in words(sentence)


def check_policy(policy: str | None, types: Sequence[DecisionType]) -> None:
    """A `failed_check_decision` must be a decision type of the process and never the
    default: a failed check must not pay."""
    if policy is None:
        return
    found = next((t for t in types if t.name == policy), None)
    if found is None:
        known = sorted(t.name for t in types)
        raise ConflictError(f"failed_check_decision {policy!r} is not a decision type ({known})")
    if found.is_default:
        raise ConflictError(
            f"failed_check_decision {policy!r} is the default decision: a failed check "
            "would change nothing"
        )


def escalation(types: Sequence[DecisionType]) -> str | None:
    """The escalation type: the highest-priority type that requires a human."""
    human = [t for t in types if t.requires_human]
    return max(human, key=lambda t: t.priority).name if human else None


def context(
    norm: str,
    description: str,
    types: Sequence[DecisionType],
    symbols: Sequence[Symbol],
    sources: compiler.Sources,
    active: Sequence[Rule],
    policy: str | None = None,
    previous: Normalization | None = None,
    feedback: str = "",
) -> str:
    """What the normalizer sees: the norm and everything a rule may use or already says.
    `active` is every rule that is not retired, whatever its status."""
    lines = [
        "Norm:",
        norm.strip(),
        "",
        "Use case description (conventions shared by all its rules):",
        description or "(no description)",
        "",
        "Decision types (name, priority, is_default, requires_human):",
        *(f"- {t.name}, {t.priority}, {t.is_default}, {t.requires_human}" for t in types),
        "",
        "Symbols of each instance (name, type, description):",
        *(f"- {s.name} ({s.type}): {s.description}" for s in symbols),
        "",
        "Sources of truth (columns and 3 sample rows):",
    ]
    for name, rows in sources.items():
        columns = list(dict.fromkeys(c for r in rows for c in r))
        lines.append(f"- {name}: columns {columns}")
        lines += [f"    {json.dumps(r, ensure_ascii=False, default=str)}" for r in rows[:3]]
    if not sources:
        lines.append("- (none)")
    lines += ["", "Existing rules (id (status): text):"]
    lines += [f"- {r.id}{f' ({r.status})' if r.status else ''}: {r.text}" for r in active] or [
        "- (none)"
    ]
    lines += ["", "Decision of a `policy` check of kind `violation`:"]
    lines.append(f"- {policy}" if policy else "- (not set: follow the fallbacks)")
    lines += ["", "Decision of a `policy` check of kind `doubt`:"]
    lines.append(f"- {escalation(types) or '(no type requires a human: follow the fallbacks)'}")
    if previous is not None:
        lines += ["", "Your previous proposal (JSON):", previous.model_dump_json()]
    if feedback.strip():
        lines += ["", "The manager's feedback on it:", feedback.strip()]
    return "\n".join(lines)


async def normalize(
    norm: str,
    description: str,
    types: Sequence[DecisionType],
    symbols: Sequence[Symbol],
    sources: compiler.Sources,
    active: Sequence[Rule],
    setup: llm.Setup | None = None,
    previous: Normalization | None = None,
    feedback: str = "",
) -> tuple[Normalization, llm.Trace]:
    """The normalizer on one norm, without the database."""
    policy = setup.settings.failed_check_decision if setup else None
    check_policy(policy, types)
    deps = Deps(
        decisions={t.name for t in types},
        default=next(t.name for t in types if t.is_default),
        rules={r.id: r.text for r in active},
        policy=policy,
        escalate=escalation(types),
    )
    prompt = context(norm, description, types, symbols, sources, active, policy, previous, feedback)
    return await llm.run(
        normalizer,
        "normalizer",
        prompt,
        instructions=llm.prompt("normalizer"),
        setup=setup,
        deps=deps,
    )


async def _definition(session: AsyncSession, process_id: int):
    """Decision types and symbols as the draft has them, else as published."""
    if await session.get(Process, process_id) is None:
        raise NotFoundError(f"Process {process_id} does not exist")
    from app.features.processes import service as processes
    from app.features.versions.model import ProcessDraft

    draft = await session.get(ProcessDraft, process_id)
    if draft:
        types = [DecisionType(**t) for t in draft.snapshot["process"]["decision_types"]]
        symbols = [Symbol(**s) for s in draft.snapshot["process"]["symbols"]]
        return types, symbols
    process = await processes.get(session, process_id)
    types = [DecisionType(**t.model_dump()) for t in process.decision_types]
    symbols = [Symbol(**s.model_dump()) for s in process.symbols]
    return types, symbols


async def _existing(session: AsyncSession, process_id: int) -> list[Rule]:
    """Every rule that is not retired: a draft or compiling rule is not written twice either."""
    return list(
        await session.scalars(
            select(Rule)
            .where(Rule.process_id == process_id, Rule.status != "retired")
            .order_by(Rule.id)
        )
    )


async def preview(
    session: AsyncSession,
    process_id: int,
    norm: str,
    feedback: str = "",
    previous: Normalization | None = None,
) -> NormPreview:
    """What the normalizer makes of a norm, revised with the manager's feedback when given.
    Writes nothing: `persist` saves what the manager accepts."""
    types, symbols = await _definition(session, process_id)
    description, sources, setups = await compiler.read_process(session, process_id)
    existing = await _existing(session, process_id)
    with events.span("normalize_norm", process_id=process_id) as span:
        output, _ = await normalize(
            norm,
            description,
            types,
            symbols,
            sources,
            existing,
            setups.get("normalizer"),
            previous=previous,
            feedback=feedback,
        )
        checks = sum(len(s.checks) for s in output.norm_rules)
        span.set(output=output.model_dump(), norm_rules=len(output.norm_rules), checks=checks)
    covered = {i for s in output.norm_rules for i in s.covered}
    return NormPreview(
        norm_rules=output.norm_rules,
        existing=[
            ExistingRule(
                id=r.id, text=r.text, summary=r.summary, status=r.status, decision=r.decision
            )
            for r in existing
            if r.id in covered
        ],
    )


async def persist(session: AsyncSession, process_id: int, output: Normalization) -> NormOut:
    """Save a reviewed normalization: one norm rule per sentence that keeps a check or a
    policy, each check a `Rule` linked to it, with its reading in `report["norm"]`. The
    manager may have dropped checks, so what the model validator checked is checked again."""
    types, _ = await _definition(session, process_id)
    names = {t.name for t in types}
    default = next((t.name for t in types if t.is_default), None)
    taken = {r.text.strip() for r in await _existing(session, process_id)}
    texts = [c.text.strip() for s in output.norm_rules for c in s.checks]
    problems = []
    for c in (c for s in output.norm_rules for c in s.checks):
        if c.decision not in names:
            problems.append(f"{c.decision!r} is not a decision type ({sorted(names)})")
        elif c.decision == default:
            problems.append(f"check {c.text!r} decides the default {c.decision!r}")
    if len(set(texts)) < len(texts):
        problems.append("check texts must be unique")
    if repeated := set(texts) & taken:
        problems.append(f"these checks already exist as rules: {sorted(repeated)}")
    if problems:
        raise ConflictError("; ".join(problems))
    created = []
    for sentence in output.norm_rules:
        if not (sentence.checks or sentence.policies):
            continue
        norm_rule = NormRule(
            process_id=process_id,
            number=sentence.number,
            text=sentence.text,
            policies=sentence.policies,
        )
        session.add(norm_rule)
        await session.flush()
        checks = []
        for check in sentence.checks:
            data = RuleIn(
                text=check.text,
                summary=check.summary or None,
                type=check.type,
                decision=check.decision,
            )
            rule = await session.get(Rule, (await rules.create(session, process_id, data)).id)
            rule.norm_rule_id = norm_rule.id
            reading = {
                "norm_rule": sentence.text,
                "interpretation": check.interpretation,
                "decision_source": check.decision_source,
                "quote": check.quote,
                "kind": check.kind,
                "kind_reason": check.kind_reason,
                "policies": sentence.policies,
                "covered": sentence.covered,
            }
            rule.report = {**(rule.report or {}), "norm": reading}
            checks.append(CreatedCheck(**check.model_dump(), rule_id=rule.id))
        created.append(
            CreatedNormRule(
                **sentence.model_dump(exclude={"checks"}), id=norm_rule.id, checks=checks
            )
        )
    await session.commit()
    return NormOut(norm_rules=created)


async def normalize_norm(session: AsyncSession, process_id: int, norm: str) -> NormOut:
    """Normalize a norm and save it as is, without review (the CLI, evals and tests)."""
    proposal = await preview(session, process_id, norm)
    return await persist(session, process_id, Normalization(norm_rules=proposal.norm_rules))
