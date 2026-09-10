# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T4: base-agent/v1 namespace fence, public entries, zero start_snapshot change."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from kernel_fixture import Catalog, NoopPort, create_agent
from provider_fixture import MODEL, ScriptedProvider

from simple_harness import AgentIdentity, AllowAllAdmission, Message, MessageRole, canonical_json
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.runtime import assemble_runtime
from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.execution.command_ingress import CommandError, CommandErrorCode
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.runtime import (
    ConversationContinuationInput,
    ConversationTurnInput,
    ConversationTurnOutput,
    DriverResult,
    RunClient,
    RunStart,
    RuntimePorts,
    RuntimeProfile,
    SqliteContextPort,
    build_runtime,
)
from simple_harness.runtime.start_snapshot import bind_start_snapshot

SRC = Path(__file__).resolve().parents[2] / "src" / "simple_harness"
# sha256 of the files at fd12e7dd: Slice 1 must not touch them (closure E2/#3).
FROZEN_FILES = {
    "runtime/start_snapshot.py": "a349f434744eeefea24fbfafdf420e71cc4eda9c21127d96a575450704b96bae",
    "runtime/drivers/start_mode.py": "adcf81141ea4fc535739fa1db2a662a46b11c0b6274b2a3337238c09b88737ea",
}


def _ports(tmp_path, provider):
    return AgentRuntimePorts(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "fence.db"),
        model=MODEL,
        owner_id="fence-owner",
    )


def _identity() -> AgentIdentity:
    return AgentIdentity("deployment", "household", "actor", "session-legacy")


def test_legacy_start_conversation_rejects_base_agent_run(tmp_path):
    async def case():
        assembled = assemble_runtime(_ports(tmp_path, ScriptedProvider([])))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="fenced-a")
            with pytest.raises(CommandError) as info:
                await RunClient(runtime).start_conversation(
                    ConversationTurnInput(_identity(), Message(MessageRole.USER, "hi"), "hi"),
                    run_id=RunId("fenced-a"),
                )
            assert info.value.code is CommandErrorCode.RUN_MODE_CONFLICT

    asyncio.run(case())


def test_signal_conversation_rejects_base_agent_run(tmp_path):
    async def case():
        assembled = assemble_runtime(_ports(tmp_path, ScriptedProvider([])))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="fenced-b")
            with pytest.raises(CommandError) as info:
                await RunClient(runtime).signal_conversation(
                    RunId("fenced-b"),
                    continuation_id="c-1",
                    value=ConversationContinuationInput(Message(MessageRole.USER, "x"), "x"),
                )
            assert info.value.code is CommandErrorCode.RUN_MODE_CONFLICT

    asyncio.run(case())


def _base_input_kwargs(agent_id: str):
    message = Message(MessageRole.USER, "hello").to_dict()
    return dict(
        agent_id=agent_id,
        turn_id=f"{agent_id}:input:i1",
        input_id="i1",
        input_hash="0" * 64,
        input_json={"message": message},
        message=message,
    )


def test_signal_base_agent_input_rejects_legacy_run(tmp_path):
    async def case():
        assembled = assemble_runtime(_ports(tmp_path, ScriptedProvider([])))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            uow.reserve_legacy_run_mode(run_id="legacy-1", intent_hash="a" * 64, now=1.0)
            with pytest.raises(CommandError) as info:
                await runtime.signal_base_agent_input(
                    RunId("legacy-1"), **_base_input_kwargs("legacy-1")
                )
            assert info.value.code is CommandErrorCode.RUN_MODE_CONFLICT
            assert uow.read_agent_turn("legacy-1:input:i1") is None

    asyncio.run(case())


def test_signal_base_agent_input_rejects_unmanaged_run(tmp_path):
    async def case():
        assembled = assemble_runtime(_ports(tmp_path, ScriptedProvider([])))
        runtime = assembled.runtime
        async with runtime:
            with pytest.raises(CommandError) as info:
                await runtime.signal_base_agent_input(
                    RunId("nobody"), **_base_input_kwargs("nobody")
                )
            assert info.value.code is CommandErrorCode.RUN_MODE_CONFLICT

    asyncio.run(case())


def test_run_client_signal_refuses_forged_base_agent_input(tmp_path):
    async def case():
        assembled = assemble_runtime(_ports(tmp_path, ScriptedProvider([])))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            uow.reserve_legacy_run_mode(run_id="legacy-2", intent_hash="a" * 64, now=1.0)
            with pytest.raises(ValueError, match="signal_base_agent_input"):
                RunClient(runtime).signal(
                    RunId("legacy-2"), signal_id="f-1", payload={"kind": "base_agent_input"}
                )

    asyncio.run(case())


class _ReactStub:
    """Legacy react driver stub: the final answer ends the Run (COMPLETED)."""

    def __init__(self) -> None:
        self.calls = 0

    async def start(self, invocation, *, context, cancel):  # type: ignore[no-untyped-def]
        del invocation, context, cancel
        self.calls += 1
        return DriverResult(
            RunState.COMPLETED,
            {"answer": "ok"},
            conversation_output=ConversationTurnOutput(Message(MessageRole.ASSISTANT, "ok"), "ok"),
        )


def _ordinary_runtime(tmp_path):
    """A separate react Runtime: the BaseAgent runtime's root profile is base_agent
    (closure E11), so ordinary Runs must be exercised on their own composition."""

    database = Database.open(tmp_path / "ordinary.db")
    uow = SqliteExecutionUnitOfWork(database)
    noop = NoopPort()
    driver = _ReactStub()
    runtime = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "react")},
        {"react": driver},
        RuntimePorts(
            provider=noop, tools=noop, authorization=noop,
            context=SqliteContextPort(database, clock=lambda: 10.0), delivery=noop,
            tool_reconciliation=noop, reconciliation=noop, provider_reconciliation=noop,
            react_checkpoint=uow, tool_catalog=Catalog(), owner_id="ordinary-owner",
            clock=lambda: 10.0, admission=AllowAllAdmission(),
        ),
        close_hook=database.close,
    )
    return runtime, uow, driver


def test_ordinary_run_unaffected(tmp_path):
    async def case():
        runtime, uow, driver = _ordinary_runtime(tmp_path)
        async with runtime:
            client = RunClient(runtime)
            record = await client.start_conversation(
                ConversationTurnInput(_identity(), Message(MessageRole.USER, "hi"), "hi"),
                run_id=RunId("ordinary-1"),
            )
            await runtime.wait_idle(RunId("ordinary-1"))
            assert driver.calls == 1
            assert uow.read_run(record.run_id).state is RunState.COMPLETED
            # The base-agent entry rejects an ordinary (legacy/runtime) Run too.
            with pytest.raises(CommandError):
                await runtime.signal_base_agent_input(
                    RunId("ordinary-1"), **_base_input_kwargs("ordinary-1")
                )

    asyncio.run(case())


def test_build_runtime_rejects_unregistered_base_agent_driver(tmp_path):
    database = Database.open(tmp_path / "reject.db")
    uow = SqliteExecutionUnitOfWork(database)
    noop = NoopPort()
    ports = RuntimePorts(
        provider=noop, tools=noop, authorization=noop,
        context=SqliteContextPort(database, clock=lambda: 10.0), delivery=noop,
        tool_reconciliation=noop, reconciliation=noop, provider_reconciliation=noop,
        react_checkpoint=uow, tool_catalog=Catalog(), owner_id="reject-owner",
        clock=lambda: 10.0, admission=AllowAllAdmission(),
    )
    with pytest.raises(ValueError, match="agent.general driver is not registered"):
        build_runtime(
            uow,
            {"agent.general": RuntimeProfile("agent.general", "base_agent")},
            {"react": _ReactStub()},
            ports,
        )
    database.close()


def test_v7_snapshot_bytes_unchanged():
    start = RunStart(
        ExecutionSessionId("session-v7"),
        RunId("run-v7"),
        RequestId("request-v7"),
        "turn-v7",
        {"prompt": "fixed"},
        1,
    )
    snapshot = bind_start_snapshot(
        start, profile_key="agent.general", driver_kind="react", policy_fingerprint=None
    )
    encoded = canonical_json(snapshot.to_json())
    assert snapshot.to_json()["schema_version"] == 7
    assert hashlib.sha256(encoded.encode("utf-8")).hexdigest() == (
        "df8a29eae8bf81f6b4c31bdd2121b81a1f7b95f69c0ab5b7400bf5f33b3112bc"
    )


def test_start_snapshot_module_untouched():
    for relative, digest in FROZEN_FILES.items():
        actual = hashlib.sha256((SRC / relative).read_bytes()).hexdigest()
        assert actual == digest, relative
