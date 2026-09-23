"""OCC-09: completion scopes freeze with an admitted new-protocol Plan Commit.

The oracle is the CommitService transaction, not a direct Store adapter call:
the actual planning admission writes the plan receipt, materialised membership,
and frozen completion scope together.  The fixture uses the registered
single-primitive GoalType whose approved semantic contract covers
``criterion-report``; it never supplies a test-invented ``CarriedCriterion``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_orchestrator.orchestrator.operation_completion import (
    OperationCompletionError,
    OperationCompletionReader,
)
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore
from agent_orchestrator.storage.store import InjectedCrash

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_completion_spec_approval import (  # noqa: E402
    _api,
    _approval_world,
    _command,
    _commit_with_refreshed_admission,
    _single_completion_command,
)


def _plan_scope_counts(world) -> tuple[int, int, int, int]:
    """The four write groups which must share the plan-commit transaction."""

    connection = world.store.connection
    return (
        int(connection.execute("SELECT count(*) FROM plan_revisions").fetchone()[0]),
        int(connection.execute("SELECT count(*) FROM plan_commit_receipts").fetchone()[0]),
        int(connection.execute("SELECT count(*) FROM operation_completion_scopes").fetchone()[0]),
        len(world.store.list_events(world.mission.id)),
    )


def _admitted_single_root(world, requirements, *, outputs=()):
    """Build the actual one-node plan and enter via real planning admission."""

    command = _single_completion_command(world, int(requirements.revision), outputs=outputs)
    receipt = _commit_with_refreshed_admission(world, command, int(requirements.revision))
    return command, receipt


def test_occ09_plan_commit_freezes_exact_single_root_completion_scope(tmp_path) -> None:
    """OCC-09 / CommitService writer seam: receipt, plan, membership and scope agree."""

    world, requirements = _approval_world(tmp_path)
    approved = _api(world).approve(_command(requirements))
    command, receipt = _admitted_single_root(world, requirements)

    active = HtnStore(world.store).active_plan_revision(world.mission.id)
    assert active is not None
    occurrence_id = str(command.network.root_occurrence_ids[0])
    stored = OperationCompletionStore(world.store).get_scope_exact(
        world.mission.id, active.revision, occurrence_id
    )
    assert stored is not None
    scope = stored["document"]
    assert stored["plan_receipt_id"] == receipt.command_id == command.command_id
    assert scope.spec_hash == approved.spec_hash
    assert scope.plan_ref.revision == active.revision
    assert scope.plan_ref.snapshot_hash == active.snapshot_hash
    assert scope.occurrence_id == occurrence_id
    assert scope.role == "MIXED"
    assert scope.content_criterion_ids == ("criterion-report",)
    assert scope.required_effect_keys == ("deliver-report",)
    assert scope.owned_effect_keys == ("deliver-report",)

    binding = HtnStore(world.store).task_semantics_of(world.mission.id, scope.task_ref.id)
    assert binding is not None
    assert scope.task_ref.revision == binding.contract_revision
    assert scope.task_ref.content_hash == binding.contract_hash
    before = world.store.connection.total_changes
    assert (
        OperationCompletionReader(world.store).read_scope(
            world.mission.id, scope.plan_ref, occurrence_id
        )
        == scope
    )
    assert world.store.connection.total_changes == before


@pytest.mark.parametrize("fault", ("plan_hash", "plan_revision", "missing_scope", "task_contract"))
def test_occ02_scope_reader_rechecks_current_exact_sources(tmp_path, fault: str) -> None:
    import dataclasses

    from agent_orchestrator.contracts.operation_completion import PlanRevisionPinV1

    world, requirements = _approval_world(tmp_path)
    _api(world).approve(_command(requirements))
    command, _ = _admitted_single_root(world, requirements)
    htn = HtnStore(world.store)
    active = htn.active_plan_revision(world.mission.id)
    assert active is not None
    occurrence_id = str(command.network.root_occurrence_ids[0])
    pin = PlanRevisionPinV1(revision=active.revision, snapshot_hash=active.snapshot_hash)
    if fault == "plan_hash":
        pin = dataclasses.replace(pin, snapshot_hash="f" * 64)
    elif fault == "plan_revision":
        pin = dataclasses.replace(pin, revision=active.revision + 1)
    elif fault == "missing_scope":
        occurrence_id = "no-such-occurrence"
    else:
        task_id = str(command.network.occurrences[0].task_id)
        binding = htn.task_semantics_of(world.mission.id, task_id)
        assert binding is not None
        htn.put_task_semantics(
            world.mission.id,
            dataclasses.replace(
                binding,
                contract_revision=int(binding.contract_revision) + 1,
                contract_hash="f" * 64,
            ),
        )
    before = world.store.connection.total_changes
    with pytest.raises(OperationCompletionError) as refused:
        OperationCompletionReader(world.store).read_scope(world.mission.id, pin, occurrence_id)
    assert refused.value.code == (
        "OP_COMPLETION_SCOPE_UNRESOLVED" if fault == "missing_scope" else "OP_EFFECT_SCOPE_STALE"
    )
    assert world.store.connection.total_changes == before


@pytest.mark.parametrize(
    "fault",
    ("completion_plan_before_scopes", "completion_plan_after_scope"),
)
def test_occ09_scope_fault_rolls_back_plan_receipt_membership_scope_and_event(
    tmp_path, fault: str
) -> None:
    """OCC-09: a crash at either scope boundary commits none of the plan publication."""

    world, requirements = _approval_world(tmp_path)
    _api(world).approve(_command(requirements))
    command = _single_completion_command(world, int(requirements.revision))
    before = _plan_scope_counts(world)
    world.store.arm(f"{fault}:operation_completion")

    with pytest.raises(InjectedCrash, match=fault):
        _commit_with_refreshed_admission(world, command, int(requirements.revision))

    assert _plan_scope_counts(world) == before
    assert HtnStore(world.store).active_plan_revision(world.mission.id) is None
    assert (
        world.store.connection.execute(
            "SELECT count(*) FROM plan_memberships WHERE mission_id=?", (world.mission.id,)
        ).fetchone()[0]
        == 0
    )


def test_occ09_replayed_plan_commit_does_not_duplicate_completion_scope_or_event(tmp_path) -> None:
    """OCC-09: plan receipt replay returns before scope creation and is write-free."""

    world, requirements = _approval_world(tmp_path)
    _api(world).approve(_command(requirements))
    command, first = _admitted_single_root(world, requirements)
    before = _plan_scope_counts(world)

    replay = world.service.commit_plan_revision(command, world.principal)

    assert replay == first
    assert _plan_scope_counts(world) == before


def test_occ09_new_protocol_plan_without_approved_spec_is_rejected_before_publication(
    tmp_path,
) -> None:
    """OCC-09: Requirements plus semantic coverage cannot silently become CONTENT_ONLY."""

    world, requirements = _approval_world(tmp_path)
    command = _single_completion_command(world, int(requirements.revision))
    before = _plan_scope_counts(world)

    with pytest.raises(OperationCompletionError) as rejected:
        _commit_with_refreshed_admission(world, command, int(requirements.revision))

    assert rejected.value.code == "OP_REQUIREMENT_MAPPING_MISSING"
    assert _plan_scope_counts(world) == before
    assert HtnStore(world.store).active_plan_revision(world.mission.id) is None
