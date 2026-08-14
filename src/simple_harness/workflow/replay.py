# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Read-only Native history and the canonical durable fork saga."""

from __future__ import annotations

import copy
import hashlib
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast

from simple_harness.contracts import JsonValue, canonical_json, validate_json_value
from simple_harness.execution.uow import RunRecord

from .errors import WorkflowContractError
from .execution_ports import (
    DangerousEffectConfirmation,
    DangerousEffectObservation,
    ForkPhase,
    ForkReceipt,
    ForkRequest,
    ForkWriteLease,
    WorkflowExecutionPorts,
    WorkflowTransaction,
)
from .native import NativeCheckpointStore, NativeSnapshotEnvelope
from .runner import WorkflowRegistry, manifest_hash

_FORK_NAMESPACE = uuid.UUID("43e3158d-908d-49f6-881d-f830c72fba40")
_ENGINE_HASH = hashlib.sha256(b"simple-harness-native-json-v1").hexdigest()
_RESERVED_PATCH_KEYS = frozenset(
    {
        "schema_version",
        "workflow_name",
        "workflow_version",
        "thread_id",
        "run_id",
        "session_id",
        "trace_id",
        "parent_run_id",
        "source_checkpoint_id",
    }
)


class WorkflowReplayError(WorkflowContractError):
    """Stable replay/fork rejection."""


def deterministic_fork_key(
    *,
    source_run_id: str,
    source_checkpoint_ns: str,
    source_checkpoint_id: str,
    source_version: int,
    state_patch: Mapping[str, JsonValue],
) -> str:
    if isinstance(source_version, bool) or source_version < 0:
        raise WorkflowReplayError("fork_invalid_version", "source version is invalid")
    patch = copy.deepcopy(dict(state_patch))
    validate_json_value(patch, path="$.state_patch")
    payload: dict[str, JsonValue] = {
        "source_run_id": source_run_id,
        "source_checkpoint_ns": source_checkpoint_ns,
        "source_checkpoint_id": source_checkpoint_id,
        "source_version": source_version,
        "state_patch": patch,
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def confirm_dangerous_effects(
    scope: str, observations: Sequence[DangerousEffectObservation]
) -> DangerousEffectConfirmation:
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("dangerous effect confirmation scope is required")
    ordered = tuple(sorted(observations, key=lambda item: item.effect_id))
    if len({item.effect_id for item in ordered}) != len(ordered):
        raise ValueError("dangerous effect observations contain duplicate identities")
    payload: list[JsonValue] = [
        {
            "effect_id": item.effect_id,
            "kind": item.kind,
            "state": item.state,
            "ledger_version": item.ledger_version,
            "request_hash": item.request_hash,
            "handoff_attempt": item.handoff_attempt,
        }
        for item in ordered
    ]
    digest = hashlib.sha256(
        canonical_json({"scope": scope, "observations": payload}).encode()
    ).hexdigest()
    return DangerousEffectConfirmation(scope, ordered, digest)


class WorkflowReplay:
    """Validate and advance one receipt-backed fork through all durable phases."""

    def __init__(
        self,
        *,
        execution_ports: WorkflowExecutionPorts,
        native_store: NativeCheckpointStore,
        registry: WorkflowRegistry,
        owner_id: str,
        clock=time.time,
        lease_ttl_seconds: float = 30.0,
    ) -> None:
        owner = execution_ports.unit_of_work.transaction_owner
        if (
            native_store.transaction_owner is not owner
            or execution_ports.replay.transaction_owner is not owner
        ):
            raise ValueError("workflow replay authorities have different owners")
        if not isinstance(registry, WorkflowRegistry):
            raise TypeError("workflow replay requires the exact SDK registry")
        if not owner_id:
            raise ValueError("workflow replay owner_id is required")
        if lease_ttl_seconds <= 0:
            raise ValueError("workflow replay lease_ttl_seconds must be positive")
        self.execution_ports = execution_ports
        self.native_store = native_store
        self.registry = registry
        self.owner_id = owner_id
        self._clock = clock
        self.lease_ttl_seconds = lease_ttl_seconds

    async def history(
        self, run_id: str, *, limit: int | None = None
    ) -> list[dict[str, JsonValue]]:
        if limit is not None and limit < 0:
            raise ValueError("history limit cannot be negative")
        if self.execution_ports.unit_of_work.read_run(run_id) is None:
            raise KeyError(f"workflow run not found: {run_id}")
        snapshots = await self.native_store.history(run_id=run_id, limit=limit)
        result: list[dict[str, JsonValue]] = []
        for snapshot in reversed(snapshots):
            pending_writes = [
                cast(JsonValue, item) for item in sorted(snapshot.node_writes)
            ]
            result.append({
                "checkpoint_id": snapshot.checkpoint_id,
                "checkpoint_ns": snapshot.checkpoint_ns,
                "parent_checkpoint_id": snapshot.parent_checkpoint_id,
                "state": snapshot.to_dict(),
                "metadata": copy.deepcopy(dict(snapshot.metadata)),
                "pending_writes": pending_writes,
                "engine_kind": snapshot.engine_kind,
            })
        return result

    @staticmethod
    def _validate_patch(
        patch: Mapping[str, JsonValue], source: NativeSnapshotEnvelope
    ) -> dict[str, JsonValue]:
        detached = copy.deepcopy(dict(patch))
        validate_json_value(detached, path="$.state_patch")
        if forbidden := sorted(_RESERVED_PATCH_KEYS & detached.keys()):
            raise WorkflowReplayError(
                "fork_reserved_identity_patch",
                f"state_patch cannot write runtime identity: {', '.join(forbidden)}",
            )
        for key, value in detached.items():
            if key.startswith("$") or "/" in key or "." in key:
                raise WorkflowReplayError(
                    "fork_non_root_patch_unsupported",
                    "fork accepts only root state channel replacements",
                )
            if key not in source.state:
                raise WorkflowReplayError(
                    "fork_unknown_state_channel",
                    f"state_patch channel does not exist: {key}",
                )
            current = source.state[key]
            if current is not None and value is not None:
                numeric = (
                    type(current) in {int, float}
                    and type(value) in {int, float}
                    and not isinstance(current, bool)
                    and not isinstance(value, bool)
                )
                if type(current) is not type(value) and not numeric:
                    raise WorkflowReplayError(
                        "fork_state_channel_type_mismatch",
                        f"state_patch changes the type of channel: {key}",
                    )
        return detached

    async def _source(
        self, run_id: str, checkpoint_id: str, expected_version: int
    ) -> tuple[RunRecord, Mapping[str, JsonValue], NativeSnapshotEnvelope]:
        run = self.execution_ports.unit_of_work.read_run(run_id)
        start = self.execution_ports.unit_of_work.read_start_snapshot(run_id)
        if not isinstance(run, RunRecord) or start is None:
            raise WorkflowReplayError("fork_source_not_found", "source run was not found")
        if run.driver_kind != "workflow" or run.version != expected_version:
            raise WorkflowReplayError(
                "fork_source_version_conflict", "source run identity changed"
            )
        namespace = start.get("checkpoint_namespace")
        thread_id = start.get("thread_id")
        if not isinstance(namespace, str) or not isinstance(thread_id, str):
            raise WorkflowReplayError(
                "fork_source_identity_incomplete",
                "source start identity is incomplete",
            )
        execution = await self.native_store.load_execution(
            run_id=run_id,
            thread_id=thread_id,
            checkpoint_ns=namespace,
            checkpoint_id=checkpoint_id,
        )
        if (
            execution.pending_results
            or execution.route_selections
            or execution.pending_consumed_interrupt_ids
        ):
            raise WorkflowReplayError(
                "fork_pending_writes_unsupported",
                "source checkpoint has pending durable writes",
            )
        if len(execution.snapshot.frontier) > 1:
            raise WorkflowReplayError(
                "fork_fanout_unsupported", "source checkpoint has active fan-out"
            )
        if execution.snapshot.interrupt is not None:
            raise WorkflowReplayError(
                "fork_interrupt_pending", "source checkpoint has a pending decision"
            )
        return run, start, execution.snapshot

    async def fork_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str,
        expected_version: int,
        state_patch: Mapping[str, JsonValue] | None = None,
        fork_key: str | None = None,
        dangerous_confirmation: DangerousEffectConfirmation | None = None,
    ) -> dict[str, JsonValue]:
        detached_patch = copy.deepcopy(dict(state_patch or {}))
        validate_json_value(detached_patch, path="$.state_patch")
        start_identity = self.execution_ports.unit_of_work.read_start_snapshot(run_id)
        namespace = (
            start_identity.get("checkpoint_namespace")
            if start_identity is not None
            else None
        )
        if not isinstance(namespace, str):
            raise WorkflowReplayError(
                "fork_source_identity_incomplete",
                "source checkpoint namespace is missing",
            )
        calculated_key = deterministic_fork_key(
            source_run_id=run_id,
            source_checkpoint_ns=namespace,
            source_checkpoint_id=checkpoint_id,
            source_version=expected_version,
            state_patch=detached_patch,
        )
        if fork_key is not None and fork_key != calculated_key:
            raise WorkflowReplayError(
                "fork_key_mismatch", "fork key does not match the request"
            )
        fork_id = fork_key or calculated_key
        existing = self.execution_ports.replay.read_fork(fork_id)
        if existing is not None:
            if (
                existing.request.source_run_id != run_id
                or existing.request.source_namespace != namespace
                or existing.request.source_checkpoint_id != checkpoint_id
                or existing.request.source_run_version != expected_version
                or canonical_json(dict(existing.request.patch))
                != canonical_json(detached_patch)
            ):
                raise WorkflowReplayError(
                    "fork_request_conflict", "fork identity has another request"
                )
            if existing.phase is ForkPhase.COMMITTED:
                return self._result(existing, created=False)
            if existing.phase is ForkPhase.ROLLED_BACK:
                raise WorkflowReplayError(
                    "fork_rolled_back", "fork request was rolled back"
                )
        run, start, source = await self._source(
            run_id, checkpoint_id, expected_version
        )
        patch = self._validate_patch(detached_patch, source)
        workflow_name = start.get("workflow_name")
        workflow_version = start.get("workflow_version")
        manifest_digest = start.get("manifest_hash")
        implementation = start.get("implementation_hash")
        namespace = start.get("checkpoint_namespace")
        if not all(
            isinstance(value, str)
            for value in (
                workflow_name,
                workflow_version,
                manifest_digest,
                implementation,
                namespace,
            )
        ):
            raise WorkflowReplayError(
                "fork_source_identity_incomplete", "source graph identity is incomplete"
            )
        entry = self.registry.require(
            str(workflow_name),
            str(workflow_version),
            expected_manifest_hash=str(manifest_digest),
            expected_implementation_hash=str(implementation),
        )
        if source.state_schema_version != entry.manifest.state_schema_version:
            raise WorkflowReplayError(
                "fork_state_schema_mismatch", "source state schema changed"
            )
        snapshot = self.execution_ports.recovery.read_recovery_snapshot(run_id)
        if snapshot.candidate.run_version != run.version:
            raise WorkflowReplayError(
                "fork_source_snapshot_conflict", "source durable snapshot changed"
            )
        source_head = snapshot.candidate.checkpoint_head
        if not isinstance(source_head, str):
            raise WorkflowReplayError("fork_source_head_missing", "source head is missing")
        request_without_fingerprint: dict[str, JsonValue] = {
            "fork_id": fork_id,
            "source_run_id": run_id,
            "source_namespace": str(namespace),
            "source_checkpoint_id": checkpoint_id,
            "source_run_version": expected_version,
            "source_head": source_head,
            "engine_hash": _ENGINE_HASH,
            "manifest_hash": manifest_hash(entry.manifest),
            "implementation_hash": entry.manifest.implementation_bundle_hash,
            "schema_hash": entry.manifest.state_hash,
            "patch": patch,
            "dangerous_confirmation": (
                None
                if dangerous_confirmation is None
                else {
                    "scope": dangerous_confirmation.scope,
                    "digest": dangerous_confirmation.digest,
                    "observations": [
                        {
                            "effect_id": item.effect_id,
                            "kind": item.kind,
                            "state": item.state,
                            "ledger_version": item.ledger_version,
                            "request_hash": item.request_hash,
                            "handoff_attempt": item.handoff_attempt,
                        }
                        for item in dangerous_confirmation.observations
                    ],
                }
            ),
        }
        fingerprint = hashlib.sha256(
            canonical_json(request_without_fingerprint).encode()
        ).hexdigest()
        request = ForkRequest(
            fork_id,
            fingerprint,
            run_id,
            str(namespace),
            checkpoint_id,
            expected_version,
            source_head,
            _ENGINE_HASH,
            manifest_hash(entry.manifest),
            entry.manifest.implementation_bundle_hash,
            entry.manifest.state_hash,
            patch,
            dangerous_confirmation,
        )

        async def prepare(transaction: WorkflowTransaction) -> ForkReceipt:
            return await self.execution_ports.replay.prepare_fork(
                transaction,
                request,
                snapshot,
                now=float(self._clock()),
            )

        receipt = await self.execution_ports.unit_of_work.run_atomic(
            prepare, fault_label="workflow:fork-prepare"
        )
        if receipt.phase is ForkPhase.ROLLED_BACK:
            raise WorkflowReplayError("fork_rolled_back", "fork request was rolled back")
        if receipt.phase is ForkPhase.COMMITTED:
            return self._result(receipt, created=False)
        lease = await self._claim_or_resume(receipt)
        if receipt.phase in {ForkPhase.PREPARED, ForkPhase.CLAIMED}:
            target_state = copy.deepcopy(dict(source.state))
            target_state.update(patch)
            target_snapshot = replace(
                source,
                thread_id=receipt.target_thread_id,
                checkpoint_id=receipt.target_checkpoint_id,
                parent_checkpoint_id=None,
                run_id=receipt.target_run_id,
                state=target_state,
                metadata={
                    **source.metadata,
                    "fork_id": fork_id,
                    "source_run_id": run_id,
                    "source_checkpoint_id": checkpoint_id,
                },
            )

            async def checkpoint(transaction: WorkflowTransaction) -> ForkReceipt:
                return await self.execution_ports.replay.checkpoint_fork(
                    transaction,
                    lease,
                    None,
                    hashlib.sha256(f"{fork_id}|checkpoint".encode()).hexdigest(),
                    target_snapshot.to_dict(),
                    now=float(self._clock()),
                )

            receipt = await self.execution_ports.unit_of_work.run_atomic(
                checkpoint, fault_label="workflow:fork-checkpoint"
            )
            if receipt.phase is ForkPhase.ROLLED_BACK:
                raise WorkflowReplayError(
                    "fork_source_changed", "source authority changed during fork"
                )
        if receipt.phase is ForkPhase.CHECKPOINTED and lease.mode != "commit_only":
            lease = replace(
                lease, expected_receipt_version=receipt.version, mode="commit_only"
            )

        async def commit(transaction: WorkflowTransaction) -> ForkReceipt:
            return await self.execution_ports.replay.commit_fork(
                transaction,
                lease,
                receipt.version,
                now=float(self._clock()),
            )

        committed = await self.execution_ports.unit_of_work.run_atomic(
            commit, fault_label="workflow:fork-commit"
        )
        if committed.phase is not ForkPhase.COMMITTED:
            raise WorkflowReplayError(
                "fork_not_committed", "fork did not reach its committed phase"
            )
        return self._result(committed, created=True)

    async def _claim_or_resume(self, receipt: ForkReceipt) -> ForkWriteLease:
        now = float(self._clock())
        if (
            receipt.phase in {ForkPhase.CLAIMED, ForkPhase.CHECKPOINTED}
            and receipt.claim_owner == self.owner_id
            and receipt.claim_epoch is not None
            and receipt.claim_expires_at is not None
            and receipt.claim_expires_at > now
        ):
            return ForkWriteLease(
                receipt.request.fork_id,
                receipt.target_run_id,
                self.owner_id,
                receipt.claim_epoch,
                receipt.claim_expires_at,
                receipt.version,
                "commit_only"
                if receipt.phase is ForkPhase.CHECKPOINTED
                else "write",
            )

        async def claim(transaction: WorkflowTransaction) -> ForkWriteLease:
            return await self.execution_ports.replay.claim_fork(
                transaction,
                receipt.request.fork_id,
                receipt.version,
                self.owner_id,
                now=now,
                ttl_seconds=self.lease_ttl_seconds,
            )

        return await self.execution_ports.unit_of_work.run_atomic(
            claim, fault_label="workflow:fork-claim"
        )

    @staticmethod
    def _result(receipt: ForkReceipt, *, created: bool) -> dict[str, JsonValue]:
        return {
            "fork_id": receipt.request.fork_id,
            "source_run_id": receipt.request.source_run_id,
            "source_checkpoint_ns": receipt.request.source_namespace,
            "source_checkpoint_id": receipt.request.source_checkpoint_id,
            "run_id": receipt.target_run_id,
            "trace_id": receipt.target_trace_id,
            "thread_id": receipt.target_thread_id,
            "checkpoint_ns": receipt.request.source_namespace,
            "checkpoint_id": receipt.target_checkpoint_id,
            "status": "created",
            "saga_status": receipt.phase.value,
            "created": created,
            "idempotent": not created,
        }


__all__ = (
    "WorkflowReplay",
    "WorkflowReplayError",
    "confirm_dangerous_effects",
    "deterministic_fork_key",
)
