# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest

from simple_harness import (
    AdmissionVerdict,
    AgentIdentity,
    AllowAllAdmission,
    CancelCommandIntent,
    CommittedTurnReceipt,
    CommittedTurnStatus,
    ContinueCommandIntent,
    ConversationContextResult,
    HostControlAuthorityV1,
    HostControlRunStartV1,
    MemoryRecallResult,
    MemoryRecallStatus,
    Message,
    MessageRole,
    StartCommandIntent,
    canonical_json,
)
from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.execution.command_ingress import CommandError
from simple_harness.execution.context_authority import ToolCatalogSnapshot
from simple_harness.execution.context_staging import ContextStagingRepository
from simple_harness.execution.delivery import (
    DeliveryDispatcher,
    DeliverySpec,
    DeliveryState,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.providers import ProviderToolSpec
from simple_harness.runtime import (
    CommandOutputState,
    CommandState,
    ConversationContinuationInput,
    ConversationTurnInput,
    ConversationTurnOutput,
    DriverResult,
    RunStart,
    RuntimeLifecycleState,
    RuntimePorts,
    RuntimeProfile,
    SqliteContextPort,
    build_runtime,
)
from simple_harness.runtime.conversation_memory import ContextPreparationMode
from simple_harness.runtime.start_snapshot import bind_start_snapshot


class NoopPort:
    async def reconcile(self) -> None:
        return None


class Catalog:
    def __init__(self, generation: int = 1, *, fingerprint: str | None = None) -> None:
        self.generation = generation
        self.fingerprint = fingerprint

    def current_generation(self) -> int:
        return self.generation

    def resolve(self, generation: int, content_fingerprint: str) -> ToolCatalogSnapshot | None:
        if generation != self.generation or content_fingerprint != self.fingerprint:
            return None
        return ToolCatalogSnapshot(
            generation,
            content_fingerprint,
            (ProviderToolSpec("read_status", "Read status", {"type": "object"}),),
            0.0,
        )


class Driver:
    def __init__(self, *, state=RunState.COMPLETED) -> None:
        self.state = state
        self.calls = 0

    async def start(self, invocation, *, context, cancel):
        del invocation, context, cancel
        self.calls += 1
        return DriverResult(self.state, {"answer": "ok"})


class ModeRouterDriver:
    def __init__(self) -> None:
        self.modes: list[str] = []

    async def start(self, invocation, *, context, cancel):  # type: ignore[no-untyped-def]
        del context, cancel
        self.modes.append(invocation.start.start_mode)
        output = (
            None
            if invocation.start.start_mode == "host_control"
            else ConversationTurnOutput(
                Message(MessageRole.ASSISTANT, "ordinary response"), "ordinary response"
            )
        )
        return DriverResult(
            RunState.COMPLETED,
            {"route": invocation.start.start_mode},
            conversation_output=output,
        )


class _ProviderCrashCut(BaseException):
    pass


class StableContextProvider:
    def __init__(self, *, crash_first: bool = False) -> None:
        self.crash_first = crash_first
        self.requests = []

    async def prepare_once(self, request):
        self.requests.append(request)
        if self.crash_first and len(self.requests) == 1:
            raise _ProviderCrashCut("before provider result")
        payload = {
            "schema_version": 1,
            "source_snapshot_ref": request.source_snapshot_ref,
            "messages": [request.current_message.to_dict()],
            "current_message": request.current_message.to_dict(),
        }
        return ConversationContextResult(
            request.preparation_id,
            request.source_snapshot_ref,
            payload,
            1,
            len(canonical_json(payload).encode()),
        )


class StableMemory:
    def __init__(self, *, crash_first: bool = False) -> None:
        self.crash_first = crash_first
        self.recalls = []
        self.releases = []
        self.committed = []

    async def recall_for_turn(self, request):
        self.recalls.append(request)
        if self.crash_first and len(self.recalls) == 1:
            raise _ProviderCrashCut("after provider result")
        return MemoryRecallResult(
            request.query_id,
            request.query_hash,
            f"result-{request.turn_id}",
            {},
            MemoryRecallStatus.READY,
            0,
            2,
            f"fence-{request.turn_id}",
        )

    async def release_recall(self, request):
        self.releases.append(request)

    async def record_committed_turn(self, request):
        self.committed.append(request)
        return CommittedTurnReceipt(
            request.turn_id,
            request.payload_hash,
            CommittedTurnStatus.APPLIED,
            f"receipt-{request.turn_id}",
        )


def request(name: str = "one", *, generation: int = 1) -> RunStart:
    return RunStart(
        ExecutionSessionId("session-1"),
        RunId(f"run-{name}"),
        RequestId(f"request-{name}"),
        f"turn-{name}",
        {"prompt": name},
        generation,
    )


def host_control_request(name: str = "one") -> HostControlRunStartV1:
    return HostControlRunStartV1(
        ExecutionSessionId("session-host"),
        RunId(f"run-host-{name}"),
        RequestId(f"request-host-{name}"),
        f"turn-host-{name}",
        {"attempt_ref": name},
        1,
        HostControlAuthorityV1(
            "skill.install.verify", f"attempt:{name}", hashlib.sha256(name.encode()).hexdigest(), 1
        ),
        "host-user",
    )


def test_host_control_is_memory_neutral_and_exactly_replayable(tmp_path) -> None:
    async def case() -> None:
        memory = StableMemory()
        driver = Driver()
        value, uow, database = runtime(
            tmp_path,
            driver=driver,
            agent_memory=memory,
            context_provider=StableContextProvider(),
        )
        try:
            await value.start()
            start = host_control_request()
            first = await value.client.start_host_control(start)
            await value.wait_idle(start.run_id)
            replay = await value.client.start_host_control(start)
            assert first.run_id == replay.run_id
            assert driver.calls == 1
            assert memory.recalls == memory.releases == memory.committed == []
            snapshot = uow.read_start_snapshot(start.run_id.value)
            assert snapshot is not None
            assert snapshot["schema_version"] == 6
            assert snapshot["start_mode"] == "host_control"
            assert snapshot["host_control_authority"] == start.authority.to_json()
            assert (
                database.connection.execute(
                    "SELECT COUNT(*) FROM provider_invocations WHERE run_id=?",
                    (start.run_id.value,),
                ).fetchone()[0]
                == 0
            )
            assert (
                database.connection.execute(
                    "SELECT COUNT(*) FROM execution_effects WHERE run_id=?", (start.run_id.value,)
                ).fetchone()[0]
                == 0
            )
            for table, column in (
                ("context_preparation_staging", "identity_key"),
                ("memory_outbox", "run_id"),
                ("conversation_outputs", "run_id"),
                ("workflow_checkpoints", "run_id"),
                ("continuations", "run_id"),
            ):
                assert (
                    database.connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
                        (start.run_id.value,),
                    ).fetchone()[0]
                    == 0
                )
            with pytest.raises(CommandError):
                value.client.signal(start.run_id, signal_id="public", payload={})
            with pytest.raises(CommandError):
                await value.client.cancel(start.run_id)
        finally:
            await value.close()
            database.close()

    asyncio.run(case())


@pytest.mark.parametrize("memory_enabled", (False, True))
def test_public_start_rejects_typed_host_control_mode_before_reservation(
    tmp_path, memory_enabled: bool
) -> None:
    async def case() -> None:
        value, _, database = runtime(
            tmp_path,
            agent_memory=StableMemory() if memory_enabled else None,
            context_provider=StableContextProvider() if memory_enabled else None,
        )
        try:
            await value.start()
            with pytest.raises(Exception, match="Host control Runs require start_host_control"):
                await value.client.start(host_control_request("direct").to_run_start())
        finally:
            await value.close()
            database.close()

    asyncio.run(case())


def test_host_control_session_owner_is_exact(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        try:
            await value.start()
            first = host_control_request("owner-one")
            await value.client.start_host_control(first)
            await value.wait_idle(first.run_id)
            same_owner = HostControlRunStartV1(
                first.execution_session_id,
                RunId("run-host-owner-two"),
                RequestId("request-host-owner-two"),
                "turn-host-owner-two",
                {"attempt_ref": "owner-two"},
                1,
                HostControlAuthorityV1("skill.install.verify", "attempt:owner-two", "b" * 64, 2),
                first.user_id,
            )
            await value.client.start_host_control(same_owner)
            await value.wait_idle(same_owner.run_id)
            wrong_owner = HostControlRunStartV1(
                first.execution_session_id,
                RunId("run-host-owner-wrong"),
                RequestId("request-host-owner-wrong"),
                "turn-host-owner-wrong",
                {"attempt_ref": "owner-wrong"},
                1,
                HostControlAuthorityV1("skill.install.verify", "attempt:owner-wrong", "c" * 64, 3),
                "another-user",
            )
            with pytest.raises(Exception, match="execution session belongs to another user"):
                await value.client.start_host_control(wrong_owner)
        finally:
            await value.close()
            database.close()

    asyncio.run(case())


def test_host_control_bidirectional_collision_and_changed_replay_fail_closed(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        try:
            await value.start()
            control = host_control_request("collision")
            await value.client.start_host_control(control)
            await value.wait_idle(control.run_id)
            with pytest.raises(CommandError):
                await value.client.start(
                    RunStart(
                        control.execution_session_id,
                        control.run_id,
                        control.request_id,
                        control.turn_id,
                        {"attempt_ref": "collision"},
                        1,
                    )
                )
            changed = HostControlRunStartV1(
                control.execution_session_id,
                control.run_id,
                control.request_id,
                control.turn_id,
                {"attempt_ref": "changed"},
                1,
                control.authority,
                control.user_id,
            )
            with pytest.raises(CommandError):
                await value.client.start_host_control(changed)

            ordinary = request("ordinary-collision")
            await value.client.start(ordinary)
            await value.wait_idle(ordinary.run_id)
            opposite = HostControlRunStartV1(
                ordinary.execution_session_id,
                ordinary.run_id,
                ordinary.request_id,
                ordinary.turn_id,
                {"prompt": "ordinary-collision"},
                1,
                control.authority,
                "host-user",
            )
            with pytest.raises(CommandError):
                await value.client.start_host_control(opposite)
        finally:
            await value.close()
            database.close()

    asyncio.run(case())


def test_host_control_recovery_uses_v6_authority_without_memory_side_state(tmp_path) -> None:
    control = host_control_request("recover")
    seeded, uow, database = runtime(tmp_path, agent_memory=StableMemory())
    del seeded
    intent_hash = hashlib.sha256(canonical_json(control.to_json()).encode()).hexdigest()
    uow.reserve_host_control_run_mode(run_id=control.run_id.value, intent_hash=intent_hash, now=1.0)
    snapshot = bind_start_snapshot(
        control.to_run_start(), profile_key="agent.general", driver_kind="react"
    )
    uow.create_with_start_snapshot(
        execution_session_id=control.execution_session_id.value,
        run_id=control.run_id.value,
        request_id=control.request_id.value,
        profile_key="agent.general",
        driver_kind="react",
        snapshot=snapshot.to_json(),
        event_id=f"{control.run_id.value}:created",
        now=1.0,
        user_id=control.user_id,
    )
    database.close()

    async def case() -> None:
        memory = StableMemory()
        driver = ModeRouterDriver()
        restarted, reopened_uow, reopened = runtime(
            tmp_path,
            driver=driver,
            owner="recovery-owner",
            agent_memory=memory,
            context_provider=StableContextProvider(),
        )
        try:
            await restarted.start()
            await restarted.wait_idle(control.run_id)
            assert reopened_uow.read_run(control.run_id.value).state is RunState.COMPLETED
            assert memory.recalls == memory.releases == memory.committed == []
            conversation = ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "adjacent-session"),
                Message(MessageRole.USER, "ordinary request"),
                "ordinary request",
            )
            ordinary_run = RunId("run-adjacent-react")
            await restarted.client.start_conversation(
                conversation,
                run_id=ordinary_run,
                request_id=RequestId("request-adjacent-react"),
                turn_id="turn-adjacent-react",
            )
            await restarted.wait_idle(ordinary_run)
            assert driver.modes == ["host_control", "ordinary"]
            assert len(memory.recalls) == len(memory.releases) == 1
        finally:
            await restarted.close()
            reopened.close()

    asyncio.run(case())


def runtime(
    tmp_path,
    *,
    driver=None,
    catalog=None,
    owner="owner-1",
    clock=lambda: 10.0,
    sleep=asyncio.sleep,
    close_timeout_seconds=5.0,
    delivery_sink=None,
    admission=None,
    lease_ttl_seconds=30.0,
    agent_memory=None,
    context_provider=None,
):
    database = Database.open(tmp_path / "runtime.db")
    uow = SqliteExecutionUnitOfWork(database)
    noop = NoopPort()
    value = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "react")},
        {"react": driver or Driver()},
        RuntimePorts(
            provider=noop,
            tools=noop,
            authorization=noop,
            context=SqliteContextPort(database, clock=clock),
            delivery=(
                noop
                if delivery_sink is None
                else DeliveryDispatcher(uow, {"fixture": delivery_sink}, clock=clock)
            ),
            tool_reconciliation=noop,
            reconciliation=noop,
            provider_reconciliation=noop,
            react_checkpoint=uow,
            tool_catalog=catalog or Catalog(),
            owner_id=owner,
            clock=clock,
            sleep=sleep,
            admission=admission or AllowAllAdmission(),
            lease_ttl_seconds=lease_ttl_seconds,
            close_timeout_seconds=close_timeout_seconds,
            conversation_memory_enabled=agent_memory is not None,
            context_staging=(None if agent_memory is None else ContextStagingRepository(database)),
            context_preparation_mode=(
                None if agent_memory is None else ContextPreparationMode.SDK_PREPARED
            ),
            agent_memory=agent_memory,
            context_provider=context_provider,
        ),
    )
    return value, uow, database


async def seed_terminal_deliveries(uow: SqliteExecutionUnitOfWork, *, count: int) -> None:
    uow.create_with_start_snapshot(
        execution_session_id="session-delivery",
        run_id="run-delivery",
        request_id="request-delivery",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={
            "schema_version": 1,
            "profile_key": "agent.general",
            "driver_kind": "react",
            "turn_id": "turn-delivery",
            "tool_catalog_generation": 1,
            "input": {},
        },
        event_id="run-delivery:created",
        now=1.0,
    )
    run, lease = uow.claim_runtime_activation(
        run_id="run-delivery",
        owner_id="delivery-seeder",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=30.0,
    )
    fence = await uow.acquire(RunId("run-delivery"), lease, now=2.0)
    uow.commit_root_terminal_with_deliveries(
        run_id=run.run_id,
        expected_version=run.version,
        terminal_state=RunState.COMPLETED,
        event_id="run-delivery:completed",
        terminal_payload={"answer": "done"},
        deliveries=tuple(
            DeliverySpec(
                f"delivery-{index:03d}",
                "fixture",
                f"delivery-key-{index:03d}",
                {"index": index},
            )
            for index in range(count)
        ),
        fence=fence,
        execution_lease=lease,
        terminal_fence_receipt_ref="runtime-fence:delivery-seeder:1",
        now=3.0,
    )
    uow.release_runtime_lease(lease, now=3.0)


def test_start_is_atomic_activated_and_idempotent(tmp_path) -> None:
    async def case() -> None:
        driver = Driver(state=RunState.WAITING)
        value, _, database = runtime(tmp_path, driver=driver)
        await value.start()
        first = await value.client.start(request())
        second = await value.client.start(request())
        assert first.run_id == second.run_id == "run-one"
        assert first.state is RunState.RUNNING
        await value.wait_idle(RunId("run-one"))
        assert value.client.query(RunId("run-one")).state is RunState.WAITING
        assert driver.calls == 1
        await value.close()
        database.close()

    asyncio.run(case())


def test_durable_command_accepts_before_driver_and_projects_closed_output(tmp_path) -> None:
    class CommandDriver:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            self.entered.set()
            await self.release.wait()
            return DriverResult(
                RunState.COMPLETED,
                {"private": "not-the-public-output"},
                conversation_output=ConversationTurnOutput(
                    Message(MessageRole.ASSISTANT, "closed answer"), "closed answer"
                ),
            )

    async def case() -> None:
        driver = CommandDriver()
        value, _, database = runtime(tmp_path, driver=driver)
        await value.start()
        intent = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "command-1",
            RunId("run-command"),
            RequestId("request-command"),
            "turn-command",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-command"),
                Message(MessageRole.USER, "hello"),
                "hello",
            ),
        )
        accepted = await value.client.submit_start(intent)
        assert accepted.state is CommandState.ACCEPTED
        await asyncio.wait_for(driver.entered.wait(), timeout=1)
        in_flight = await value.client.get_command(intent.command_id)
        assert in_flight.output_state is CommandOutputState.PENDING
        driver.release.set()
        await value.wait_idle(intent.run_id)
        for _ in range(100):
            snapshot = await value.client.get_command(intent.command_id)
            if snapshot.output_state is CommandOutputState.PRESENT:
                break
            await asyncio.sleep(0.01)
        assert snapshot.receipt.state is CommandState.APPLIED
        assert snapshot.output is not None
        assert snapshot.output.memory_text == "closed answer"
        assert not hasattr(snapshot, "private")
        await value.close()
        database.close()

        reopened = Database.open(tmp_path / "runtime.db")
        try:
            persisted = SqliteExecutionUnitOfWork(reopened).get_command_snapshot(intent.command_id)
            assert persisted.output == snapshot.output
        finally:
            reopened.close()

    asyncio.run(case())


def test_command_catalog_fingerprint_is_durable_across_reopen(tmp_path) -> None:
    async def case() -> None:
        fingerprint = "a" * 64
        value, uow, database = runtime(tmp_path, catalog=Catalog(fingerprint=fingerprint))
        await value.start()
        intent = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "command-catalog-reopen",
            RunId("run-catalog-reopen"),
            RequestId("request-catalog-reopen"),
            "turn-catalog-reopen",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-catalog"),
                Message(MessageRole.USER, "hello"),
                "hello",
            ),
            tool_catalog_fingerprint=fingerprint,
        )
        await value.client.submit_start(intent)
        for _ in range(100):
            command = await value.client.get_command(intent.command_id)
            if command.receipt.state.terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("command did not settle")
        await value.wait_idle(intent.run_id)
        start = uow.read_start_snapshot(intent.run_id.value)
        assert start is not None
        assert start["tool_catalog_generation"] == 1
        assert start["tool_catalog_fingerprint"] == fingerprint
        await value.close()
        database.close()

        restarted, restarted_uow, reopened = runtime(
            tmp_path, catalog=Catalog(fingerprint=fingerprint), owner="owner-restarted"
        )
        try:
            await restarted.start()
            persisted = restarted_uow.read_start_snapshot(intent.run_id.value)
            assert persisted is not None
            assert persisted["tool_catalog_fingerprint"] == fingerprint
            command = await restarted.client.get_command(intent.command_id)
            assert command.receipt.state is CommandState.APPLIED
        finally:
            await restarted.close()
            reopened.close()

    asyncio.run(case())


def test_command_nested_capability_snapshot_reaches_catalog_bound_run(tmp_path) -> None:
    async def case() -> None:
        fingerprint = "b" * 64
        value, uow, database = runtime(
            tmp_path,
            catalog=Catalog(fingerprint=fingerprint),
            agent_memory=StableMemory(),
            context_provider=StableContextProvider(),
        )
        await value.start()
        conversation = ConversationTurnInput(
            AgentIdentity("deployment", "household", "actor", "session-tools"),
            Message(MessageRole.USER, "use the readonly tool"),
            "use the readonly tool",
        )
        intent = StartCommandIntent(
            "deployment/phone",
            "key-tools",
            "command-tools",
            RunId("run-tools"),
            RequestId("request-tools"),
            "turn-tools",
            conversation,
            input={
                "messages": [conversation.message.to_dict()],
                "capability_snapshot": {"tools": ["read_status"]},
                "max_output_tokens": 4096,
            },
            tool_catalog_generation=1,
            tool_catalog_fingerprint=fingerprint,
        )

        await value.client.submit_start(intent)
        for _ in range(100):
            command = await value.client.get_command(intent.command_id)
            if command.receipt.state.terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("command did not settle")

        assert command.receipt.state is CommandState.APPLIED
        start = uow.read_start_snapshot(intent.run_id.value)
        assert start is not None
        assert start["tool_catalog_generation"] == 1
        assert start["tool_catalog_fingerprint"] == fingerprint
        assert start["input"]["capability_snapshot"] == {"tools": ["read_status"]}
        assert uow.read_run(intent.run_id.value) is not None
        await value.close()
        database.close()

    asyncio.run(case())


def test_command_catalog_fingerprint_drift_fails_before_driver(tmp_path) -> None:
    async def case() -> None:
        driver = Driver()
        value, _, database = runtime(tmp_path, driver=driver, catalog=Catalog(fingerprint="a" * 64))
        await value.start()
        intent = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "command-catalog-drift",
            RunId("run-catalog-drift"),
            RequestId("request-catalog-drift"),
            "turn-catalog-drift",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-catalog"),
                Message(MessageRole.USER, "hello"),
                "hello",
            ),
            tool_catalog_fingerprint="b" * 64,
        )
        await value.client.submit_start(intent)
        for _ in range(100):
            command = await value.client.get_command(intent.command_id)
            if command.receipt.state.terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("command did not settle")
        await value.wait_idle(intent.run_id)
        run = value.client.query(intent.run_id)
        assert run is not None and run.state is RunState.FAILED
        assert driver.calls == 0
        await value.close()
        database.close()

    asyncio.run(case())


def test_completed_command_missing_or_corrupt_output_is_unknown(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        await value.start()
        intent = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "command-missing-output",
            RunId("run-missing-output"),
            RequestId("request-missing-output"),
            "turn-missing-output",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-missing"),
                Message(MessageRole.USER, "hello"),
                "hello",
            ),
        )
        await value.client.submit_start(intent)
        for _ in range(100):
            missing = await value.client.get_command(intent.command_id)
            if missing.receipt.state.terminal:
                break
            await asyncio.sleep(0.01)
        assert missing.receipt.state is CommandState.APPLIED
        assert missing.output_state is CommandOutputState.UNKNOWN
        assert missing.output is None
        await value.close()
        database.close()

        class OutputDriver:
            async def start(self, invocation, *, context, cancel):
                del invocation, context, cancel
                return DriverResult(
                    RunState.COMPLETED,
                    {},
                    conversation_output=ConversationTurnOutput(
                        Message(MessageRole.ASSISTANT, "answer"), "answer"
                    ),
                )

        corrupt_root = tmp_path / "corrupt"
        corrupt_root.mkdir()
        second, _, second_database = runtime(corrupt_root, driver=OutputDriver())
        await second.start()
        valid_intent = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "command-corrupt-output",
            RunId("run-corrupt-output"),
            RequestId("request-corrupt-output"),
            "turn-corrupt-output",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-corrupt"),
                Message(MessageRole.USER, "hello"),
                "hello",
            ),
        )
        await second.client.submit_start(valid_intent)
        for _ in range(100):
            valid = await second.client.get_command(valid_intent.command_id)
            if valid.receipt.state.terminal:
                break
            await asyncio.sleep(0.01)
        assert valid.receipt.state is CommandState.APPLIED
        assert (
            await second.client.get_command(valid_intent.command_id)
        ).output_state is CommandOutputState.PRESENT
        second_database.connection.execute(
            "UPDATE conversation_outputs SET output_hash=? WHERE run_id=?",
            ("0" * 64, valid_intent.run_id.value),
        )
        conflict = await second.client.get_command(valid_intent.command_id)
        assert conflict.output_state is CommandOutputState.UNKNOWN
        assert conflict.output is None
        corrupt_json = "{"
        second_database.connection.execute(
            "UPDATE conversation_outputs SET output_json=?,output_hash=? WHERE run_id=?",
            (
                corrupt_json,
                hashlib.sha256(corrupt_json.encode()).hexdigest(),
                valid_intent.run_id.value,
            ),
        )
        corrupt = await second.client.get_command(valid_intent.command_id)
        assert corrupt.output_state is CommandOutputState.UNKNOWN
        assert corrupt.output is None
        await second.close()
        second_database.close()

    asyncio.run(case())


def test_startup_recovers_a_command_committed_before_runtime_start(tmp_path) -> None:
    async def case() -> None:
        value, uow, database = runtime(tmp_path)
        intent = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "command-before-crash",
            RunId("run-before-crash"),
            RequestId("request-before-crash"),
            "turn-before-crash",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-before-crash"),
                Message(MessageRole.USER, "recover"),
                "recover",
            ),
        )
        accepted = uow.submit_start_command(intent, now=1)
        assert accepted.state is CommandState.ACCEPTED
        assert uow.read_run(intent.run_id.value) is None
        await value.start()
        await value.wait_idle(intent.run_id)
        recovered = await value.client.get_command(intent.command_id)
        assert recovered.receipt.state is CommandState.APPLIED
        assert value.client.query(intent.run_id).state is RunState.COMPLETED
        await value.close()
        database.close()

    asyncio.run(case())


@pytest.mark.parametrize("crash_point", ("before_provider", "after_provider"))
@pytest.mark.parametrize("same_provider_instance", (False, True))
def test_start_command_provider_crash_replays_stable_preparation_intent(
    tmp_path, crash_point: str, same_provider_instance: bool
) -> None:
    async def case() -> None:
        first_context = StableContextProvider(crash_first=crash_point == "before_provider")
        crashing_memory = StableMemory(crash_first=crash_point == "after_provider")
        first, _, first_database = runtime(
            tmp_path,
            owner="crashed-start-owner",
            agent_memory=crashing_memory,
            context_provider=first_context,
        )
        await first.start()
        intent = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "start-provider-cut",
            RunId("run-start-provider-cut"),
            RequestId("request-start-provider-cut"),
            "turn-start-provider-cut",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-start-cut"),
                Message(MessageRole.USER, "start"),
                "start",
                context_source_snapshot_ref="sha256:" + "e" * 64,
            ),
        )
        accepted = await first.client.submit_start(intent)
        for _ in range(200):
            task = first._command_pump_task
            if task is not None and task.done():
                break
            await asyncio.sleep(0.01)
        assert task is not None and task.done()
        crashed = await first.client.get_command(intent.command_id)
        assert crashed.receipt.state is CommandState.CONTEXT_CALL_INTENT
        assert len(first_context.requests) == 1
        if crash_point == "before_provider":
            assert crashing_memory.recalls == []
        else:
            assert len(crashing_memory.recalls) == 1
        first_request = first_context.requests[0]
        await first.close()
        first_database.connection.execute(
            "UPDATE conversation_commands SET lease_expires_at=0 WHERE command_id=?",
            (intent.command_id,),
        )
        first_database.connection.execute(
            "UPDATE context_preparation_staging SET lease_expires_at=0 "
            "WHERE identity_key=? AND state='preparing'",
            (intent.run_id.value,),
        )
        first_database.close()

        retry_context = first_context if same_provider_instance else StableContextProvider()
        second, _, second_database = runtime(
            tmp_path,
            owner="recovered-start-owner",
            agent_memory=StableMemory(),
            context_provider=retry_context,
        )
        await second.start()
        for _ in range(200):
            recovered = await second.client.get_command(intent.command_id)
            if recovered.receipt.state.terminal:
                break
            await asyncio.sleep(0.01)
        assert recovered.receipt.state is CommandState.APPLIED
        assert recovered.receipt.intent_hash == accepted.intent_hash == intent.intent_hash
        assert retry_context.requests[-1] == first_request
        assert first_request.preparation_id.startswith("agent-memory-stage/v1/")
        await second.close()
        second_database.close()

    asyncio.run(case())


def test_pre_call_cancel_public_snapshots_are_absent_without_driver_call(tmp_path) -> None:
    async def case() -> None:
        driver = Driver()
        value, uow, database = runtime(tmp_path, driver=driver)
        start = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "start-cancelled-before-call",
            RunId("run-cancelled-before-call"),
            RequestId("request-cancelled-before-call"),
            "turn-cancelled-before-call",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-pre-cancel"),
                Message(MessageRole.USER, "do not run"),
                "do not run",
            ),
        )
        cancel = CancelCommandIntent(
            start.namespace,
            start.projection_key_id,
            "cancel-before-call",
            start.run_id,
        )
        uow.submit_start_command(start, now=1)
        uow.submit_cancel_command(cancel, now=2)
        await value.start()
        start_snapshot = await value.client.get_command(start.command_id)
        cancel_snapshot = await value.client.get_command(cancel.command_id)
        assert start_snapshot.receipt.state is CommandState.CANCELLED
        assert start_snapshot.output_state is CommandOutputState.ABSENT
        assert cancel_snapshot.receipt.state is CommandState.APPLIED
        assert cancel_snapshot.output_state is CommandOutputState.ABSENT
        assert driver.calls == 0
        assert value.client.query(start.run_id) is None
        await value.close()
        database.close()

    asyncio.run(case())


def test_dual_runtime_lease_takeover_treats_stale_owner_as_converged(tmp_path) -> None:
    class SlowFirstAdmission:
        def __init__(self) -> None:
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def evaluate(self, start):
            del start
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                await self.release.wait()
            return AdmissionVerdict(True)

    async def case() -> None:
        slow = SlowFirstAdmission()
        first, _, first_database = runtime(
            tmp_path,
            owner="command-owner-1",
            clock=time.monotonic,
            admission=slow,
            lease_ttl_seconds=0.05,
        )
        await first.start()
        intent = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "command-lease-takeover",
            RunId("run-lease-takeover"),
            RequestId("request-lease-takeover"),
            "turn-lease-takeover",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-takeover"),
                Message(MessageRole.USER, "hello"),
                "hello",
            ),
        )
        await first.client.submit_start(intent)
        await asyncio.wait_for(slow.entered.wait(), timeout=1)
        heartbeat = next(
            task
            for task in asyncio.all_tasks()
            if task.get_name() == f"simple-harness-command-heartbeat:{intent.command_id}"
        )
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await asyncio.sleep(0.06)

        second, _, second_database = runtime(
            tmp_path,
            owner="command-owner-2",
            clock=time.monotonic,
            lease_ttl_seconds=0.05,
        )
        await second.start()
        for _ in range(100):
            takeover = await second.client.get_command(intent.command_id)
            if takeover.receipt.state is CommandState.APPLIED:
                break
            await asyncio.sleep(0.01)
        assert takeover.receipt.state is CommandState.APPLIED

        slow.release.set()
        await asyncio.sleep(0.05)
        assert first._command_pump_task is not None
        assert not first._command_pump_task.done()

        followup = StartCommandIntent(
            "deployment/phone-2",
            "key-2",
            "command-after-stale-owner",
            RunId("run-after-stale-owner"),
            RequestId("request-after-stale-owner"),
            "turn-after-stale-owner",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-followup"),
                Message(MessageRole.USER, "again"),
                "again",
            ),
        )
        await first.client.submit_start(followup)
        for _ in range(100):
            settled = await first.client.get_command(followup.command_id)
            if settled.receipt.state.terminal:
                break
            await asyncio.sleep(0.01)
        assert settled.receipt.state is CommandState.APPLIED
        assert first._command_pump_task is not None
        assert not first._command_pump_task.done()
        await first.close()
        await second.close()
        first_database.close()
        second_database.close()

    asyncio.run(case())


@pytest.mark.parametrize("crash_point", ("before_provider", "after_provider"))
@pytest.mark.parametrize("same_provider_instance", (False, True))
def test_continue_command_provider_crash_replays_stable_preparation_intent(
    tmp_path, crash_point: str, same_provider_instance: bool
) -> None:
    async def case() -> None:
        waiting_driver = Driver(state=RunState.WAITING)
        first, _, first_database = runtime(
            tmp_path, driver=waiting_driver, owner="seed-waiting-owner"
        )
        await first.start()
        start = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "start-provider-crash",
            RunId("run-provider-crash"),
            RequestId("request-provider-crash"),
            "turn-provider-crash-root",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-provider-crash"),
                Message(MessageRole.USER, "root"),
                "root",
            ),
        )
        await first.client.submit_start(start)
        for _ in range(200):
            start_snapshot = await first.client.get_command(start.command_id)
            run = first.client.query(start.run_id)
            if (
                start_snapshot.receipt.state is CommandState.APPLIED
                and run is not None
                and run.state is RunState.WAITING
            ):
                break
            await asyncio.sleep(0.01)
        assert run is not None and run.state is RunState.WAITING
        await first.close()
        first_database.close()

        first_context = StableContextProvider(crash_first=crash_point == "before_provider")
        crashing_memory = StableMemory(crash_first=crash_point == "after_provider")
        crashed_runtime, _, crashed_database = runtime(
            tmp_path,
            driver=Driver(state=RunState.COMPLETED),
            owner="crashed-command-owner",
            agent_memory=crashing_memory,
            context_provider=first_context,
        )
        await crashed_runtime.start()
        continuation = ContinueCommandIntent(
            start.namespace,
            start.projection_key_id,
            "continue-provider-crash",
            start.run_id,
            "continuation-provider-crash",
            "turn-provider-crash-continuation",
            ConversationContinuationInput(
                Message(MessageRole.USER, "continue"),
                "continue",
                "sha256:" + "d" * 64,
            ),
        )
        accepted = await crashed_runtime.client.submit_continue(continuation)
        accepted_intent_hash = accepted.intent_hash
        for _ in range(200):
            task = crashed_runtime._command_pump_task
            if task is not None and task.done():
                break
            await asyncio.sleep(0.01)
        assert task is not None and task.done()
        crashed = await crashed_runtime.client.get_command(continuation.command_id)
        assert crashed.receipt.state is CommandState.CONTEXT_CALL_INTENT
        assert crashed.output_state is CommandOutputState.PENDING
        assert len(first_context.requests) == 1
        if crash_point == "before_provider":
            assert crashing_memory.recalls == []
        else:
            assert len(crashing_memory.recalls) == 1
        first_request = first_context.requests[0]
        await crashed_runtime.close()
        crashed_database.connection.execute(
            "UPDATE conversation_commands SET lease_expires_at=0 WHERE command_id=?",
            (continuation.command_id,),
        )
        crashed_database.connection.execute(
            "UPDATE context_preparation_staging SET lease_expires_at=0 "
            "WHERE identity_key=? AND state='preparing'",
            (continuation.continuation_id,),
        )
        crashed_database.close()

        retry_context = first_context if same_provider_instance else StableContextProvider()
        healthy_memory = StableMemory()
        recovered_runtime, _, recovered_database = runtime(
            tmp_path,
            driver=Driver(state=RunState.COMPLETED),
            owner="recovered-command-owner",
            agent_memory=healthy_memory,
            context_provider=retry_context,
        )
        await recovered_runtime.start()
        for _ in range(200):
            recovered = await recovered_runtime.client.get_command(continuation.command_id)
            if recovered.receipt.state.terminal:
                break
            await asyncio.sleep(0.01)
        assert recovered.receipt.state is CommandState.APPLIED
        assert recovered.receipt.intent_hash == accepted_intent_hash
        assert recovered.output_state is CommandOutputState.PENDING
        retry_request = retry_context.requests[-1]
        assert retry_request == first_request
        assert retry_request.preparation_id.startswith("agent-memory-stage/v1/")
        prior = await recovered_runtime.client.get_command(start.command_id)
        assert prior.output_state is CommandOutputState.PENDING
        stored_hash = recovered_database.connection.execute(
            "SELECT intent_hash FROM conversation_commands WHERE command_id=?",
            (continuation.command_id,),
        ).fetchone()
        assert stored_hash is not None and str(stored_hash[0]) == continuation.intent_hash
        await recovered_runtime.close()
        recovered_database.close()

    asyncio.run(case())


def test_continue_command_is_fifo_and_owns_the_terminal_output(tmp_path) -> None:
    class TwoTurnDriver:
        def __init__(self) -> None:
            self.calls = 0

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            self.calls += 1
            if self.calls == 1:
                return DriverResult(RunState.WAITING, {"waiting": True})
            return DriverResult(
                RunState.COMPLETED,
                {},
                conversation_output=ConversationTurnOutput(
                    Message(MessageRole.ASSISTANT, "second answer"), "second answer"
                ),
            )

    async def case() -> None:
        driver = TwoTurnDriver()
        value, _, database = runtime(tmp_path, driver=driver)
        await value.start()
        start = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "start-two-turn",
            RunId("run-two-turn"),
            RequestId("request-two-turn"),
            "turn-1",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-two-turn"),
                Message(MessageRole.USER, "first"),
                "first",
            ),
        )
        await value.client.submit_start(start)
        for _ in range(100):
            if (
                run := value.client.query(start.run_id)
            ) is not None and run.state is RunState.WAITING:
                break
            await asyncio.sleep(0.01)
        continuation = ContinueCommandIntent(
            start.namespace,
            start.projection_key_id,
            "continue-two-turn",
            start.run_id,
            "continuation-two-turn",
            "turn-2",
            ConversationContinuationInput(Message(MessageRole.USER, "second"), "second"),
        )
        accepted = await value.client.submit_continue(continuation)
        assert accepted.accept_seq == 1
        for _ in range(100):
            snapshot = await value.client.get_command(continuation.command_id)
            if snapshot.output_state is CommandOutputState.PRESENT:
                break
            await asyncio.sleep(0.01)
        assert snapshot.output is not None and snapshot.output.memory_text == "second answer"
        prior_start = await value.client.get_command(start.command_id)
        assert prior_start.receipt.state is CommandState.APPLIED
        assert prior_start.output_state is CommandOutputState.ABSENT
        assert prior_start.output is None
        terminal_continuation = await value.client.get_command(continuation.command_id)
        assert terminal_continuation.output_state is CommandOutputState.PRESENT
        assert terminal_continuation.output == snapshot.output
        row = database.connection.execute(
            "SELECT command_id FROM conversation_outputs WHERE run_id=?",
            (start.run_id.value,),
        ).fetchone()
        assert row is not None and row[0] == continuation.command_id
        await value.close()
        database.close()

    asyncio.run(case())


def test_cancel_command_fences_and_converges_an_active_run(tmp_path) -> None:
    class BlockingCommandDriver:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled driver must not return")

    async def case() -> None:
        driver = BlockingCommandDriver()
        value, _, database = runtime(tmp_path, driver=driver)
        await value.start()
        start = StartCommandIntent(
            "deployment/phone",
            "key-1",
            "start-cancel",
            RunId("run-cancel-command"),
            RequestId("request-cancel-command"),
            "turn-cancel-command",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session-cancel"),
                Message(MessageRole.USER, "wait"),
                "wait",
            ),
        )
        await value.client.submit_start(start)
        await asyncio.wait_for(driver.entered.wait(), timeout=1)
        cancel = CancelCommandIntent(
            start.namespace,
            start.projection_key_id,
            "cancel-command",
            start.run_id,
        )
        accepted = await value.client.submit_cancel(cancel)
        assert accepted.accept_seq == 1
        for _ in range(100):
            snapshot = await value.client.get_command(cancel.command_id)
            run = value.client.query(start.run_id)
            if (
                snapshot.receipt.state is CommandState.APPLIED
                and run is not None
                and run.state is RunState.CANCELLED
            ):
                break
            await asyncio.sleep(0.01)
        assert snapshot.output_state is CommandOutputState.ABSENT
        await value.close()
        database.close()

    asyncio.run(case())


def test_concurrent_start_publishes_ready_once_and_clients_fail_before_ready(
    tmp_path,
) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def reconcile() -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()

        value._ports.reconciliation.reconcile = reconcile
        first = asyncio.create_task(value.start())
        second = asyncio.create_task(value.start())
        await entered.wait()
        assert value.state is RuntimeLifecycleState.STARTING
        try:
            await value.client.start(request("too-early"))
        except Exception as error:  # public error code is the contract
            assert getattr(error, "code", None) == "runtime_not_ready"
        else:
            raise AssertionError("client start must fail before READY")
        assert (
            database.connection.execute("SELECT count(*) FROM conversation_run_modes").fetchone()[0]
            == 0
        )
        assert (
            database.connection.execute(
                "SELECT count(*) FROM conversation_command_namespaces"
            ).fetchone()[0]
            == 0
        )
        release.set()
        await asyncio.gather(first, second)
        assert calls == 1
        assert value.state is RuntimeLifecycleState.READY
        try:
            await value.client.start(object())  # type: ignore[arg-type]
        except TypeError as error:
            assert str(error) == "value must use RunStart"
        else:
            raise AssertionError("legacy start must reject an open input shape")
        assert (
            database.connection.execute("SELECT count(*) FROM conversation_run_modes").fetchone()[0]
            == 0
        )
        try:
            await value.client.start_conversation(
                ConversationTurnInput(
                    AgentIdentity("deployment", "household", "actor", "session-preflight"),
                    Message(MessageRole.USER, "invalid"),
                    "invalid",
                ),
                run_id=RunId("run-invalid-preflight"),
                tool_catalog_generation=0,
            )
        except ValueError as error:
            assert "tool_catalog_generation" in str(error)
        else:
            raise AssertionError("invalid conversation start must fail before reservation")
        assert (
            database.connection.execute("SELECT count(*) FROM conversation_run_modes").fetchone()[0]
            == 0
        )
        await value.close()
        assert value.state is RuntimeLifecycleState.CLOSED
        database.close()

    asyncio.run(case())


def test_start_failure_is_not_restartable_but_close_is_idempotent(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)

        async def fail() -> None:
            raise RuntimeError("startup failed")

        value._ports.reconciliation.reconcile = fail
        for _ in range(2):
            try:
                await value.start()
            except RuntimeError:
                pass
            else:
                raise AssertionError("failed Runtime must not become ready")
        assert value.state is RuntimeLifecycleState.FAILED
        await value.close()
        await value.close()
        assert value.state is RuntimeLifecycleState.CLOSED
        database.close()

    asyncio.run(case())


def test_policy_pin_rejects_drift_after_pre_checkpoint_restart(tmp_path) -> None:
    class BeforeCheckpointDriver:
        def __init__(self, fingerprint: str, *, block: bool) -> None:
            self.policy_fingerprint = fingerprint
            self.block = block
            self.calls = 0
            self.started = asyncio.Event()

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            self.calls += 1
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            return DriverResult(RunState.COMPLETED, {"answer": "must-not-run"})

    async def case() -> None:
        first_driver = BeforeCheckpointDriver("policy-a", block=True)
        first, first_uow, first_database = runtime(
            tmp_path, driver=first_driver, owner="owner-policy-a"
        )
        await first.start()
        await first.client.start(request("policy-drift"))
        await first_driver.started.wait()
        assert first_uow.read_react_checkpoint("run-policy-drift") is None
        snapshot = first_uow.read_start_snapshot("run-policy-drift")
        assert snapshot is not None and snapshot["policy_fingerprint"] == "policy-a"
        await first.close()
        first_database.close()

        drifted_driver = BeforeCheckpointDriver("policy-b", block=False)
        second, second_uow, second_database = runtime(
            tmp_path, driver=drifted_driver, owner="owner-policy-b"
        )
        await second.start()
        await second.wait_idle(RunId("run-policy-drift"))
        recovered = second_uow.read_run("run-policy-drift")
        assert recovered is not None and recovered.state is RunState.FAILED
        assert drifted_driver.calls == 0
        assert second_uow.read_react_checkpoint("run-policy-drift") is None
        await second.close()
        second_database.close()

    asyncio.run(case())


def test_start_racing_close_never_publishes_ready(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def reconcile() -> None:
            entered.set()
            await release.wait()

        value._ports.reconciliation.reconcile = reconcile
        starting = asyncio.create_task(value.start())
        await entered.wait()
        closing = asyncio.create_task(value.close())
        await asyncio.wait_for(closing, timeout=0.2)
        outcome = await asyncio.gather(starting, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError)
        assert value.state is RuntimeLifecycleState.CLOSED
        assert value._wake_drain_task is None
        assert value._delivery_pump_task is None
        release.set()
        database.close()

    asyncio.run(case())


def test_concurrent_close_is_single_terminal_lifecycle_transition(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        await value.start()
        await asyncio.gather(*(value.close() for _ in range(8)))
        assert value.state is RuntimeLifecycleState.CLOSED
        assert value._wake_drain_task is None
        assert value._delivery_pump_task is None
        assert value._leases == {}
        database.close()

    asyncio.run(case())


def test_delivery_sink_failure_is_released_and_retried_by_runtime(tmp_path) -> None:
    class FlakySink:
        def __init__(self) -> None:
            self.calls = 0
            self.keys: list[str] = []

        async def deliver(self, payload, *, idempotency_key):
            del payload
            self.calls += 1
            self.keys.append(idempotency_key)
            if self.calls == 1:
                raise RuntimeError("transient sink failure")

    async def case() -> None:
        sink = FlakySink()
        value, uow, database = runtime(tmp_path, delivery_sink=sink)
        await seed_terminal_deliveries(uow, count=1)

        await value.start()

        delivery = uow.read_delivery("delivery-000")
        assert delivery is not None and delivery.state is DeliveryState.DELIVERED
        assert sink.calls == 2
        assert sink.keys == ["delivery-key-000", "delivery-key-000"]
        await value.close()
        database.close()

    asyncio.run(case())


def test_startup_backlog_remains_durable_and_drains_after_runtime_reopen(
    tmp_path,
) -> None:
    class FailingSink:
        def __init__(self) -> None:
            self.calls = 0

        async def deliver(self, payload, *, idempotency_key):
            del payload, idempotency_key
            self.calls += 1
            raise RuntimeError("sink remains offline")

    class RecordingSink:
        def __init__(self) -> None:
            self.keys: list[str] = []
            self.all_delivered = asyncio.Event()

        async def deliver(self, payload, *, idempotency_key):
            del payload
            self.keys.append(idempotency_key)
            if len(self.keys) == 101:
                self.all_delivered.set()

    async def case() -> None:
        failing = FailingSink()
        first, first_uow, first_database = runtime(
            tmp_path,
            owner="runtime-before-reopen",
            delivery_sink=failing,
        )
        await seed_terminal_deliveries(first_uow, count=101)
        await first.start()
        assert failing.calls >= 100
        assert (
            first_database.connection.execute(
                "SELECT count(*) FROM delivery_outbox WHERE state='pending'"
            ).fetchone()[0]
            == 101
        )
        await first.close()
        first_database.close()

        recording = RecordingSink()
        second, second_uow, second_database = runtime(
            tmp_path,
            owner="runtime-after-reopen",
            delivery_sink=recording,
        )
        await second.start()
        second._delivery_wake.set()
        await asyncio.wait_for(recording.all_delivered.wait(), timeout=0.2)
        delivered = second_database.connection.execute(
            "SELECT count(*) FROM delivery_outbox WHERE state='delivered'"
        ).fetchone()[0]
        assert delivered == 101
        assert len(recording.keys) == len(set(recording.keys)) == 101
        assert second_uow.read_delivery("delivery-100").state is DeliveryState.DELIVERED
        await second.close()
        second_database.close()

    asyncio.run(case())


def test_fixed_root_has_no_classifier_and_rejects_override(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        await value.start()
        await value.client.start(request("fixed"))
        await value.wait_idle(RunId("run-fixed"))
        record = value.client.query(RunId("run-fixed"))
        assert record.profile_key == "agent.general"
        await value.close()
        database.close()

    asyncio.run(case())

    value, uow, _ = runtime(tmp_path)
    try:
        build_runtime(
            uow,
            {"other": RuntimeProfile("other", "react")},
            {"react": Driver()},
            value._ports,
            root_profile_key="other",
        )
    except ValueError as error:
        assert "fixed" in str(error)
    else:
        raise AssertionError("root override must fail")
    value._uow.database.close()


def test_workflow_start_snapshot_requires_typed_admission() -> None:
    start = request("workflow-roundtrip")
    try:
        bind_start_snapshot(start, profile_key="workflow.demo", driver_kind="workflow")
    except ValueError as error:
        assert "workflow admission" in str(error)
    else:
        raise AssertionError("workflow start must carry durable admission")


def test_runtime_ports_require_all_authority_seams() -> None:
    try:
        RuntimePorts(  # type: ignore[call-arg]
            context=None,
            tool_catalog=Catalog(),
        )
    except TypeError:
        pass
    else:
        raise AssertionError("authority Ports must not be optional")


def test_recovery_lease_is_single_owner_then_expiry_allows_takeover(tmp_path) -> None:
    _, uow, database = runtime(tmp_path, owner="owner-1", clock=lambda: 10.0)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-recover",
        request_id="request-recover",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={
            "schema_version": 1,
            "profile_key": "agent.general",
            "driver_kind": "react",
            "turn_id": "turn-run-recover",
            "tool_catalog_generation": 1,
            "input": {},
        },
        event_id="run-recover:created",
        now=1.0,
    )
    first, first_lease = uow.claim_runtime_activation(
        run_id="run-recover",
        owner_id="owner-1",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=5.0,
    )
    assert first.state is RunState.RUNNING
    from simple_harness.execution.uow import UnitOfWorkConflict

    try:
        uow.claim_runtime_activation(
            run_id="run-recover",
            owner_id="owner-2",
            namespace="runtime.kernel",
            now=3.0,
            lease_ttl_seconds=5.0,
        )
    except UnitOfWorkConflict:
        pass
    else:
        raise AssertionError("live owner must not be stolen")
    _, second_lease = uow.claim_runtime_activation(
        run_id="run-recover",
        owner_id="owner-2",
        namespace="runtime.kernel",
        now=8.0,
        lease_ttl_seconds=5.0,
    )
    assert second_lease.epoch == first_lease.epoch + 1
    database.close()


def test_catalog_stale_terminalizes_once_without_driver(tmp_path) -> None:
    async def case() -> None:
        driver = Driver()
        value, _, database = runtime(tmp_path, driver=driver, catalog=Catalog(2))
        await value.start()
        await value.client.start(request("stale", generation=1))
        await value.wait_idle(RunId("run-stale"))
        assert value.client.query(RunId("run-stale")).state is RunState.FAILED
        assert driver.calls == 0
        failures = database.connection.execute(
            "SELECT count(*) FROM run_events WHERE run_id=? AND kind='run.failed'",
            ("run-stale",),
        ).fetchone()[0]
        assert failures == 1
        await value.close()
        database.close()

    asyncio.run(case())


def test_runtime_heartbeat_loss_cancels_stale_owner_before_any_write(tmp_path) -> None:
    class Clock:
        value = 10.0

        def __call__(self) -> float:
            return self.value

    class ControlledSleep:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def __call__(self, delay: float) -> None:
            assert delay > 0
            self.entered.set()
            await self.release.wait()

    class BlockingDriver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False
            self.stale_append_rejected = False

        async def start(self, invocation, *, context, cancel):
            del cancel
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                from simple_harness.contracts.messages import Message, MessageRole
                from simple_harness.execution.uow import UnitOfWorkConflict

                try:
                    context.append(
                        RunId(invocation.run.run_id),
                        invocation.execution_lease,
                        0,
                        "stale-append",
                        (Message(MessageRole.USER, "must-not-write"),),
                    )
                except UnitOfWorkConflict:
                    self.stale_append_rejected = True
                raise

    async def case() -> None:
        clock = Clock()
        controlled_sleep = ControlledSleep()
        driver = BlockingDriver()
        value, uow, database = runtime(
            tmp_path,
            driver=driver,
            clock=clock,
            sleep=controlled_sleep,
            owner="owner-heartbeat",
        )
        await value.start()
        await value.client.start(request("heartbeat"))
        await driver.started.wait()
        await controlled_sleep.entered.wait()
        clock.value = 41.0
        _, stolen = uow.claim_runtime_activation(
            run_id="run-heartbeat",
            owner_id="owner-thief",
            namespace="runtime.kernel",
            now=clock(),
            lease_ttl_seconds=30.0,
        )
        assert stolen.owner_id == "owner-thief"
        controlled_sleep.release.set()
        for _ in range(100):
            if driver.cancelled:
                break
            await asyncio.sleep(0)
        assert driver.cancelled is True
        assert driver.stale_append_rejected is True
        assert (
            database.connection.execute(
                "SELECT count(*) FROM workflow_checkpoints WHERE run_id='run-heartbeat'"
            ).fetchone()[0]
            == 0
        )
        assert (
            database.connection.execute(
                "SELECT count(*) FROM run_events WHERE run_id='run-heartbeat' "
                "AND kind IN ('run.completed','run.failed','run.cancelled')"
            ).fetchone()[0]
            == 0
        )
        await value.close()
        assert value._heartbeats == {}
        database.close()

    asyncio.run(case())


def test_close_is_bounded_for_noncooperative_driver_and_stops_heartbeat(
    tmp_path,
) -> None:
    class NonCooperativeDriver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.release.wait()
                self.finished.set()
                raise

    async def case() -> None:
        driver = NonCooperativeDriver()
        value, uow, database = runtime(
            tmp_path,
            driver=driver,
            close_timeout_seconds=0.01,
        )
        await value.start()
        await value.client.start(request("noncooperative"))
        await driver.started.wait()

        await asyncio.wait_for(value.close(), timeout=0.2)

        assert value._heartbeats == {}
        assert value._leases == {}
        row = database.connection.execute(
            "SELECT expires_at FROM workflow_leases "
            "WHERE run_id='run-noncooperative' AND namespace='runtime.kernel'"
        ).fetchone()
        assert row is not None and row[0] == 10.0
        assert (
            database.connection.execute(
                "SELECT state FROM run_fences WHERE run_id='run-noncooperative'"
            ).fetchone()[0]
            == "released"
        )

        driver.release.set()
        await asyncio.wait_for(driver.finished.wait(), timeout=0.2)
        assert uow.read_run("run-noncooperative").state is RunState.RUNNING
        database.close()

    asyncio.run(case())


def test_h13_combined_wake_drain_stops_before_noncooperative_driver_and_heartbeat(
    tmp_path,
) -> None:
    class NonCooperativeDriver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.release.wait()
                raise

    async def case() -> None:
        driver = NonCooperativeDriver()
        value, _, database = runtime(
            tmp_path,
            driver=driver,
            close_timeout_seconds=0.01,
        )
        await value.start()
        assert value._wake_drain_task is not None
        await value.client.start(request("h13-close"))
        await driver.started.wait()
        await asyncio.wait_for(value.close(), timeout=0.2)
        assert value._wake_drain_task is None
        assert value._heartbeats == {}
        assert value._leases == {}
        driver.release.set()
        database.close()

    asyncio.run(case())
