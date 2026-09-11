# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The sandbox executor port and its two adapters (P3.2 plan v3 D1 / D2; plan review
round 1 P0-2 / P1-2 / P1-3 / P1-4, round 2 P1-2 / P2-1 / P2-9).

Every piece of model-written code this build runs — ``run_tests``, the ``code_test`` layer,
Mission ``pytest:`` criteria, the evaluator's oracle — goes through
:meth:`SandboxExecutorPort.execute`, which returns an :class:`ExecutionReceipt` saying what
really happened: exit code, bounded output (the tail is kept, where pytest puts its
summary), whether the wall clock or a limit stopped it, and whether every process of the
run is gone — measured after the reaping, never assumed.

``SeatbeltExecutor`` (macOS ``sandbox-exec``) is the sandbox: no network, no file content
outside a read whitelist computed from the interpreter it runs, no writes outside the run's
own copy and scratch directory, no signals to processes outside the sandbox, no mach
lookups, no information about other processes.  Its processes are found by *sandbox
identity*: each run's profile allows reading one canary path and denies a decoy, and
``sandbox_check()`` asked about every process of this user picks out exactly the processes
of this run — however they left the process tree (``setsid``, double fork, ``chdir("/")``,
closed descriptors).  ``ProcessOnlyExecutor`` is not a sandbox and says so
(``isolated=False``): it follows the parent chain and the run's directory, and a full
daemonize can leave it — for trusted code only.

Honest limits (plan §2): CPU time and file size are hard limits (``setrlimit``, inherited
by every descendant); memory and the process count are *soft* limits — sampled while the
run lasts and enforced by reaping, because this platform has no cgroups, ``RLIMIT_AS`` /
``RLIMIT_DATA`` cannot be set and ``RLIMIT_NPROC`` counts every process of the user.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import pwd
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

SANDBOX_VERSION = "sandbox-v1"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PS = "/bin/ps"
LSOF = "/usr/sbin/lsof"
DEFAULT_PATH = "/usr/bin:/bin"
# read-only system locations every interpreter needs (plan v3 D1; "/" itself is listed by
# the interpreter at start-up — without it every program aborts, review round 2 P2-1)
SYSTEM_READ_PATHS = ("/System", "/usr", "/bin", "/private/etc", "/private/var/db/timezone")
DEVICE_READ_LITERALS = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom")
PROBE_SECRET = "P32-PROBE-SECRET"


class SandboxUnavailable(RuntimeError):
    """The deployment asked for a sandbox this machine cannot provide (plan D2)."""


# ------------------------------------------------------------------ contracts
@dataclass(frozen=True, slots=True)
class SandboxSpec:
    cpu_seconds: int = 120
    wall_seconds: float = 120.0
    max_processes: int = 64
    max_rss_bytes: int = 2 * 1024 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    env: Mapping[str, str] = field(default_factory=dict)
    network: str = "none"

    def __post_init__(self) -> None:
        if self.network != "none":
            raise ValueError("this build only runs model-written code without a network")
        if self.cpu_seconds < 1 or self.wall_seconds <= 0 or self.max_output_bytes < 1:
            raise ValueError("sandbox limits must be positive")

    def effective_limits(self, *, isolated: bool = True) -> dict[str, dict[str, Any]]:
        """What this run is really held to.  ``cpu_seconds`` is a *per-process* rlimit, not
        a total for the run; the network is only denied when an adapter isolates the run —
        an unsandboxed one says so rather than claiming a limit it does not apply (code
        review round 1 P1-3 / P2-5)."""

        return {
            "cpu_seconds": {"value": self.cpu_seconds, "enforcement": "hard", "scope": "process"},
            "wall_seconds": {"value": self.wall_seconds, "enforcement": "hard"},
            "max_file_bytes": {"value": self.max_file_bytes, "enforcement": "hard"},
            "max_output_bytes": {"value": self.max_output_bytes, "enforcement": "hard"},
            "max_rss_bytes": {"value": self.max_rss_bytes, "enforcement": "soft"},
            "max_processes": {"value": self.max_processes, "enforcement": "soft"},
            "network": (
                {"value": self.network, "enforcement": "hard"}
                if isolated
                else {"value": "unrestricted", "enforcement": "none"}
            ),
        }


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    execution_id: str
    kind: str  # seatbelt | process_only
    isolated: bool
    environment_digest: str
    effective_limits: Mapping[str, Any]
    exit_code: int | None
    output: str
    truncated: bool
    timed_out: bool
    limit_exceeded: str | None  # cpu | rss | processes
    tree_killed: bool
    residual_pids: tuple[int, ...]
    status: str  # ok | error (a process of the run could not be removed)

    def to_json(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "kind": self.kind,
            "isolated": self.isolated,
            "environment_digest": self.environment_digest,
            "effective_limits": dict(self.effective_limits),
            "exit_code": self.exit_code,
            "output": self.output,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
            "limit_exceeded": self.limit_exceeded,
            "tree_killed": self.tree_killed,
            "residual_pids": list(self.residual_pids),
            "status": self.status,
        }


class SandboxExecutorPort(Protocol):
    kind: str
    isolated: bool
    interpreter: str

    @property
    def environment_digest(self) -> str: ...

    async def execute(
        self, command: Sequence[str], *, cwd: str, spec: SandboxSpec
    ) -> ExecutionReceipt: ...


# ------------------------------------------------------------------ process table
@dataclass(frozen=True, slots=True)
class _Proc:
    pid: int
    ppid: int
    uid: int
    rss_kb: int
    zombie: bool


def _process_table() -> list[_Proc]:
    try:
        out = subprocess.run(
            [PS, "-A", "-o", "pid=,ppid=,uid=,rss=,stat="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    table = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            pid, ppid, uid, rss = (int(value) for value in parts[:4])
            table.append(_Proc(pid, ppid, uid, rss, parts[4][:1] == "Z"))
        except ValueError:
            continue
    return table


def _descendants(table: Sequence[_Proc], root: int) -> set[int]:
    found = {root}
    changed = True
    while changed:
        changed = False
        for proc in table:
            if proc.ppid in found and proc.pid not in found:
                found.add(proc.pid)
                changed = True
    return found


def _sigkill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _preexec(cpu_seconds: int, max_file_bytes: int) -> Callable[[], None]:
    def apply() -> None:  # runs in the child before exec: inherited by every descendant
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))

    return apply


class _Tail:
    """Keeps the last ``cap`` bytes of a stream (pytest's summary is at the end)."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.data = bytearray()
        self.total = 0

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            self.total += len(chunk)
            self.data.extend(chunk)
            if len(self.data) > self.cap:
                del self.data[: len(self.data) - self.cap]

    def text(self) -> tuple[str, bool]:
        truncated = self.total > self.cap
        # a character cut at the front is dropped rather than replaced: the bound holds
        return self.data.decode("utf-8", "ignore" if truncated else "replace"), truncated


# ------------------------------------------------------------------ the shared engine
class _Executor:
    kind = ""
    isolated = False

    def __init__(
        self,
        interpreter: str | None = None,
        *,
        poll_interval: float = 0.1,
        kill: Callable[[int], None] | None = None,
        max_sweeps: int = 6,
        exec_root: str | Path | None = None,
    ) -> None:
        self.interpreter = str(interpreter or sys.executable)
        self._poll = poll_interval
        self._kill = kill or _sigkill  # seam: what removes a descendant
        self._max_sweeps = max(1, max_sweeps)
        self._exec_root = (
            Path(exec_root)
            if exec_root is not None
            else Path(tempfile.gettempdir()).resolve() / f"orch-exec-{os.getuid()}"
        )

    @property
    def environment_digest(self) -> str:
        return _digest({"kind": self.kind, "version": SANDBOX_VERSION, **self._identity()})

    def _identity(self) -> dict[str, Any]:
        return {"interpreter": os.path.realpath(self.interpreter)}

    # subclass hooks -------------------------------------------------------
    def _argv(self, command: Sequence[str], run: _Run) -> list[str]:
        return list(command)

    def _members(self, run: _Run) -> set[int]:
        raise NotImplementedError

    def _survivors(self, run: _Run) -> set[int]:
        raise NotImplementedError

    # the run ----------------------------------------------------------------
    async def execute(
        self, command: Sequence[str], *, cwd: str, spec: SandboxSpec
    ) -> ExecutionReceipt:
        run = _Run.start(self._exec_root, Path(cwd).resolve())
        env = {
            "PATH": DEFAULT_PATH,
            "HOME": str(run.scratch),
            "TMPDIR": str(run.scratch / "tmp"),
            "USER": run.user,
            "LOGNAME": run.user,
            "LANG": os.environ.get("LANG") or "en_US.UTF-8",
            **dict(spec.env),
        }
        tail = _Tail(spec.max_output_bytes)
        timed_out = False
        limit: str | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self._argv(command, run),
                cwd=str(run.cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=_preexec(spec.cpu_seconds, spec.max_file_bytes),
            )
            run.root_pid = process.pid
            assert process.stdout is not None
            reader = asyncio.ensure_future(tail.drain(process.stdout))
            waiter = asyncio.ensure_future(process.wait())
            loop = asyncio.get_running_loop()
            deadline = loop.time() + spec.wall_seconds
            try:
                while not waiter.done():
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        timed_out = True
                        break
                    await asyncio.wait({waiter}, timeout=min(self._poll, remaining))
                    if waiter.done():
                        break
                    members, rss = await asyncio.to_thread(self._sample, run)
                    if len(members) > spec.max_processes:
                        limit = "processes"
                        break
                    if rss > spec.max_rss_bytes:
                        limit = "rss"
                        break
            except asyncio.CancelledError:
                await asyncio.shield(asyncio.to_thread(self._reap, run, root_alive=True))
                raise
            # code review round 1 P1-5: once ``process.wait()`` has returned, the child is
            # reaped and its pid may already belong to someone else — never signal it then
            residual = await asyncio.to_thread(self._reap, run, root_alive=not waiter.done())
            returncode = await waiter
            try:
                await asyncio.wait_for(reader, timeout=2.0)
            except TimeoutError:  # a survivor still holds the pipe: keep what was read
                reader.cancel()
        finally:
            run.close()
        if limit is None and returncode == -signal.SIGXCPU:
            limit = "cpu"
        output, truncated = tail.text()
        return ExecutionReceipt(
            execution_id=run.execution_id,
            kind=self.kind,
            isolated=self.isolated,
            environment_digest=self.environment_digest,
            effective_limits=spec.effective_limits(isolated=self.isolated),
            exit_code=None if timed_out else returncode,
            output=output,
            truncated=truncated,
            timed_out=timed_out,
            limit_exceeded=limit,
            tree_killed=not residual,
            residual_pids=tuple(sorted(residual)),
            status="ok" if not residual else "error",
        )

    def _sample(self, run: _Run) -> tuple[set[int], int]:
        members = self._members(run)
        run.seen |= members
        rss = sum(p.rss_kb for p in run.table if p.pid in members) * 1024
        return members, rss

    def _reap(self, run: _Run, *, root_alive: bool = True) -> set[int]:
        """Kill every process of the run, sweep again until none is left or the sweeps
        run out; what is still alive afterwards is returned (``tree_killed`` = empty).

        ``root_alive`` says whether the run's own process is still running.  After a normal
        exit it is not, and its pid may have been handed to an unrelated process — signalling
        the group then could kill a stranger (code review round 1 P1-5)."""

        for _ in range(self._max_sweeps):
            if root_alive and run.root_pid is not None:
                try:
                    os.killpg(run.root_pid, signal.SIGKILL)  # the run's own group
                except (ProcessLookupError, PermissionError):
                    pass
                _sigkill(run.root_pid)
            root_alive = False  # after one sweep the run's own process is gone either way
            survivors = self._survivors(run)
            if not survivors:
                return set()
            for pid in survivors:
                self._kill(pid)
            time.sleep(0.05)
        return self._survivors(run)


@dataclass
class _Run:
    execution_id: str
    cwd: Path
    scratch: Path
    marks: Path  # canary / decoy: outside every write path of the run
    user: str
    root_pid: int | None = None
    seen: set[int] = field(default_factory=set)
    table: list[_Proc] = field(default_factory=list)

    @property
    def canary(self) -> Path:
        return self.marks / "canary"

    @property
    def decoy(self) -> Path:
        return self.marks / "decoy"

    @classmethod
    def start(cls, exec_root: Path, cwd: Path) -> _Run:
        exec_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        execution_id = uuid.uuid4().hex
        scratch = exec_root / execution_id
        marks = exec_root / f"{execution_id}.marks"
        (scratch / "tmp").mkdir(parents=True, mode=0o700)
        marks.mkdir(mode=0o700)
        (marks / "canary").write_text("canary", encoding="utf-8")
        (marks / "decoy").write_text("decoy", encoding="utf-8")
        return cls(execution_id, cwd, scratch, marks, pwd.getpwuid(os.getuid()).pw_name)

    def close(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)
        shutil.rmtree(self.marks, ignore_errors=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ process only
class ProcessOnlyExecutor(_Executor):
    """No sandbox: a child in its own session with the hard rlimits, reaped along the
    parent chain and by the processes holding its directory.  For trusted code only."""

    kind = "process_only"
    isolated = False

    def __init__(self, interpreter: str | None = None, **options: Any) -> None:
        options.setdefault("poll_interval", 0.1)
        super().__init__(interpreter, **options)

    @property
    def usable(self) -> bool:
        return True

    def _members(self, run: _Run) -> set[int]:
        run.table = _process_table()
        if run.root_pid is None:
            return set()
        alive = {p.pid for p in run.table if not p.zombie}
        return _descendants(run.table, run.root_pid) & alive

    def _survivors(self, run: _Run) -> set[int]:
        run.table = _process_table()
        alive = {p.pid for p in run.table if not p.zombie and p.uid == os.getuid()}
        found = set(run.seen)
        if run.root_pid is not None:
            found |= _descendants(run.table, run.root_pid)
        found |= _holding(run.cwd)
        found.discard(os.getpid())
        return found & alive


def _holding(directory: Path) -> set[int]:
    """Processes of this user whose cwd or an open file lies in ``directory``."""

    if not Path(LSOF).exists():
        return set()
    try:
        out = subprocess.run(
            [LSOF, "-t", "-u", str(os.getuid()), "-a", "+D", str(directory)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(line) for line in out.split() if line.strip().isdigit()}


# ------------------------------------------------------------------ seatbelt
_CHECK: Callable[[int, str], int] | None = None


def _sandbox_check() -> Callable[[int, str], int]:
    """``sandbox_check(pid, "file-read-data", PATH | NO_REPORT, path)``: 0 allowed, 1 denied
    (an undocumented libsystem_sandbox export, used the way WebKit and Chromium do)."""

    global _CHECK
    if _CHECK is None:
        library = ctypes.CDLL("/usr/lib/system/libsystem_sandbox.dylib")
        no_report = ctypes.c_int.in_dll(library, "SANDBOX_CHECK_NO_REPORT").value
        function = library.sandbox_check
        function.restype = ctypes.c_int
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]  # fixed part only
        filter_path = 1  # SANDBOX_FILTER_PATH

        def check(pid: int, path: str) -> int:
            target = ctypes.c_char_p(os.fsencode(path))  # the variadic argument, typed
            return int(function(pid, b"file-read-data", filter_path | no_report, target))

        _CHECK = check
    return _CHECK


def _quote(path: str | Path) -> str:
    return json.dumps(str(path))  # an SBPL string literal


def _interpreter_facts(interpreter: str) -> dict[str, Any]:
    probe = (
        "import json, os, sys; print(json.dumps({'prefix': sys.prefix, 'base': sys.base_prefix,"
        " 'executable': sys.executable, 'path': sys.path, 'version': sys.version}))"
    )
    try:
        done = subprocess.run(
            [interpreter, "-I", "-c", probe],
            cwd="/",
            env={"PATH": DEFAULT_PATH},
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        message = f"cannot inspect the interpreter {interpreter}: {error}"
        raise SandboxUnavailable(message) from error


class SeatbeltExecutor(_Executor):
    kind = "seatbelt"
    isolated = True

    def __init__(
        self,
        interpreter: str,
        *,
        read_paths: Sequence[str],
        python_version: str = "",
        **options: Any,
    ) -> None:
        if sys.platform != "darwin" or not Path(SANDBOX_EXEC).exists():
            raise SandboxUnavailable("the seatbelt sandbox needs macOS sandbox-exec")
        options.setdefault("poll_interval", 0.25)
        super().__init__(interpreter, **options)
        self._read_paths = tuple(dict.fromkeys(read_paths))
        self._python_version = python_version
        self.probe_report: ProbeReport | None = None
        try:
            _sandbox_check()
        except (OSError, AttributeError, ValueError) as error:
            raise SandboxUnavailable(f"sandbox_check is not available: {error}") from error

    @classmethod
    def for_interpreter(
        cls,
        interpreter: str | None = None,
        *,
        extra_read_paths: Sequence[str] = (),
        **options: Any,
    ) -> SeatbeltExecutor:
        """The read whitelist comes from the interpreter the runs use (review round 2
        P2-9): its prefix, base prefix, the directory of its real executable and every
        existing ``sys.path`` entry (``.pth`` paths included) — plus the system paths."""

        chosen = str(interpreter or sys.executable)
        facts = _interpreter_facts(chosen)
        paths: list[str] = list(SYSTEM_READ_PATHS)
        for raw in (
            facts["prefix"],
            facts["base"],
            os.path.dirname(os.path.realpath(facts["executable"])),
            os.path.dirname(os.path.realpath(chosen)),
            *facts["path"],
        ):
            if raw and raw != "/" and os.path.exists(raw):
                paths.append(os.path.realpath(raw))
        paths.extend(os.path.realpath(p) for p in extra_read_paths)
        return cls(chosen, read_paths=paths, python_version=str(facts["version"]), **options)

    @property
    def base_read_paths(self) -> tuple[str, ...]:
        return self._read_paths

    @property
    def usable(self) -> bool:
        return (
            self.probe_report is not None
            and self.probe_report.ok
            and self.probe_report.environment_digest == self.environment_digest
        )

    def _identity(self) -> dict[str, Any]:
        return {
            "interpreter": os.path.realpath(self.interpreter),
            "python": self._python_version,
            "read_paths": list(self._read_paths),
            "profile": _profile("<cwd>", "<scratch>", "<canary>", self._read_paths),
        }

    def _argv(self, command: Sequence[str], run: _Run) -> list[str]:
        profile = _profile(run.cwd, run.scratch, run.canary, self._read_paths)
        return [SANDBOX_EXEC, "-p", profile, *command]

    def _identified(self, run: _Run, *, uid_only: bool = True) -> set[int]:
        check = _sandbox_check()
        run.table = _process_table()
        members = set()
        for proc in run.table:
            if proc.zombie or proc.pid == os.getpid() or (uid_only and proc.uid != os.getuid()):
                continue
            if check(proc.pid, str(run.canary)) == 0 and check(proc.pid, str(run.decoy)) == 1:
                members.add(proc.pid)
        return members

    def _members(self, run: _Run) -> set[int]:
        return self._identified(run)

    def _survivors(self, run: _Run) -> set[int]:
        return self._identified(run)


def _profile(cwd: Any, scratch: Any, canary: Any, read_paths: Sequence[str]) -> str:
    reads = " ".join(f"(subpath {_quote(p)})" for p in read_paths)
    devices = " ".join(f"(literal {_quote(p)})" for p in DEVICE_READ_LITERALS)
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        '(deny file-read-data (subpath "/"))\n'
        f'(allow file-read-data (literal "/") {devices} {reads} '
        f"(subpath {_quote(cwd)}) (subpath {_quote(scratch)}) (literal {_quote(canary)}))\n"
        '(deny file-write* (subpath "/"))\n'
        f"(allow file-write* (subpath {_quote(cwd)}) (subpath {_quote(scratch)}) "
        '(literal "/dev/null"))\n'
        "(deny signal)\n"
        "(allow signal (target same-sandbox))\n"
        "(deny mach-lookup)\n"
        "(deny process-info* (target others))\n"
    )


# ------------------------------------------------------------------ the capability probe
@dataclass(frozen=True, slots=True)
class ProbeReport:
    ok: bool
    items: tuple[dict[str, Any], ...]
    targets: tuple[str, ...]
    disjoint: bool
    environment_digest: str

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "items": [dict(item) for item in self.items],
            "targets": list(self.targets),
            "disjoint": self.disjoint,
            "environment_digest": self.environment_digest,
            "version": SANDBOX_VERSION,
        }


_ACCESS_PROBE = """
import json, os, socket, sys
cfg = json.loads(sys.argv[1])
out = {}
def attempt(name, fn):
    try:
        fn()
        out[name] = "ALLOWED"
    except Exception as error:
        out[name] = type(error).__name__
attempt("read_home", lambda: os.listdir(cfg["home"]))
attempt("read_other_temp", lambda: open(cfg["secret"]).read())
attempt("write_outside", lambda: open(cfg["escape"], "w").write("x"))
attempt("network_tcp", lambda: socket.create_connection(("127.0.0.1", cfg["port"]), timeout=2))
def unix():
    s = socket.socket(socket.AF_UNIX)
    s.connect(cfg["unix"])
attempt("network_unix", unix)
attempt("signal_host", lambda: os.kill(cfg["host_pid"], 0))
open("probe.json", "w").write(json.dumps(out))
"""
_DAEMON_PROBE = """
import os, sys, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        open("daemon.pid", "w").write(str(os.getpid()))
        os.chdir("/")
        os.closerange(0, 256)
        time.sleep(600)
    os._exit(0)
time.sleep(0.5)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _under(path: str, roots: Sequence[str]) -> bool:
    resolved = Path(path).resolve()
    return any(resolved == Path(r) or Path(r) in resolved.parents for r in roots)


async def probe_sandbox(executor: SeatbeltExecutor) -> ProbeReport:
    """Eight behavioural checks (plan v3 D1); any miss makes the adapter unusable.  Every
    target is computed here, apart from the read whitelist, and must lie outside it."""

    home = str(Path.home().resolve())
    with (
        tempfile.TemporaryDirectory(prefix="orch-probe-other-") as raw_other,
        tempfile.TemporaryDirectory(prefix="orch-probe-ws-") as raw_ws,
    ):
        other = Path(raw_other).resolve()
        workdir = Path(raw_ws).resolve()
        secret = other / "secret.txt"
        secret.write_text(PROBE_SECRET, encoding="utf-8")
        escape = other / "escape.txt"
        unix_path = other / "u.sock"
        targets = (home, str(other), str(secret), str(escape), str(unix_path))
        disjoint = not any(_under(t, executor.base_read_paths) for t in targets)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        unix = socket.socket(socket.AF_UNIX)
        unix.bind(str(unix_path))
        unix.listen(1)
        items: list[dict[str, Any]] = []
        try:
            cfg = {
                "home": home,
                "secret": str(secret),
                "escape": str(escape),
                "port": listener.getsockname()[1],
                "unix": str(unix_path),
                "host_pid": os.getpid(),
            }
            script = workdir / "access_probe.py"
            script.write_text(_ACCESS_PROBE, encoding="utf-8")
            receipt = await executor.execute(
                [executor.interpreter, str(script), json.dumps(cfg)],
                cwd=str(workdir),
                spec=SandboxSpec(cpu_seconds=30, wall_seconds=60),
            )
            try:
                seen = json.loads((workdir / "probe.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                seen = {}

            def denied(name: str) -> bool:
                return seen.get(name) == "PermissionError"

            for name, ok in (
                ("read_home", denied("read_home")),
                ("read_other_temp", denied("read_other_temp")),
                ("write_outside", denied("write_outside") and not escape.exists()),
                ("network", denied("network_tcp") and denied("network_unix")),
                ("signal_host", denied("signal_host")),
            ):
                items.append(
                    {
                        "name": name,
                        "ok": bool(ok) and receipt.exit_code == 0,
                        "observed": {k: v for k, v in seen.items() if k.startswith(name[:7])}
                        or seen.get(name),
                    }
                )
            script = workdir / "daemon_probe.py"
            script.write_text(_DAEMON_PROBE, encoding="utf-8")
            receipt = await executor.execute(
                [executor.interpreter, str(script)],
                cwd=str(workdir),
                spec=SandboxSpec(cpu_seconds=30, wall_seconds=30),
            )
            try:
                daemon = int((workdir / "daemon.pid").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                daemon = None
            gone = daemon is not None and not _alive(daemon)
            if daemon is not None and not gone:
                _sigkill(daemon)
            items.append(
                {
                    "name": "daemon_reaped",
                    "ok": gone and receipt.tree_killed,
                    "observed": {"pid": daemon, "tree_killed": receipt.tree_killed},
                }
            )
            receipt = await executor.execute(
                [executor.interpreter, "-c", "import sys; sys.stdout.write('x' * 100000)"],
                cwd=str(workdir),
                spec=SandboxSpec(cpu_seconds=30, wall_seconds=30, max_output_bytes=4096),
            )
            items.append(
                {
                    "name": "output_truncated",
                    "ok": receipt.truncated and len(receipt.output.encode()) <= 4096,
                    "observed": {"truncated": receipt.truncated},
                }
            )
            receipt = await executor.execute(
                [executor.interpreter, "-c", "while True:\n    pass"],
                cwd=str(workdir),
                spec=SandboxSpec(cpu_seconds=1, wall_seconds=30),
            )
            items.append(
                {
                    "name": "cpu_killed",
                    "ok": receipt.limit_exceeded == "cpu" and not receipt.timed_out,
                    "observed": {"limit_exceeded": receipt.limit_exceeded},
                }
            )
        finally:
            listener.close()
            unix.close()
    report = ProbeReport(
        ok=disjoint and all(item["ok"] for item in items),
        items=tuple(items),
        targets=targets,
        disjoint=disjoint,
        environment_digest=executor.environment_digest,
    )
    executor.probe_report = report
    return report


# ------------------------------------------------------------------ resolving a deployment
def resolve_executor(deployment: Any, executor: Any = None) -> SandboxExecutorPort | None:
    """The executor a deployment runs model-written code with (plan v3 D2): ``off`` →
    none; ``process_only`` → the given one or a process executor; ``sandboxed`` → only a
    seatbelt executor whose probe passed on this very environment."""

    mode = getattr(deployment, "code_execution", "process_only")
    if mode == "off":
        return None
    if mode == "sandboxed":
        if not isinstance(executor, SeatbeltExecutor):
            raise SandboxUnavailable(
                "code_execution='sandboxed' needs a SeatbeltExecutor whose probe passed"
            )
        if not executor.usable:
            raise SandboxUnavailable("the seatbelt executor has not passed its probe here")
        return executor
    return executor if executor is not None else ProcessOnlyExecutor()


__all__ = (
    "ExecutionReceipt",
    "ProbeReport",
    "ProcessOnlyExecutor",
    "SANDBOX_VERSION",
    "SandboxExecutorPort",
    "SandboxSpec",
    "SandboxUnavailable",
    "SeatbeltExecutor",
    "probe_sandbox",
    "resolve_executor",
)
