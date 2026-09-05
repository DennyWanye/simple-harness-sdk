"""Actual no-Run Context/release calls and immutable stage provenance."""

import json
import uuid
from dataclasses import dataclass

from simple_harness.contracts import canonical_json
from simple_harness.execution.audit import (
    RunAuditUnavailable,
    RunOperationAuditV1,
    audit_hash,
    audit_reference,
)
from simple_harness.execution.uow import UnitOfWorkConflict


@dataclass(frozen=True)
class StageCall:
    stage_id: str
    incarnation: str
    call_id: str
    event_sequence: int
    source_hash: str


def _append(connection, stage_id, incarnation, operation, payload, now):
    cursor = connection.execute(
        "INSERT INTO sdk_stage_audit_events("
        "stage_id,incarnation,operation,payload_json,created_at) "
        "VALUES(?,?,?,?,?)",
        (stage_id, incarnation, operation, canonical_json(payload), now),
    )
    return dict(
        connection.execute(
            "SELECT * FROM sdk_stage_audit_events WHERE event_seq=?", (cursor.lastrowid,)
        ).fetchone()
    )


def _last(connection, stage_id):
    row = connection.execute(
        "SELECT * FROM sdk_stage_audit_events WHERE stage_id=? ORDER BY event_seq DESC LIMIT 1",
        (stage_id,),
    ).fetchone()
    if row is None:
        raise RunAuditUnavailable("stage_observation_unavailable")
    return dict(row)


def _origin(connection, stage_id):
    row = connection.execute(
        "SELECT * FROM sdk_stage_audit_events WHERE stage_id=? "
        "AND operation IN ('stage.created','stage.legacy_baseline') "
        "ORDER BY event_seq DESC LIMIT 1",
        (stage_id,),
    ).fetchone()
    if row is None:
        raise RunAuditUnavailable("stage_incarnation_unavailable")
    return dict(row)


def begin_prepare(connection, claim, *, request_hash, now):
    row = connection.execute(
        "SELECT * FROM context_preparation_staging WHERE stage_id=?", (claim.stage_id,)
    ).fetchone()
    if (
        row is None
        or any(
            row[key] != getattr(claim, key)
            for key in (
                "kind",
                "identity_key",
                "user_id",
                "session_id",
                "input_hash",
                "mode",
                "source_snapshot_ref",
                "lease_owner",
                "lease_token",
            )
        )
        or row["state"] != "preparing"
        or row["lease_expires_at"] <= now
    ):
        raise UnitOfWorkConflict("stage prepare claim differs from actual authority")
    origin = _origin(connection, claim.stage_id)
    observed = connection.execute(
        "SELECT * FROM sdk_stage_audit_events WHERE stage_id=? AND incarnation=? "
        "AND operation IN ('stage.created','stage.claimed','stage.legacy_baseline') "
        "ORDER BY event_seq DESC LIMIT 1",
        (claim.stage_id, origin["incarnation"]),
    ).fetchone()
    if observed is None:
        raise RunAuditUnavailable("stage_claim_observation_unavailable")
    call_id = uuid.uuid4().hex
    payload = dict(
        call_id=call_id,
        request_hash=request_hash,
        claim_sequence=observed["event_seq"],
        claim_hash=audit_hash(dict(observed)),
        owner_hash=audit_hash(claim.lease_owner),
    )
    record = _append(
        connection, claim.stage_id, origin["incarnation"], "context.prepare.started", payload, now
    )
    return StageCall(
        claim.stage_id, origin["incarnation"], call_id, record["event_seq"], audit_hash(record)
    )


def finish(
    connection, call, *, state, now, result_hash=None, error_code=None, transition_sequence=None
):
    row = connection.execute(
        "SELECT * FROM sdk_stage_audit_events WHERE event_seq=?", (call.event_sequence,)
    ).fetchone()
    if (
        row is None
        or audit_hash(dict(row)) != call.source_hash
        or row["incarnation"] != call.incarnation
    ):
        raise UnitOfWorkConflict("stage call origin differs")
    payload = json.loads(row["payload_json"])
    if payload["call_id"] != call.call_id or row["stage_id"] != call.stage_id:
        raise UnitOfWorkConflict("stage call identity differs")
    operation = row["operation"].removesuffix(".started") + ".settled"
    if (
        connection.execute(
            "SELECT 1 FROM sdk_stage_audit_events WHERE stage_id=? AND operation=? "
            "AND json_extract(payload_json,'$.call_id')=?",
            (call.stage_id, operation, call.call_id),
        ).fetchone()
        is not None
    ):
        raise UnitOfWorkConflict("stage call already settled")
    _append(
        connection,
        call.stage_id,
        call.incarnation,
        operation,
        dict(
            payload,
            state=state,
            start_sequence=call.event_sequence,
            start_hash=call.source_hash,
            result_hash=result_hash,
            error_code=error_code,
            transition_sequence=transition_sequence,
        ),
        now,
    )


def _rows(connection, stage_id=None, incarnation=None):
    clause, parameters = [], []
    if stage_id is not None:
        clause.append("stage_id=?")
        parameters.append(stage_id)
    if incarnation is not None:
        clause.append("incarnation=?")
        parameters.append(incarnation)
    where = " WHERE " + " AND ".join(clause) if clause else ""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM sdk_stage_audit_events" + where + " ORDER BY event_seq", parameters
        )
    ]


def stage_operations(rows):
    ends = {}
    starts = {}
    for row in rows:
        value = json.loads(row["payload_json"])
        if row["operation"].endswith(".started"):
            starts[row["event_seq"]] = row
        elif row["operation"].endswith(".settled"):
            start = starts.get(value["start_sequence"])
            if start is None or audit_hash(start) != value["start_hash"]:
                raise RunAuditUnavailable("stage_call_pair_invalid")
            ends[value["start_sequence"]] = row
    for row in rows:
        value = json.loads(row["payload_json"])
        operation = row["operation"]
        if operation.endswith(".settled"):
            continue
        attempt = operation.endswith(".started")
        end = ends.get(row["event_seq"])
        outcome = json.loads(end["payload_json"]) if end else {}
        yield RunOperationAuditV1(
            "stage:" + str(row["event_seq"]) + ":" + row["incarnation"],
            "context",
            outcome.get("state", "unknown") if attempt else value.get("state", "recorded"),
            row["event_seq"],
            audit_hash(row),
            "stage:" + str(row["event_seq"]),
            record_type="boundary" if attempt else "receipt",
            operation_name=operation.removesuffix(".started"),
            created_at=row["created_at"],
            handed_off_at=row["created_at"] if attempt else None,
            settled_at=end["created_at"] if end else None,
            request_hash=value.get("request_hash"),
            result_hash=outcome.get("result_hash"),
            owner_ref_hash=value.get("owner_hash"),
            error_code=outcome.get("error_code"),
        )


def stage_coverage(rows):
    gaps = set()
    claimed = {}
    calls = set()
    for row in rows:
        value = json.loads(row["payload_json"])
        if row["operation"] == "stage.legacy_baseline":
            gaps.add("stage_introduction_unverified")
        if row["operation"] in {"stage.created", "stage.claimed", "stage.legacy_baseline"}:
            if value.get("mode") == "sdk_prepared" and value.get("state") == "preparing":
                claimed[row["event_seq"]] = row
        if row["operation"] == "context.prepare.started":
            claim = claimed.get(value["claim_sequence"])
            if claim is None or audit_hash(claim) != value["claim_hash"]:
                gaps.add("stage_prepare_origin_unverified")
            calls.add(value["claim_sequence"])
    if set(claimed) - calls:
        gaps.add("stage_prepare_call_unverified")
    by_sequence = {row["event_seq"]: row for row in rows}
    for row in rows:
        value = json.loads(row["payload_json"])
        if (
            row["operation"] == "stage.updated"
            and value.get("state") == "staged"
            and value.get("mode") == "sdk_prepared"
        ):
            matching = []
            actual_claims = [
                source
                for source in rows
                if source["incarnation"] == row["incarnation"]
                and source["event_seq"] < row["event_seq"]
                and source["operation"]
                in {"stage.created", "stage.claimed", "stage.legacy_baseline"}
            ]
            claim_sequence = actual_claims[-1]["event_seq"] if actual_claims else None
            for end in rows:
                if (
                    end["operation"] != "context.prepare.settled"
                    or end["event_seq"] >= row["event_seq"]
                ):
                    continue
                outcome = json.loads(end["payload_json"])
                start = by_sequence.get(outcome["start_sequence"])
                if (
                    start is not None
                    and audit_hash(start) == outcome["start_hash"]
                    and end["incarnation"] == row["incarnation"]
                    and outcome["claim_sequence"] == claim_sequence
                    and outcome["state"] == "returned"
                    and outcome.get("result_hash") is not None
                    and outcome["result_hash"] == value.get("product_result_hash")
                ):
                    matching.append(start)
            if not matching:
                gaps.add("stage_prepared_result_unverified")
    witnessed = set()
    for row in rows:
        value = json.loads(row["payload_json"])
        if row["operation"] == "release.legacy_baseline":
            gaps.add("release_introduction_unverified")
        if row["operation"] == "memory.release_recall.started":
            source = by_sequence.get(value["source_sequence"])
            if source is None or audit_hash(source) != value["source_hash"]:
                gaps.add("release_call_origin_unverified")
        if row["operation"] == "memory.release_recall.settled":
            start = by_sequence.get(value["start_sequence"])
            transition = by_sequence.get(value.get("transition_sequence"))
            if start is None or audit_hash(start) != value["start_hash"]:
                gaps.add("release_call_origin_unverified")
            elif transition is not None:
                changed = json.loads(transition["payload_json"])
                if (
                    transition["operation"] != "release.updated"
                    or audit_reference("release", changed["release_id"]) != value["release_ref"]
                ):
                    gaps.add("release_settlement_authority_unverified")
                else:
                    witnessed.add(transition["event_seq"])
    previous = {}
    for row in rows:
        if not row["operation"].startswith("release."):
            continue
        value = json.loads(row["payload_json"])
        prior = previous.get(value["release_id"])
        if row["operation"] == "release.updated" and (
            prior is None
            or value["attempt_count"] != prior["attempt_count"]
            or value["state"] != prior["state"]
        ):
            if row["event_seq"] not in witnessed:
                gaps.add("release_call_unverified")
        previous[value["release_id"]] = value
    return gaps


def run_stages(connection, run_id):
    rows = connection.execute(
        "SELECT a.stage_id,a.incarnation FROM sdk_stage_audit_events a "
        "LEFT JOIN continuations c ON c.continuation_id="
        "json_extract(a.payload_json,'$.consumed_continuation_id') "
        "WHERE a.operation='stage.consumed' AND "
        "(json_extract(a.payload_json,'$.consumed_run_id')=? OR c.run_id=?) "
        "ORDER BY a.event_seq",
        (run_id, run_id),
    )
    return tuple(dict.fromkeys(tuple(row) for row in rows))


def run_operations(connection, run_id):
    for stage_id, incarnation in run_stages(connection, run_id):
        yield from stage_operations(_rows(connection, stage_id, incarnation))


def run_coverage(connection, run_id):
    gaps = set()
    expected = {
        row[0]
        for row in connection.execute(
            "SELECT context_stage_id FROM continuations WHERE run_id=? "
            "AND context_stage_id IS NOT NULL",
            (run_id,),
        )
    }
    start = connection.execute(
        "SELECT snapshot_json FROM run_start_snapshots WHERE run_id=?", (run_id,)
    ).fetchone()
    if start is not None:
        stage_id = json.loads(start[0]).get("context_stage_id")
        if stage_id is not None:
            expected.add(stage_id)
    expected.update(
        row[0]
        for row in connection.execute(
            "SELECT stage_id FROM context_preparation_staging WHERE consumed_run_id=?", (run_id,)
        )
    )
    bound = {stage_id for stage_id, _ in run_stages(connection, run_id)}
    if expected - bound:
        gaps.add("stage_consumption_authority_unverified")
    for stage_id, incarnation in run_stages(connection, run_id):
        gaps.update(stage_coverage(_rows(connection, stage_id, incarnation)))
    return gaps


class _Header(dict):
    def to_json(self):
        return dict(self)


def read_stage_snapshot(connection, stage_id, operation_sink):
    rows = _rows(connection, stage_id)
    if not rows:
        raise RunAuditUnavailable("stage_audit_unavailable")
    for operation in stage_operations(rows):
        operation_sink.append(operation)
    gaps = stage_coverage(rows)
    return _Header(
        schema_version=1,
        stage_ref=audit_reference("stage", stage_id),
        coverage_gaps=sorted(gaps),
        history_coverage="partial" if gaps else "recorded",
        recording_contract_version=2,
        recording_coverage="unverified" if gaps else "verified_current_intervals",
        source_set=["sdk_stage_audit_events"],
        recording_boundaries=["context.prepare", "memory.release_recall"],
        history_limitations=["legacy_unleased_release_without_transition_not_reconstructable"],
        operations=[],
        truncated=False,
        current_source_complete=True,
        snapshot_hash=None,
    )


def stage_incarnation(connection, stage_id):
    row = connection.execute(
        "SELECT * FROM sdk_stage_audit_events WHERE stage_id=? ORDER BY event_seq LIMIT 1",
        (stage_id,),
    ).fetchone()
    if row is None:
        raise RunAuditUnavailable("stage_audit_unavailable")
    return audit_hash(dict(row))


def stage_cut(connection, stage_id, *, sequence=None):
    row = connection.execute(
        "SELECT * FROM sdk_stage_audit_events WHERE stage_id=? "
        + ("ORDER BY event_seq DESC LIMIT 1" if sequence is None else "AND event_seq=?"),
        (stage_id,) if sequence is None else (stage_id, sequence),
    ).fetchone()
    if row is None:
        raise RunAuditUnavailable("stage_cut_unavailable")
    return dict(sequence=row["event_seq"], source_hash=audit_hash(dict(row)))


def run_stage_cut(connection, run_id, saved=None):
    if saved is not None:
        for item in saved:
            if stage_cut(connection, item["stage_id"], sequence=item["sequence"]) != {
                "sequence": item["sequence"],
                "source_hash": item["source_hash"],
            }:
                raise RunAuditUnavailable("stage_run_cut_mismatch")
            row = connection.execute(
                "SELECT * FROM sdk_stage_audit_events WHERE event_seq=?",
                (item["binding_sequence"],),
            ).fetchone()
            if (
                row is None
                or audit_hash(dict(row)) != item["binding_hash"]
                or (item["stage_id"], item["incarnation"]) not in run_stages(connection, run_id)
            ):
                raise RunAuditUnavailable("stage_run_binding_unavailable")
        return saved
    values = []
    for stage_id, incarnation in run_stages(connection, run_id):
        binding = connection.execute(
            "SELECT * FROM sdk_stage_audit_events WHERE stage_id=? AND incarnation=? "
            "AND operation='stage.consumed' ORDER BY event_seq LIMIT 1",
            (stage_id, incarnation),
        ).fetchone()
        cut = stage_cut(connection, stage_id)
        values.append(
            dict(
                stage_id=stage_id,
                incarnation=incarnation,
                binding_sequence=binding["event_seq"],
                binding_hash=audit_hash(dict(binding)),
                **cut,
            )
        )
    return values


def begin_release(connection, release_id, *, now):
    from dataclasses import asdict

    from simple_harness.runtime.agent_memory import MemoryReleaseRequest

    row = connection.execute(
        "SELECT * FROM memory_recall_releases WHERE release_id=?", (release_id,)
    ).fetchone()
    if row is None or row["state"] != "pending" or row["retry_at"] > now:
        return None
    request = MemoryReleaseRequest(
        row["query_id"], row["query_hash"], row["result_id"], row["result_hash"], row["write_fence"]
    )
    origin = _origin(connection, row["stage_id"])
    source = connection.execute(
        "SELECT * FROM sdk_stage_audit_events WHERE stage_id=? AND operation LIKE 'release.%' "
        "AND incarnation=? AND json_extract(payload_json,'$.release_id')=? "
        "ORDER BY event_seq DESC LIMIT 1",
        (row["stage_id"], origin["incarnation"], release_id),
    ).fetchone()
    if source is None:
        raise RunAuditUnavailable("release_observation_unavailable")
    payload = dict(
        call_id=uuid.uuid4().hex,
        request_hash=audit_hash(asdict(request)),
        release_ref=audit_reference("release", release_id),
        source_sequence=source["event_seq"],
        source_hash=audit_hash(dict(source)),
    )
    record = _append(
        connection,
        row["stage_id"],
        origin["incarnation"],
        "memory.release_recall.started",
        payload,
        now,
    )
    return (
        StageCall(
            row["stage_id"],
            origin["incarnation"],
            payload["call_id"],
            record["event_seq"],
            audit_hash(record),
        ),
        request,
    )


def settle_release(connection, release_id, call, *, now, returned):
    from dataclasses import asdict

    from simple_harness.runtime.agent_memory import MemoryReleaseRequest

    origin = connection.execute(
        "SELECT * FROM sdk_stage_audit_events WHERE event_seq=?", (call.event_sequence,)
    ).fetchone()
    if (
        origin is None
        or audit_hash(dict(origin)) != call.source_hash
        or origin["stage_id"] != call.stage_id
        or origin["incarnation"] != call.incarnation
        or origin["operation"] != "memory.release_recall.started"
    ):
        raise UnitOfWorkConflict("release call origin differs")
    proof = json.loads(origin["payload_json"])
    if proof["call_id"] != call.call_id or proof["release_ref"] != audit_reference(
        "release", release_id
    ):
        raise UnitOfWorkConflict("release call identity differs")
    row = connection.execute(
        "SELECT * FROM memory_recall_releases WHERE release_id=?", (release_id,)
    ).fetchone()
    if row is not None:
        request = MemoryReleaseRequest(
            row["query_id"],
            row["query_hash"],
            row["result_id"],
            row["result_hash"],
            row["write_fence"],
        )
        if row["stage_id"] != call.stage_id or audit_hash(asdict(request)) != proof["request_hash"]:
            raise UnitOfWorkConflict("release request differs from actual call")
    # The old invocation may really return after its queue was cleaned/recreated.
    # Preserve that outcome under its original call, without settling the new row.
    if row is not None and _origin(connection, call.stage_id)["incarnation"] != call.incarnation:
        row = None
    transition = None
    if row is not None and row["state"] == "pending":
        attempts = row["attempt_count"] + 1
        connection.execute(
            "UPDATE memory_recall_releases SET attempt_count=?,state=?,retry_at=?,released_at=? "
            "WHERE release_id=? AND state='pending'",
            (
                attempts,
                "released" if returned else "pending",
                now if returned else now + min(60, 2 ** min(attempts, 6)),
                now if returned else None,
                release_id,
            ),
        )
        transition = _last(connection, call.stage_id)["event_seq"]
    finish(
        connection,
        call,
        state="returned" if returned else "unknown",
        now=now,
        result_hash=audit_hash(None) if returned else None,
        transition_sequence=transition,
        error_code=None if returned else "memory_release_interrupted",
    )
