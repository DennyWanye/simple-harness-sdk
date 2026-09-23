"""Strict V1.4 completion documents before any outcome/import path exists.

OCC-02 source: §2.1–§2.2 and §3.1 of the Operation Completion addendum.  These
are contract-boundary checks: malformed documents must never reach the future
requirement approval, plan compiler, T0, or T3 writers.  OCC-01/OCC-03–OCC-12
remain deliberately absent here: their production seams require the actual
acceptance/outcome/resolution producers, and are not represented as skips.
"""

from __future__ import annotations

import json

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.operation_completion import (
    MAX_COMPLETION_BYTES,
    AcceptanceContributionScopeV1,
    OccurrenceCompletionScopeV1,
    OperationCompletionRequirementsV1,
    OperationOutcomeReviewBindingV1,
    validate_scope_owners,
    validate_spec_scope_coverage,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _pin(identifier: str, revision: int, content_hash: str) -> dict[str, object]:
    return {"id": identifier, "revision": revision, "content_hash": content_hash}


def _required_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "requirements_ref": _pin("requirements-1", 1, HASH_A),
        "mode": "REQUIRED_EFFECTS",
        "content_criterion_ids": ["criterion-report"],
        "effects": [
            {
                "effect_key": "deliver-report",
                "obligation_id": "obligation-root",
                "criterion_ids": ["criterion-delivered"],
                "required_milestone": "DELIVERED",
                "milestone_policy_ref": _pin("milestone-policy", 1, HASH_B),
                "evidence_policy_ref": _pin("evidence-policy", 1, HASH_C),
                "source_slot_key": "approved-delivery-slot",
            }
        ],
    }


def _aggregate_scope(*, owned: list[str] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "requirements_ref": _pin("requirements-1", 1, HASH_A),
        "spec_hash": OperationCompletionRequirementsV1.from_json(_required_spec()).content_hash(),
        "plan_ref": {"revision": 1, "snapshot_hash": HASH_D},
        "occurrence_id": "occurrence-root",
        "task_ref": _pin("task-root", 1, HASH_D),
        "obligation_id": "obligation-root",
        "role": "AGGREGATE",
        "content_criterion_ids": ["criterion-report"],
        "required_effect_keys": ["deliver-report"],
        "owned_effect_keys": ["deliver-report"] if owned is None else owned,
    }


def _content_scope() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "requirements_ref": _pin("requirements-1", 1, HASH_A),
        "spec_hash": OperationCompletionRequirementsV1.from_json(_required_spec()).content_hash(),
        "plan_ref": {"revision": 1, "snapshot_hash": HASH_D},
        "occurrence_id": "occurrence-report",
        "task_ref": _pin("task-report", 1, HASH_B),
        "obligation_id": "obligation-root",
        "role": "CONTENT",
        "content_criterion_ids": ["criterion-report"],
        "required_effect_keys": [],
        "owned_effect_keys": [],
    }


def _content_contribution() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "acceptance_id": "acceptance-content",
        "completion_scope_id": "scope-content",
        "spec_hash": HASH_A,
        "kind": "CONTENT",
        "content_criterion_ids": ["criterion-report"],
        "effect_keys": [],
        "output_artifact_refs": [],
        "outcome_binding_id": None,
        "delivery_receipt_ref": None,
    }


def _outcome_binding() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": "mission-completion-contract",
        "spec_hash": HASH_A,
        "effect_key": "deliver-report",
        "completion_scope_id": "scope-root",
        "intent_id": "intent-deliver-report",
        "operation_id": "operation-deliver-report",
        "operation_occurrence_id": "operation-occurrence-deliver-report",
        "action_key": "deliver-report",
        "action_version": 1,
        "request_hash": HASH_A,
        "candidate_file_hash": HASH_B,
        "parameters_content_hash": HASH_C,
        "params_hash": HASH_D,
        "effect_contract_hash": HASH_A,
        "link_hash": HASH_B,
        "connector_profile_hash": HASH_C,
        "namespace_hash": HASH_D,
        "target_identity_hash": HASH_A,
        "milestone_policy_ref": _pin("milestone-policy", 1, HASH_B),
        "observed_milestone": "DELIVERED",
        "covered_handoff_ids": ["handoff-deliver-report"],
        "source_receipt_refs": [_pin("receipt-deliver-report", 1, HASH_C)],
    }


def test_occ02_strict_spec_codec_preserves_a_approved_effect_mapping() -> None:
    """OCC-02 / contracts.operation_completion strict-codec seam.

    A correctly formed approved mapping survives canonical encoding.  This is a
    document-contract oracle, not evidence that a model, capability profile, or
    absent intent may infer an effect.
    """

    spec = OperationCompletionRequirementsV1.from_json(_required_spec())

    assert spec.to_json() == _required_spec()
    assert OperationCompletionRequirementsV1.from_bytes(spec.to_bytes()) == spec
    assert len(spec.content_hash()) == 64


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: {**raw, "untrusted_approved": True},
        lambda raw: {**raw, "schema_version": True},
        lambda raw: {**raw, "effects": []},
        lambda raw: {**raw, "content_criterion_ids": ["criterion-report", "criterion-delivered"]},
        lambda raw: {
            **raw,
            "effects": [
                *raw["effects"],  # type: ignore[index]
                {**raw["effects"][0], "source_slot_key": "another-slot"},  # type: ignore[index]
            ],
        },
    ),
)
def test_occ02_spec_rejects_unapproved_or_ambiguous_document_shape(mutate) -> None:
    """OCC-02 / strict decoder: no `.get(..., [])` default may lower requirements."""

    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_json(mutate(_required_spec()))


def test_occ02_duplicate_json_key_cannot_replace_an_effect_requirement() -> None:
    """OCC-02 / byte decoder: duplicate JSON keys cannot turn REQUIRED into CONTENT."""

    raw = json.dumps(_required_spec(), separators=(",", ":"))
    duplicate = raw.replace(
        '"mode":"REQUIRED_EFFECTS"', '"mode":"REQUIRED_EFFECTS","mode":"CONTENT_ONLY"'
    )

    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_bytes(duplicate.encode("utf-8"))


@pytest.mark.parametrize(
    ("decoder", "raw_factory", "replace_array"),
    (
        (
            OperationCompletionRequirementsV1,
            _required_spec,
            lambda raw, value: raw["effects"][0].__setitem__("criterion_ids", value),  # type: ignore[index,union-attr]
        ),
        (
            OperationCompletionRequirementsV1,
            _required_spec,
            lambda raw, value: raw.__setitem__("content_criterion_ids", value),
        ),
        (
            OccurrenceCompletionScopeV1,
            _aggregate_scope,
            lambda raw, value: raw.__setitem__("content_criterion_ids", value),
        ),
        (
            OccurrenceCompletionScopeV1,
            _aggregate_scope,
            lambda raw, value: raw.__setitem__("required_effect_keys", value),
        ),
        (
            OccurrenceCompletionScopeV1,
            _aggregate_scope,
            lambda raw, value: raw.__setitem__("owned_effect_keys", value),
        ),
        (
            AcceptanceContributionScopeV1,
            _content_contribution,
            lambda raw, value: raw.__setitem__("content_criterion_ids", value),
        ),
        (
            AcceptanceContributionScopeV1,
            _content_contribution,
            lambda raw, value: raw.__setitem__("effect_keys", value),
        ),
        (
            OperationOutcomeReviewBindingV1,
            _outcome_binding,
            lambda raw, value: raw.__setitem__("covered_handoff_ids", value),
        ),
    ),
)
@pytest.mark.parametrize("not_an_array", ("criterion", {"criterion": "criterion"}, 1, True, None))
def test_occ02_from_json_rejects_non_array_identifier_fields(
    decoder, raw_factory, replace_array, not_an_array
) -> None:
    """Every JSON identifier-array is validated before any tuple conversion."""

    raw = raw_factory()
    replace_array(raw, not_an_array)

    with pytest.raises(ContractError):
        decoder.from_json(raw)


def test_occ02_byte_codec_normalizes_deep_and_oversize_json_to_contract_error() -> None:
    """Malformed untrusted bytes must never escape as parser recursion errors."""

    deep: object = "leaf"
    for _ in range(17):
        deep = {"nested": deep}
    parser_deep = (b'{"nested":' * 2_000) + b"null" + (b"}" * 2_000)
    oversized = b'{"payload":"' + (b"x" * MAX_COMPLETION_BYTES) + b'"}'

    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_bytes(json.dumps(deep).encode("utf-8"))
    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_bytes(parser_deep)
    with pytest.raises(ContractError):
        OperationCompletionRequirementsV1.from_bytes(oversized)


def test_occ02_scope_coverage_and_owner_are_checked_across_real_scope_documents() -> None:
    """OCC-02/OCC-10 / compiler contract seam.

    The aggregate scope owns the MUST effect while a sibling is CONTENT-only;
    sharing an ObligationId alone never copies an effect requirement to that child.
    """

    spec = OperationCompletionRequirementsV1.from_json(_required_spec())
    aggregate = OccurrenceCompletionScopeV1.from_json(_aggregate_scope())
    content = OccurrenceCompletionScopeV1.from_json(_content_scope())

    validate_spec_scope_coverage(spec, (aggregate, content), root_occurrence_id="occurrence-root")
    validate_scope_owners(spec, (aggregate, content))


def test_occ02_scope_revalidation_refuses_missing_or_duplicate_effect_owner() -> None:
    """OCC-02/OCC-10 / plan revalidation seam, before any plan row is written."""

    spec = OperationCompletionRequirementsV1.from_json(_required_spec())
    missing_owner = OccurrenceCompletionScopeV1.from_json(_aggregate_scope(owned=[]))
    content = OccurrenceCompletionScopeV1.from_json(_content_scope())
    duplicate_owner_raw = _content_scope()
    duplicate_owner_raw.update(
        role="MIXED",
        required_effect_keys=["deliver-report"],
        owned_effect_keys=["deliver-report"],
    )
    duplicate_owner = OccurrenceCompletionScopeV1.from_json(duplicate_owner_raw)

    with pytest.raises(ContractError):
        validate_scope_owners(spec, (missing_owner, content))
    with pytest.raises(ContractError):
        validate_scope_owners(
            spec, (OccurrenceCompletionScopeV1.from_json(_aggregate_scope()), duplicate_owner)
        )


def test_occ02_primitive_mixed_root_is_a_complete_spec_scope() -> None:
    """A real primitive root may carry CONTENT and all required effects itself."""

    spec = OperationCompletionRequirementsV1.from_json(_required_spec())
    raw = _aggregate_scope()
    raw["role"] = "MIXED"
    primitive_mixed = OccurrenceCompletionScopeV1.from_json(raw)

    validate_spec_scope_coverage(spec, (primitive_mixed,), root_occurrence_id="occurrence-root")
    validate_scope_owners(spec, (primitive_mixed,))
