from typing import Literal

from fastapi import APIRouter, BackgroundTasks, status

from app.core import events
from app.core.database import Session
from app.features.agents import normalizer
from app.features.agents.normalizer import (
    Normalization,
    NormIn,
    NormOut,
    NormPreview,
    NormPreviewIn,
)
from app.features.decisions.schemas import ImpactOut
from app.features.rules import service
from app.features.rules.schemas import NormRuleOut, RuleDetail, RuleIn, RuleOut
from app.features.users.dependencies import Manager

router = APIRouter(tags=["rules"])

Status = Literal["compiling", "draft", "active", "blocked", "retired"]


@router.get("/processes/{process_id}/rules", operation_id="listRules", summary="Rules")
async def list_rules(
    process_id: int, session: Session, status: Status | None = None
) -> list[RuleOut]:
    return await service.list_all(session, process_id, status)


@router.post(
    "/processes/{process_id}/rules",
    operation_id="createRule",
    status_code=status.HTTP_201_CREATED,
    summary="Add a rule as text. It is compiled in the background (status `compiling`)",
)
async def create_rule(
    process_id: int,
    body: RuleIn,
    session: Session,
    background: BackgroundTasks,
    user: Manager,
) -> RuleDetail:
    with events.span("save_rule", process_id=process_id, author=user.name) as span:
        rule = await service.create(session, process_id, body)
        span.set(rule_id=rule.id)
    background.add_task(service.compile_all_in_background, [rule.id], span)
    return rule


@router.post(
    "/processes/{process_id}/norm",
    operation_id="normalizeNorm",
    status_code=status.HTTP_201_CREATED,
    summary="Turn a norm in natural language into norm rules and their checks. The checks "
    "compile in the background, concurrently (status `compiling`)",
    description="The normalizer agent keeps each sentence of the norm (any language) as one "
    "norm rule and splits it into atomic checks in English, each an ordinary rule "
    "(`norm_rule_id` links it) with the decision the norm implies and how it was read (kept "
    "in the rule's `report.norm`). Statements that are not checkable conditions come back as "
    "the norm rule's `policies`; ids of active rules that already cover it, as `covered`.",
    responses={502: {"description": "The normalizer's model failed"}},
)
async def normalize_norm(
    process_id: int,
    body: NormIn,
    session: Session,
    user: Manager,
    background: BackgroundTasks,
) -> NormOut:
    # One trace: the normalizer, then every check's compilation in the background.
    with events.span("norm", process_id=process_id, author=user.name) as span:
        out = await normalizer.normalize_norm(session, process_id, body.text)
    ids = [c.rule_id for n in out.norm_rules for c in n.checks]
    background.add_task(service.compile_all_in_background, ids, span)
    return out


@router.post(
    "/processes/{process_id}/norm/preview",
    operation_id="previewNorm",
    summary="What the normalizer makes of a norm, without saving anything",
    description="Same reading as `POST /norm`, returned for review: nothing is written. With "
    "`previous` (an earlier preview) and `feedback` the normalizer revises that proposal. "
    "`existing` holds the rules that the sentences' `covered` ids name, whatever their status: "
    "what the norm asks for that is already there. `POST /norm/accept` saves the reviewed one.",
    responses={502: {"description": "The normalizer's model failed"}},
)
async def preview_norm(
    process_id: int, body: NormPreviewIn, session: Session, user: Manager
) -> NormPreview:
    with events.span("norm_preview", process_id=process_id, author=user.name):
        return await normalizer.preview(
            session, process_id, body.text, body.feedback, body.previous
        )


@router.post(
    "/processes/{process_id}/norm/accept",
    operation_id="acceptNorm",
    status_code=status.HTTP_201_CREATED,
    summary="Save a reviewed norm preview: its checks become rules that compile in the "
    "background (status `compiling`)",
    description="The body is a preview's `norm_rules`, possibly with checks or sentences "
    "removed. 409 when a check decides a decision type that does not exist or is the default, "
    "or says exactly what an existing rule says.",
    responses={409: {"description": "A check is invalid or already exists"}},
)
async def accept_norm(
    process_id: int,
    body: Normalization,
    session: Session,
    user: Manager,
    background: BackgroundTasks,
) -> NormOut:
    with events.span("norm", process_id=process_id, author=user.name) as span:
        out = await normalizer.persist(session, process_id, body)
    ids = [c.rule_id for n in out.norm_rules for c in n.checks]
    background.add_task(service.compile_all_in_background, ids, span)
    return out


@router.get(
    "/processes/{process_id}/norm-rules",
    operation_id="listNormRules",
    summary="The sentences of the client's norm, each with its atomic rules",
)
async def list_norm_rules(process_id: int, session: Session) -> list[NormRuleOut]:
    return await service.list_norm_rules(session, process_id)


@router.get("/rules/{rule_id}", operation_id="getRule", summary="A rule with its code")
async def get_rule(rule_id: int, session: Session) -> RuleDetail:
    return await service.get(session, rule_id)


@router.post(
    "/rules/{rule_id}/compile",
    operation_id="compileRule",
    summary="Recompile a draft or blocked rule: tests, code, validation; waits for it",
)
async def compile_rule(rule_id: int, session: Session, user: Manager) -> RuleDetail:
    return await service.compile_rule(session, rule_id, user.name)


@router.get(
    "/rules/{rule_id}/impact",
    operation_id="getImpact",
    summary="What activating (or retiring) this rule would change, without doing it",
)
async def get_impact(rule_id: int, session: Session) -> ImpactOut:
    return ImpactOut.model_validate(await service.impact(session, rule_id), from_attributes=True)


@router.post(
    "/rules/{rule_id}/activate",
    operation_id="activateRule",
    summary="Stage a validated rule in the process draft; publish the draft to activate",
    responses={
        409: {
            "description": "Not compiled, discrepancies unresolved, or it would "
            "contradict a decision a person took"
        }
    },
)
async def activate_rule(rule_id: int, session: Session, user: Manager) -> RuleDetail:
    return await service.activate(session, rule_id, user.name)


@router.post(
    "/rules/{rule_id}/retire",
    operation_id="retireRule",
    summary="Stage removal of a rule; validate and publish the process draft to retire it",
    responses={409: {"description": "It would contradict a decision a person took"}},
)
async def retire_rule(rule_id: int, session: Session, user: Manager) -> RuleDetail:
    return await service.retire(session, rule_id, user.name)
