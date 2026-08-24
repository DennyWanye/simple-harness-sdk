# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import inspect

from future_consumer_fixture import FutureConsumerCapabilityFixture

from simple_harness.contracts import CallId, EffectId, RequestId, RunId
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.tools import (
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationResult,
    CancellationToken,
    EffectExecutor,
    ExecutableToolRecord,
    ReconciliationObservation,
    ReconciliationState,
    ToolCall,
    ToolContext,
    ToolExposureMode,
    ToolResult,
)


class ConsumerPermission:
    def __init__(self) -> None:
        self.authorized: list[str] = []

    async def authorize(self, prepared) -> AuthorizationResult:  # type: ignore[no-untyped-def]
        self.authorized.append(prepared.effect_id.value)
        return AuthorizationResult(
            AuthorizationDecision.ALLOW,
            receipt_ref=f"consumer:allow:{prepared.effect_id.value}",
        )

    async def bind_effect_handoff(
        self, prepared, authorization_receipt_ref, sdk_receipt
    ) -> AuthorizationReceipt:  # type: ignore[no-untyped-def]
        del prepared, authorization_receipt_ref
        identity = "consumer:effect-handoff"
        return AuthorizationReceipt(
            identity,
            hashlib.sha256(identity.encode()).hexdigest(),
            sdk_receipt.receipt_hash,
        )


class ConsumerReconciliation:
    async def observe(self, prepared) -> ReconciliationObservation:  # type: ignore[no-untyped-def]
        return ReconciliationObservation(
            ReconciliationState.STILL_UNKNOWN,
            f"consumer:unknown:{prepared.effect_id.value}",
        )


async def _exercise_future_consumer_catalog(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    physical_calls: list[str] = []

    def handler(arguments, context):  # type: ignore[no-untyped-def]
        physical_calls.append(str(arguments["path"]))
        assert context.call_id is not None
        return ToolResult.succeeded(context.call_id, {"text": "fixture"})

    permission = ConsumerPermission()
    source = ExecutableToolRecord(
        capability_id="consumer:read_note",
        namespace="consumer",
        source="future-consumer",
        source_revision="fixture-v1",
        exposure_mode=ToolExposureMode.DEFERRED,
        provider_name="read_note",
        description="Read one note from the consumer workspace.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    fixture = FutureConsumerCapabilityFixture(source, permission, handler)
    catalog, exposure, registry = fixture.build()
    run_id = RunId("future-consumer-catalog-run")
    exposure.restore(run_id, None)

    found = exposure.search(run_id, "consumer workspace")
    described = exposure.describe(run_id, found.items[0].capability_id)
    exposure.activate(run_id, source.capability_id, described.nonce)
    assert [item.name for item in exposure.provider_specs(run_id)] == ["read_note"]
    assert physical_calls == []

    database = Database.open(tmp_path / "future-consumer-catalog.sqlite3")
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="future-consumer-session",
        run_id=run_id.value,
        request_id="future-consumer-request",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"catalog_generation": catalog.snapshot.generation},
        event_id="future-consumer-created",
        now=1.0,
    )
    _run, lease = uow.claim_runtime_activation(
        run_id=run_id.value,
        owner_id="future-consumer-worker",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=30.0,
    )
    fence = await uow.acquire(run_id, lease, now=2.0)
    executor = EffectExecutor(
        uow=uow,
        registry=registry,
        authorization=permission,
        reconciliation=ConsumerReconciliation(),
        clock=lambda: 3.0,
    )
    execution = await executor.execute(
        effect_id=EffectId("future-consumer-effect"),
        call=ToolCall(CallId("future-consumer-call"), "read_note", {"path": "note.md"}),
        context=ToolContext(
            run_id,
            RequestId("future-consumer-request"),
            CancellationToken(),
        ),
        execution_lease=lease,
        run_fence=fence,
    )

    assert execution.result.value == {"text": "fixture"}
    assert physical_calls == ["note.md"]
    assert permission.authorized == ["future-consumer-effect"]
    row = database.connection.execute(
        "SELECT state,authorization_receipt_ref,handoff_receipt_ref "
        "FROM execution_effects WHERE effect_id=?",
        ("future-consumer-effect",),
    ).fetchone()
    assert row is not None
    assert row[0] == "succeeded"
    assert row[1] == "consumer:allow:future-consumer-effect"
    assert str(row[2]).startswith("authorization-binding-v1:")
    database.close()


def test_future_consumer_catalog_keeps_execution_behind_authorization_and_ledger(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    asyncio.run(_exercise_future_consumer_catalog(tmp_path))


def test_future_consumer_catalog_fixture_has_no_host_imports() -> None:
    source = inspect.getsource(__import__("future_consumer_fixture"))
    assert "deskpet" not in source
    assert "tauri" not in source
    assert "fastapi" not in source
