# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Installed-wheel Linux ARM64 gate for the Harness and Memory candidates."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import platform
import sqlite3
import stat
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from simple_harness.contracts import RunId, freeze_json
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.memory_outbox import (
    CommittedTurnSpec,
    MemoryDispatcher,
    MemoryOutboxRepository,
    MemoryOutboxState,
)
from simple_harness.execution.sqlite import (
    SCHEMA_VERSION,
    Database,
    SqliteExecutionUnitOfWork,
)
from simple_harness.execution.uow import RunState
from simple_harness.runtime.agent_memory import (
    AgentIdentity,
    CommittedTurn,
    MemoryScopeRef,
)
from simple_harness.runtime.conversation_memory import ConversationTurnInput
from simple_harness.runtime.start_snapshot import StartSnapshot
from simple_harness.version import __version__

_HARNESS_DISTRIBUTION = "simple-harness-sdk"
_MEMORY_DISTRIBUTION = "simple-harness-memory-sdk"
_MEMORY_VERSION = "0.3.0"


class Arm64CandidateGateError(RuntimeError):
    """Stable, content-free ARM64 candidate admission failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _InjectedRestart(BaseException):
    """Simulate process loss without running normal exception cleanup."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise Arm64CandidateGateError(code)


def _distribution_identity(
    distribution_name: str,
    package_name: str,
    expected_version: str,
) -> dict[str, object]:
    try:
        distribution = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError:
        raise Arm64CandidateGateError("arm64-candidate-distribution-missing") from None
    _require(
        distribution.version == expected_version,
        "arm64-candidate-version-mismatch",
    )
    direct_url = distribution.read_text("direct_url.json")
    if direct_url is not None:
        try:
            direct_url_value = json.loads(direct_url)
        except json.JSONDecodeError:
            raise Arm64CandidateGateError("arm64-candidate-install-provenance-invalid") from None
        _require(
            not bool(direct_url_value.get("dir_info", {}).get("editable")),
            "arm64-candidate-wheel-required",
        )
    module = importlib.import_module(package_name)
    origin_value = getattr(module, "__file__", None)
    _require(isinstance(origin_value, str), "arm64-candidate-module-origin-invalid")
    origin = Path(cast(str, origin_value)).resolve(strict=True)
    distribution_root = Path(str(distribution.locate_file(""))).resolve(strict=True)
    try:
        origin.relative_to(distribution_root)
    except ValueError:
        raise Arm64CandidateGateError("arm64-candidate-wheel-required") from None
    return {
        "distribution": distribution_name,
        "version": distribution.version,
        "installation": "wheel",
    }


def _owner_only(path: Path) -> bool:
    details = path.stat()
    return (
        stat.S_ISREG(details.st_mode)
        and stat.S_IMODE(details.st_mode) == 0o600
        and details.st_uid == os.geteuid()
    )


def _load_memory_authority() -> tuple[type[Any], type[Any]]:
    root = importlib.import_module("simple_harness_memory")
    backends = importlib.import_module("simple_harness_memory.backends")
    adapter = getattr(root, "ConversationMemoryAdapter", None)
    backend = getattr(backends, "SQLiteMemoryBackend", None)
    _require(callable(adapter), "arm64-memory-adapter-missing")
    _require(callable(backend), "arm64-memory-backend-missing")
    return cast(type[Any], backend), cast(type[Any], adapter)


def _execution_snapshot(turn: ConversationTurnInput) -> dict[str, object]:
    snapshot = StartSnapshot(
        profile_key="agent.arm64-gate",
        driver_kind="react",
        turn_id="turn-arm64-gate",
        tool_catalog_generation=1,
        input=freeze_json({"messages": [turn.message.to_dict()]}),
        conversation=turn,
    )
    return cast(dict[str, object], snapshot.to_json())


def _probe_memory_sqlite(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        violations = tuple(connection.execute("PRAGMA foreign_key_check").fetchall())
        schema_version_row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        messages = connection.execute(
            "SELECT source_event_id,user_id,session_id,role,COUNT(*) "
            "FROM messages GROUP BY source_event_id,user_id,session_id,role"
        ).fetchall()
    finally:
        connection.close()
    _require(foreign_keys == 1, "arm64-memory-foreign-keys-disabled")
    _require(journal_mode == "wal", "arm64-memory-wal-disabled")
    _require(integrity == "ok" and not violations, "arm64-memory-integrity-failed")
    _require(schema_version_row is not None, "arm64-memory-schema-missing")
    _require(
        messages
        == [
            (
                "harness-memory/v1/user/run-arm64-gate",
                "user-arm64-gate",
                "session-arm64-gate",
                "user",
                1,
            )
        ],
        "arm64-memory-outbox-deduplication-failed",
    )
    _require(_owner_only(path), "arm64-memory-owner-mode-invalid")
    return {
        "schema_version": int(schema_version_row[0]),
        "journal_mode": journal_mode,
        "foreign_keys": True,
        "integrity": integrity,
        "mode": "0600",
        "message_count": 1,
    }


async def _exercise_runtime(root: Path) -> dict[str, object]:
    execution_path = root / "execution.db"
    memory_path = root / "memory.db"
    backend_type, adapter_type = _load_memory_authority()
    turn = ConversationTurnInput(
        AgentIdentity(
            "deployment-arm64-gate", "household-arm64-gate", "user-arm64-gate", "session-arm64-gate"
        ),
        Message(MessageRole.USER, "remember the ARM64 candidate"),
        "remember the ARM64 candidate",
    )
    committed_turn = CommittedTurnSpec.from_domain(
        CommittedTurn(
            "turn-arm64-gate",
            turn.identity,
            "remember the ARM64 candidate",
            "ARM64 candidate acknowledged",
            MemoryScopeRef.personal(turn.user_id),
            "a" * 64,
            1.0,
        )
    )

    database = Database.open(execution_path, wal=True)
    first_dispatcher: MemoryDispatcher | None = None
    first_backend: Any = None
    try:
        uow = SqliteExecutionUnitOfWork(database)
        run = uow.create_with_start_snapshot(
            execution_session_id=turn.session_id,
            run_id="run-arm64-gate",
            request_id="request-arm64-gate",
            profile_key="agent.arm64-gate",
            driver_kind="react",
            snapshot=cast(Any, _execution_snapshot(turn)),
            event_id="run-arm64-gate:created",
            user_id=turn.user_id,
            now=1.0,
        )
        database.connection.execute(
            "INSERT INTO agent_identity_bindings VALUES(?,?,?,?,?,?)",
            (
                turn.session_id,
                turn.identity.deployment_id,
                turn.identity.household_id,
                turn.identity.actor_id,
                "b" * 64,
                1.0,
            ),
        )
        stored_snapshot = uow.read_start_snapshot(run.run_id)
        _require(run.state is RunState.CREATED, "arm64-minimal-runtime-state-invalid")
        _require(stored_snapshot is not None, "arm64-minimal-runtime-snapshot-missing")
        restored = StartSnapshot.from_json(cast(Any, stored_snapshot))
        _require(restored.conversation == turn, "arm64-minimal-runtime-snapshot-invalid")
        _, runtime_lease = uow.claim_runtime_activation(
            run_id=run.run_id,
            owner_id="arm64-gate-runtime",
            namespace="runtime.kernel",
            now=1.1,
            lease_ttl_seconds=30.0,
        )
        fence = await uow.acquire(RunId(run.run_id), runtime_lease, now=1.1)
        current = uow.read_run(run.run_id)
        assert current is not None
        uow.commit_root_terminal_with_deliveries(
            run_id=run.run_id,
            expected_version=current.version,
            terminal_state=RunState.COMPLETED,
            event_id="run-arm64-gate:terminal",
            terminal_payload={"answer": "ARM64 candidate acknowledged"},
            deliveries=(),
            fence=fence,
            execution_lease=runtime_lease,
            terminal_fence_receipt_ref="receipt://arm64-gate/terminal/1",
            committed_turn=committed_turn,
            now=1.2,
        )
        first_backend = backend_type(str(memory_path))
        await first_backend.initialize()
        first_adapter = adapter_type(first_backend)
        first_repository = MemoryOutboxRepository(database)
        first_dispatcher = MemoryDispatcher(
            first_repository,
            first_adapter,
            owner_id="arm64-gate-before-restart",
            clock=lambda: 1.0,
        )

        def crash_after_record(point: str) -> None:
            if point == "memory_dispatcher.after_record_before_ack":
                raise _InjectedRestart("injected-arm64-gate-restart")

        try:
            await first_dispatcher.run_once(fault=crash_after_record)
        except _InjectedRestart:
            pass
        released = first_repository.read(committed_turn.intent_id)
        if released is None:
            raise Arm64CandidateGateError("arm64-memory-outbox-record-missing")
        _require(
            released.state is MemoryOutboxState.CLAIMED and released.attempt_count == 1,
            "arm64-memory-outbox-restart-state-invalid",
        )
    finally:
        if first_dispatcher is not None:
            await first_dispatcher.close()
        if first_backend is not None:
            await first_backend.close()
        database.close()

    reopened_database = Database.open(execution_path, wal=True)
    second_dispatcher: MemoryDispatcher | None = None
    second_backend: Any = None
    try:
        _require(
            reopened_database.schema_version == SCHEMA_VERSION,
            "arm64-execution-schema-mismatch",
        )
        _require(
            reopened_database.foreign_keys_enabled,
            "arm64-execution-foreign-keys-disabled",
        )
        _require(
            reopened_database.journal_mode.lower() == "wal",
            "arm64-execution-wal-disabled",
        )
        _require(
            reopened_database.integrity_check() == ("ok",)
            and not reopened_database.foreign_key_violations(),
            "arm64-execution-integrity-failed",
        )
        _require(_owner_only(execution_path), "arm64-execution-owner-mode-invalid")
        second_backend = backend_type(str(memory_path))
        await second_backend.initialize()
        second_adapter = adapter_type(second_backend)
        second_repository = MemoryOutboxRepository(reopened_database)
        second_dispatcher = MemoryDispatcher(
            second_repository,
            second_adapter,
            owner_id="arm64-gate-after-restart",
            clock=lambda: 3.0,
        )
        _require(
            await second_dispatcher.run_once(),
            "arm64-memory-outbox-replay-missing",
        )
        settled = second_repository.read(committed_turn.intent_id)
        if settled is None:
            raise Arm64CandidateGateError("arm64-memory-outbox-record-missing")
        _require(
            settled.state is MemoryOutboxState.APPLIED and settled.attempt_count == 2,
            "arm64-memory-outbox-replay-failed",
        )
        outbox = {
            "state": settled.state.value,
            "attempt_count": settled.attempt_count,
            "turn_id": settled.turn_id,
        }
    finally:
        if second_dispatcher is not None:
            await second_dispatcher.close()
        if second_backend is not None:
            await second_backend.close()
        reopened_database.close()

    memory_sqlite = _probe_memory_sqlite(memory_path)
    return {
        "execution": {
            "schema_version": SCHEMA_VERSION,
            "journal_mode": "wal",
            "foreign_keys": True,
            "integrity": "ok",
            "mode": "0600",
        },
        "memory": memory_sqlite,
        "outbox": outbox,
    }


async def _run_core_gate() -> dict[str, object]:
    machine = platform.machine().lower()
    _require(platform.system() == "Linux", "arm64-linux-required")
    _require(machine in {"aarch64", "arm64"}, "arm64-architecture-required")
    harness_identity = _distribution_identity(
        _HARNESS_DISTRIBUTION,
        "simple_harness",
        __version__,
    )
    memory_identity = _distribution_identity(
        _MEMORY_DISTRIBUTION,
        "simple_harness_memory",
        _MEMORY_VERSION,
    )
    with TemporaryDirectory(prefix="simple-harness-arm64-gate-") as raw:
        root = Path(raw)
        root.chmod(0o700)
        observations = await _exercise_runtime(root)
    return {
        "schema_version": "simple-harness.arm64-core-gate.v1",
        "status": "PASS",
        "minimal_runtime": True,
        "memory_outbox_restart": True,
        "sqlite_reopen": True,
        "identity": {
            "platform": "linux",
            "architecture": machine,
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "distributions": {
                _HARNESS_DISTRIBUTION: harness_identity,
                _MEMORY_DISTRIBUTION: memory_identity,
            },
        },
        "sqlite": {
            "execution": observations["execution"],
            "memory": observations["memory"],
        },
        "outbox": observations["outbox"],
    }


def run_core_gate() -> dict[str, object]:
    """Run the exact installed-wheel Linux ARM64 core admission gate."""

    try:
        return asyncio.run(_run_core_gate())
    except Arm64CandidateGateError:
        raise
    except Exception:
        raise Arm64CandidateGateError("arm64-core-gate-failed") from None


def main() -> int:
    try:
        result = run_core_gate()
    except Arm64CandidateGateError as error:
        print(error.code)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ("Arm64CandidateGateError", "run_core_gate")
