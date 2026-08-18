# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Consumer adapter layer regression tests (0.1.3 defects).

Covers C-AC-1..5:
- model field reaches ProviderTarget (fixes hardcoded "consumer-model")
- usage trusted when the provider echoes the declared model
- usage still unknown/refused when the model mismatches (FAIL-2 not weakened)
- closed tool schema accepts arguments; no-schema tools stay no-arg fail-closed
- 0.1.2 consumers (no new fields) still build and run
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from simple_harness.contracts import (
    CallId,
    ExecutionSessionId,
    Message,
    MessageRole,
    RequestId,
    RunId,
)
from simple_harness.execution.uow import RunState
from simple_harness.providers import (
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    ProviderUsage,
)
from simple_harness.runtime import (
    AuthorizationRequest,
    AuthorizationResult,
    RunStart,
    RunClient,
)
from simple_harness.runtime.consumer_adapter import (
    ConsumerRuntimePorts,
    _ConsumerProviderAdapter,
    _ConsumerToolExecutorAdapter,
    build_consumer_runtime,
)
from simple_harness.tools import ToolCall, ToolResult


class _AllowAll:
    async def request_authorization(self, request: AuthorizationRequest) -> AuthorizationResult:
        return AuthorizationResult.allow()


class _EchoToolExecutor:
    """Records the arguments it was invoked with, so tests can assert they
    survived schema validation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        self.calls.append((call.name, dict(call.arguments)))
        return ToolResult.succeeded(call.call_id, {"ok": True})


def _calculator_provider(model: str):
    """A two-turn mock provider: turn 1 requests the calculator tool (no args),
    turn 2 returns the final answer. Both echo `model` with usage."""

    class _Provider:
        def __init__(self) -> None:
            self.call_count = 0

        async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:
            self.call_count += 1
            if self.call_count == 1:
                return ProviderResponse(
                    request_id=request.request_id,
                    message=Message(MessageRole.ASSISTANT, ""),
                    tool_calls=(
                        ProviderToolCall(CallId("call-1"), "calculate", {}),
                    ),
                    usage=ProviderUsage(50, 20, 70),
                    model=model,
                    finish_reason="tool_calls",
                )
            return ProviderResponse(
                request_id=request.request_id,
                message=Message(MessageRole.ASSISTANT, "answer is 4"),
                tool_calls=(),
                usage=ProviderUsage(80, 30, 110),
                model=model,
                finish_reason="stop",
            )

    return _Provider()


def _run_once(ports: ConsumerRuntimePorts) -> RunState:
    import asyncio

    async def _go() -> RunState:
        runtime = await build_consumer_runtime(ports)
        try:
            await runtime.__aenter__()
            client = RunClient(runtime)
            suffix = uuid.uuid4().hex[:8]
            run_id = RunId(f"run-{suffix}")
            await client.start(
                RunStart(
                    execution_session_id=ExecutionSessionId(f"session-{suffix}"),
                    run_id=run_id,
                    request_id=RequestId(f"req-{suffix}"),
                    turn_id="turn-001",
                    tool_catalog_generation=1,
                    input={
                        "messages": [{"role": "user", "content": "2+2?"}],
                        "capability_snapshot": {"tools": ["calculate"]},
                        "max_output_tokens": 1024,
                    },
                )
            )
            await runtime.wait_idle(run_id)
            record = client.query(run_id)
            return record.state if record is not None else RunState.FAILED
        finally:
            await runtime.__aexit__(None, None, None)

    return asyncio.run(_go())


def test_model_field_reaches_provider_target() -> None:
    adapter = _ConsumerProviderAdapter(_calculator_provider("unused"), "gpt-4o")
    assert adapter.target.model == "gpt-4o"


def test_default_model_preserved_for_backward_compat() -> None:
    adapter = _ConsumerProviderAdapter(_calculator_provider("unused"), "consumer-model")
    assert adapter.target.model == "consumer-model"


def test_usage_trusted_when_model_matches() -> None:
    db = Path(tempfile.mkdtemp(prefix="consumer-test-")) / "execution.db"
    ports = ConsumerRuntimePorts(
        provider=_calculator_provider("gpt-4o"),
        tool_executor=_EchoToolExecutor(),
        authorization=_AllowAll(),
        database_path=str(db),
        tool_names=("calculate",),
        max_turns=10,
        max_tool_calls=20,
        model="gpt-4o",
    )
    state = _run_once(ports)
    assert state is RunState.COMPLETED


def test_model_mismatch_still_unknown_and_refuses() -> None:
    db = Path(tempfile.mkdtemp(prefix="consumer-test-")) / "execution.db"
    ports = ConsumerRuntimePorts(
        provider=_calculator_provider("other-model"),
        tool_executor=_EchoToolExecutor(),
        authorization=_AllowAll(),
        database_path=str(db),
        tool_names=("calculate",),
        max_turns=10,
        max_tool_calls=20,
        model="gpt-4o",  # provider echoes "other-model" -> mismatch
    )
    state = _run_once(ports)
    assert state is not RunState.COMPLETED


def test_tool_with_schema_accepts_arguments() -> None:
    executor = _EchoToolExecutor()
    adapter = _ConsumerToolExecutorAdapter(
        executor,
        ("calculate",),
        {
            "calculate": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "additionalProperties": False,
            }
        },
    )
    registry = adapter.build_registry()
    spec = next(s for s in registry.specs if s.name == "calculate")
    assert spec.input_schema["properties"]["expression"] == {"type": "string"}


def test_tool_without_schema_is_noarg_only() -> None:
    executor = _EchoToolExecutor()
    adapter = _ConsumerToolExecutorAdapter(executor, ("calculate",), {})
    registry = adapter.build_registry()
    spec = next(s for s in registry.specs if s.name == "calculate")
    assert spec.input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_legacy_ports_without_new_fields_still_build() -> None:
    db = Path(tempfile.mkdtemp(prefix="consumer-test-")) / "execution.db"
    ports = ConsumerRuntimePorts(
        provider=_calculator_provider("consumer-model"),
        tool_executor=_EchoToolExecutor(),
        authorization=_AllowAll(),
        database_path=str(db),
        tool_names=("calculate",),
        max_turns=10,
        max_tool_calls=20,
        # no model / tool_schemas -> 0.1.2 backward-compat path
    )
    state = _run_once(ports)
    assert state is RunState.COMPLETED
