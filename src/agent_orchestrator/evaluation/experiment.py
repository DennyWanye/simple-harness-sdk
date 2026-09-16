# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frozen matrix bookkeeping, not implementations of the S/R/D/F algorithms.

Executors own budget admission, physical workers, and official scoring. They must
report cumulative settled counters on the runner's event loop, including during
cancellation cleanup. Deadlines are cooperative: cancellation must stop physical
work before the executor exits. A process crash leaves an interrupted identity,
never an automatically retried episode.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ARMS = ("S", "R", "D", "F")
HIERARCHICAL_ARM = "H"
# G5: the hierarchical arm is declared after the frozen four so that a manifest that
# names only S/R/D/F keeps the bytes (and therefore the fingerprint) it always had.
ARM_NAMES = ARMS + (HIERARCHICAL_ARM,)
DECLARABLE_ARMS = (ARMS, ARM_NAMES)
_TERMINAL = {"success", "failure", "deadline", "interrupted"}


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )


@dataclass(frozen=True, slots=True)
class ExperimentBudget:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    calls: int
    seconds: float

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "total_tokens", "calls"):
            _integer(getattr(self, name), name, 1)
        if (
            isinstance(self.seconds, bool)
            or not isinstance(self.seconds, (int, float))
            or not math.isfinite(self.seconds)
            or self.seconds <= 0
        ):
            raise ValueError("seconds must be positive and finite")
        object.__setattr__(self, "seconds", float(self.seconds))


@dataclass(frozen=True, slots=True)
class ArmSpec:
    arm: str
    executor_id: str  # Host-declared implementation version or content hash.

    def __post_init__(self) -> None:
        if self.arm not in ARM_NAMES:
            raise ValueError("arm must be S, R, D, F, or H")
        _text(self.executor_id, "executor_id")


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    experiment_id: str
    provider: str
    model: str
    budget: ExperimentBudget
    task_ids: tuple[str, ...]
    repetitions: int
    seed: int
    physical_slots: int
    arms: tuple[ArmSpec, ...]
    schedule_version: str = "latin-square-v1"

    def __post_init__(self) -> None:
        for name in ("experiment_id", "provider", "model"):
            _text(getattr(self, name), name)
        if not isinstance(self.budget, ExperimentBudget):
            raise TypeError("budget must be ExperimentBudget")
        if isinstance(self.task_ids, str):
            raise TypeError("task_ids must be a sequence of task identifiers")
        object.__setattr__(self, "task_ids", tuple(self.task_ids))
        object.__setattr__(self, "arms", tuple(self.arms))
        for task_id in self.task_ids:
            _text(task_id, "task_id")
        if not self.task_ids or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("task_ids must be nonempty and unique")
        if not all(isinstance(arm, ArmSpec) for arm in self.arms):
            raise TypeError("arms must contain ArmSpec values")
        if tuple(arm.arm for arm in self.arms) not in DECLARABLE_ARMS:
            raise ValueError("declare each arm exactly once, in S/R/D/F[/H] order")
        _integer(self.repetitions, "repetitions", 1)
        _integer(self.seed, "seed")
        _integer(self.physical_slots, "physical_slots", 1)
        if self.schedule_version != "latin-square-v1":
            raise ValueError("unsupported experiment schedule")

    def to_dict(self) -> dict[str, Any]:
        return json.loads(_json(asdict(self)))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_json(self.to_dict()).encode()).hexdigest()

    def runs(self) -> tuple[RunSpec, ...]:
        fingerprint = self.fingerprint
        runs = []
        for task_index, task_id in enumerate(self.task_ids):
            for repetition in range(self.repetitions):
                offset = (task_index + repetition) % len(self.arms)
                order = self.arms[offset:] + self.arms[:offset]
                for arm in order:
                    identity = [fingerprint, task_id, repetition, arm.arm]
                    run_id = hashlib.sha256(_json(identity).encode()).hexdigest()
                    runs.append(
                        RunSpec(run_id, arm.arm, task_id, repetition, self.seed + repetition)
                    )
        return tuple(runs)


@dataclass(frozen=True, slots=True)
class RunSpec:
    run_id: str
    arm: str
    task_id: str
    repetition: int
    seed: int


@dataclass(frozen=True, slots=True)
class ExecutionCounters:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    calls: int
    peak_physical_slots: int

    def __post_init__(self) -> None:
        _text(self.provider, "provider")
        _text(self.model, "model")
        names = ("input_tokens", "output_tokens", "total_tokens", "calls", "peak_physical_slots")
        for name in names:
            _integer(getattr(self, name), name)
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    success: bool  # Executor-reported outcome, not an official benchmark score.
    counters: ExecutionCounters

    def __post_init__(self) -> None:
        if type(self.success) is not bool or not isinstance(self.counters, ExecutionCounters):
            raise TypeError("ExecutionResult requires bool success and ExecutionCounters")


@dataclass(frozen=True, slots=True)
class RunContext:
    manifest: ExperimentManifest
    run: RunSpec
    report_usage: Callable[[ExecutionCounters], None]


@dataclass(frozen=True, slots=True)
class ArmExecutor:
    arm: str
    executor_id: str
    provider: str
    model: str
    execute: Callable[[RunContext], Awaitable[ExecutionResult]]


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    # OS locks release on crashes; unlike lockfile creation, they need no stale-lock retry.
    with path.open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            stream.write(b"\0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if sys.platform == "win32":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write(path: Path, document: dict[str, Any]) -> None:
    payload = _json(document) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load(path: Path, manifest: ExperimentManifest) -> dict[str, Any]:
    specs = [asdict(run) for run in manifest.runs()]
    if not path.exists():
        document = {
            "schema_version": 1,
            "manifest": manifest.to_dict(),
            "manifest_sha256": manifest.fingerprint,
            "runs": [{"run": spec, "status": "pending", "counters": None} for spec in specs],
        }
        _write(path, document)
        return document
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or _json(document.get("manifest")) != _json(manifest.to_dict())
        or document.get("manifest_sha256") != manifest.fingerprint
    ):
        raise ValueError("experiment configuration mismatch")
    records = document.get("runs")
    if (
        not isinstance(records, list)
        or len(records) != len(specs)
        or any(
            not isinstance(record, dict)
            or _json(record.get("run")) != _json(spec)
            or record.get("status") not in _TERMINAL | {"pending", "running"}
            for record, spec in zip(records, specs, strict=True)
        )
    ):
        raise ValueError("experiment run identities or states mismatch")
    _json(document)  # Refuse non-finite numbers in a damaged receipt.
    for record in records:
        if record["status"] == "running":
            record.update(status="interrupted", finished_at=_now(), error="previous_runner_stopped")
    _write(path, document)
    return document


async def _execute_one(
    manifest: ExperimentManifest,
    spec: RunSpec,
    executor: ArmExecutor,
    record: dict[str, Any],
    persist: Callable[[], None],
) -> None:
    active = True
    violation: str | None = None

    def report(counters: ExecutionCounters) -> None:
        nonlocal violation
        if not active:
            raise RuntimeError("episode has ended")
        if not isinstance(counters, ExecutionCounters):
            raise TypeError("executor must report ExecutionCounters")
        previous = record["counters"]
        current = asdict(counters)
        record["counters"] = current
        if (counters.provider, counters.model) != (manifest.provider, manifest.model):
            violation = "provider_or_model_substitution"
        names = ("input_tokens", "output_tokens", "total_tokens", "calls", "peak_physical_slots")
        if previous and any(current[name] < previous[name] for name in names):
            violation = violation or "counters_decreased"
        if any(current[name] > getattr(manifest.budget, name) for name in names[:-1]):
            violation = violation or "budget_exceeded"
        if counters.peak_physical_slots > manifest.physical_slots:
            violation = violation or "physical_slots_exceeded"
        if violation:
            record["error"] = violation
            record.setdefault("invalid_counters", current)
        persist()
        if violation:
            raise ValueError(violation)

    record.update(status="running", started_at=_now())
    persist()  # Admission is durable before any executor side effect.
    started = time.monotonic()
    deadline = asyncio.timeout(manifest.budget.seconds)
    try:
        async with deadline:
            result = await executor.execute(RunContext(manifest, spec, report))
            if not isinstance(result, ExecutionResult):
                raise TypeError("executor must return ExecutionResult")
            report(result.counters)
            record["status"] = "success" if result.success else "failure"
            if not result.success:
                record["error"] = "executor_reported_failure"
    except asyncio.CancelledError:
        record.update(status="interrupted", error=violation or "runner_cancelled")
        raise
    except Exception as error:
        record.update(status="failure", error=violation or type(error).__name__)
    finally:
        active = False
        elapsed = time.monotonic() - started
        if record["status"] != "interrupted" and (
            deadline.expired() or elapsed >= manifest.budget.seconds
        ):
            record.update(status="deadline", error=violation or "deadline_exceeded")
        record.update(finished_at=_now(), elapsed_seconds=elapsed)
        persist()


async def run_experiment(
    manifest: ExperimentManifest,
    *,
    executors: Mapping[str, ArmExecutor],
    evidence_root: str | Path,
) -> dict[str, Any]:
    """Run serially; resume only pending identities in <evidence_root>/experiment.json.

    Names, implementation IDs, provider and model must match the declaration.
    IDs are caller attestations; this function cannot inspect an executor's
    algorithm or provider wire. Terminal failures/deadlines are never replaced.
    """
    bound = dict(executors)
    declared = tuple(arm.arm for arm in manifest.arms)
    if set(bound) != set(declared):
        raise ValueError(f"explicit executors for exactly {'/'.join(declared)} are required")
    for arm in manifest.arms:
        executor = bound[arm.arm]
        if not isinstance(executor, ArmExecutor) or (
            executor.arm,
            executor.executor_id,
            executor.provider,
            executor.model,
        ) != (arm.arm, arm.executor_id, manifest.provider, manifest.model):
            raise ValueError(f"executor binding mismatch for {arm.arm}")
        if not callable(executor.execute):
            raise TypeError(f"executor {arm.arm} is not callable")
    root = Path(evidence_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "experiment.json"
    with _exclusive(root / "experiment.lock"):
        document = _load(path, manifest)
        for spec, record in zip(manifest.runs(), document["runs"], strict=True):
            if record["status"] == "pending":
                await _execute_one(
                    manifest, spec, bound[spec.arm], record, lambda: _write(path, document)
                )
        return json.loads(_json(document))


__all__ = (
    "ARMS",
    "ARM_NAMES",
    "DECLARABLE_ARMS",
    "HIERARCHICAL_ARM",
    "ArmExecutor",
    "ArmSpec",
    "ExecutionCounters",
    "ExecutionResult",
    "ExperimentBudget",
    "ExperimentManifest",
    "RunContext",
    "RunSpec",
    "run_experiment",
)
