# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Real HTTP adapter and AgentTurn: bounded recovery of confirmed truncated tools."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext

import httpx
import pytest

from simple_harness.agents import (
    AgentConfig,
    AgentLimits,
    AgentRuntimePorts,
    AgentTurnState,
    build_agent_runtime,
)
from simple_harness.agents.ports import AllowAllAuthorization
from simple_harness.contracts import RunId
from simple_harness.execution.provider_admission import (
    ProviderAdmissionDenied,
    ProviderAdmissionFailure,
    ProviderAdmissionTicket,
)
from simple_harness.providers import OpenAICompatibleProvider, Secret
from simple_harness.tools import ToolResult


class RecordingAdmission:
    """A deterministic external budget authority; the SDK must still consult it."""

    fingerprint = "length-test-budget-v1"

    def __init__(self, remaining=100_000):
        self.remaining = remaining
        self.caps = []
        self.settled = []

    def waiting_for_slot(self, **kwargs):
        return False

    async def acquire(self, *, request, record, **kwargs):
        self.caps.append(request.max_output_tokens)
        needed = 100 + request.max_output_tokens
        if needed > self.remaining:
            raise ProviderAdmissionDenied(
                admission_detail=ProviderAdmissionFailure(
                    reason_code="budget_exhausted",
                    dimension="tokens",
                    requested=needed,
                    remaining=self.remaining,
                )
            )
        self.remaining -= needed
        return ProviderAdmissionTicket(
            record.invocation_id,
            record.handoff_attempt + 1,
            record.request_fingerprint,
            self.fingerprint,
        )

    def handoff(self, ticket, **kwargs):
        return nullcontext()

    def observe(self, ticket, *, record):
        if record is not None:
            self.settled.append((record.invocation_id, str(record.state)))

    def recover(self, uow):
        pass


class RecordingTool:
    def __init__(self):
        self.calls = []

    async def execute(self, call, context):
        self.calls.append(call)
        return ToolResult.succeeded(call.call_id, {"ok": True})


async def exercise(
    tmp_path,
    *,
    reason="length",
    usage="valid",
    fail_times=1,
    retries=2,
    ceiling=32768,
    stop=None,
    remaining=100_000,
    malformed_stage="tool_parse",
    max_calls=32,
):
    caps, ids, runtime_ref = [], [], {}
    sent_messages = []
    clock = {"now": 10.0}
    tools, admission = RecordingTool(), RecordingAdmission(remaining)

    async def transport(request):
        body = json.loads(request.content)
        caps.append(body["max_tokens"])
        sent_messages.append(body["messages"])
        n = len(caps)
        if n <= fail_times:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "partial",
                        "type": "function",
                        "function": {"name": "probe", "arguments": '{"path":"unfinished'},
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": "complete"}
        payload = {
            "id": f"response-{n}",
            "model": "deepseek-flash",
            "choices": [{"message": message, "finish_reason": reason}],
        }
        if malformed_stage == "response_shape" and n <= fail_times:
            payload["choices"] = []
        if usage is not None:
            payload["usage"] = {
                "prompt_tokens": 100,
                "completion_tokens": caps[-1],
                "total_tokens": 100 + caps[-1],
            }
            if usage == "invalid":
                payload["usage"]["prompt_tokens"] = True
        if stop == "cancel" and n == 1:
            agent = runtime_ref["agent"]
            await runtime_ref["runtime"].cancel_turn(
                agent.agent_id,
                agent.turn_id_for("input"),
                command_id="stop",
                wait_timeout=0,
            )
        if stop == "deadline" and n == 1:
            clock["now"] += 3.0
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = OpenAICompatibleProvider(
            client, "https://length.invalid/v1", "deepseek-flash", Secret("fixture-only")
        )
        ports = AgentRuntimePorts(
            provider=provider,
            authorization=AllowAllAuthorization(),
            database_path=str(tmp_path / "execution.db"),
            model="deepseek-flash",
            provider_admission=admission,
            tool_executor=tools,
            tool_names=("probe",),
            tool_schemas={"probe": {"type": "object", "properties": {"path": {"type": "string"}}}},
            default_max_output_tokens=8192,
            max_output_tokens_ceiling=ceiling,
            empty_response_retries=retries,
            **({"clock": lambda: clock["now"]} if stop == "deadline" else {}),
        )
        async with build_agent_runtime(ports) as runtime:
            agent = await runtime.create(
                AgentConfig(
                    name="length",
                    instructions="Use probe.",
                    model_profile_ref="default",
                    limits=AgentLimits(
                        max_model_calls_per_turn=max_calls,
                        turn_deadline_seconds=2 if stop == "deadline" else 900,
                    ),
                ),
                creation_key="length",
            )
            runtime_ref.update(runtime=runtime, agent=agent)
            result = await agent.ask("Read the file", input_id="input", timeout=10)
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            ids = [r.request_id for r in records]
            before = [(r.invocation_id, r.version, r.usage_json, r.state) for r in records]
            receipt = await agent.submit("Read the file", input_id="input")
            repeated = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert repeated == result
            await runtime.kernel.reconcile()
            after = [
                (r.invocation_id, r.version, r.usage_json, r.state)
                for r in runtime.uow.list_provider_invocations(RunId(agent.run_id))
            ]
            assert before == after
            assert len(ids) == len(set(ids))
            assert all(r.rehandoff_count == 0 for r in records)
            assert not tools.calls  # No malformed call ever becomes a tool invocation.
        # A new runtime owner reads the same completed/failed AgentTurn without
        # reconstructing a failed request, retrying it, or adding billing records.
        count_before = len(caps)
        async with build_agent_runtime(ports) as reopened:
            cold = await reopened.create(agent.config, creation_key="length")
            cached = await cold.ask("Read the file", input_id="input", timeout=10)
            assert cached == result
            assert reopened.uow.list_provider_invocations(RunId(cold.run_id)) == records
            assert len(caps) == count_before
        assert all(messages == sent_messages[0] for messages in sent_messages)
        return result, caps, records, admission


def test_confirmed_tool_truncation_grows_cap_preserving_original_failure(tmp_path):
    result, caps, records, admission = asyncio.run(exercise(tmp_path))
    assert result.state is AgentTurnState.COMMITTED, result.error
    assert caps == [8192, 16384]
    assert admission.caps == caps
    assert [str(r.state) for r in records] == ["failed", "succeeded"]
    assert records[0].error_code == "provider_protocol_error"
    assert records[0].usage_json["usage"]["total_tokens"] == 8292
    assert records[0].response_json is None
    assert result.public_output.content == "complete"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reason": "stop"},
        {"reason": None},
        {"usage": None},
        {"usage": "invalid"},
        {"malformed_stage": "response_shape"},
        {"retries": 0},
        {"ceiling": 8192},
    ],
)
def test_unconfirmed_or_disallowed_growth_never_resamples(tmp_path, kwargs):
    result, caps, records, _ = asyncio.run(exercise(tmp_path, **kwargs))
    assert result.state is AgentTurnState.FAILED
    assert caps == [8192]
    assert records[0].error_code == "provider_protocol_error"
    if kwargs.get("usage", "valid") != "valid":
        assert not records[0].usage_json.get("usage")


def test_truncation_stops_at_original_retry_and_output_ceiling(tmp_path):
    result, caps, records, _ = asyncio.run(exercise(tmp_path, fail_times=10))
    assert result.state is AgentTurnState.FAILED
    assert caps == [8192, 16384, 32768]
    assert len(records) == 3 and all(str(r.state) == "failed" for r in records)
    assert sum(r.usage_json["usage"]["total_tokens"] for r in records) == sum(caps) + 300


def test_cancel_after_truncated_response_prevents_next_http_call(tmp_path):
    result, caps, records, _ = asyncio.run(exercise(tmp_path, stop="cancel"))
    assert result.state is AgentTurnState.FAILED
    assert result.error["error_code"] == "agent_turn_cancelled"
    assert caps == [8192]


def test_larger_output_still_requires_new_budget_admission(tmp_path):
    result, caps, records, admission = asyncio.run(exercise(tmp_path, remaining=20_000))
    assert result.state is AgentTurnState.FAILED
    assert result.error["error_code"] == "provider_admission_denied"
    assert admission.caps == [8192, 16384]
    assert caps == [8192]
    assert records[0].usage_json["usage"]["total_tokens"] == 8292


def test_original_turn_deadline_prevents_resampling(tmp_path):
    result, caps, records, _ = asyncio.run(exercise(tmp_path, stop="deadline"))
    assert result.state is AgentTurnState.FAILED
    assert result.error["error_code"] == "react_wall_clock_exceeded"
    assert caps == [8192]
    assert records[0].usage_json["usage"]["total_tokens"] == 8292


def test_failed_invocation_still_counts_toward_turn_model_limit(tmp_path):
    result, caps, records, _ = asyncio.run(exercise(tmp_path, max_calls=1))
    assert result.state is AgentTurnState.FAILED
    assert caps == [8192]
    assert records[0].usage_json["usage"]["total_tokens"] == 8292


def test_non_length_tool_reason_survives_failed_turn_and_cold_read(tmp_path):
    result, caps, records, _ = asyncio.run(exercise(tmp_path, reason="tool_calls"))
    assert result.state is AgentTurnState.FAILED
    assert caps == [8192]
    assert result.error["detail"]["finish_reason"] == "tool_calls"
    assert result.error["detail"]["parse_stage"] == "tool_parse"
    assert result.error["detail"]["tool_parse_reason"] == "arguments_json"
    assert records[0].usage_json["usage"]["total_tokens"] == 8292
    assert records[0].rehandoff_count == 0
