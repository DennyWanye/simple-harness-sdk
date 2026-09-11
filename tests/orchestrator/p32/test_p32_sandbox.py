# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 slice A · P32-1 / P32-2 / P32-3: the sandbox executor port and its seatbelt adapter
(plan v3 D1; plan review round 1 P0-2 / P1-2 / P1-4, round 2 P1-2 / P2-1).

The oracles are behavioural: a process started through the port really cannot read a file
outside the read whitelist, write outside its copy and scratch, connect to a socket the
test opened, or signal the test process; a grandchild that left the process group — with
``setsid``, a double fork, or a full daemonize that also changed to ``/`` and closed every
descriptor — is really gone afterwards under the seatbelt adapter (``os.kill(pid, 0)``
raises); an output flood is really cut; a busy loop is really stopped by the CPU limit.
The process-only adapter is not a sandbox and says so (``isolated=False``); the full
daemonize is its registered gap.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from agent_orchestrator.runtime.sandbox import (
    ProcessOnlyExecutor,
    SandboxSpec,
    SeatbeltExecutor,
    probe_sandbox,
)
from agent_orchestrator.runtime.tool_gateway import run_pytest

SEATBELT = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists()
needs_seatbelt = pytest.mark.skipif(not SEATBELT, reason="seatbelt needs macOS sandbox-exec")


def _run(coro, limit=90.0):
    return asyncio.run(asyncio.wait_for(coro, timeout=limit))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie child of ours still answers kill(0); reap it if it is ours
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _script(workspace: Path, name: str, body: str) -> list[str]:
    path = workspace / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


def _spec(**overrides) -> SandboxSpec:
    values = dict(cpu_seconds=20, wall_seconds=20, max_processes=32, max_output_bytes=64_000)
    values.update(overrides)
    return SandboxSpec(**values)


def _executor(adapter: str):
    if adapter == "seatbelt" and not SEATBELT:
        pytest.skip("seatbelt needs macOS sandbox-exec")
    return (
        ProcessOnlyExecutor() if adapter == "process_only" else SeatbeltExecutor.for_interpreter()
    )


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


# ------------------------------------------------------------------ P32-1 capability probe
@needs_seatbelt
def test_p32_1_probe_passes_all_eight_items_on_this_machine():
    executor = SeatbeltExecutor.for_interpreter()
    report = _run(probe_sandbox(executor), limit=120)
    assert [item["name"] for item in report.items] == [
        "read_home",
        "read_other_temp",
        "write_outside",
        "network",
        "signal_host",
        "daemon_reaped",
        "output_truncated",
        "cpu_killed",
    ]
    failed = [item for item in report.items if not item["ok"]]
    assert not failed, failed
    assert report.ok and executor.usable
    assert report.to_json()["environment_digest"] == executor.environment_digest


@needs_seatbelt
def test_p32_1_probe_targets_are_disjoint_from_the_read_whitelist():
    executor = SeatbeltExecutor.for_interpreter()
    report = _run(probe_sandbox(executor), limit=120)
    allowed = [Path(p).resolve() for p in executor.base_read_paths]
    assert report.targets, "the probe names what it tried to reach"
    for target in report.targets:
        resolved = Path(target).resolve()
        assert not any(resolved == a or a in resolved.parents for a in allowed), target


@needs_seatbelt
def test_p32_1_a_widened_profile_fails_the_probe_and_is_unusable():
    # a deployment mistake that lets the sandbox read HOME must be caught by the probe
    executor = SeatbeltExecutor.for_interpreter(extra_read_paths=(str(Path.home()),))
    report = _run(probe_sandbox(executor), limit=120)
    assert not report.ok and not executor.usable
    assert "read_home" in {item["name"] for item in report.items if not item["ok"]}


# ------------------------------------------------------------------ P32-2 behaviour oracle
@needs_seatbelt
def test_p32_2_model_written_test_cannot_escape_the_sandbox(tmp_path, workspace):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("SECRET-P32-2", encoding="utf-8")
    escape = outside / "escaped.txt"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    # an AF_UNIX path must stay short (104 bytes on macOS); /private/tmp is outside the
    # read whitelist just as well
    short = Path(tempfile.mkdtemp(prefix="p32u-", dir="/private/tmp"))
    unix_path = str(short / "u.sock")
    unix = socket.socket(socket.AF_UNIX)
    unix.bind(unix_path)
    unix.listen(1)
    (workspace / "test_escape.py").write_text(
        textwrap.dedent(
            f"""
            import json, os, socket
            out = {{}}
            def attempt(name, fn):
                try:
                    fn(); out[name] = "ALLOWED"
                except Exception as error:
                    out[name] = type(error).__name__
            attempt("read_secret", lambda: open({str(secret)!r}).read())
            attempt("list_home", lambda: os.listdir({str(Path.home())!r}))
            attempt("write_outside", lambda: open({str(escape)!r}, "w").write("x"))
            attempt("tcp", lambda: socket.create_connection(("127.0.0.1", {port}), timeout=2))
            def unix_connect():
                s = socket.socket(socket.AF_UNIX); s.connect({unix_path!r})
            attempt("unix", unix_connect)
            attempt("signal_host", lambda: os.kill({os.getpid()}, 0))
            open("probe.json", "w").write(json.dumps(out))
            def test_still_runs():
                assert True
            """
        ),
        encoding="utf-8",
    )
    executor = SeatbeltExecutor.for_interpreter()
    try:
        run = _run(run_pytest(str(workspace), path=None, timeout=60, executor=executor))
    finally:
        listener.close()
        unix.close()
        shutil.rmtree(short, ignore_errors=True)
    assert run.passed, run.stdout
    observed = json.loads((workspace / "probe.json").read_text())
    assert set(observed) == {
        "read_secret",
        "list_home",
        "write_outside",
        "tcp",
        "unix",
        "signal_host",
    }
    assert all(value != "ALLOWED" for value in observed.values()), observed
    assert "SECRET-P32-2" not in run.stdout
    assert not escape.exists()
    assert run.receipt is not None and run.receipt.kind == "seatbelt" and run.receipt.isolated


@needs_seatbelt
def test_p32_2_user_name_and_time_zone_still_work_inside(workspace):
    command = _script(
        workspace,
        "env_ok.py",
        """
        import datetime, getpass, time
        print(getpass.getuser())
        print(time.localtime().tm_year > 2000, datetime.datetime.now().astimezone().tzinfo is not None)
        """,
    )
    receipt = _run(
        SeatbeltExecutor.for_interpreter().execute(command, cwd=str(workspace), spec=_spec())
    )
    assert receipt.exit_code == 0, receipt.output
    assert "True True" in receipt.output


# ------------------------------------------------------------------ P32-3 no survivors
GROUP_CHILD = """
    import subprocess, time
    child = subprocess.Popen(["/bin/sleep", "600"])
    open("pids.txt", "w").write(str(child.pid))
    time.sleep(600)
"""
SETSID_CHILD = """
    import subprocess, time
    child = subprocess.Popen(["/bin/sleep", "600"], start_new_session=True)
    open("pids.txt", "w").write(str(child.pid))
    time.sleep(600)
"""
DOUBLE_FORK_DAEMON = """
    import os, sys, time
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            open("pids.txt", "w").write(str(os.getpid()))
            time.sleep(600)
        os._exit(0)
    time.sleep(0.5)
    sys.exit(0)  # the command returns at once and leaves the daemon behind
"""
FULL_DAEMONIZE = """
    import os, sys, time
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            open("pids.txt", "w").write(str(os.getpid()))
            os.chdir("/")
            os.closerange(0, 256)
            time.sleep(600)
        os._exit(0)
    time.sleep(0.5)
    sys.exit(0)
"""


@pytest.mark.parametrize(
    "body,expect_timeout",
    [(GROUP_CHILD, True), (SETSID_CHILD, True), (DOUBLE_FORK_DAEMON, False)],
    ids=["same-group", "setsid", "double-fork-daemon"],
)
@pytest.mark.parametrize("adapter", ["process_only", "seatbelt"])
def test_p32_3_no_descendant_survives(adapter, body, expect_timeout, workspace):
    executor = _executor(adapter)
    command = _script(workspace, "spawn.py", body)
    receipt = _run(executor.execute(command, cwd=str(workspace), spec=_spec(wall_seconds=3)))
    pid = int((workspace / "pids.txt").read_text())
    try:
        assert receipt.timed_out is expect_timeout
        assert receipt.tree_killed is True and receipt.residual_pids == ()
        assert _wait_dead(pid), f"descendant {pid} survived"
        assert receipt.status == "ok"
    finally:
        _kill_quietly(pid)


@needs_seatbelt
def test_p32_3_a_full_daemonize_is_reaped_by_sandbox_identity(workspace):
    command = _script(workspace, "daemonize.py", FULL_DAEMONIZE)
    receipt = _run(
        SeatbeltExecutor.for_interpreter().execute(command, cwd=str(workspace), spec=_spec())
    )
    pid = int((workspace / "pids.txt").read_text())
    try:
        assert receipt.tree_killed is True and receipt.residual_pids == ()
        assert _wait_dead(pid), f"daemon {pid} survived the sandbox-identity sweep"
    finally:
        _kill_quietly(pid)


def test_p32_3_process_only_is_not_a_sandbox_and_says_so(workspace):
    # the registered gap: a full daemonize can leave the process-only adapter
    command = _script(workspace, "daemonize.py", FULL_DAEMONIZE)
    receipt = _run(ProcessOnlyExecutor().execute(command, cwd=str(workspace), spec=_spec()))
    try:
        assert receipt.isolated is False and receipt.kind == "process_only"
    finally:
        if (workspace / "pids.txt").exists():
            _kill_quietly(int((workspace / "pids.txt").read_text()))


def test_p32_3_a_survivor_that_cannot_be_killed_makes_the_run_an_error(workspace):
    # seam: a kill that does nothing stands for a process the reaper cannot remove
    executor = ProcessOnlyExecutor(kill=lambda pid: None, max_sweeps=2)
    command = _script(workspace, "spawn.py", SETSID_CHILD)
    try:
        receipt = _run(executor.execute(command, cwd=str(workspace), spec=_spec(wall_seconds=2)))
        pid = int((workspace / "pids.txt").read_text())
        assert receipt.status == "error"
        assert receipt.tree_killed is False
        assert pid in receipt.residual_pids
    finally:
        if (workspace / "pids.txt").exists():
            _kill_quietly(int((workspace / "pids.txt").read_text()))


@pytest.mark.parametrize("adapter", ["process_only", "seatbelt"])
def test_p32_3_output_is_bounded(adapter, workspace):
    executor = _executor(adapter)
    command = _script(workspace, "flood.py", "import sys\nsys.stdout.write('x' * 5_000_000)\n")
    receipt = _run(
        executor.execute(command, cwd=str(workspace), spec=_spec(max_output_bytes=10_000))
    )
    assert receipt.truncated is True
    assert len(receipt.output.encode()) <= 10_000
    # an output flood is cut, never a reason to stop the run (code review round 1 P2-6)
    assert receipt.limit_exceeded is None
    assert receipt.exit_code == 0


@pytest.mark.parametrize("adapter", ["process_only", "seatbelt"])
def test_p32_3_cpu_limit_stops_a_busy_loop(adapter, workspace):
    executor = _executor(adapter)
    command = _script(workspace, "busy.py", "while True:\n    pass\n")
    started = time.monotonic()
    receipt = _run(
        executor.execute(command, cwd=str(workspace), spec=_spec(cpu_seconds=1, wall_seconds=30))
    )
    assert time.monotonic() - started < 15
    assert receipt.limit_exceeded == "cpu"
    assert receipt.effective_limits["cpu_seconds"]["enforcement"] == "hard"


@pytest.mark.parametrize("adapter", ["process_only", "seatbelt"])
def test_p32_3_wall_timeout_is_reported(adapter, workspace):
    executor = _executor(adapter)
    command = _script(workspace, "sleepy.py", "import time\ntime.sleep(60)\n")
    receipt = _run(executor.execute(command, cwd=str(workspace), spec=_spec(wall_seconds=1)))
    assert receipt.timed_out is True and receipt.exit_code is None
    assert receipt.tree_killed is True


# ------------------------------------------------------------------ receipts
def test_receipt_shape_and_limits_are_labelled(workspace):
    executor = ProcessOnlyExecutor()
    command = _script(workspace, "hello.py", "print('hello')\n")
    receipt = _run(executor.execute(command, cwd=str(workspace), spec=_spec()))
    data = receipt.to_json()
    assert data["exit_code"] == 0 and "hello" in data["output"]
    assert data["kind"] == "process_only" and data["isolated"] is False
    assert data["execution_id"] and data["environment_digest"]
    assert data["status"] == "ok"
    limits = data["effective_limits"]
    assert limits["cpu_seconds"]["enforcement"] == "hard"
    assert limits["max_file_bytes"]["enforcement"] == "hard"
    assert limits["wall_seconds"]["enforcement"] == "hard"
    assert limits["max_rss_bytes"]["enforcement"] == "soft"
    assert limits["max_processes"]["enforcement"] == "soft"


@needs_seatbelt
def test_seatbelt_receipt_names_the_sandbox(workspace):
    executor = SeatbeltExecutor.for_interpreter()
    command = _script(workspace, "hello.py", "print('hello')\n")
    receipt = _run(executor.execute(command, cwd=str(workspace), spec=_spec()))
    assert receipt.exit_code == 0, receipt.output
    assert receipt.kind == "seatbelt" and receipt.isolated is True
    assert receipt.environment_digest == executor.environment_digest
