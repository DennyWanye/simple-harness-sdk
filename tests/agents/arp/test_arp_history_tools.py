"""Model-side ``session_history_search`` / ``session_history_read`` on the native plane:
the same frozen-index engine as the automatic recall under the MODEL_SEARCH / MODEL_READ
purposes, paging by cursor, byte-capped pages, cursor/request binding, and no Session id
in any request."""

from __future__ import annotations

import asyncio

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.strict import digest
from simple_harness.agents.arp.strict import plain
from simple_harness.agents.arp.tools import READ_TOOL_NAME, SEARCH_TOOL_NAME
from simple_harness.agents.memory.embedding import HashEmbedder
from simple_harness.contracts import CallId, RequestId, RunId
from simple_harness.tools import CancellationToken, ToolContext

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")
SECRET = "工程暗号 是 蓝鲸七号，请牢记。"
FILLERS = [f"第{i}条闲聊：今天天气不错，我们聊聊别的话题 {i}。" for i in range(6)]


async def _seed(runtime):  # type: ignore[no-untyped-def]
    agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
    for i, text in enumerate([SECRET, *FILLERS]):
        receipt = await agent.submit(text, input_id=f"i{i}")
        await agent.wait_turn(receipt.turn_id, timeout=10)
    return agent


def _tools(runtime):  # type: ignore[no-untyped-def]
    by_name = {t.spec.name: t for t in runtime._session_tools.function_tools()}
    return by_name[SEARCH_TOOL_NAME], by_name[READ_TOOL_NAME]


def _context(agent, n: int) -> ToolContext:  # type: ignore[no-untyped-def]
    return ToolContext(RunId(agent.run_id), RequestId(f"{agent.run_id}:tool:{n}"), CancellationToken(), call_id=CallId(f"call-{n}"))


@pytest.mark.parametrize("embedding", [None, HashEmbedder(dim=32)], ids=["lexical", "hybrid-mock-embedding"])
def test_model_search_pages_a_frozen_query_to_the_secret(tmp_path, embedding) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["好的。"] * (len(FILLERS) + 1))
        runtime = build(tmp_path, provider, embedding=embedding)
        async with runtime:
            agent = await _seed(runtime)
            runtime.arp.tick()  # closed groups of the last Turn are indexed by the tick, not by a prepare
            search, _read = _tools(runtime)
            pages = []
            arguments = {"query": "工程暗号", "limit": 4, "max_bytes": 8192}
            cursor = None
            for n in range(64):
                result = await search.invoke({**arguments, **({"cursor": cursor} if cursor else {})}, _context(agent, n))
                assert result.outcome.value == "succeeded", result
                page = plain(result.value)
                pages.append(page)
                assert page["cursor_purpose"] == "MODEL_SEARCH"
                assert page["body_bytes"] <= arguments["max_bytes"]
                if page["page_semantics"] == "PROGRESS":
                    assert page["items"] == [] and page["has_more"] and page["next_cursor"]
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            assert pages[0]["page_semantics"] == "PROGRESS", "scanning progress is not an empty hit"
            final = [p for p in pages if p["page_semantics"] == "APPEND_FINAL"]
            assert final and final[-1]["has_more"] is False and final[-1]["next_cursor"] is None
            assert final[0]["receipt"]["coverage"]["phase"] == "RESULTS" and final[0]["receipt"]["coverage"]["ranking_final"]
            assert final[0]["receipt"]["coverage"]["mode"] == ("HYBRID" if embedding is not None else "LEXICAL_ONLY")
            items = [i for p in final for i in p["items"]]
            assert items and all(len(p["items"]) <= 4 for p in final)
            assert [i["rank_ordinal"] for i in items] == list(range(1, len(items) + 1))
            connection = runtime.uow.database.connection
            session = store.read_live_session(connection, agent.agent_id)
            service = runtime.arp.search_for(session)[0]
            texts = [service.chunk_text(final[0]["receipt"]["index_generation"], i["chunk_id"]) for i in items]
            assert any("蓝鲸七号" in t for t in texts)
            # The model tool never wrote an internal recall row or a CONTEXT_RECALL cursor.
            assert store.list_pending_recalls(connection) == ()
            assert runtime._session_tools.searches == len(pages)
            # A cursor re-sent with another limit is refused; the same request replays the page.
            bad = await search.invoke({**arguments, "limit": 3, "cursor": pages[0]["next_cursor"]}, _context(agent, 99))
            assert bad.outcome.value != "succeeded" and bad.error_code == "session_history_cursor_request_mismatch"
            again = await search.invoke({**arguments, "cursor": pages[0]["next_cursor"]}, _context(agent, 100))
            assert plain(again.value) == pages[1]

    asyncio.run(case())


def test_model_read_pages_the_exact_journal_and_caps_bytes(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["好的。"] * (len(FILLERS) + 1))
        runtime = build(tmp_path, provider)
        async with runtime:
            agent = await _seed(runtime)
            _search, read = _tools(runtime)
            highwater = runtime.uow.agent_journal_highwater(agent.agent_id)
            seen: list[int] = []
            cursor = None
            pages = 0
            for n in range(64):
                arguments = {"seq_from": 1, "seq_to": highwater, "max_bytes": 2048}
                if cursor:
                    arguments["cursor"] = cursor
                result = await read.invoke(arguments, _context(agent, n))
                assert result.outcome.value == "succeeded", result
                page = plain(result.value)
                pages += 1
                assert page["cursor_purpose"] == "MODEL_READ" and page["body_bytes"] <= 2048
                seen.extend(i["seq"] for i in page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    assert page["has_more"] is False
                    break
            assert pages >= 3 and seen == sorted(seen)
            assert set(seen) == set(range(1, highwater + 1)) - {s for r in page["hidden_seq_ranges"] for s in range(r["start"], r["end"] + 1)}
            texts = "".join(i["text"] for i in (await read.invoke({"seq_from": 1, "seq_to": 2, "max_bytes": 65536}, _context(agent, 200))).value["items"])
            assert "蓝鲸七号" in texts
            # Out of the frozen range: a named refusal, never an empty page.
            beyond = await read.invoke({"seq_from": highwater + 5, "seq_to": highwater + 9}, _context(agent, 201))
            assert beyond.outcome.value != "succeeded" and beyond.error_code == "session_history_history_byte_range_invalid"
            assert runtime._session_tools.reads == pages + 2

    asyncio.run(case())


def test_a_cursor_never_crosses_purposes(tmp_path) -> None:
    """A management cursor is refused on the model channel and vice versa (CURSOR_SCOPE_MISMATCH)."""

    async def case() -> None:
        provider = ScriptedProvider(["好的。"] * (len(FILLERS) + 1))
        runtime = build(tmp_path, provider)
        async with runtime:
            agent = await _seed(runtime)
            retriever = runtime.arp.retriever
            session = store.read_live_session(runtime.uow.database.connection, agent.agent_id)
            caller = Pin("principal", "host:admin", 0, digest("host:admin"))
            request = {"schema_version": 1, "query": "工程暗号", "cursor": None, "limit": 1, "max_bytes": 4096}
            pages = {}
            for purpose in ("MANAGEMENT_SEARCH", "MODEL_SEARCH"):
                access = retriever.access_for(session, purpose=purpose, caller_ref=caller, turn_id=None)
                pages[purpose] = (access, retriever.search(access, request))
                assert pages[purpose][1]["cursor_purpose"] == purpose
            for mine, other in (("MODEL_SEARCH", "MANAGEMENT_SEARCH"), ("MANAGEMENT_SEARCH", "MODEL_SEARCH")):
                with pytest.raises(ArpError) as refused:
                    retriever.search(pages[mine][0], {**request, "cursor": pages[other][1]["next_cursor"]})
                assert refused.value.code == "CURSOR_SCOPE_MISMATCH"
            # Same purpose, another caller: also refused (owner scope is part of the cursor).
            stranger = retriever.access_for(session, purpose="MODEL_SEARCH", caller_ref=Pin("principal", "agent:other", 0, digest("x")), turn_id=None)
            with pytest.raises(ArpError) as refused:
                retriever.search(stranger, {**request, "cursor": pages["MODEL_SEARCH"][1]["next_cursor"]})
            assert refused.value.code == "CURSOR_SCOPE_MISMATCH"

    asyncio.run(case())
