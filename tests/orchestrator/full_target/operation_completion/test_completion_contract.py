"""Strict V1.4 completion documents before any outcome/import path exists.

OCC-02 source: §2.1–§2.2 and §3.1 of the Operation Completion addendum.  These
are contract-boundary checks: malformed documents must never reach the future
requirement approval, plan compiler, T0, or T3 writers.  OCC-01/OCC-03–OCC-12
remain deliberately absent here: their production seams require the actual
acceptance/outcome/resolution producers, and are not represented as skips.
"""

from __future__ import annotations

import json

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.operation_completion import (
    MAX_COMPLETION_BYTES,
    AcceptanceContributionScopeV1,
    OccurrenceCompletionScopeV1,
    OperationCompletionRequirementsV1,
    OperationOutcomeReviewBindingV1,
    validate_scope_owners,
    validate_spec_scope_coverage,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _pin(identifier: str, revision: int, content_hash: str) -> dict[str, object]:
    return {"id": identifier, "revision": revision, "content_hash": content_hash}


def _required_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "requirements_ref": _pin("requirements-1", 1, HASH_A),
        "mode": "REQUIRED_EFFECTS",
        "content_criterion_ids": ["criterion-report"],
        "effects": [
            {
                "effect_key": "deliver-report",
                "obligation_id": "obligation-root",
                "criterion_ids": ["criterion-delivered"],
                "required_milestone": "DELIVERED",
                "milestone_policy_ref": _pin("milestone-policy", 1, HASH_B),
                "evidence_policy_ref": _pin("evidence-policy", 1, HASH_C),
                "source_slot_key": "approved-delivery-slot",
            }
        ],
    }


def _aggregate_scope(*, owned: list[str] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "requirements_ref": _pin("requirements-1", 1, HASH_A),
        "spec_hash": OperationCompletionRequirementsV1.from_json(_required_spec()).content_hash(),
        "plan_ref": {"revision": 1, "snapshot_hash": HASH_D},
        "occurrence_id": "occurrence-root",
        "task_ref": _pin("task-root", 1, HASH_D),
        "obligation_id": "obligation-root",
        "role": "AGGREGATE",
        "content_criterion_ids": ["criterion-report"],
        "required_effect_keys": ["deliver-report"],
        "owned_effect_keys": ["deliver-report"] if owned is None else owned,
    }


def _content_scope() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "requirements_ref": _pin("requirements-1", 1, HASH_A),
        "spec_hash": OperationCompletionRequirementsV1.from_json(_required_spec()).content_hash(),
        "plan_ref": {"revision": 1, "snapshot_hash": HASH_D},
        "occurrence_id": "occurrence-report",
        "task_ref": _pin("task-report", 1, HASH_B),
        "obligation_id": "obligation-root",
        "role": "CONTENT",
        "content_criterion_ids": ["criterion-report"],
        "required_effect_keys": [],
        "owned_effect_keys": [],
    }


def _content_contribution() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "acceptance_id": "acceptance-content",
        "completion_scope_id": "scope-content",
        "spec_hash": HASH_A,
        "kind": "CONTENT",
        "content_criterion_ids": ["criterion-report"],
        "effect_keys": [],
        "output_artifact_refs": [],
        "outcome_binding_id": None,
        "delivery_receipt_ref": None,
    }


def _outcome_binding() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "spec_hash": HASH_A,
        "effect_key": "deliver-report",
        "completion_scope_id": "scope-root",
        "intent_id": "intent-deliver-report",
        "operation_id": "operation-deliver-report",
        "operation_occurrence_id": "operation-occurrence-deliver-report",
        "action_key": "deliver-report",
        "action_version": 1,
        "request_hash": HASH_A,
        "candidate_file_hash": HASH_B,
        "parameters_content_hash": HASH_C,
        "params_hash": HASH_D,
        "effect_contract_hash": HASH_A,
        "link_hash": HASH_B,
        "connector_profile_hash": HASH_C,
        "namespace_hash": HASH_D,
        "target_identity_hash": HASH_A,
        "milestone_policy_ref": _pin("milestone-policy", 1, HASH_B),
        "observed_milestone": "DELIVERED",
        "covered_handoff_ids": ["handoff-deliver-report"],
        "source_receipt_refs": [_pin("receipt-deliver-report", 1, HASH_C)],
    }


def test_occ02_strict_spec_codec_preserves_a_approved_effect_mapping() -> None:
    """OCC-02 / contracts.operation_completion strict-codec seam.

    A correctly formed approved mapping survives canonical encoding.  This is a
    document-contract oracle, not evidence that a model, capability profile, or
    absent intent may infer an effect.
    """

    spec = OperationCompletionRequirementsV1.from_json(_required_spec())

    assert spec.to_json() == _required_spec()
    assert OperationCompletionRequirementsV1.from_bytes(spec.to_bytes()) == spec
    assert len(spec.content_hash()) == 64


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: {**raw, "untrusted_approved": True},
        lambda raw: {**raw, "schema_version": True},
        lambda raw: {**raw, "effects": []},
        lambda raw: {**raw, "content_criterion_ids": ["criterion-report", "criterion-delivered"]},
        lambda raw: {
            **raw,
            "effects": [
                *raw["effects"],  # type: ignore[index]
                {**raw["effects"][0], "source_slot_key": "another-slot"},  # type: ignore[index]
            ],
        },
    ),
)
def test_occ02_spec_rejects_unapproved_or_ambiguous_document_shape(mutate) -> None:
    """OCC-02 / strict decoder: no `.get(..., [])` default may lower requirements."""

    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_json(mutate(_required_spec()))


def test_occ02_duplicate_json_key_cannot_replace_an_effect_requirement() -> None:
    """OCC-02 / byte decoder: duplicate JSON keys cannot turn REQUIRED into CONTENT."""

    raw = json.dumps(_required_spec(), separators=(",", ":"))
    duplicate = raw.replace(
        '"mode":"REQUIRED_EFFECTS"', '"mode":"REQUIRED_EFFECTS","mode":"CONTENT_ONLY"'
    )

    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_bytes(duplicate.encode("utf-8"))


@pytest.mark.parametrize(
    ("decoder", "raw_factory", "replace_array"),
    (
        (
            OperationCompletionRequirementsV1,
            _required_spec,
            lambda raw, value: raw["effects"][0].__setitem__("criterion_ids", value),  # type: ignore[index,union-attr]
        ),
        (
            OperationCompletionRequirementsV1,
            _required_spec,
            lambda raw, value: raw.__setitem__("content_criterion_ids", value),
        ),
        (
            OccurrenceCompletionScopeV1,
            _aggregate_scope,
            lambda raw, value: raw.__setitem__("content_criterion_ids", value),
        ),
        (
            OccurrenceCompletionScopeV1,
            _aggregate_scope,
            lambda raw, value: raw.__setitem__("required_effect_keys", value),
        ),
        (
            OccurrenceCompletionScopeV1,
            _aggregate_scope,
            lambda raw, value: raw.__setitem__("owned_effect_keys", value),
        ),
        (
            AcceptanceContributionScopeV1,
            _content_contribution,
            lambda raw, value: raw.__setitem__("content_criterion_ids", value),
        ),
        (
            AcceptanceContributionScopeV1,
            _content_contribution,
            lambda raw, value: raw.__setitem__("effect_keys", value),
        ),
        (
            OperationOutcomeReviewBindingV1,
            _outcome_binding,
            lambda raw, value: raw.__setitem__("covered_handoff_ids", value),
        ),
    ),
)
@pytest.mark.parametrize("not_an_array", ("criterion", {"criterion": "criterion"}, 1, True, None))
def test_occ02_from_json_rejects_non_array_identifier_fields(
    decoder, raw_factory, replace_array, not_an_array
) -> None:
    """Every JSON identifier-array is validated before any tuple conversion."""

    raw = raw_factory()
    replace_array(raw, not_an_array)

    with pytest.raises(ContractError):
        decoder.from_json(raw)


def test_occ02_byte_codec_normalizes_deep_and_oversize_json_to_contract_error() -> None:
    """Malformed untrusted bytes must never escape as parser recursion errors."""

    deep: object = "leaf"
    for _ in range(17):
        deep = {"nested": deep}
    parser_deep = (b'{"nested":' * 2_000) + b"null" + (b"}" * 2_000)
    oversized = b'{"payload":"' + (b"x" * MAX_COMPLETION_BYTES) + b'"}'

    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_bytes(json.dumps(deep).encode("utf-8"))
    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_bytes(parser_deep)
    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_bytes(oversized)


def test_occ02_scope_coverage_and_owner_are_checked_across_real_scope_documents() -> None:
    """OCC-02/OCC-10 / compiler contract seam.

    The aggregate scope owns the MUST effect while a sibling is CONTENT-only;
    sharing an ObligationId alone never copies an effect requirement to that child.
    """

    spec = OperationCompletionRequirementsV1.from_json(_required_spec())
    aggregate = OccurrenceCompletionScopeV1.from_json(_aggregate_scope())
    content = OccurrenceCompletionScopeV1.from_json(_content_scope())

    validate_spec_scope_coverage(spec, (aggregate, content), root_occurrence_id="occurrence-root")
    validate_scope_owners(spec, (aggregate, content))


def test_occ02_scope_revalidation_refuses_missing_or_duplicate_effect_owner() -> None:
    """OCC-02/OCC-10 / plan revalidation seam, before any plan row is written."""

    spec = OperationCompletionRequirementsV1.from_json(_required_spec())
    missing_owner = OccurrenceCompletionScopeV1.from_json(_aggregate_scope(owned=[]))
    content = OccurrenceCompletionScopeV1.from_json(_content_scope())
    duplicate_owner_raw = _content_scope()
    duplicate_owner_raw.update(
        role="MIXED",
        required_effect_keys=["deliver-report"],
        owned_effect_keys=["deliver-report"],
    )
    duplicate_owner = OccurrenceCompletionScopeV1.from_json(duplicate_owner_raw)

    with pytest.raises(ContractError):
        validate_scope_owners(spec, (missing_owner, content))
    with pytest.raises(ContractError):
        validate_scope_owners(
            spec, (OccurrenceCompletionScopeV1.from_json(_aggregate_scope()), duplicate_owner)
        )


def test_occ02_primitive_mixed_root_is_a_complete_spec_scope() -> None:
    """A real primitive root may carry CONTENT and all required effects itself."""

    spec = OperationCompletionRequirementsV1.from_json(_required_spec())
    raw = _aggregate_scope()
    raw["role"] = "MIXED"
    primitive_mixed = OccurrenceCompletionScopeV1.from_json(raw)

    validate_spec_scope_coverage(spec, (primitive_mixed,), root_occurrence_id="occurrence-root")
    validate_scope_owners(spec, (primitive_mixed,))


# =============================================================================
# OCC-01 … OCC-12 (Assurance 1.1 item 10): the actual acceptance / outcome /
# resolution producers, driven through the shared real operation world in
# tests/orchestrator/full_target/assurance_exec/_operation_world.py.  Reviews are
# scripted service intents; the connector is the real FilePublishConnector.
# =============================================================================

import asyncio  # noqa: E402
import dataclasses  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

_ASSURANCE_EXEC = Path(__file__).resolve().parents[1] / "assurance_exec"
for _path in (Path(__file__).resolve().parent, _ASSURANCE_EXEC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _operation_world import OperationWorld, completion_command, insert_requirements  # noqa: E402
from test_completion_pending_stall import _pending_stall_loop  # noqa: E402
from test_completion_spec_approval import _api  # noqa: E402

from agent_orchestrator.contracts.resolution import DeliveryReceipt, DeliveryStage  # noqa: E402
from agent_orchestrator.contracts.state_machines import MissionStatus, TaskStatus  # noqa: E402
from agent_orchestrator.orchestrator.completion_status import (  # noqa: E402
    read_current_effect,
    read_occurrence_completion,
)
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch  # noqa: E402
from agent_orchestrator.orchestrator.operation_completion import (
    OperationCompletionError,  # noqa: E402
)
from agent_orchestrator.orchestrator.resolution_commits import (
    ResolutionCommitRejected,  # noqa: E402
)
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.operation_completion_store import (
    OperationCompletionStore,  # noqa: E402
)

_TWO_EFFECTS = (("deliver-report", "criterion-delivered", "reports/final.json"),
                ("deliver-summary", "criterion-archived", "reports/summary.json"))
_TABLES = ("acceptances", "delivery_receipts", "operation_outcome_review_bindings", "operation_acceptance_scopes",
           "actions", "events", "tasks", "attempts", "goal_resolutions", "operation_intent_bindings")


def _occ_rows(w):
    return {t: w.count(f"SELECT COUNT(*) FROM {t} WHERE mission_id=?", w.mission_id) for t in _TABLES}


def _occ_code(error):
    return getattr(error, "code", None) or str(error)


def _effect_state(w, key="deliver-report"):
    return read_current_effect(w.store, w.mission_id, w.approved.spec_hash, key)["state"]


def test_report_preparation_does_not_finish_effect(tmp_path) -> None:
    """OCC-01: the accepted report is a preparation, readable by DATA and by T0."""
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    htn = HtnStore(w.store)
    assert len(htn.list_acceptances(w.mission_id)) == 1
    assert w.count("SELECT COUNT(*) FROM operation_acceptance_scopes WHERE mission_id=?", w.mission_id) == 1
    status = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert status.preparation_ready and status.content_ready and not status.effects_ready and not status.complete
    dispatch = HierarchicalDispatch(w.store, w.commit)
    outputs = dispatch.accepted_outputs(w.mission_id, dispatch.network(w.mission_id))
    assert [o.artifact_id for o in outputs.outputs] == [w.artifacts["deliver-report"].id]  # DATA readable
    assert w.store.get_task(w.task.id).status is TaskStatus.VERIFYING  # MIXED Task not COMPLETED
    assert not dispatch.terminal(w.mission_id) and not dispatch.root_review_ready(w.mission_id)
    assert w.count("SELECT COUNT(*) FROM goal_resolutions WHERE mission_id=?", w.mission_id) == 0
    # T0 / ACTION_PROPOSAL read the accepted report now, with the Mission still ACTIVE.
    assert w.store.get_mission(w.mission_id).status is MissionStatus.ACTIVE
    w.submit("deliver-report")
    assert w.captured["draft"].package.package_id
    assert w.store.get_mission(w.mission_id).status is MissionStatus.ACTIVE
    assert not read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete


def test_completion_mapping_missing_or_ambiguous(tmp_path) -> None:
    """OCC-02 (runtime side): no Spec / no Scope / unsupported milestone are named refusals."""
    with pytest.raises(OperationCompletionError) as missing:
        OperationWorld(tmp_path / "nospec", approve_spec=False, key="occ02-nospec")
    assert missing.value.code == "OP_REQUIREMENT_MAPPING_MISSING"
    w = OperationWorld(tmp_path / "spec", milestone="DELIVERED", key="occ02-spec")
    w.produce_and_accept()
    with pytest.raises(Exception) as unsupported:
        w.submit("deliver-report")
    assert _occ_code(unsupported.value) == "OP_CAPABILITY_UNSUPPORTED"
    with pytest.raises(Exception):
        read_occurrence_completion(w.store, w.mission_id, "occurrence-without-scope")
    assert read_current_effect(w.store, w.mission_id, "0" * 64, "deliver-report")["state"] == "SCOPE_STALE"
    # The exact approval is persisted once in the original requirement approval chain.
    rows = w.store.connection.execute(
        "SELECT spec_hash FROM operation_completion_specs WHERE mission_id=?", (w.mission_id,)).fetchall()
    assert [r[0] for r in rows] == [w.approved.spec_hash]
    replay = _api(w.world).approve(completion_command(w.requirements, milestone="DELIVERED", effects=w.effects,
                                                      profiles=w.profiles))
    assert replay.spec_hash == w.approved.spec_hash
    assert w.count("SELECT COUNT(*) FROM operation_completion_specs WHERE mission_id=?", w.mission_id) == 1


def test_exact_effect_reaches_outcome_review_and_completion(tmp_path) -> None:
    """OCC-03: real T0/T1/connector/T3 chain; resends yield one official acceptance."""
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    intent = w.submit("deliver-report")
    assert w.submit("deliver-report") == intent  # same command, same intent
    w.materialize("deliver-report")
    w.execute("deliver-report")
    assert not w.dispatch() and len(w.published_files()) == 1
    prepared = w.prepare_outcome("deliver-report")
    record = w.review_outcome(prepared)
    again = w.review_outcome(prepared)
    assert again == record
    receipt = w.accept_outcome(prepared.binding_id)
    replay = w.accept_outcome(prepared.binding_id)
    assert replay.replayed and replay.acceptance == receipt.acceptance
    contribution = OperationCompletionStore(w.store).get_acceptance_scope_exact(
        w.mission_id, str(receipt.acceptance.acceptance_id))
    delivery = HtnStore(w.store).list_delivery_receipts(w.mission_id, acceptance_id=str(receipt.acceptance.acceptance_id))
    assert contribution is not None and len(delivery) == 1 and delivery[0].stage is DeliveryStage.PERSISTED
    assert w.count("SELECT COUNT(*) FROM acceptances WHERE mission_id=?", w.mission_id) == 2  # preparation + effect
    assert w.count("SELECT COUNT(*) FROM operation_outcome_review_bindings WHERE mission_id=?", w.mission_id) == 1
    assert read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete
    assert w.store.get_task(w.task.id).status is TaskStatus.COMPLETED
    assert w.count("SELECT COUNT(*) FROM goal_resolutions WHERE mission_id=?", w.mission_id) == 0  # root review still owed
    assert w.store.get_mission(w.mission_id).status is MissionStatus.ACTIVE
    assert len(w.published_files()) == 1


def test_every_outcome_identity_axis_is_checked(tmp_path) -> None:
    """OCC-04: every frozen identity axis refuses; bare receipts never complete."""
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    w.submit("deliver-report")
    w.materialize("deliver-report")
    action = w.execute("deliver-report")
    prepared = w.prepare_outcome("deliver-report")
    w.review_outcome(prepared)
    original_read = OperationCompletionStore.get_outcome_binding_exact
    document = original_read(OperationCompletionStore(w.store), w.mission_id, prepared.binding_id)["document"]
    variants = {"operation_id": "op-other", "operation_occurrence_id": "occ-other",
                "action_version": document.action_version + 1, "link_hash": "1" * 64, "effect_contract_hash": "2" * 64,
                "spec_hash": "3" * 64, "mission_id": "mission-other", "completion_scope_id": "scope-other",
                "target_identity_hash": "4" * 64, "namespace_hash": "5" * 64, "params_hash": "6" * 64,
                "covered_handoff_ids": ("handoff-forged",), "request_hash": "7" * 64,
                "source_receipt_refs": (), "observed_milestone": "DELIVERED"}
    before = _occ_rows(w)
    for field, value in variants.items():
        try:
            forged = dataclasses.replace(document, **{field: value})
        except Exception:  # the document codec itself refuses the value
            continue

        def read(self, mission_id, binding_id, _forged=forged):
            row = original_read(self, mission_id, binding_id)
            return None if row is None else {**row, "document": _forged}

        with patch.object(OperationCompletionStore, "get_outcome_binding_exact", read):
            with pytest.raises(Exception) as refused:
                w.accept_outcome(prepared.binding_id)
        assert _occ_code(refused.value), field
        assert _occ_rows(w) == before, field
    # SENT / CONFIRMED receipts with a non-empty operation id are claims, not the chain.
    for stage in (DeliveryStage.SENT, DeliveryStage.CONFIRMED):
        with w.store.transaction():
            HtnStore(w.store).record_delivery_receipt(
                w.mission_id, DeliveryReceipt("bare-" + stage.lower(), w.mission_id, str(w.acceptance.acceptance_id),
                                              stage, int(w.store.now * 1000), document.operation_id),
                command_id="bare-" + stage.lower(), intent_hash="0" * 64)
    assert not read_occurrence_completion(w.store, w.mission_id, w.occurrence).effects_ready
    assert _effect_state(w) == "AWAITING_OUTCOME_REVIEW"
    # The genuine effect facts are archived unchanged and complete once.
    assert w.store.get_action(action["action_key"]) == action
    assert not w.accept_outcome(prepared.binding_id).replayed
    assert read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete


def test_requirement_input_revocation_blocks_old_contribution(tmp_path) -> None:
    """OCC-05: moved requirements / withdrawn support stale the old chain; unrelated changes do not."""
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    w.submit("deliver-report")
    w.materialize("deliver-report")
    w.execute("deliver-report")
    prepared = w.prepare_outcome("deliver-report")
    w.review_outcome(prepared)
    htn = HtnStore(w.store)
    before = _occ_rows(w)
    # Withdrawn support for the producer Task: the T3 acceptance sees a dirty source.
    with w.store.transaction():
        htn.mark_dirty(w.mission_id, subject_kind="task", subject_id=w.task.id, scope_id="mission",
                       epoch=htn.epoch(w.mission_id, "mission"), reason="support withdrawn")
    with pytest.raises(Exception) as dirty:
        w.accept_outcome(prepared.binding_id)
    assert _occ_code(dirty.value) == "OP_EFFECT_SCOPE_STALE" and _occ_rows(w) == before
    with w.store.transaction():
        w.store.connection.execute("DELETE FROM validity_dirty WHERE mission_id=? AND subject_id=?",
                                   (w.mission_id, w.task.id))
    # An unrelated change (one more Mission event) does not stale the exact chain.
    with w.store.transaction():
        w.commit._emit("HostObservationUnavailable", w.mission_id, key="unrelated", payload={"reason": "n/a"})
    assert not w.accept_outcome(prepared.binding_id).replayed
    complete_rows = _occ_rows(w)
    # After completion the requirements move: history stays, nothing re-sends, the
    # old contribution no longer satisfies the new requirement.
    insert_requirements(w.world, w.effects, revision=2)
    assert _effect_state(w) == "SCOPE_STALE"
    with pytest.raises(OperationCompletionError):
        read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert not w.dispatch() and len(w.published_files()) == 1
    assert _occ_rows(w) == complete_rows


def test_multiple_required_effects_are_not_collapsed(tmp_path) -> None:
    """OCC-06: A cannot complete B; B's stages (no intent / awaiting approval / UNKNOWN) stay visible."""
    w = OperationWorld(tmp_path, effects=_TWO_EFFECTS)
    w.produce_and_accept()
    prepared_a, _, _ = w.complete_effect("deliver-report")
    assert not read_occurrence_completion(w.store, w.mission_id, w.occurrence).effects_ready
    assert _effect_state(w, "deliver-summary") == "AWAITING_INTENT"
    original_read = OperationCompletionStore.get_outcome_binding_exact
    cloned = dataclasses.replace(original_read(OperationCompletionStore(w.store), w.mission_id,
                                               prepared_a.binding_id)["document"], effect_key="deliver-summary")

    def read(self, mission_id, binding_id):
        row = original_read(self, mission_id, binding_id)
        return None if row is None else {**row, "document": cloned}

    with patch.object(OperationCompletionStore, "get_outcome_binding_exact", read):
        assert w.accept_outcome(prepared_a.binding_id).replayed  # A's binding never becomes B
    assert _effect_state(w, "deliver-summary") == "AWAITING_INTENT"
    w.submit("deliver-summary")
    w.materialize("deliver-summary", approve=False)
    assert _effect_state(w, "deliver-summary") == "AWAITING_APPROVAL"
    w.approve_action("deliver-summary")
    # The connector applies the publish but its reply is lost: the action is
    # UNKNOWN, visible, never re-sent blindly.
    real_execute = FilePublishConnector.execute

    def applied_but_lost(self, *args, **kwargs):
        real_execute(self, *args, **kwargs)
        raise RuntimeError("reply lost after the service applied it")

    with patch.object(FilePublishConnector, "execute", applied_but_lost):
        assert w.dispatch()
    action = w.store.get_action(w.actions["deliver-summary"]["action_key"])
    assert action["state"] == "UNKNOWN" and not action.get("receipt")
    assert _effect_state(w, "deliver-summary") == "RECONCILIATION_REQUIRED"
    assert not w.dispatch() and len(w.published_files()) == 2  # applied once, not again
    assert not read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete
    # Reconciliation reads the connector's own ledger + published file: the real
    # outcome is booked without a second send; B completes with its own chain.
    settled = asyncio.run(w.executor.reconcile(w.mission_id))
    assert settled and w.store.get_action(action["action_key"])["state"] == "SUCCEEDED"
    assert len(w.published_files()) == 2
    prepared_b = w.prepare_outcome("deliver-summary")
    w.review_outcome(prepared_b)
    w.accept_outcome(prepared_b.binding_id)
    assert read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete
    assert w.count("SELECT COUNT(*) FROM goal_resolutions WHERE mission_id=?", w.mission_id) == 0


def test_report_only_and_legacy_remain_compatible(tmp_path) -> None:
    """OCC-07: delegated to the E08 case on the assured content-only world and a legacy Mission."""
    from test_e_assurance import test_content_legacy_compatibility

    test_content_legacy_compatibility(tmp_path)


def test_direct_final_commit_cannot_skip_effect_gate(tmp_path) -> None:
    """OCC-08: CURRENT children acceptances + no delivery contract cannot be forced into a root resolution."""
    from agent_orchestrator.contracts.resolution import (
        CriterionVerdict,
        GoalResolution,
        ResolutionCriterion,
        ReviewVerdict,
        Validity,
    )
    from agent_orchestrator.orchestrator.leaf_acceptance import LeafAcceptanceAssembly
    from agent_orchestrator.orchestrator.resolution_commits import (
        CommitGoalResolutionCommand,
        ResolutionPrincipal,
    )
    from agent_orchestrator.verification.acceptance_rules import ExecutionPosture, IndependenceFacts

    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    htn = HtnStore(w.store)
    acceptance = htn.list_acceptances(w.mission_id)[0]
    record = htn.get_review_record(str(acceptance.review_record_id)).record
    package = htn.get_review_package(str(record.package_id))
    semantics = htn.task_semantics_of(w.mission_id, w.task.id)
    dispatch = HierarchicalDispatch(w.store, w.commit)
    assert not dispatch.root_review_ready(w.mission_id) and not dispatch.terminal(w.mission_id)
    assembly = LeafAcceptanceAssembly(w.store, w.commit, reviewer_agent_id="forger")
    now_ms = int(w.store.now * 1000)
    witness = assembly._witness(w.mission_id, w.task.id, acceptance_id="acc-forced-root", now_ms=now_ms)
    resolution = GoalResolution(
        resolution_id="res-forced-root", mission_id=w.mission_id, obligation_id=str(acceptance.obligation_id),
        goal_task_id=w.task.id, requirements_version=1, contract_revision=int(semantics.contract_revision),
        method_instance_id=None, input_manifest_hash=acceptance.input_manifest_hash, artifact_refs=(),
        child_resolution_ids=(),
        criteria=tuple(ResolutionCriterion(c.criterion_id, CriterionVerdict.PASS) for c in w.requirements.criteria),
        review_receipt_id=str(record.record_id),
        verdict=ReviewVerdict.ACCEPT, validity=Validity.CURRENT)
    command = CommitGoalResolutionCommand(
        command_id="force-root", mission_id=w.mission_id, resolution=resolution, package=package, record=record,
        requirements=w.requirements, witness_id=witness.witness_id,
        independence=IndependenceFacts(producer_agent_ids=package.producer_agent_ids, reviewer_can_write_candidate=False),
        posture=ExecutionPosture(cancellation_requested=False),
        read_set=assembly._read_set(w.mission_id, semantics, w.requirements), decided_at_ms=now_ms,
        is_mission_root=True, issued_by="forger")
    before = _occ_rows(w)
    reservations = w.store.connection.execute("SELECT subject_id,state FROM budget_reservations ORDER BY subject_id").fetchall()
    with pytest.raises((ResolutionCommitRejected, ContractError)) as refused:
        w.commit.commit_goal_resolution(command, ResolutionPrincipal("forger", manager_epoch=htn.epoch(w.mission_id, "mission")))
    assert _occ_code(refused.value)
    assert _occ_rows(w) == before and not w.store.connection.in_transaction
    assert w.store.connection.execute("SELECT subject_id,state FROM budget_reservations ORDER BY subject_id").fetchall() == reservations
    assert w.store.get_mission(w.mission_id).status is MissionStatus.ACTIVE
    assert w.store.get_task(w.task.id).status is TaskStatus.VERIFYING
    # The only way through is the matching effect proof.
    w.complete_effect("deliver-report")
    assert read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete


def test_scope_acceptance_delivery_and_receipt_are_atomic(tmp_path) -> None:
    """OCC-09: a cut at any T3 write point rolls the whole acceptance back; no second connector call."""
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    w.submit("deliver-report")
    w.materialize("deliver-report")
    action = w.execute("deliver-report")
    prepared = w.prepare_outcome("deliver-report")
    w.review_outcome(prepared)
    before = _occ_rows(w)
    cuts = ((HtnStore, "record_delivery_receipt"), (OperationCompletionStore, "insert_acceptance_scope"),
            (HtnStore, "insert_acceptance"))
    for owner, name in cuts:
        with patch.object(owner, name, side_effect=OSError("cut: " + name)):
            with pytest.raises(OSError):
                w.accept_outcome(prepared.binding_id)
        assert _occ_rows(w) == before, name
        assert not w.store.connection.in_transaction
        assert w.store.get_action(action["action_key"]) == action  # the original fact survives
    assert len(w.published_files()) == 1 and not w.dispatch()
    receipt = w.accept_outcome(prepared.binding_id)
    after = _occ_rows(w)
    assert after["acceptances"] == before["acceptances"] + 1 and after["delivery_receipts"] == before["delivery_receipts"] + 1
    assert w.accept_outcome(prepared.binding_id).replayed and _occ_rows(w) == after
    assert len(w.published_files()) == 1
    assert HtnStore(w.store).find_acceptance_receipt(w.mission_id, "accept:" + prepared.binding_id).event_id == receipt.commit.event_id


def test_shared_obligation_uses_per_occurrence_completion_scope(tmp_path) -> None:
    """OCC-10 (compiler + reader level): siblings under one Obligation get their own scope."""
    from test_completion_scope_compiler import _approved_coverage, _committed_network, _spec

    from agent_orchestrator.contracts.htn import TaskForm
    from agent_orchestrator.contracts.operation_completion import CompletionScopeRole
    from agent_orchestrator.planning.htn.completion_scopes import compile_completion_scopes

    world, plan, plan_ref = _committed_network(tmp_path)
    scopes = compile_completion_scopes(_spec(world.mission.id), plan, _approved_coverage(plan), plan_ref=plan_ref)
    by_occurrence = {scope.occurrence_id: scope for scope in scopes}
    root = str(plan.root_occurrence_ids[0])
    leaves = [str(item.occurrence_id) for item in plan.occurrences if item.form is TaskForm.PRIMITIVE]
    assert by_occurrence[root].role is CompletionScopeRole.AGGREGATE
    assert by_occurrence[root].required_effect_keys == ("deliver-report",) == by_occurrence[root].owned_effect_keys
    for leaf in leaves:  # CONTENT children never wait for the sibling's effect
        assert by_occurrence[leaf].role is CompletionScopeRole.CONTENT
        assert by_occurrence[leaf].required_effect_keys == () and by_occurrence[leaf].owned_effect_keys == ()
        assert by_occurrence[leaf].obligation_id == by_occurrence[root].obligation_id
    owners = [s.occurrence_id for s in scopes if "deliver-report" in s.owned_effect_keys]
    assert owners == [root]  # one owner
    # Reader level on the MIXED single-root world: a sibling-free acceptance of the
    # same obligation (the preparation) does not complete the MIXED occurrence.
    w = OperationWorld(tmp_path / "mixed", key="occ10-mixed")
    w.produce_and_accept()
    status = read_occurrence_completion(w.store, w.mission_id, w.occurrence)
    assert status.content_ready and not status.complete
    dispatch = HierarchicalDispatch(w.store, w.commit)
    assert dispatch.accepted_outputs(w.mission_id, dispatch.network(w.mission_id)).outputs  # DATA readable
    assert not dispatch.terminal(w.mission_id)  # ORDER not released


def test_outcome_review_callback_cold_replay(tmp_path) -> None:
    """OCC-11: restart at each T3 point reuses the same binding / record / acceptance."""
    from agent_orchestrator.orchestrator.operation_outcomes import (
        persist_operation_outcome_review,
        prepare_operation_outcome_review,
    )
    from agent_orchestrator.storage.store import Store

    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    w.submit("deliver-report")
    w.materialize("deliver-report")
    w.execute("deliver-report")
    # (a) facts saved, review not yet dispatched: preparing twice is one binding.
    first = prepare_operation_outcome_review(w.store, intent_id=w.intents["deliver-report"],
                                             connectors=w.connectors, profiles=w.profiles)
    second = prepare_operation_outcome_review(w.store, intent_id=w.intents["deliver-report"],
                                              connectors=w.connectors, profiles=w.profiles)
    assert first.binding_id == second.binding_id
    with w.store.transaction():
        persist_operation_outcome_review(w.commit, first, runtime=w.runtime)
    with w.store.transaction():
        persist_operation_outcome_review(w.commit, second, runtime=w.runtime)
    assert w.count("SELECT COUNT(*) FROM operation_outcome_review_bindings WHERE mission_id=?", w.mission_id) == 1
    w.bindings["deliver-report"] = first.binding_id
    # (b) dispatched, verdict returned: the same turn recorded twice is one official record.
    record = w.review_outcome(first)
    assert w.review_outcome(first) == record
    assert w.count("SELECT COUNT(*) FROM review_records WHERE mission_id=? AND official=1 AND purpose='OPERATION_OUTCOME'",
                   w.mission_id) == 1
    # (c) verdict saved, acceptance not committed: a second connection sees the same
    # durable facts and the acceptance is idempotent across both.
    other = Store.open(w.store.path)
    try:
        assert other.connection.execute("SELECT COUNT(*) FROM operation_outcome_review_bindings WHERE mission_id=?",
                                        (w.mission_id,)).fetchone()[0] == 1
        receipt = w.accept_outcome(first.binding_id)
        assert other.connection.execute("SELECT COUNT(*) FROM acceptances WHERE acceptance_id=?",
                                        (str(receipt.acceptance.acceptance_id),)).fetchone()[0] == 1
        with pytest.raises(Exception):  # the unique acceptance cannot be written twice
            other.connection.execute("INSERT INTO acceptances SELECT * FROM acceptances WHERE acceptance_id=?",
                                     (str(receipt.acceptance.acceptance_id),))
    finally:
        other.close()
    assert w.accept_outcome(first.binding_id).replayed
    assert len(w.published_files()) == 1
    assert w.count("SELECT COUNT(*) FROM dispatch_intents WHERE mission_id=? AND config_json LIKE '%operation_outcome_reviewer%'",
                   w.mission_id) == 1


def test_effect_pending_is_work_not_false_completion_or_retry(tmp_path) -> None:
    """OCC-12: pending effects are work; timeouts book UNKNOWN, never success; no worker churn."""
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    w.submit("deliver-report")
    w.materialize("deliver-report", approve=False)
    loop = _pending_stall_loop(w.world, tmp_path / "loop")
    counts = _occ_rows(w)
    for _ in range(3):
        assert not w.dispatch()
        asyncio.run(loop._record_hierarchical_stall())
        assert w.mission_id not in loop._stalled_at
        assert asyncio.run(loop._confirm_and_stop_stalled()) is False
    assert _occ_rows(w) == counts and _effect_state(w) == "AWAITING_APPROVAL"
    assert w.store.get_mission(w.mission_id).status is MissionStatus.ACTIVE
    w.approve_action("deliver-report")
    # The connector call outlives its deadline and never applies: UNKNOWN, no
    # receipt, no success; the late failure clears nothing; reconciliation with
    # no service evidence keeps it UNKNOWN (never a fabricated non-application).
    import time

    w.executor._timeout = 0.05

    def slow_failure(self, *args, **kwargs):
        time.sleep(0.3)
        raise RuntimeError("late failure after the deadline")

    with patch.object(FilePublishConnector, "execute", slow_failure):
        assert w.dispatch()
        action = w.store.get_action(w.actions["deliver-report"]["action_key"])
        assert action["state"] == "UNKNOWN" and not action.get("receipt")
        assert _effect_state(w) == "RECONCILIATION_REQUIRED"
        assert not w.dispatch()  # not re-sent while unresolved
        time.sleep(0.4)
    w.executor._timeout = 30.0
    assert w.store.get_action(action["action_key"])["state"] == "UNKNOWN"
    asyncio.run(w.executor.reconcile(w.mission_id))
    action = w.store.get_action(action["action_key"])
    assert action["state"] == "UNKNOWN" and action.get("reconcile") == "STILL_UNKNOWN"
    assert not read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete
    assert w.store.get_task(w.task.id).status is TaskStatus.VERIFYING
    assert _occ_rows(w)["attempts"] == counts["attempts"]  # no Worker reopened
    assert w.published_files() == [] and not w.dispatch()
    # The service applies late (after the deadline): reconciliation books the real
    # outcome from the connector ledger; nothing is re-sent; completion proceeds.
    real_execute = FilePublishConnector.execute
    real_execute(w.publish, str(action["operation"]), str(action["target"]), dict(action["params"]),
                 idempotency_key=str(action["idempotency_key"]))
    assert len(w.published_files()) == 1
    settled = asyncio.run(w.executor.reconcile(w.mission_id))
    assert settled and w.store.get_action(action["action_key"])["state"] == "SUCCEEDED"
    assert len(w.published_files()) == 1
    prepared = w.prepare_outcome("deliver-report")
    w.review_outcome(prepared)
    w.accept_outcome(prepared.binding_id)
    assert read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete
