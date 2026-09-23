# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Expiry events for the original tick: durable wakeups, no parallel scheduler."""

from __future__ import annotations

from ..contracts import Event
from ..storage.assurance_work import WorkTarget, atomic
from ..storage.store import Store
from .codec import AssuranceError, decode, fields, fingerprint, integer, text

EXPIRY_EVENT = "AssuranceUseExpiryDue"


def validity_work_key(body: dict) -> str:
    return "validity:" + fingerprint(
        {
            key: body[key]
            for key in (
                "consumer_kind",
                "consumer_id",
                "purpose",
                "scope_id",
                "root_incarnation_id",
            )
        }
    )


def classify_expiry(event: Event, consumer: str) -> tuple[WorkTarget, ...]:
    """Used by the registered event adapter; never infer class from a prefix."""
    if event.type != EXPIRY_EVENT or consumer not in {"VALIDITY", "CLOSEOUT"}:
        return ()
    body = fields(
        dict(event.payload),
        {
            "certificate_id",
            "certificate_hash",
            "consumer_kind",
            "consumer_id",
            "purpose",
            "scope_id",
            "root_incarnation_id",
            "not_after_ms",
        },
    )
    key = validity_work_key(body) if consumer == "VALIDITY" else "closeout:" + event.mission_id
    return (
        WorkTarget(
            key, fingerprint({"type": event.type, "mission_id": event.mission_id, "boundary": body})
        ),
    )


class AssuranceExpiry:
    def __init__(self, store: Store) -> None:
        self.store = store

    def emit_due(
        self, mission_id: str, *, root_incarnation_id: str, now_ms: int, limit: int = 128
    ) -> tuple[Event, ...]:
        """Append one original event per usable certificate boundary.

        Only the most recently inserted certificate for an exact consumer/root
        can schedule. Comparing row insertion order also works after wall-clock
        rollback. The root id must come from the authenticated root gate.
        Cursor recovery handles a crash after event append but before ingestion.
        """
        integer(now_ms)
        integer(limit, minimum=1, maximum=128)
        text(root_incarnation_id)
        emitted = []
        with atomic(self.store) as connection:
            rows = connection.execute(
                "SELECT c.* FROM assurance_use_certificates c WHERE c.mission_id=? "
                "AND c.not_after_ms IS NOT NULL AND c.not_after_ms>c.issued_at_ms "
                "AND c.not_after_ms<=? AND json_extract(c.certificate_json,'$.decision')='USABLE' "
                "AND json_extract(c.certificate_json,'$.root_incarnation_id')=? "
                "AND NOT EXISTS(SELECT 1 FROM assurance_use_certificates newer "
                "WHERE newer.mission_id=c.mission_id AND newer.consumer_kind=c.consumer_kind "
                "AND newer.consumer_id=c.consumer_id AND newer.purpose=c.purpose "
                "AND newer.scope_id=c.scope_id AND newer.rowid>c.rowid "
                "AND json_extract(newer.certificate_json,'$.root_incarnation_id')=?) "
                "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.mission_id=c.mission_id "
                "AND e.type=? AND json_extract(e.payload_json,'$.certificate_id')=c.certificate_id "
                "AND json_extract(e.payload_json,'$.root_incarnation_id')=? "
                "AND json_extract(e.payload_json,'$.certificate_hash')=c.certificate_hash) "
                "ORDER BY c.not_after_ms,c.certificate_id LIMIT ?",
                (
                    mission_id,
                    now_ms,
                    root_incarnation_id,
                    root_incarnation_id,
                    EXPIRY_EVENT,
                    root_incarnation_id,
                    limit,
                ),
            ).fetchall()
            for row in rows:
                certificate = decode(row["certificate_json"])
                if fingerprint(certificate) != row["certificate_hash"]:
                    raise AssuranceError("CERTIFICATE_HASH_MISMATCH")
                for key in (
                    "mission_id",
                    "consumer_kind",
                    "consumer_id",
                    "purpose",
                    "scope_id",
                    "issued_at_ms",
                    "not_after_ms",
                ):
                    if certificate[key] != row[key]:
                        raise AssuranceError("CERTIFICATE_COLUMN_MISMATCH", key)
                body = {
                    key: certificate[key]
                    for key in (
                        "consumer_kind",
                        "consumer_id",
                        "purpose",
                        "scope_id",
                        "root_incarnation_id",
                        "not_after_ms",
                    )
                }
                body.update(
                    certificate_id=row["certificate_id"], certificate_hash=row["certificate_hash"]
                )
                identity = fingerprint(
                    {"mission_id": mission_id, "type": EXPIRY_EVENT, "boundary": body}
                )
                event = self.store.append_event(
                    Event(
                        id="assurance-expiry:" + identity,
                        type=EXPIRY_EVENT,
                        trace_id="assurance-expiry:" + identity,
                        mission_id=mission_id,
                        task_id=None,
                        attempt_id=None,
                        actor_type="system",
                        actor_id="assurance-tick",
                        payload=body,
                        idempotency_key="assurance-expiry:" + identity,
                        created_at=now_ms / 1000,
                    )
                )
                if dict(event.payload) != body or event.mission_id != mission_id:
                    raise AssuranceError("EXPIRY_EVENT_IDENTITY_CONFLICT")
                emitted.append(event)
        return tuple(emitted)
