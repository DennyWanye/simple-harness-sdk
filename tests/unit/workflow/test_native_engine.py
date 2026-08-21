# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping

import pytest

import simple_harness.workflow.native as native_module
from simple_harness.workflow.contracts import (
    ChannelSpec,
    JsonType,
    ReducerKind,
    RetryPolicy,
    StatePatch,
    WorkflowContext,
)
from simple_harness.workflow.control import WorkflowSuspended, workflow_interrupt
from simple_harness.workflow.definition import (
    ConditionalEdge,
    Edge,
    NodeDefinition,
    NodeDispatch,
    WorkflowDefinition,
    compile_workflow,
)
from simple_harness.workflow.errors import WorkflowErrorCode, WorkflowNodeError
from simple_harness.workflow.native import (
    TERMINAL_REQUEST_FACTORY_HASH,
    TERMINAL_REQUEST_SCHEMA_HASH,
    InMemoryNativeCheckpointStore,
    NativeExecutionPolicy,
    NativeWorkflowExecutable,
    TerminalProjectionDescriptor,
)


class _TerminalProjectionPort:
    def project_public(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs


class _TerminalCommitProjectionPort:
    def lookup(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs


def _native(compiled, store):  # type: ignore[no-untyped-def]
    return NativeWorkflowExecutable(
        compiled,
        store,
        terminal_projection_port=_TerminalProjectionPort(),
        terminal_commit_projection_port=_TerminalCommitProjectionPort(),
    )


def _channel(writer: str, value_type: JsonType = JsonType.STRING) -> ChannelSpec:
    return ChannelSpec(value_type, ReducerKind.SINGLE_WRITER, frozenset({writer}))


def _executable(
    definition: WorkflowDefinition,
) -> tuple[NativeWorkflowExecutable, InMemoryNativeCheckpointStore]:
    store = InMemoryNativeCheckpointStore()
    return _native(compile_workflow(definition), store), store


def test_native_engine_executes_linear_graph_to_terminal() -> None:
    async def start(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"first": "done"})

    async def finish(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "ok"})

    executable, store = _executable(
        WorkflowDefinition(
            "linear",
            "1",
            1,
            "start",
            (NodeDefinition("start", start), NodeDefinition("finish", finish)),
            {"first": _channel("start"), "answer": _channel("finish")},
            16,
            8,
            edges=(Edge("start", "finish"), Edge("finish", "__end__")),
        )
    )
    result = asyncio.run(
        executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="run")
    )
    assert result == {"first": "done", "answer": "ok"}
    assert store.snapshot is not None and store.snapshot.frontier == ()
    assert set(store.materialized_intents) == {"run:terminal"}


def test_native_engine_persists_conditional_route_before_frontier() -> None:
    calls = 0

    async def start(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"choice": "right"})

    def route(state, context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        assert not any(callable(value) for value in vars(context).values())
        return str(state["choice"])

    async def selected(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "selected"})

    executable, store = _executable(
        WorkflowDefinition(
            "route",
            "1",
            1,
            "start",
            (NodeDefinition("start", start), NodeDefinition("right", selected)),
            {"choice": _channel("start"), "answer": _channel("right")},
            16,
            8,
            edges=(Edge("right", "__end__"),),
            conditional_edges=(
                ConditionalEdge("start", route, {"right": "right"}, selector_effect_policy="pure"),
            ),
        )
    )
    result = asyncio.run(
        executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="route-run")
    )
    assert isinstance(result, Mapping)
    assert result["answer"] == "selected"
    assert calls == 1
    assert store.snapshot is not None and store.snapshot.frontier == ()


def test_route_receives_only_frozen_checkpoint_time_and_immutable_state() -> None:
    seen_times: list[float] = []

    async def start(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"choice": "done", "logical_timestamp": 999.0, "nested": {"safe": True}})

    def route(state, context):  # type: ignore[no-untyped-def]
        seen_times.extend([state["logical_timestamp"], context.logical_timestamp])
        with pytest.raises(TypeError):
            state["nested"]["safe"] = False
        with pytest.raises(TypeError):
            context.state["nested"]["safe"] = False
        return "done"

    async def done(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "done"})

    executable, _store = _executable(
        WorkflowDefinition(
            "route-time",
            "1",
            1,
            "start",
            (NodeDefinition("start", start), NodeDefinition("done", done)),
            {
                "choice": _channel("start"),
                "logical_timestamp": _channel("start", JsonType.NUMBER),
                "nested": _channel("start", JsonType.OBJECT),
                "answer": _channel("done"),
            },
            16,
            8,
            edges=(Edge("done", "__end__"),),
            conditional_edges=(
                ConditionalEdge("start", route, {"done": "done"}, selector_effect_policy="pure"),
            ),
        )
    )
    result = asyncio.run(
        executable.ainvoke(
            {},
            WorkflowContext(),
            thread_id="thread",
            run_id="route-time",
            configurable={"logical_timestamp": 10.0},
        )
    )
    assert isinstance(result, Mapping) and result["answer"] == "done"
    assert seen_times == [10.0, 10.0]
    assert result["logical_timestamp"] == 999.0


def test_native_engine_parallel_frontier_merges_deterministically() -> None:
    async def root(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({})

    async def left(_state, _context):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        return StatePatch({"left": "L"})

    async def right(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"right": "R"})

    executable, _store = _executable(
        WorkflowDefinition(
            "parallel",
            "1",
            1,
            "root",
            (
                NodeDefinition("root", root),
                NodeDefinition("left", left, dispatch=NodeDispatch.PARALLEL),
                NodeDefinition("right", right, dispatch=NodeDispatch.PARALLEL),
            ),
            {"left": _channel("left"), "right": _channel("right")},
            16,
            8,
            edges=(
                Edge("root", "left"),
                Edge("root", "right"),
                Edge("left", "__end__"),
                Edge("right", "__end__"),
            ),
        )
    )
    context = WorkflowContext(ports={"native_execution_policy": NativeExecutionPolicy(2)})
    result = asyncio.run(executable.ainvoke({}, context, thread_id="thread", run_id="parallel-run"))
    assert result == {"left": "L", "right": "R"}


def test_native_engine_retries_and_keeps_stable_pending_identity() -> None:
    attempts = 0

    async def flaky(_state, _context):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WorkflowNodeError(
                code=WorkflowErrorCode.RETRYABLE_TOOL,
                message_ref="retry",
            )
        return StatePatch({"answer": "ok"})

    executable, _store = _executable(
        WorkflowDefinition(
            "retry",
            "1",
            1,
            "flaky",
            (
                NodeDefinition(
                    "flaky",
                    flaky,
                    retry_policy=RetryPolicy(
                        max_attempts=2,
                        initial_delay_seconds=0,
                        max_delay_seconds=0,
                        retryable_codes=frozenset({"retryable_tool"}),
                    ),
                ),
            ),
            {"answer": _channel("flaky")},
            16,
            8,
            edges=(Edge("flaky", "__end__"),),
        )
    )
    with pytest.raises(WorkflowNodeError, match="retry"):
        asyncio.run(
            executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="retry-run")
        )
    result = asyncio.run(
        executable.ainvoke(None, WorkflowContext(), thread_id="thread", run_id="retry-run")
    )
    assert isinstance(result, Mapping)
    assert result["answer"] == "ok" and attempts == 2


def test_native_engine_interrupt_reopens_and_consumes_stable_response() -> None:
    async def approval(_state, _context):  # type: ignore[no-untyped-def]
        response = workflow_interrupt({"question": "continue?"})
        assert isinstance(response, Mapping)
        return StatePatch({"approved": bool(response["approved"])})

    executable, store = _executable(
        WorkflowDefinition(
            "interrupt",
            "1",
            1,
            "approval",
            (
                NodeDefinition(
                    "approval",
                    approval,
                    interrupt_capable=True,
                    barrier=True,
                    exclusive_superstep=True,
                    pre_interrupt_effect_policy="pure",
                ),
            ),
            {"approved": _channel("approval", JsonType.BOOLEAN)},
            16,
            8,
            edges=(Edge("approval", "__end__"),),
        )
    )
    with pytest.raises(WorkflowSuspended):
        asyncio.run(executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="hitl"))
    assert store.interrupt is not None
    interrupt_id = str(store.interrupt["interrupt_id"])
    result = asyncio.run(
        executable.resume(
            {interrupt_id: {"approved": True}},
            WorkflowContext(),
            thread_id="thread",
            run_id="hitl",
        )
    )
    assert isinstance(result, Mapping)
    assert result["approved"] is True


def test_native_engine_max_supersteps_fails_durably() -> None:
    async def node(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({})

    executable, store = _executable(
        WorkflowDefinition(
            "max",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {},
            5,
            4,
            edges=(Edge("node", "node"),),
            loop_budgets={"loop": 3},
            loop_budget_bindings={"node->node": "loop"},
        )
    )
    with pytest.raises(WorkflowNodeError, match="max_supersteps"):
        asyncio.run(executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="max-run"))
    assert store.failures


def test_native_engine_bounded_cycle_advances_epoch_and_exits() -> None:
    async def loop(state, _context):  # type: ignore[no-untyped-def]
        counters = state.get("loop_counters", {})
        current = int(counters.get("loop", 0)) if isinstance(counters, Mapping) else 0
        return StatePatch({"loop_counters": {"loop": current + 1}})

    def select(state, _context):  # type: ignore[no-untyped-def]
        return "again" if state["loop_counters"]["loop"] < 2 else "done"

    executable, store = _executable(
        WorkflowDefinition(
            "cycle",
            "1",
            1,
            "loop",
            (NodeDefinition("loop", loop),),
            {"loop_counters": _channel("loop", JsonType.OBJECT)},
            16,
            8,
            conditional_edges=(
                ConditionalEdge(
                    "loop",
                    select,
                    {"again": "loop", "done": "__end__"},
                    selector_effect_policy="pure",
                ),
            ),
            loop_budgets={"loop": 2},
            loop_budget_bindings={"loop->loop": "loop"},
        )
    )
    result = asyncio.run(
        executable.ainvoke(
            {"loop_counters": {"loop": 0}},
            WorkflowContext(),
            thread_id="thread",
            run_id="cycle",
        )
    )
    assert isinstance(result, Mapping)
    assert result["loop_counters"] == {"loop": 2}
    assert store.snapshot is not None and store.snapshot.step == 2


def test_native_engine_join_fires_once_after_all_sources() -> None:
    async def empty(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({})

    async def joined(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "joined"})

    executable, store = _executable(
        WorkflowDefinition(
            "join",
            "1",
            1,
            "root",
            (
                NodeDefinition("root", empty),
                NodeDefinition("left", empty),
                NodeDefinition("right", empty),
                NodeDefinition("join", joined),
            ),
            {"answer": _channel("join")},
            16,
            8,
            edges=(
                Edge("root", "left"),
                Edge("root", "right"),
                Edge(("left", "right"), "join"),
                Edge("join", "__end__"),
            ),
        )
    )
    result = asyncio.run(
        executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="join-run")
    )
    assert isinstance(result, Mapping)
    assert result["answer"] == "joined"
    assert store.snapshot is not None
    assert len(store.snapshot.join_firings) == 1


def test_pending_result_after_commit_response_loss_does_not_rerun_node() -> None:
    calls = 0

    async def node(_state, _context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return StatePatch({"answer": "once"})

    class LoseTaskCommitResponse(InMemoryNativeCheckpointStore):
        lose = True

        async def commit_task_result(self, **kwargs):  # type: ignore[no-untyped-def]
            await super().commit_task_result(**kwargs)
            if self.lose:
                self.lose = False
                raise ConnectionError("response lost after commit")

    store = LoseTaskCommitResponse()
    executable = _native(
        compile_workflow(
            WorkflowDefinition(
                "pending",
                "1",
                1,
                "node",
                (NodeDefinition("node", node),),
                {"answer": _channel("node")},
                16,
                8,
                edges=(Edge("node", "__end__"),),
            )
        ),
        store,
    )
    with pytest.raises(ConnectionError, match="response lost"):
        asyncio.run(executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="pending"))
    result = asyncio.run(
        executable.ainvoke(None, WorkflowContext(), thread_id="thread", run_id="pending")
    )
    assert isinstance(result, Mapping)
    assert result["answer"] == "once" and calls == 1


def test_route_receipt_after_commit_response_loss_does_not_rerun_selector() -> None:
    selector_calls = 0

    async def start(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"choice": "done"})

    def route(_state, _context):  # type: ignore[no-untyped-def]
        nonlocal selector_calls
        selector_calls += 1
        return "done"

    async def done(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "done"})

    class LoseRouteCommitResponse(InMemoryNativeCheckpointStore):
        lose = True

        async def commit_route_selection(self, **kwargs):  # type: ignore[no-untyped-def]
            result = await super().commit_route_selection(**kwargs)
            if self.lose:
                self.lose = False
                raise ConnectionError("route response lost")
            return result

    store = LoseRouteCommitResponse()
    executable = _native(
        compile_workflow(
            WorkflowDefinition(
                "route-loss",
                "1",
                1,
                "start",
                (NodeDefinition("start", start), NodeDefinition("done", done)),
                {"choice": _channel("start"), "answer": _channel("done")},
                16,
                8,
                edges=(Edge("done", "__end__"),),
                conditional_edges=(
                    ConditionalEdge(
                        "start", route, {"done": "done"}, selector_effect_policy="pure"
                    ),
                ),
            )
        ),
        store,
    )
    with pytest.raises(ConnectionError, match="route response lost"):
        asyncio.run(
            executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="route-loss")
        )
    result = asyncio.run(
        executable.ainvoke(None, WorkflowContext(), thread_id="thread", run_id="route-loss")
    )
    assert isinstance(result, Mapping)
    assert result["answer"] == "done" and selector_calls == 1


def test_permanent_node_failure_is_recorded() -> None:
    async def fail(_state, _context):  # type: ignore[no-untyped-def]
        raise WorkflowNodeError(code=WorkflowErrorCode.PERMANENT, message_ref="permanent")

    executable, store = _executable(
        WorkflowDefinition(
            "failure",
            "1",
            1,
            "fail",
            (NodeDefinition("fail", fail),),
            {},
            16,
            8,
            edges=(Edge("fail", "__end__"),),
        )
    )
    with pytest.raises(WorkflowNodeError, match="permanent"):
        asyncio.run(executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="failure"))
    assert store.failures


def test_generic_delivery_is_canonical_and_terminal_is_last() -> None:
    state: dict[str, object] = {
        "values": {
            "delivery_intents": [
                {"intent_id": "b", "kind": "artifact", "channel": "card", "payload": {"id": 2}},
                {"intent_id": "a", "kind": "assistant", "channel": "chat", "payload": {"id": 1}},
            ]
        }
    }
    intents = NativeWorkflowExecutable.terminal_intents(
        state, run_id="delivery", status="completed", error=None, recovery_action=None
    )
    assert [intent.intent_id for intent in intents] == ["a", "b", "delivery:run-terminal"]
    assert intents[-1].event_type == "workflow.final"
    with pytest.raises(Exception, match="private"):
        NativeWorkflowExecutable.terminal_intents(
            {
                "values": {
                    "delivery_intents": [
                        {
                            "intent_id": "x",
                            "kind": "assistant",
                            "channel": "chat",
                            "payload": {"secret_token": "leak"},
                        }
                    ]
                }
            },
            run_id="delivery",
            status="completed",
            error=None,
            recovery_action=None,
        )


def _delivery_intent(**changes: object) -> dict[str, object]:
    intent: dict[str, object] = {
        "intent_id": "answer",
        "kind": "assistant",
        "channel": "chat",
        "payload": {"answer": "ok"},
    }
    intent.update(changes)
    return intent


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: {key: value for key, value in item.items() if key != "intent_id"},
        lambda item: {**item, "unknown": True},
        lambda item: {**item, "intent_id": ""},
        lambda item: {**item, "intent_id": "x" * 129},
        lambda item: {**item, "intent_id": "contains space"},
        lambda item: {**item, "kind": ""},
        lambda item: {**item, "kind": "x" * 65},
        lambda item: {**item, "kind": "final"},
        lambda item: {**item, "channel": ""},
        lambda item: {**item, "channel": "x" * 65},
        lambda item: {**item, "payload": []},
    ],
)
def test_generic_delivery_rejects_every_schema_and_identity_mutation(
    mutation,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(Exception, match="delivery|engine-owned"):
        NativeWorkflowExecutable.terminal_intents(
            {"values": {"delivery_intents": [mutation(_delivery_intent())]}},
            run_id="delivery",
            status="completed",
            error=None,
            recovery_action=None,
        )


def test_generic_delivery_enforces_count_size_depth_items_and_unique_id_bounds() -> None:
    def project(items: list[dict[str, object]]) -> tuple[object, ...]:
        return NativeWorkflowExecutable.terminal_intents(
            {"values": {"delivery_intents": items}},
            run_id="delivery",
            status="completed",
            error=None,
            recovery_action=None,
        )

    assert len(project([_delivery_intent(intent_id=f"item-{index}") for index in range(16)])) == 17
    with pytest.raises(Exception, match="16"):
        project([_delivery_intent(intent_id=f"item-{index}") for index in range(17)])

    # {"data":""} contributes 11 canonical UTF-8 bytes.
    assert len(project([_delivery_intent(payload={"data": "x" * (32 * 1024 - 11)})])) == 2
    with pytest.raises(Exception, match="32KiB"):
        project([_delivery_intent(payload={"data": "x" * (32 * 1024 - 10)})])

    depth_eight: dict[str, object] = {"leaf": True}
    for _ in range(6):
        depth_eight = {"nested": depth_eight}
    assert len(project([_delivery_intent(payload=depth_eight)])) == 2
    too_deep = {"nested": depth_eight}
    with pytest.raises(Exception, match="depth 8"):
        project([_delivery_intent(payload=too_deep)])

    assert len(project([_delivery_intent(payload={"items": list(range(511))})])) == 2
    with pytest.raises(Exception, match="512 items"):
        project([_delivery_intent(payload={"items": list(range(512))})])

    duplicate = _delivery_intent()
    with pytest.raises(Exception, match="unique"):
        project([duplicate, copy.deepcopy(duplicate)])


@pytest.mark.parametrize(
    "private_key",
    [
        "topic",
        "raw_query",
        "prompt",
        "messages",
        "credentials",
        "secrets",
        "private_state",
        "_private_note",
        "secret_token",
    ],
)
def test_generic_delivery_rejects_private_keys_recursively(private_key: str) -> None:
    with pytest.raises(Exception, match="private"):
        NativeWorkflowExecutable.terminal_intents(
            {
                "values": {
                    "delivery_intents": [
                        _delivery_intent(payload={"safe": [{"nested": {private_key: "leak"}}]})
                    ]
                }
            },
            run_id="delivery",
            status="completed",
            error=None,
            recovery_action=None,
        )


def test_terminal_projection_descriptor_rejects_request_contract_drift() -> None:
    fingerprint = "a" * 64
    descriptor = TerminalProjectionDescriptor.create(
        capability_id="terminal.commit", version="1", projector_fingerprint=fingerprint
    )
    assert descriptor.request_schema_hash == TERMINAL_REQUEST_SCHEMA_HASH
    assert descriptor.request_factory_hash == TERMINAL_REQUEST_FACTORY_HASH
    with pytest.raises(Exception, match="schema hash"):
        TerminalProjectionDescriptor(
            "terminal.commit",
            "1",
            fingerprint,
            "b" * 64,
            TERMINAL_REQUEST_FACTORY_HASH,
        )
    with pytest.raises(Exception, match="factory hash"):
        TerminalProjectionDescriptor(
            "terminal.commit",
            "1",
            fingerprint,
            TERMINAL_REQUEST_SCHEMA_HASH,
            "c" * 64,
        )


def test_terminal_projection_factory_hash_is_bound_to_every_canonical_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = copy.deepcopy(native_module._TERMINAL_REQUEST_FACTORY_SPEC)
    bindings = original["bindings"]
    assert isinstance(bindings, list)
    for index in range(len(bindings)):
        mutated = copy.deepcopy(original)
        mutated_bindings = mutated["bindings"]
        assert isinstance(mutated_bindings, list)
        binding = mutated_bindings[index]
        assert isinstance(binding, list)
        binding[1] = "state" if binding[1] != "state" else "run_id"
        monkeypatch.setattr(native_module, "_TERMINAL_REQUEST_FACTORY_SPEC", mutated)
        with pytest.raises(Exception, match="factory implementation drifted"):
            TerminalProjectionDescriptor.create(
                capability_id="terminal.commit",
                version="1",
                projector_fingerprint="a" * 64,
            )
        monkeypatch.setattr(
            native_module, "_TERMINAL_REQUEST_FACTORY_SPEC", copy.deepcopy(original)
        )


def test_terminal_projection_prepare_reopens_without_rerunning_projector() -> None:
    calls = 0

    async def node(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "ok"})

    async def project(_request, context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        assert context.run_id == "projection"
        return {
            "intents": [
                {
                    "intent_id": "answer",
                    "kind": "assistant",
                    "channel": "chat",
                    "payload": {"answer": "ok"},
                }
            ],
            "blob_refs": [],
        }

    descriptor = TerminalProjectionDescriptor.create(
        capability_id="terminal.commit", version="1", projector_fingerprint="d" * 64
    )
    expected_descriptor = descriptor

    class CommitPort:
        def lookup(  # type: ignore[no-untyped-def]
            self, workflow_name, workflow_version, descriptor
        ):
            del workflow_name, workflow_version
            return project if descriptor == expected_descriptor else None

    class LosePrepareResponse(InMemoryNativeCheckpointStore):
        lose = True

        async def prepare_terminal_projection(self, **kwargs):  # type: ignore[no-untyped-def]
            receipt = await super().prepare_terminal_projection(**kwargs)
            if self.lose:
                self.lose = False
                raise ConnectionError("prepare response lost")
            return receipt

    compiled = compile_workflow(
        WorkflowDefinition(
            "projection",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {"answer": _channel("node")},
            16,
            8,
            edges=(Edge("node", "__end__"),),
            terminal_projection_descriptor=descriptor,
        )
    )
    legacy = compile_workflow(
        WorkflowDefinition(
            "projection",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {"answer": _channel("node")},
            16,
            8,
            edges=(Edge("node", "__end__"),),
        )
    )
    assert compiled.manifest.terminal_projection_descriptor == descriptor
    assert compiled.manifest.definition_hash != legacy.manifest.definition_hash
    assert compiled.manifest.policy_hash != legacy.manifest.policy_hash
    assert (
        compiled.manifest.implementation_bundle_hash != legacy.manifest.implementation_bundle_hash
    )
    store = LosePrepareResponse()
    executable = NativeWorkflowExecutable(
        compiled,
        store,
        terminal_projection_port=_TerminalProjectionPort(),
        terminal_commit_projection_port=CommitPort(),
    )
    with pytest.raises(ConnectionError, match="prepare response lost"):
        asyncio.run(
            executable.ainvoke({}, WorkflowContext(), thread_id="thread", run_id="projection")
        )
    result = asyncio.run(
        executable.ainvoke(None, WorkflowContext(), thread_id="thread", run_id="projection")
    )
    assert isinstance(result, Mapping)
    assert calls == 1
    assert len(store.projection_prepares) == 1
    assert store.consumed_projection_prepares == set(store.projection_prepares)
    assert set(store.materialized_intents) == {"terminal:answer", "run:terminal"}


def test_pinned_terminal_projection_cannot_downgrade_to_legacy() -> None:
    async def node(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "ok"})

    descriptor = TerminalProjectionDescriptor.create(
        capability_id="terminal.commit", version="1", projector_fingerprint="e" * 64
    )
    compiled = compile_workflow(
        WorkflowDefinition(
            "projection-missing",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {"answer": _channel("node")},
            16,
            8,
            edges=(Edge("node", "__end__"),),
            terminal_projection_descriptor=descriptor,
        )
    )
    assert compiled.manifest.terminal_projection_descriptor == descriptor
    executable = NativeWorkflowExecutable(
        compiled,
        InMemoryNativeCheckpointStore(),
        terminal_projection_port=_TerminalProjectionPort(),
        terminal_commit_projection_port=_TerminalCommitProjectionPort(),
    )
    with pytest.raises(Exception, match="unavailable"):
        asyncio.run(
            executable.ainvoke(
                {}, WorkflowContext(), thread_id="thread", run_id="projection-missing"
            )
        )


def test_projector_runtime_error_is_converted_to_durable_engine_failure() -> None:
    async def node(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "ok"})

    async def project(_request, _context):  # type: ignore[no-untyped-def]
        raise RuntimeError("projector leaked private cause")

    descriptor = TerminalProjectionDescriptor.create(
        capability_id="terminal.commit",
        version="1",
        projector_fingerprint="f" * 64,
    )

    class CommitPort:
        def lookup(self, *_identity):  # type: ignore[no-untyped-def]
            return project

    compiled = compile_workflow(
        WorkflowDefinition(
            "projection-failure",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {"answer": _channel("node")},
            16,
            8,
            edges=(Edge("node", "__end__"),),
            terminal_projection_descriptor=descriptor,
        )
    )
    store = InMemoryNativeCheckpointStore()
    executable = NativeWorkflowExecutable(
        compiled,
        store,
        terminal_projection_port=_TerminalProjectionPort(),
        terminal_commit_projection_port=CommitPort(),
    )
    with pytest.raises(WorkflowNodeError, match="frontier_failure") as caught:
        asyncio.run(
            executable.ainvoke(
                {}, WorkflowContext(), thread_id="thread", run_id="projection-failure"
            )
        )
    assert caught.value.__cause__ is None
    assert store.failures
