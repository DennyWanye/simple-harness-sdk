# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-26 (Host 真机): queued Assurance work is not a stall.

All six steps of a Mission were accepted; the root's final review answered with
malformed JSON and its one format repair waited in the REVIEW queue.  The stall
check ran in the same idle cycle and failed the Mission as NO_DISPATCHABLE_WORK.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from agent_orchestrator.orchestrator.event_handler import Orchestrator


def _pending(*rows: tuple[str, str, str | None]) -> bool:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE assurance_pending_work(mission_id, consumer, work_key, state, wait_reason)")
    connection.executemany(
        "INSERT INTO assurance_pending_work VALUES (?,?,?,?,?)",
        [(mission, "REVIEW", f"w{i}", state, reason) for i, (mission, state, reason) in enumerate(rows)],
    )
    fake = SimpleNamespace(store=SimpleNamespace(connection=connection))
    return Orchestrator._has_pending_assurance_work(fake, "m1")


def test_a_queued_format_repair_holds_off_the_stall() -> None:
    assert _pending(("m1", "WAITING", "RECHECK_REQUIRED"))
    assert _pending(("m1", "PENDING", None))
    assert _pending(("m1", "RUNNING", None))


def test_finished_manual_or_foreign_work_does_not() -> None:
    assert not _pending(("m1", "DONE", None), ("m1", "REJECTED", None))
    assert not _pending(("m1", "WAITING", "MANUAL_REQUIRED"))
    assert not _pending(("m2", "PENDING", None))
    assert not Orchestrator._has_pending_assurance_work(
        SimpleNamespace(store=SimpleNamespace(connection=sqlite3.connect(":memory:"))), "m1"
    )
