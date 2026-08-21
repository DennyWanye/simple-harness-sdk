# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Observability regression: key SDK paths must keep emitting structured logs.

Split into three layers (L-AC-6 / L-AC-7):
1. caplog behaviour tests — actually trigger the path and assert the event fires
   (guards FAIL-4: a log point placed on a dead/wrong branch).
2. AST existence tests — assert the key functions still call logger.*
   (guards FAIL-3: someone deleted the log point).
3. redaction assertions — assert sensitive values are not emitted verbatim
   (guards FAIL-2).
"""

from __future__ import annotations

import ast
import logging
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
from simple_harness.execution.budget import (
    BudgetExceededError,
    BudgetPolicy,
    BudgetSnapshot,
    BudgetUnknownError,
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
    RunClient,
    RunStart,
)
from simple_harness.runtime.consumer_adapter import (
    ConsumerRuntimePorts,
    build_consumer_runtime,
)
from simple_harness.tools import ToolCall, ToolResult

_SRC = Path(__file__).resolve().parents[3] / "src" / "simple_harness"


class _AllowAll:
    async def request_authorization(self, request: AuthorizationRequest) -> AuthorizationResult:
        return AuthorizationResult.allow()


class _EchoToolExecutor:
    async def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        return ToolResult.succeeded(call.call_id, {"ok": True})


def _calculator_provider(model: str):
    class _Provider:
        def __init__(self) -> None:
            self.call_count = 0

        async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:
            self.call_count += 1
            if self.call_count == 1:
                return ProviderResponse(
                    request_id=request.request_id,
                    message=Message(MessageRole.ASSISTANT, ""),
                    tool_calls=(ProviderToolCall(CallId("call-1"), "calculate", {}),),
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


def _ports(model: str, provider_model: str, *, db: str) -> ConsumerRuntimePorts:
    return ConsumerRuntimePorts(
        provider=_calculator_provider(provider_model),
        tool_executor=_EchoToolExecutor(),
        authorization=_AllowAll(),
        database_path=db,
        tool_names=("calculate",),
        max_turns=10,
        max_tool_calls=20,
        model=model,
    )


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


# --- behaviour tests ---------------------------------------------------------


def test_run_lifecycle_logs_start_and_complete(caplog) -> None:
    db = Path(tempfile.mkdtemp(prefix="obs-")) / "execution.db"
    caplog.set_level(logging.INFO, logger="simple_harness.runtime.kernel")
    state = _run_once(_ports("demo-model", "demo-model", db=str(db)))
    assert state is RunState.COMPLETED
    assert any(r.message == "run.start" for r in caplog.records)
    assert any(r.message == "run.complete" for r in caplog.records)


def test_provider_usage_untrusted_logged_on_model_mismatch(caplog) -> None:
    db = Path(tempfile.mkdtemp(prefix="obs-")) / "execution.db"
    caplog.set_level(logging.WARNING, logger="simple_harness.execution.dispatch")
    state = _run_once(_ports("demo-model", "other-model", db=str(db)))
    assert state is not RunState.COMPLETED  # mismatch still refuses (FAIL-2)
    assert any(r.message == "provider.usage_untrusted" for r in caplog.records)


def test_tool_logs_invoked_authorized_settled(caplog) -> None:
    db = Path(tempfile.mkdtemp(prefix="obs-")) / "execution.db"
    caplog.set_level(logging.INFO, logger="simple_harness.tools.executor")
    state = _run_once(_ports("demo-model", "demo-model", db=str(db)))
    assert state is RunState.COMPLETED
    assert any(r.message == "tool.invoked" for r in caplog.records)
    assert any(r.message == "tool.authorized" for r in caplog.records)
    assert any(r.message == "tool.effect_settled" for r in caplog.records)


def test_budget_refused_on_unknown_logged(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="simple_harness.execution.budget")
    policy = BudgetPolicy()  # refuse_on_unknown defaults to True
    snapshot = BudgetSnapshot(has_unknown_charge=True)
    with pytest.raises(BudgetUnknownError):
        policy.authorize(snapshot, reservation_micros=None)
    assert any(r.message == "budget.refused_on_unknown" for r in caplog.records)


def test_budget_exceeded_logged(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="simple_harness.execution.budget")
    policy = BudgetPolicy(hard_cap_micros=100)
    snapshot = BudgetSnapshot(committed_micros=100)
    with pytest.raises(BudgetExceededError):
        policy.authorize(snapshot, reservation_micros=1)
    assert any(r.message == "budget.exceeded" for r in caplog.records)


# --- AST existence tests -----------------------------------------------------


def _function_uses_logger(source: Path, func_name: str) -> bool:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "logger"
                ):
                    return True
    return False


@pytest.mark.parametrize(
    "file,func",
    [
        ("runtime/kernel.py", "_terminalize"),
        ("execution/dispatch.py", "_response_charge"),
        ("tools/executor.py", "execute"),
        ("execution/budget.py", "authorize"),
    ],
)
def test_key_path_functions_keep_logging(file: str, func: str) -> None:
    assert _function_uses_logger(_SRC / file, func), f"{file}:{func} lost its logger call"


# --- redaction assertions ----------------------------------------------------


def _logger_call_field_names(source: Path) -> set[str]:
    """Collect every keyword argument name passed to any logger.<level>(...) call."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
            continue
        for kw in node.keywords:
            if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                for k in kw.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        names.add(k.value)
            elif kw.arg:
                names.add(kw.arg)
    return names


@pytest.mark.parametrize(
    "file",
    [
        "runtime/kernel.py",
        "execution/dispatch.py",
        "tools/executor.py",
        "execution/budget.py",
    ],
)
def test_no_sensitive_field_names_in_log_calls(file: str) -> None:
    names = _logger_call_field_names(_SRC / file)
    assert not names & {"api_key", "secret", "prompt", "token", "key"}


def test_tool_args_log_keys_not_values() -> None:
    src = (_SRC / "tools/executor.py").read_text(encoding="utf-8")
    # tool.invoked must log only the argument keys, never the argument dict itself
    assert 'extra={"tool": call.name, "args_keys": list(arguments)[:20]}' in src
