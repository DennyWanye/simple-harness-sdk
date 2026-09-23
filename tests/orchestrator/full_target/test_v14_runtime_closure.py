"""Focused V1.4 runtime-closure regressions.

This module deliberately uses the production stores and original public/persisted
interfaces.  Provider calls and the broad regression suite belong to the parent
acceptance run, not test preparation.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_orchestrator.contracts.htn import MethodRef
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import RequestHumanDecision
from agent_orchestrator.evaluation.experiment import (
    ARMS,
    ArmSpec,
    ExecutionCounters,
    ExperimentBudget,
    ExperimentManifest,
    RunContext,
)
from agent_orchestrator.evaluation.htn_matrix import EpisodeReceipt
from agent_orchestrator.evaluation.htn_meter import DurableMeteredProvider
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.planning_repair_requests import (
    address_requests,
    pending_requests,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.htn.cross_domain_acceptance import FourArm, ScenarioKind
from agent_orchestrator.planning.htn.method_selection import (
    MethodSelectionCandidateV1,
    SelectionCallLedger,
    SelectionRoute,
    select_method,
)
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.method_evaluation_store import MethodEvaluationStore
from agent_orchestrator.storage.planning_human_store import PlanningHumanStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import StoreConflict
from agent_orchestrator.contracts.planning_decisions import PlanningDecisionStatus
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from simple_harness import Message, MessageRole
from simple_harness.contracts import RequestId
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderUsage,
)

from test_htn_end_to_end import build_world
from test_h1i_production_entry import _config, _events, _open_planner_round, _seed_new_protocol


def _candidate(name: str) -> MethodSelectionCandidateV1:
    return MethodSelectionCandidateV1(method_id=name, report="APPLICABLE")


def test_h3_one_candidate_is_local_but_many_reserve_one_selection_identity() -> None:
    one = select_method([_candidate("only")], plan_revision=4, evidence_epoch=9)
    ledger = SelectionCallLedger()
    many = select_method(
        [_candidate("b"), _candidate("a")],
        plan_revision=4,
        evidence_epoch=9,
        ledger=ledger,
        subject_id="occ-1",
    )
    replay = select_method(
        [_candidate("a"), _candidate("b")],
        plan_revision=4,
        evidence_epoch=9,
        ledger=SelectionCallLedger.from_json(ledger.to_json()),
        subject_id="occ-1",
    )
    assert (one.route, one.selected_method_id) == (SelectionRoute.DETERMINISTIC, "only")
    assert many.route is SelectionRoute.MODEL_REFINE
    assert replay.route is SelectionRoute.SELECTION_ALREADY_ATTEMPTED
    assert replay.identity == many.identity


def test_h3_selection_identity_changes_for_a_different_occurrence() -> None:
    candidates = [_candidate("a"), _candidate("b")]
    left = select_method(candidates, plan_revision=1, evidence_epoch=2, subject_id="occ-1")
    right = select_method(candidates, plan_revision=1, evidence_epoch=2, subject_id="occ-2")
    assert left.identity.candidate_set_digest != right.identity.candidate_set_digest


def test_h3_thirteenth_candidate_is_part_of_identity_even_when_prompt_view_caps_at_twelve() -> None:
    first = [_candidate(f"method-{index:02d}") for index in range(13)]
    changed = [*first[:12], _candidate("method-13-replaced")]
    before = select_method(first, plan_revision=7, evidence_epoch=3, subject_id="occ")
    after = select_method(changed, plan_revision=7, evidence_epoch=3, subject_id="occ")
    assert len(before.applicable) == 13
    assert before.identity.candidate_set_digest != after.identity.candidate_set_digest


def test_v14_malformed_planning_reply_records_unreadable_without_secondary_failure(tmp_path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="v14-malformed-reply"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            await loop._collect_plan_decision(
                opener, object(), mission,
                "<planning_decision>{not-json}</planning_decision>", dispatch,
            )
            stored = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                opener.intent_id, 0
            )
            evaluated = _events(loop, mission.id, "PlanningDecisionEvaluated")[-1]
            assert stored is not None
            assert stored["status"] == str(PlanningDecisionStatus.UNREADABLE)
            assert stored["canonical_hash"] is None
            assert evaluated.payload["status"] == str(PlanningDecisionStatus.UNREADABLE)
            assert evaluated.payload["canonical_hash"] is None
    asyncio.run(case())


def _human_store(tmp_path, key: str):
    world = build_world(tmp_path, key=key)
    htn = HtnStore(world.store)
    plan = htn.active_plan_revision(world.mission.id)
    requirements = htn.latest_requirements_revision(world.mission.id)
    binding = {
        "plan_revision": 0 if plan is None else plan.revision,
        "requirements_revision": 0 if requirements is None else requirements.revision,
        "manager_epoch": htn.epoch(world.mission.id, "mission"),
    }
    return world, PlanningHumanStore(world.store), binding


def test_h4_nonblocking_question_is_durable_without_pausing_dispatch(tmp_path) -> None:
    world, humans, binding = _human_store(tmp_path, "v14-human-nonblocking")
    humans.register(
        decision_id="decision-1",
        mission_id=world.mission.id,
        subject_key="subject-1",
        payload=RequestHumanDecision("Add context?", (), False),
        request_binding=binding,
        next_ordinal=2,
    )
    assert humans.pending(world.mission.id) is False
    assert humans.get("decision-1")["state"] == "PENDING"


def test_h4_blocking_question_pauses_and_authenticated_answer_is_cas_idempotent(tmp_path) -> None:
    world, humans, binding = _human_store(tmp_path, "v14-human-blocking")
    humans.register(
        decision_id="decision-2",
        mission_id=world.mission.id,
        subject_key="subject-2",
        payload=RequestHumanDecision("Choose", (), True),
        request_binding=binding,
        next_ordinal=3,
    )
    assert humans.pending(world.mission.id) is True
    receipt = humans.answer(
        decision_id="decision-2",
        tenant_id=world.mission.tenant_id,
        principal=Principal("operator"),
        answer="continue",
        expected_version=1,
        nonce="nonce-1",
    )
    assert humans.pending(world.mission.id) is False
    assert humans.answer(
        decision_id="decision-2",
        tenant_id=world.mission.tenant_id,
        principal=Principal("operator"),
        answer="continue",
        expected_version=1,
        nonce="nonce-1",
    ) == receipt


def test_h4_stale_question_cannot_be_answered(tmp_path) -> None:
    world, humans, binding = _human_store(tmp_path, "v14-human-stale")
    humans.register(
        decision_id="decision-stale",
        mission_id=world.mission.id,
        subject_key="subject",
        payload=RequestHumanDecision("Old question", (), True),
        request_binding={**binding, "manager_epoch": binding["manager_epoch"] + 1},
        next_ordinal=2,
    )
    humans.retire_stale(world.mission.id)
    assert humans.get("decision-stale")["state"] == "STALE"
    with pytest.raises(StoreConflict, match="version changed"):
        humans.answer(
            decision_id="decision-stale",
            tenant_id=world.mission.tenant_id,
            principal=Principal("operator"),
            answer="continue",
            expected_version=2,
            nonce="nonce",
        )


def test_h4_repair_request_is_consumed_only_by_committed_same_subject(tmp_path) -> None:
    world = build_world(tmp_path, key="v14-repair-address")
    subject = {"subject_key": "s-a", "occurrence_id": "occ-a", "task_id": "task-a"}
    package = {
        "planning_subjects": [subject, {"subject_key": "s-b", "occurrence_id": "occ-b"}],
        "repair_requests": [{
            "request_id": "repair-1",
            "impact": {"revalidate": ["occ-a"], "supersede": [], "new_work": []},
        }],
    }
    from agent_orchestrator.orchestrator.hierarchical_dispatch import append_hierarchical_event
    append_hierarchical_event(
        world.store,
        "PlanningRepairRequested",
        world.mission.id,
        key="source-1",
        payload={"source_key": "source-1", "request_id": "repair-1", "request": {}, "impact": {}},
    )
    address_requests(world.store, world.mission.id, package=package, decision_id="d1",
                     decision_type="REFINE", status="COMMIT_REJECTED", subject_key="s-a")
    address_requests(world.store, world.mission.id, package=package, decision_id="d2",
                     decision_type="REFINE", status="COMMITTED", subject_key="s-b")
    assert [item["request_id"] for item in pending_requests(world.store, world.mission.id)] == ["repair-1"]
    address_requests(world.store, world.mission.id, package=package, decision_id="d3",
                     decision_type="REFINE", status="COMMITTED", subject_key="s-a")
    assert pending_requests(world.store, world.mission.id) == []


def test_h6_evaluation_refuses_to_invent_numbers_for_a_nonterminal_mission(tmp_path) -> None:
    world = build_world(tmp_path, key="v14-h6-no-fabrication")
    reference = MethodRef("missing.method", 1, "a" * 64)
    with pytest.raises(StoreConflict, match="not terminal"):
        MethodEvaluationStore(world.store)._run(world.mission.id, reference)


TARGET = ProviderTarget("fake", "fixed-model", "pricing", "https://example.test/v1", "v1")


class _Provider:
    target = TARGET

    async def invoke(self, request, *, cancel):
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "ok"),
            usage=ProviderUsage(4, 2, 6),
            model=TARGET.model,
        )


def _meter_context():
    manifest = ExperimentManifest(
        "v14-meter", "fake", "fixed-model", ExperimentBudget(20, 10, 30, 3, 30),
        ("case",), 1, 1, 1, tuple(ArmSpec(arm, arm) for arm in ARMS),
    )
    snapshots = []
    return RunContext(manifest, manifest.runs()[0], snapshots.append), snapshots


def _request(name: str) -> ProviderRequest:
    return ProviderRequest(RequestId(name), (Message(MessageRole.USER, "hello"),), max_output_tokens=3)


def test_h8_durable_meter_restores_actual_calls_and_remaining_budget(tmp_path) -> None:
    async def case() -> None:
        context, _ = _meter_context()
        path = tmp_path / "meter.json"
        first = DurableMeteredProvider(_Provider(), context, estimate_input_tokens=lambda _: 5,
                                       checkpoint=path, run_identity="run-1")
        await first.invoke(_request("request-1"), cancel=CancelToken())
        restored = DurableMeteredProvider(_Provider(), context, estimate_input_tokens=lambda _: 5,
                                          checkpoint=path, run_identity="run-1", resume=True)
        assert restored.counters == first.counters
        await restored.invoke(_request("request-2"), cancel=CancelToken())
        assert restored.counters.calls == 2
        assert [row["ordinal"] for row in restored.observations] == [1, 2]
    asyncio.run(case())


def test_h8_durable_meter_requires_explicit_recovery_and_matching_identity(tmp_path) -> None:
    context, _ = _meter_context()
    path = tmp_path / "meter.json"
    DurableMeteredProvider(_Provider(), context, estimate_input_tokens=lambda _: 5,
                           checkpoint=path, run_identity="run-1")
    with pytest.raises(ContractError, match="explicit recovery"):
        DurableMeteredProvider(_Provider(), context, estimate_input_tokens=lambda _: 5,
                               checkpoint=path, run_identity="run-1")
    with pytest.raises(ContractError, match="identity or content changed"):
        DurableMeteredProvider(_Provider(), context, estimate_input_tokens=lambda _: 5,
                               checkpoint=path, run_identity="other", resume=True)


def test_h8_started_call_recovery_closes_meter_instead_of_resetting_budget(tmp_path) -> None:
    context, _ = _meter_context()
    path = tmp_path / "meter.json"
    meter = DurableMeteredProvider(_Provider(), context, estimate_input_tokens=lambda _: 5,
                                   checkpoint=path, run_identity="run-1")
    envelope = json.loads(path.read_text())
    envelope["state"]["counters"]["calls"] = 1
    envelope["state"]["observations"] = [{"ordinal": 1, "status": "started"}]
    from agent_orchestrator.contracts.semantic_base import content_hash_of
    envelope["sha256"] = content_hash_of(envelope["state"])
    path.write_text(json.dumps(envelope))
    restored = DurableMeteredProvider(_Provider(), context, estimate_input_tokens=lambda _: 5,
                                      checkpoint=path, run_identity="run-1", resume=True)
    assert restored.counters.calls == 1
    assert restored.unknown_usage_calls == 1
    assert restored._closed is True


@pytest.mark.parametrize("kind", [ScenarioKind.REPAIR, ScenarioKind.EVIDENCE_CONFLICT, ScenarioKind.RECOVERY])
def test_h8_non_normal_episode_cannot_pass_without_triggered_intervention(kind) -> None:
    budget = ExperimentBudget(20, 10, 30, 3, 30)
    manifest = SimpleNamespace(budget=budget, physical_slots=1)
    run = SimpleNamespace(scenario=SimpleNamespace(kind=kind), arm=FourArm.STRONG_SINGLE_AGENT)
    receipt = EpisodeReceipt(
        "run", "manifest", "COMPLETED", True,
        SimpleNamespace(), SimpleNamespace(), "fake", "fixed-model", "tools",
        ExecutionCounters("fake", "fixed-model", 4, 2, 6, 1, 1),
        0, 0, 0, 1.0, None, intervention_triggered=False,
    )
    assert receipt.passed(manifest, run) is False
