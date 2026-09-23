"""OC3 operation-intent source input stays strict and authority-free on the wire."""

from __future__ import annotations

import dataclasses

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.operation_intents import (
    OperationIntentSourceKind,
    SubmitOperationIntentV2,
)
from agent_orchestrator.contracts.semantic_base import Provenance, TypedRef, TypedRefKind

HASH_A = "a" * 64
HASH_B = "b" * 64


def _wire() -> dict[str, object]:
    return {
        "schema_version": 2,
        "mission_id": "mission-oc3",
        "idempotency_key": "submit-1",
        "intent_source": {"kind": "USER_COMMAND"},
        "candidate_artifact_ref": TypedRef(
            TypedRefKind.ARTIFACT, "artifact-1", 1, HASH_A, Provenance.TOOL
        ).to_json(),
        "prepared_acceptance_refs": [
            TypedRef(TypedRefKind.ACCEPTANCE, "acceptance-1", 1, HASH_B, Provenance.TOOL).to_json()
        ],
        "supersedes_intent_id": None,
        "completion_slot": {"spec_hash": HASH_A, "effect_key": "send-report"},
    }


def test_submit_operation_intent_v2_is_strict_and_contains_no_caller_authority() -> None:
    command = SubmitOperationIntentV2.from_json(_wire())

    assert command.intent_source.kind is OperationIntentSourceKind.USER_COMMAND
    assert command.completion_slot.effect_key == "send-report"
    assert "principal" not in command.to_json()
    assert "tenant_id" not in command.to_json()

    for injected in ({"principal": "model"}, {"authority": True}, {"tenant_id": "other"}):
        with pytest.raises(ContractError):
            SubmitOperationIntentV2.from_json(_wire() | injected)


def test_user_command_cannot_smuggle_origin_and_authorized_slot_is_complete() -> None:
    smuggled = _wire()
    smuggled["intent_source"] = {
        "kind": "USER_COMMAND",
        "origin_receipt_id": "receipt-1",
        "slot_key": "slot-1",
    }
    with pytest.raises(ContractError):
        SubmitOperationIntentV2.from_json(smuggled)

    authorized = _wire()
    authorized["intent_source"] = {
        "kind": "AUTHORIZED_SLOT",
        "origin_receipt_id": "receipt-1",
        "slot_key": "slot-1",
    }
    command = SubmitOperationIntentV2.from_json(authorized)
    assert command.intent_source.origin_receipt_id == "receipt-1"


def test_completion_slot_and_prepared_acceptance_are_mandatory() -> None:
    missing = _wire()
    missing.pop("completion_slot")
    with pytest.raises(ContractError):
        SubmitOperationIntentV2.from_json(missing)

    empty = _wire()
    empty["prepared_acceptance_refs"] = []
    with pytest.raises(ContractError):
        SubmitOperationIntentV2.from_json(empty)


@pytest.mark.parametrize("schema", [True, 2.0, "2"])
def test_intent_schema_requires_exact_integer(schema):
    with pytest.raises(ContractError):
        SubmitOperationIntentV2.from_json(_wire() | {"schema_version": schema})


@pytest.mark.parametrize("mutation", [None, "tenant", "acceptance-hash", "dirty", "slot"])
def test_sources_resolve_real_preparation_and_reject_invalid_authority(tmp_path, mutation):
    from test_scoped_content_commit import _mixed_world

    from agent_orchestrator.artifacts.store import ArtifactStore
    from agent_orchestrator.contracts.semantic_base import content_hash_of
    from agent_orchestrator.governance.permissions import Principal
    from agent_orchestrator.orchestrator.operation_intent_sources import (
        OperationIntentSourceError,
        prepare_operation_intent_sources,
    )
    from agent_orchestrator.storage.htn_store import HtnStore

    world, _, approved, task, result, artifact = _mixed_world(tmp_path, with_output=True)
    htn = HtnStore(world.store)
    acceptance = htn.list_acceptances(world.mission.id)[0]
    wire = _wire() | {
        "mission_id": world.mission.id,
        "candidate_artifact_ref": TypedRef(
            TypedRefKind.ARTIFACT,
            artifact.id,
            artifact.version,
            artifact.content_hash,
            Provenance.TOOL,
        ).to_json(),
        "prepared_acceptance_refs": [
            TypedRef(
                TypedRefKind.ACCEPTANCE,
                str(acceptance.acceptance_id),
                1,
                content_hash_of(acceptance.to_json()),
                Provenance.TOOL,
            ).to_json()
        ],
        "completion_slot": {"spec_hash": approved.spec_hash, "effect_key": "send-report"},
    }
    from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore

    spec_row = OperationCompletionStore(world.store).get_spec_exact(
        world.mission.id,
        acceptance.requirements_revision,
        htn.get_requirements_revision(
            world.mission.id, acceptance.requirements_revision
        ).content_hash(),
    )
    wire["completion_slot"]["effect_key"] = spec_row["document"].effects[0].effect_key
    command = SubmitOperationIntentV2.from_json(wire)
    tenant = world.mission.tenant_id
    if mutation == "tenant":
        tenant = "other-tenant"
    elif mutation == "acceptance-hash":
        command = dataclasses.replace(
            command,
            prepared_acceptance_refs=(
                dataclasses.replace(
                    command.prepared_acceptance_refs[0],
                    content_hash=HASH_A,
                ),
            ),
        )
    elif mutation == "dirty":
        htn.mark_dirty(
            world.mission.id,
            subject_kind="acceptance",
            subject_id=str(acceptance.acceptance_id),
            scope_id="mission",
            epoch=1,
            reason="actual support changed",
        )
    elif mutation == "slot":
        command = dataclasses.replace(
            command,
            completion_slot=dataclasses.replace(
                command.completion_slot,
                effect_key="unapproved-effect",
            ),
        )
    before = world.store.connection.total_changes
    if mutation is not None:
        with pytest.raises(OperationIntentSourceError):
            prepare_operation_intent_sources(
                world.store,
                ArtifactStore(tmp_path / "scoped-cas"),
                command,
                tenant_id=tenant,
                principal=Principal("user"),
            )
    else:
        sources = prepare_operation_intent_sources(
            world.store,
            ArtifactStore(tmp_path / "scoped-cas"),
            command,
            tenant_id=tenant,
            principal=Principal("user"),
        )
        assert sources.producer_result.envelope.id == result.envelope.id
        assert sources.producer_scope.task_ref.id == task.id
        assert sources.raw_candidate_bytes == b'{"report":"verified local preparation"}\n'
    assert world.store.connection.total_changes == before
