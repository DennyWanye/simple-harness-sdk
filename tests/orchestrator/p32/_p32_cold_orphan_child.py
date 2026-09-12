"""Owned OS fixture: only pytest's parent launches/kills these bounded processes."""

# ruff: noqa: E402
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from agent_orchestrator.artifacts.workspace import WorkspaceCleanupIncomplete, WorkspaceManager
from agent_orchestrator.runtime import sandbox

# The final child has no parent/session/cwd/fd relationship useful to cold recovery.
DAEMON = r"""
import json, os, sys, time
ready = os.path.abspath("daemon.ready")
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        os.chdir("/")
        os.closerange(0, 256)
        deadline = time.monotonic() + 3
        while os.getppid() != 1 and time.monotonic() < deadline:
            time.sleep(.01)
        payload = {"pid": os.getpid(), "ppid": os.getppid(), "cwd": os.getcwd(), "closed_fds": True}
        with open(ready + ".tmp", "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(ready + ".tmp", ready)
        time.sleep(60)
        os._exit(0)
    os._exit(0)
if sys.argv[1] == "live-residual":
    deadline = time.monotonic() + 5
    while not os.path.exists(ready) and time.monotonic() < deadline:
        time.sleep(.01)
    time.sleep(.1)
else:
    time.sleep(60)
"""


def emit(path, body):
    sandbox._durable_json(path, body)


async def host(root, marker, mode):
    manager = WorkspaceManager(root / "workspaces")
    manager.create("attempt", seed={"daemon.py": DAEMON})
    copy = manager.exec_copy("attempt")
    # Save copy path before spawn, so failed setup can still be reclaimed by identity.
    emit(root / "copy.json", {"cwd": str(copy.root)})
    executor = sandbox.SeatbeltExecutor.for_interpreter(
        exec_root=root / "sandbox-scratch",
        poll_interval=0.02,
        max_sweeps=2,
        **({"kill": lambda pid: None} if mode == "live-residual" else {}),
    )
    running = asyncio.create_task(
        executor.execute(
            [sys.executable, str(copy.root / "daemon.py"), mode],
            cwd=str(copy.root),
            spec=sandbox.SandboxSpec(wall_seconds=45, cpu_seconds=20),
        )
    )

    async def wait_ready():
        while not (copy.root / "daemon.ready").exists():
            if running.done():
                raise AssertionError(f"execution ended before marker: {running.result()}")
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_ready(), 10)
    (index,) = (manager.root / sandbox.SANDBOX_RUNS).glob("*.run.json")
    identity = json.loads(index.read_text())
    daemon = json.loads((copy.root / "daemon.ready").read_text())
    assert daemon["ppid"] == 1 and daemon["cwd"] == "/" and daemon["closed_fds"]
    if mode == "live-residual":
        receipt = await asyncio.wait_for(running, 10)
        manager.discard(copy)  # the real gateway's finally path
        assert receipt.status == "error" and daemon["pid"] in receipt.residual_pids
        assert copy.root.exists() and Path(identity["marks"]).exists()
        emit(marker, {"identity": identity, "daemon": daemon, "receipt": receipt.to_json()})
    else:
        emit(marker, {"identity": identity, "daemon": daemon})
        await running  # deliberately no cancellation/teardown before parent's SIGKILL


def cold(root, marker, fault):
    signals = []
    actual_kill = sandbox._sigkill

    def observed_kill(pid):
        # Observe the actual signal boundary, not just the final filesystem state.
        matching = []
        check = sandbox._sandbox_check()
        for index in (root / "workspaces" / sandbox.SANDBOX_RUNS).glob("*.run.json"):
            identity = json.loads(index.read_text())
            marks = Path(identity["marks"])
            if check(pid, str(marks / "canary")) == 0 and check(pid, str(marks / "decoy")) == 1:
                matching.append(identity)
        assert len(matching) == 1, "cold signal must belong to exactly the original sandbox"
        identity = matching[0]
        assert all(Path(identity[key]).exists() for key in ("cwd", "scratch", "marks"))
        assert json.loads((Path(identity["marks"]) / "identity.json").read_text()) == identity
        signals.append(
            {
                "pid": pid,
                "identity_sha256": identity["identity_sha256"],
                "copy_and_marks_present": True,
            }
        )
        if fault != "refuse-kill":
            actual_kill(pid)

    sandbox._sigkill = observed_kill
    if fault == "scan-unavailable":

        def unavailable(**kwargs):
            raise sandbox.SandboxUnavailable("controlled inventory failure")

        sandbox._process_table = unavailable
    manager = WorkspaceManager(root / "workspaces")
    try:
        removed = manager.sweep_exec_copies(older_than=0)
    except WorkspaceCleanupIncomplete as error:
        emit(
            marker,
            {
                "status": "incomplete",
                "reports": list(error.reports),
                "removed": [],
                "signals": signals,
            },
        )
    else:
        emit(
            marker,
            {
                "status": "ok",
                "reports": list(manager.last_sandbox_cleanup),
                "removed": removed,
                "signals": signals,
            },
        )


if __name__ == "__main__":
    mode, raw_root, raw_marker = sys.argv[1:4]
    root, marker = Path(raw_root).resolve(), Path(raw_marker).resolve()
    if mode in {"host", "live-residual"}:
        asyncio.run(asyncio.wait_for(host(root, marker, mode), 50))
    else:
        cold(root, marker, mode)
