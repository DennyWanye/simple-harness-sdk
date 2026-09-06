"""SDK-owned association with the existing Provider claim/handoff transaction."""

import hashlib
import json

from simple_harness.contracts import RequestId, RunId, canonical_json, thaw_json
from simple_harness.execution.context_use import (
    ProviderContextUseAttemptV1,
    ProviderContextUseGrantV1,
    ProviderContextUseViewV1,
)
from simple_harness.execution.provider_invocations import provider_invocation_id

DDL = """
CREATE TABLE provider_context_use_attempts (
 invocation_id TEXT NOT NULL,
 run_id TEXT NOT NULL REFERENCES runs(run_id),
 handoff_ordinal INTEGER NOT NULL CHECK(handoff_ordinal>=1),
 intent_hash TEXT NOT NULL CHECK(length(intent_hash)=64), intent_json TEXT NOT NULL,
 grant_hash TEXT CHECK(grant_hash IS NULL OR length(grant_hash)=64), grant_json TEXT,
 PRIMARY KEY(invocation_id,handoff_ordinal),
 CHECK((grant_hash IS NULL)=(grant_json IS NULL))
) STRICT;
CREATE TABLE provider_context_use_receipt_bindings (
 authority_scope_ref TEXT NOT NULL, subject TEXT NOT NULL, receipt_id TEXT NOT NULL,
 receipt_hash TEXT NOT NULL CHECK(length(receipt_hash)=64),
 invocation_id TEXT NOT NULL REFERENCES provider_invocations(invocation_id),
 handoff_ordinal INTEGER NOT NULL, receipt_ordinal INTEGER NOT NULL CHECK(receipt_ordinal>=1),
 PRIMARY KEY(authority_scope_ref,subject,receipt_id),
 UNIQUE(invocation_id,handoff_ordinal,receipt_ordinal),
 FOREIGN KEY(invocation_id,handoff_ordinal) REFERENCES provider_context_use_attempts(invocation_id,handoff_ordinal)
) STRICT;
CREATE TRIGGER provider_context_use_intent_immutable BEFORE UPDATE ON provider_context_use_attempts
 WHEN OLD.invocation_id!=NEW.invocation_id OR OLD.run_id!=NEW.run_id
 OR OLD.handoff_ordinal!=NEW.handoff_ordinal OR OLD.intent_hash!=NEW.intent_hash OR OLD.intent_json!=NEW.intent_json
 OR OLD.grant_json IS NOT NULL
 BEGIN SELECT RAISE(ABORT,'context_use_immutable'); END;
CREATE TRIGGER provider_context_use_attempt_no_delete BEFORE DELETE ON provider_context_use_attempts
 BEGIN SELECT RAISE(ABORT,'context_use_immutable'); END;
CREATE TRIGGER provider_context_use_receipt_no_update BEFORE UPDATE ON provider_context_use_receipt_bindings
 BEGIN SELECT RAISE(ABORT,'context_use_immutable'); END;
CREATE TRIGGER provider_context_use_receipt_no_delete BEFORE DELETE ON provider_context_use_receipt_bindings
 BEGIN SELECT RAISE(ABORT,'context_use_immutable'); END;
CREATE TABLE context_use_upgrade_receipt (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), receipt_json TEXT NOT NULL, receipt_hash TEXT NOT NULL
) STRICT;
CREATE TRIGGER context_use_upgrade_no_update BEFORE UPDATE ON context_use_upgrade_receipt
 BEGIN SELECT RAISE(ABORT,'context_use_upgrade_immutable'); END;
CREATE TRIGGER context_use_upgrade_no_delete BEFORE DELETE ON context_use_upgrade_receipt
 BEGIN SELECT RAISE(ABORT,'context_use_upgrade_immutable'); END;
"""


def _identity(attempt):
    return provider_invocation_id(RunId(attempt.run_id), RequestId(attempt.provider_request_id))


def read_attempt(connection, invocation_id, ordinal):
    row = connection.execute(
        "SELECT * FROM provider_context_use_attempts WHERE invocation_id=? AND handoff_ordinal=?",
        (invocation_id, ordinal),
    ).fetchone()
    if row is None:
        return None
    attempt = ProviderContextUseAttemptV1.from_json(json.loads(row["intent_json"]))
    if (
        attempt.intent_hash != row["intent_hash"]
        or _identity(attempt) != invocation_id
        or attempt.handoff_ordinal != ordinal
        or attempt.run_id != row["run_id"]
    ):
        raise ValueError("context_use_intent_corrupt")
    return attempt


def require_live(uow, connection, attempt, lease, now, *, retry=False):
    uow._require_runtime_lease(connection, lease, now=now)
    if lease.run_id != attempt.run_id:
        raise ValueError("context_use_lease_run_differs")
    stored = uow.read_react_checkpoint(attempt.run_id)
    if stored is None:
        raise ValueError("context_use_checkpoint_missing")
    payload = thaw_json(stored.checkpoint)
    if hashlib.sha256(canonical_json(payload).encode()).hexdigest() != stored.checkpoint_hash:
        raise ValueError("context_use_checkpoint_corrupt")
    original = ProviderContextUseAttemptV1.from_json(payload.get("context_use_attempt"))
    if (
        original.handoff_ordinal != 1
        or payload.get("provider_request_id") != attempt.provider_request_id
        or payload.get("provider_turns_reserved_total") != attempt.provider_turn_ordinal
        or payload.get("provider_request_fingerprint") != attempt.request_fingerprint
        or hashlib.sha256(
            canonical_json(payload.get("provider_request_snapshot")).encode()
        ).hexdigest()
        != attempt.request_fingerprint
    ):
        raise ValueError("context_use_checkpoint_request_differs")
    expected = original.to_json()
    actual = attempt.to_json()
    if retry:
        actual.update(handoff_ordinal=original.handoff_ordinal, requested_at=original.requested_at)
    if (
        actual != expected
        or payload.get("phase") != "provider_reserved"
        or payload.get("active_turn_id") != attempt.turn_id
        or payload.get("active_continuation_id") != attempt.continuation_id
        or payload.get("context_use_authority_scope") != attempt.authority_scope_ref
    ):
        raise ValueError("context_use_checkpoint_identity_differs")
    if attempt.continuation_id is not None:
        row = connection.execute(
            "SELECT run_id,state,payload_json FROM continuations WHERE continuation_id=?",
            (attempt.continuation_id,),
        ).fetchone()
        if (
            row is None
            or row["run_id"] != attempt.run_id
            or row["state"] not in ("claimed", "acked")
        ):
            raise ValueError("context_use_continuation_unverified")
        if json.loads(row["payload_json"]).get("context_use_turn_id") != attempt.turn_id:
            raise ValueError("context_use_continuation_turn_unverified")
    else:
        start = uow.read_start_snapshot(attempt.run_id)
        if start is None or start.get("turn_id") != attempt.turn_id:
            raise ValueError("context_use_root_turn_unverified")


def prepare(uow, attempt, lease, now, *, retry=False):
    invocation_id = _identity(attempt)
    with uow.database.transaction() as connection:
        require_live(uow, connection, attempt, lease, now, retry=retry)
        existing = read_attempt(connection, invocation_id, attempt.handoff_ordinal)
        if existing is not None:
            # Retain the original requested_at on crash/restart retry preparation.
            compared = attempt.to_json()
            if retry:
                compared["requested_at"] = existing.requested_at
            if compared != existing.to_json():
                raise ValueError("context_use_attempt_conflict")
            return existing
        connection.execute(
            "INSERT INTO provider_context_use_attempts VALUES (?,?,?,?,?,NULL,NULL)",
            (
                invocation_id,
                attempt.run_id,
                attempt.handoff_ordinal,
                attempt.intent_hash,
                canonical_json(attempt.to_json()),
            ),
        )
    return attempt


def read_grant(connection, invocation_id, ordinal):
    attempt = read_attempt(connection, invocation_id, ordinal)
    if attempt is None:
        return None
    row = connection.execute(
        "SELECT grant_json,grant_hash FROM provider_context_use_attempts WHERE invocation_id=? AND handoff_ordinal=?",
        (invocation_id, ordinal),
    ).fetchone()
    if row["grant_json"] is None:
        if row["grant_hash"] is not None:
            raise ValueError("context_use_grant_corrupt")
        return None
    grant = ProviderContextUseGrantV1.from_json(json.loads(row["grant_json"]))
    if grant.attempt != attempt or grant.grant_hash != row["grant_hash"]:
        raise ValueError("context_use_grant_corrupt")
    links = [
        tuple(r)
        for r in connection.execute(
            "SELECT authority_scope_ref,subject,receipt_id,receipt_hash,receipt_ordinal FROM provider_context_use_receipt_bindings WHERE invocation_id=? AND handoff_ordinal=? ORDER BY receipt_ordinal",
            (invocation_id, ordinal),
        )
    ]
    expected = [
        (attempt.authority_scope_ref, attempt.subject, r.receipt_id, r.receipt_hash, i + 1)
        for i, r in enumerate(grant.receipts)
    ]
    if links != expected:
        raise ValueError("context_use_receipt_links_corrupt")
    return grant


def bind(connection, invocation_id, grant):
    if _identity(grant.attempt) != invocation_id:
        raise ValueError("context_use_invocation_differs")
    attempt = read_attempt(connection, invocation_id, grant.attempt.handoff_ordinal)
    if attempt != grant.attempt:
        raise ValueError("context_use_unprepared_grant")
    existing = read_grant(connection, invocation_id, attempt.handoff_ordinal)
    if existing is not None:
        if existing != grant:
            raise ValueError("context_use_grant_conflict")
        return
    for i, receipt in enumerate(grant.receipts):
        connection.execute(
            "INSERT INTO provider_context_use_receipt_bindings VALUES (?,?,?,?,?,?,?)",
            (
                attempt.authority_scope_ref,
                attempt.subject,
                receipt.receipt_id,
                receipt.receipt_hash,
                invocation_id,
                attempt.handoff_ordinal,
                i + 1,
            ),
        )
    if (
        connection.execute(
            "UPDATE provider_context_use_attempts SET grant_json=?,grant_hash=? WHERE invocation_id=? AND handoff_ordinal=? AND grant_json IS NULL",
            (
                canonical_json(grant.to_json()),
                grant.grant_hash,
                invocation_id,
                attempt.handoff_ordinal,
            ),
        ).rowcount
        != 1
    ):
        raise ValueError("context_use_bind_cas_failed")


def require_handoff(uow, connection, record, lease, now):
    present = read_attempt(connection, record.invocation_id, 1)
    if present is None:
        return
    grant = read_grant(connection, record.invocation_id, record.handoff_attempt + 1)
    if grant is None:
        raise ValueError("context_use_grant_missing")
    require_live(
        uow, connection, grant.attempt, lease, now, retry=grant.attempt.handoff_ordinal > 1
    )
    grant.validate_handoff(now)


def view(uow, run_id, request_id):
    invocation_id = provider_invocation_id(run_id, request_id)
    with uow.database.transaction(read_only=True) as connection:
        record = uow.read_provider_invocation(invocation_id)
        if record is None:
            return None
        ordinal = record.handoff_attempt + (1 if record.state.value == "claimed" else 0)
        grant = read_grant(connection, invocation_id, ordinal)
        if grant is None:
            if read_attempt(connection, invocation_id, 1) is not None:
                raise ValueError("context_use_grant_missing")
            return None
        return ProviderContextUseViewV1.from_grant(record, grant)
