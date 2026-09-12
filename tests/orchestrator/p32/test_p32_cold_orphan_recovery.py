"""F-P32-1: real daemonize + SIGKILL owner + separate cold sandbox-identity reap.

No network/model/UI. Failed cleanup preserves original identity, source hashes,
copy and marks. Every child has an outer OS timeout independent of asyncio.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_orchestrator.artifacts.workspace import WorkspaceCleanupIncomplete, WorkspaceManager
from agent_orchestrator.runtime import sandbox

CHILD = Path(__file__).with_name("_p32_cold_orphan_child.py")
SEATBELT = sys.platform == "darwin" and Path(sandbox.SANDBOX_EXEC).is_file()
needs_seatbelt = pytest.mark.skipif(not SEATBELT, reason="real seatbelt requires macOS")


def _spawn(mode, root, marker):
    log = root / (marker.stem + ".stderr.log")
    with log.open("wb") as stream:
        child = subprocess.Popen(
            [sys.executable, str(CHILD), mode, str(root), str(marker)],
            cwd=CHILD.parents[3],
            stdout=subprocess.DEVNULL,
            stderr=stream,
            start_new_session=True,
        )
    return child, log


def _stderr(log):
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 8192))
        return stream.read().decode(errors="replace")


def _cleanup(child):
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait(timeout=3)


def _marker(child, marker, log):
    deadline = time.monotonic() + 20  # includes interpreter/seatbelt preparation
    while time.monotonic() < deadline:
        if marker.exists():
            return json.loads(marker.read_text())
        assert child.poll() is None, _stderr(log)
        time.sleep(0.02)
    raise AssertionError("OS fixture marker exceeded 20s: " + _stderr(log))


def _cold(root, name, mode="reap"):
    marker = root / (name + ".json")
    child, log = _spawn(mode, root, marker)
    try:
        child.wait(timeout=20)  # includes process inventory and teardown
        assert child.returncode == 0, _stderr(log)
        return json.loads(marker.read_text())
    finally:
        _cleanup(child)


def _alive(pid):
    return any(
        p.pid == pid and not p.zombie for p in sandbox._process_table(strict=True, timeout=2)
    )


def _assert_reaped(root, original, result):
    identity = original["identity"]
    assert result["status"] == "ok"
    (report,) = result["reports"]
    assert report["status"] == "reaped" and report["residual_pids"] == []
    assert report["mode"] == "cold"
    assert any(signal["pid"] == original["daemon"]["pid"] for signal in result["signals"])
    assert all(
        signal["copy_and_marks_present"]
        and signal["identity_sha256"] == identity["identity_sha256"]
        for signal in result["signals"]
    )
    assert report["identity"] == identity
    assert (
        identity["source_sha256"] == hashlib.sha256(Path(sandbox.__file__).read_bytes()).hexdigest()
    )
    assert len(identity["identity_sha256"]) == 64
    assert not _alive(original["daemon"]["pid"])
    assert Path(identity["cwd"]).name in result["removed"]
    assert all(not Path(identity[key]).exists() for key in ("cwd", "scratch", "marks"))
    registry = root / "workspaces" / sandbox.SANDBOX_RUNS
    assert not list(registry.glob("*.run.json"))
    assert list(registry.glob("*.cleanup.json"))  # proof outlives deleted copy/marks


@needs_seatbelt
def test_cold_sigkill_owner_reaps_full_daemon_before_deleting_marks_and_copy(tmp_path):
    marker = tmp_path / "host.ready.json"
    host, log = _spawn("host", tmp_path, marker)
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        original = _marker(host, marker, log)
        pid = original["daemon"]["pid"]
        assert _alive(pid)
        # Fresh and artificially old copies both retain their live owner's lock.
        active = _cold(tmp_path, "active")
        assert active["status"] == "ok" and active["removed"] == []
        assert [r["status"] for r in active["reports"]] == ["active"]
        assert _alive(pid) and bystander.poll() is None
        os.killpg(host.pid, signal.SIGKILL)
        host.wait(timeout=3)
        assert host.returncode == -signal.SIGKILL
        assert _alive(pid)  # really escaped the killed host's process group
        _assert_reaped(tmp_path, original, _cold(tmp_path, "recovered"))
        assert bystander.poll() is None
        again = _cold(tmp_path, "again")
        assert again["reports"] == [] and again["removed"] == []
    finally:
        _cleanup(host)
        try:
            _cold(tmp_path, "fixture-final-cleanup")
        finally:
            _cleanup(bystander)


@needs_seatbelt
@pytest.mark.parametrize(
    "fault,status", [("refuse-kill", "residual"), ("scan-unavailable", "unknown")]
)
def test_cold_uncertain_cleanup_preserves_identity_copy_and_marks_until_retry(
    tmp_path, fault, status
):
    marker = tmp_path / "host.ready.json"
    host, log = _spawn("host", tmp_path, marker)
    try:
        original = _marker(host, marker, log)
        os.killpg(host.pid, signal.SIGKILL)
        host.wait(timeout=3)
        assert host.returncode == -signal.SIGKILL
        failed = _cold(tmp_path, "failed-recovery", fault)
        assert failed["status"] == "incomplete" and failed["removed"] == []
        (report,) = failed["reports"]
        assert report["status"] == status and report["identity"] == original["identity"]
        if fault == "refuse-kill":
            assert original["daemon"]["pid"] in report["residual_pids"]
        assert _alive(original["daemon"]["pid"])
        identity = original["identity"]
        assert all(Path(identity[k]).exists() for k in ("cwd", "scratch", "marks"))
        manager = WorkspaceManager(tmp_path / "workspaces")
        copy = manager._workspace(Path(identity["cwd"]), "attempt", True)
        manager.discard(copy)
        assert copy.root.exists()
        with pytest.raises(WorkspaceCleanupIncomplete):
            manager.remove(copy.root.name)
        _assert_reaped(tmp_path, original, _cold(tmp_path, "retry"))
        history = [
            json.loads(p.read_text())
            for p in (tmp_path / "workspaces" / sandbox.SANDBOX_RUNS).glob("*.cleanup.json")
        ]
        assert any(r["status"] == status and r["identity"] == identity for r in history)
    finally:
        _cleanup(host)
        _cold(tmp_path, "fixture-final-cleanup")


@needs_seatbelt
def test_live_residual_receipt_does_not_discard_cold_recovery_identity(tmp_path):
    marker = tmp_path / "live.ready.json"
    host, log = _spawn("live-residual", tmp_path, marker)
    try:
        original = _marker(host, marker, log)
        host.wait(timeout=15)
        assert host.returncode == 0, _stderr(log)
        assert original["receipt"]["status"] == "error"
        assert original["receipt"]["tree_killed"] is False
        assert _alive(original["daemon"]["pid"])
        _assert_reaped(tmp_path, original, _cold(tmp_path, "recovered"))
    finally:
        _cleanup(host)
        _cold(tmp_path, "fixture-final-cleanup")


@pytest.mark.parametrize("returncode", [0, 1])
def test_missing_process_inventory_is_unknown_not_an_empty_success(monkeypatch, returncode):
    from types import SimpleNamespace

    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=returncode, stdout=""),
    )
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox._process_table(strict=True)
