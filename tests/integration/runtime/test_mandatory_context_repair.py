"""Actual SQLite invocation/checkpoint/tool recovery; no external Provider/model.

SQL below is SDK-owned forensic oracle, never an API required from Host.
"""
import asyncio

import pytest

from simple_harness import MandatoryContextActionRequired, MandatoryContextRejectionV1
from simple_harness.contracts import CallId, RequestId, RunId, thaw_json
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.context_authority import ContextRouteReceipt
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.providers import CancelToken, ProviderResponse, ProviderToolCall, ProviderToolSpec
from simple_harness.runtime import EffectBatchExecutor, RuntimeServices, SqliteContextPort
from simple_harness.runtime.drivers.react_loop import ReActLoop, ReActRunInput, AgentLoopCollaborator
from simple_harness.runtime.task_scope_protocol import TaskScopeRoute
from simple_harness.runtime.termination import TerminationLimits, TerminationState
from simple_harness.tools import EffectExecutor, FunctionTool, ToolRegistry, ToolSpec, ToolResult

from .test_react_sqlite_runtime import Provider, OpaqueResolver, Authorization, Reconciliation, Noop


class PowerLoss(BaseException):
    pass


@pytest.mark.parametrize("boundary", [None, "physical_success", "response_reserved",
                                     "context_action_reserved", "feedback_append"])
def test_real_response_and_repair_reopen_without_resend(tmp_path, boundary):
    asyncio.run(_case(tmp_path, boundary=boundary))


@pytest.mark.parametrize("error", ["exhausted", "foreign", "ordinary"])
def test_precise_rejection_and_bounded_failure(tmp_path, error):
    asyncio.run(_case(tmp_path, error=error))


async def _case(tmp_path, *, boundary=None, error=None):
    path = tmp_path / "execution.db"
    armed = [boundary]
    sends = []
    class Physical(Provider):
        async def invoke(self, request, *, cancel):
            sends.append(request.request_id.value)
            ordinal = int(request.request_id.value.rsplit(":", 1)[-1])
            if ordinal > 1:
                assert any(m.metadata.get("source") == "sdk_mandatory_context_control" for m in request.messages)
            calls = ()
            if ordinal == 2 and error is None:
                calls = (ProviderToolCall(CallId("actual-ack"), "prospective_ack", {}),)
            return ProviderResponse(request.request_id, Message(MessageRole.ASSISTANT, "43"),
                calls, model="model", finish_reason="tool_calls" if calls else "stop")
    physical = Physical()
    class Store(SqliteExecutionUnitOfWork):
        def cas_react_checkpoint(self, **values):
            result = super().cas_react_checkpoint(**values)
            if armed[0] == values["checkpoint"]["phase"]:
                armed[0] = None
                raise PowerLoss()
            return result
    class Coordinator(ProviderInvocationCoordinator):
        async def invoke(self, *args, **kwargs):
            result = await super().invoke(*args, **kwargs)
            if armed[0] == "physical_success":
                armed[0] = None
                raise PowerLoss()
            return result
    class Context(SqliteContextPort):
        def append(self, *args, **kwargs):
            result = super().append(*args, **kwargs)
            if armed[0] == "feedback_append" and str(args[3]).endswith(":mandatory-context-feedback"):
                armed[0] = None
                raise PowerLoss()
            return result
    class Decision:
        async def check_mandatory_context_actions(self, *, run_id, provider_turn_ordinal, request_fingerprint):
            # Real SDK Effect success is the test authority for its ACK tool.
            acked = database.connection.execute(
                "SELECT COUNT(*) FROM execution_effects WHERE run_id=? AND tool_name='prospective_ack' AND state='succeeded'",
                (run_id.value,),
            ).fetchone()[0]
            if acked:
                return
            if error == "ordinary":
                raise ValueError("actual_source_permission_denied")
            raise MandatoryContextActionRequired(MandatoryContextRejectionV1(
                "foreign" if error == "foreign" else run_id.value,
                provider_turn_ordinal, request_fingerprint))
        async def record_no_recall(self, **values):
            await self.check_mandatory_context_actions(**values)
            return ContextRouteReceipt("real-no-recall", values["run_id"].value,
                "real-no-recall", "real-no-recall", TaskScopeRoute.DIRECT_STANDALONE, None, None)
    async def ack(call, context):
        return ToolResult.succeeded(CallId("actual-ack"), {"acknowledged": True})
    registry = ToolRegistry()
    registry.register(FunctionTool(ToolSpec("prospective_ack", "Acknowledge test pending item", {"type": "object"}), ack))
    def open_services():
        db = Database.open(path)
        uow = Store(db)
        return db, uow
    database, store = open_services()
    store.create_with_start_snapshot(execution_session_id="session", run_id="run-1",
        request_id="root", profile_key="agent.general", driver_kind="react",
        snapshot={"catalog_generation": 1}, event_id="created", now=1.)
    _, lease = store.claim_runtime_activation(run_id="run-1", owner_id="owner",
        namespace="runtime.kernel", now=2., lease_ttl_seconds=100.)
    fence = await store.acquire(RunId("run-1"), lease, now=2.)
    async def drive():
        coordinator = Coordinator(uow=store, resolver=OpaqueResolver(physical), clock=lambda: 3.)
        context = Context(database, clock=lambda: 3.)
        auth, reconcile = Authorization(), Reconciliation()
        effects = EffectExecutor(uow=store, registry=registry, authorization=auth,
            reconciliation=reconcile, clock=lambda: 3.)
        services = RuntimeServices(provider=coordinator, tools=effects, authorization=auth,
            context=context, delivery=Noop(), tool_reconciliation=reconcile,
            reconciliation=Noop(), provider_reconciliation=Noop(), react_checkpoint=store,
            runtime_decision_sink=Decision())
        return await ReActLoop(collaborator=AgentLoopCollaborator(limits=TerminationLimits(max_turns=6)),
            effects=EffectBatchExecutor(), clock=lambda: 3.).run(
                ReActRunInput(RunId("run-1"), RequestId("root"),
                    tools=(ProviderToolSpec("prospective_ack", "ACK", {"type": "object"}),)),
                services=services, execution_lease=lease, run_fence=fence, cancel=CancelToken(),
                initial_messages=(Message(MessageRole.USER, "28+15"),))
    try:
        if boundary:
            with pytest.raises(PowerLoss):
                await drive()
            assert len(sends) == 1
            database.close()
            database, store = open_services()
        if error:
            expected = {"exhausted": "mandatory_context_action_repair_exhausted",
                        "foreign": "mandatory_context_rejection_checkpoint_differs",
                        "ordinary": "actual_source_permission_denied"}[error]
            with pytest.raises((ValueError, RuntimeError), match=expected):
                await drive()
            assert len(sends) == (3 if error == "exhausted" else 1)
        else:
            result = await drive()
            assert result.termination.turns == 3
            assert len(result.termination.mandatory_context_repairs) == 1
            assert result.termination.to_json()["schema_version"] == 8
            assert sends == [f"run-1:provider-turn:{i}" for i in (1, 2, 3)]
        states = database.connection.execute("SELECT state FROM provider_invocations").fetchall()
        assert len(states) == len(sends) and all(row[0] == "succeeded" for row in states)
        payload = thaw_json(store.read_react_checkpoint("run-1").checkpoint)
        if error not in {"ordinary", "foreign"}:
            assert TerminationState.from_json(payload).to_json() == payload
            assert len(payload["mandatory_context_repairs"]) == (2 if error == "exhausted" else 1)
    finally:
        database.close()
