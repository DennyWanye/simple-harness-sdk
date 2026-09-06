"""Actual kernel selection, continuous recording, and opaque-driver refusal."""
import asyncio

import pytest

from simple_harness import RunId
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.runtime import ReActDriver, StartModeDriverRouter

from .test_react_sqlite_runtime import (
    AuthorizationScenario, PhysicalToolCounter, authorization_runtime,
)
from .test_run_operation_audit_core import Calls, start
from .test_kernel_start import Driver, host_control_request, runtime as control_runtime


class OpaqueRouter:
    # A self-reported capability string cannot certify custom execution.
    recording_contract = "sdk.react.v2"

    def __init__(self, delegate):
        self.delegate = delegate

    @property
    def policy_fingerprint(self):
        return self.delegate.policy_fingerprint

    async def start(self, invocation, *, context, cancel):
        return await self.delegate.start(invocation, context=context, cancel=cancel)


class CustomRouter(StartModeDriverRouter):
    recording_contract = "sdk.react.v2"


@pytest.mark.parametrize("kind", ["sdk", "opaque", "subclass"])
def test_real_ordinary_driver_recording_uses_actual_selected_implementation(tmp_path, kind):
    async def case():
        delegate = ReActDriver(clock=lambda: 10.0)
        control = Driver()
        router = (StartModeDriverRouter(delegate, control) if kind == "sdk"
                  else OpaqueRouter(delegate) if kind == "opaque"
                  else CustomRouter(delegate, control))
        provider = Calls(())
        path = tmp_path / "runtime.db"
        value, uow, database = authorization_runtime(
            path, authorization=AuthorizationScenario(), physical=PhysicalToolCounter(),
            owner_id="actual-router", clock=lambda: 10.0, provider=provider, driver=router,
        )
        try:
            await start(value)
            snapshot = uow.read_run_operation_audit(RunId("run-fault"), limit=4096)
            assert len(provider.requests) == 1
            assert control.calls == 0
            assert snapshot.to_json()["recording_coverage"] == (
                "verified_current_intervals" if kind == "sdk" else "unverified")
            assert ("driver_or_uow_recording_unverified" in snapshot.coverage_gaps) == (kind != "sdk")
            assert any(o.operation_name == "runtime.driver" and o.state == "completed"
                       for o in snapshot.operations)
        finally:
            await value.close()
            database.close()
        reopened = Database.open(path)
        try:
            after = SqliteExecutionUnitOfWork(reopened).read_run_operation_audit(RunId("run-fault"), limit=4096)
            assert after.snapshot_hash == snapshot.snapshot_hash
            assert len(provider.requests) == 1
        finally:
            reopened.close()
    asyncio.run(case())


def test_real_host_control_selects_custom_driver_without_claiming_react_coverage(tmp_path):
    async def case():
        control = Driver()
        router = StartModeDriverRouter(ReActDriver(), control)
        value, uow, database = control_runtime(tmp_path, driver=router)
        request = host_control_request("routed")
        try:
            await value.start()
            await value.client.start_host_control(request)
            await value.wait_idle(request.run_id)
            await value.client.start_host_control(request)
            assert control.calls == 1
            snapshot = uow.read_start_snapshot(request.run_id.value)
            assert snapshot["host_control_authority"] == request.authority.to_json()
            audit = uow.read_run_operation_audit(request.run_id, limit=4096)
            assert "driver_or_uow_recording_unverified" in audit.coverage_gaps
            assert audit.to_json()["recording_coverage"] == "unverified"
            assert not any(o.kind in {"provider", "effect"} for o in audit.operations)
        finally:
            await value.close()
            database.close()
    asyncio.run(case())
