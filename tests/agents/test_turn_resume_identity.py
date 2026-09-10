# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T7 (AC3 / AC4): UNKNOWN and authorization waits resume the *same* turn."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from kernel_fixture import create_agent, submit
from provider_fixture import MODEL, ScriptedProvider
from tool_fixture import ECHO_SCHEMA, EchoToolExecutor

from simple_harness.agents import (
    AgentConfig,
    AgentLimits,
    AgentTurnState,
    AgentTurnTimeout,
    build_agent_runtime,
)
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.runtime import assemble_runtime
from simple_harness.contracts import RunId, canonical_json, thaw_json
from simple_harness.execution.uow import ContinuationState, RunState
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
from simple_harness.tools.authorization import (
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationRequest,
    AuthorizationResult,
)

TOOL = ("echo", {})


def _ports(tmp_path, provider, executor=None, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="identity-owner",
    )
    if executor is not None:
        base.update(
            tool_executor=executor, tool_names=("echo",), tool_schemas={"echo": ECHO_SCHEMA}
        )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config(**limits):
    return AgentConfig(
        name="w",
        instructions="你是助手。",
        model_profile_ref="p",
        tool_names=("echo",),
        limits=AgentLimits(**limits),
    )


class NotStarted:
    def __init__(self):
        self.observed = 0

    async def observe(self, invocation):
        self.observed += 1
        return ProviderReconciliationObservation(
            ProviderReconciliationState.CONFIRMED_NOT_STARTED,
            f"test-evidence:{invocation.invocation_id}",
        )


def _policies(reconciliation):
    return ConsumerRuntimePolicies(
        "unpriced_local",
        False,
        "consumer_reconciles",
        tool_reconciliation=_DefaultToolReconciliation(),
        provider_reconciliation=reconciliation,
        runtime_reconciliation=_DefaultRuntimeReconciliation(),
    )


def _rows(uow, sql, *params):
    return uow.database.connection.execute(sql, params).fetchall()


def _checkpoint(uow, run_id):
    stored = uow.read_react_checkpoint(run_id)
    return None if stored is None else thaw_json(stored.checkpoint)


async def _until(predicate, *, tries=100):
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def test_unknown_provider_resumes_the_same_turn_with_the_same_request_identity(tmp_path):
    async def case():
        provider = ScriptedProvider(["恢复后的回答"])
        original = provider.invoke
        state = {"failed": False}

        async def transport_failure_once(request, *, cancel):
            if not state["failed"]:
                state["failed"] = True
                raise ProviderTransportError()
            return await original(request, cancel=cancel)

        provider.invoke = transport_failure_once  # type: ignore[method-assign]
        reconciliation = NotStarted()
        async with build_agent_runtime(
            _ports(tmp_path, provider, policies=_policies(reconciliation))
        ) as runtime:
            agent = await runtime.create(_config(), creation_key="u")
            receipt = await agent.submit("问", input_id="i1")
            uow = runtime.uow
            assert await _until(lambda: uow.list_open_wait_blockers_for_run(agent.run_id))
            # (a) blocked turn: running, checkpoint in flight with the frozen request id.
            snapshot = agent.turn_snapshot(receipt.turn_id)
            assert snapshot.state is AgentTurnState.RUNNING and snapshot.blocked
            assert snapshot.blocker is not None and snapshot.blocker["kind"] == "provider"
            assert agent.get_result(receipt.turn_id) is None
            before = _checkpoint(uow, agent.run_id)
            assert before["phase"] == "provider_reserved"
            request_id = before["provider_request_id"]
            assert request_id == f"{agent.run_id}:provider-turn:1"
            invocations_before = _rows(
                uow, "SELECT invocation_id FROM provider_invocations WHERE run_id=?", agent.run_id
            )
            assert len(invocations_before) == 1
            # (b) reconcile: the same turn resumes, the same invocation is reused.
            await runtime.kernel.reconcile()
            result = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert result.state is AgentTurnState.COMMITTED
            assert result.public_output.content == "恢复后的回答"
            assert result.usage_refs == (f"provider-request:{request_id}",)
            invocations_after = _rows(
                uow, "SELECT invocation_id FROM provider_invocations WHERE run_id=?", agent.run_id
            )
            assert invocations_after == invocations_before
            continuation = uow.read_continuation(receipt.turn_id)
            assert continuation is not None and continuation.state is ContinuationState.ACKED
            acks = _rows(
                uow,
                "SELECT COUNT(*) FROM continuation_progress_receipts WHERE continuation_id=?",
                receipt.turn_id,
            )[0][0]
            assert acks == 1
            assert agent.turn_snapshot(receipt.turn_id).seq == 1
            after = agent.turn_snapshot(receipt.turn_id)
            assert after.blocked is False and after.provider_turn_ordinal_from == 0
            assert after.provider_turn_ordinal_to == 1
            assert provider.calls == 1 and reconciliation.observed >= 1

    asyncio.run(case())


class RequireUserOnce:
    """SDK-native authorization port: first call needs a user decision, later calls allow."""

    def __init__(self) -> None:
        self.prepares = 0
        self.binds = 0

    async def prepare(self, prepared):  # type: ignore[no-untyped-def]
        self.prepares += 1
        if self.prepares == 1:
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_USER,
                reason_code="confirmation_required",
                public_message="Confirm.",
                request=AuthorizationRequest("Run echo?", "nonce-1", None),
            )
        return AuthorizationResult(
            AuthorizationDecision.ALLOW, receipt_ref=f"allow:{prepared.effect_id.value}"
        )

    async def bind_decision(self, prepared, request, decision, sdk_receipt):  # type: ignore[no-untyped-def]
        del prepared, request, decision
        self.binds += 1
        ref = f"host:decision:{self.binds}"
        return AuthorizationReceipt(
            ref, hashlib.sha256(ref.encode()).hexdigest(), sdk_receipt.receipt_hash
        )

    async def bind_effect_handoff(self, prepared, authorization_receipt_ref, sdk_receipt):  # type: ignore[no-untyped-def]
        del prepared, authorization_receipt_ref
        ref = "host:handoff"
        return AuthorizationReceipt(
            ref, hashlib.sha256(ref.encode()).hexdigest(), sdk_receipt.receipt_hash
        )


def test_authorization_wait_resumes_the_same_turn(tmp_path):
    async def case():
        provider = ScriptedProvider([TOOL, "工具之后"])
        executor = EchoToolExecutor()
        authorization = RequireUserOnce()
        async with build_agent_runtime(
            _ports(tmp_path, provider, executor, authorization=authorization)
        ) as runtime:
            agent = await runtime.create(_config(), creation_key="auth")
            receipt = await agent.submit("要工具", input_id="i1")
            uow = runtime.uow

            def open_decision():
                return _rows(
                    uow,
                    "SELECT decision_id, version FROM decisions WHERE run_id=? AND state='open'",
                    agent.run_id,
                )

            assert await _until(lambda: bool(open_decision()))
            decision_id, version = open_decision()[0]
            assert agent.turn_state(receipt.turn_id) is AgentTurnState.RUNNING
            assert agent.get_result(receipt.turn_id) is None
            assert executor.calls == []
            decision = uow.read_decision(decision_id)
            await runtime.kernel.client.decide_authorization(
                RunId(agent.run_id),
                decision_id=decision_id,
                nonce=str(thaw_json(decision.request)["nonce"]),
                expected_version=version,
                decision=AuthorizationDecision.ALLOW,
            )
            result = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert result.state is AgentTurnState.COMMITTED
            assert result.public_output.content == "工具之后"
            effects_after = _rows(
                uow, "SELECT effect_id, call_id FROM execution_effects WHERE run_id=?", agent.run_id
            )
            assert len(effects_after) == 1  # one durable effect for the one authorized call
            assert executor.calls == ["echo"]
            assert authorization.prepares == 1 and authorization.binds == 1
            decided = uow.read_decision(decision_id)
            assert decided is not None and decided.state.value == "allowed"
            assert provider.calls == 2
            assert agent.turn_snapshot(receipt.turn_id).seq == 1
            assert len(agent.history()) == 1

    asyncio.run(case())


def test_resuming_a_different_turn_on_an_inflight_checkpoint_is_refused(tmp_path):
    async def case():
        provider = ScriptedProvider(["一", "不该被调用"])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-ident")
            first = await submit(runtime, uow, agent_id="agent-ident", input_id="i1", text="一")
            assert await _until(lambda: uow.read_agent_turn_result(first.turn_id) is not None)
            await _until(lambda: "agent-ident" not in runtime._leases)
            # Forge an in-flight checkpoint reserved by "someone else": total (1) is not
            # above the next turn's first-admission baseline (1).
            stored = uow.read_react_checkpoint("agent-ident")
            payload = dict(thaw_json(stored.checkpoint))
            payload.update(
                phase="provider_reserved",
                provider_request_id="agent-ident:provider-turn:1",
                provider_request_snapshot={"forged": True},
                provider_request_fingerprint="0" * 64,
            )
            forged = canonical_json(payload)
            uow.database.connection.execute(
                "UPDATE workflow_checkpoints SET checkpoint_json=?, checkpoint_hash=? "
                "WHERE run_id=? AND namespace='react.termination.v1' AND version=?",
                (
                    forged,
                    hashlib.sha256(forged.encode()).hexdigest(),
                    "agent-ident",
                    stored.version,
                ),
            )
            uow.database.connection.commit()
            second = await submit(runtime, uow, agent_id="agent-ident", input_id="i2", text="二")
            assert await _until(
                lambda: uow.read_run("agent-ident").state is RunState.FAILED, tries=150
            )
            events = _rows(
                uow,
                "SELECT payload_json FROM run_events WHERE run_id=? AND kind='run.failed'",
                "agent-ident",
            )
            assert events and "base_agent_turn_identity_conflict" in str(events[0][0])
            assert provider.calls == 1
            assert uow.read_agent_turn_result(second.turn_id) is None

    asyncio.run(case())


def test_checkpoint_stays_single_key_and_ordinals_never_repeat(tmp_path):
    async def case():
        provider = ScriptedProvider([TOOL, "一", TOOL, TOOL, "二"])
        async with build_agent_runtime(_ports(tmp_path, provider, EchoToolExecutor())) as runtime:
            agent = await runtime.create(_config(), creation_key="single")
            r1 = await agent.ask("一", input_id="i1", timeout=5)
            r2 = await agent.ask("二", input_id="i2", timeout=5)
            assert r1.state is AgentTurnState.COMMITTED and r2.state is AgentTurnState.COMMITTED
            namespaces = _rows(
                runtime.uow,
                "SELECT DISTINCT namespace FROM workflow_checkpoints WHERE run_id=? "
                "AND namespace LIKE 'react.termination%'",
                agent.run_id,
            )
            # D7: one run-level termination checkpoint key, never one per turn.
            assert [row[0] for row in namespaces] == ["react.termination.v1"]
            s1, s2 = agent.turn_snapshot(r1.turn_id), agent.turn_snapshot(r2.turn_id)
            assert (s1.provider_turn_ordinal_from, s1.provider_turn_ordinal_to) == (0, 2)
            assert (s2.provider_turn_ordinal_from, s2.provider_turn_ordinal_to) == (2, 5)
            requests = _rows(
                runtime.uow,
                "SELECT request_id FROM provider_invocations WHERE run_id=? ORDER BY request_id",
                agent.run_id,
            )
            ids = [row[0] for row in requests]
            assert len(ids) == len(set(ids)) == 5
            assert _checkpoint(runtime.uow, agent.run_id)["phase"] == "ready"

    asyncio.run(case())


def test_ask_timeout_carries_the_receipt_and_caller_cancel_does_not_cancel_the_turn(tmp_path):
    async def case():
        provider = ScriptedProvider(["慢回答"], blocked=True)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="slow")
            with pytest.raises(AgentTurnTimeout) as info:
                await agent.ask("慢一点", input_id="i1", timeout=0.05)
            error = info.value
            assert error.receipt is not None
            assert error.receipt.turn_id == error.turn_id
            assert error.receipt.seq == 1 and error.receipt.input_id == "i1"
            phase_before = agent.turn_state(error.turn_id)
            waiter = asyncio.create_task(agent.wait_turn(error.turn_id, timeout=5))
            await asyncio.sleep(0.02)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert agent.turn_state(error.turn_id) == phase_before
            provider.allow.set()
            result = await agent.wait_turn(error.turn_id, timeout=5)
            assert result.public_output.content == "慢回答"
            assert provider.calls == 1

    asyncio.run(case())


def test_turn_snapshot_reports_blocked_with_a_reason(tmp_path):
    async def case():
        provider = ScriptedProvider(["答"])
        original = provider.invoke
        state = {"failed": False}

        async def once(request, *, cancel):
            if not state["failed"]:
                state["failed"] = True
                raise ProviderTransportError()
            return await original(request, cancel=cancel)

        provider.invoke = once  # type: ignore[method-assign]
        async with build_agent_runtime(
            _ports(tmp_path, provider, policies=_policies(NotStarted()))
        ) as runtime:
            agent = await runtime.create(_config(), creation_key="snap")
            receipt = await agent.submit("问", input_id="i1")
            uow = runtime.uow
            assert await _until(lambda: uow.list_open_wait_blockers_for_run(agent.run_id))
            snapshot = agent.turn_snapshot(receipt.turn_id)
            assert snapshot.blocked is True and snapshot.blocker["kind"]
            assert agent.get_result(receipt.turn_id) is None
            await runtime.kernel.reconcile()
            await agent.wait_turn(receipt.turn_id, timeout=5)
            done = agent.turn_snapshot(receipt.turn_id)
            assert done.blocked is False and done.state is AgentTurnState.COMMITTED
            assert json.dumps(done.blocker) == "null"

    asyncio.run(case())
