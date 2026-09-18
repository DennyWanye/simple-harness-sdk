# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strict codec for the H1 ``<planning_decision>`` wire object (V2 §42).

This module is the boundary between the model's raw reply and the contract
object.  It is a *pure* function: it reads no store, opens no database and sends
no event.  The caller supplies the request id, the attempt ordinal and the raw
output hash; the codec only decodes, refuses and canonicalises.

``parse_planning_decision`` is the seven-step flow of V2 §42:

1. exactly one ``<planning_decision>`` block, via the *unchanged*
   :func:`agent_orchestrator.runtime.output_blocks.extract_block`;
2. mixed-protocol guard — a reply that also carries a legacy
   ``<plan_revision_proposal>`` or a synthesizer ``<method_proposal>`` block is
   refused even though the decision block itself parsed;
3. the block body is already JSON; a non-object is refused by ``extract_block``;
4. an envelope key that is neither a Core field nor a system field is
   ``UNKNOWN_FIELD``;
5. the envelope decodes through ``PlanningDecisionEnvelopeV1.from_json``;
6. a system field (§32) written at the envelope top level, among a payload's own
   keys or inside a ``reason_refs[]`` entry is ``MODEL_SET_SYSTEM_FIELD`` — the
   keys of ``payload.bindings`` are *values*, never claims;
7. canonicalisation happens in :func:`canonical_decision_json` /
   :func:`decision_payload_hash`, never by mutating the returned envelope.

A malformed reply never half-returns: every refusal raises
:class:`PlanningDecisionCodecError`.  The contract layer's own refusals are
translated through the explicit :data:`CONTRACT_ERROR_CODE_MAPPINGS` table; a
``ContractError`` no row matches becomes ``MALFORMED_DECISION`` and is never
swallowed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts.models import ContractError
from ..contracts.planning_decisions import (
    ENVELOPE_FIELDS,
    PlanningDecisionEnvelopeV1,
    PlanningDecisionRejectionCode,
    PlanningDecisionType,
    canonical_decision_hash,
    compute_decision_id,
)
from ..contracts.planning_decisions import (
    canonical_decision_json as _contract_canonical_decision_json,
)
from ..runtime.output_blocks import BlockError, extract_block
from .htn.registry import MethodProposal

#: §13 / §58: the tag, the codec version string and the two foreign tags the
#: mixed-protocol guard watches for.  They live here (not in ``role_templates``,
#: which is H1-E's hot file).
PLANNING_DECISION_TAG = "planning_decision"
PLANNING_DECISION_CODEC_V1 = "planning-decision-codec-v1"
LEGACY_PLAN_BLOCK_TAG = "plan_revision_proposal"
METHOD_BLOCK_TAG = "method_proposal"

#: V2 §32: the fields the system binds.  A model that writes one of these at the
#: envelope top level, among a payload's own keys or inside a ``reason_refs[]``
#: entry is claiming an authority it does not have.  The keys of
#: ``payload.bindings`` are deliberately *not* scanned: a domain parameter that
#: happens to be called ``scope`` is a value, not a claim (§32, and the P2.3
#: ``test_a_method_parameter_that_shares_a_name_with_a_bound_field_is_a_value``).
SYSTEM_FIELD_KEYS = frozenset(
    {
        "mission_id",
        "tenant_id",
        "principal",
        "principal_id",
        "scope",
        "scope_id",
        "manager_epoch",
        "budget_account",
        "budget_grant_revision",
        "registry_status",
        "opened_by",
        "authorization_ref",
        "grant_ref",
        "provenance",
        "authored_by",
        "dispatch_generation",
        "plan_revision",
        "expected_plan_revision",
        "operation_id",
        "acceptance_id",
        "approval_id",
        "decision_id",
        "request_id",
    }
)

_BLOCK_ERROR_CODES: Mapping[str, PlanningDecisionRejectionCode] = {
    "empty_output": PlanningDecisionRejectionCode.DECISION_BLOCK_MISSING,
    "block_missing": PlanningDecisionRejectionCode.DECISION_BLOCK_MISSING,
    "block_ambiguous": PlanningDecisionRejectionCode.MULTIPLE_DECISIONS,
    "invalid_json": PlanningDecisionRejectionCode.MALFORMED_DECISION,
    "not_an_object": PlanningDecisionRejectionCode.MALFORMED_DECISION,
}


class PlanningDecisionCodecError(ContractError):
    """A planner reply the codec refused, carrying the §33 rejection code.

    Not a generic ``ContractError``: the caller needs the code to record the
    refusal, route the repair hint and decide whether to re-ask, so the code is
    carried as a first-class attribute rather than left in the message.
    """

    def __init__(self, code: PlanningDecisionRejectionCode, detail: str = "") -> None:
        if not isinstance(code, PlanningDecisionRejectionCode):
            code = PlanningDecisionRejectionCode(str(code))
        super().__init__(f"{code}: {detail}" if detail else f"{code}")
        self._code = code
        self.detail = detail

    @property
    def code(self) -> PlanningDecisionRejectionCode:
        return self._code


# --------------------------------------------------------------------------------------
# Mapping contract-layer refusals onto rejection codes (one row per decision)
# --------------------------------------------------------------------------------------


def _is_unknown_decision_type(error: ContractError) -> bool:
    """``enum_of(PlanningDecisionType, ...)`` rejected a *string* value.

    ``enum_of`` has two refusals with different meanings: ``... must be one of
    [...]`` (a string that is not a known type -> ``DECISION_TYPE_UNKNOWN``) and
    ``... must be a string`` (a wrong type or ``null`` -> ``MALFORMED_DECISION``).
    Matching the former only keeps a malformed shape out of the "unknown type"
    bucket.
    """

    message = str(error)
    return message.startswith("decision.decision_type must be one of ")


@dataclass(frozen=True, slots=True)
class ContractErrorMapping:
    """One row of the contract-error -> rejection-code table.

    ``matches`` is the predicate that claims an error; the last row of the table
    carries ``matches=None`` and acts as the ``MALFORMED_DECISION`` fallback, so
    an unmapped refusal can never be dropped on the floor.
    """

    code: PlanningDecisionRejectionCode
    matches: Callable[[ContractError], bool] | None
    detail: str

    def claims(self, error: ContractError) -> bool:
        return self.matches is None or self.matches(error)


#: Ordered: the first row whose ``matches`` claims the error names the code, else
#: the ``matches=None`` fallback.  Adding a specific mapping means inserting a row
#: *above* the fallback and pinning it with its own test.
CONTRACT_ERROR_CODE_MAPPINGS: tuple[ContractErrorMapping, ...] = (
    ContractErrorMapping(
        code=PlanningDecisionRejectionCode.DECISION_TYPE_UNKNOWN,
        matches=_is_unknown_decision_type,
        detail="decision_type is not one of the nine H1 types",
    ),
    ContractErrorMapping(
        code=PlanningDecisionRejectionCode.MALFORMED_DECISION,
        matches=None,
        detail="the contract layer refused the envelope shape",
    ),
)


def _map_contract_error(error: ContractError) -> PlanningDecisionRejectionCode:
    for row in CONTRACT_ERROR_CODE_MAPPINGS:
        if row.claims(error):
            return row.code
    # Unreachable while the table ends in a matches=None fallback, but a wrong
    # table must degrade to MALFORMED_DECISION rather than raise a bare error.
    return PlanningDecisionRejectionCode.MALFORMED_DECISION


# --------------------------------------------------------------------------------------
# The strict parse
# --------------------------------------------------------------------------------------


def _refuse_system_fields(raw: Mapping[str, Any], where: str) -> None:
    claimed = sorted(SYSTEM_FIELD_KEYS & set(raw))
    if claimed:
        raise PlanningDecisionCodecError(
            PlanningDecisionRejectionCode.MODEL_SET_SYSTEM_FIELD,
            f"{where} sets {claimed}, which the system binds",
        )


def _scan_structural_system_fields(raw: Mapping[str, Any]) -> None:
    """§32 at the three structural positions, and nowhere else.

    Envelope top level, each payload's *own* keys, and each ``reason_refs[]``
    entry.  ``payload.bindings`` is skipped on purpose: those keys are a domain's
    vocabulary, not a claim about authority.
    """

    _refuse_system_fields(raw, "planning decision")
    payload = raw.get("payload")
    if isinstance(payload, Mapping):
        _refuse_system_fields(payload, "decision.payload")
    refs = raw.get("reason_refs")
    if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
        for position, ref in enumerate(refs):
            if isinstance(ref, Mapping):
                _refuse_system_fields(ref, f"decision.reason_refs[{position}]")


def _refuse_unknown_keys(raw: Mapping[str, Any]) -> None:
    unknown = sorted(set(raw) - set(ENVELOPE_FIELDS) - SYSTEM_FIELD_KEYS)
    if unknown:
        raise PlanningDecisionCodecError(
            PlanningDecisionRejectionCode.UNKNOWN_FIELD,
            f"planning decision has unknown fields: {unknown}",
        )


def _refuse_foreign_blocks(text: str) -> None:
    for tag in (LEGACY_PLAN_BLOCK_TAG, METHOD_BLOCK_TAG):
        if f"<{tag}>" in text:
            raise PlanningDecisionCodecError(
                PlanningDecisionRejectionCode.MIXED_PROTOCOL_BLOCKS,
                f"the reply also carries a <{tag}> block",
            )


def _validate_method_proposal(payload: object) -> None:
    """Addendum-2 §三: a PROPOSE_METHOD payload's inner proposal is real JSON.

    The contract layer only requires ``method_proposal`` to be an object (it does
    not import the planning package); the H1-C codec is where the existing
    :class:`MethodProposal` codec validates it.  A refusal is a malformed
    decision, never a silent drop.
    """

    inner = payload.get("method_proposal") if isinstance(payload, Mapping) else None
    try:
        MethodProposal.from_json(inner)
    except ContractError as error:
        raise PlanningDecisionCodecError(
            PlanningDecisionRejectionCode.MALFORMED_DECISION,
            f"propose_method.method_proposal is unreadable: {error}",
        ) from error


def parse_planning_decision(
    text: str,
    *,
    request_id: str | None = None,
    attempt_ordinal: int | None = None,
    raw_output_hash: str | None = None,
) -> PlanningDecisionEnvelopeV1:
    """Decode one planner reply into a ``PlanningDecisionEnvelopeV1`` (V2 §42).

    ``request_id`` / ``attempt_ordinal`` / ``raw_output_hash`` are supplied by the
    caller (H1-F) and do not participate in the decode: the model never writes a
    decision id, so the id is minted by :func:`allocate_decision_id` once the
    caller has the three parts.  They are accepted here so a caller can pass the
    same request context it used to ask, without the decode depending on it.
    """

    del request_id, attempt_ordinal, raw_output_hash  # reserved for the caller

    # 1. The single, unique decision block.
    try:
        raw = extract_block(text, PLANNING_DECISION_TAG)
    except BlockError as error:
        code = _BLOCK_ERROR_CODES.get(
            error.reason, PlanningDecisionRejectionCode.MALFORMED_DECISION
        )
        raise PlanningDecisionCodecError(code, error.detail or error.reason) from error

    # 2. Mixed-protocol guard: the block parsed, but a foreign block tags along.
    _refuse_foreign_blocks(text)

    # 3. JSON is already parsed; a non-object was refused by extract_block.
    # 4. Unknown envelope keys, with the §32 system fields classified separately
    #    so a system key is never swallowed as a generic unknown field.
    if not isinstance(raw, Mapping):  # pragma: no cover - extract_block returns a dict
        raise PlanningDecisionCodecError(
            PlanningDecisionRejectionCode.MALFORMED_DECISION,
            f"the decision block is a {type(raw).__name__}, not an object",
        )
    _scan_structural_system_fields(raw)
    _refuse_unknown_keys(raw)

    # 5. The envelope decodes through the contract; its refusals map by table.
    try:
        decision = PlanningDecisionEnvelopeV1.from_json(raw)
    except ContractError as error:
        raise PlanningDecisionCodecError(_map_contract_error(error), str(error)) from error

    # The inner method proposal of a decode-only PROPOSE_METHOD is real.
    if decision.decision_type is PlanningDecisionType.PROPOSE_METHOD:
        _validate_method_proposal(raw.get("payload"))

    # 6/7. The scan above already ran on the raw object; canonicalisation is a
    # separate function so the returned envelope is never mutated.
    return decision


# --------------------------------------------------------------------------------------
# Canonicalisation, hashing and the deterministic decision id (§15, §35)
# --------------------------------------------------------------------------------------


def serialize_planning_decision(decision: PlanningDecisionEnvelopeV1) -> str:
    """``'<planning_decision>' + canonical_json(decision.to_json()) + '</planning_decision>'``."""

    return f"<{PLANNING_DECISION_TAG}>{canonical_decision_json(decision)}</{PLANNING_DECISION_TAG}>"


def canonical_decision_json(decision: PlanningDecisionEnvelopeV1) -> str:
    """The §15 canonical JSON; object key order does not move it, array order does.

    Delegates to the contract's single canonicalisation so the codec and the
    envelope cannot drift apart.
    """

    return _contract_canonical_decision_json(decision)


def decision_payload_hash(decision: PlanningDecisionEnvelopeV1) -> str:
    """``sha256`` of :func:`canonical_decision_json`, lowercase hex (64 chars).

    The model never submits this hash; the codec derives it (V2 §15).
    """

    return canonical_decision_hash(decision)


def allocate_decision_id(
    *, request_id: str, attempt_ordinal: int, raw_output_hash: str
) -> str:
    """The deterministic §35 id, delegated to the contract's one formula."""

    return compute_decision_id(request_id, attempt_ordinal, raw_output_hash)


def hash_raw_output(raw_bytes: bytes) -> str:
    """``sha256`` of the model's raw reply bytes, lowercase hex (V2 §15)."""

    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise ContractError("raw output must be bytes")
    return hashlib.sha256(bytes(raw_bytes)).hexdigest()


__all__ = (
    "CONTRACT_ERROR_CODE_MAPPINGS",
    "ContractErrorMapping",
    "LEGACY_PLAN_BLOCK_TAG",
    "METHOD_BLOCK_TAG",
    "PLANNING_DECISION_CODEC_V1",
    "PLANNING_DECISION_TAG",
    "SYSTEM_FIELD_KEYS",
    "PlanningDecisionCodecError",
    "allocate_decision_id",
    "canonical_decision_json",
    "decision_payload_hash",
    "hash_raw_output",
    "parse_planning_decision",
    "serialize_planning_decision",
)
