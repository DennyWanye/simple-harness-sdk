"""Actual SIGKILL after Worker window rotation, then an independent cold process.

Single Mission/Attempt, local deterministic Provider, one runtime profile. This
does not claim multi-Mission pressure, network-provider or BPE-model acceptance.

v20 rework retains the 20s/10s/3s bounds. Its failed evidence remains under
.local-test-evidence/2026-09-12/p33-g/pytest-tmp/g-checkpoint-integration-v20/.

The warm parent uses the 20s child bound for the whole subprocess and the 10s
stage bound as a startup/no-progress watchdog. This keeps import startup separate
from runtime progress and lets the child report which phase stalled.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _p35_context_cold_child import (
    CHILD_SECONDS,
    READS,
    STAGE_SECONDS,
    TEARDOWN_SECONDS,
    assert_request_identities,
    rows,
    snapshot,
)

CHILD = Path(__file__).with_name("_p35_context_cold_child.py")


def _spawn(mode, root, marker, result):
    log = root.parent / f"context-{mode}.stderr.log"
    with log.open("wb") as stream:
        child = subprocess.Popen(
            [sys.executable, str(CHILD), mode, str(root), str(marker), str(result)],
            cwd=CHILD.parents[3],
            stdout=subprocess.DEVNULL,
            stderr=stream,
            start_new_session=True,
        )
    return child, log


def _tail(log):
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 8192))
        return stream.read().decode(errors="replace")


def _progress_path(marker):
    return marker.with_suffix(".progress.json")


def _wait_for_warm_marker(
    child,
    marker,
    log,
    *,
    monotonic=time.monotonic,
    sleep=time.sleep,
):
    started = monotonic()
    hard_deadline = started + CHILD_SECONDS
    last_progress_at = started
    last_progress = None
    progress_path = _progress_path(marker)
    while not marker.exists():
        assert child.poll() is None, _tail(log)
        now = monotonic()
        if progress_path.exists():
            progress = json.loads(progress_path.read_text())
            if progress != last_progress:
                last_progress = progress
                last_progress_at = now
        if last_progress is None:
            assert now - started < STAGE_SECONDS, (
                f"warm child startup made no progress for {STAGE_SECONDS}s: " + _tail(log)
            )
        else:
            assert now - last_progress_at < STAGE_SECONDS, (
                f"warm child runtime made no progress for {STAGE_SECONDS}s: "
                + json.dumps(last_progress, sort_keys=True)
                + "; stderr="
                + _tail(log)
            )
        assert now < hard_deadline, (
            f"warm child exceeded {CHILD_SECONDS}s hard limit: progress="
            + json.dumps(last_progress, sort_keys=True)
            + "; stderr="
            + _tail(log)
        )
        sleep(0.01)
    return json.loads(marker.read_text())


class _RunningChild:
    @staticmethod
    def poll():
        return None


def test_warm_marker_watchdog_rejects_no_startup_progress(tmp_path):
    marker, log = tmp_path / "warm.json", tmp_path / "warm.stderr.log"
    log.write_text("")
    now = [0.0]

    def advance(_seconds):
        now[0] += 1.0

    with pytest.raises(AssertionError, match="startup made no progress for 10s"):
        _wait_for_warm_marker(
            _RunningChild(), marker, log, monotonic=lambda: now[0], sleep=advance
        )
    assert now[0] == STAGE_SECONDS


def test_warm_marker_watchdog_caps_continuous_progress_at_child_limit(tmp_path):
    marker, log = tmp_path / "warm.json", tmp_path / "warm.stderr.log"
    progress = _progress_path(marker)
    log.write_text("")
    now = [0.0]

    def advance(_seconds):
        now[0] += 1.0
        progress.write_text(json.dumps({"phase": "still_running", "tick": now[0]}))

    with pytest.raises(AssertionError, match="exceeded 20s hard limit"):
        _wait_for_warm_marker(
            _RunningChild(), marker, log, monotonic=lambda: now[0], sleep=advance
        )
    assert now[0] == CHILD_SECONDS


def _cleanup(child):
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait(timeout=TEARDOWN_SECONDS)


def test_rotated_worker_context_sigkill_cold_unknown_preserves_frozen_request(tmp_path):
    root = tmp_path / "context-orchestrator"
    marker_path, result_path = tmp_path / "warm.json", tmp_path / "cold.json"
    warm, log = _spawn("warm", root, marker_path, result_path)
    try:
        marker = _wait_for_warm_marker(warm, marker_path, log)
        assert marker["pid"] == warm.pid and marker["provider_calls"] == READS + 1
        assert marker["lease_expires_at"] > time.time(), "warm boundary lost Orch authority"
        assert marker["selection"]["dropped_ranges"]
        assert marker["source_sha256"] == hashlib.sha256(CHILD.read_bytes()).hexdigest()
        assert warm.poll() is None
        os.killpg(warm.pid, signal.SIGKILL)
        warm.wait(timeout=TEARDOWN_SECONDS)
        assert warm.returncode == -signal.SIGKILL
    finally:
        _cleanup(warm)
    assert snapshot(root, marker["agent_id"]) == marker["snapshot"]
    assert (
        assert_request_identities(
            snapshot(root, marker["agent_id"]),
            marker["invocation_id"],
            marker["selection"],
            marker["wire_request"],
        )
        == marker["request_identity"]
    )
    db = root / "execution.db"
    query = (
        "SELECT state,response_json,json_extract(usage_json,'$.usage') AS actual_usage,"
        "handoff_attempt,rehandoff_count FROM provider_invocations WHERE invocation_id=?"
    )
    assert rows(db, query, (marker["invocation_id"],)) == [
        {
            "state": "handed_off",
            "response_json": None,
            "actual_usage": None,
            "handoff_attempt": 1,
            "rehandoff_count": 0,
        }
    ]
    (sdk_lease,) = rows(
        db,
        "SELECT expires_at FROM workflow_leases WHERE run_id=? AND namespace='runtime.kernel'",
        (marker["agent_id"],),
    )
    # The long turn can outlive its original Orch lease while SDK heartbeats keep
    # renewing. Read the final durable SDK expiry after SIGKILL as well.
    deadline = max(marker["lease_expires_at"], sdk_lease["expires_at"])
    delay = max(0, deadline - time.time()) + 0.05
    assert delay < 2
    time.sleep(delay)  # real expiry; never rewrite lease/time rows
    cold, log = _spawn("cold", root, marker_path, result_path)
    try:
        try:
            cold.wait(timeout=CHILD_SECONDS)
        except subprocess.TimeoutExpired:
            raise AssertionError("cold child exceeded 20s: " + _tail(log)) from None
        assert cold.returncode == 0, _tail(log)
        result = json.loads(result_path.read_text())
        assert result["pid"] == cold.pid and cold.pid != warm.pid
        assert result["provider_calls"] == 0 and result["retrieval"] == {
            "prewarm": 0,
            "search": 0,
        }, (
            f"cold receipt: provider_calls={result['provider_calls']}, "
            f"retrieval={result['retrieval']}"
        )
    finally:
        _cleanup(cold)
    assert snapshot(root, marker["agent_id"]) == marker["snapshot"]
    assert (
        assert_request_identities(
            snapshot(root, marker["agent_id"]),
            marker["invocation_id"],
            marker["selection"],
            marker["wire_request"],
        )
        == marker["request_identity"]
    )
    (pending,) = rows(db, query, (marker["invocation_id"],))
    assert pending["state"] in {"handed_off", "unknown"}
    assert pending["response_json"] is None and pending["actual_usage"] is None
    assert pending["handoff_attempt"] == 1 and pending["rehandoff_count"] == 0
