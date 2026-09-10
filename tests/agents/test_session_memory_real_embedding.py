# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 4 · BA24 with a real embedding model (WeMM-Embedding-2B via the Host venv).
Opt-in: ``--run-real-embedding``."""

from __future__ import annotations

import asyncio
import json

import pytest
from embedding_fixture import HostVenvWemmEmbedder, available
from provider_fixture import MODEL, ScriptedProvider

from simple_harness.agents import AgentConfig, build_agent_runtime
from simple_harness.agents.context import ContextPolicy, TiktokenTokenizer
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import canonical_json

pytestmark = pytest.mark.real_embedding

RECORDS = [
    "请把 src/auth/session.py 里的 refresh_token 改成异步实现，并加超时。",
    "我们把发布日期定在十月十五号，届时要冻结功能。",
    "数据库连接池上限设为 20，超过就排队。",
    "今天午饭吃了牛肉面，味道不错。",
]


def test_chinese_paraphrase_and_english_identifier_queries(tmp_path):
    if not available():
        pytest.skip("Host venv / WeMM model not present")
    embedder = HostVenvWemmEmbedder()

    async def case():
        provider = ScriptedProvider(["记下了"] * len(RECORDS))
        ports = AgentRuntimePorts(
            provider=provider,
            authorization=AllowAllAuthorization(),
            database_path=str(tmp_path / "runtime.db"),
            model=MODEL,
            owner_id="real-emb",
            context_policy=ContextPolicy(),
            tokenizer=TiktokenTokenizer(),
            embedding=embedder,
        )
        async with build_agent_runtime(ports) as runtime:
            agent = await runtime.create(
                AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p"),
                creation_key="real",
            )
            for index, text in enumerate(RECORDS):
                await agent.ask(text, input_id=f"i{index}", timeout=10)
            await runtime.index_pending()
            r = runtime.retriever
            # Chinese semantic paraphrase: no shared trigram with the record.
            paraphrase = r.search_sync(agent.agent_id, "什么时候上线？功能冻结的截止时间", limit=3)
            # English function / path query.
            english = r.search_sync(
                agent.agent_id, "make refresh_token async in auth/session.py", limit=3
            )
            report = {
                "embedding": embedder.fingerprint,
                "dim": embedder.dim,
                "paraphrase": paraphrase.to_json(),
                "english": english.to_json(),
                "index_status": runtime.uow.agent_index_status(
                    agent_id=agent.agent_id, embedding_fingerprint=embedder.fingerprint
                ),
            }
            print("REAL_EMBEDDING_REPORT " + json.dumps(report, ensure_ascii=False))
            (tmp_path / "real-embedding-report.json").write_text(
                canonical_json(report), encoding="utf-8"
            )
            assert paraphrase.index_partial is False
            assert paraphrase.hits and "十月十五" in paraphrase.hits[0].text
            assert "vector" in paraphrase.hits[0].sources
            assert english.hits and "refresh_token" in english.hits[0].text
            assert {"words", "vector"} & set(english.hits[0].sources)

    asyncio.run(case())
