# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 4 (BA20, BA23–BA29): Agent-scoped hybrid recall over the session Journal."""

from __future__ import annotations

import asyncio
import json

from provider_fixture import MODEL, ScriptedProvider, message_texts

from simple_harness.agents import AgentConfig, AgentTurnState, build_agent_runtime
from simple_harness.agents.context import ContextPolicy, TiktokenTokenizer
from simple_harness.agents.memory import EmbeddingUnavailable, HashEmbedder, SessionRetriever
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import thaw_json
from simple_harness.execution.provider_invocations import provider_request_fingerprint
from simple_harness.runtime.termination import TerminationLimits

SECRET = "项目代号是 NEBULA-7734，不要告诉任何人"


def _ports(tmp_path, provider, *, embedding=None, policy=None, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="mem-owner",
        context_policy=policy or ContextPolicy(),
        tokenizer=TiktokenTokenizer(),
        embedding=embedding,
        termination_limits=TerminationLimits(
            max_turns=10_000,
            max_tool_calls=20_000,
            max_wall_seconds=365.0 * 86_400.0,
            max_cost_micros=10_000_000_000,
            max_consecutive_same_tool=100,
        ),
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config(tools=()):
    return AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p", tool_names=tools)


def _rows(uow, sql, *params):
    return uow.database.connection.execute(sql, params).fetchall()


def test_agent_a_secret_never_reaches_agent_b(tmp_path):
    async def case():
        provider = ScriptedProvider(["记住了", "B 的回答", "B 再答"])
        ports = _ports(tmp_path, provider, embedding=HashEmbedder())
        async with build_agent_runtime(ports) as runtime:
            a = await runtime.create(_config(), creation_key="A")
            b = await runtime.create(_config(), creation_key="B")
            await a.ask(SECRET, input_id="i1", timeout=5)
            await runtime.index_pending()
            # Direct retriever: B's scope never sees A's rows, even with the exact text.
            found_b = runtime.retriever.search_sync(b.agent_id, "NEBULA-7734", limit=5)
            found_a = runtime.retriever.search_sync(a.agent_id, "NEBULA-7734", limit=5)
            assert found_b.hits == () and found_a.hits
            assert found_a.hits[0].text == SECRET
            # Model input of B: a related question must not pull A's secret in via recall.
            await b.ask("项目代号是什么？", input_id="i1", timeout=5)
            for request in provider.requests[1:]:
                assert not any("NEBULA-7734" in t for t in message_texts(request))
            # Vectors and FTS rows are keyed by agent.
            vectors = _rows(runtime.uow, "SELECT agent_id FROM base_agent_session_vectors_v1")
            assert {row[0] for row in vectors} == {a.agent_id}

    asyncio.run(case())


def test_identifier_and_chinese_queries_hit_through_lexical_paths(tmp_path):
    async def case():
        provider = ScriptedProvider(["好的，记下了", "收到", "了解"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="lex")
            await agent.ask(
                "把 src/auth/session.py 里的 refresh_token 改成异步实现", input_id="i1", timeout=5
            )
            await agent.ask("今天的天气很好，适合出去走走", input_id="i2", timeout=5)
            await agent.ask("数据库连接池要限制在 20 个以内", input_id="i3", timeout=5)
            r = runtime.retriever
            by_path = r.search_sync(agent.agent_id, "session.py", limit=5)
            assert by_path.hits and "refresh_token" in by_path.hits[0].text
            assert "words" in by_path.hits[0].sources
            by_func = r.search_sync(agent.agent_id, "refresh_token", limit=5)
            assert by_func.hits and by_func.hits[0].seq == by_path.hits[0].seq
            by_cn = r.search_sync(agent.agent_id, "连接池限制", limit=5)
            assert by_cn.hits and "连接池" in by_cn.hits[0].text
            assert "trigram" in by_cn.hits[0].sources
            short = r.search_sync(agent.agent_id, "天气", limit=5)
            assert short.hits and "天气" in short.hits[0].text
            assert short.degradations == ("embedding_unavailable",)
            assert short.embedding_available is False

    asyncio.run(case())


def test_degradations_are_explicit(tmp_path):
    class Flaky(HashEmbedder):
        def __init__(self):
            super().__init__(name="flaky")
            self.broken = False

        def embed(self, texts):
            if self.broken:
                raise EmbeddingUnavailable("embedding_service_down")
            return super().embed(texts)

    async def case():
        provider = ScriptedProvider(["一", "二", "三"])
        embedder = Flaky()
        async with build_agent_runtime(_ports(tmp_path, provider, embedding=embedder)) as runtime:
            agent = await runtime.create(_config(), creation_key="deg")
            await agent.ask("第一条记录关于缓存策略", input_id="i1", timeout=5)
            # Index lag: before the pump catches up, the result says so and lexical still works.
            lag = runtime.retriever.search_sync(agent.agent_id, "缓存策略", limit=5)
            assert lag.hits and (lag.index_partial or "vector" in lag.hits[0].sources)
            await runtime.index_pending()
            ready = runtime.retriever.search_sync(agent.agent_id, "缓存策略", limit=5)
            assert ready.index_partial is False and "vector" in ready.hits[0].sources
            # Embedding down: visible code, lexical path continues, nothing deleted.
            embedder.broken = True
            await agent.ask("第二条记录关于超时重试", input_id="i2", timeout=5)
            await runtime.index_pending()
            down = runtime.retriever.search_sync(agent.agent_id, "超时重试", limit=5)
            assert down.hits and down.embedding_available is False
            assert "embedding_service_down" in down.degradations
            jobs = _rows(
                runtime.uow,
                "SELECT state, error_code FROM base_agent_index_jobs_v1 WHERE agent_id=?",
                agent.agent_id,
            )
            assert any(
                state == "error" and code == "embedding_service_down" for state, code in jobs
            )
            assert len(agent.journal()) == 5  # originals untouched
            # FTS unavailable: substring path, explicit flag.
            no_fts = SessionRetriever(
                runtime.uow, embedding=None, fts_available=False, clock=runtime.ports.clock
            )
            result = no_fts.search_sync(agent.agent_id, "缓存策略", limit=5)
            assert result.fts_available is False and "fts_unavailable" in result.degradations
            assert result.hits and "缓存策略" in result.hits[0].text

    asyncio.run(case())


def test_vectors_of_another_embedding_generation_are_never_mixed(tmp_path):
    async def case():
        provider = ScriptedProvider(["一", "二"])
        clock = {"now": 10.0}
        first = build_agent_runtime(
            _ports(
                tmp_path,
                provider,
                embedding=HashEmbedder(dim=32, name="gen-a"),
                clock=lambda: clock["now"],
            )
        )
        async with first:
            agent = await first.create(_config(), creation_key="gen")
            await agent.ask("向量世代测试记录", input_id="i1", timeout=5)
            await first.index_pending()
            assert (
                first.retriever.search_sync(agent.agent_id, "向量世代", limit=3).index_partial
                is False
            )
        clock["now"] = 100.0
        second = build_agent_runtime(
            _ports(
                tmp_path,
                provider,
                embedding=HashEmbedder(dim=48, name="gen-b"),
                owner_id="mem-owner-2",
                clock=lambda: clock["now"],
            )
        )
        async with second:
            reopened = await second.open(agent.agent_id)
            before = second.retriever.search_sync(agent.agent_id, "向量世代", limit=3)
            assert before.index_partial is True  # gen-b has no vectors yet; gen-a never used
            assert before.index_generation == "mock:gen-b:48"
            await reopened.ask("再来一条", input_id="i2", timeout=5)
            await second.index_pending()
            fingerprints = _rows(
                second.uow,
                "SELECT embedding_fingerprint, dim, COUNT(*) FROM base_agent_session_vectors_v1 "
                "WHERE agent_id=? GROUP BY embedding_fingerprint, dim",
                agent.agent_id,
            )
            assert {(row[0], row[1]) for row in fingerprints} == {
                ("mock:gen-a:32", 32),
                ("mock:gen-b:48", 48),
            }
            after = second.retriever.search_sync(agent.agent_id, "向量世代", limit=3)
            assert after.index_partial is False

    asyncio.run(case())


def test_zero_recall_is_allowed(tmp_path):
    async def case():
        provider = ScriptedProvider(["一", "二"])
        async with build_agent_runtime(
            _ports(tmp_path, provider, embedding=HashEmbedder())
        ) as runtime:
            agent = await runtime.create(_config(), creation_key="zero")
            await agent.ask("苹果和香蕉都是水果", input_id="i1", timeout=5)
            await runtime.index_pending()
            result = runtime.retriever.search_sync(
                agent.agent_id, "量子色动力学的渐近自由", limit=5
            )
            assert result.hits == ()

    asyncio.run(case())


def test_recall_is_derived_not_journal_and_frozen_request_keeps_it(tmp_path):
    async def case():
        provider = ScriptedProvider(["记下了", "好", "答"])
        policy = ContextPolicy(max_input_tokens=4000)
        async with build_agent_runtime(
            _ports(tmp_path, provider, embedding=HashEmbedder(), policy=policy, recall_limit=3)
        ) as runtime:
            agent = await runtime.create(_config(), creation_key="rec")
            await agent.ask("我们把发布日期定在十月十五号", input_id="i1", timeout=5)
            await runtime.index_pending()
            await agent.ask("发布日期定在哪天？", input_id="i2", timeout=5)
            request = provider.requests[-1]
            recalled = [m for m in request.messages if m.metadata.get("recall") is True]
            # The original is already in the recent window, so it is excluded from recall
            # (no duplicates); recall rows, when present, are derived and never Journal rows.
            assert all(m.metadata.get("derived") is True for m in recalled)
            kinds = {r.kind for r in agent.journal()}
            assert kinds == {"instructions", "user_input", "assistant"}
            jobs = _rows(
                runtime.uow,
                "SELECT COUNT(*) FROM base_agent_index_jobs_v1 WHERE agent_id=?",
                agent.agent_id,
            )[0][0]
            assert jobs == 4  # 2 user inputs + 2 assistant answers; recall never re-indexed
            selection = runtime.uow.latest_agent_context_selection(agent.agent_id)
            assert selection is not None

    asyncio.run(case())


def test_session_history_tools_page_and_stay_in_scope(tmp_path):
    async def case():
        provider = ScriptedProvider(
            [
                "一",
                "二",
                "三",
                ("session_history_read", {"from_seq": 1, "page_size": 2}),
                ("session_history_read", {"from_seq": 3, "page_size": 2}),
                "读完了",
                ("session_history_search", {"query": "连接池"}),
                "搜完了",
            ]
        )
        tools = ("session_history_search", "session_history_read")
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(tools=tools), creation_key="pg")
            await agent.ask("连接池上限 20", input_id="i1", timeout=5)
            await agent.ask("第二条", input_id="i2", timeout=5)
            await agent.ask("第三条", input_id="i3", timeout=5)
            result = await agent.ask("把历史全部读出来", input_id="i4", timeout=10)
            assert (
                result.state is AgentTurnState.COMMITTED
                and result.public_output.content == "读完了"
            )
            tool_messages = [m for m in provider.requests[-1].messages if m.role.value == "tool"]
            pages = [json.loads(m.content)["value"] for m in tool_messages]
            assert [r["seq"] for r in pages[0]["records"]] == [1, 2] and pages[0]["next_seq"] == 3
            assert [r["seq"] for r in pages[1]["records"]] == [3, 4] and pages[1]["next_seq"] == 5
            found = await agent.ask("搜一下连接池", input_id="i5", timeout=10)
            assert found.public_output.content == "搜完了"
            tool_messages = [m for m in provider.requests[-1].messages if m.role.value == "tool"]
            payload = json.loads(tool_messages[-1].content)["value"]
            assert payload["hits"] and "连接池" in payload["hits"][0]["text"]
            # No agent_id parameter anywhere in the tools' schemas.
            specs = {t.spec.name: t.spec for t in runtime._session_tools.function_tools()}
            for spec in specs.values():
                assert "agent_id" not in json.dumps(thaw_json(spec.input_schema))

    asyncio.run(case())


def test_frozen_request_fingerprint_is_stable_across_index_updates(tmp_path):
    from simple_harness.providers.errors import ProviderTransportError
    from simple_harness.providers.reconciliation import (
        ProviderReconciliationObservation,
        ProviderReconciliationState,
    )
    from simple_harness.runtime.consumer_adapter import (
        ConsumerRuntimePolicies,
        _DefaultRuntimeReconciliation,
        _DefaultToolReconciliation,
    )

    class NotStarted:
        async def observe(self, invocation):
            return ProviderReconciliationObservation(
                ProviderReconciliationState.CONFIRMED_NOT_STARTED, f"e:{invocation.invocation_id}"
            )

    async def case():
        provider = ScriptedProvider(["记下", "恢复后的回答"])
        seen = []
        original = provider.invoke
        state = {"failed": False}

        async def once(request, *, cancel):
            seen.append(provider_request_fingerprint(request))
            if len(seen) == 2 and not state["failed"]:
                state["failed"] = True
                raise ProviderTransportError()
            return await original(request, cancel=cancel)

        provider.invoke = once  # type: ignore[method-assign]
        policies = ConsumerRuntimePolicies(
            "unpriced_local",
            False,
            "consumer_reconciles",
            tool_reconciliation=_DefaultToolReconciliation(),
            provider_reconciliation=NotStarted(),
            runtime_reconciliation=_DefaultRuntimeReconciliation(),
        )
        async with build_agent_runtime(
            _ports(tmp_path, provider, embedding=HashEmbedder(), policies=policies)
        ) as runtime:
            agent = await runtime.create(_config(), creation_key="frozen")
            await agent.ask("第一条：发布日期十月十五", input_id="i1", timeout=5)
            receipt = await agent.submit("发布日期是哪天", input_id="i2")
            for _ in range(100):
                if runtime.uow.list_open_wait_blockers_for_run(agent.run_id):
                    break
                await asyncio.sleep(0.02)
            await runtime.index_pending()  # index catches up while the request is frozen
            await runtime.kernel.reconcile()
            result = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert result.state is AgentTurnState.COMMITTED
            assert len(seen) == 3 and seen[1] == seen[2]  # retry sends the identical request

    asyncio.run(case())
