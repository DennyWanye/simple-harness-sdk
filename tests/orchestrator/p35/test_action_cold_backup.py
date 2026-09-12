"""P35 A05/A08 oracle: actual external apply across cold recovery and offline backup.

Submit a Mission through the public Orchestrator; the real scripted Planner and
Worker produce the action artifact, and normal public human approval authorizes
it. TestConfigService, outside every backup root, actually applies exactly once.
Lose its result and SIGKILL the owned child at the durable external-ledger/outbox
boundary. A cold process must preserve the original action identity and key;
STILL_UNKNOWN lookup must retain UNKNOWN and its budget hold without resending.
Close safely, coordinate production backup of both profile execution databases,
the orchestration database and CAS, and restore into a separate root. Remove
access to the original root before opening the actual production Orchestrator
with the same profile identities and an empty Provider. Unknown lookup still
must prevent resend; only the authoritative service's actual original receipt
may reconcile that same action exactly once. External applied_count stays one,
and the original action ID, idempotency key and receipt remain identical.

No Store/Claim/PASS/action seeding, chosen-Task driving, fabricated SDK receipts,
network Provider, or production patches. Child lifetime <=25s, each phase <=10s,
cleanup <=3s. This is a strict assertion oracle, never an xfail/skip workaround;
an API or semantic gap must fail explicitly. Main owns execution; AST is not a
runtime PASS, and this fixture is not production-connector or native UI evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

CHILD = Path(__file__).with_name("_p35_action_cold_child.py")
REPO = Path(__file__).resolve().parents[3]
CHILD_SECONDS = 25
PHASE_SECONDS = 10
CLEANUP_SECONDS = 3


def _source_identity():
    """Actual source attestation; the dirty shared tree is never called a wheel."""
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=CLEANUP_SECONDS,
    ).stdout.strip()
    digest = hashlib.sha256()
    for path in sorted((REPO / "src").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(path.relative_to(REPO).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return {"commit": commit, "production_inputs_sha256": digest.hexdigest()}


def _child(base, mode, *, crash=False):
    """Only this Popen's owned PID is signalled, never a discovered process name."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO / "src")}
    log_path = base / f"{mode}.log"
    started = time.monotonic()
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(CHILD), mode, str(base)],
            cwd=REPO,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            if crash:
                marker = base / "crash.json"
                deadline = started + PHASE_SECONDS
                while not marker.exists():
                    assert process.poll() is None, log_path.read_text(errors="replace")[-8000:]
                    assert time.monotonic() < deadline, "actual apply marker exceeded 10s"
                    time.sleep(0.01)
                boundary = json.loads(marker.read_text())
                assert boundary["pid"] == process.pid
                assert boundary["external_state"]["applied_count"] == 1
                assert boundary["receipt"]["applied"] is True
                process.send_signal(signal.SIGKILL)
                assert process.wait(timeout=CLEANUP_SECONDS) == -signal.SIGKILL
                return boundary
            process.wait(
                timeout=max(0.01, CHILD_SECONDS - CLEANUP_SECONDS - (time.monotonic() - started))
            )
            assert process.returncode == 0, (
                f"{mode} child exit={process.returncode}; "
                + log_path.read_text(errors="replace")[-8000:]
            )
            result = json.loads((base / f"{mode}-done.json").read_text())
            assert result["elapsed_seconds"] < CHILD_SECONDS
            return result
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=CLEANUP_SECONDS)
            assert time.monotonic() - started <= CHILD_SECONDS


def _crash_outbox(base, original):
    """Read-only SQL observes the real commit after OS death; it never seeds state."""
    path = base / "original" / "orchestrator.db"
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM actions WHERE action_key=?", (original["identity"]["action_key"],)
        ).fetchone()
        assert row is not None
        action = json.loads(row["json"])
        assert row["state"] == action["state"] == "HANDED_OFF", (
            "SIGKILL must precede the connector timeout/outcome commit",
            action,
        )
        assert {key: action[key] for key in original["identity"]} == original["identity"]
        assert action["receipt"] is None and action["handoffs"] == 1
        held = db.execute(
            "SELECT state,reserved_tool_calls,settled_tool_calls FROM budget_reservations "
            "WHERE subject_id=?",
            (f"action:{action['action_key']}",),
        ).fetchone()
        assert tuple(held) == ("RESERVED", 1, None)
        return action


def _verify_bundle(base, original, identity):
    bundle = base / "bundle"
    receipt = json.loads((base / "backup.json").read_text())
    manifest_bytes = (bundle / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == receipt["manifest_sha256"]
    manifest = json.loads(manifest_bytes)
    assert manifest["source_identity"] == identity
    assert len(manifest["databases"]) == 3
    assert {d["profile_id"] for d in manifest["databases"] if d["role"] == "execution"} == {
        "default",
        "critic",
    }
    assert manifest["pending_reconciliation"]["orchestrator.db"] == [
        {
            "kind": "action",
            "id": original["identity"]["action_key"],
            "state": "UNKNOWN",
        }
    ]
    for relative, metadata in manifest["files"].items():
        assert hashlib.sha256((bundle / relative).read_bytes()).hexdigest() == metadata["sha256"]
    cas_hashes = {
        metadata["sha256"]
        for relative, metadata in manifest["files"].items()
        if relative.startswith("artifacts/sha256/")
    }
    assert set(original["inventory"]["artifacts"].values()) <= cas_hashes
    assert not any("external" in Path(relative).parts for relative in manifest["files"])


def test_action_applied_receipt_lost_sigkill_cold_and_offline_backup(tmp_path):
    """One A05/A08 cross-case; strict failures preserve logs under pytest tmp_path."""
    started = time.monotonic()
    (tmp_path / "external").mkdir()
    identity = _source_identity()
    (tmp_path / "source-identity.json").write_text(json.dumps(identity))
    original = _child(tmp_path, "crash", crash=True)
    outbox = _crash_outbox(tmp_path, original)
    # Actual lease expiry (no clock jump or SQLite lease edits) precedes cold open.
    deadline = time.monotonic() + PHASE_SECONDS
    while time.time() <= float(outbox["lease_expires_at"]) + 0.05:
        assert time.monotonic() < deadline, "real action lease failed to expire within 10s"
        time.sleep(0.02)
    _child(tmp_path, "cold-backup")
    _verify_bundle(tmp_path, original, identity)
    root, sealed = tmp_path / "original", tmp_path / "sealed-original"
    root.rename(sealed)
    sealed.chmod(0)
    root.write_text("Original storage is deliberately inaccessible for isolated restore.\n")
    try:
        _child(tmp_path, "restore")
        assert _source_identity() == identity, "production inputs changed during the oracle"
    finally:
        sealed.chmod(0o700)
    (tmp_path / "oracle-elapsed.json").write_text(
        json.dumps(
            {
                "elapsed_seconds": time.monotonic() - started,
                "child_limit_seconds": CHILD_SECONDS,
                "phase_limit_seconds": PHASE_SECONDS,
                "cleanup_limit_seconds": CLEANUP_SECONDS,
            }
        )
    )
