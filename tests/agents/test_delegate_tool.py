# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T8: agent.delegate — single level, quota, idempotent resume, result from child row."""

from __future__ import annotations

import asyncio
import secrets

import pytest
from provider_fixture import MODEL, Message, MessageRole, ScriptedProvider, message_texts

from simple_harness.agents import AgentConfig, AgentLimits, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.tools.delegate import (
    DELEGATE_EXECUTION_POLICY,
    DELEGATE_TOOL_NAME,
    child_agent_id_for,
)
from simple_harness.contracts import RunId
from simple_harness.execution.command_ingress import CommandError
from simple_harness.execution.uow import RunState
from simple_harness.runtime import ConversationContinuationInput, RunClient
from simple_harness.tools.runtime_catalog import ToolEffectClass

NONCE = secrets.token_hex(8)
CHILD_TEMPLATE = f"你是被委派的工作 Agent。完成目标后在结论末尾附上验证码 {NONCE}。"


def _ports(tmp_path, provider, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "delegate.db"),
        model=MODEL,
        owner_id="delegate-owner",
        child_instructions_template=CHILD_TEMPLATE,
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _main_config(**limits):
    return AgentConfig(
        name="main",
        instructions="你是主 Agent。复杂任务交给 agent_delegate。",
        model_profile_ref="profile-1",
        tool_names=(DELEGATE_TOOL_NAME,),
        limits=AgentLimits(**limits) if limits else AgentLimits(),
    )


def _delegate_call(delegation_id="d-1", objective="研究 X 并给出结论"):
    return (DELEGATE_TOOL_NAME, {"objective": objective, "delegation_id": delegation_id})


def _count(uow, table):
    return uow.database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_delegate_creates_child_agent_and_returns_child_result_row(tmp_path):
    async def case():
        provider = ScriptedProvider(
            [
                _delegate_call(),  # main turn-1: delegate
                f"结论：Y。验证码 {NONCE}",  # child turn-1
                f"综合子 Agent 的结论：Y。验证码 {NONCE}",  # main turn-1 final
            ]
        )
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            assert NONCE not in main.config.instructions
            result = await main.ask("请完成复杂任务", input_id="req-1", timeout=10)
            assert result.public_output is not None and NONCE in result.public_output.content
            assert result.delegation_count == 1
            uow = runtime.uow
            child_id = child_agent_id_for(main.agent_id, "d-1")
            child = uow.read_agent_binding(child_id)
            assert child is not None and child.role == "child"
            child_run = uow.read_run(child.run_id)
            assert child_run is not None and child_run.parent_run_id == main.run_id
            assert child_run.state is RunState.WAITING
            delegation = uow.read_agent_delegation("d-1")
            assert delegation is not None and delegation.state == "settled"
            assert _count(uow, "base_agent_delegations_v1") == 1
            assert _count(uow, "child_terminal_receipts") == 0
            child_result = uow.read_agent_turn_result(f"{child_id}:input:objective")
            assert child_result is not None
            # The tool result (visible to the model) carries the child's exact result hash.
            final_request = provider.requests[2]
            texts = "\n".join(message_texts(final_request))
            assert child_result.result_hash in texts and NONCE in texts
            # NONCE never reached the parent through instructions/inputs/tool schema.
            first_request = provider.requests[0]
            assert NONCE not in "\n".join(message_texts(first_request))
            assert runtime._delegate.launches == 1
            assert provider.calls == 3

    asyncio.run(case())


def test_child_run_is_mode_fenced_before_it_exists(tmp_path):
    async def case():
        provider = ScriptedProvider([_delegate_call(), "子结论", "主结论"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            await main.ask("任务", input_id="r1", timeout=10)
            uow = runtime.uow
            child_id = child_agent_id_for(main.agent_id, "d-1")
            row = uow.database.connection.execute(
                "SELECT namespace, created_at FROM conversation_run_modes WHERE run_id=?",
                (child_id,),
            ).fetchone()
            assert row is not None and row[0] == "base-agent/v1"
            created = uow.database.connection.execute(
                "SELECT created_at FROM runs WHERE run_id=?", (child_id,)
            ).fetchone()[0]
            assert float(row[1]) <= float(created)
            with pytest.raises(CommandError):
                await RunClient(runtime.kernel).signal_conversation(
                    RunId(child_id),
                    continuation_id="x",
                    value=ConversationContinuationInput(Message(MessageRole.USER, "hi"), "hi"),
                )

    asyncio.run(case())


def test_child_catalog_excludes_delegate(tmp_path):
    async def case():
        provider = ScriptedProvider([_delegate_call(), "子结论", "主结论"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            await main.ask("任务", input_id="r1", timeout=10)
            child_id = child_agent_id_for(main.agent_id, "d-1")
            snapshot = runtime.uow.read_start_snapshot(child_id)
            tools = snapshot["input"]["capability_snapshot"]["tools"]
            assert DELEGATE_TOOL_NAME not in tools
            child = await runtime.open(child_id)
            assert DELEGATE_TOOL_NAME not in child.config.tool_names
            # The child's provider request advertised no delegate tool either.
            child_request = provider.requests[1]
            assert all(
                getattr(t, "name", "") != DELEGATE_TOOL_NAME for t in (child_request.tools or ())
            )

    asyncio.run(case())


def test_quota_rejects_second_delegation_in_same_turn(tmp_path):
    async def case():
        provider = ScriptedProvider(
            [
                _delegate_call("d-1"),
                "子结论 1",
                _delegate_call("d-2"),  # second delegation in the same parent turn
                "主结论（第二次被拒后）",
            ]
        )
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(
                _main_config(max_delegations_per_turn=1), creation_key="main"
            )
            result = await main.ask("任务", input_id="r1", timeout=10)
            assert result.delegation_count == 1
            uow = runtime.uow
            assert _count(uow, "base_agent_delegations_v1") == 1
            rejected_request = "\n".join(message_texts(provider.requests[3]))
            assert "agent_delegation_quota_exceeded" in rejected_request
            assert runtime._delegate.launches == 1

    asyncio.run(case())


def test_same_delegation_id_is_idempotent(tmp_path):
    async def case():
        provider = ScriptedProvider(
            [
                _delegate_call("d-1"),
                "子结论",
                _delegate_call("d-1"),  # same delegation_id again in the same turn
                "主结论",
            ]
        )
        async with build_agent_runtime(
            _ports(
                tmp_path,
                provider,
            ),
        ) as runtime:
            main = await runtime.create(
                _main_config(max_delegations_per_turn=1), creation_key="main"
            )
            result = await main.ask("任务", input_id="r1", timeout=10)
            assert result.delegation_count == 1
            uow = runtime.uow
            assert _count(uow, "base_agent_delegations_v1") == 1
            assert _count(uow, "base_agent_bindings_v1") == 2
            assert runtime._delegate.launches == 1
            second_reply = "\n".join(message_texts(provider.requests[3]))
            assert child_agent_id_for(main.agent_id, "d-1") in second_reply
            assert provider.calls == 4

    asyncio.run(case())


def test_one_delegation_is_exactly_one_child_turn(tmp_path):
    async def case():
        provider = ScriptedProvider([_delegate_call(), "子结论", "主结论"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            await main.ask("任务", input_id="r1", timeout=10)
            child_id = child_agent_id_for(main.agent_id, "d-1")
            turns = runtime.uow.list_agent_turns(child_id)
            assert [t.seq for t in turns] == [1] and turns[0].phase == "committed"

    asyncio.run(case())


def test_reserved_delegation_is_resumed_not_poisoned(tmp_path):
    async def case():
        provider = ScriptedProvider(
            [
                _delegate_call("d-1"),  # first attempt: crashes after the pre-fence, before launch
                _delegate_call("d-1"),  # retry in the same turn (model resends the same call)
                "子结论",
                "主结论",
            ]
        )
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(
                _main_config(max_delegations_per_turn=1), creation_key="main"
            )
            delegate = runtime._delegate
            crashed = {"count": 0}

            def crash_once(point: str) -> None:
                if point == "delegate.after_reserve" and crashed["count"] == 0:
                    crashed["count"] += 1
                    raise RuntimeError("injected crash between pre-fence and launch")

            delegate.fault = crash_once
            result = await main.ask("任务", input_id="r1", timeout=10)
            uow = runtime.uow
            assert crashed["count"] == 1
            delegation = uow.read_agent_delegation("d-1")
            assert delegation is not None and delegation.state == "settled"
            assert delegate.launches == 1
            assert _count(uow, "base_agent_delegations_v1") == 1
            assert result.delegation_count == 1

    asyncio.run(case())


def test_child_snapshot_passes_kernel_preflight(tmp_path):
    async def case():
        provider = ScriptedProvider([_delegate_call(), "子结论", "主结论"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            await main.ask("任务", input_id="r1", timeout=10)
            child_id = child_agent_id_for(main.agent_id, "d-1")
            kinds = [
                str(r[0])
                for r in runtime.uow.database.connection.execute(
                    "SELECT kind FROM run_events WHERE run_id=?", (child_id,)
                )
            ]
            assert "run.failed" not in kinds
            snapshot = runtime.uow.read_start_snapshot(child_id)
            assert snapshot["profile_key"] == "agent.base"
            assert snapshot["driver_kind"] == "base_agent"
            assert snapshot["policy_fingerprint"] == runtime.driver.policy_fingerprint

    asyncio.run(case())


def test_delegate_timeout_returns_visible_result_not_raise(tmp_path):
    async def case():
        provider = ScriptedProvider([_delegate_call(), "子结论（很慢）", "主结论：子 Agent 超时"])
        original_invoke = provider.invoke
        gate = asyncio.Event()

        async def slow_child(request, *, cancel):
            texts = "\n".join(message_texts(request))
            if "研究 X" in texts and CHILD_TEMPLATE in texts:
                await gate.wait()
            return await original_invoke(request, cancel=cancel)

        provider.invoke = slow_child  # type: ignore[method-assign]
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(
                _main_config(delegation_wait_seconds=0.2), creation_key="main"
            )
            result = await main.ask("任务", input_id="r1", timeout=10)
            assert result.state.value == "committed"
            uow = runtime.uow
            delegation = uow.read_agent_delegation("d-1")
            assert delegation is not None and delegation.state == "launched"
            final_request = "\n".join(message_texts(provider.requests[-1]))
            assert "agent_delegation_timeout" in final_request
            assert runtime._delegate.launches == 1
            gate.set()
            child = await runtime.open(child_agent_id_for(main.agent_id, "d-1"))
            late = await child.wait_turn(f"{child.agent_id}:input:objective", timeout=5)
            assert late.public_output is not None

    asyncio.run(case())


def test_delegate_is_non_project_effect():
    assert DELEGATE_EXECUTION_POLICY.effect_class is ToolEffectClass.NON_PROJECT_EFFECT
    assert DELEGATE_EXECUTION_POLICY.capability_id == DELEGATE_TOOL_NAME


# --- LLM behaviour variants (acceptance V1–V5) --------------------------------


def test_unknown_delegation_id_is_rejected_not_created(tmp_path):
    async def case():
        provider = ScriptedProvider(
            [
                _delegate_call("d-1"),
                "子结论 1",
                "主结论 1",  # turn 1 owns d-1
                _delegate_call("d-1"),
                "主结论 2",  # turn 2 reuses d-1 → rejected
            ]
        )
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            await main.ask("任务一", input_id="r1", timeout=10)
            await main.ask("任务二", input_id="r2", timeout=10)
            uow = runtime.uow
            assert _count(uow, "base_agent_delegations_v1") == 1
            assert "agent_delegation_unknown_id" in "\n".join(message_texts(provider.requests[-1]))
            assert runtime._delegate.launches == 1

    asyncio.run(case())


def test_malformed_arguments_never_create_child(tmp_path):
    async def case():
        provider = ScriptedProvider(
            [
                (DELEGATE_TOOL_NAME, {"objective": "x", "delegation_id": "d-1", "extra": 1}),
                (DELEGATE_TOOL_NAME, {"objective": 12, "delegation_id": "d-2"}),
                (DELEGATE_TOOL_NAME, {"delegation_id": "d-3"}),
                (
                    DELEGATE_TOOL_NAME,
                    {"objective": "x", "delegation_id": "d-4", "role_hint": "boss"},
                ),
                "主结论",
            ]
        )
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(
                _main_config(max_delegations_per_turn=5), creation_key="main"
            )
            result = await main.ask("任务", input_id="r1", timeout=10)
            uow = runtime.uow
            assert result.delegation_count == 0
            assert _count(uow, "base_agent_delegations_v1") == 0
            assert _count(uow, "base_agent_bindings_v1") == 1
            assert _count(uow, "runs") == 1
            assert runtime._delegate.launches == 0

    asyncio.run(case())


def test_oversize_objective_is_rejected_before_launch(tmp_path):
    async def case():
        provider = ScriptedProvider([_delegate_call("d-1", objective="x" * 9000), "主结论"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            await main.ask("任务", input_id="r1", timeout=10)
            uow = runtime.uow
            assert _count(uow, "base_agent_delegations_v1") == 0
            assert _count(uow, "runs") == 1
            assert runtime._delegate.launches == 0

    asyncio.run(case())


def test_oversize_child_result_returns_ref_not_truncated_fact(tmp_path):
    async def case():
        big = "长" * 20000
        provider = ScriptedProvider([_delegate_call(), big, "主结论"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            await main.ask("任务", input_id="r1", timeout=10)
            uow = runtime.uow
            child_id = child_agent_id_for(main.agent_id, "d-1")
            stored = uow.read_agent_turn_result(f"{child_id}:input:objective")
            from simple_harness.agents import AgentTurnResult

            assert AgentTurnResult.from_json(stored.result_json).public_output.content == big
            final_request = "\n".join(message_texts(provider.requests[-1]))
            assert "result_ref" in final_request
            assert f"base_agent_turn_result:{child_id}" in final_request
            assert big not in final_request

    asyncio.run(case())


def test_no_delegation_still_commits_a_valid_turn(tmp_path):
    async def case():
        provider = ScriptedProvider(["我自己回答了"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            result = await main.ask("任务", input_id="r1", timeout=10)
            assert result.state.value == "committed" and result.delegation_count == 0
            uow = runtime.uow
            assert _count(uow, "base_agent_delegations_v1") == 0
            assert _count(uow, "runs") == 1

    asyncio.run(case())


def test_delegate_unknown_reconciles_from_child_result_row(tmp_path):
    """AC9/DG04: an UNKNOWN delegate effect settles from the child'
    s result row, never relaunches.
    """
    from simple_harness.agents.tools.delegate import AgentDelegationReconciliation
    from simple_harness.tools.reconciliation import ReconciliationState

    class _Effect:
        def __init__(self, arguments, call_id):
            self.tool_name = DELEGATE_TOOL_NAME
            self.arguments = arguments
            self.call_id = call_id

    class _Fallback:
        def __init__(self):
            self.seen = []

        async def observe(self, effect):
            self.seen.append(effect)
            raise AssertionError("delegate effects must not reach the fallback")

    async def case():
        provider = ScriptedProvider([_delegate_call(), "子结论", "主结论"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            main = await runtime.create(_main_config(), creation_key="main")
            await main.ask("任务", input_id="r1", timeout=10)
            uow = runtime.uow
            from simple_harness.contracts import CallId

            reconciliation = AgentDelegationReconciliation(uow, _Fallback())
            observed = await reconciliation.observe(
                _Effect({"objective": "x", "delegation_id": "d-1"}, CallId("call-x"))
            )
            assert observed.state is ReconciliationState.COMPLETED
            child_id = child_agent_id_for(main.agent_id, "d-1")
            stored = uow.read_agent_turn_result(f"{child_id}:input:objective")
            assert observed.evidence_ref == stored.commit_receipt_id
            assert observed.result is not None
            assert dict(observed.result.value)["result_hash"] == stored.result_hash
            pending = await reconciliation.observe(
                _Effect({"objective": "x", "delegation_id": "d-nope"}, CallId("call-y"))
            )
            assert pending.state is ReconciliationState.STILL_UNKNOWN
            assert pending.evidence_ref == "base_agent_delegation:d-nope:pending"
            assert runtime._delegate.launches == 1  # observing never launches

    asyncio.run(case())
