# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-E case reconciliation: the acceptance cases of ``specs/1.1.1/implementation/sdk-cases.json``
that no earlier ARP test pinned down (R09 cross-root symlink, M09 query text safety and short
CJK fallback, K06 dependency cycle / depth, N12 + RV_R4 vector normalisation and stable
ranking ties, RV_R5 policy limit boundaries)."""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider
from skill_fixture import import_skill, native_bundle

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.arp.creation import MARKER_FILE
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.profile import POLICY_HARD_LIMITS, check_policy, default_policy
from simple_harness.agents.arp.rules import rrf, validate_vector
from simple_harness.agents.arp.search import QueryPlan, normalize_words, trigrams
from simple_harness.agents.arp.strict import plain
from simple_harness.agents.arp.tools import SEARCH_TOOL_NAME
from simple_harness.agents.contracts import AgentTurnState
from simple_harness.contracts import CallId, RequestId, RunId
from simple_harness.tools import CancellationToken, ToolContext

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")
SECRET = "工程暗号 是 蓝鲸七号，请牢记。"


async def _turn(agent, text: str, input_id: str):  # type: ignore[no-untyped-def]
    receipt = await agent.submit(text, input_id=input_id)
    return await agent.wait_turn(receipt.turn_id, timeout=10)


# ---- R09: a session directory replaced by a cross-root symlink ---------------------------------


def test_cross_root_symlink_is_refused_and_the_target_is_left_alone(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path / "arp", ScriptedProvider(["记住了。"]))
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert (await _turn(agent, "你好，请记住蓝鲸。", "i0")).state is AgentTurnState.COMMITTED
            session = store.read_live_session(runtime.uow.database.connection, agent.agent_id)
            command = {
                "schema_version": 1, "session_id": session.session_id, "expected_generation": session.generation, "command_id": "destroy-1",
                "reason": "USER_DESTROY", "retention_policy_ref": runtime.arp.ports.profile.refs.retention_policy_ref.to_json(),
            }
            sealed = await runtime.arp.sessions.destroy(command, caller=trusted_caller("d1"), command_id="destroy-1")
            source = runtime.arp.root.resolve_relative(sealed.purge_progress["source_relative_directory"])
            # An outside directory that even carries the session's own marker bytes.
            outside = tmp_path / "outside"
            shutil.copytree(source, outside)
            (outside / "keep.txt").write_bytes(b"external data")
            shutil.rmtree(source)
            os.symlink(outside, source, target_is_directory=True)
            before = sorted(p.name for p in outside.iterdir())
            with pytest.raises(ArpError) as refused:
                runtime.arp.sessions.advance(session.session_id)
            assert refused.value.code == "REF_OUTSIDE_SCOPE"
            assert sorted(p.name for p in outside.iterdir()) == before and (outside / MARKER_FILE).is_file()
            assert store.read_session(runtime.uow.database.connection, session.session_id).state == "PURGING"
            # The purge job reports the refusal by name instead of deleting through the link.
            runtime.arp.tick()
            jobs = store.list_session_jobs(runtime.uow.database.connection, session.session_id, kinds=["PURGE"], states=["BLOCKED", "PENDING", "DONE"])
            assert jobs and jobs[0].state == "BLOCKED" and jobs[0].result_receipt_ref is not None
            assert sorted(p.name for p in outside.iterdir()) == before

    asyncio.run(case())


# ---- M09: query text is data, short CJK falls back to a bounded substring ----------------------


def _context(agent, n: int) -> ToolContext:  # type: ignore[no-untyped-def]
    return ToolContext(RunId(agent.run_id), RequestId(f"{agent.run_id}:tool:{n}"), CancellationToken(), call_id=CallId(f"call-{n}"))


def test_query_text_is_never_sql_and_short_cjk_still_finds_the_record(tmp_path) -> None:
    assert normalize_words("Hello, WORLD! 蓝鲸七号 x_1") == ["hello", "world", "蓝鲸七号", "x_1"]
    assert trigrams("蓝鲸") == {"蓝鲸"} and trigrams("a b c") == {"abc"} and trigrams("") == set()
    plan = QueryPlan.build("'; DROP TABLE arp_chunks; -- seq 3 #7", None)
    assert plan.seqs == {3, 7} and "drop" in plan.words

    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["好的。"] * 4))
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            for i, text in enumerate([SECRET, "第一条闲聊。", "第二条闲聊。", "第三条闲聊。"]):
                assert (await _turn(agent, text, f"i{i}")).state is AgentTurnState.COMMITTED
            runtime.arp.tick()
            search = {t.spec.name: t for t in runtime._session_tools.function_tools()}[SEARCH_TOOL_NAME]
            tables_before = runtime.uow.database.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            hostile = await search.invoke({"query": "'; DROP TABLE arp_chunks; --", "limit": 4, "max_bytes": 8192}, _context(agent, 0))
            assert hostile is not None
            assert runtime.uow.database.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == tables_before
            items, generation, cursor = [], None, None
            for n in range(1, 64):
                result = await search.invoke({"query": "蓝鲸", "limit": 4, "max_bytes": 8192, **({"cursor": cursor} if cursor else {})}, _context(agent, n))
                assert result.outcome.value == "succeeded", result
                page = plain(result.value)
                if page["page_semantics"] == "APPEND_FINAL":
                    items.extend(page["items"])
                    generation = page["receipt"]["index_generation"]
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            session = store.read_live_session(runtime.uow.database.connection, agent.agent_id)
            service = runtime.arp.search_for(session)[0]
            texts = [service.chunk_text(generation, item["chunk_id"]) for item in items]
            assert any("蓝鲸七号" in text for text in texts), "the two-character query must reach the record through the bounded substring fallback"

    asyncio.run(case())


# ---- K06: dependency cycles and depth are refused, never unpacked recursively ------------------


def test_dependency_cycle_and_depth_are_refused_by_name(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            body = ("# 说明\n".encode(), "INSTRUCTIONS")

            def bundle(skill_id: str, requires: list) -> bytes:
                return native_bundle(runtime, skill_id=skill_id, implementation={"kind": "INSTRUCTIONS"}, files={"SKILL.md": body}, required_skill_refs=[p.to_json() for p in requires])

            b1 = import_skill(runtime, bundle("dep-b", []), command="b1", fmt="NATIVE").revision
            a1 = import_skill(runtime, bundle("dep-a", [b1.pin]), command="a1", fmt="NATIVE").revision
            assert runtime.arp.skills.latest_lock(a1)["complete"] is True
            # dep-b@2 requiring dep-a@1 closes the loop dep-b -> dep-a -> dep-b.
            with pytest.raises(ArpError) as cycle:
                import_skill(runtime, bundle("dep-b", [a1.pin]), command="b2", fmt="NATIVE")
            assert cycle.value.code == "DEPENDENCY_CYCLE"
            # A chain deeper than the lock depth is refused instead of being unpacked.
            previous = None
            for i in range(16):
                previous = import_skill(runtime, bundle(f"chain-{i}", [] if previous is None else [previous.pin]), command=f"c{i}", fmt="NATIVE").revision
            with pytest.raises(ArpError) as deep:
                import_skill(runtime, bundle("chain-16", [previous.pin]), command="c16", fmt="NATIVE")
            assert deep.value.code == "ARRAY_LIMIT"

    asyncio.run(case())


# ---- N12 / RV_R4: vectors and ranking ties ----------------------------------------------------


def test_vector_normalisation_and_stable_ranking_ties() -> None:
    unit = validate_vector([1e308, 0.0, -1e308], 3)
    assert all(abs(a) <= 1.0 for a in unit) and any(a != 0 for a in unit) and abs(sum(a * a for a in unit) - 1.0) < 1e-6
    for bad, code in (([float("nan"), 1.0], "INVALID_EMBEDDING"), ([0.0, 0.0], "ZERO_EMBEDDING"), ([1.0], "EMBEDDING_DIM_MISMATCH"), (["1", 1.0], "INVALID_EMBEDDING")):
        with pytest.raises(ArpError) as refused:
            validate_vector(bad, 2)
        assert refused.value.code == code
    keys = {"x": ("r2", "h", 0, 4, "c"), "y": ("r1", "h", 0, 4, "c"), "z": ("r1", "h", 0, 4, "b")}
    # Equal scores: the five-tuple decides, dictionary order of insertion never does.
    assert [k for k, _ in rrf({"words": [("x", 1.0), ("y", 1.0), ("z", 1.0)]}, stable_keys=keys)] == ["z", "y", "x"]
    # Per-channel top-K is cut before fusion, but every channel keeps its own head: M does not truncate K early.
    fused = rrf({"words": [("x", 2.0), ("y", 1.0)], "trigram": [("y", 5.0), ("z", 4.0)]}, 2, stable_keys=keys, channel_limit=1)
    assert [k for k, _ in fused] == ["x", "y"]
    for channels, code in (({"words": [("x", 1.0), ("x", 1.0)]}, "DUPLICATE_ITEM"), ({"other": [("x", 1.0)]}, "ENUM"), ({"words": [("q", 1.0)]}, "SOURCE_UNAVAILABLE")):
        with pytest.raises(ArpError) as refused:
            rrf(channels, stable_keys=keys)
        assert refused.value.code == code
    with pytest.raises(ArpError):
        rrf({"words": []}, 513, stable_keys=keys)


# ---- RV_R5: accepted limits at the boundary, one past it refused -------------------------------


def test_policy_limits_hold_at_the_boundary_and_refuse_one_past_it() -> None:
    base = default_policy("limits")
    for field, ceiling in POLICY_HARD_LIMITS.items():
        assert check_policy({**base, field: ceiling})[field] == ceiling
        with pytest.raises(ArpError):
            check_policy({**base, field: ceiling + 1})
    assert check_policy({**base, "query_cursor_ttl_ms": 900000})["query_cursor_ttl_ms"] == 900000
    with pytest.raises(ArpError):
        check_policy({**base, "query_cursor_ttl_ms": 900001})
    with pytest.raises(ArpError) as budget:
        check_policy({**base, "output_reserve_tokens": base["max_context_tokens"]})
    assert budget.value.code == "NO_INPUT_BUDGET"
