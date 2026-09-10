# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Minimal kernel-level fixture for BaseAgent tests (no agents/ runtime assembly).

Mirrors ``tests/integration/runtime/test_kernel_start.py``: noop ports, a fake
driver, and helpers that create a base_agent Run plus bindings through the kernel's
internal start path (the public entry points arrive in T4/T7).
"""

from __future__ import annotations

import asyncio
import hashlib

from simple_harness import AllowAllAdmission, Message, MessageRole, canonical_json
from simple_harness.agents.contracts import AgentTurnResult, AgentTurnState
from simple_harness.contracts import ExecutionSessionId, RequestId, RunId, thaw_json
from simple_harness.execution.base_agent import BASE_AGENT_INPUT_KIND
from simple_harness.execution.context_authority import ToolCatalogSnapshot
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.providers import ProviderToolSpec
from simple_harness.runtime import (
    DriverResult,
    RunStart,
    RuntimePorts,
    RuntimeProfile,
    SqliteContextPort,
    build_runtime,
)


class NoopPort:
    async def reconcile(self) -> None:
        return None


class Catalog:
    def current_generation(self) -> int:
        return 1

    def resolve(self, generation: int, content_fingerprint: str) -> ToolCatalogSnapshot | None:
        if generation != 1:
            return None
        return ToolCatalogSnapshot(
            generation,
            content_fingerprint,
            (ProviderToolSpec("read_status", "Read status", {"type": "object"}),),
            0.0,
        )


class EchoAgentDriver:
    """Fake BaseAgent driver: answers each ``base_agent_input`` with one turn result."""

    def __init__(self, *, answer: str = "answer") -> None:
        self.answer = answer
        self.calls = 0
        self.inputs: list[dict] = []

    async def start(self, invocation, *, context, cancel):  # type: ignore[no-untyped-def]
        del context, cancel
        self.calls += 1
        turn_input = None
        for continuation in invocation.continuations:
            payload = thaw_json(continuation.payload)
            if isinstance(payload, dict) and payload.get("kind") == BASE_AGENT_INPUT_KIND:
                turn_input = payload
        if turn_input is None:
            return DriverResult(RunState.WAITING, {"base_agent_stage": "idle"})
        self.inputs.append(turn_input)
        result = AgentTurnResult(
            turn_id=str(turn_input["turn_id"]),
            agent_id=str(turn_input["agent_id"]),
            seq=int(turn_input["seq"]),
            state=AgentTurnState.COMMITTED,
            public_output=Message(MessageRole.ASSISTANT, f"{self.answer}#{turn_input['seq']}"),
        )
        outcome = result.to_outcome(
            input_id=str(turn_input["input_id"]), input_hash=str(turn_input["input_hash"])
        )
        return DriverResult(
            RunState.WAITING, {"response_present": True}, agent_turn_outcome=outcome
        )


def build(tmp_path, *, driver, clock=lambda: 10.0, owner="owner-1", name="runtime.db"):
    database = Database.open(tmp_path / name)
    uow = SqliteExecutionUnitOfWork(database)
    noop = NoopPort()
    runtime = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "base_agent")},
        {"base_agent": driver},
        RuntimePorts(
            provider=noop,
            tools=noop,
            authorization=noop,
            context=SqliteContextPort(database, clock=clock),
            delivery=noop,
            tool_reconciliation=noop,
            reconciliation=noop,
            provider_reconciliation=noop,
            react_checkpoint=uow,
            tool_catalog=Catalog(),
            owner_id=owner,
            clock=clock,
            admission=AllowAllAdmission(),
        ),
        close_hook=database.close,
    )
    return runtime, uow, database


def input_hash_for(text: str) -> str:
    return hashlib.sha256(canonical_json({"text": text}).encode("utf-8")).hexdigest()


async def create_agent(runtime, uow, *, agent_id: str, owner_scope: str = "owner") -> str:
    """Create the Run + binding for one BaseAgent through kernel-internal paths."""

    start = RunStart(
        ExecutionSessionId(f"session-{agent_id}"),
        RunId(agent_id),
        RequestId(f"request-{agent_id}"),
        f"{agent_id}:start",
        {
            "capability_snapshot": {"tools": []},
            "base_agent_binding": {
                "agent_id": agent_id,
                "config_hash": "0" * 64,
                "api_mode": "base_agent_v1",
                "role": "root",
                "owner_scope": owner_scope,
            },
            "messages": [],
            # An upper-bound reservation needs max_output_tokens; otherwise the
            # charge stays UNKNOWN and the fail-closed budget refuses the next turn.
            "max_output_tokens": 1024,
        },
        1,
    )
    await runtime.start_base_agent_run(start)
    uow.create_agent_binding(
        agent_id=agent_id,
        run_id=agent_id,
        owner_scope=owner_scope,
        role="root",
        creation_key=f"create:{agent_id}",
        config_json={"name": agent_id},
        config_hash="0" * 64,
        now=1.0,
    )
    await runtime.wait_idle(RunId(agent_id))
    return agent_id


async def submit(runtime, uow, *, agent_id: str, input_id: str, text: str, now: float = 2.0):
    del uow, now
    turn_id = f"{agent_id}:input:{input_id}"
    message = Message(MessageRole.USER, text).to_dict()
    return await runtime.signal_base_agent_input(
        RunId(agent_id),
        agent_id=agent_id,
        turn_id=turn_id,
        input_id=input_id,
        input_hash=input_hash_for(text),
        input_json={"message": message},
        message=message,
    )


async def run_states(uow, run_id: str) -> list[str]:
    return [
        str(row[0])
        for row in uow.database.connection.execute(
            "SELECT kind FROM run_events WHERE run_id=? ORDER BY durable_seq", (run_id,)
        )
    ]
