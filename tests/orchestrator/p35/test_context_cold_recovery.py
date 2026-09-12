"""Actual SIGKILL after Worker window rotation, then an independent cold process.

Single Mission/Attempt, local deterministic Provider, one runtime profile. This
does not claim multi-Mission pressure, network-provider or BPE-model acceptance.

v20 rework retains the 20s/10s/3s bounds. Its failed evidence remains under
.local-test-evidence/2026-09-12/p33-g/pytest-tmp/g-checkpoint-integration-v20/.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

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
        deadline = time.monotonic() + STAGE_SECONDS
        while not marker_path.exists():
            assert warm.poll() is None, _tail(log)
            assert time.monotonic() < deadline, "warm marker exceeded 10s: " + _tail(log)
            time.sleep(0.01)
        marker = json.loads(marker_path.read_text())
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
