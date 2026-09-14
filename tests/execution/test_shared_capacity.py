# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import multiprocessing
import os
import queue
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from simple_harness.execution.shared_capacity import (
    CapacityConfigurationError,
    CapacityKeyError,
    CapacityLedger,
    CapacityTransitionError,
    InvalidCapacityTicket,
)


def _contend(path: Path, key: str, weight: int, start, release, output) -> None:  # type: ignore[no-untyped-def]
    ledger = CapacityLedger(path, pool_id="physical-a", max_slots=2, max_tokens=2)
    owner = f"{key}:{os.getpid()}:{uuid4().hex}"
    ticket = ledger.enqueue(key, weight=weight, owner=owner, pid=os.getpid())
    output.put(("queued", key))
    start.wait(10)
    deadline = time.monotonic() + 10
    while not ledger.try_acquire(ticket):
        if time.monotonic() >= deadline:
            raise TimeoutError(key)
        time.sleep(0.01)
    output.put(("acquired", key))
    release.wait(10)
    ledger.finish(ticket, known_terminal=False)


def _hold_handoff(path: Path, output, stop) -> None:  # type: ignore[no-untyped-def]
    ledger = CapacityLedger(path, pool_id="physical-a", max_slots=1, max_tokens=3)
    ticket = ledger.enqueue("outbound", weight=3, owner=f"child:{uuid4().hex}", pid=os.getpid())
    assert ledger.try_acquire(ticket)
    ledger.mark_handed_off(ticket)
    output.put(ticket)
    stop.wait(30)


def _hold_before_handoff(path: Path, reserve: bool, output, stop) -> None:  # type: ignore[no-untyped-def]
    ledger = CapacityLedger(path, pool_id="physical-a", max_slots=1, max_tokens=1)
    ticket = ledger.enqueue("waiting", weight=1, owner=f"child:{uuid4().hex}", pid=os.getpid())
    if reserve:
        assert ledger.try_acquire(ticket)
    output.put(ticket)
    stop.wait(30)


def _message(output, expected: tuple[str, str]) -> None:  # type: ignore[no-untyped-def]
    assert output.get(timeout=10) == expected


def _stop(processes: list[multiprocessing.Process]) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
        process.join(5)


def test_spawn_processes_keep_weighted_fifo_across_the_pool(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "capacity.sqlite3"
    CapacityLedger(path, pool_id="physical-a", max_slots=2, max_tokens=2)
    output = context.Queue()
    starts = [context.Event() for _ in range(3)]
    releases = [context.Event() for _ in range(3)]
    specs = (("first", 2), ("second", 2), ("third", 1))
    processes: list[multiprocessing.Process] = []
    try:
        for index, (key, weight) in enumerate(specs):
            process = context.Process(
                target=_contend,
                args=(path, key, weight, starts[index], releases[index], output),
            )
            process.start()
            processes.append(process)
            _message(output, ("queued", key))
            if index == 0:
                starts[0].set()
                _message(output, ("acquired", "first"))

        starts[1].set()
        starts[2].set()
        with pytest.raises(queue.Empty):
            output.get(timeout=0.2)
        releases[0].set()
        _message(output, ("acquired", "second"))
        with pytest.raises(queue.Empty):
            output.get(timeout=0.2)
        releases[1].set()
        _message(output, ("acquired", "third"))
        releases[2].set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        _stop(processes)


def test_cancel_front_waiter_allows_next_without_capacity_leak(tmp_path: Path) -> None:
    ledger = CapacityLedger(tmp_path / "capacity.sqlite3", pool_id="p", max_slots=1, max_tokens=1)
    first = ledger.enqueue("first", weight=1, owner="epoch-a", pid=os.getpid())
    second = ledger.enqueue("second", weight=1, owner="epoch-b", pid=os.getpid())
    with pytest.raises(CapacityTransitionError, match="WAITING"):
        ledger.reconcile("first", evidence_ref="sdk-record:first:1:v1")
    ledger.cancel_waiter(first)
    assert ledger.try_acquire(second)
    with pytest.raises(CapacityTransitionError, match="RESERVED"):
        ledger.reconcile("second", evidence_ref="sdk-record:second:1:v1")
    ledger.cancel_waiter(second)
    snapshot = ledger.snapshot()
    assert (snapshot.held_slots, snapshot.held_tokens) == (0, 0)
    assert [row.state for row in snapshot.rows] == ["RELEASED", "RELEASED"]


def test_killed_handoff_becomes_unknown_and_stays_held(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "capacity.sqlite3"
    output, stop = context.Queue(), context.Event()
    process = context.Process(target=_hold_handoff, args=(path, output, stop))
    process.start()
    ticket = output.get(timeout=10)
    process.terminate()
    process.join(10)
    ledger = CapacityLedger(path, pool_id="physical-a", max_slots=1, max_tokens=3)
    snapshot = ledger.snapshot()
    assert (snapshot.rows[0].state, snapshot.held_slots, snapshot.held_tokens) == (
        "UNKNOWN",
        1,
        3,
    )
    with pytest.raises(CapacityTransitionError):
        ledger.cancel_waiter(ticket)
    ledger.finish(ticket, known_terminal=False)
    assert ledger.snapshot().rows[0].state == "UNKNOWN"
    proof = "sdk-record:outbound:ordinal-1:v3"
    ledger.reconcile("outbound", evidence_ref=proof)
    settled = CapacityLedger(path, pool_id="physical-a", max_slots=1, max_tokens=3).snapshot()
    assert (settled.rows[0].state, settled.rows[0].evidence_ref) == ("SETTLED", proof)
    assert (settled.held_slots, settled.held_tokens) == (0, 0)
    ledger.reconcile("outbound", evidence_ref=proof)
    with pytest.raises(CapacityTransitionError, match="different"):
        ledger.reconcile("outbound", evidence_ref="sdk-record:outbound:ordinal-1:v4")


@pytest.mark.parametrize("reserve", (False, True), ids=("waiting", "reserved"))
def test_killed_pre_handoff_work_is_released_without_leak(tmp_path: Path, reserve: bool) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "capacity.sqlite3"
    output, stop = context.Queue(), context.Event()
    process = context.Process(target=_hold_before_handoff, args=(path, reserve, output, stop))
    process.start()
    output.get(timeout=10)
    process.terminate()
    process.join(10)
    snapshot = CapacityLedger(path, pool_id="physical-a", max_slots=1, max_tokens=1).snapshot()
    assert snapshot.rows[0].state == "RELEASED"
    assert (snapshot.held_slots, snapshot.held_tokens) == (0, 0)


def test_owner_epoch_blocks_stale_callback_and_pool_limits_are_fixed(tmp_path: Path) -> None:
    path = tmp_path / "capacity.sqlite3"
    ledger = CapacityLedger(path, pool_id="physical-a", max_slots=1, max_tokens=2)
    ticket = ledger.enqueue("key", weight=2, owner="current-epoch", pid=os.getpid())
    stale = replace(ticket, owner="old-epoch")
    with pytest.raises(InvalidCapacityTicket):
        ledger.try_acquire(stale)
    assert ledger.try_acquire(ticket)
    with pytest.raises(CapacityConfigurationError):
        CapacityLedger(path, pool_id="physical-a", max_slots=2, max_tokens=2)
    other = CapacityLedger(path, pool_id="physical-b", max_slots=2, max_tokens=4)
    assert other.snapshot().rows == ()


def test_enqueue_replay_and_strict_arguments_never_reissue_terminal_key(tmp_path: Path) -> None:
    ledger = CapacityLedger(tmp_path / "capacity.sqlite3", pool_id="p", max_slots=1, max_tokens=2)
    ticket = ledger.enqueue("key", weight=2, owner="epoch", pid=os.getpid())
    assert ledger.enqueue("key", weight=2, owner="epoch", pid=os.getpid()) == ticket
    with pytest.raises(CapacityKeyError):
        ledger.enqueue("key", weight=1, owner="epoch", pid=os.getpid())
    with pytest.raises(CapacityKeyError):
        ledger.enqueue("key", weight=2, owner="other", pid=os.getpid())
    ledger.cancel_waiter(ticket)
    with pytest.raises(CapacityKeyError):
        ledger.enqueue("key", weight=2, owner="epoch", pid=os.getpid())
    with pytest.raises(ValueError, match="exceeds"):
        ledger.enqueue("heavy", weight=3, owner="epoch", pid=os.getpid())
    with pytest.raises(TypeError, match="weight"):
        ledger.enqueue("bool", weight=True, owner="epoch", pid=os.getpid())
    with pytest.raises(TypeError, match="known_terminal"):
        ledger.finish(ticket, known_terminal=1)  # type: ignore[arg-type]
    with pytest.raises(CapacityTransitionError):
        ledger.cancel_waiter(ticket)


@pytest.mark.parametrize("phase", ["WAITING", "RESERVED", "HANDED_OFF"])
def test_reused_pid_does_not_inherit_old_process_capacity(tmp_path, monkeypatch, phase):
    from simple_harness.execution import shared_capacity as module

    monkeypatch.setattr(module, "_process_identity", lambda pid: "identity-100")
    ledger = CapacityLedger(tmp_path / "capacity.db", pool_id="p", max_slots=1, max_tokens=10)
    ticket = ledger.enqueue("old", weight=10, owner="old-process", pid=os.getpid())
    if phase != "WAITING":
        assert ledger.try_acquire(ticket)
    if phase == "HANDED_OFF":
        ledger.mark_handed_off(ticket)
    # Same numeric PID, but the kernel reports a different process start identity.
    monkeypatch.setattr(module, "_process_identity", lambda pid: "identity-200")
    row = ledger.snapshot().rows[0]
    assert row.state == ("UNKNOWN" if phase == "HANDED_OFF" else "RELEASED")
    assert ledger.snapshot().held_slots == (1 if phase == "HANDED_OFF" else 0)


@pytest.mark.parametrize("phase", ["WAITING", "RESERVED", "HANDED_OFF"])
def test_wall_clock_adjustment_does_not_evict_live_owner(tmp_path, monkeypatch, phase):
    import psutil

    from simple_harness.execution import shared_capacity as module

    ledger = CapacityLedger(tmp_path / "capacity.db", pool_id="p", max_slots=1, max_tokens=10)
    ticket = ledger.enqueue("live", weight=10, owner="live", pid=os.getpid())
    if phase != "WAITING":
        assert ledger.try_acquire(ticket)
    if phase == "HANDED_OFF":
        ledger.mark_handed_off(ticket)
    real_create_time = psutil.Process.create_time
    monkeypatch.setattr(psutil.Process, "create_time", lambda self: real_create_time(self) + 120)
    assert ledger.snapshot().rows[0].state == phase
    assert module._process_identity(os.getpid()) is not None


def test_legacy_wall_clock_identity_is_never_compared_with_stable_identity(tmp_path):
    import sqlite3

    path = tmp_path / "capacity.db"
    ledger = CapacityLedger(path, pool_id="p", max_slots=1, max_tokens=10)
    ticket = ledger.enqueue("legacy", weight=10, owner="old", pid=os.getpid())
    assert ledger.try_acquire(ticket)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE shared_capacity_entries ADD COLUMN process_started REAL")
        connection.execute(
            "UPDATE shared_capacity_entries SET process_identity=NULL, process_started=1.0"
        )
    reopened = CapacityLedger(path, pool_id="p", max_slots=1, max_tokens=10)
    assert reopened.snapshot().rows[0].state == "RESERVED"
