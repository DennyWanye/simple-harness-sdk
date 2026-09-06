"""Real public Memory + public Harness consumer; no SQL/private SDK/test helper.

Provider is a deterministic local consumer port, not a model or external network.
Counts are corroborating observations: the public durable Harness grant/state is
asserted inside the actual Provider callback and after close/reopen/replay.
"""

import asyncio
import dataclasses as dc
import hashlib
import json

import pytest

import simple_harness as h

from .context_use_public_fixture import PublicMemoryFixture


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class SnapshotAuthority:
    def __init__(self, fixture, fragments, mode="normal"):
        self.fixture, self.fragments, self.mode = fixture, fragments, mode
        self.requests = []

    async def prepare_snapshot(self, request):
        self.requests.append(request)
        messages = (h.Message(h.MessageRole.USER, "Use my two known preferences."),) + tuple(
            h.Message(h.MessageRole.SYSTEM, canonical(h.thaw_json(f.public_payload)).decode())
            for f in self.fragments
        )
        # Independent serialization of the real Provider request, with no receipt fields.
        raw_messages = [
            dict(role=m.role.value, content=m.content, name=None, call_id=None, metadata={})
            for m in messages
        ]
        payload = dict(
            messages=raw_messages, tools=[], temperature=None, max_output_tokens=128, metadata={}
        )
        bindings = tuple((i + 2, digest(raw_messages[i + 1])) for i in range(2))
        if self.mode == "wrong_message":
            bindings = ((2, "0" * 64), bindings[1])
        intent = h.RecallContextUseIntentV1(self.fragments, bindings)
        return h.RunContextSnapshot(
            "snapshot-1",
            request.run_id.value,
            request.provider_turn_ordinal,
            request.prior_context_revision,
            1,
            {"memory": 1},
            messages,
            (),
            None,
            128,
            {},
            digest(payload),
            schema_version=1 if self.mode == "legacy" else 2,
            recall_subject=None if self.mode == "legacy" else self.fixture.principal.actor_id,
            recall_intents=None if self.mode == "legacy" else (intent,),
        )


class UnusedTools:
    async def execute(self, call, context):
        raise AssertionError("no tools requested")


class UnusedAuthorization:
    async def request_authorization(self, request):
        raise AssertionError("no tool authorization requested")


class Provider:
    def __init__(self, fixture):
        self.fixture, self.calls, self.client = fixture, [], None

    async def invoke(self, request, *, cancel):
        view = self.client.read_provider_context_use(
            h.RunId(self.fixture.run_id), request.request_id
        )
        assert view.invocation_state == "handed_off"
        assert view.handoff_attempt == 1
        assert len(view.receipts) == 1
        assert view.receipts[0] == self.fixture.receipts[-1]
        assert len(view.requests[0].snapshot_fragment_bindings) == 2
        assert view.requested_at == self.fixture.requests[-1].requested_at
        assert view.turn_id == self.fixture.turn_id
        self.calls.append((request, view))
        from simple_harness.providers import ProviderResponse, ProviderUsage

        return ProviderResponse(
            request.request_id,
            h.Message(h.MessageRole.ASSISTANT, "Preferences used."),
            usage=ProviderUsage(20, 5, 25),
            model="consumer-model",
            finish_reason="stop",
        )


@pytest.mark.parametrize("mode", ["receipt_first", "suppression_first", "legacy", "wrong_message"])
def test_actual_two_item_public_consumer_and_reopen(tmp_path, mode):
    async def run():
        fixture = await PublicMemoryFixture(tmp_path / "memory.sqlite").open()
        try:
            await fixture.seed()
            fragments = await fixture.recall()
            assert {f.recall_binding.item_id for f in fragments} == set(fixture.created)
            snapshot = SnapshotAuthority(fixture, fragments, mode)
            if mode == "receipt_first":
                fixture.after_authorize = fixture.forget
            elif mode == "suppression_first":
                fixture.before_authorize = fixture.forget
            provider = Provider(fixture)
            ports = h.ConsumerRuntimePorts(
                provider=provider,
                tool_executor=UnusedTools(),
                authorization=UnusedAuthorization(),
                database_path=str(tmp_path / "execution.sqlite"),
                run_context_authority=snapshot,
                recall_context_use_authority=fixture,
            )
            start = h.RunStart(
                h.ExecutionSessionId("session-1"),
                h.RunId(fixture.run_id),
                h.RequestId("root-request"),
                turn_id=fixture.turn_id,
                tool_catalog_generation=1,
                input={
                    "messages": [{"role": "user", "content": "Use my two known preferences."}],
                    "capability_snapshot": {"tools": []},
                    "max_output_tokens": 128,
                },
            )
            runtime = await h.build_consumer_runtime(ports)
            try:
                await runtime.__aenter__()
                provider.client = h.RunClient(runtime)
                await provider.client.start(start)
                await runtime.wait_idle(start.run_id)
                result = provider.client.query(start.run_id)
                if mode != "receipt_first":
                    assert len(provider.calls) == 0
                    assert result.state.value != "completed"
                    assert not fixture.receipts
                    return
                assert result.state.value == "completed"
                assert len(provider.calls) == 1
                before = provider.client.read_provider_context_use(
                    start.run_id, h.RequestId(fixture.run_id + ":provider-turn:1")
                )
                assert before.invocation_state == "succeeded"
                assert before.receipts[0] == fixture.receipts[0]
                await provider.client.start(
                    start
                )  # Replay durable completed root, not a counter-only wrapper.
                await runtime.wait_idle(start.run_id)
                assert len(provider.calls) == 1
            finally:
                await runtime.__aexit__(None, None, None)
            await fixture.close()
            await fixture.open()
            reopened = await h.build_consumer_runtime(ports)
            try:
                await reopened.__aenter__()
                provider.client = h.RunClient(reopened)
                after = provider.client.read_provider_context_use(
                    start.run_id, h.RequestId(fixture.run_id + ":provider-turn:1")
                )
                assert after == before
                await provider.client.start(start)
                await reopened.wait_idle(start.run_id)
                assert len(provider.calls) == 1
                # New attempt cannot borrow the old receipt after suppression.
                original = fixture.requests[0]
                with pytest.raises(Exception):
                    await fixture.manager.authorize_recall_context_use(
                        principal=fixture.principal,
                        request=dc.replace(
                            original, provider_attempt_id="new-attempt-after-forget"
                        ),
                        now=original.requested_at,
                    )
                replay = await fixture.manager.authorize_recall_context_use(
                    principal=fixture.principal, request=original, now=original.requested_at
                )
                assert replay == before.receipts[0]
            finally:
                await reopened.__aexit__(None, None, None)
        finally:
            await fixture.close()

    asyncio.run(run())


def test_real_public_continue_command_uses_accepted_turn_not_continuation_label(tmp_path):
    async def run():
        fixture = await PublicMemoryFixture(tmp_path / "memory.sqlite").open()
        try:
            await fixture.seed()
            fixture.turn_id = "actual-continued-turn-2"
            fragments = await fixture.recall(turn_id=fixture.turn_id)
            snapshot = SnapshotAuthority(fixture, fragments)
            provider = Provider(fixture)
            ports = h.ConsumerRuntimePorts(
                provider=provider,
                tool_executor=UnusedTools(),
                authorization=UnusedAuthorization(),
                database_path=str(tmp_path / "execution.sqlite"),
                run_context_authority=snapshot,
                recall_context_use_authority=fixture,
            )
            runtime = continuation_runtime(ports)
            try:
                await runtime.__aenter__()
                client = h.RunClient(runtime)
                provider.client = client
                run_id = h.RunId(fixture.run_id)
                identity = h.AgentIdentity(
                    "fixture-deployment", "fixture-household", "subject-1", "session-1"
                )
                first = h.StartCommandIntent(
                    "fixture-namespace",
                    "fixture-projection",
                    "start-command",
                    run_id,
                    h.RequestId("root-command-request"),
                    "actual-root-turn",
                    h.ConversationTurnInput(
                        identity, h.Message(h.MessageRole.USER, "root message"), "root message"
                    ),
                    input={
                        "messages": [{"role": "user", "content": "root message"}],
                        "capability_snapshot": {"tools": []},
                        "max_output_tokens": 128,
                    },
                )
                next_command = h.ContinueCommandIntent(
                    "fixture-namespace",
                    "fixture-projection",
                    "continue-command",
                    run_id,
                    "distinct-continuation-label",
                    fixture.turn_id,
                    h.ConversationContinuationInput(
                        h.Message(h.MessageRole.USER, "continue message"), "continue message"
                    ),
                )
                await client.submit_start(first)
                for _ in range(200):
                    waiting = client.query(run_id)
                    if waiting is not None and waiting.state.value == "waiting":
                        break
                    await asyncio.sleep(0.01)
                assert waiting.state.value == "waiting"
                accepted = await client.submit_continue(next_command)
                assert accepted.intent_hash == next_command.intent_hash
                await runtime.wait_idle(run_id)
                for _ in range(200):
                    current = await client.get_command(next_command.command_id)
                    if current.receipt.state.value in ("applied", "rejected", "cancelled"):
                        break
                    await asyncio.sleep(0.01)
                await runtime.wait_idle(run_id)
                assert current.receipt.state.value == "applied"
                assert client.query(run_id).state.value == "completed"
                assert len(provider.calls) == 1
                view = provider.calls[0][1]
                assert view.turn_id == fixture.turn_id
                assert view.continuation_id == "distinct-continuation-label"
                assert view.receipts[0].turn_id != first.turn_id
                assert snapshot.requests[0].continuation_id == view.continuation_id
                assert snapshot.requests[0].turn_id == view.turn_id
                with pytest.raises(ValueError):
                    view.receipts[0].validate_request(
                        dc.replace(fixture.requests[0], turn_id=first.turn_id)
                    )
            finally:
                await runtime.__aexit__(None, None, None)
        finally:
            await fixture.close()

    asyncio.run(run())


def continuation_runtime(ports):
    """Public kernel composition with a test-only ordinary WAITING driver boundary.

    It creates no receipt/grant/reservation. The actual resumed call delegates to
    the unmodified production ReAct driver and real receipt-bound coordinator.
    """
    from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
    from simple_harness.execution.delivery import DeliveryDispatcher
    from simple_harness.execution.dispatch import ProviderInvocationCoordinator
    from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
    from simple_harness.execution.uow import RunState
    from simple_harness.providers import ProviderTarget
    from simple_harness.runtime.drivers.react import ReActDriver
    from simple_harness.runtime.kernel import DriverResult
    from simple_harness.tools import EffectExecutor, ToolRegistry

    database = Database.open(ports.database_path)
    uow = SqliteExecutionUnitOfWork(database)
    provider = ports.provider
    provider.target = ProviderTarget("fixture", "consumer-model", "model", "local", "fixture")
    coordinator = ProviderInvocationCoordinator(
        uow=uow,
        provider=provider,
        budget_policy=BudgetPolicy(),
        estimator=FrozenPriceEstimator("fixture-price", "model", 0, 0),
        context_use_authority=ports.recall_context_use_authority,
    )

    class Unused:
        async def reconcile(self, *args, **kwargs):
            return None

        async def observe(self, *args, **kwargs):
            raise AssertionError("no effects")

        def current_generation(self):
            return 1

    unused = Unused()
    effects = EffectExecutor(
        uow=uow, registry=ToolRegistry(), authorization=unused, reconciliation=unused
    )

    class WaitingThenReAct:
        async def start(self, invocation, *, context, cancel):
            if not invocation.continuations:
                return DriverResult(RunState.WAITING, {"fixture_wait": "public_continue"})
            return await ReActDriver().start(invocation, context=context, cancel=cancel)

    return h.build_runtime(
        uow,
        {"agent.general": h.RuntimeProfile("agent.general", "react")},
        {"react": WaitingThenReAct()},
        h.RuntimePorts(
            provider=coordinator,
            tools=effects,
            authorization=unused,
            context=h.SqliteContextPort(database),
            delivery=DeliveryDispatcher(uow, {}),
            tool_reconciliation=unused,
            reconciliation=unused,
            provider_reconciliation=unused,
            react_checkpoint=uow,
            tool_catalog=unused,
            run_context_authority=ports.run_context_authority,
        ),
        close_hook=uow.close,
    )
