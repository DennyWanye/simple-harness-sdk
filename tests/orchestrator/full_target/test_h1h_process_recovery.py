from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from test_h1h_planning_authorization import _mission, _request

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import Store


_CHILD = r"""
import os
import sys

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.storage.store import Store

db_path, mode = sys.argv[1:]
store = Store.open(db_path)
if mode == "inside":
    store.connection.create_function("process_abort", 0, lambda: os._exit(87))
    store.connection.execute(
        "CREATE TEMP TRIGGER abort_after_grant_insert "
        "AFTER INSERT ON planning_lane_grants "
        "BEGIN SELECT process_abort(); END"
    )
PlanningAuthorizationApi(
    store, tenant_id="tenant-a", principal=Principal("host")
).issue("m-auth", command_id="cmd-process", request_id="req-auth")
os._exit(88)
"""


def _seed(path: Path) -> None:
    store = Store.open(path)
    store.connection.execute("PRAGMA journal_mode = WAL")
    store.insert_mission(_mission(), spec_hash="f" * 64)
    store.connection.execute(
        "INSERT INTO mission_planning_protocols VALUES (?,?,?,?,?,?)",
        ("m-auth", "planning-decision-v1", 1, "p1", "g" * 64, 2.0),
    )
    PlanningDecisionStore(store).insert_planning_request(_request())
    store.close()


def _run_child(path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _CHILD, str(path), mode],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _authority_rows(store: Store) -> tuple[list[tuple], list[tuple]]:
    grants = store.connection.execute(
        "SELECT grant_id,revision,issuer_command_id,issuer_receipt_hash "
        "FROM planning_lane_grants ORDER BY grant_id,revision"
    ).fetchall()
    bindings = store.connection.execute(
        "SELECT request_id,grant_id,grant_revision,grant_hash "
        "FROM planning_request_authority_bindings ORDER BY request_id"
    ).fetchall()
    return ([tuple(row) for row in grants], [tuple(row) for row in bindings])


def test_i03_process_exit_between_grant_and_side_binding_recovers_neither(tmp_path: Path) -> None:
    path = tmp_path / "i03-inside.db"
    _seed(path)

    child = _run_child(path, "inside")
    assert child.returncode == 87, (child.stdout, child.stderr)

    recovered = Store.open(path)
    assert _authority_rows(recovered) == ([], [])
    # Recovery is observational: reopening must not mint authority on its own.
    recovered.close()
    reopened = Store.open(path)
    assert _authority_rows(reopened) == ([], [])
    reopened.close()


def test_i03_process_exit_after_issue_replays_original_receipt_without_resigning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "i03-after.db"
    _seed(path)

    child = _run_child(path, "after")
    assert child.returncode == 88, (child.stdout, child.stderr)

    recovered = Store.open(path)
    grants_before, bindings_before = _authority_rows(recovered)
    assert len(grants_before) == len(bindings_before) == 1
    receipt_hash = grants_before[0][3]
    changes_before = recovered.connection.total_changes

    replay = PlanningAuthorizationApi(
        recovered, tenant_id="tenant-a", principal=Principal("host")
    ).issue("m-auth", command_id="cmd-process", request_id="req-auth")

    assert replay.issuer_receipt_hash == receipt_hash
    assert recovered.connection.total_changes == changes_before
    assert _authority_rows(recovered) == (grants_before, bindings_before)
    recovered.close()
