"""Frozen H8 context must detect changing the physical response transport."""
import asyncio
from types import SimpleNamespace

import httpx

from agent_orchestrator.evaluation.htn_identity import context_profile_identity
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.providers import OpenAICompatibleProvider, Secret


def test_stream_transport_change_cannot_reuse_a_nonstream_context_identity():
    async def check():
        async with httpx.AsyncClient() as client:
            common = (client, "https://fixture.invalid/v1", "fixture", Secret("secret"))
            legacy = OpenAICompatibleProvider(*common)
            streaming = OpenAICompatibleProvider(*common, stream=True)
            tokenizer = SimpleNamespace(fingerprint="frozen-tokenizer")
            policy = ContextPolicy(max_input_tokens=8192, output_reserve=512)
            assert legacy.target.provider_id == streaming.target.provider_id
            assert legacy.target.model == streaming.target.model
            assert context_profile_identity(tokenizer, policy, legacy, 0) != (
                context_profile_identity(tokenizer, policy, streaming, 0)
            )
    asyncio.run(check())
