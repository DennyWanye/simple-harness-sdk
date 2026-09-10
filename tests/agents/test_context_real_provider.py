# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 3 · BA13 on a real endpoint: the model's own ``usage.input_tokens`` never exceeds
``B_input`` across a multi-turn session with a small budget.  Opt-in (``--run-real-provider``)."""

from __future__ import annotations

import asyncio
import json

import pytest
from real_provider_config import resolve_real_provider

from simple_harness.agents import AgentConfig, build_agent_runtime
from simple_harness.agents.context import ContextPolicy, TiktokenTokenizer
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import canonical_json
from simple_harness.providers.openai_compatible import OpenAICompatibleProvider

pytestmark = pytest.mark.real_provider

BUDGET = 1_500
PROMPTS = [
    "请用两百字介绍一下长江的地理特征。",
    "接着上面的内容，再补充它对沿岸经济的影响，也写两百字。",
    "把我们刚才讨论过的所有要点列成一个清单。",
    "最后用一句话总结这次对话。",
]


class UsageRecorder:
    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner
        self.usages: list[dict] = []

    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        response = await self._inner.invoke(request, cancel=cancel)
        usage = response.usage
        self.usages.append(
            {
                "request_id": request.request_id.value,
                "messages": len(request.messages),
                "input_tokens": None if usage is None else usage.input_tokens,
                "output_tokens": None if usage is None else usage.output_tokens,
                "finish_reason": response.finish_reason,
            }
        )
        return response


def test_real_model_input_tokens_stay_within_budget(tmp_path):
    config = resolve_real_provider()
    if config is None:
        pytest.skip("no real provider configured")
    tokenizer = TiktokenTokenizer()

    async def case():
        recorder = UsageRecorder(
            OpenAICompatibleProvider(
                base_url=config.base_url, api_key=config.api_key, model=config.model
            )
        )
        policy = ContextPolicy(max_input_tokens=BUDGET, output_reserve=512, safety_margin=64)
        ports = AgentRuntimePorts(
            provider=recorder,
            authorization=AllowAllAuthorization(),
            database_path=str(tmp_path / "real.db"),
            model=config.model,
            owner_id="real-ctx",
            context_policy=policy,
            tokenizer=tokenizer,
            default_max_output_tokens=512,
        )
        async with build_agent_runtime(ports) as runtime:
            agent = await runtime.create(
                AgentConfig(name="w", instructions="你是简洁的中文助手。", model_profile_ref="p"),
                creation_key="real-ctx",
            )
            results = []
            for index, prompt in enumerate(PROMPTS):
                result = await agent.ask(prompt, input_id=f"i{index}", timeout=180)
                results.append(result)
            report = {
                "model": config.model,
                "budget_input_tokens": policy.input_budget(),
                "usages": recorder.usages,
                "states": [r.state.value for r in results],
                "errors": [None if r.error is None else dict(r.error) for r in results],
                "journal_records": len(agent.journal()),
                "selections": [
                    {
                        "revision": s.revision,
                        "message_tokens": s.message_tokens,
                        "dropped_ranges": [list(r) for r in s.dropped_ranges],
                        "request_tokens": s.request_tokens,
                    }
                    for s in [runtime.uow.latest_agent_context_selection(agent.agent_id)]
                    if s is not None
                ],
            }
            (tmp_path / "real-context-report.json").write_text(
                canonical_json(report), encoding="utf-8"
            )
            print("REAL_CONTEXT_REPORT " + json.dumps(report, ensure_ascii=False))
            assert config.api_key not in canonical_json(report)
            assert all(state == "committed" for state in report["states"]), report["errors"]
            for usage in recorder.usages:
                assert usage["input_tokens"] is not None
                assert (
                    usage["input_tokens"] <= policy.input_budget() + policy.render_slack_tokens
                ), usage
            # The later turns must have rotated history out (the budget is small).
            assert report["selections"] and report["selections"][0]["dropped_ranges"]

    asyncio.run(case())
