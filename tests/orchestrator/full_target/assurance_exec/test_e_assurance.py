# SPDX-License-Identifier: Apache-2.0
"""E group (effects / operation completion): plan cases E01–E08.

Every case runs the real operation completion chain from ``_operation_world``:
Spec approval, frozen single-root MIXED scope, the production content
acceptance (preparation), T0 submit, official ACTION_PROPOSAL review, T1
materialisation and human approval, the production dispatcher over the real
``ActionExecutor`` + ``FilePublishConnector``, T3 outcome preparation, the
official OPERATION_OUTCOME review and ``accept_operation_outcome``. Reviews are
scripted service intents; no model, no Host. Where a case's plan text reaches
beyond what this deterministic world can drive (a fresh plan revision, the
Mission-root resolution writer) the docstring says so.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "operation_completion"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts/assurance_seams"))

from _operation_world import OperationWorld, completion_command, insert_requirements  # noqa: E402
from test_completion_pending_stall import _pending_stall_loop  # noqa: E402
from test_completion_spec_approval import _api  # noqa: E402

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.resolution import DeliveryReceipt, DeliveryStage
from agent_orchestrator.contracts.state_machines import MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.completion_status import (
    read_current_effect,
    read_occurrence_completion,
)
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.orchestrator.operation_completion import OperationCompletionError
from agent_orchestrator.orchestrator.operation_outcomes import OperationOutcomeError
from agent_orchestrator.orchestrator.scoped_content_review import uses_completion_protocol
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore

TWO_EFFECTS = (("deliver-report", "criterion-delivered", "reports/final.json"),
               ("deliver-summary", "criterion-archived", "reports/summary.json"))


def _code(error):
    return getattr(error, "code", None) or str(error)


def _ledger_snapshot(w):
    ledger = w.root / "publish-ledger"
    return sorted((p.relative_to(ledger).as_posix(), p.read_bytes()) for p in ledger.rglob("*") if p.is_file())


def _rows(w):
    return {t: w.count(f"SELECT COUNT(*) FROM {t} WHERE mission_id=?", w.mission_id) for t in (
        "acceptances", "delivery_receipts", "operation_outcome_review_bindings", "operation_acceptance_scopes",
        "actions", "events", "tasks", "attempts")}


# --------------------------------------------------------------------------- E01
def test_completion_spec_before_intent(tmp_path):
    w = OperationWorld(tmp_path / "spec")
    w.produce_and_accept()
    # The Spec + Scope are approved and frozen before any OperationId exists; the
    # content preparation is accepted; nothing waits for the Mission to finish.
    status = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert status.preparation_ready and status.content_ready and not status.effects_ready and not status.complete
    assert status.scope.required_effect_keys == ("deliver-report",) and status.scope.content_criterion_ids == ("criterion-report",)
    effect = read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-report")
    assert effect == {"state": "AWAITING_INTENT", "effect_key": "deliver-report", "complete": False}
    assert w.count("SELECT COUNT(*) FROM operation_intent_bindings WHERE mission_id=?", w.mission_id) == 0
    mission = w.store.get_mission(w.mission_id)
    assert mission.status is MissionStatus.ACTIVE and w.store.get_task(w.task.id).status is TaskStatus.VERIFYING
    # T0 and the proposal review run against that frozen Spec, still with no loop
    # and no Mission completion prerequisite.
    w.submit("deliver-report")
    action = w.materialize("deliver-report", approve=False)
    assert action["state"] == "AWAITING_APPROVAL"
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-report")["state"] == "AWAITING_APPROVAL"
    loop = _pending_stall_loop(w.world, tmp_path / "loop")
    before = _rows(w)
    asyncio.run(loop._record_hierarchical_stall())
    assert w.mission_id not in loop._stalled_at
    assert asyncio.run(loop._confirm_and_stop_stalled()) is False
    assert _rows(w) == before and w.store.get_mission(w.mission_id).status is MissionStatus.ACTIVE
    assert not w.dispatch()  # an unapproved action is never sent
    assert w.published_files() == []
    # Without an approved Spec the new lane refuses to publish a plan at all: it
    # is never silently downgraded to CONTENT_ONLY.
    with pytest.raises(OperationCompletionError) as refused:
        OperationWorld(tmp_path / "nospec", approve_spec=False, key="assurance-exec-nospec")
    assert refused.value.code == "OP_REQUIREMENT_MAPPING_MISSING"


# --------------------------------------------------------------------------- E02
def test_profile_does_not_choose_milestone(tmp_path):
    w = OperationWorld(tmp_path, milestone="DELIVERED")
    w.produce_and_accept()
    spec_row = w.store.connection.execute(
        "SELECT spec_hash, document_json FROM operation_completion_specs WHERE mission_id=?", (w.mission_id,)).fetchall()
    assert len(spec_row) == 1
    # The user requires DELIVERED; the built-in profile only reaches FILE_PUBLISHED /
    # CONTENT_HASH_VERIFIED. T0 says so explicitly instead of downgrading.
    with pytest.raises(ContractError) as refused:
        w.submit("deliver-report")
    assert _code(refused.value) == "OP_CAPABILITY_UNSUPPORTED"
    assert w.count("SELECT COUNT(*) FROM operation_intent_bindings WHERE mission_id=?", w.mission_id) == 0
    assert w.count("SELECT COUNT(*) FROM actions WHERE mission_id=?", w.mission_id) == 0
    assert w.store.connection.execute(
        "SELECT spec_hash, document_json FROM operation_completion_specs WHERE mission_id=?", (w.mission_id,)).fetchall() == spec_row
    status = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert not status.effects_ready and not status.complete
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-report")["state"] == "AWAITING_INTENT"
    assert HtnStore(w.store).list_delivery_receipts(w.mission_id) == ()


# --------------------------------------------------------------------------- E03
def test_exact_outcome_chain(tmp_path):
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    w.submit("deliver-report")
    w.materialize("deliver-report")
    action = w.execute("deliver-report")
    assert action["state"] == "SUCCEEDED" and len(w.published_files()) == 1
    assert not w.dispatch()  # the dispatcher never re-sends a succeeded action
    prepared = w.prepare_outcome("deliver-report")
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-report")["state"] in (
        "AWAITING_OUTCOME_REVIEW", "AWAITING_OUTCOME_ACCEPTANCE")
    record = w.review_outcome(prepared)
    assert str(record.verdict) == "ACCEPT"
    before = _rows(w)
    receipt = w.accept_outcome(prepared.binding_id)
    after = _rows(w)
    # One atomic step: effect Acceptance + Contribution + PERSISTED Delivery + events.
    assert receipt.acceptance.acceptance_id == "acc-" + prepared.binding_id
    assert after["acceptances"] == before["acceptances"] + 1
    assert after["operation_acceptance_scopes"] == before["operation_acceptance_scopes"] + 1
    assert after["delivery_receipts"] == before["delivery_receipts"] + 1
    delivery = HtnStore(w.store).list_delivery_receipts(w.mission_id)[0]
    assert delivery.stage is DeliveryStage.PERSISTED and delivery.operation_id is not None
    assert str(delivery.acceptance_id) == "acc-" + prepared.binding_id
    assert w.events()[-2:] == ["TaskCompleted", "OperationOutcomeAccepted"]
    status = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert status.effects_ready and status.complete
    # Replay of the same official outcome writes nothing new.
    replay = w.accept_outcome(prepared.binding_id)
    assert replay.replayed and _rows(w) == after
    # The root still needs its own composition/review: no goal resolution, no
    # Mission completion from the effect alone.
    assert w.count("SELECT COUNT(*) FROM goal_resolutions WHERE mission_id=?", w.mission_id) == 0
    assert w.store.get_mission(w.mission_id).status is MissionStatus.ACTIVE
    assert "MissionCompleted" not in w.events()


# --------------------------------------------------------------------------- E04
def test_outcome_identity_variants(tmp_path):
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    w.submit("deliver-report")
    w.materialize("deliver-report")
    action = w.execute("deliver-report")
    prepared = w.prepare_outcome("deliver-report")
    w.review_outcome(prepared)
    original_read = OperationCompletionStore.get_outcome_binding_exact
    genuine = original_read(OperationCompletionStore(w.store), w.mission_id, prepared.binding_id)
    document = genuine["document"]
    variants = {
        "operation_id": "op-other", "operation_occurrence_id": "occ-other", "action_version": document.action_version + 1,
        "link_hash": "1" * 64, "effect_contract_hash": "2" * 64, "namespace_hash": "3" * 64,
        "target_identity_hash": "4" * 64, "request_hash": "5" * 64, "params_hash": "6" * 64,
        "candidate_file_hash": "7" * 64, "parameters_content_hash": "8" * 64, "connector_profile_hash": "9" * 64,
        "spec_hash": "a" * 64, "completion_scope_id": "scope-other", "observed_milestone": "DELIVERED",
        "covered_handoff_ids": ("handoff-forged",), "action_key": "action-other", "intent_id": "intent-other",
        "effect_key": "deliver-other",
    }
    before = _rows(w)
    action_row = w.store.get_action(action["action_key"])
    for field, value in variants.items():
        forged = dataclasses.replace(document, **{field: value})

        def read(self, mission_id, binding_id, _forged=forged):
            row = original_read(self, mission_id, binding_id)
            return None if row is None else {**row, "document": _forged}

        with patch.object(OperationCompletionStore, "get_outcome_binding_exact", read):
            with pytest.raises(Exception) as refused:
                w.accept_outcome(prepared.binding_id)
        assert _code(refused.value) not in (None, ""), field
        assert _rows(w) == before, field
        assert not w.store.connection.in_transaction
    # The real execution facts are untouched by every refusal.
    assert w.store.get_action(action["action_key"]) == action_row and action_row["state"] == "SUCCEEDED"
    assert len(w.published_files()) == 1
    # A non-empty operation id is not sufficient by itself: the genuine chain accepts once.
    receipt = w.accept_outcome(prepared.binding_id)
    assert not receipt.replayed and read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete


# --------------------------------------------------------------------------- E05
def test_multiple_effects_single_receipt(tmp_path):
    w = OperationWorld(tmp_path, effects=TWO_EFFECTS)
    w.produce_and_accept()
    status = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert status.scope.required_effect_keys == ("deliver-report", "deliver-summary")
    w.submit("deliver-report")
    w.materialize("deliver-report")
    w.execute("deliver-report")
    prepared_a = w.prepare_outcome("deliver-report")
    w.review_outcome(prepared_a)
    # A's OutcomeBinding cloned onto B's slot is refused: B needs its own chain.
    original_read = OperationCompletionStore.get_outcome_binding_exact
    genuine = original_read(OperationCompletionStore(w.store), w.mission_id, prepared_a.binding_id)
    cloned = dataclasses.replace(genuine["document"], effect_key="deliver-summary")

    def read(self, mission_id, binding_id):
        row = original_read(self, mission_id, binding_id)
        return None if row is None else {**row, "document": cloned}

    before = _rows(w)
    with patch.object(OperationCompletionStore, "get_outcome_binding_exact", read):
        with pytest.raises(Exception) as refused:
            w.accept_outcome(prepared_a.binding_id)
    assert _code(refused.value) not in (None, "")
    assert _rows(w) == before
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-summary")["state"] == "AWAITING_INTENT"
    # A's genuine chain completes A only.
    w.accept_outcome(prepared_a.binding_id)
    status = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert not status.effects_ready and not status.complete  # A alone never completes B
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-report")["complete"]
    pending = read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-summary")
    assert pending == {"state": "AWAITING_INTENT", "effect_key": "deliver-summary", "complete": False}
    assert w.store.get_task(w.task.id).status is TaskStatus.VERIFYING
    # Replaying the clone against the now-accepted A is a no-op, never a B completion.
    with patch.object(OperationCompletionStore, "get_outcome_binding_exact", read):
        replay = w.accept_outcome(prepared_a.binding_id)
    assert replay.replayed
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-summary")["state"] == "AWAITING_INTENT"
    # B waits visibly at each of its own stages, then completes with its own chain.
    w.submit("deliver-summary")
    action = w.materialize("deliver-summary", approve=False)
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-summary")["state"] == "AWAITING_APPROVAL"
    assert not w.dispatch() and len(w.published_files()) == 1
    w.approve_action("deliver-summary")
    w.execute("deliver-summary")
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-summary")["state"] == "AWAITING_OUTCOME_REVIEW"
    prepared_b = w.prepare_outcome("deliver-summary")
    assert prepared_b.binding_id != prepared_a.binding_id
    w.review_outcome(prepared_b)
    w.accept_outcome(prepared_b.binding_id)
    status = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert status.effects_ready and status.complete and len(w.published_files()) == 2
    assert w.store.get_task(w.task.id).status is TaskStatus.COMPLETED
    assert w.count("SELECT COUNT(*) FROM delivery_receipts WHERE mission_id=?", w.mission_id) == 2
    del action


# --------------------------------------------------------------------------- E06
def test_late_fact_new_requirement(tmp_path):
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    w.submit("deliver-report")
    w.materialize("deliver-report")
    action = w.execute("deliver-report")
    published = w.published_files()
    ledger_before = _ledger_snapshot(w)
    # The user's requirements move after the action happened.
    requirements_2 = insert_requirements(w.world, w.effects, revision=2)
    before = _rows(w)
    with pytest.raises(ContractError) as refused:
        w.prepare_outcome("deliver-report")
    assert _code(refused.value) in {"OP_REQUIREMENT_MAPPING_MISSING", "OP_EFFECT_SCOPE_STALE"}
    assert _rows(w) == before
    # The original fact and its cost stay exactly as recorded; nothing is re-sent.
    assert w.store.get_action(action["action_key"]) == action and action["state"] == "SUCCEEDED"
    assert not w.dispatch() and w.published_files() == published
    assert _ledger_snapshot(w) == ledger_before
    # The old preparation acceptance does not satisfy the new requirement.
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-report")["state"] == "SCOPE_STALE"
    with pytest.raises(OperationCompletionError) as stale:
        read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert stale.value.code == "OP_EFFECT_SCOPE_STALE"
    # A new Spec for the amended requirements is a new approval, not a rewrite of
    # the old one; the frozen plan-1 scope does not adopt it, so the original
    # fact can only be re-reviewed under a new plan revision (not driven here).
    approved_2 = _api(w.world).approve(completion_command(
        requirements_2, command_id="confirm-completion-2", milestone=w.milestone, effects=w.effects,
        profiles=w.profiles))
    assert approved_2.spec_hash != w.approved.spec_hash
    assert w.count("SELECT COUNT(*) FROM operation_completion_specs WHERE mission_id=?", w.mission_id) == 2
    assert read_current_effect(w.store, w.mission_id, approved_2.spec_hash, "deliver-report")["state"] == "SCOPE_STALE"
    with pytest.raises(ContractError) as refused:
        w.prepare_outcome("deliver-report")
    assert _code(refused.value) == "OP_EFFECT_SCOPE_STALE"
    assert w.published_files() == published and _rows(w)["actions"] == before["actions"]


# --------------------------------------------------------------------------- E07
def test_delivery_writer_bypass(tmp_path):
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    htn = HtnStore(w.store)
    acceptance_id = str(w.acceptance.acceptance_id)
    now_ms = int(w.store.now * 1000)
    before = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert before.preparation_ready and not before.effects_ready
    # A bare SENT receipt written straight into the delivery table, and PERSISTED /
    # ENQUEUED notifications, are claims about the world: none of them is the
    # effect's proof chain, so the effect stays owed.
    for stage, operation in ((DeliveryStage.SENT, "op-claimed"), (DeliveryStage.PERSISTED, None),
                             (DeliveryStage.ENQUEUED, None)):
        with w.store.transaction():
            htn.record_delivery_receipt(
                w.mission_id, DeliveryReceipt(f"bypass-{stage.lower()}", w.mission_id, acceptance_id, stage, now_ms,
                                              operation), command_id=f"bypass-{stage.lower()}", intent_hash="0" * 64)
    assert len(htn.list_delivery_receipts(w.mission_id)) == 3
    status = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert status.preparation_ready and not status.effects_ready and not status.complete
    assert read_current_effect(w.store, w.mission_id, w.approved.spec_hash, "deliver-report")["state"] == "AWAITING_INTENT"
    dispatch = HierarchicalDispatch(w.store, w.commit)
    assert not dispatch.terminal(w.mission_id) and not dispatch.root_review_ready(w.mission_id)
    assert w.store.get_task(w.task.id).status is TaskStatus.VERIFYING
    # The T3 writer without its complete source chain refuses.
    with pytest.raises(OperationOutcomeError) as refused:
        w.accept_outcome("delivery-bypass")
    assert refused.value.code == "OP_OUTCOME_SOURCE_UNAVAILABLE"
    with pytest.raises(OperationOutcomeError) as refused:
        w.accept_outcome("acc-" + acceptance_id)
    assert refused.value.code == "OP_OUTCOME_SOURCE_UNAVAILABLE"
    # Only the genuine chain closes the effect, and it names its own operation.
    prepared, _, receipt = w.complete_effect("deliver-report")
    genuine = htn.list_delivery_receipts(w.mission_id, acceptance_id=str(receipt.acceptance.acceptance_id))
    assert [d.stage for d in genuine] == [DeliveryStage.PERSISTED] and genuine[0].operation_id is not None
    assert read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete


# --------------------------------------------------------------------------- E08
def test_content_legacy_compatibility(tmp_path):
    from _assured_fixture import AssuredRuntime
    from _deploy import TENANT, deployment

    from agent_orchestrator.orchestrator.commit_service import MissionSpec
    from agent_orchestrator.orchestrator.operation_runtime import advance_operation_outcomes
    from agent_orchestrator.storage.assurance_store import AssuranceStore

    reply = {"schema_version": 2, "verdict": "ACCEPT", "assessments": [
        {"criterion_id": "criterion-report", "verdict": "PASS", "evidence_ids": [], "reason": "fixture",
         "limitations": []}], "findings": []}

    async def content_only():
        async with AssuredRuntime(tmp_path / "content", [reply], content_only=True) as rt:
            store, mission_id = rt.store, rt.mission.id
            verdict, record = await rt.run_critic()
            rt.record_critic_layer(record)
            rt.settle_fixture_worker()
            completed = rt.accept_now()
            assert completed.accepted_result_id == rt.stored.envelope.id
            status = read_occurrence_completion(store, mission_id, str(
                HierarchicalDispatch(store, rt.commit).network(mission_id).root_occurrence_ids[0]))
            assert str(status.scope.role) == "CONTENT"
            assert status.scope.required_effect_keys == () and status.complete
            # A pure report never invents an action: no intent, no action, no delivery.
            for table in ("operation_intent_bindings", "actions", "delivery_receipts", "operation_outcome_review_bindings"):
                assert store.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE mission_id=?",
                                                (mission_id,)).fetchone()[0] == 0, table
            # An operation intent against a CONTENT_ONLY scope is refused (no effect slot).
            from agent_orchestrator.contracts.operation_intents import SubmitOperationIntentV2
            from agent_orchestrator.contracts.semantic_base import (
                Provenance,
                TypedRef,
                TypedRefKind,
                content_hash_of,
            )
            spec_hash = store.connection.execute(
                "SELECT spec_hash FROM operation_completion_specs WHERE mission_id=?", (mission_id,)).fetchone()[0]
            acceptance = HtnStore(store).list_acceptances(mission_id)[0]
            command = SubmitOperationIntentV2.from_json({
                "schema_version": 2, "mission_id": mission_id, "idempotency_key": "content-only-publish",
                "intent_source": {"kind": "USER_COMMAND"},
                "candidate_artifact_ref": TypedRef(TypedRefKind.ARTIFACT, rt.artifact.id, rt.artifact.version,
                                                   rt.artifact.content_hash, Provenance.TOOL).to_json(),
                "prepared_acceptance_refs": [TypedRef(TypedRefKind.ACCEPTANCE, str(acceptance.acceptance_id), 1,
                                                      content_hash_of(acceptance.to_json()), Provenance.TOOL).to_json()],
                "supersedes_intent_id": None,
                "completion_slot": {"spec_hash": spec_hash, "effect_key": "deliver-report"}})
            with pytest.raises(Exception) as refused:
                rt.commit.submit_operation_intent(command, tenant_id=rt.mission.tenant_id,
                                                  principal=__import__("agent_orchestrator.governance.permissions",
                                                                       fromlist=["Principal"]).Principal("owner"))
            assert _code(refused.value)
            assert store.connection.execute("SELECT COUNT(*) FROM operation_intent_bindings WHERE mission_id=?",
                                            (mission_id,)).fetchone()[0] == 0

    async def legacy():
        async with deployment(tmp_path / "legacy") as world:
            created = world.commit.create_mission(MissionSpec(
                goal="legacy report", success_criteria=("a report exists",), tenant_id=TENANT,
                idempotency_key="legacy-e08"))
            mission = created[0] if isinstance(created, tuple) else created
            assert not uses_completion_protocol(world.store, mission.id)
            assert AssuranceStore(world.store).lane(mission.id) != "ASSURANCE_1_1"  # original protocol
            events = [e.to_json() for e in world.store.list_events(mission.id)]
            rows = {t: world.store.connection.execute(f"SELECT COUNT(*) FROM {t} WHERE mission_id=?",
                                                      (mission.id,)).fetchone()[0]
                    for t in ("operation_completion_specs", "operation_completion_scopes", "operation_intent_bindings",
                              "actions", "assurance_mission_bindings")}
            assert set(rows.values()) == {0}
            # The new machinery reads a legacy Mission as "nothing to do" and writes nothing.
            assert advance_operation_outcomes(world.orch, mission.id) is False
            assert [e.to_json() for e in world.store.list_events(mission.id)] == events
            assert world.store.get_mission(mission.id).to_json() == mission.to_json()

    asyncio.run(content_only())
    asyncio.run(legacy())
