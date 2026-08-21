# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ProfileLaunchTicket,
    child_launch_fingerprint,
)
from simple_harness.execution.uow import ContinuationState, RunState
from simple_harness.runtime import (
    ChildLaunchRequest,
    DriverResult,
    ProfileLaunchTicketRef,
)
from simple_harness.runtime.start_snapshot import RunStart

from .test_kernel_start import Driver, request, runtime


class ContinuationDriver:
    def __init__(self) -> None:
        self.seen: list[str | None] = []

    async def start(self, invocation, *, context, cancel):
        del context, cancel
        continuation_id = (
            None
            if not invocation.continuations
            else invocation.continuations[0].continuation_id
        )
        self.seen.append(continuation_id)
        if continuation_id == "continuation-2":
            return DriverResult(RunState.COMPLETED, {"handled": continuation_id})
        return DriverResult(RunState.WAITING, {"handled": continuation_id})


def test_two_queued_continuations_auto_drain_then_terminal_and_quarantine(
    tmp_path,
) -> None:
    async def case() -> None:
        driver = ContinuationDriver()
        value, uow, database = runtime(tmp_path, driver=driver)
        await value.start()
        await value.client.start(request("continuations"))
        await value.wait_idle(RunId("run-continuations"))
        assert uow.read_run("run-continuations").state is RunState.WAITING  # type: ignore[union-attr]
        for index in range(1, 4):
            value.client.signal(
                RunId("run-continuations"),
                signal_id=f"continuation-{index}",
                payload={"index": index},
            )
        await asyncio.sleep(0)
        await value.wait_idle(RunId("run-continuations"))
        await asyncio.sleep(0)
        await value.wait_idle(RunId("run-continuations"))

        assert driver.seen == [None, "continuation-1", "continuation-2"]
        assert uow.read_run("run-continuations").state is RunState.COMPLETED  # type: ignore[union-attr]
        assert uow.read_continuation("continuation-1").state is ContinuationState.ACKED  # type: ignore[union-attr]
        assert uow.read_continuation("continuation-2").state is ContinuationState.ACKED  # type: ignore[union-attr]
        assert (
            uow.read_continuation("continuation-3").state
            is ContinuationState.QUARANTINED
        )  # type: ignore[union-attr]
        await value.close()
        database.close()

    asyncio.run(case())


def test_driver_exception_with_claim_atomically_fails_and_acks(tmp_path) -> None:
    class FailingContinuationDriver:
        async def start(self, invocation, *, context, cancel):
            del context, cancel
            if invocation.continuations:
                raise RuntimeError("boom")
            return DriverResult(RunState.WAITING)

    async def case() -> None:
        value, uow, database = runtime(tmp_path, driver=FailingContinuationDriver())
        await value.start()
        await value.client.start(request("exception"))
        await value.wait_idle(RunId("run-exception"))
        value.client.signal(
            RunId("run-exception"), signal_id="continuation-1", payload={"go": True}
        )
        await asyncio.sleep(0)
        await value.wait_idle(RunId("run-exception"))

        assert uow.read_run("run-exception").state is RunState.FAILED  # type: ignore[union-attr]
        assert uow.read_continuation("continuation-1").state is ContinuationState.ACKED  # type: ignore[union-attr]
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM continuation_progress_receipts"
            ).fetchone()[0]
            == 1
        )
        await value.close()
        database.close()

    asyncio.run(case())


def test_generic_signal_cannot_forge_reserved_conversation_continuation(
    tmp_path,
) -> None:
    async def case() -> None:
        value, uow, database = runtime(tmp_path, driver=ContinuationDriver())
        await value.start()
        await value.client.start(request("reserved-conversation-signal"))
        await value.wait_idle(RunId("run-reserved-conversation-signal"))

        with pytest.raises(
            ValueError,
            match="conversation_user continuations require signal_conversation",
        ):
            value.client.signal(
                RunId("run-reserved-conversation-signal"),
                signal_id="forged-conversation",
                payload={
                    "kind": "conversation_user",
                    "conversation": {},
                    "prepared_context": {},
                },
            )
        assert uow.read_continuation("forged-conversation") is None

        await value.close()
        database.close()

    asyncio.run(case())


def test_public_children_launch_returns_true_records_and_schedules(tmp_path) -> None:
    async def case() -> None:
        driver = Driver(state=RunState.WAITING)
        value, uow, database = runtime(tmp_path, driver=driver)
        value._profiles["workflow.child"] = value._profiles["agent.general"].__class__(
            "workflow.child", "react"
        )
        await value.start()
        await value.client.start(
            RunStart(
                ExecutionSessionId("session-1"),
                RunId("root-child"),
                RequestId("request-root-child"),
                "turn-root-child",
                {"prompt": "root"},
                1,
            )
        )
        await value.wait_idle(RunId("root-child"))
        launch = {
            "profile_key": "workflow.child",
            "driver_kind": "react",
            "catalog_generation": 1,
            "objective": "child",
        }
        ticket = ProfileLaunchTicket(
            "ticket-child",
            "root-child",
            "workflow.child",
            1,
            child_launch_fingerprint(launch),
        )
        uow.issue_profile_launch_ticket(ticket, now=11.0)
        handle = await value.children.launch(
            ChildLaunchRequest(
                ProfileLaunchTicketRef("ticket-child", 1),
                "command-child",
                "child-1",
                "request-child-1",
                AttachmentPolicy.DETACHED,
                launch,
                {
                    "schema_version": 1,
                    "profile_key": "workflow.child",
                    "driver_kind": "react",
                    "turn_id": "turn-child-1",
                    "tool_catalog_generation": 1,
                    "input": {"prompt": "child"},
                },
            )
        )
        assert handle.run.run_id == handle.command.child_run_id == "child-1"
        assert handle.ticket.child_run_id == "child-1"
        await value.wait_idle(RunId("child-1"))
        assert uow.read_run("child-1").state is RunState.WAITING  # type: ignore[union-attr]
        await value.close()
        database.close()

    asyncio.run(case())


def test_public_reconcile_and_delivery_delegate_without_private_access(
    tmp_path,
) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        calls = 0

        async def reconcile() -> None:
            nonlocal calls
            calls += 1

        async def run_once() -> bool:
            return False

        value._ports.reconciliation.reconcile = reconcile
        value._ports.delivery.run_once = run_once
        await value.start()
        await value.reconcile()
        assert calls == 2
        assert await value.dispatch_deliveries_once() is False
        await value.close()
        database.close()

    asyncio.run(case())
