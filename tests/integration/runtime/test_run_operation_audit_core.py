"""Actual kernel rejection boundaries and continuous recording proof oracles."""

import asyncio

import pytest

from simple_harness import CallId, ExecutionSessionId, Message, MessageRole, RequestId, RunId
from simple_harness.providers import ProviderResponse, ProviderToolCall
from simple_harness.runtime import RunStart

from .test_react_sqlite_runtime import (
    AuthorizationScenario,
    PhysicalToolCounter,
    Provider,
    authorization_runtime,
)


async def start(runtime):
    await runtime.start()
    await runtime.client.start(
        RunStart(
            ExecutionSessionId("session-fault"),
            RunId("run-fault"),
            RequestId("request-fault"),
            "turn-fault",
            {
                "messages": [{"role": "user", "content": "write"}],
                "capability_snapshot": {"tools": ["write_note"]},
                "max_output_tokens": 100,
            },
            1,
        )
    )
    await runtime.wait_idle(RunId("run-fault"))


class Calls(Provider):
    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    async def invoke(self, request, *, cancel):
        self.requests.append(request)
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "done"),
            model="model",
            tool_calls=self.calls if len(self.requests) == 1 else (),
        )


@pytest.mark.parametrize("boundary", ["duplicate", "context", "preflight", "route", "envelope"])
def test_actual_pre_effect_rejection_is_durable(tmp_path, boundary):
    from .test_human_memory_react_barrier import PolicyExposure

    class RefuseContext:
        def prepare_snapshot(self, *args, **kwargs):
            raise RuntimeError("context unavailable")

    class RefusePolicy(PolicyExposure):
        def execution_policy(self, *args, **kwargs):
            raise RuntimeError("catalog unavailable")

    class StandalonePolicy(PolicyExposure):
        def execution_policy(self, *args, **kwargs):
            from simple_harness.tools.runtime_catalog import (
                ToolEffectClass,
                ToolExecutionPolicy,
                ToolRouteRequirement,
                ToolTaskScopeRequirement,
            )

            return ToolExecutionPolicy(
                "host:note",
                "d" * 64,
                ToolEffectClass.NON_PROJECT_EFFECT,
                ToolRouteRequirement.FORBIDDEN,
                ToolTaskScopeRequirement.FORBIDDEN,
            )

    class RefuseEnvelope:
        async def issue_envelope(self, request):
            assert request.run_id == RunId("run-fault")
            raise RuntimeError("actual task authority unavailable")

    async def case():
        calls = (ProviderToolCall(CallId("call-1"), "write_note", {}),)
        if boundary == "duplicate":
            calls = calls * 2
        provider = Calls(calls)
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            tmp_path / "core.db",
            authorization=AuthorizationScenario(),
            physical=physical,
            provider=provider,
            owner_id="core-owner",
            clock=lambda: 10.0,
            run_context_authority=RefuseContext() if boundary == "context" else None,
            task_execution_authority=RefuseEnvelope() if boundary == "envelope" else None,
            tool_exposure=(
                RefusePolicy()
                if boundary == "preflight"
                else StandalonePolicy()
                if boundary == "envelope"
                else PolicyExposure()
                if boundary == "route"
                else None
            ),
        )
        await start(runtime)
        assert physical.calls == 0
        assert len(provider.requests) == (
            0 if boundary == "context" else 2 if boundary == "route" else 1
        )
        snapshot = uow.read_run_operation_audit(RunId("run-fault"), limit=4096)
        name, state = {
            "duplicate": ("tool.proposal", "failed"),
            "context": ("context.prepare", "failed"),
            "preflight": ("tool.preflight", "failed"),
            "route": ("tool.route_gate", "rejected"),
            "envelope": ("tool.envelope", "failed"),
        }[boundary]
        assert any(o.operation_name == name and o.state == state for o in snapshot.operations)
        assert not any(o.kind == "effect" for o in snapshot.operations)
        proposals = [o for o in snapshot.operations if o.record_type == "proposal"]
        assert len(proposals) == (
            0 if boundary == "context" else 2 if boundary == "duplicate" else 1
        )
        assert len({o.operation_id for o in proposals}) == len(proposals)
        assert len({o.raw_call_id_hash for o in proposals}) == len(proposals)
        assert all(o.provider_invocation_id and o.effect_id is None for o in proposals)
        assert uow.read_run("run-fault").state.value == (
            "completed" if boundary == "route" else "failed"
        )
        await runtime.close()
        database.close()
        from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork

        with Database.open(tmp_path / "core.db") as reopened:
            after = SqliteExecutionUnitOfWork(reopened).read_run_operation_audit(
                RunId("run-fault"), limit=4096
            )
            assert after.to_json() == snapshot.to_json()

    asyncio.run(case())


@pytest.mark.parametrize("custom_driver", [False, True])
def test_supported_runtime_restart_coverage_and_custom_driver_boundary(tmp_path, custom_driver):
    from simple_harness.runtime.drivers import ReActDriver
    from simple_harness.tools import AuthorizationDecision

    from .test_react_sqlite_runtime import start_authorization_wait, wait_for_scenario

    class CustomDriver(ReActDriver):
        pass

    async def case():
        physical = PhysicalToolCounter()

        def open_runtime(owner, **kwargs):
            return authorization_runtime(
                tmp_path / "restart.db",
                authorization=AuthorizationScenario(),
                physical=physical,
                owner_id=owner,
                clock=lambda: 10.0,
                driver=CustomDriver(clock=lambda: 10.0) if custom_driver else None,
                **kwargs,
            )

        runtime, uow, database = open_runtime("first")
        decision = await start_authorization_wait(runtime, uow)
        await runtime.close()
        database.close()
        runtime, uow, database = open_runtime("second", emit_tool_call=False)
        await runtime.start()
        await runtime.client.decide_authorization(
            RunId("run-fault"),
            decision_id=decision.decision_id,
            nonce=str(decision.request["nonce"]),
            expected_version=decision.version,
            decision=AuthorizationDecision.ALLOW,
        )
        await wait_for_scenario(runtime, lambda: physical.calls == 1)
        await runtime.wait_idle(RunId("run-fault"))
        assert uow.read_run("run-fault").state.value == "completed"
        assert physical.calls == 1
        metadata = uow.read_run_operation_audit(RunId("run-fault"), limit=4096).to_json()
        assert metadata["recording_contract_version"] == 2
        if custom_driver:
            assert metadata["recording_coverage"] == "unverified"
            assert "driver_or_uow_recording_unverified" in metadata["coverage_gaps"]
        else:
            assert metadata["coverage_gaps"] == []
            assert metadata["recording_coverage"] == "verified_current_intervals"
        await runtime.close()
        database.close()

    asyncio.run(case())


@pytest.mark.parametrize("failure", ["estimator", "budget"])
def test_preclaim_failure_has_no_invocation_or_delegate(tmp_path, failure):
    from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator

    class BrokenEstimator(FrozenPriceEstimator):
        def estimate_upper_bound(self, request):
            raise RuntimeError("external estimator unavailable")

    async def case():
        provider = Calls(())
        runtime, uow, database = authorization_runtime(
            tmp_path / "preclaim.db",
            authorization=AuthorizationScenario(),
            physical=PhysicalToolCounter(),
            provider=provider,
            owner_id="preclaim",
            clock=lambda: 10.0,
            provider_estimator=(
                BrokenEstimator if failure == "estimator" else FrozenPriceEstimator
            )(
                "price-v1",
                "model",
                1_000_000,
                1_000_000,
            ),
            provider_budget_policy=BudgetPolicy(hard_cap_micros=0),
        )
        await start(runtime)
        snapshot = uow.read_run_operation_audit(RunId("run-fault"))
        assert len(provider.requests) == 0
        assert uow.read_run("run-fault").state.value == "failed"
        assert not any(o.kind == "provider" for o in snapshot.operations)
        assert any(
            o.operation_name == "provider.prepare" and o.state == "failed"
            for o in snapshot.operations
        )
        await runtime.close()
        database.close()

    asyncio.run(case())


@pytest.mark.parametrize("changed", ["identity_hash", "runtime_epoch", "owner_hash", "name"])
def test_foreign_settlement_cannot_close_actual_interval(tmp_path, changed):
    import json

    from simple_harness.contracts import canonical_json
    from simple_harness.execution.audit import RunAuditUnavailable, audit_hash

    async def case():
        runtime, uow, database = authorization_runtime(
            tmp_path / "pair.db",
            authorization=AuthorizationScenario(),
            physical=PhysicalToolCounter(),
            provider=Calls(()),
            owner_id="pair",
            clock=lambda: 10.0,
        )
        await start(runtime)
        await runtime.close()
        row = database.connection.execute(
            "SELECT * FROM run_events WHERE kind='audit.runtime.v2' "
            "AND json_extract(payload_json,'$.state')='completed' LIMIT 1",
        ).fetchone()
        value = json.loads(row["payload_json"])
        value[changed] = (
            value[changed] + 1
            if changed == "runtime_epoch"
            else "provider.prepare"
            if changed == "name"
            else "0" * 64
        )
        # Corruption oracle: alter only one audit settlement, keep all original
        # canonical evidence and the actual start receipt intact.
        with database.transaction() as connection:
            connection.execute(
                "UPDATE run_events SET event_id=?,payload_json=? WHERE event_id=?",
                (
                    "audit-runtime:" + value["operation_id"] + ":completed:" + audit_hash(value),
                    canonical_json(value),
                    row["event_id"],
                ),
            )
        with pytest.raises(RunAuditUnavailable, match="audit_runtime_interval_mismatch"):
            uow.read_run_operation_audit(RunId("run-fault"))
        database.close()

    asyncio.run(case())


@pytest.mark.parametrize(
    "boundary",
    [
        "runtime.preflight",
        "context.prepare",
        "context.verify",
        "provider.prepare",
        "tool.proposal",
        "tool.preflight",
    ],
)
def test_missing_whole_preflight_interval_is_not_certified(tmp_path, boundary):
    async def case():
        runtime, uow, database = authorization_runtime(
            tmp_path / "missing-preflight.db",
            authorization=AuthorizationScenario(),
            physical=PhysicalToolCounter(),
            provider=Calls(()),
            owner_id="preflight",
            clock=lambda: 10.0,
        )
        await start(runtime)
        await runtime.close()
        before = uow.read_run_operation_audit(RunId("run-fault"), limit=4096)
        assert before.coverage_gaps == ()
        # Explicit corruption negative: retain all independent canonical Run,
        # activation, Provider and terminal evidence. Never a legacy fixture.
        with database.transaction() as connection:
            assert (
                connection.execute(
                    "DELETE FROM run_events WHERE kind='audit.runtime.v2' "
                    "AND json_extract(payload_json,'$.name')=?",
                    (boundary,),
                ).rowcount
                == 2
            )
        after = uow.read_run_operation_audit(RunId("run-fault"), limit=4096)
        assert (
            "runtime_parent_interval_unverified"
            if boundary == "runtime.preflight"
            else "runtime_child_interval_unverified"
        ) in after.coverage_gaps
        assert after.to_json()["recording_coverage"] == "unverified"
        database.close()

    asyncio.run(case())
