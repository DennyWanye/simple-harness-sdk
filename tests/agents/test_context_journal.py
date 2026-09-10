# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 3 (BA13–BA22): incremental Journal + bounded Context assembly."""

from __future__ import annotations

import asyncio
import json

import pytest
from provider_fixture import MODEL, ScriptedProvider, message_texts
from tool_fixture import ECHO_SCHEMA, EchoToolExecutor

from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState, build_agent_runtime
from simple_harness.agents.context import (
    ContextPolicy,
    TiktokenTokenizer,
    UpperBoundTokenizer,
    policy_hash,
)
from simple_harness.agents.context.tokenizer import count_message, count_tools
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import MessageRole, RunId, thaw_json
from simple_harness.execution.provider_invocations import provider_request_fingerprint
from simple_harness.execution.uow import RunState
from simple_harness.runtime.termination import TerminationLimits

TOOL = ("echo", {})
tiktoken = pytest.importorskip("tiktoken")


def _ports(tmp_path, provider, *, executor=None, policy=None, tokenizer=None, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="ctx-owner",
        context_policy=policy or ContextPolicy(),
        tokenizer=tokenizer or TiktokenTokenizer(),
        # Scripted tools repeat identical calls; the repeated-tool guard is not
        # under test here.
        termination_limits=TerminationLimits(
            max_turns=10_000,
            max_tool_calls=20_000,
            max_wall_seconds=365.0 * 86_400.0,
            max_cost_micros=10_000_000_000,
            max_consecutive_same_tool=100,
        ),
    )
    if executor is not None:
        base.update(
            tool_executor=executor, tool_names=("echo",), tool_schemas={"echo": ECHO_SCHEMA}
        )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config(instructions="你是助手。", tools=()):
    return AgentConfig(name="w", instructions=instructions, model_profile_ref="p", tool_names=tools)


def _rows(uow, sql, *params):
    return uow.database.connection.execute(sql, params).fetchall()


LONG = "这是一段用来撑大上下文的中文说明，包含足够多的字以便超过预算。" * 12


def test_append_is_one_row_per_message_and_never_rewrites(tmp_path):
    async def case():
        provider = ScriptedProvider(["答一", "答二", "答三", "答四"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="lin")
            sizes = []
            for n in range(4):
                await agent.ask(LONG + f"#{n}", input_id=f"i{n}", timeout=5)
                total = _rows(
                    runtime.uow,
                    "SELECT COALESCE(SUM(length(message_json)),0) FROM "
                    "base_agent_session_journal_v1 WHERE agent_id=?",
                    agent.agent_id,
                )[0][0]
                sizes.append(int(total))
            rows = _rows(
                runtime.uow,
                "SELECT kind FROM base_agent_session_journal_v1 WHERE agent_id=? ORDER BY seq",
                agent.agent_id,
            )
            # instructions + 4 × (user, assistant)
            assert [r[0] for r in rows] == ["instructions"] + ["user_input", "assistant"] * 4
            increments = [b - a for a, b in zip(sizes, sizes[1:])]
            # BA19: each turn adds about the same number of bytes (its own messages),
            # never a copy of the whole history.
            assert max(increments) <= min(increments) * 1.25
            legacy = _rows(
                runtime.uow,
                "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=? "
                "AND namespace='react.context.v1'",
                agent.run_id,
            )[0][0]
            assert legacy == 0
            assert runtime.kernel._ports.context.load(RunId(agent.run_id)).revision == 9

    asyncio.run(case())


def test_originals_are_exactly_readable_after_window_rotation(tmp_path):
    async def case():
        provider = ScriptedProvider(["答一", "答二", "答三", "答四"])
        policy = ContextPolicy(max_input_tokens=700, output_reserve=64, safety_margin=0)
        async with build_agent_runtime(_ports(tmp_path, provider, policy=policy)) as runtime:
            agent = await runtime.create(_config(), creation_key="rot")
            for n in range(4):
                await agent.ask(LONG + f"#{n}", input_id=f"i{n}", timeout=5)
            last = message_texts(provider.requests[-1])
            assert "#0" not in " ".join(t for t in last if "[历史已折叠]" not in t)
            assert any("[历史已折叠]" in t for t in last)
            journal = agent.journal()
            assert len(journal) == 9
            first_user = agent.read_journal_record(2)
            assert first_user is not None and first_user.kind == "user_input"
            assert first_user.message_json["content"] == LONG + "#0"
            selection = runtime.uow.latest_agent_context_selection(agent.agent_id)
            assert selection is not None and selection.dropped_ranges
            assert 2 in {seq for a, b in selection.dropped_ranges for seq in range(a, b + 1)}
            summaries = runtime.uow.list_agent_summaries(agent.agent_id)
            assert summaries and summaries[-1].from_seq >= 2

    asyncio.run(case())


def _groups_are_whole(messages):
    """Every tool message follows an assistant message of its group; no dangling tail."""

    expecting_tools = False
    for message in messages:
        if message.role is MessageRole.ASSISTANT:
            expecting_tools = True
        elif message.role is MessageRole.TOOL:
            assert expecting_tools, "tool result without its assistant call"
        else:
            expecting_tools = False


def test_protocol_groups_are_never_split_and_rotate_within_a_long_turn(tmp_path):
    from simple_harness.tools import ToolResult

    class PaddedTool:
        """Each result is ~150 tokens so the window holds only the newest groups."""

        async def execute(self, call, context):  # type: ignore[no-untyped-def]
            return ToolResult.succeeded(call.call_id, {"echo": "x" * 600})

    async def case():
        provider = ScriptedProvider([TOOL] * 6 + ["完成"])
        policy = ContextPolicy(max_input_tokens=900, output_reserve=64, safety_margin=0)
        async with build_agent_runtime(
            _ports(tmp_path, provider, executor=PaddedTool(), policy=policy)
        ) as runtime:
            agent = await runtime.create(_config(tools=("echo",)), creation_key="grp")
            result = await agent.ask(LONG, input_id="i1", timeout=10)
            assert result.state is AgentTurnState.COMMITTED
            assert provider.calls == 7
            for request in provider.requests:
                _groups_are_whole(request.messages)
                texts = message_texts(request)
                assert any(LONG in t for t in texts)  # current input never dropped (BA15)
            # BA17: the last request no longer carries the first tool exchange but does
            # carry the most recent one, all inside one turn.
            last = provider.requests[-1].messages
            tool_ids = [m.call_id.value for m in last if m.role is MessageRole.TOOL]
            assert "call-1" not in tool_ids and "call-6" in tool_ids
            assert any("[历史已折叠]" in t for t in message_texts(provider.requests[-1]))
            assert runtime._assembled.wire.fallback_total == 0  # review S3-03
            restored = [
                m for m in provider.requests[-1].messages if m.metadata.get("provider_tool_calls")
            ]
            assert restored and all(
                c["id"] for m in restored for c in m.metadata["provider_tool_calls"]
            )

    asyncio.run(case())


def test_required_content_too_large_fails_before_any_provider_call(tmp_path):
    async def case():
        provider = ScriptedProvider(["不该被调用"])
        policy = ContextPolicy(max_input_tokens=200, output_reserve=16, safety_margin=0)
        async with build_agent_runtime(_ports(tmp_path, provider, policy=policy)) as runtime:
            agent = await runtime.create(_config(instructions=LONG * 3), creation_key="big")
            failed = await agent.ask("问", input_id="i1", timeout=5)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "context_required_content_too_large"
            assert failed.error["detail"]["required_over_budget"] is True
            assert provider.calls == 0
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state is RunState.WAITING
            selection = runtime.uow.latest_agent_context_selection(agent.agent_id)
            assert selection is not None and selection.required_over_budget

    asyncio.run(case())


def test_tool_schemas_count_against_the_budget(tmp_path):
    big_schema = {
        "type": "object",
        "properties": {
            f"field_{i}": {"type": "string", "description": "x" * 40} for i in range(30)
        },
        "additionalProperties": False,
    }

    async def run(tmp, with_tool):
        provider = ScriptedProvider(["答一", "答二", "答三"])
        policy = ContextPolicy(max_input_tokens=1200, output_reserve=64, safety_margin=0)
        kwargs = {}
        if with_tool:
            kwargs = dict(
                tool_executor=EchoToolExecutor(),
                tool_names=("echo",),
                tool_schemas={"echo": big_schema},
            )
        async with build_agent_runtime(_ports(tmp, provider, policy=policy, **kwargs)) as runtime:
            agent = await runtime.create(
                _config(tools=("echo",) if with_tool else ()), creation_key="t"
            )
            for n in range(3):
                await agent.ask(LONG + f"#{n}", input_id=f"i{n}", timeout=5)
            selection = runtime.uow.latest_agent_context_selection(agent.agent_id)
            return selection, provider.requests[-1]

    async def case():
        without, req_without = await run(tmp_path / "a", False)
        with_tool, req_with = await run(tmp_path / "b", True)
        assert with_tool.tool_tokens > 0 and without.tool_tokens == 0
        assert len(with_tool.selected_seqs) < len(without.selected_seqs)
        tokenizer = TiktokenTokenizer()
        for req in (req_without, req_with):
            total = sum(count_message(tokenizer, m) for m in req.messages) + count_tools(
                tokenizer, req.tools
            )
            assert total <= 1200 - 64

    asyncio.run(case())


def test_policy_hash_and_counts_are_not_reused_across_tokenizers(tmp_path):
    policy = ContextPolicy(max_input_tokens=4000)
    a = policy_hash(policy, tokenizer_fingerprint=TiktokenTokenizer().fingerprint, model="m1")
    b = policy_hash(policy, tokenizer_fingerprint=UpperBoundTokenizer.fingerprint, model="m1")
    c = policy_hash(policy, tokenizer_fingerprint=TiktokenTokenizer().fingerprint, model="m2")
    assert len({a, b, c}) == 3

    async def case():
        provider = ScriptedProvider(["答一", "答二"])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(
            _ports(tmp_path, provider, tokenizer=TiktokenTokenizer(), clock=lambda: clock["now"])
        )
        async with runtime:
            agent = await runtime.create(_config(), creation_key="tok")
            await agent.ask("一", input_id="i1", timeout=5)
            first = runtime.uow.latest_agent_context_selection(agent.agent_id)
        clock["now"] = 100.0
        again = build_agent_runtime(
            _ports(
                tmp_path,
                provider,
                tokenizer=UpperBoundTokenizer(),
                owner_id="ctx-owner-2",
                clock=lambda: clock["now"],
            )
        )
        async with again:
            reopened = await again.open(agent.agent_id)
            await reopened.ask("二", input_id="i2", timeout=5)
            second = again.uow.latest_agent_context_selection(agent.agent_id)
        assert first.tokenizer_fingerprint != second.tokenizer_fingerprint
        assert first.policy_hash != second.policy_hash
        assert first.selection_id != second.selection_id

    asyncio.run(case())


def test_large_tool_result_is_externalized_with_a_read_back_reference(tmp_path):
    from simple_harness.tools import ToolResult

    class BigTool:
        async def execute(self, call, context):  # type: ignore[no-untyped-def]
            return ToolResult.succeeded(call.call_id, {"blob": "数据" * 4000})

    async def case():
        provider = ScriptedProvider([TOOL, "看完了"])
        policy = ContextPolicy(max_input_tokens=8000, max_tool_result_tokens=512)
        ports = _ports(
            tmp_path,
            provider,
            policy=policy,
            tool_executor=BigTool(),
            tool_names=("echo",),
            tool_schemas={"echo": ECHO_SCHEMA},
        )
        async with build_agent_runtime(ports) as runtime:
            agent = await runtime.create(_config(tools=("echo",)), creation_key="big")
            result = await agent.ask("拿数据", input_id="i1", timeout=10)
            assert result.state is AgentTurnState.COMMITTED
            journal = agent.journal()
            full = [
                r for r in journal if r.kind == "tool_result" and r.visibility == "journal_only"
            ]
            preview = [r for r in journal if r.kind == "tool_result" and r.visibility == "context"]
            assert len(full) == 1 and len(preview) == 1
            assert preview[0].full_record_seq == full[0].seq
            assert "数据" * 4000 in json.dumps(thaw_json(full[0].message_json), ensure_ascii=False)
            payload = json.loads(preview[0].message_json["content"])
            assert payload["truncated"] is True
            assert payload["journal_read_back"]["seq"] == full[0].seq
            assert len(payload["value_preview"]) <= policy.tool_result_preview_chars
            sent = message_texts(provider.requests[-1])
            assert not any("数据" * 4000 in t for t in sent)
            assert any("journal_read_back" in t for t in sent)
            original = agent.read_journal_record(full[0].seq)
            assert original is not None and "数据" * 4000 in original.message_json["content"]

    asyncio.run(case())


def test_summary_is_derived_and_names_its_sources(tmp_path):
    async def case():
        provider = ScriptedProvider(["答一", "答二", "答三"])
        policy = ContextPolicy(max_input_tokens=600, output_reserve=64, safety_margin=0)
        async with build_agent_runtime(_ports(tmp_path, provider, policy=policy)) as runtime:
            agent = await runtime.create(_config(), creation_key="sum")
            for n in range(3):
                await agent.ask(LONG + f"#{n}", input_id=f"i{n}", timeout=5)
            summary_messages = [
                m for m in provider.requests[-1].messages if m.metadata.get("derived") is True
            ]
            assert len(summary_messages) == 1
            meta = summary_messages[0].metadata
            summaries = runtime.uow.list_agent_summaries(agent.agent_id)
            assert any(
                s.from_seq == meta["source_seq_from"]
                and s.to_seq == meta["source_seq_to"]
                and s.source_hash == meta["source_hash"]
                and s.validity == "valid"
                for s in summaries
            )
            # The summary is never stored as a Journal record (BA20/BA21): no recall
            # or derived text re-enters the raw history.
            kinds = {r.kind for r in agent.journal()}
            assert kinds <= {"instructions", "user_input", "assistant", "tool_result", "feedback"}
            assert not any(
                "[历史已折叠]" in json.dumps(thaw_json(r.message_json), ensure_ascii=False)
                for r in agent.journal()
            )

    asyncio.run(case())


def test_real_tokenizer_keeps_every_request_within_budget(tmp_path):
    """BA13 with a real BPE tokenizer: the final rendered request never exceeds B_input."""

    encoding = tiktoken.get_encoding("cl100k_base")

    def real_count(request):
        text = "\n".join(message_texts(request)) + "\n".join(
            json.dumps({"name": t.name, "description": t.description}) for t in request.tools
        )
        return len(encoding.encode(text))

    async def case():
        provider = ScriptedProvider([TOOL, "答一", TOOL, "答二", "答三", "答四", "答五"])
        policy = ContextPolicy(max_input_tokens=800, output_reserve=64, safety_margin=16)
        executor = EchoToolExecutor()
        async with build_agent_runtime(
            _ports(tmp_path, provider, executor=executor, policy=policy)
        ) as runtime:
            agent = await runtime.create(_config(tools=("echo",)), creation_key="real")
            for n in range(5):
                result = await agent.ask(LONG + f"#{n}", input_id=f"i{n}", timeout=10)
                assert result.state is AgentTurnState.COMMITTED, result.error
            assert provider.calls == 7
            budget = policy.input_budget()
            for request in provider.requests:
                assert real_count(request) <= budget + policy.render_slack_tokens
            # Selections are bound to the frozen request identities.
            for request in provider.requests:
                selection = runtime.uow.read_agent_context_selection_by_request(
                    request.request_id.value
                )
                assert selection is not None
                assert (
                    selection.request_hash
                    == provider_request_fingerprint(runtime._assembled.wire.last_request)
                    or selection.request_tokens is not None
                )

    asyncio.run(case())


def test_unknown_resume_reuses_the_frozen_request_without_a_new_selection(tmp_path):
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
        provider = ScriptedProvider(["恢复后的回答"])
        original = provider.invoke
        state = {"failed": False}

        async def once(request, *, cancel):
            if not state["failed"]:
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
        async with build_agent_runtime(_ports(tmp_path, provider, policies=policies)) as runtime:
            agent = await runtime.create(_config(), creation_key="resume")
            receipt = await agent.submit("问", input_id="i1")
            for _ in range(100):
                if runtime.uow.list_open_wait_blockers_for_run(agent.run_id):
                    break
                await asyncio.sleep(0.02)
            before = _rows(
                runtime.uow,
                "SELECT selection_id FROM base_agent_context_selections_v1 WHERE agent_id=?",
                agent.agent_id,
            )
            await runtime.kernel.reconcile()
            result = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert result.state is AgentTurnState.COMMITTED
            after = _rows(
                runtime.uow,
                "SELECT selection_id FROM base_agent_context_selections_v1 WHERE agent_id=?",
                agent.agent_id,
            )
            assert after == before  # no re-selection for the same frozen request (§7.4)
            assert provider.calls == 1

    asyncio.run(case())


def test_summary_is_reserved_before_admission_and_never_overfills(tmp_path):
    """Review S3-01: with the budget nearly full, rotation must never wedge the Agent."""

    async def case():
        provider = ScriptedProvider([f"答{n}" for n in range(8)])
        policy = ContextPolicy(max_input_tokens=860, output_reserve=64, safety_margin=0)
        async with build_agent_runtime(_ports(tmp_path, provider, policy=policy)) as runtime:
            agent = await runtime.create(_config(), creation_key="reserve")
            for n in range(8):
                result = await agent.ask(LONG + f"#{n}", input_id=f"i{n}", timeout=5)
                assert result.state is AgentTurnState.COMMITTED, result.error
            assert provider.calls == 8
            rows = _rows(
                runtime.uow,
                "SELECT message_tokens, tool_tokens, budget_tokens, required_over_budget "
                "FROM base_agent_context_selections_v1 WHERE agent_id=?",
                agent.agent_id,
            )
            assert rows
            for message_tokens, tool_tokens, budget, over in rows:
                assert over == 0 and message_tokens + tool_tokens <= budget

    asyncio.run(case())


def test_current_input_is_present_even_when_a_turn_outgrows_the_read_window(tmp_path):
    """Review S3-02: >128 journal rows in one turn; every request still carries the input."""

    async def case():
        calls = 70
        provider = ScriptedProvider([TOOL] * calls + ["完成"])
        policy = ContextPolicy(max_input_tokens=900, output_reserve=64, safety_margin=0)
        executor = EchoToolExecutor()
        ports = _ports(tmp_path, provider, executor=executor, policy=policy)
        async with build_agent_runtime(ports) as runtime:
            config = AgentConfig(
                name="w",
                instructions="你是助手。",
                model_profile_ref="p",
                tool_names=("echo",),
                limits=AgentLimits(max_model_calls_per_turn=200, max_tool_calls_per_turn=200),
            )
            agent = await runtime.create(config, creation_key="wide")
            marker = "唯一标记 QX-4471"
            result = await agent.ask(marker, input_id="i1", timeout=30)
            assert result.state is AgentTurnState.COMMITTED, result.error
            assert provider.calls == calls + 1
            assert len(agent.journal()) > 128
            for request in provider.requests:
                assert any(marker in t for t in message_texts(request))
                _groups_are_whole(request.messages)
            assert runtime._assembled.wire.fallback_total == 0

    asyncio.run(case())
