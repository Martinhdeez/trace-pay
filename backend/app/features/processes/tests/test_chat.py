"""Process conversations distinguish explanation, proposed edits and publication."""

import copy

import pytest
from sqlalchemy import func, select

from app.core.database import session_factory
from app.features.agents import llm
from app.features.decisions.model import Decision, DecisionReview
from app.features.processes.tests.test_drafts import (
    accept,
    add_decision,
    plan,
    post,
    prepare_new,
    scripts,
)
from app.features.processes.tests.test_drafts import (
    api as api,
)
from app.features.versions import configuration
from app.features.versions.model import ProcessVersion
from tests.support.models import down, per_role, user_json


async def test_discussion_preserves_accepted_proposals_and_preview(api, monkeypatch):
    draft = await prepare_new(api, monkeypatch)
    seen = {}
    monkeypatch.setattr(
        llm,
        "model_for",
        per_role(
            {
                "discovery": [
                    {
                        "message": "The approved proposal escalates amounts above 100.",
                        "evidence": ["chat:1"],
                    }
                ]
            },
            seen,
        ),
    )
    response = await api.post(
        f"/process-drafts/{draft['id']}/messages",
        json={
            "revision": draft["revision"],
            "message": "Explain the threshold, without changing it.",
        },
    )
    assert response.status_code == 200, response.text
    discussed = response.json()
    for key in ("plan", "reviews", "preview", "changes"):
        assert discussed[key] == draft[key]
    assert discussed["revision"] == draft["revision"] + 1
    assert discussed["messages"][-1]["evidence"] == ["chat:1"]
    published = await post(api, discussed, "publish")
    assert published["published_process_id"] is not None


async def test_chat_reads_past_cases_and_separate_draft_without_changing_them(api, monkeypatch):
    initial = await post(api, await prepare_new(api, monkeypatch), "publish")
    pid = initial["published_process_id"]
    decision_id = await add_decision(pid)
    staged = (await api.put(f"/processes/{pid}/draft", json={"description": "Pending edit"})).json()
    draft = (await api.post("/process-drafts", json={"process_id": pid})).json()
    async with session_factory() as session:
        iid = (await session.get(Decision, decision_id)).instance_id
    seen = {}
    monkeypatch.setattr(
        llm,
        "model_for",
        per_role(
            {
                "discovery": [
                    {
                        "message": "The case had amount 50; there is also a pending context edit.",
                        "evidence": [
                            f"case:{iid}",
                            "sampling.outcome_counts",
                            "sampling.author_counts",
                        ],
                        "questions": [
                            "Do you want to discuss the published configuration or pending edit?"
                        ],
                    }
                ]
            },
            seen,
        ),
    )
    result = await post(api, draft, "messages", mode="discuss", message="Why did this case pay?")
    context = user_json(seen["discovery"][0])
    assert f"case:{iid}" in context["past_cases"]["evidence"]
    assert context["past_cases"]["sampling"]["selected"] == 1
    assert context["past_cases"]["sampling"]["outcome_counts"] == {"PAY": 1}
    assert context["past_cases"]["sampling"]["author_counts"] == {"engine": 1}
    assert context["editable_version_draft"]["snapshot"]["process"]["description"] == "Pending edit"
    assert result["plan"] == draft["plan"]
    assert (await api.get(f"/processes/{pid}/draft")).json() == staged


async def test_discussion_failure_leaves_conversation_unchanged(api, monkeypatch):
    draft = await prepare_new(api, monkeypatch)
    monkeypatch.setattr(llm, "model_for", lambda _: down("offline"))
    response = await api.post(
        f"/process-drafts/{draft['id']}/messages",
        json={"revision": draft["revision"], "message": "Explain this"},
    )
    assert response.status_code == 502
    assert (await api.get(f"/process-drafts/{draft['id']}")).json() == draft


@pytest.fixture
async def scripted_configuration(monkeypatch):
    original = configuration.agents

    async def capture(*args):
        settings = await original(*args)
        for value in settings.values():
            value["settings"]["model"] = None  # use the scripted model_for, like publish_fixture
        return settings

    monkeypatch.setattr(configuration, "agents", capture)


async def test_guidance_change_previews_without_recompiling_or_changing_decisions(
    api, monkeypatch, scripted_configuration
):
    proposal = plan()
    proposal["decision_review"] = {"guidance": "Explain concerns for a manager."}
    proposal["guidance"] = [
        {
            "name": "ownership",
            "text": "Use the recorded owner.",
            "evidence": [{"reference": "chat:1", "explanation": "Manager policy"}],
        }
    ]
    initial = await post(api, await prepare_new(api, monkeypatch, proposal), "publish")
    pid = initial["published_process_id"]
    decision_id = await add_decision(pid)
    before_process = (await api.get(f"/processes/{pid}")).json()
    before_rules = (await api.get(f"/processes/{pid}/rules")).json()
    draft = (await api.post("/process-drafts", json={"process_id": pid})).json()
    changed = copy.deepcopy(draft["plan"])
    changed["guidance"][0]["text"] = "Recommend escalation when ownership is uncertain."
    changed["guidance"][0]["evidence"] = [{"reference": "chat:1", "explanation": "Manager change"}]
    seen = {}
    monkeypatch.setattr(
        llm,
        "model_for",
        per_role(
            {
                "discovery": [changed],
                "decision_reviewer": [
                    {
                        "decision": "PAY",
                        "reasoning": "Recorded owner accepted",
                        "evidence": ["ownership"],
                    },
                    {
                        "decision": "REVIEW",
                        "reasoning": "Ownership is uncertain",
                        "evidence": ["ownership"],
                    },
                ],
            },
            seen,
        ),
    )
    draft = await post(api, draft, "messages", message="Propose escalation guidance for ownership.")
    assert [c["field"] for c in draft["changes"]] == ["guidance"]
    blocked = await api.post(
        f"/process-drafts/{draft['id']}/prepare", json={"revision": draft["revision"]}
    )
    assert blocked.status_code == 409
    draft = await post(api, await accept(api, draft), "prepare")
    assert draft["preview"]["valid"]
    preview = draft["preview"]["review"]["previews"][0]
    assert preview["before"]["assessment"]["decision"] == "PAY"
    assert preview["after"]["assessment"]["decision"] == "REVIEW"
    assert preview["final_decision"]["decision"] == "PAY"
    assert set(seen) == {"discovery", "decision_reviewer"}
    assert (await api.get(f"/processes/{pid}")).json() == before_process
    async with session_factory() as session:
        decision = await session.get(Decision, decision_id)
        assert decision.decision == "PAY"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(DecisionReview)
                .where(DecisionReview.decision_id == decision_id)
            )
            == 0
        )
    await post(api, draft, "publish")
    after_rules = (await api.get(f"/processes/{pid}/rules")).json()
    assert [r["id"] for r in after_rules] == [r["id"] for r in before_rules]
    after = (await api.get(f"/processes/{pid}")).json()
    assert after["active_version_id"] != before_process["active_version_id"]
    async with session_factory() as session:
        version = await session.get(ProcessVersion, after["active_version_id"])
        assert version.snapshot["guidance"]["ownership"] == changed["guidance"][0]["text"]
        assert version.validation["review"]["previews"] == [preview]


async def test_revision_clears_approval_and_reports_configuration_diff(api, monkeypatch):
    draft = await prepare_new(api, monkeypatch)
    changed = copy.deepcopy(draft["plan"])
    changed["description"] = "All values are expressed in cents."
    monkeypatch.setattr(llm, "model_for", per_role({"discovery": [changed]}))
    revised = await post(api, draft, "messages", message="Change the amount convention to cents.")
    assert revised["reviews"] == {} and revised["preview"] is None
    assert any(c["field"] == "description" for c in revised["changes"])


async def test_source_schema_change_recompiles_unchanged_rules(api, monkeypatch):
    proposal = plan()
    proposal["sources"] = [
        {
            "name": "parameters",
            "kind": "constant",
            "rows": [{"old": 1}],
            "explanation": "Manager parameter",
            "evidence": [{"reference": "chat:1", "explanation": "Manager parameter"}],
        }
    ]
    initial = await post(api, await prepare_new(api, monkeypatch, proposal), "publish")
    draft = (
        await api.post("/process-drafts", json={"process_id": initial["published_process_id"]})
    ).json()
    changed = copy.deepcopy(draft["plan"])
    changed["sources"][0] = copy.deepcopy(proposal["sources"][0])
    changed["sources"][0]["rows"] = [{"renamed": 1}]
    seen = {}
    monkeypatch.setattr(llm, "model_for", per_role(scripts(changed), seen))
    draft = await post(api, draft, "messages", message="Rename the parameter column.")
    draft = await post(api, await accept(api, draft), "prepare")
    assert draft["preview"]["valid"]
    assert "normalizer" in seen and "compiler" in seen
    assert "existing_rule_id" not in draft["preview"]["compilations"][0]


async def test_source_only_change_previews_both_evidence_sets(
    api, monkeypatch, scripted_configuration
):
    proposal = plan()
    proposal["rules"] = []
    proposal["examples"] = [e for e in proposal["examples"] if e["name"] != "large"]
    proposal["decision_review"] = {"guidance": "Escalate when the source risk is high."}
    proposal["sources"] = [
        {
            "name": "parameters",
            "kind": "constant",
            "rows": [{"risk": 1}],
            "explanation": "Manager parameter",
            "evidence": [{"reference": "chat:1", "explanation": "Manager parameter"}],
        }
    ]
    monkeypatch.setattr(llm, "model_for", per_role({"discovery": [proposal]}))
    draft = (await api.post("/process-drafts", json={})).json()
    draft = await post(api, draft, "messages", message="Set up a review-only process.")
    initial = await post(api, await post(api, await accept(api, draft), "prepare"), "publish")
    pid = initial["published_process_id"]
    await add_decision(pid)
    draft = (await api.post("/process-drafts", json={"process_id": pid})).json()
    changed = copy.deepcopy(draft["plan"])
    changed["sources"][0] = copy.deepcopy(proposal["sources"][0])
    changed["sources"][0]["rows"] = [{"risk": 2}]
    seen = {}
    monkeypatch.setattr(
        llm,
        "model_for",
        per_role(
            {
                "discovery": [changed],
                "decision_reviewer": [
                    {"decision": "PAY", "reasoning": "Low risk", "evidence": ["guidance"]},
                    {"decision": "REVIEW", "reasoning": "High risk", "evidence": ["guidance"]},
                ],
            },
            seen,
        ),
    )
    draft = await post(api, draft, "messages", message="Set the risk parameter to 2.")
    draft = await post(api, await accept(api, draft), "prepare")
    assert draft["preview"]["valid"] and draft["preview"]["compilations"] == []
    contexts = [user_json(messages) for messages in seen["decision_reviewer"]]
    assert [c["engine"]["decision"] for c in contexts] == ["PAY", "PAY"]
    source_rows = [
        next(v["rows"] for k, v in c["evidence"].items() if k.startswith("source:"))
        for c in contexts
    ]
    assert source_rows == [[{"risk": 1}], [{"risk": 2}]]
    assert draft["preview"]["review"]["previews"][0]["after"]["assessment"]["decision"] == "REVIEW"


async def test_norm_ingestion_reads_chat_published_schema_and_conventions(
    api, monkeypatch, scripted_configuration
):
    from app.features.agents import compiler, normalizer

    initial = await post(api, await prepare_new(api, monkeypatch), "publish")
    pid = initial["published_process_id"]
    draft = (await api.post("/process-drafts", json={"process_id": pid})).json()
    changed = plan(initial["plan"]["name"], field="value", review="ESCALATE")
    changed["description"] = "Values are cents; compare exactly."
    monkeypatch.setattr(llm, "model_for", per_role(scripts(changed, field="value")))
    draft = await post(api, draft, "messages", message="Rename amount to value and use ESCALATE.")
    await post(api, await post(api, await accept(api, draft), "prepare"), "publish")
    calls = []

    async def normalize(norm, description, types, symbols, sources, active, setup, **_):
        calls.append((description, {t.name for t in types}, {s.name for s in symbols}))
        return normalizer.Normalization(norm_rules=[]), None

    monkeypatch.setattr(normalizer, "normalize", normalize)
    async with session_factory() as session:
        description, _, _ = await compiler.read_process(session, pid)
        assert description == changed["description"]
        await normalizer.normalize_norm(session, pid, "Explain conventions")
    assert calls == [(changed["description"], {"PAY", "ESCALATE"}, {"value"})]
