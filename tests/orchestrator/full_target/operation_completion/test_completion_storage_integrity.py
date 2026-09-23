"""Immutable completion payloads must match their persisted identity columns.

These cases deliberately use the authenticated Spec approval and the real Plan
Commit scope producer.  Outcome and contribution rows have no production
producer in this slice, so this file does not manufacture them to claim an
unearned end-to-end result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_orchestrator.contracts.operation_completion import OccurrenceCompletionScopeV1
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore
from agent_orchestrator.storage.store import StoreConflict
from simple_harness.contracts import canonical_json

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_completion_plan_commit import _admitted_single_root  # noqa: E402
from test_completion_spec_approval import _api, _approval_world, _command  # noqa: E402


def _approved_scope(tmp_path):
    world, requirements = _approval_world(tmp_path)
    approved = _api(world).approve(_command(requirements))
    command, _ = _admitted_single_root(world, requirements)
    active = HtnStore(world.store).active_plan_revision(world.mission.id)
    assert active is not None
    occurrence_id = str(command.network.root_occurrence_ids[0])
    completion = OperationCompletionStore(world.store)
    scope = completion.get_scope_exact(world.mission.id, active.revision, occurrence_id)
    assert scope is not None
    return world, approved, active, occurrence_id, completion, scope


def _remove_immutable_trigger(connection, trigger: str) -> None:
    """Test-only disk-corruption simulation; production triggers remain unchanged."""

    connection.execute(f"DROP TRIGGER {trigger}")  # noqa: S608 - fixed test-only names


def test_completion_store_reads_untampered_real_spec_and_scope_rows(tmp_path) -> None:
    """Existing identity columns still serve ordinary approved Spec/Scope reads."""

    world, approved, active, occurrence_id, completion, scope_row = _approved_scope(tmp_path)

    spec_row = completion.get_spec_exact(
        world.mission.id,
        int(scope_row["document"].requirements_ref.revision),
        scope_row["document"].requirements_ref.content_hash,
    )
    reread_scope = completion.get_scope_exact(world.mission.id, active.revision, occurrence_id)

    assert spec_row is not None
    assert spec_row["spec_hash"] == approved.spec_hash == spec_row["document"].content_hash()
    assert reread_scope is not None
    assert reread_scope["scope_hash"] == reread_scope["document"].content_hash()


def test_completion_store_rejects_scope_document_with_wrong_stored_hash_without_writes(
    tmp_path,
) -> None:
    """A valid codec document cannot replace a frozen scope while its old hash remains."""

    world, _, active, occurrence_id, completion, scope_row = _approved_scope(tmp_path)
    raw = scope_row["document"].to_json()
    raw["content_criterion_ids"] = []
    _remove_immutable_trigger(
        world.store.connection, "operation_completion_scopes_immutable_update"
    )
    world.store.connection.execute(
        "UPDATE operation_completion_scopes SET document_json=? WHERE scope_id=?",
        (canonical_json(raw), scope_row["scope_id"]),
    )
    before = world.store.connection.total_changes
    events_before = tuple(world.store.list_events(world.mission.id))

    with pytest.raises(StoreConflict, match="scope"):
        completion.get_scope_exact(world.mission.id, active.revision, occurrence_id)

    assert world.store.connection.total_changes == before
    assert tuple(world.store.list_events(world.mission.id)) == events_before


def test_completion_store_rejects_scope_identity_even_when_attacker_rehashes_document(
    tmp_path,
) -> None:
    """The row's occurrence/scope identity is checked separately from scope_hash."""

    world, _, active, occurrence_id, completion, scope_row = _approved_scope(tmp_path)
    raw = scope_row["document"].to_json()
    raw["occurrence_id"] = "corrupt-occurrence"
    corrupt = OccurrenceCompletionScopeV1.from_json(raw)
    _remove_immutable_trigger(
        world.store.connection, "operation_completion_scopes_immutable_update"
    )
    world.store.connection.execute(
        "UPDATE operation_completion_scopes SET document_json=?,scope_hash=? WHERE scope_id=?",
        (canonical_json(corrupt.to_json()), corrupt.content_hash(), scope_row["scope_id"]),
    )
    before = world.store.connection.total_changes

    with pytest.raises(StoreConflict, match="scope"):
        completion.get_scope_exact(world.mission.id, active.revision, occurrence_id)

    assert world.store.connection.total_changes == before


def test_completion_store_rejects_tampered_real_approved_spec_payload(tmp_path) -> None:
    """Spec payload corruption is rejected before a reader can reuse the approval row."""

    world, approved, _, _, completion, _ = _approved_scope(tmp_path)
    row = world.store.connection.execute(
        "SELECT document_json FROM operation_completion_specs WHERE spec_id=?", (approved.spec_id,)
    ).fetchone()
    assert row is not None
    raw = json.loads(str(row["document_json"]))
    raw["effects"][0]["required_milestone"] = "RECEIVED"
    _remove_immutable_trigger(world.store.connection, "operation_completion_specs_immutable_update")
    world.store.connection.execute(
        "UPDATE operation_completion_specs SET document_json=? WHERE spec_id=?",
        (canonical_json(raw), approved.spec_id),
    )
    before = world.store.connection.total_changes

    with pytest.raises(StoreConflict, match="Spec"):
        completion.get_spec_exact(
            world.mission.id,
            int(raw["requirements_ref"]["revision"]),
            str(raw["requirements_ref"]["content_hash"]),
        )

    assert world.store.connection.total_changes == before
