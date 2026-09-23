# SPDX-License-Identifier: Apache-2.0
"""Bind original local checker facts to an original, currently admitted scope."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..assurance.check_bindings import CheckBinding
from ..assurance.check_specs import EXECUTOR_LAYERS
from ..assurance.codec import AssuranceError, decode, fields, fingerprint
from ..assurance.local_checks import validate_local_check
from ..assurance.refs import AssuranceRef, Pin
from ..contracts.operation_completion import OccurrenceCompletionScopeV1
from ..storage.assurance_reads import AssuranceReader
from ..storage.assurance_store import AssuranceStore
from .completion_inputs import load_completion_result_inputs
from .operation_completion import OperationCompletionReader

if TYPE_CHECKING:
    from .assurance_local_checks import AssuranceLocalChecks

# execution_ref kind -> (payload key, receipt commit prefix, import receipt kind,
# adapter pin name, executor-run?)
_SOURCES = {
    "local_check_receipt": (
        "local_check",
        "assurance-local-check:",
        "AssuranceLocalCheckImported",
        "assurance-local-check-adapter",
        False,
    ),
    "execution_receipt": (
        "executor_check",
        "assurance-executor-check:",
        "AssuranceExecutorCheckImported",
        "assurance-executor-check-adapter",
        True,
    ),
}


def read_local_check_binding_locked(
    adapter: AssuranceLocalChecks,
    *,
    mission_id: str,
    execution_ref: AssuranceRef,
    completion_scope: AssuranceRef,
) -> CheckBinding:
    """Read and authenticate original facts; no registration, imports or writes."""
    store = adapter.commit.store
    if not store.connection.in_transaction:
        raise AssuranceError("CHECK_IMPORT_TRANSACTION_REQUIRED")
    adapter._require_deployment()
    source = _SOURCES.get(execution_ref.kind)
    if source is None or completion_scope.kind != "completion_scope":
        raise AssuranceError("CHECK_IMPORT_SOURCE_UNSUPPORTED")
    payload_key, commit_prefix, receipt_kind, adapter_name, executor = source
    reader = AssuranceReader(store, tenant_id=adapter.tenant_id, mission_id=mission_id)
    if AssuranceStore(store).lane(mission_id) != "ASSURANCE_1_1":
        raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
    scope = OccurrenceCompletionScopeV1.from_json(
        decode(reader.read_exact_metadata(completion_scope).body_json)
    )
    if (
        OperationCompletionReader(store).read_scope(mission_id, scope.plan_ref, scope.occurrence_id)
        != scope
    ):
        raise AssuranceError("CHECK_SCOPE_CHANGED")
    event = decode(reader.read_exact_metadata(execution_ref).body_json)
    payload = fields(
        event["payload"],
        {
            payload_key,
            "result_ref",
            "input_manifest_ref",
            "outputs",
            *(("execution",) if executor else ()),
        },
    )
    actual = payload[payload_key]
    result_ref = AssuranceRef.from_json(payload["result_ref"], kinds={"result"})
    manifest_ref = AssuranceRef.from_json(payload["input_manifest_ref"], kinds={"input_manifest"})
    result = decode(reader.read_exact_metadata(result_ref).body_json)
    stored_result = store.get_result(result_ref.pin.id)
    frozen = load_completion_result_inputs(store, stored_result)
    if frozen is None or frozen.scope != scope:
        raise AssuranceError("CHECK_SCOPE_CHANGED")
    manifest = decode(reader.read_exact_metadata(manifest_ref).body_json)
    if (
        result["task_id"] != scope.task_ref.id
        or result["mission_id"] != mission_id
        or event["task_id"] != result["task_id"]
        or event["attempt_id"] != result["attempt_id"]
        or manifest.get("schema") != "assurance-local-verification-input-v1"
        or manifest.get("envelope") != result
        or manifest.get("tenant_id") != adapter.tenant_id
        or manifest.get("subject_hash") != result_ref.pin.content_hash
        or manifest.get("mission_id") != mission_id
        or actual.get("input_manifest_hash") != manifest_ref.pin.content_hash
        or actual.get("environment_hash") != adapter.environment_hash
        or actual.get("recorder_implementation_hash") != adapter.recorder_hash
    ):
        raise AssuranceError("CHECK_SOURCE_IDENTITY")
    spec = AssuranceRef.from_json(actual["check_spec_ref"], kinds={"check_spec"})
    entries = [
        entry
        for entry in adapter._read_registry(mission_id).values()
        if entry.binding.spec_ref == spec
    ]
    if len(entries) != 1 or (entries[0].layer in EXECUTOR_LAYERS) != executor:
        raise AssuranceError("CHECKER_REGISTRY_IDENTITY")
    registered = entries[0]
    state, verdict = validate_local_check(
        actual,
        expected_spec=spec,
        expected_subject_hash=result_ref.pin.content_hash,
        expected_mission_id=mission_id,
        assertion_key=registered.binding.assertion_key,
    )
    key = fingerprint({"mission_id": mission_id, "run_nonce": actual["run_nonce"]})
    source_id = commit_prefix + key
    original = store.connection.execute(
        "SELECT * FROM commit_receipts WHERE commit_id=?", (source_id,)
    ).fetchone()
    expected = {
        "mission_id": mission_id,
        "source_hash": fingerprint(payload),
        "event_ref": execution_ref.to_json(),
        "run_nonce": actual["run_nonce"],
        "result_ref": result_ref.to_json(),
    }
    if (
        original is None
        or original["kind"] != receipt_kind
        or original["subject_id"] != result_ref.pin.id
        or original["base_version"] != 0
        or original["proposal_hash"] != fingerprint(payload)
        or decode(original["receipt_json"]) != expected
    ):
        raise AssuranceError("CHECK_IMPORT_RECEIPT_MISMATCH")
    outputs = tuple(AssuranceRef.from_json(row, kinds={"artifact"}) for row in payload["outputs"])
    assertion_hashes = {
        row["output_hash"] for row in actual["assertions"] if row["output_hash"] is not None
    }
    if (
        len(outputs) != len(set(outputs))
        or {r.pin.content_hash for r in outputs} != assertion_hashes
    ):
        raise AssuranceError("CHECK_OUTPUT_MANIFEST_MISMATCH")
    for ref in outputs:
        output = decode(reader.read_exact_metadata(ref).body_json)
        if output["task_id"] != result["task_id"] or output["attempt_id"] != result["attempt_id"]:
            raise AssuranceError("CHECK_OUTPUT_MANIFEST_MISMATCH")
    return CheckBinding(
        mission_id=mission_id,
        check_spec_ref=spec,
        subject_hash=result_ref.pin.content_hash,
        input_manifest_hash=manifest_ref.pin.content_hash,
        output_manifest_hash=fingerprint(payload["outputs"]),
        assertion_key=registered.binding.assertion_key,
        execution_ref=execution_ref,
        adapter_ref=Pin(adapter_name, 1, adapter.recorder_hash),
        environment_hash=adapter.environment_hash,
        scope_hash=scope.content_hash(),
        execution_state=state,
        verdict=verdict,
        evidence_refs=outputs,
        observed_at_ms=actual["finished_at_ms"],
        not_after_ms=None,
    )


def import_local_check_locked(
    adapter: AssuranceLocalChecks,
    *,
    mission_id: str,
    execution_ref: AssuranceRef,
    completion_scope: AssuranceRef,
) -> AssuranceRef:
    """Original Commit transaction only. Never execute the checker again here."""
    binding = read_local_check_binding_locked(
        adapter,
        mission_id=mission_id,
        execution_ref=execution_ref,
        completion_scope=completion_scope,
    )
    store = adapter.commit.store
    spec = binding.check_spec_ref
    binding_id = "assurance-check:" + fingerprint(
        {
            "mission": mission_id,
            "execution": execution_ref.key,
            "spec": spec.key,
            "subject": binding.subject_hash,
            "assertion": binding.assertion_key,
        }
    )
    binding_hash = fingerprint(binding.to_json())
    receipt_id = "assurance-check-bound:" + binding_id
    receipt = {
        "mission_id": mission_id,
        "check_binding_id": binding_id,
        "binding_hash": binding_hash,
        "execution_ref": execution_ref.to_json(),
    }
    old = store.get_receipt(receipt_id)
    if old is not None and dict(old) != receipt:
        raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
    if old is None:
        store.insert_receipt(
            commit_id=receipt_id,
            kind="AssuranceCheckBound",
            subject_id=binding_id,
            base_version=0,
            proposal_hash=binding_hash,
            receipt=receipt,
        )
    ref = AssuranceStore(store).record_check_binding(
        binding_id,
        binding,
        receipt=AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(receipt))),
    )
    if old is None:
        reader = AssuranceReader(store, tenant_id=adapter.tenant_id, mission_id=mission_id)
        event = decode(reader.read_exact_metadata(execution_ref).body_json)
        adapter.commit._emit(
            "AssuranceCheckBound",
            mission_id,
            key=binding_id,
            task_id=event["task_id"],
            attempt_id=event["attempt_id"],
            payload={"check_binding_ref": ref.to_json(), "execution_ref": execution_ref.to_json()},
        )
    return ref
