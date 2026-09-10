# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T10 真实 provider 复现（证据，不作为 MUST 判定）。

Opt in with ``--run-real-provider``; skipped when no endpoint is configured.  The
report of this file is kept separate from the mock report (BA40).
"""

from __future__ import annotations

import asyncio
import secrets

import pytest
from real_provider_config import build_real_provider, resolve_real_provider

from simple_harness import canonical_json
from simple_harness.agents import AgentConfig, AgentLimits, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.tools.delegate import DELEGATE_TOOL_NAME, child_agent_id_for
from simple_harness.execution.uow import RunState

pytestmark = pytest.mark.real_provider


class CountingProvider:
    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner
        self.requests = []
        self.responses = []

    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        response = await self._inner.invoke(request, cancel=cancel)
        self.responses.append(response)
        return response


def test_main_agent_delegates_to_a_child_with_a_real_model(tmp_path):
    config = resolve_real_provider()
    if config is None:
        pytest.skip("no real provider configured (SH_BASEURL/SH_APIKEY or Host .env)")
    nonce = secrets.token_hex(6)
    provider = CountingProvider(build_real_provider(config))
    ports = AgentRuntimePorts(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "real.db"),
        model=config.model,
        owner_id="real-provider-owner",
        child_instructions_template=(
            "你是被委派的工作 Agent。用不超过 120 字完成交给你的目标，"
            f"并且必须在结论的最后一行原样写出：验证码 {nonce}"
        ),
    )
    main_config = AgentConfig(
        name="main",
        instructions=(
            "你是主 Agent。收到任务后必须先调用工具 agent_delegate 把整个任务委派出去"
            "（delegation_id 用 d-1），等它返回后，用一两句话综合它的结论作答，"
            "并把它结论里的验证码一字不改地放在你回答的最后一行。不要自己编造验证码。"
        ),
        model_profile_ref=config.model,
        tool_names=(DELEGATE_TOOL_NAME,),
        limits=AgentLimits(max_delegations_per_turn=1, delegation_wait_seconds=170.0),
    )

    async def case():
        async with build_agent_runtime(ports) as runtime:
            main = await runtime.create(main_config, creation_key="main")
            result = await main.ask(
                "请比较用 SQLite 和用 PostgreSQL 保存单机桌面应用的会话记录各自的优缺点，"
                "给出推荐。",
                input_id="req-1",
                timeout=200,
            )
            uow = runtime.uow
            child_id = child_agent_id_for(main.agent_id, "d-1")
            child_result = uow.read_agent_turn_result(f"{child_id}:input:objective")
            delegation = uow.read_agent_delegation("d-1")
            child_run = uow.read_run(child_id)
            report = {
                "model": config.model,
                "provider_calls": len(provider.requests),
                "main_state": result.state.value,
                "delegation_count": result.delegation_count,
                "delegation_state": None if delegation is None else delegation.state,
                "child_run_state": None if child_run is None else child_run.state.value,
                "nonce_in_child_result": (
                    child_result is not None
                    and nonce
                    in canonical_json(
                        __import__("simple_harness").thaw_json(child_result.result_json)
                    )
                ),
                "nonce_in_final_answer": (
                    result.public_output is not None
                    and isinstance(result.public_output.content, str)
                    and nonce in result.public_output.content
                ),
                "final_answer": (
                    result.public_output.content if result.public_output is not None else None
                ),
            }
            (tmp_path / "real-provider-report.json").write_text(
                canonical_json(report), encoding="utf-8"
            )
            print("REAL_PROVIDER_REPORT " + canonical_json(report))
            # Key hygiene: the secret never appears in durable rows or the answer.
            rows = uow.database.connection.execute("SELECT payload_json FROM run_events").fetchall()
            assert all(config.api_key not in str(row[0]) for row in rows)
            assert config.api_key not in canonical_json(report)
            # Evidence assertions (AC1–AC3 real-model reproduction).
            assert result.state.value == "committed"
            assert delegation is not None and delegation.state == "settled"
            assert child_run is not None and child_run.state is RunState.WAITING
            assert report["nonce_in_child_result"]
            assert report["nonce_in_final_answer"], report["final_answer"]
            assert len(provider.requests) >= 3

    asyncio.run(case())
