"""Live H1-H operation boundaries backed by the formal T0/T1 producer.

This file deliberately does not manufacture authoritative negative evidence.  The
production reader consumes ``reconciliation_proof``, but no production writer currently
persists that document; the supported O07 branches below therefore cover a real matching
success receipt and a mismatching receipt only.
"""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_OPERATION = _HERE / "operation_completion"
for _directory in (_HERE, _OPERATION, _HERE.parent / "step07"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from helpers_step07 import ledger_service  # noqa: E402
from operation_runtime_fixture import materialized_file_publish  # noqa: E402
from test_h1h_operation_alias import _origin, _propose  # noqa: E402

from agent_orchestrator.orchestrator.action_commits import ActionCommitError  # noqa: E402
from agent_orchestrator.runtime.actions import ActionExecutor  # noqa: E402
from agent_orchestrator.runtime.planning_operations import (  # noqa: E402
    OperationEffect,
    SourceUnavailable,
    StoreOperationReader,
    build_operation_snapshot,
)
from agent_orchestrator.storage.planning_admission_store import (  # noqa: E402
    PlanningAdmissionStore,
)


def test_o07_real_t0_t1_success_receipt_is_applied_and_wrong_receipt_is_refused(
    tmp_path,
) -> None:
    fixture = materialized_file_publish(tmp_path)
    executed = asyncio.run(
        ActionExecutor(
            fixture.world.service,
            fixture.connectors,
            fixture.deployment,
            owner="o07-live-executor",
            source_storage_roots=(tmp_path / "artifacts",),
        ).hand_off(fixture.action["action_key"])
    )
    assert executed is not None and executed["state"] == "SUCCEEDED"

    snapshot = build_operation_snapshot(
        fixture.world.mission.id,
        reader=StoreOperationReader(fixture.world.store),
    )
    [effect] = snapshot.effects
    assert effect[1] is OperationEffect.APPLIED

    # Corrupt the receipt identity while preserving the successful state.  A successful
    # status without an exact connector/operation/target/params/idempotency receipt is
    # unavailable evidence, never APPLIED and never a guessed negative proof.
    broken = copy.deepcopy(fixture.world.store.get_action(fixture.action["action_key"]))
    assert broken is not None and broken["receipt"] is not None
    broken["receipt"]["params_hash"] = "0" * 64
    fixture.world.store.put_action(broken)
    with pytest.raises(SourceUnavailable, match="success_without_matching_receipt"):
        build_operation_snapshot(
            fixture.world.mission.id,
            reader=StoreOperationReader(fixture.world.store),
        )


def test_o09_action_and_exact_link_rollback_together_then_same_command_replays_once(
    tmp_path, monkeypatch
) -> None:
    service, mission, tasks, _config, connectors, _deployment = ledger_service(tmp_path)
    origin = _origin(
        service,
        mission,
        tasks["A"],
        operation_id="operation-o09-atomic",
        occurrence="occ-o09-atomic",
    )
    adapter = PlanningAdmissionStore(service.store)
    before_actions = tuple(service.store.list_actions(mission.id))
    before_events = tuple(service.store.list_events(mission.id))
    original_put = PlanningAdmissionStore.put_operation_action_link

    def fail_between_action_and_link(self, link):
        # ``propose_action`` has already inserted the action inside its transaction when
        # it reaches this seam.  Raising here exercises the exact rollback window.
        assert tuple(self._store.list_actions(mission.id)) != before_actions
        raise RuntimeError("o09 injected link failure")

    monkeypatch.setattr(
        PlanningAdmissionStore, "put_operation_action_link", fail_between_action_and_link
    )
    with pytest.raises(ActionCommitError, match="o09 injected link failure"):
        _propose(service, mission, tasks["A"], connectors, "on", origin)

    assert tuple(service.store.list_actions(mission.id)) == before_actions
    assert tuple(service.store.list_events(mission.id)) == before_events
    assert adapter.get_operation_action_link(origin.operation_id) is None

    monkeypatch.setattr(PlanningAdmissionStore, "put_operation_action_link", original_put)
    first = _propose(service, mission, tasks["A"], connectors, "on", origin)
    actions_after = tuple(service.store.list_actions(mission.id))
    events_after = tuple(service.store.list_events(mission.id))
    link_after = adapter.get_operation_action_link(origin.operation_id)
    assert link_after is not None and link_after["action_key"] == first["action_key"]

    replay = _propose(service, mission, tasks["A"], connectors, "on", origin)
    assert replay == first
    assert tuple(service.store.list_actions(mission.id)) == actions_after
    assert tuple(service.store.list_events(mission.id)) == events_after
    assert adapter.get_operation_action_link(origin.operation_id) == link_after
