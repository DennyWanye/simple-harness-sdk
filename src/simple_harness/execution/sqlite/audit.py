"""SDK-owned audit projection and append-only pre-effect facts; schema7 tables."""

from __future__ import annotations

import json
from dataclasses import replace

from simple_harness.execution.audit import (
    RunAuditUnavailable,
    RunAuditUsageV1,
    RunOperationAuditSnapshotV1,
    RunOperationAuditV1,
    audit_error_code,
    audit_hash,
    audit_label_syntax,
    audit_reference,
)


def read_snapshot(connection, run_id, limit, *, operation_sink=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4096:
        raise ValueError("audit limit must be 1..4096")
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise RunAuditUnavailable("run_not_found")
    operations = [] if operation_sink is None else operation_sink
    truncated = False
    query_limit = limit + 1 if operation_sink is None else -1

    def bounded_rows(cursor):
        nonlocal truncated
        for index, row in enumerate(cursor):
            if operation_sink is None and index == limit:
                truncated = True
                break
            yield row

    for table, identity, kind in (
        ("provider_invocations", "invocation_id", "provider"),
        ("execution_effects", "effect_id", "effect"),
        ("decisions", "decision_id", "decision"),
        ("run_admissions", "admission_id", "admission"),
        ("continuations", "continuation_id", "continuation"),
        ("conversation_commands", "command_id", "control"),
    ):
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE run_id=? ORDER BY {identity} LIMIT ?",
            (run_id, query_limit),
        )
        for row in bounded_rows(rows):
            body = dict(row)
            operations.append(
                RunOperationAuditV1(
                    f"{kind}:{row[identity]}",
                    kind,
                    row["state"],
                    row["version"],
                    audit_hash(body),
                    row[identity],
                    body.get("handoff_attempt"),
                    body.get("rehandoff_count"),
                    usage=_usage(body) if kind == "provider" else None,
                    **_details(body, kind, connection),
                )
            )
            if kind == "provider" and body.get("response_json"):
                for proposal in bounded_rows(_proposals(body)):
                    operations.append(proposal)
    rows = connection.execute(
        "SELECT * FROM run_events WHERE run_id=? "
        "AND kind IN ('audit.tool.v1','audit.transition.v1') ORDER BY durable_seq LIMIT ?",
        (run_id, query_limit),
    )
    for row in bounded_rows(rows):
        value = json.loads(row["payload_json"])
        if row["event_id"].rsplit(":", 1)[-1] != audit_hash(value):
            raise RunAuditUnavailable("audit_fact_hash_mismatch")
        if row["kind"] == "audit.tool.v1":
            operations.append(
                RunOperationAuditV1(
                    value["operation_id"],
                    "tool",
                    value["state"],
                    row["durable_seq"],
                    audit_hash(value),
                    row["event_id"],
                    record_type="boundary",
                    created_at=row["created_at"],
                    operation_name=value.get("registered_tool_name"),
                    operation_name_hash=value.get("operation_name_hash")
                    or (
                        audit_hash(value["operation_name"]) if value.get("operation_name") else None
                    ),
                    call_id=value.get("call_id"),
                    error_code=audit_error_code(value.get("error_code")),
                    error_code_hash=value.get("error_code_hash"),
                    raw_call_id_hash=value.get("raw_call_id_hash")
                    or (audit_hash(value["raw_call_id"]) if value.get("raw_call_id") else None),
                    effect_id=value.get("effect_id"),
                    turn_ordinal=value.get("turn_ordinal"),
                    call_ordinal=value.get("call_ordinal"),
                )
            )
        else:
            details = dict(value.get("details", {}))
            name = details.get("operation_name")
            details["operation_name_hash"] = details.get("operation_name_hash") or (
                audit_hash(name) if name else None
            )
            details["operation_name"] = (
                _registered_name(connection, run_id, details.get("effect_id"))
                if value["kind"] == "effect"
                else "provider.invoke"
            )
            details["error_code"] = audit_error_code(details.get("error_code"))
            raw = details.pop("raw_call_id", None)
            if raw is not None:
                details["raw_call_id_hash"] = audit_hash(raw)
            operations.append(
                RunOperationAuditV1(
                    value["operation_id"],
                    value["kind"],
                    value["state"],
                    value["source_version"],
                    value["source_hash"],
                    row["event_id"],
                    value["handoff_attempt"],
                    value["rehandoff_count"],
                    record_type="transition",
                    **details,
                )
            )
    from .delivery_audit import operations as delivery_operations

    for operation in bounded_rows(delivery_operations(connection, run_id)):
        operations.append(operation)
    from simple_harness.execution.runtime_audit import RUNTIME_BOUNDARIES

    runtime_rows = connection.execute(
        "SELECT * FROM run_events WHERE run_id=? AND kind='audit.runtime.v2' "
        "ORDER BY durable_seq LIMIT ?",
        (run_id, query_limit),
    )
    for row in bounded_rows(runtime_rows):
        value = json.loads(row["payload_json"])
        if (
            row["event_id"].rsplit(":", 1)[-1] != audit_hash(value)
            or value["name"] not in RUNTIME_BOUNDARIES
        ):
            raise RunAuditUnavailable("audit_runtime_fact_invalid")
        operations.append(
            RunOperationAuditV1(
                "runtime:" + value["operation_id"],
                "runtime",
                value["state"],
                row["durable_seq"],
                audit_hash(value),
                row["event_id"],
                record_type="boundary",
                operation_name=value["name"],
                error_code=audit_error_code(value.get("error_code")),
                runtime_epoch=value["runtime_epoch"],
                parent_operation_id=value.get("parent_operation_id"),
                created_at=value["started_at"],
                settled_at=None if value["state"] == "started" else row["created_at"],
                request_hash=value["identity_hash"],
                result_hash=value["receipt_hash"],
            )
        )
    operations.append(
        RunOperationAuditV1(
            "run:" + run_id, "run", run["state"], run["version"], audit_hash(dict(run)), run_id
        )
    )
    events = connection.execute(
        "SELECT * FROM run_events WHERE run_id=? "
        "AND kind NOT IN ('audit.tool.v1','audit.transition.v1','audit.runtime.v2') "
        "ORDER BY durable_seq LIMIT ?",
        (run_id, query_limit),
    )
    for row in bounded_rows(events):
        if row["kind"] == "audit.claim.v2":
            from .audit_witness import CLAIM_SOURCES

            value = json.loads(row["payload_json"])
            if (
                row["event_id"] != "audit-claim:" + audit_hash(value)
                or value["table"] not in CLAIM_SOURCES
            ):
                raise RunAuditUnavailable("audit_claim_fact_invalid")
            operations.append(
                RunOperationAuditV1(
                    "claim:" + value["identity_hash"],
                    CLAIM_SOURCES[value["table"]][2],
                    "claimed",
                    value["source_version"],
                    value["source_hash"],
                    row["event_id"],
                    record_type="transition",
                    operation_name=value["table"] + ".claim",
                    created_at=row["created_at"],
                    runtime_epoch=value["runtime_epoch"],
                    claim_epoch=value["claim_epoch"],
                )
            )
            continue
        # Kind and payload are source data, not arbitrary exported error strings.
        operations.append(
            RunOperationAuditV1(
                "run:" + run_id,
                "run",
                "recorded_event",
                row["durable_seq"],
                audit_hash(dict(row)),
                row["event_id"],
                record_type="receipt",
                operation_name="run.event",
                operation_name_hash=audit_hash(row["kind"]),
                created_at=row["created_at"],
            )
        )
    checkpoints = connection.execute(
        "SELECT * FROM workflow_checkpoints WHERE run_id=? ORDER BY namespace,version LIMIT ?",
        (run_id, query_limit),
    )
    for row in bounded_rows(checkpoints):
        operations.append(
            RunOperationAuditV1(
                "context:" + row["checkpoint_id"],
                "context",
                "recorded_snapshot",
                row["version"],
                row["checkpoint_hash"],
                row["checkpoint_id"],
                record_type="receipt",
            )
        )
    for source_kind, table, key in (
        ("provider", "provider_invocations", "invocation_id"),
        ("tool", "execution_effects", "effect_id"),
    ):
        receipts = connection.execute(
            f"SELECT r.* FROM reconciliation_resolutions r JOIN {table} h "
            f"ON h.{key}=r.ledger_identity WHERE h.run_id=? AND r.kind=? "
            "ORDER BY r.resolution_id LIMIT ?",
            (run_id, source_kind, query_limit),
        )
        for row in bounded_rows(receipts):
            operations.append(
                RunOperationAuditV1(
                    "reconciliation:" + row["resolution_id"],
                    "reconciliation",
                    row["outcome"],
                    row["handoff_attempt"],
                    audit_hash(dict(row)),
                    row["resolution_id"],
                    handoff_attempt=row["handoff_attempt"],
                    record_type="receipt",
                    operation_name=source_kind + ".reconcile",
                    created_at=row["created_at"],
                    evidence_ref_hash=audit_hash(row["evidence_ref"]),
                    effect_id=row["ledger_identity"] if source_kind == "tool" else None,
                    provider_invocation_id=row["ledger_identity"]
                    if source_kind == "provider"
                    else None,
                )
            )
    from .audit_core import core_operation, core_rows
    from .command_audit import command_event_operation

    for row in bounded_rows(
        connection.execute(
            "SELECT * FROM sdk_command_audit_events WHERE run_id=? ORDER BY event_seq LIMIT ?",
            (run_id, query_limit),
        )
    ):
        operations.append(command_event_operation(row))

    for table, keys, kind, body in bounded_rows(core_rows(connection, run_id, limit=query_limit)):
        operations.append(core_operation(table, keys, kind, body))
    from .audit_coverage import recording_coverage

    gaps, recording_version = recording_coverage(connection, run_id)
    if operation_sink is not None:
        return RunOperationAuditSnapshotV1(
            run_id,
            run["state"],
            run["version"],
            (),
            False,
            gaps,
            root_run_id=audit_reference("run", run["root_run_id"]),
            parent_run_id=audit_reference("run", run["parent_run_id"]),
            recording_contract_version=recording_version,
        )
    operations = [_opaque_operation(o, run_id) for o in operations]
    operations.sort(
        key=lambda o: (o.kind, o.operation_id, o.source_version, o.record_type, o.source_id)
    )
    truncated |= len(operations) > limit
    return RunOperationAuditSnapshotV1(
        run_id,
        run["state"],
        run["version"],
        tuple(operations[:limit]),
        truncated,
        gaps,
        root_run_id=audit_reference("run", run["root_run_id"]),
        parent_run_id=audit_reference("run", run["parent_run_id"]),
        recording_contract_version=recording_version,
    )


def head_fact(connection, kind, identity):
    table, key = {
        "provider": ("provider_invocations", "invocation_id"),
        "effect": ("execution_effects", "effect_id"),
    }[kind]
    row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (identity,)).fetchone()
    if row is None:
        raise RunAuditUnavailable("operation_not_found")
    value = dict(row)
    payload = dict(
        operation_id=f"{kind}:{identity}",
        kind=kind,
        source_id=identity,
        source_version=row["version"],
        source_hash=audit_hash(value),
        state=row["state"],
        handoff_attempt=row["handoff_attempt"],
        rehandoff_count=row["rehandoff_count"],
        details=_details(value, kind, connection),
    )
    return row["run_id"], payload


def _usage(row):
    raw = json.loads(row["usage_json"]) if row.get("usage_json") else {}
    usage = raw.get("usage") or {}
    budget = raw.get("budget") or {}
    return RunAuditUsageV1(
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
        budget.get("kind", "unknown"),
        budget.get("amount_micros"),
        cache_tokens=usage.get("cache_tokens"),
        reasoning_tokens=usage.get("reasoning_tokens"),
    )


def _details(row, kind, connection=None):
    result = json.loads(row["result_json"]) if row.get("result_json") else {}
    error = row.get("error_code") or row.get("last_error_code") or result.get("error_code")
    name = (
        row.get("tool_name")
        if kind == "effect"
        else ("provider.invoke" if kind == "provider" else kind)
    )
    invocation_id, request_id = row.get("invocation_id"), row.get("request_id")
    if kind == "effect" and connection is not None:
        invocation_id, request_id = _effect_provider_link(connection, row)
    return dict(
        operation_name=(
            _registered_name(connection, row["run_id"], row["effect_id"])
            if kind == "effect" and connection is not None
            else audit_label_syntax(name)
        ),
        operation_name_hash=audit_hash(name) if name else None,
        error_code=audit_error_code(error),
        error_code_hash=None if error is None else audit_hash(error),
        created_at=row.get("claimed_at", row.get("prepared_at", row.get("created_at"))),
        handed_off_at=row.get("handed_off_at"),
        settled_at=row.get("settled_at", row.get("resolved_at")),
        request_id=request_id,
        call_id=row.get("call_id"),
        raw_call_id_hash=audit_hash(row["raw_call_id"]) if row.get("raw_call_id") else None,
        turn_ordinal=row.get("turn_ordinal"),
        call_ordinal=row.get("call_ordinal"),
        effect_id=row.get("effect_id"),
        provider_invocation_id=invocation_id,
        request_hash=row.get(
            "request_fingerprint", row.get("request_hash", row.get("intent_hash"))
        ),
        result_hash=audit_hash(json.loads(row["response_json"]))
        if row.get("response_json")
        else (audit_hash(result) if row.get("result_json") else None),
        evidence_ref_hash=audit_hash(row["evidence_ref"]) if row.get("evidence_ref") else None,
        authorization_ref_hash=audit_hash(row["authorization_receipt_ref"])
        if row.get("authorization_receipt_ref")
        else None,
    )


def _proposals(row):
    """Actual public response calls, including rejected batches with no effect."""
    from simple_harness.execution.provider_invocations import provider_response_from_json

    raw = json.loads(row["response_json"])
    response = provider_response_from_json(raw)
    prefix = row["run_id"] + ":provider-turn:"
    suffix = row["request_id"][len(prefix) :] if row["request_id"].startswith(prefix) else ""
    turn = int(suffix) if suffix.isdigit() else None
    for ordinal, call in enumerate(response.tool_calls):
        identity = "proposal:" + row["invocation_id"] + ":" + str(ordinal)
        yield RunOperationAuditV1(
            identity,
            "tool",
            "proposed",
            row["version"],
            audit_hash(raw),
            identity,
            record_type="proposal",
            operation_name="tool.proposal",
            operation_name_hash=audit_hash(call.name),
            raw_call_id_hash=audit_hash(call.call_id.value),
            turn_ordinal=turn,
            call_ordinal=ordinal,
            created_at=row["settled_at"],
            request_id=row["request_id"],
            provider_invocation_id=row["invocation_id"],
        )


def _effect_provider_link(connection, effect):
    """Verify the existing ReAct v1 identity, actual response and call bytes."""
    from simple_harness.contracts import RunId, thaw_json
    from simple_harness.execution.effects import effect_request_hash
    from simple_harness.execution.provider_invocations import provider_response_from_json
    from simple_harness.runtime.drivers.react_loop import _internal_effect_identity

    if not effect.get("turn_ordinal"):
        return None, None  # standalone effect: no invented Provider parent
    if not effect.get("raw_call_id"):
        return None, None
    expected_call, expected_effect = _internal_effect_identity(
        RunId(effect["run_id"]),
        effect["turn_ordinal"],
        effect["raw_call_id"],
        effect["call_ordinal"],
    )
    if expected_call.value != effect["call_id"] or expected_effect.value != effect["effect_id"]:
        return None, None  # another driver has not supplied a ReAct parent binding
    request_id = f"{effect['run_id']}:provider-turn:{effect['turn_ordinal']}"
    provider = connection.execute(
        "SELECT invocation_id,response_json FROM provider_invocations "
        "WHERE run_id=? AND request_id=?",
        (effect["run_id"], request_id),
    ).fetchone()
    if provider is None or provider["response_json"] is None:
        return None, None
    response = provider_response_from_json(json.loads(provider["response_json"]))
    ordinal = effect["call_ordinal"]
    if ordinal >= len(response.tool_calls):
        raise RunAuditUnavailable("effect_provider_call_missing")
    call = response.tool_calls[ordinal]
    internal, identity = _internal_effect_identity(
        RunId(effect["run_id"]), effect["turn_ordinal"], call.call_id.value, ordinal
    )
    if (
        internal.value != effect["call_id"]
        or identity.value != effect["effect_id"]
        or call.call_id.value != effect["raw_call_id"]
        or call.name != effect["tool_name"]
        or effect_request_hash(tool_name=call.name, arguments=thaw_json(call.arguments))
        != effect["request_hash"]
    ):
        raise RunAuditUnavailable("effect_provider_binding_mismatch")
    return provider["invocation_id"], request_id


def _registered_name(connection, run_id, effect_id):
    if effect_id is None:
        return None
    rows = connection.execute(
        "SELECT event_id,payload_json FROM run_events WHERE run_id=? "
        "AND kind='audit.tool.v1' AND json_extract(payload_json,'$.effect_id')=? "
        "AND json_extract(payload_json,'$.state')='requested'",
        (run_id, effect_id),
    ).fetchall()
    for row in rows:
        value = json.loads(row["payload_json"])
        if row["event_id"].rsplit(":", 1)[-1] != audit_hash(value):
            raise RunAuditUnavailable("audit_fact_hash_mismatch")
        # Only the new executor's successful registry lookup supplies this marker.
        # Old persisted operation_name is not evidence of registration.
        name = value.get("registered_tool_name")
        if name is not None:
            return audit_label_syntax(name)
    return None


def _opaque_operation(operation, run_id):
    """Exact source hashes survive; external identifiers become opaque join refs."""
    kind = operation.kind
    entity_kind = "effect" if kind == "tool" else kind
    identity = operation.effect_id if kind == "tool" else operation.operation_id.split(":", 1)[-1]
    if operation.record_type == "proposal":
        entity_kind, identity = "proposal", operation.operation_id
    source_kind = kind if operation.record_type == "head" else "event"
    if kind in {"context", "reconciliation"}:
        source_kind = kind
    raw_hash = operation.raw_call_id_hash
    return replace(
        operation,
        operation_id=audit_reference(entity_kind, identity),
        source_id=audit_reference(source_kind, operation.source_id),
        request_id=audit_reference("request", operation.request_id),
        call_id=audit_reference("call", operation.call_id),
        raw_call_id=None,
        raw_call_id_hash=None
        if raw_hash is None
        else audit_hash([run_id, operation.turn_ordinal, operation.call_ordinal, raw_hash]),
        effect_id=audit_reference("effect", operation.effect_id),
        provider_invocation_id=audit_reference("provider", operation.provider_invocation_id),
        parent_operation_id=(
            audit_reference("delivery", operation.parent_operation_id.split(":", 1)[-1])
            if kind == "delivery" and operation.parent_operation_id is not None
            else audit_reference("runtime", operation.parent_operation_id)
        ),
    )
