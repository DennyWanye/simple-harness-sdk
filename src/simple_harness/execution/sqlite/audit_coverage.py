"""Coverage from actual event/operation witnesses, never a birth-version assertion."""

import json

from simple_harness.execution.audit import RunAuditUnavailable, audit_hash


def recording_coverage(connection, run_id):
    originals, proofs, heads = set(), set(), set()
    started, settled, supported_epochs = {}, {}, set()
    gaps = set()
    births, birth_proofs = set(), set()
    inputs = set()
    activation_events = {}
    activation_owners = {}
    event_bindings = []
    claims = {}
    intervals = {}
    for row in connection.execute(
        "SELECT * FROM run_events WHERE run_id=? ORDER BY durable_seq", (run_id,)
    ):
        value = json.loads(row["payload_json"])
        if row["kind"] == "audit.event.v2":
            if row["event_id"] != "audit-event:" + audit_hash(value):
                raise RunAuditUnavailable("audit_event_witness_corrupt")
            if value.get("contract") != "sdk.core.v2":
                gaps.add("event_recording_contract_unverified")
            proofs.add((value["source_id"], value["source_hash"]))
            if value.get("runtime_operation") is not None:
                event_bindings.append((value["source_sequence"], value["runtime_operation"]))
            if value.get("birth") is True:
                birth_proofs.add((value["source_id"], value["source_hash"]))
        elif row["kind"] == "audit.runtime.v2":
            if row["event_id"].rsplit(":", 1)[-1] != audit_hash(value):
                raise RunAuditUnavailable("audit_runtime_fact_invalid")
            identity = value["operation_id"]
            interval = tuple(
                value.get(key)
                for key in (
                    "name",
                    "identity_hash",
                    "runtime_epoch",
                    "owner_hash",
                    "contract",
                    "started_at",
                    "parent_operation_id",
                )
            )
            if value["state"] == "started":
                if identity in started:
                    raise RunAuditUnavailable("audit_runtime_interval_duplicate")
                started[identity] = interval
                intervals[identity] = dict(value, start_sequence=row["durable_seq"])
            else:
                if identity in settled or started.get(identity) != interval:
                    raise RunAuditUnavailable("audit_runtime_interval_mismatch")
                if value["state"] not in {"completed", "failed", "interrupted", "rejected"}:
                    raise RunAuditUnavailable("audit_runtime_fact_invalid")
                settled[identity] = interval
                intervals[identity]["end_sequence"] = row["durable_seq"]
                intervals[identity]["child_operation_ids"] = value.get("child_operation_ids")
                inputs.add((value["name"], value["identity_hash"], value["state"]))
            if value["name"] == "runtime.driver":
                if value.get("contract") in {"sdk.react.v2", "sdk.workflow.v2"}:
                    supported_epochs.add(value["runtime_epoch"])
                else:
                    gaps.add("driver_or_uow_recording_unverified")
        elif row["kind"] == "audit.claim.v2":
            if row["event_id"] != "audit-claim:" + audit_hash(value):
                raise RunAuditUnavailable("audit_claim_fact_invalid")
            claims.setdefault((value["table"], value["identity_hash"]), set()).add(
                value["claim_epoch"]
            )
        elif row["kind"] == "audit.transition.v1":
            if row["event_id"].rsplit(":", 1)[-1] != audit_hash(value):
                raise RunAuditUnavailable("audit_fact_hash_mismatch")
            heads.add((value["kind"], value["source_hash"]))
        elif not row["kind"].startswith("audit."):
            original = (row["event_id"], audit_hash(dict(row)))
            originals.add(original)
            if row["kind"] in {"run.created", "child.created"}:
                births.add(original)
            if row["kind"] in {"run.activated", "run.recovered"}:
                epoch = value.get("lease_epoch", value.get("runtime_lease_epoch"))
                if type(epoch) is int:
                    activation_events[epoch] = original
                    activation_owners[epoch] = audit_hash(value.get("owner_id"))
    birth = bool(births) and births <= birth_proofs
    if not birth:
        gaps.add("legacy_birth_unverified")
    if originals - proofs:
        gaps.add("canonical_event_interval_unverified")
    if started != settled:
        gaps.add("runtime_operation_interval_unclosed")
    for epoch, owner in activation_owners.items():
        if not any(
            value["name"] == "runtime.preflight"
            and value["runtime_epoch"] == epoch
            and value["owner_hash"] == owner
            and "end_sequence" in value
            for value in intervals.values()
        ):
            gaps.add("activation_preflight_interval_unverified")
    for sequence, binding in event_bindings:
        value = intervals.get(binding["operation_id"])
        if (
            value is None
            or any(value[key] != binding[key] for key in ("runtime_epoch", "owner_hash"))
            or not (value["start_sequence"] < sequence < value.get("end_sequence", -1))
        ):
            gaps.add("event_runtime_interval_unverified")
    for value in intervals.values():
        children = value.get("child_operation_ids")
        if not isinstance(children, list) or len(set(children)) != len(children):
            gaps.add("runtime_child_interval_unverified")
        elif any(
            child not in intervals
            or intervals[child].get("parent_operation_id") != value["operation_id"]
            for child in children
        ):
            gaps.add("runtime_child_interval_unverified")
        if value["name"] == "runtime.preflight":
            continue
        parent = intervals.get(value.get("parent_operation_id"))
        if parent is None or any(
            value[key] != parent[key] for key in ("runtime_epoch", "owner_hash")
        ):
            gaps.add("runtime_parent_interval_unverified")
            continue
        if value["name"] == "runtime.driver":
            if (
                parent["name"] != "runtime.preflight"
                or parent.get("end_sequence", float("inf")) >= value["start_sequence"]
            ):
                gaps.add("runtime_parent_interval_unverified")
        elif (
            value["operation_id"] not in (parent.get("child_operation_ids") or [])
            or parent["name"] == "runtime.preflight"
            or not (
                parent["start_sequence"] < value["start_sequence"]
                and value.get("end_sequence", float("inf")) < parent.get("end_sequence", -1)
            )
        ):
            gaps.add("runtime_parent_interval_unverified")
    lease = connection.execute(
        "SELECT epoch FROM workflow_leases WHERE run_id=? AND namespace='runtime.kernel'",
        (run_id,),
    ).fetchone()
    if lease is not None and activation_events.get(lease["epoch"]) not in proofs:
        gaps.add("runtime_activation_interval_unverified")
    if activation_events and not supported_epochs:
        gaps.add("driver_or_uow_recording_unverified")
    from .command_audit import command_coverage

    for command in connection.execute(
        "SELECT * FROM conversation_commands WHERE run_id=?", (run_id,)
    ):
        gaps.update(command_coverage(connection, command))
    from .audit_witness import CLAIM_SOURCES

    for table, (key, owner, _) in CLAIM_SOURCES.items():
        for row in connection.execute(f"SELECT * FROM {table} WHERE {owner}=?", (run_id,)):
            recorded = claims.get((table, audit_hash(row[key])), set())
            if len(recorded) != row["claim_epoch"] or any(
                type(epoch) is not int or not 1 <= epoch <= row["claim_epoch"] for epoch in recorded
            ):
                gaps.add("claim_interval_unverified")
    run = connection.execute("SELECT driver_kind FROM runs WHERE run_id=?", (run_id,)).fetchone()
    for table, kind in (("provider_invocations", "provider"), ("execution_effects", "effect")):
        for row in connection.execute(f"SELECT * FROM {table} WHERE run_id=?", (run_id,)):
            if (kind, audit_hash(dict(row))) not in heads:
                gaps.add("canonical_operation_interval_unverified")
            if not supported_epochs:
                gaps.add("driver_or_uow_recording_unverified")
            expected = []
            if kind == "provider":
                expected.append(
                    (
                        "provider.prepare",
                        audit_hash({"run": run_id, "request": row["request_fingerprint"]}),
                        "completed",
                    )
                )
                if run["driver_kind"] == "react":
                    if row["response_json"] is not None:
                        gaps.update(_response_coverage(run_id, row, inputs))
                    expected.extend(
                        [
                            (
                                "context.prepare",
                                audit_hash({"run": run_id, "request": row["request_id"]}),
                                "completed",
                            ),
                            (
                                "context.verify",
                                audit_hash({"request": row["request_fingerprint"]}),
                                "completed",
                            ),
                        ]
                    )
            elif run["driver_kind"] == "react" and row["turn_ordinal"]:
                expected.append(
                    (
                        "tool.envelope",
                        audit_hash(
                            {
                                "run": run_id,
                                "turn": row["turn_ordinal"],
                                "ordinal": row["call_ordinal"],
                                "name": row["tool_name"],
                                "call": row["raw_call_id"],
                                "arguments": json.loads(row["arguments_json"]),
                            }
                        ),
                        "completed",
                    )
                )
            if any(item not in inputs for item in expected):
                gaps.add("operation_boundary_receipt_unverified")
    from .delivery_audit import coverage as delivery_coverage

    gaps.update(delivery_coverage(connection, run_id))
    return tuple(sorted(gaps)), (2 if birth else None)


def _response_coverage(run_id, row, inputs):
    """Every actual proposal has a disposition even when no effect was created."""
    from simple_harness.contracts import RunId, thaw_json
    from simple_harness.execution.provider_invocations import provider_response_from_json
    from simple_harness.runtime.drivers.react_loop import _internal_effect_identity

    raw = json.loads(row["response_json"])
    response = provider_response_from_json(raw)
    proposal_hash = audit_hash(
        {
            "request": row["request_id"],
            "calls": [
                {"call": c.call_id.value, "name": c.name, "arguments": thaw_json(c.arguments)}
                for c in response.tool_calls
            ],
        }
    )
    states = {
        state
        for name, identity, state in inputs
        if name == "tool.proposal" and identity == proposal_hash
    }
    if not states:
        return {"proposal_disposition_unverified"}
    if "completed" not in states:
        return set()  # Recorded validation failure: no accepted batch to execute.
    preflight_hash = audit_hash({"request": row["request_id"], "response": audit_hash(raw)})
    preflight = {
        state
        for name, identity, state in inputs
        if name == "tool.preflight" and identity == preflight_hash
    }
    if not preflight:
        return {"tool_preflight_interval_unverified"}
    if "completed" not in preflight:
        return set()
    if not response.tool_calls:
        return set()
    prefix = run_id + ":provider-turn:"
    if not row["request_id"].startswith(prefix):
        return {"proposal_call_identity_unverified"}
    turn_text = row["request_id"][len(prefix) :]
    if not turn_text.isdigit():
        return {"proposal_call_identity_unverified"}
    for ordinal, call in enumerate(response.tool_calls):
        internal, effect = _internal_effect_identity(
            RunId(run_id), int(turn_text), call.call_id.value, ordinal
        )
        gate_hash = audit_hash({"effect": effect.value, "call": internal.value})
        if not any(
            name == "tool.route_gate" and identity == gate_hash for name, identity, _ in inputs
        ):
            return {"proposal_call_disposition_unverified"}
    return set()
