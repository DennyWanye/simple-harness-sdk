# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-B: session partition, INDEX jobs, frozen search pages, automatic recall, history read.

The acceptance shape of CONTEXT-RECALL §8 in narrow form: the target lives in an
early Turn, the frozen scan needs more than one page (page_rows=4), mandatory and
recent groups are excluded, the aggregate is READY before the composer sees it,
a crash between pages resumes the same query without a second embedding call,
and a page cursor re-sent returns the same page.
"""

from __future__ import annotations

import asyncio

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.search import SessionAccess
from simple_harness.agents.arp.strict import digest
from simple_harness.agents.contracts import AgentTurnState
from simple_harness.agents.memory.embedding import HashEmbedder

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")
SECRET = "工程暗号 是 蓝鲸七号，请牢记。"
FILLERS = [f"第{i}条闲聊：今天天气不错，我们聊聊别的话题 {i}。" for i in range(6)]
# A deliberately small deployment window (the test counter bills one token per
# whitespace word): the recent suffix holds only the last few Turns, so the early
# secret can only come back through the frozen-index recall (F).
SMALL = dict(
    input_limit=72,
    max_output=16,
    policy_overrides=dict(
        max_context_tokens=72, output_reserve_tokens=16, safety_reserve_tokens=2, tool_headroom_tokens=2,
        recent_min_tokens=12, recall_max_tokens=24, fixed_soft_max_tokens=32,
    ),
)


async def _turn(agent, text: str, input_id: str):  # type: ignore[no-untyped-def]
    receipt = await agent.submit(text, input_id=input_id)
    return await agent.wait_turn(receipt.turn_id, timeout=10)


def _latest_manifest(runtime):  # type: ignore[no-untyped-def]
    connection = runtime.uow.database.connection
    row = connection.execute(
        "SELECT original_request_key FROM arp_context_requests ORDER BY created_at_ms DESC, provider_request_ordinal DESC LIMIT 1"
    ).fetchone()
    return store.read_context_by_request_key(connection, str(row[0]))


def _recall_of(runtime, manifest):  # type: ignore[no-untyped-def]
    return store.read_context_recall(runtime.uow.database.connection, manifest.manifest["retrieval_receipt_ref"]["id"])


async def _seed(runtime, provider_script_len: int):  # type: ignore[no-untyped-def]
    agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
    result = await _turn(agent, SECRET, "i0")
    assert result.state is AgentTurnState.COMMITTED
    for i, filler in enumerate(FILLERS, 1):
        result = await _turn(agent, filler, f"i{i}")
        assert result.state is AgentTurnState.COMMITTED
    return agent


@pytest.mark.parametrize("embedding", [None, HashEmbedder(dim=32)], ids=["lexical", "hybrid-mock-embedding"])
def test_recall_finds_the_early_secret_beyond_the_first_scan_page(tmp_path, embedding) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["记住了。"] + ["好的。"] * len(FILLERS) + ["暗号是蓝鲸七号。"])
        runtime = build(tmp_path, provider, embedding=embedding, **SMALL)
        async with runtime:
            agent = await _seed(runtime, len(FILLERS) + 2)
            connection = runtime.uow.database.connection
            session = store.read_live_session(connection, agent.agent_id)
            # Closed groups of finished Turns were materialised in the partition.
            done = connection.execute("SELECT COUNT(*) FROM arp_jobs WHERE kind='INDEX' AND state='DONE'").fetchone()[0]
            assert done >= 2 * (len(FILLERS))  # anchor + terminal answer of every finished Turn
            publication = store.active_index_publication(connection, session.session_id)
            assert publication is not None and publication.state == "ACTIVE"
            result = await _turn(agent, "请问工程暗号 是什么？", "ask")
            assert result.state is AgentTurnState.COMMITTED
            manifest = _latest_manifest(runtime)
            body = manifest.manifest
            recall = _recall_of(runtime, manifest)
            assert recall.phase == "READY" and recall.result["outcome"] == "READY"
            coverage = recall.result["coverage"]
            assert coverage["phase"] == "RESULTS" and coverage["ranking_final"]
            expected_mode = "HYBRID" if embedding is not None else "LEXICAL_ONLY"
            assert coverage["mode"] == expected_mode
            assert recall.result["status"] in ("COMPLETE", "LEXICAL_ONLY", "PARTIAL")
            # More than one SCANNING page: page_rows=4 and dozens of chunks.
            assert recall.checkpoint["scan_pages_committed"] >= 2
            assert recall.checkpoint["result_pages_committed"] >= 1
            assert len(recall.result["page_refs"]) == recall.checkpoint["scan_pages_committed"] + recall.checkpoint["result_pages_committed"]
            assert recall.result["query_result_count"] == len(recall.result["candidate_items"])
            assert body["recalled_chunk_ids"], "the secret chunk must be recalled into F"
            # The recalled chunk comes from a group that is neither mandatory nor recent.
            recalled = {i["chunk_id"]: i for i in recall.result["candidate_items"]}
            for chunk_id in body["recalled_chunk_ids"]:
                assert not (set(recalled[chunk_id]["source_group_ids"]) & set(body["recent_group_ids"]))
                assert not (set(recalled[chunk_id]["source_group_ids"]) & set(recall.request["mandatory_group_ids"]))
            sent = provider.requests[-1]
            texts = [m.content for m in sent.messages if isinstance(m.content, str)]
            assert any("recalled_history" in t and "蓝鲸七号" in t for t in texts)
            # The query embedding (hybrid) was made exactly once under its call key.
            if embedding is not None:
                assert recall.checkpoint["embedding_invocation_ref"] is not None
                calls = connection.execute(
                    "SELECT COUNT(*) FROM run_events WHERE kind='arp.embedding_call.v1' AND payload_json LIKE '%SESSION_QUERY%'"
                ).fetchone()[0]
                hybrid = connection.execute(
                    "SELECT COUNT(*) FROM arp_context_recalls WHERE json_extract(progress_json,'$.mode')='HYBRID'"
                ).fetchone()[0]
                assert calls == hybrid  # one query embedding per coordinated recall, never more
                mine = connection.execute(
                    "SELECT COUNT(*) FROM run_events WHERE kind='arp.embedding_call.v1' AND payload_json LIKE ?",
                    (f"%{recall.checkpoint['query_call_key']}%",),
                ).fetchone()[0]
                assert mine == 1
            summary_status = recall.result["status"]
            assert summary_status != "COMPLETE_EMPTY"

    asyncio.run(case())


@pytest.mark.parametrize("embedding", [None, HashEmbedder(dim=32)], ids=["lexical", "hybrid-mock-embedding"])
@pytest.mark.parametrize("crash_after_page", [1, 2])
def test_recall_resumes_the_same_frozen_query_after_a_crash_between_pages(tmp_path, embedding, crash_after_page) -> None:
    """A crash after the first page (query + first cursor exist in the partition, no cursor in
    the checkpoint yet) and after a later page both resume the same frozen query; hybrid mode
    makes no second query embedding call."""

    async def case() -> None:
        armed = {"on": False, "hits": 0}

        def fault(name: str) -> None:
            if armed["on"] and name == "recall.after_page":
                armed["hits"] += 1
                # The process is gone from this page on: every later attempt (the background
                # pump retries the recall) dies at the same point until the "restart" below.
                # Failing only once let the pump finish the recall before we looked (flaky).
                if armed["hits"] >= crash_after_page:
                    raise RuntimeError("process exit between pages")

        provider = ScriptedProvider(["记住了。"] + ["好的。"] * len(FILLERS) + ["暗号是蓝鲸七号。"])
        runtime = build(tmp_path, provider, fault=fault, embedding=embedding, **SMALL)
        async with runtime:
            agent = await _seed(runtime, len(FILLERS) + 2)
            connection = runtime.uow.database.connection
            armed["on"] = True
            receipt = await agent.submit("请问工程暗号 是什么？", input_id="ask")
            try:
                await agent.wait_turn(receipt.turn_id, timeout=10)
            except Exception:  # noqa: BLE001 - the crash window is what we test
                pass
            assert armed["hits"] >= crash_after_page
            rows = store.list_pending_recalls(connection)
            assert len(rows) == 1
            row = rows[0]
            assert row.phase == "SCANNING" and row.checkpoint["scan_pages_committed"] == crash_after_page - 1
            first_refs = list(row.checkpoint["page_refs"])
            # A restart resumes from the checkpoint: same key, same query, same snapshot.
            armed["on"] = False
            runtime.arp.recall._pages.clear()
            outcome = runtime.arp.tick()
            assert outcome["recalls"] == 1 and outcome["blocked"] == 0
            after = store.read_context_recall(connection, row.recall_key)
            assert after.phase == "READY" and after.query_id == row.query_id
            assert after.checkpoint["index_snapshot_ref"] == row.checkpoint["index_snapshot_ref"]
            assert after.checkpoint["page_refs"][: len(first_refs)] == first_refs
            assert len({p["id"] for p in after.checkpoint["page_refs"]}) == len(after.checkpoint["page_refs"])
            assert after.result["query_result_count"] == len(after.result["candidate_items"]) >= 1
            if embedding is not None:
                assert after.checkpoint["mode"] == "HYBRID"
                calls = connection.execute(
                    "SELECT COUNT(*) FROM run_events WHERE kind='arp.embedding_call.v1' AND payload_json LIKE ?",
                    (f"%{after.checkpoint['query_call_key']}%",),
                ).fetchone()[0]
                assert calls == 1

    asyncio.run(case())


def test_reprepare_of_the_same_request_after_a_crash_at_c0_continues_the_frozen_recall(tmp_path) -> None:
    """The caller re-sends the same provider request after a crash right after C0: the
    composer resumes the frozen recall (one row, one query) instead of failing on a second
    clock receipt / a second request body."""

    async def case() -> None:
        armed = {"on": False, "hits": 0}
        seen: dict[str, object] = {}

        def fault(name: str) -> None:
            if armed["on"] and name == "recall.after_c0":
                armed["hits"] += 1
                if armed["hits"] == 1:
                    raise RuntimeError("process exit after C0")

        provider = ScriptedProvider(["记住了。"] + ["好的。"] * len(FILLERS) + ["暗号是蓝鲸七号。"])
        runtime = build(tmp_path, provider, fault=fault, **SMALL)
        async with runtime:
            agent = await _seed(runtime, len(FILLERS) + 2)
            connection = runtime.uow.database.connection
            context = runtime.arp.context
            original = context.freeze

            def freeze(run_id, request):  # type: ignore[no-untyped-def]
                seen["args"] = (run_id, request)
                return original(run_id, request)

            context.freeze = freeze  # type: ignore[method-assign]
            armed["on"] = True
            receipt = await agent.submit("请问工程暗号 是什么？", input_id="ask")
            result = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert result.state is AgentTurnState.FAILED  # the injected crash is a definite failure
            run_id, request = seen["args"]  # type: ignore[misc]
            request_key = request.request_id.value  # type: ignore[attr-defined]
            assert store.get_context_recall_exact(connection, request_key) is not None
            # Same request key, same body: continue, do not rebuild.
            wire = original(run_id, request)
            assert any(isinstance(m.content, str) and "recalled_history" in m.content for m in wire.messages)
            recalls = connection.execute("SELECT COUNT(*) FROM arp_context_recalls WHERE original_request_key=?", (request_key,)).fetchone()[0]
            assert recalls == 1
            row = store.get_context_recall_exact(connection, request_key)
            assert row.phase == "READY"
            manifest = store.read_context_by_request_key(connection, request_key)
            assert manifest is not None and manifest.manifest["retrieval_receipt_ref"]["id"] == row.recall_key
            # And a third send replays the frozen manifest byte for byte.
            again = original(run_id, request)
            assert again == wire

    asyncio.run(case())


def test_a_poison_index_job_blocks_itself_and_never_the_turn(tmp_path) -> None:
    async def case() -> None:
        armed = {"hits": 0}

        def fault(name: str) -> None:
            if name == "index.before_materialize":
                armed["hits"] += 1
                if armed["hits"] == 1:
                    raise RuntimeError("partition write exploded")

        provider = ScriptedProvider(["记住了。", "好的。"])
        runtime = build(tmp_path, provider, fault=fault, **SMALL)
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert (await _turn(agent, SECRET, "i0")).state is AgentTurnState.COMMITTED
            assert (await _turn(agent, FILLERS[0], "i1")).state is AgentTurnState.COMMITTED
            connection = runtime.uow.database.connection
            blocked = connection.execute("SELECT job_id FROM arp_jobs WHERE kind='INDEX' AND state='BLOCKED'").fetchall()
            assert len(blocked) == 1
            reasons = connection.execute(
                "SELECT payload_json FROM run_events WHERE kind LIKE 'arp.%' AND payload_json LIKE '%INTERNAL:RuntimeError%'"
            ).fetchall()
            assert reasons, "the block reason is recorded on the job result"

    asyncio.run(case())


def test_empty_query_is_skipped_without_a_cursor_or_embedding(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["好的。"])
        runtime = build(tmp_path, provider, embedding=HashEmbedder(dim=16))
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            result = await _turn(agent, "   ", "i1")
            assert result.state is AgentTurnState.COMMITTED
            recall = _recall_of(runtime, _latest_manifest(runtime))
            assert recall.phase == "SKIPPED"
            assert recall.result["skip_code"] == "EMPTY_QUERY" and recall.result["status"] == "NOT_REQUESTED"
            assert recall.result["index_snapshot_ref"] is None and recall.checkpoint["cursor_token"] is None
            calls = runtime.uow.database.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE kind='arp.embedding_call.v1' AND payload_json LIKE '%SESSION_QUERY%'"
            ).fetchone()[0]
            assert calls == 0

    asyncio.run(case())


def test_search_cursor_replay_returns_the_same_page_and_history_reads_slice_utf8(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["记住了。"] + ["好的。"] * len(FILLERS))
        runtime = build(tmp_path, provider)
        async with runtime:
            agent = await _seed(runtime, len(FILLERS) + 1)
            connection = runtime.uow.database.connection
            session = store.read_live_session(connection, agent.agent_id)
            service, guard, generation = runtime.arp.search_for(session)
            access = runtime.arp.context._access(session, "manual")
            highwater = runtime.uow.agent_journal_highwater(agent.agent_id)
            expected = state_groups = int(runtime.arp.index.state_for(session).partition.connection.execute("SELECT COUNT(*) FROM group_sources").fetchone()[0])
            assert state_groups >= 4
            with guard.held():
                snapshot = service.freeze_snapshot(
                    access, index_generation=generation.index_generation, journal_highwater=highwater,
                    expected_group_set_hash=digest([]), expected_groups=expected, source_snapshot_hash="a" * 64,
                    embedding_fingerprint=generation.embedding_fingerprint, chunker_fingerprint=generation.chunker_fingerprint,
                    view_policy_hash=generation.view_policy_hash, identity={"manual": 1},
                )
                request = {
                    "query_text": "暗号", "query_hash": digest("暗号"), "mandatory_group_ids": [], "protected_group_ids": [],
                    "journal_highwater": highwater, "control_generation": session.generation, "policy_ref": runtime.arp.policy.pin.to_json(),
                    "limits": {"page_rows": 4, "channel_top_k": 8, "global_candidates": 8}, "recall_key": "manual",
                }
                page, cursor = service.start_frozen(access, request, snapshot, query_vector=None, query_vector_ref=None, mode="LEXICAL_ONLY", query_id="manual:query")
                assert page["page_semantics"] == "PROGRESS" and page["items"] == [] and page["has_more"] and cursor
                assert page["receipt"]["status"] == "PARTIAL" and page["receipt"]["query_result_count"] is None
                second, cursor2 = service.resume_frozen(access, cursor)
                replay, cursor2b = service.resume_frozen(access, cursor)
                assert replay == second and cursor2b == cursor2
                # Walk to the final ranking; every RESULTS page appends in fixed order.
                pages = [page, second]
                token = cursor2
                while token is not None:
                    nxt, token = service.resume_frozen(access, token)
                    pages.append(nxt)
                final = [p for p in pages if p["page_semantics"] == "APPEND_FINAL"]
                assert final and final[-1]["has_more"] is False
                ranks = [i["rank_ordinal"] for p in final for i in p["items"]]
                assert ranks == list(range(1, len(ranks) + 1)) and ranks
                with pytest.raises(ArpError) as info:
                    service.resume_frozen(access, "0" * 64)
                assert info.value.code == "CURSOR_UNKNOWN"
                # History read: exact Journal slices on UTF-8 boundaries with a cursor chain.
                # 1024 bytes cannot hold one slice with its exact metadata and a cursor:
                # that is a named refusal, never a silent truncation.
                with pytest.raises(ArpError) as info:
                    service.read_history(
                        access, connection, {"schema_version": 1, "seq_from": 1, "seq_to": highwater, "cursor": None, "max_bytes": 1024},
                        purpose="MANAGEMENT_READ", journal_highwater=highwater, source_snapshot_hash="a" * 64,
                    )
                assert info.value.code == "ITEM_TOO_LARGE"
                read = service.read_history(
                    access, connection, {"schema_version": 1, "seq_from": 1, "seq_to": highwater, "cursor": None, "max_bytes": 2048},
                    purpose="MANAGEMENT_READ", journal_highwater=highwater, source_snapshot_hash="a" * 64,
                )
                assert read["body_bytes"] <= 2048 and read["has_more"]
                collected = list(read["items"])
                pages_read = 1
                while read["next_cursor"] is not None:
                    read = service.read_history(
                        access, connection, {"schema_version": 1, "seq_from": 1, "seq_to": highwater, "cursor": read["next_cursor"], "max_bytes": 2048},
                        purpose="MANAGEMENT_READ", journal_highwater=highwater, source_snapshot_hash="a" * 64,
                    )
                    assert read["body_bytes"] <= 2048
                    collected.extend(read["items"])
                    pages_read += 1
                assert pages_read >= 3
                seqs = sorted({i["seq"] for i in collected})
                assert seqs == list(range(1, highwater + 1))
                for item in collected:
                    assert item["text"].encode("utf-8") == item["text"].encode("utf-8")[: item["utf8_end"] - item["utf8_start"]]
                assert any(SECRET in i["text"] for i in collected)
                with pytest.raises(ArpError) as info:
                    service.read_history(
                        access, connection, {"schema_version": 1, "seq_from": 1, "seq_to": highwater + 5, "cursor": None, "max_bytes": 1024},
                        purpose="MANAGEMENT_READ", journal_highwater=highwater, source_snapshot_hash="a" * 64,
                    )
                assert info.value.code == "HISTORY_BYTE_RANGE_INVALID"

    asyncio.run(case())


def test_partition_identity_and_generation_are_bound_to_the_session(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["好的。"]))
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            await _turn(agent, "你好", "i1")
            connection = runtime.uow.database.connection
            session = store.read_live_session(connection, agent.agent_id)
            state = runtime.arp.index.state_for(session)
            path = runtime.arp.root.resolve_relative(session.relative_directory) / "index.sqlite3"
            assert path.is_file() and state.generation.state == "READY"
            row = state.partition.connection.execute("SELECT session_id, agent_id, schema_version FROM session_partition").fetchone()
            assert (row[0], row[1], row[2]) == (session.session_id, agent.agent_id, 2)
            # Same fingerprints reuse the generation; a different chunker starts a new one.
            with state.partition.transaction() as pconn:
                same = state.partition.ensure_generation_locked(
                    pconn, view_policy_hash=state.generation.view_policy_hash, chunker_fingerprint=state.generation.chunker_fingerprint,
                    embedding_fingerprint=state.generation.embedding_fingerprint,
                )
                other = state.partition.ensure_generation_locked(
                    pconn, view_policy_hash=state.generation.view_policy_hash, chunker_fingerprint="b" * 64,
                    embedding_fingerprint=state.generation.embedding_fingerprint,
                )
            assert same.index_generation == state.generation.index_generation
            assert other.index_generation == state.generation.index_generation + 1
            # Materialising the same group id with another source hash is a conflict.
            gsrc = state.partition.connection.execute("SELECT group_id, source_hash FROM group_sources LIMIT 1").fetchone()
            assert gsrc is not None
            with pytest.raises(ArpError) as info, state.partition.transaction() as pconn:
                state.partition.materialize_group_locked(
                    pconn, generation=other, group_id=str(gsrc[0]), source_hash="c" * 64, seq_from=1, seq_to=1,
                    closed_receipt_ref=Pin("receipt", "x", 0, "d" * 64), view_hash="e" * 64, chunks=[], vectors=None, embedding_receipt_ref=None,
                )
            assert info.value.code == "GROUP_HASH_CONFLICT"

    asyncio.run(case())


def test_session_access_scope_hash_is_caller_bound() -> None:
    a = SessionAccess(Pin("session", "s", 1, "a" * 64), Pin("agent", "g", 0, "a" * 64), None, "inc", "CONTEXT_RECALL", Pin("principal", "p1", 0, "a" * 64), (), "b" * 64, 1, 10)
    b = SessionAccess(Pin("session", "s", 1, "a" * 64), Pin("agent", "g", 0, "a" * 64), None, "inc", "CONTEXT_RECALL", Pin("principal", "p2", 0, "a" * 64), (), "b" * 64, 1, 10)
    assert a.owner_scope_hash != b.owner_scope_hash
