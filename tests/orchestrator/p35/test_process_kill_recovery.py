"""Actual SIGKILL at durable SDK boundaries, followed by a separate cold process.

These deterministic Provider fixtures exercise real SDK/Orchestrator SQLite IO,
not a network Provider. Only the result case uses both runtime profiles. Recovery
is deliberately scoped to collection/verification and bounded scheduler steps;
it does not claim complete Mission delivery or indefinite UNKNOWN liveness.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _p35_process_kill_child import ResultProvider, _orchestrator

from simple_harness.contracts import RunId

CHILD = Path(__file__).with_name("_p35_process_kill_child.py")
MAX_MARKER_SECONDS = 15  # Includes interpreter/import startup; child prepare is 10s.
MAX_RECOVERY_SECONDS = 30  # Hard process deadline, including cancellation/teardown.
STEP_SECONDS = 10
REAP_SECONDS = 3


def _spawn(root, scenario, marker, *, cold=False):
    script = Path(__file__) if cold else CHILD
    log = root.parent / (scenario + ("-cold" if cold else "-warm") + ".stderr.log")
    # A file cannot fill an undrained stderr pipe and deadlock the tested process.
    with log.open("wb") as stderr:
        child = subprocess.Popen(
            [sys.executable, str(script), str(root), scenario, str(marker)],
            cwd=CHILD.parents[3],
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            start_new_session=True,
        )
    return child, log


def _stderr(log):
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 8192))
        return stream.read().decode("utf-8", errors="replace")


def _marker(child, marker, log):
    deadline = time.monotonic() + MAX_MARKER_SECONDS
    while time.monotonic() < deadline:
        if marker.exists():
            return json.loads(marker.read_text(encoding="utf-8"))
        if child.poll() is not None:
            raise AssertionError(f"child exited {child.returncode}: {_stderr(log)}")
        time.sleep(0.02)
    raise AssertionError(f"durable marker timeout: {_stderr(log)}")


def _kill(child):
    os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=REAP_SECONDS)
    assert child.returncode == -signal.SIGKILL


def _cleanup(child):
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait(timeout=REAP_SECONDS)


def _rows(path, query, args=()):
    # Only called after the owning process has exited. A hot rollback journal
    # requires recovery even for SELECT; let SQLite recover an evidence copy,
    # leaving the original DB and sidecars untouched for the actual cold owner.
    # Copy WAL too; immutable=1 or copying only the main file would lose facts.
    with tempfile.TemporaryDirectory(prefix="sqlite-observation-", dir=path.parent) as directory:
        copied = Path(directory) / path.name
        shutil.copyfile(path, copied)
        for suffix in ("-journal", "-wal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_file():
                shutil.copyfile(sidecar, Path(str(copied) + suffix))
        connection = sqlite3.connect(copied)
        try:
            return connection.execute(query, args).fetchall()
        finally:
            connection.close()


def _sdk_rows(root, profile="default"):
    path = root / ("execution.db" if profile == "default" else "execution-critic.db")
    return _rows(
        path,
        "SELECT invocation_id, run_id, state, handoff_attempt, rehandoff_count"
        " FROM provider_invocations ORDER BY invocation_id",
    )


def _durable_boundary(root, marker, scenario):
    paths = [root / name for name in ("orchestrator.db", "execution.db", "execution-critic.db")]
    assert all(path.is_file() for path in paths)
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
    assert _sdk_rows(root, "critic") == []  # Instantiated, not yet used.
    original = _sdk_rows(root)
    assert {r[0] for r in original} == set(marker["invocation_ids"])
    state = "succeeded" if scenario == "sdk-result" else "handed_off"
    assert all(r[1:] == (marker["agent_id"], state, 1, 0) for r in original)
    assert _rows(
        root / "orchestrator.db",
        "SELECT state, agent_id, expected_turn_id FROM dispatch_intents WHERE intent_id=?",
        (marker["intent_id"],),
    ) == [("SUBMITTED", marker["agent_id"], marker["turn_id"])]
    assert (
        _rows(
            root / "orchestrator.db",
            "SELECT result_id FROM results WHERE attempt_id=?",
            (marker["subject_id"],),
        )
        == []
    )
    results = _rows(
        root / "execution.db", "SELECT turn_id, result_hash FROM base_agent_turn_results_v1"
    )
    assert len(results) == (1 if scenario == "sdk-result" else 0)
    if results:
        assert results[0][0] == marker["turn_id"]
    else:
        assert _rows(
            root / "execution.db",
            "SELECT response_json, json_extract(usage_json, '$.usage') FROM provider_invocations",
        ) == [(None, None)]
    return original, results


def _exercise(tmp_path, scenario):
    root = tmp_path / "two-profile-sqlite"
    marker_path = tmp_path / (scenario + ".marker")
    child, log = _spawn(root, scenario, marker_path)
    try:
        marker = _marker(child, marker_path, log)
        assert marker["point"] == (
            "sdk-result-before-orchestrator-collect"
            if scenario == "sdk-result"
            else "provider-handoff-without-usage"
        )
        assert marker["original_worker_calls"] == (2 if scenario == "sdk-result" else 1)
        assert child.poll() is None
        _kill(child)
    finally:
        _cleanup(child)
    original, results = _durable_boundary(root, marker, scenario)
    held_before = _rows(
        root / "orchestrator.db",
        "SELECT state, reserved_tokens, reserved_cost_micros,"
        " settled_tokens, settled_cost_micros"
        " FROM budget_reservations WHERE subject_id=?",
        (marker["subject_id"],),
    )
    grant_bounds_before = _rows(
        root / "orchestrator.db",
        "SELECT invocation_id, total_upper, cost_upper_micros FROM provider_token_grants"
        " WHERE subject_id=? ORDER BY invocation_id",
        (marker["subject_id"],),
    )
    # Actual expiry, no forged clock/lease rows; SDK TTL=1s < Orch lease=4s.
    delay = max(0, marker["lease_expires_at"] - time.time()) + 0.05
    assert delay <= 5
    time.sleep(delay)
    cold, log = _spawn(root, scenario, marker_path, cold=True)
    try:
        try:
            cold.wait(timeout=MAX_RECOVERY_SECONDS)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"cold process exceeded 30s: {_stderr(log)}") from None
        assert cold.returncode == 0, _stderr(log)
    finally:
        _cleanup(cold)
    # Check persisted state again after the cold runtime has shut down.
    after = _sdk_rows(root)
    assert [(r[0], r[1], r[3], r[4]) for r in after] == [(r[0], r[1], r[3], r[4]) for r in original]
    if scenario == "sdk-result":
        assert after == original
        assert (
            _rows(
                root / "execution.db", "SELECT turn_id, result_hash FROM base_agent_turn_results_v1"
            )
            == results
        )
        critic = _sdk_rows(root, "critic")
        assert len(critic) == 2
        assert all(r[1] != marker["agent_id"] and r[2:] == ("succeeded", 1, 0) for r in critic)
    else:
        assert all(r[2] in {"handed_off", "unknown"} for r in after)
        assert _sdk_rows(root, "critic") == []
        assert (
            _rows(
                root / "orchestrator.db",
                "SELECT state, reserved_tokens, reserved_cost_micros,"
                " settled_tokens, settled_cost_micros"
                " FROM budget_reservations WHERE subject_id=?",
                (marker["subject_id"],),
            )
            == held_before
        )
        assert (
            _rows(
                root / "orchestrator.db",
                "SELECT invocation_id, total_upper, cost_upper_micros FROM provider_token_grants"
                " WHERE subject_id=? ORDER BY invocation_id",
                (marker["subject_id"],),
            )
            == grant_bounds_before
        )
        assert _rows(
            root / "orchestrator.db",
            "SELECT state, actual_tokens, actual_cost_micros FROM provider_token_grants"
            " WHERE invocation_id=?",
            (marker["invocation_ids"][0],),
        ) == [("UNKNOWN", None, None)]
        assert _rows(
            root / "orchestrator.db",
            "SELECT state, settled_tokens, settled_cost_micros FROM budget_reservations"
            " WHERE subject_id=?",
            (marker["subject_id"],),
        ) == [("RESERVED", None, None)]


def _grant(orch, invocation_id):
    row = orch.store.connection.execute(
        "SELECT * FROM provider_token_grants WHERE invocation_id=?", (invocation_id,)
    ).fetchone()
    assert row is not None
    return dict(row)


async def _step(awaitable):
    return await asyncio.wait_for(awaitable, timeout=STEP_SECONDS)


async def _recover(root, scenario, marker):
    provider = ResultProvider()
    orch = _orchestrator(root, provider)
    try:
        await _step(orch.__aenter__())
        assert orch._owner != marker["original_owner"]
        intent = orch.store.get_intent(marker["intent_id"])
        assert intent is not None and intent.state == "SUBMITTED"
        assert (intent.agent_id, intent.expected_turn_id) == (marker["agent_id"], marker["turn_id"])
        bridge = orch.bridge_for(intent)
        attempt = orch.store.get_attempt(intent.subject_id)
        assert attempt is not None and attempt.lease_expires_at <= orch.store.now
        with orch.store.transaction():
            reservation = dict(orch.commit.ledger.reservation(intent.subject_id))
        grant_before = _grant(orch, marker["invocation_ids"][0])
        await _step(orch.recover())
        live = await _step(
            bridge.liveness(agent_id=intent.agent_id, turn_id=intent.expected_turn_id)
        )
        orch.commit.renew_lease(
            attempt.id,
            owner=orch._owner,
            lease_seconds=orch._config.lease_seconds,
            liveness=live.to_json(),
        )
        if scenario == "sdk-result":
            assert await _step(orch._collect(intent))
            stored = orch.store.find_result_for_attempt(intent.subject_id)
            assert stored is not None, orch.progress_log[-12:]
            assert stored.envelope.attempt_id == marker["subject_id"]
            assert stored.envelope.task_id == attempt.task_id
            assert provider.calls == 0  # Collection itself never hands off again.
            assert await _step(orch._verify(stored.envelope.id))
            verified = orch.store.get_result(stored.envelope.id)
            assert verified.verdict == "PASS", orch.progress_log[-12:]
            assert orch.store.get_task(attempt.task_id).accepted_result_id == stored.envelope.id
            assert provider.by_role == {"critic": 2}
            for invocation_id in marker["invocation_ids"]:
                grant = _grant(orch, invocation_id)
                assert (grant["state"], grant["actual_tokens"], grant["actual_cost_micros"]) == (
                    "SETTLED",
                    150,
                    200,
                )
            with orch.store.transaction():
                settled = orch.commit.ledger.reservation(intent.subject_id)
            assert (
                settled["state"],
                settled["settled_tokens"],
                settled["settled_cost_micros"],
            ) == ("SETTLED", 300, 400)
            critics = [i for i in orch.store.list_intents("SETTLED") if i.kind == "critic"]
            assert len(critics) == 1 and orch.profile_of(critics[0]) == "critic"
            assert critics[0].config["attempt_id"] == attempt.id
            before = len(orch.store.list_events(intent.mission_id))
            await _step(orch.recover())
            assert not await _step(orch._verify(stored.envelope.id))
            assert len(orch.store.list_events(intent.mission_id)) == before
            assert provider.by_role == {"critic": 2}
        else:
            # Two recovery passes, three real scheduler cycles each. UNKNOWN is
            # allowed to remain in flight; run(max_cycles=N) is NOT a wall timeout.
            for _ in range(2):
                await _step(orch.recover())
                for _ in range(3):
                    await _step(orch._cycle())
                    await asyncio.sleep(0.01)
                grant = _grant(orch, marker["invocation_ids"][0])
                assert grant["state"] == "UNKNOWN"
                assert grant["actual_tokens"] is None and grant["actual_cost_micros"] is None
                assert grant["total_upper"] == grant_before["total_upper"] > 0
                assert grant["cost_upper_micros"] == grant_before["cost_upper_micros"] > 0
                with orch.store.transaction():
                    held = orch.commit.ledger.reservation(intent.subject_id)
                assert held["state"] == "RESERVED"
                assert held["settled_tokens"] is None and held["settled_cost_micros"] is None
                assert held["reserved_tokens"] == reservation["reserved_tokens"] > 0
                assert held["reserved_cost_micros"] == reservation["reserved_cost_micros"] > 0
                assert provider.calls == 0
                assert len(orch.store.list_attempts(attempt.task_id)) == 1
                (record,) = bridge.runtime.uow.list_provider_invocations(RunId(intent.agent_id))
                assert record.invocation_id == marker["invocation_ids"][0]
                assert record.handoff_attempt == 1 and record.rehandoff_count == 0
    finally:
        await asyncio.wait_for(orch.__aexit__(None, None, None), timeout=5)


def test_sigkill_after_sdk_result_before_collection_cold_owner_does_not_rehandoff(tmp_path):
    _exercise(tmp_path, "sdk-result")


def test_sigkill_after_handoff_without_usage_cold_owner_keeps_budget_held(tmp_path):
    _exercise(tmp_path, "handoff")


if __name__ == "__main__":
    asyncio.run(
        _recover(
            Path(sys.argv[1]),
            sys.argv[2],
            json.loads(Path(sys.argv[3]).read_text(encoding="utf-8")),
        )
    )
