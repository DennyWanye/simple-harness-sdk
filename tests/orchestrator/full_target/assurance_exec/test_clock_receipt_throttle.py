# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-26: clock observations no longer write a receipt on every tick.

A real Host library had 475k ``AssuranceClockObserved`` receipts after two days
(18 per second), and the root check scanned them all on every cycle: the event
loop sat at 100% CPU and the chat channel could not connect.
"""

from __future__ import annotations

from pathlib import Path

from agent_orchestrator.orchestrator.assurance_clock import CLOCK_PERSIST_INTERVAL_MS, observe_assurance_clock
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.storage import schema
from agent_orchestrator.storage.store import Store

T0 = 1_790_000_000_000


def _commit(tmp_path: Path) -> CommitService:
    store = Store.open(tmp_path / "orchestrator.db")
    store.connection.execute("PRAGMA foreign_keys = OFF")
    store.connection.execute(
        "INSERT INTO assurance_environment_state VALUES(1,0,0,?,'STABLE',1,'env-install')", (T0,))
    return CommitService(store)


def _clock_receipts(commit: CommitService) -> int:
    return commit.store.connection.execute(
        "SELECT COUNT(*) FROM commit_receipts WHERE kind='AssuranceClockObserved'").fetchone()[0]


def test_ticks_within_the_interval_write_no_receipt(tmp_path: Path) -> None:
    commit = _commit(tmp_path)
    for step in range(1, 30):
        state = observe_assurance_clock(commit, now_ms=T0 + step * 1000)
        assert state.state == "STABLE" and state.wall_high_ms == T0 + step * 1000
    assert _clock_receipts(commit) == 0


def test_one_receipt_per_interval(tmp_path: Path) -> None:
    commit = _commit(tmp_path)
    observe_assurance_clock(commit, now_ms=T0 + CLOCK_PERSIST_INTERVAL_MS)
    assert _clock_receipts(commit) == 1
    observe_assurance_clock(commit, now_ms=T0 + CLOCK_PERSIST_INTERVAL_MS + 5_000)
    assert _clock_receipts(commit) == 1
    observe_assurance_clock(commit, now_ms=T0 + 2 * CLOCK_PERSIST_INTERVAL_MS)
    assert _clock_receipts(commit) == 2


def test_a_rollback_is_persisted_at_once(tmp_path: Path) -> None:
    commit = _commit(tmp_path)
    observe_assurance_clock(commit, now_ms=T0 + CLOCK_PERSIST_INTERVAL_MS)
    state = observe_assurance_clock(commit, now_ms=T0)
    assert state.state == "ROLLBACK" and state.generation == 1
    assert _clock_receipts(commit) == 2
    row = commit.store.connection.execute(
        "SELECT clock_state, clock_generation FROM assurance_environment_state").fetchone()
    assert tuple(row) == ("ROLLBACK", 1)


def test_the_root_lookup_has_an_index() -> None:
    assert schema.MIGRATIONS[-1].name == "orchestrator-commit-receipts-kind-index"
    assert "commit_receipts(kind)" in schema.MIGRATIONS[-1].ddl
