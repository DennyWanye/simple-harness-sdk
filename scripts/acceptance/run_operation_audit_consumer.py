# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Standalone installed public consumer: deterministic adapters, actual SQLite kernel.

Run from an external working directory with no PYTHONPATH and an exact wheel installed.
No SDK tests, private module, source overlay, Provider network or credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

import simple_harness
from simple_harness import CallId, ExecutionSessionId, Message, MessageRole, RequestId, RunId
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.providers import ProviderResponse, ProviderTarget, ProviderToolCall
from simple_harness.runtime import (
    RunStart,
    RuntimePorts,
    RuntimeProfile,
    SqliteContextPort,
    build_runtime,
)
from simple_harness.runtime.drivers import ReActDriver
from simple_harness.tools import EffectExecutor, FunctionTool, ToolRegistry, ToolResult, ToolSpec
from simple_harness.tools.authorization import (
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationResult,
)
from simple_harness.tools.reconciliation import ReconciliationObservation, ReconciliationState

CANARY = "opaqueInstalledConsumer4931"


class Provider:
    target = ProviderTarget("deterministic", "model", "model", "local", "deterministic")

    def __init__(self):
        self.calls = 0

    async def invoke(self, request, *, cancel):
        self.calls += 1
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "done"),
            model="model",
            tool_calls=tuple(
                ProviderToolCall(CallId(CANARY + mode), "audit_action", {"mode": mode})
                for mode in ("success", "failure", "deny")
            )
            if self.calls == 1
            else (),
        )


class Authority:
    async def prepare(self, prepared):
        if prepared.call.arguments["mode"] == "deny":
            return AuthorizationResult(AuthorizationDecision.DENY, reason_code="blocked")
        return AuthorizationResult(AuthorizationDecision.ALLOW, receipt_ref="accepted")

    async def bind_effect_handoff(self, prepared, authorization_receipt_ref, sdk_receipt):
        return AuthorizationReceipt(
            "handoff", hashlib.sha256(b"handoff").hexdigest(), sdk_receipt.receipt_hash
        )


class Reconciliation:
    async def observe(self, prepared):
        return ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "unknown")


class Noop:
    async def reconcile(self):
        return None


class Sink:
    async def deliver(self, payload, *, idempotency_key):
        return None


class Catalog:
    def current_generation(self):
        return 1


async def main(folder):
    assert not os.environ.get("PYTHONPATH"), "consumer must not use source overlay"
    assert "site-packages" in str(Path(simple_harness.__file__).resolve())
    assert version("simple-harness-sdk") == sys.argv[2]
    folder.mkdir(parents=True, exist_ok=True)
    database = Database.open(folder / "consumer.db")
    uow = SqliteExecutionUnitOfWork(database)
    registry, provider, authority = ToolRegistry(), Provider(), Authority()
    physical = {"success": 0, "failure": 0, "deny": 0}

    async def action(arguments, context):
        mode = arguments["mode"]
        physical[mode] += 1
        return (
            ToolResult.failed(context.call_id, CANARY, CANARY)
            if mode == "failure"
            else ToolResult.succeeded(context.call_id, {"private": CANARY})
        )

    registry.register(
        FunctionTool(
            ToolSpec(
                "audit_action",
                "Exercise a recorded action.",
                {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["success", "failure", "deny"]}
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
            ),
            action,
        )
    )

    def clock():
        return 10.0

    reconciliation = Reconciliation()
    runtime = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "react")},
        {"react": ReActDriver(clock=clock)},
        RuntimePorts(
            provider=ProviderInvocationCoordinator(
                uow=uow,
                provider=provider,
                budget_policy=BudgetPolicy(),
                estimator=FrozenPriceEstimator("price", "model", 0, 0),
                clock=clock,
            ),
            tools=EffectExecutor(
                uow=uow,
                registry=registry,
                authorization=authority,
                reconciliation=reconciliation,
                clock=clock,
            ),
            authorization=authority,
            context=SqliteContextPort(database, clock=clock),
            delivery=DeliveryDispatcher(uow, {"fixture": Sink()}, clock=clock),
            tool_reconciliation=reconciliation,
            reconciliation=Noop(),
            provider_reconciliation=Noop(),
            react_checkpoint=uow,
            tool_catalog=Catalog(),
            owner_id="installed-consumer",
            clock=clock,
        ),
    )
    run_id = RunId("installed-audit-run")
    await runtime.start()
    await runtime.client.start(
        RunStart(
            ExecutionSessionId("installed-session"),
            run_id,
            RequestId("installed-request"),
            "turn",
            {
                "messages": [{"role": "user", "content": "exercise audit"}],
                "capability_snapshot": {"tools": ["audit_action"]},
                "max_output_tokens": 100,
            },
            1,
        )
    )
    await runtime.wait_idle(run_id)
    assert uow.read_run(run_id.value).state.value == "completed"
    assert physical == {"success": 1, "failure": 1, "deny": 0} and provider.calls == 2
    dispatch_before = provider.calls + sum(physical.values())
    expected = await runtime.client.read_run_operation_audit(run_id, limit=4096)
    assert not expected.truncated
    assert expected.terminal_evidence is not None
    assert expected.terminal_evidence.state == "completed"
    assert (
        expected.terminal_evidence.event_record_hash
        != expected.terminal_evidence.event_payload_hash
    )
    first = await runtime.client.open_run_operation_audit(run_id, page_size=3)
    values = [o.to_json() for o in first.operations]
    snapshot_hash, cursor = first.snapshot_hash, first.next_cursor
    await runtime.close()
    database.close()
    reopened = Database.open(folder / "consumer.db")
    reader = SqliteExecutionUnitOfWork(reopened)
    while cursor:
        page = reader.read_run_operation_audit_page(run_id, cursor=cursor)
        assert page.snapshot_hash == snapshot_hash
        from simple_harness import RunTerminalAuditEvidenceV1

        assert (
            RunTerminalAuditEvidenceV1.from_json(page.to_json()["metadata"]["terminal_evidence"])
            == expected.terminal_evidence
        )
        values.extend(o.to_json() for o in page.operations)
        cursor = page.next_cursor
    assert values == [o.to_json() for o in expected.operations]
    assert CANARY not in json.dumps(values)
    heads = [o for o in values if o["kind"] == "effect" and o["record_type"] == "head"]
    assert sorted(o["state"] for o in heads) == ["failed", "succeeded"]
    denied = [o for o in values if o["kind"] == "tool" and o["state"] == "rejected"]
    assert len(denied) == 1 and denied[0]["effect_id"] not in {o["effect_id"] for o in heads}
    assert physical == {"success": 1, "failure": 1, "deny": 0} and provider.calls == 2
    reopened.close()
    print(
        json.dumps(
            dict(
                version=version("simple-harness-sdk"),
                module=simple_harness.__file__,
                physical=physical,
                provider_calls=provider.calls,
                audit_dispatch_delta=provider.calls + sum(physical.values()) - dispatch_before,
                operations=len(values),
                snapshot_hash=snapshot_hash,
                history_coverage=first.metadata["history_coverage"],
                terminal_evidence=expected.terminal_evidence.to_json(),
                coverage_gaps=list(first.metadata["coverage_gaps"]),
                result="PASS",
            )
        )
    )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
