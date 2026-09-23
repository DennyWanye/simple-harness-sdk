# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Durable TaskGraph notifications, using the original Store transaction owner."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
from ..graph.notification_contracts import FollowupCauseRef, FollowupV1, _hash, _integer, _text
from ..planning.htn.grounding import derive_id
from .htn_store import HtnStore
from .store import Store, StoreConflict, StoreError


class ReceiptKind(StrEnum):
    EVENT = "EVENT"
    DISPATCH_INTENT = "DISPATCH_INTENT"
    PLAN_COMMIT = "PLAN_COMMIT"


@dataclass(frozen=True, slots=True, kw_only=True)
class DurableFollowupReceipt:
    kind: ReceiptKind
    identity: str
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ReceiptKind(self.kind))
        _text(self.identity, "receipt.identity")
        _hash(self.content_hash, "receipt.content_hash")

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "identity": self.identity,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimedFollowup:
    message_id: str
    message: FollowupV1
    owner: str
    row_version: int
    attempts: int
    lease_until_ms: int

    @property
    def command_key(self) -> str:
        return derive_id("tg-followup-consumer", self.message_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class BlockedFollowup:
    """An unreadable delivery was isolated without invoking its consumer."""
    message_id: str


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class TaskGraphFollowupStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def convergence_diagnostics(self, mission_id: str, job_id: str) -> tuple[dict[str, Any], ...]:
        """References to actual durable delivery/consumer records, from one read."""
        with self.store.read_view() as db:
            if db.execute("SELECT 1 FROM taskgraph_convergence_jobs WHERE mission_id=? AND job_id=?",
                          (mission_id, job_id)).fetchone() is None:
                raise StoreError("TASKGRAPH_CONVERGENCE_MISSING")
            rows = []
            for row in db.execute("SELECT * FROM taskgraph_followups WHERE mission_id=? "
                                  "AND kind='CONVERGE' AND subject_key=? ORDER BY message_id", (mission_id, job_id)):
                message = FollowupV1.from_json(json.loads(row["payload_json"]))
                if (message.mission_id != mission_id or message.subject_key != job_id
                        or str(message.kind) != "CONVERGE" or message.source_event_id != row["source_event_id"]
                        or _digest(message.to_json()) != row["payload_hash"]
                        or canonical_json(message.to_json()) != row["payload_json"]):
                    raise StoreError("TASKGRAPH_FOLLOWUP_CORRUPT")
                if row["delivery_state"] == "ACKED":
                    receipt = DurableFollowupReceipt(**json.loads(row["consumer_receipt_json"]))
                    actual = self.consumer_receipt(mission_id,
                        derive_id("tg-followup-consumer", row["message_id"]), receipt.kind, receipt.identity)
                    if actual != receipt:
                        raise StoreError("TASKGRAPH_FOLLOWUP_RECEIPT_CHANGED")
                rows.append(dict(row))
            if not rows:
                raise StoreError("TASKGRAPH_CONVERGENCE_NOTIFICATION_MISSING")
            # A long external wait can produce many durable wakes. Reference the
            # entire verified collection rather than truncating history or making
            # the bounded public view unreadable after its sixty-fifth delivery.
            # Every row version is monotone and rows are retained, so their sum is
            # a revision of this original collection, not an effect receipt.
            return (FollowupCauseRef(kind="taskgraph_followup_set", id=job_id,
                revision=sum(row["row_version"] for row in rows),
                content_hash=_digest({"mission_id": mission_id, "job_id": job_id,
                                      "followups": rows})).to_json(),)

    def append_followup(self, message: FollowupV1, *, now_ms: int) -> str:
        """Must join the transaction persisting the original event or its cursor."""
        _integer(now_ms, "now_ms")
        if not self.store.connection.in_transaction:
            raise StoreError("TASKGRAPH_FOLLOWUP_REQUIRES_SOURCE_TRANSACTION")
        payload = canonical_json(message.to_json())
        digest = _digest(message.to_json())
        dedupe = derive_id(
            "tg-followup-dedupe", message.source_event_id, message.kind, message.subject_key
        )
        identity = derive_id("tg-followup", dedupe)
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT message_id,payload_hash,payload_json FROM taskgraph_followups WHERE dedupe_key=?",
                (dedupe,),
            ).fetchone()
            if previous is not None:
                if previous[1] != digest or previous[2] != payload:
                    raise StoreConflict("TASKGRAPH_FOLLOWUP_PAYLOAD_CONFLICT")
                return str(previous[0])
            db.execute(
                "INSERT INTO taskgraph_followups "
                "(message_id,mission_id,source_event_id,kind,subject_key,dedupe_key,payload_hash,payload_json,"
                "delivery_state,attempts,row_version,next_attempt_ms) VALUES (?,?,?,?,?,?,?,?,'PENDING',0,1,?)",
                (
                    identity,
                    message.mission_id,
                    message.source_event_id,
                    str(message.kind),
                    message.subject_key,
                    dedupe,
                    digest,
                    payload,
                    now_ms,
                ),
            )
        return identity

    def claim_followup(
        self, owner: str, *, now_ms: int, lease_ms: int = 30_000
    ) -> ClaimedFollowup | BlockedFollowup | None:
        _text(owner, "owner")
        _integer(now_ms, "now_ms")
        _integer(lease_ms, "lease_ms", minimum=1)
        until = _integer(now_ms + lease_ms, "lease_until_ms")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT message_id,row_version FROM taskgraph_followups WHERE "
                "(delivery_state='PENDING' AND next_attempt_ms<=?) OR "
                "(delivery_state='LEASED' AND lease_until_ms<=?) ORDER BY next_attempt_ms,message_id LIMIT 1",
                (now_ms, now_ms),
            ).fetchone()
            if row is None:
                return None
            changed = db.execute(
                "UPDATE taskgraph_followups SET delivery_state='LEASED',lease_owner=?,lease_until_ms=?,"
                "attempts=attempts+1,row_version=row_version+1 WHERE message_id=? AND row_version=? AND "
                "((delivery_state='PENDING' AND next_attempt_ms<=?) OR (delivery_state='LEASED' AND lease_until_ms<=?))",
                (owner, until, row[0], row[1], now_ms, now_ms),
            )
            if changed.rowcount != 1:
                raise StoreConflict("TASKGRAPH_FOLLOWUP_CLAIM_CONFLICT")
            claimed = db.execute(
                "SELECT * FROM taskgraph_followups WHERE message_id=?", (row[0],)
            ).fetchone()
            try:
                message = FollowupV1.from_json(json.loads(claimed["payload_json"]))
                if (
                    _digest(message.to_json()) != claimed["payload_hash"]
                    or canonical_json(message.to_json()) != claimed["payload_json"]
                    or message.mission_id != claimed["mission_id"]
                    or message.source_event_id != claimed["source_event_id"]
                    or str(message.kind) != claimed["kind"]
                    or message.subject_key != claimed["subject_key"]
                ):
                    raise StoreError("TASKGRAPH_FOLLOWUP_CORRUPT")
            except (ContractError, StoreError, ValueError, TypeError):
                # Retain the exact damaged bytes and all convergence holds. A
                # corrupt first row must not roll back its claim on every tick
                # and prevent unrelated Missions from receiving their work.
                db.execute(
                    "UPDATE taskgraph_followups SET delivery_state='BLOCKED',"
                    "lease_owner=NULL,lease_until_ms=NULL,row_version=row_version+1,"
                    "last_error_code='TASKGRAPH_FOLLOWUP_CORRUPT' WHERE message_id=?",
                    (claimed["message_id"],),
                )
                return BlockedFollowup(message_id=claimed["message_id"])
            return ClaimedFollowup(
                message_id=claimed["message_id"],
                message=message,
                owner=owner,
                row_version=claimed["row_version"],
                attempts=claimed["attempts"],
                lease_until_ms=until,
            )

    def read_consumer_receipt(
        self, claim: ClaimedFollowup, kind: ReceiptKind, identity: str
    ) -> DurableFollowupReceipt:
        """Read an actual durable object and bind it to this notification's stable key."""
        with self.store.read_view():
            return self._read_receipt(claim, kind, identity)

    def _read_receipt(
        self, claim: ClaimedFollowup, kind: ReceiptKind, identity: str
    ) -> DurableFollowupReceipt:
        return self.consumer_receipt(claim.message.mission_id, claim.command_key, kind, identity)

    def consumer_receipt(self, mission_id: str, command_key: str,
                         kind: ReceiptKind, identity: str) -> DurableFollowupReceipt:
        """Fixed handlers return a persisted object; ACK independently rechecks it."""
        kind = ReceiptKind(kind)
        db = self.store.connection
        payload: dict[str, Any]
        if kind == ReceiptKind.PLAN_COMMIT:
            receipt = HtnStore(self.store).get_commit_receipt(identity)
            if (
                receipt is None
                or receipt.mission_id != mission_id
                or receipt.command_id != command_key
            ):
                raise StoreError("TASKGRAPH_CONSUMER_RECEIPT_MISSING")
            payload = receipt.to_json()
        elif kind == ReceiptKind.DISPATCH_INTENT:
            intent = self.store.get_intent(identity)
            if (
                intent is None
                or intent.mission_id != mission_id
                or intent.creation_key != command_key
            ):
                raise StoreError("TASKGRAPH_CONSUMER_RECEIPT_MISSING")
            # Mutable worker state and lease are deliberately outside the durable identity.
            payload = {
                "intent_id": intent.intent_id,
                "kind": intent.kind,
                "subject_id": intent.subject_id,
                "mission_id": intent.mission_id,
                "creation_key": intent.creation_key,
                "input_id": intent.input_id,
                "input_hash": intent.input_hash,
            }
        else:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (identity,)).fetchone()
            if (
                row is None
                or row["mission_id"] != mission_id
                or row["idempotency_key"] != command_key
            ):
                raise StoreError("TASKGRAPH_CONSUMER_RECEIPT_MISSING")
            payload = {
                key: row[key]
                for key in ("event_id", "idempotency_key", "type", "mission_id", "payload_json")
            }
        return DurableFollowupReceipt(kind=kind, identity=identity, content_hash=_digest(payload))

    def ack_followup(self, claim: ClaimedFollowup, receipt: DurableFollowupReceipt) -> None:
        with self.store.transaction() as db:
            self._verify_claim(claim)
            if self._read_receipt(claim, receipt.kind, receipt.identity) != receipt:
                raise StoreConflict("TASKGRAPH_CONSUMER_RECEIPT_CHANGED")
            changed = db.execute(
                "UPDATE taskgraph_followups SET delivery_state='ACKED',consumer_receipt_json=?,lease_owner=NULL,"
                "lease_until_ms=NULL,row_version=row_version+1 WHERE message_id=? AND delivery_state='LEASED' "
                "AND lease_owner=? AND row_version=?",
                (
                    canonical_json(receipt.to_json()),
                    claim.message_id,
                    claim.owner,
                    claim.row_version,
                ),
            )
            if changed.rowcount != 1:
                raise StoreConflict("TASKGRAPH_FOLLOWUP_ACK_CONFLICT")

    def retry_followup(self, claim: ClaimedFollowup, *, error_code: str, now_ms: int) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", error_code):
            raise StoreError("TASKGRAPH_FOLLOWUP_ERROR_CODE_INVALID")
        _integer(now_ms, "now_ms")
        with self.store.transaction() as db:
            self._verify_claim(claim)
            next_ms = _integer(
                now_ms + (1000 * 2 ** (min(claim.attempts, 4) - 1)), "next_attempt_ms"
            )
            changed = db.execute(
                "UPDATE taskgraph_followups SET delivery_state=CASE WHEN attempts>=5 THEN 'BLOCKED' ELSE 'PENDING' END,"
                "lease_owner=NULL,lease_until_ms=NULL,row_version=row_version+1,next_attempt_ms=?,last_error_code=? "
                "WHERE message_id=? AND delivery_state='LEASED' AND lease_owner=? AND row_version=?",
                (next_ms, error_code, claim.message_id, claim.owner, claim.row_version),
            )
            if changed.rowcount != 1:
                raise StoreConflict("TASKGRAPH_FOLLOWUP_RETRY_CONFLICT")
            return "BLOCKED" if claim.attempts >= 5 else "PENDING"

    def _verify_claim(self, claim: ClaimedFollowup) -> None:
        row = self.store.connection.execute(
            "SELECT * FROM taskgraph_followups WHERE message_id=?", (claim.message_id,)
        ).fetchone()
        if (
            row is None
            or row["delivery_state"] != "LEASED"
            or row["lease_owner"] != claim.owner
            or row["row_version"] != claim.row_version
            or row["attempts"] != claim.attempts
            or row["payload_json"] != canonical_json(claim.message.to_json())
        ):
            raise StoreConflict("TASKGRAPH_FOLLOWUP_LEASE_LOST")

    def reset_blocked(
        self,
        *,
        mission_id: str,
        message_id: str,
        expected_version: int,
        now_ms: int,
        repair_command_id: str,
        verify_repair: Callable[[Store, str, str, str], None],
    ) -> None:
        """The required reader must verify real repair plus operator authorization, or raise.

        It runs synchronously inside the CAS transaction; no model or external IO is permitted.
        There is no implicit authorization or automatically generated repair command.
        """
        _text(repair_command_id, "repair_command_id")
        _integer(expected_version, "expected_version", minimum=1)
        _integer(now_ms, "now_ms")
        with self.store.transaction() as db:
            verify_repair(self.store, mission_id, message_id, repair_command_id)
            changed = db.execute(
                "UPDATE taskgraph_followups SET delivery_state='PENDING',row_version=row_version+1,next_attempt_ms=?,"
                "last_error_code=NULL,attempts=0 WHERE message_id=? AND mission_id=? "
                "AND delivery_state='BLOCKED' AND row_version=?",
                (now_ms, message_id, mission_id, expected_version),
            )
            if changed.rowcount != 1:
                raise StoreConflict("TASKGRAPH_FOLLOWUP_REPAIR_CONFLICT")
