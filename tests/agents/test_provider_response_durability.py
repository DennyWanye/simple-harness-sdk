# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T10 (AC13 / F-BA-1): an empty final response is a visible failed turn."""

from __future__ import annotations

import asyncio

from provider_fixture import MODEL, ScriptedProvider

from simple_harness import Message, MessageRole
from simple_harness.agents import AgentConfig, AgentTurnState, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.execution.uow import RunState
from simple_harness.providers import ProviderResponse


def _ports(tmp_path, provider, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="durability-owner",
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config():
    return AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")


class EmptyOnceProvider(ScriptedProvider):
    """First call answers with empty content and no tool calls (reasoning-only model)."""

    def __init__(self, script, *, finish_reason="stop"):
        super().__init__(script)
        self.finish_reason = finish_reason
        self.empty_sent = False

    async def invoke(self, request, *, cancel):
        if not self.empty_sent:
            self.empty_sent = True
            self.requests.append(request)
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, ""),
                model=MODEL,
                finish_reason=self.finish_reason,
            )
        return await super().invoke(request, cancel=cancel)


def test_empty_final_response_is_a_visible_failed_turn(tmp_path):
    async def case():
        provider = EmptyOnceProvider(["下一轮正常"], finish_reason="stop")
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="e1")
            failed = await agent.ask("综合一下", input_id="i1", timeout=5)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "provider_empty_response"
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state is RunState.WAITING
            invocations = runtime.uow.database.connection.execute(
                "SELECT state FROM provider_invocations WHERE run_id=?", (agent.run_id,)
            ).fetchall()
            assert [row[0] for row in invocations] == ["failed"]
            ok = await agent.ask("再来", input_id="i2", timeout=5)
            assert ok.state is AgentTurnState.COMMITTED
            assert ok.public_output.content == "下一轮正常"
            assert provider.calls == 2

    asyncio.run(case())


def test_empty_response_with_tool_calls_is_not_treated_as_empty(tmp_path):
    async def case():
        provider = ScriptedProvider([("agent_delegate", {"objective": "x"}), "完成"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(
                AgentConfig(
                    name="w",
                    instructions="你是助手。",
                    model_profile_ref="p",
                    tool_names=("agent_delegate",),
                ),
                creation_key="e2",
            )
            result = await agent.ask("委派", input_id="i1", timeout=10)
            # A tool-call response has empty text by design; it must not fail the turn.
            assert result.error is None or result.error["error_code"] != "provider_empty_response"

    asyncio.run(case())


def test_empty_response_detail_keeps_finish_reason_and_usage(tmp_path):
    """Review F6: the request really completed; what it cost stays in the turn result."""

    from simple_harness.providers import ProviderUsage

    class EmptyWithUsage(EmptyOnceProvider):
        async def invoke(self, request, *, cancel):
            if not self.empty_sent:
                self.empty_sent = True
                self.requests.append(request)
                return ProviderResponse(
                    request.request_id,
                    Message(MessageRole.ASSISTANT, ""),
                    model=MODEL,
                    finish_reason="stop",
                    usage=ProviderUsage(input_tokens=120, output_tokens=4096, total_tokens=4216),
                )
            return await ScriptedProvider.invoke(self, request, cancel=cancel)

    async def case():
        provider = EmptyWithUsage(["好"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="e3")
            failed = await agent.ask("综合", input_id="i1", timeout=5)
            assert failed.state is AgentTurnState.FAILED
            detail = failed.error["detail"]
            assert detail["finish_reason"] == "stop"
            assert detail["usage"]["output_tokens"] == 4096

    asyncio.run(case())


class LengthExhausted(ScriptedProvider):
    """Empty content with finish_reason=length until ``fail_times`` calls have passed."""

    def __init__(self, script, *, fail_times):
        super().__init__(script)
        self.fail_times = fail_times
        self.caps: list[int | None] = []

    async def invoke(self, request, *, cancel):
        from simple_harness.providers import ProviderUsage

        self.caps.append(request.max_output_tokens)
        if len(self.caps) <= self.fail_times:
            self.requests.append(request)
            cap = request.max_output_tokens or 0
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, ""),
                model=MODEL,
                finish_reason="length",
                usage=ProviderUsage(
                    input_tokens=100,
                    output_tokens=cap,
                    total_tokens=100 + cap,
                    reasoning_tokens=cap,
                ),
            )
        return await super().invoke(request, cancel=cancel)


def test_length_exhausted_reasoning_escalates_the_output_cap_then_succeeds(tmp_path):
    """F-BA-1 closure: reasoning ate the cap → retry with a doubled cap inside the turn."""

    async def case():
        provider = LengthExhausted(["终于有正文了"], fail_times=1)
        async with build_agent_runtime(
            _ports(tmp_path, provider, default_max_output_tokens=1024)
        ) as runtime:
            agent = await runtime.create(_config(), creation_key="esc")
            result = await agent.ask("综合一下", input_id="i1", timeout=5)
            assert result.state is AgentTurnState.COMMITTED
            assert result.public_output.content == "终于有正文了"
            assert provider.caps == [1024, 2048]
            invocations = runtime.uow.database.connection.execute(
                "SELECT state FROM provider_invocations WHERE run_id=? ORDER BY invocation_id",
                (agent.run_id,),
            ).fetchall()
            assert sorted(row[0] for row in invocations) == ["failed", "succeeded"]
            assert [r.kind for r in agent.journal()] == ["instructions", "user_input", "assistant"]

    asyncio.run(case())


def test_output_cap_escalation_is_bounded_and_fails_visibly(tmp_path):
    async def case():
        provider = LengthExhausted([], fail_times=10)
        async with build_agent_runtime(
            _ports(
                tmp_path,
                provider,
                default_max_output_tokens=1024,
                empty_response_retries=2,
                max_output_tokens_ceiling=8192,
            )
        ) as runtime:
            agent = await runtime.create(_config(), creation_key="cap")
            failed = await agent.ask("综合一下", input_id="i1", timeout=5)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "provider_empty_response"
            assert provider.caps == [1024, 2048, 4096]
            assert [e["max_output_tokens"] for e in failed.error["output_cap_escalations"]] == [
                2048,
                4096,
            ]
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state is RunState.WAITING
            ok_provider = ScriptedProvider(["下一轮"])
            # The Agent lives on: a later turn with a healthy provider commits.
            runtime._assembled.wire._inner = ok_provider  # type: ignore[attr-defined]
            ok = await agent.ask("再来", input_id="i2", timeout=5)
            assert ok.state is AgentTurnState.COMMITTED

    asyncio.run(case())
