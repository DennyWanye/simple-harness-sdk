# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""CAS persistence for durable planning-repair continuations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from .store import Store, StoreConflict

ACTIVE_FENCE_STATES = ("WAITING", "READY", "PAUSED", "MANUAL_REQUIRED", "STALE_FENCED")


@dataclass(frozen=True, slots=True)
class StoredPlanningRepairContinuation:
    continuation_id: str
    mission_id: str
    decision_id: str
    request_id: str
    command_id: str
    raw_artifact_ref: Mapping[str, Any]
    raw_hash: str
    decision_hash: str
    codec_version: str
    protocol_version: str
    package_hash: str
    prompt_hash: str
    base_plan_revision: int
    requirements_revision: int
    authority_binding: Mapping[str, Any]
    targets: tuple[Mapping[str, Any], ...]
    last_preview_hash: str
    state: str
    row_version: int
    policy_version: str
    created_at_ms: int
    deadline_ms: int
    resume_limit: int
    resume_count: int
    next_check_at_ms: int
    owner_id: str | None
    lease_until_ms: int | None
    last_reason_code: str | None
    updated_at_ms: int


def _decode(row: Any) -> StoredPlanningRepairContinuation | None:
    if row is None:
        return None
    value = dict(row)
    try:
        raw_ref = json.loads(value.pop("raw_artifact_ref_json"))
        authority = json.loads(value.pop("authority_binding_json"))
        targets = tuple(json.loads(value.pop("targets_json")))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StoreConflict("planning repair continuation JSON is unreadable") from error
    return StoredPlanningRepairContinuation(
        raw_artifact_ref=raw_ref,
        authority_binding=authority,
        targets=targets,
        **value,
    )


class PlanningRepairStore:
    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, continuation_id: str) -> StoredPlanningRepairContinuation | None:
        row = self._store.connection.execute(
            "SELECT * FROM planning_repair_continuations WHERE continuation_id=?",
            (str(continuation_id),),
        ).fetchone()
        return _decode(row)

    def get_by_decision(self, decision_id: str) -> StoredPlanningRepairContinuation | None:
        row = self._store.connection.execute(
            "SELECT * FROM planning_repair_continuations WHERE decision_id=?",
            (str(decision_id),),
        ).fetchone()
        return _decode(row)

    def put(self, value: StoredPlanningRepairContinuation) -> StoredPlanningRepairContinuation:
        fields = tuple(value.__dataclass_fields__)
        encoded = {
            **{name: getattr(value, name) for name in fields},
            "raw_artifact_ref_json": canonical_json(dict(value.raw_artifact_ref)),
            "authority_binding_json": canonical_json(dict(value.authority_binding)),
            "targets_json": canonical_json([dict(item) for item in value.targets]),
        }
        for name in ("raw_artifact_ref", "authority_binding", "targets"):
            encoded.pop(name)
        columns = tuple(encoded)
        values = tuple(encoded[name] for name in columns)
        with self._store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM planning_repair_continuations WHERE continuation_id=? "
                "OR decision_id=? OR command_id=?",
                (value.continuation_id, value.decision_id, value.command_id),
            ).fetchone()
            if existing is not None:
                parsed = _decode(existing)
                if parsed != value:
                    raise StoreConflict("planning repair continuation identity conflict")
                assert parsed is not None
                return parsed
            connection.execute(
                "INSERT INTO planning_repair_continuations("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                values,
            )
        stored = self.get(value.continuation_id)
        assert stored is not None
        return stored

    def list_active_fences(self, mission_id: str) -> tuple[StoredPlanningRepairContinuation, ...]:
        marks = ",".join("?" for _ in ACTIVE_FENCE_STATES)
        rows = self._store.connection.execute(
            f"SELECT * FROM planning_repair_continuations WHERE mission_id=? "
            f"AND state IN ({marks}) ORDER BY created_at_ms,continuation_id",
            (str(mission_id), *ACTIVE_FENCE_STATES),
        ).fetchall()
        return tuple(item for row in rows if (item := _decode(row)) is not None)

    def claim_due(
        self, *, owner_id: str, now_ms: int, lease_until_ms: int
    ) -> StoredPlanningRepairContinuation | None:
        with self._store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM planning_repair_continuations WHERE state IN ('WAITING','READY') "
                "AND next_check_at_ms<=? AND (owner_id IS NULL OR lease_until_ms<=?) "
                "ORDER BY next_check_at_ms,created_at_ms,continuation_id LIMIT 1",
                (int(now_ms), int(now_ms)),
            ).fetchone()
            current = _decode(row)
            if current is None:
                return None
            cursor = connection.execute(
                "UPDATE planning_repair_continuations SET owner_id=?,lease_until_ms=?,"
                "row_version=row_version+1,updated_at_ms=? WHERE continuation_id=? "
                "AND row_version=? AND state IN ('WAITING','READY') "
                "AND next_check_at_ms<=? AND (owner_id IS NULL OR lease_until_ms<=?)",
                (
                    str(owner_id),
                    int(lease_until_ms),
                    int(now_ms),
                    current.continuation_id,
                    current.row_version,
                    int(now_ms),
                    int(now_ms),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self.get(current.continuation_id)

    def transition(
        self,
        current: StoredPlanningRepairContinuation,
        *,
        owner_id: str,
        new_state: str,
        now_ms: int,
        next_check_at_ms: int,
        resume_count: int,
        reason: str | None,
        preview_hash: str | None = None,
    ) -> StoredPlanningRepairContinuation:
        with self._store.transaction() as connection:
            cursor = connection.execute(
                "UPDATE planning_repair_continuations SET state=?,row_version=row_version+1,"
                "last_reason_code=?,updated_at_ms=?,next_check_at_ms=?,resume_count=?,"
                "last_preview_hash=COALESCE(?,last_preview_hash),"
                "owner_id=NULL,lease_until_ms=NULL WHERE continuation_id=? AND row_version=? "
                "AND state=? AND owner_id=? AND lease_until_ms>?",
                (
                    str(new_state),
                    reason,
                    int(now_ms),
                    int(next_check_at_ms),
                    int(resume_count),
                    preview_hash,
                    current.continuation_id,
                    current.row_version,
                    current.state,
                    str(owner_id),
                    int(now_ms),
                ),
            )
            if cursor.rowcount != 1:
                raise StoreConflict("planning repair continuation lease/version changed")
            self._record_transition(current, str(new_state), reason, int(now_ms))
        stored = self.get(current.continuation_id)
        assert stored is not None
        return stored

    def _record_transition(self, current, state: str, reason: str | None, now_ms: int) -> None:
        from ..orchestrator.hierarchical_dispatch import append_hierarchical_event

        append_hierarchical_event(self._store, "PlanningRepairStateChanged", current.mission_id,
            key=f"{current.continuation_id}:{current.row_version + 1}",
            payload={"continuation_id": current.continuation_id, "decision_id": current.decision_id,
                "from_state": current.state, "state": state, "reason": reason,
                "requires_manual_action": state in {"MANUAL_REQUIRED", "STALE_FENCED"},
                "observed_at_ms": now_ms})

    def administrative_transition(
        self,
        current: StoredPlanningRepairContinuation,
        *,
        new_state: str,
        now_ms: int,
        next_check_at_ms: int,
        reason: str | None,
    ) -> StoredPlanningRepairContinuation:
        with self._store.transaction() as connection:
            cursor = connection.execute(
                "UPDATE planning_repair_continuations SET state=?,row_version=row_version+1,"
                "last_reason_code=?,updated_at_ms=?,next_check_at_ms=?,"
                "owner_id=NULL,lease_until_ms=NULL WHERE continuation_id=? "
                "AND row_version=? AND state=?",
                (
                    str(new_state),
                    reason,
                    int(now_ms),
                    int(next_check_at_ms),
                    current.continuation_id,
                    current.row_version,
                    current.state,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreConflict("planning repair continuation version changed")
            self._record_transition(current, str(new_state), reason, int(now_ms))
        stored = self.get(current.continuation_id)
        assert stored is not None
        return stored


__all__ = (
    "ACTIVE_FENCE_STATES",
    "PlanningRepairStore",
    "StoredPlanningRepairContinuation",
)
