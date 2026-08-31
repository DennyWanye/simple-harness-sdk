# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast

import pytest

import simple_harness
import simple_harness.runtime as runtime
from simple_harness.contracts import fingerprint_json
from simple_harness.runtime import MemoryScopeRef
from simple_harness.runtime.evidence_protocol import (
    EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1,
    EvidenceActorRole,
    EvidenceProvenance,
    EvidenceSourceKind,
    EvidenceSpanRef,
    EvidenceSupportKind,
)
from simple_harness.runtime.memory_protocol import (
    ProcedureLifecycleState,
    ProcedureRiskLevel,
    ProspectiveEventTrigger,
    ProspectiveLifecycleState,
    ProspectiveTimeTrigger,
)
from simple_harness.runtime.procedure_observation_protocol import (
    PROCEDURE_APPLICABILITY_FINGERPRINT_VERSION,
    PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION,
    ProcedureApplicabilityContext,
    ProcedureHazard,
    ProcedureObservationAuthority,
    ProcedureObservationAuthorityRef,
    ProcedureObservationIntent,
    ProcedureObservationKind,
    ProcedureObservationOutcome,
    issue_procedure_observation_authority,
    verify_procedure_observation_authority,
)
from simple_harness.runtime.prospective_signal_protocol import (
    PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION,
    ProspectiveSignalAuthority,
    ProspectiveSignalAuthorityRef,
    ProspectiveSignalIntent,
    ProspectiveSignalKind,
    issue_prospective_signal_authority,
    prospective_trigger_hash,
    verify_prospective_signal_authority,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _span() -> EvidenceSpanRef:
    quote = "terminal tool result"
    return EvidenceSpanRef(
        span_id="span-1",
        evidence_id="evidence-1",
        envelope_hash="a" * 64,
        sanitized_hash="b" * 64,
        admission_receipt_id="admission-1",
        admission_receipt_hash="c" * 64,
        source_kind=EvidenceSourceKind.TOOL_RESULT,
        item_ordinal=1,
        item_id="tool-result-1",
        item_json_pointer="/public_text",
        start_byte=0,
        end_byte=len(quote.encode("utf-8")),
        exact_quote=quote,
        quote_hash=_sha(quote),
        source_hash="d" * 64,
        normalization_version=EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1,
        actor_role=EvidenceActorRole.TOOL,
        provenance=EvidenceProvenance.TRUSTED_TOOL,
        support_kind=EvidenceSupportKind.RUNTIME_EVENT,
        typed_observation=None,
    )


def _applicability() -> ProcedureApplicabilityContext:
    return ProcedureApplicabilityContext(
        "tool.publish", "macos", "1.2.3", "e" * 64
    )


def _procedure_intent(**changes: object) -> ProcedureObservationIntent:
    value = ProcedureObservationIntent(
        observation_id="procedure-observation-1",
        subject="user-1",
        scope=MemoryScopeRef.personal("user-1"),
        target_memory_id="procedure-1",
        target_revision=3,
        kind=ProcedureObservationKind.TERMINAL_OUTCOME,
        applicability=_applicability(),
        risk_level=ProcedureRiskLevel.LOW,
        hazard=ProcedureHazard.NONE,
        task_scope_id="task-scope-1",
        evidence_span=_span(),
        terminal_receipt_id="terminal-receipt-1",
        terminal_receipt_hash="f" * 64,
        outcome=ProcedureObservationOutcome.SUCCESS,
        attributable=True,
        observed_at=12.0,
        transition_from=ProcedureLifecycleState.ELIGIBLE_FOR_ACTIVATION,
        transition_to=ProcedureLifecycleState.ACTIVE,
        run_id="run-1",
        operation_id="procedure-operation-1",
    )
    return replace(value, **cast(Any, changes))


class _ProcedureAuthorityPort:
    def __init__(self, authority: ProcedureObservationAuthority) -> None:
        self.authority = authority
        self.calls = 0

    async def resolve_procedure_observation_authority(
        self, reference: ProcedureObservationAuthorityRef
    ) -> ProcedureObservationAuthority:
        del reference
        self.calls += 1
        return self.authority


def _procedure_authority() -> tuple[
    ProcedureObservationIntent,
    ProcedureObservationAuthority,
    ProcedureObservationAuthorityRef,
]:
    intent = _procedure_intent()
    authority = issue_procedure_observation_authority(
        intent,
        authority_id="procedure-authority-1",
        issued_at=10.0,
        expires_at=20.0,
        nonce="procedure-nonce-1",
        issuer_ref="host-procedure-authority:v1",
    )
    return intent, authority, ProcedureObservationAuthorityRef.from_authority(authority)


def test_procedure_authority_is_ref_only_exact_once_and_round_trips_strictly() -> None:
    intent, authority, reference = _procedure_authority()
    port = _ProcedureAuthorityPort(authority)
    assert PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION == 1
    assert PROCEDURE_APPLICABILITY_FINGERPRINT_VERSION == 1
    assert _applicability().fingerprint == _sha(
        "\x1f".join(("tool.publish", "macos", "1.2.3", "e" * 64))
    )
    assert asyncio.run(
        verify_procedure_observation_authority(reference, port, current_time=10.0)
    ) is authority
    assert port.calls == 1
    assert ProcedureObservationIntent.from_json(intent.to_json()) == intent
    assert ProcedureObservationAuthority.from_json(authority.to_json()) == authority
    assert ProcedureObservationAuthorityRef.from_json(reference.to_json()) == reference
    assert len(authority.replay_identity) == len(authority.authority_hash) == 64
    with pytest.raises(TypeError, match="reference"):
        asyncio.run(
            verify_procedure_observation_authority(
                cast(ProcedureObservationAuthorityRef, authority), port, current_time=15.0
            )
        )


@pytest.mark.parametrize(
    "current_time, message",
    ((9.999, "not yet valid"), (20.0, "expired"), (21.0, "expired")),
)
def test_procedure_authority_time_window_is_half_open(
    current_time: float, message: str
) -> None:
    _, authority, reference = _procedure_authority()
    with pytest.raises(ValueError, match=message):
        asyncio.run(
            verify_procedure_observation_authority(
                reference, _ProcedureAuthorityPort(authority), current_time=current_time
            )
        )


@pytest.mark.parametrize(
    "changed",
    (
        {"subject": "user-2"},
        {"scope": MemoryScopeRef.family("household-1")},
        {"target_revision": 4},
        {"terminal_receipt_hash": "9" * 64},
        {"transition_to": ProcedureLifecycleState.REINFORCED},
        {"run_id": "run-2"},
        {"operation_id": "procedure-operation-2"},
    ),
)
def test_procedure_intent_hash_binds_identity_scope_receipt_transition_and_operation(
    changed: dict[str, object],
) -> None:
    original = _procedure_intent()
    assert replace(original, **cast(Any, changed)).intent_hash != original.intent_hash


def test_procedure_protocol_rejects_unsafe_transition_and_legacy_or_unknown_wire() -> None:
    with pytest.raises(ValueError, match="high-risk observation"):
        _procedure_intent(hazard=ProcedureHazard.PUBLISH)
    with pytest.raises(ValueError, match="non-attributable"):
        _procedure_intent(
            outcome=ProcedureObservationOutcome.FAILURE,
            attributable=False,
            transition_to=ProcedureLifecycleState.REVISED,
        )
    with pytest.raises(ValueError, match="invalid expected transition"):
        _procedure_intent(transition_to=ProcedureLifecycleState.SUPERSEDED)
    _, authority, reference = _procedure_authority()
    for decoder, wire in (
        (ProcedureObservationIntent.from_json, _procedure_intent().to_json()),
        (ProcedureObservationAuthority.from_json, authority.to_json()),
        (ProcedureObservationAuthorityRef.from_json, reference.to_json()),
    ):
        unknown = deepcopy(wire)
        unknown["unknown"] = "rejected"
        with pytest.raises(ValueError, match="fields differ"):
            decoder(unknown)
        legacy = deepcopy(wire)
        legacy["schema_version"] = 0
        with pytest.raises(ValueError, match="unsupported"):
            decoder(legacy)


def _prospective_intent(**changes: object) -> ProspectiveSignalIntent:
    value = ProspectiveSignalIntent(
        signal_id="scheduler-signal-1",
        subject="user-1",
        scope=MemoryScopeRef.personal("user-1"),
        target_memory_id="prospective-1",
        target_revision=2,
        signal_kind=ProspectiveSignalKind.TIME_DUE,
        trigger=ProspectiveTimeTrigger(100.0, "Asia/Shanghai"),
        scheduler_registration_ref="scheduler-registration-1",
        registration_revision=1,
        signal_receipt_id="clock-receipt-1",
        signal_receipt_hash="1" * 64,
        observed_at=100.0,
        transition_from=ProspectiveLifecycleState.PENDING,
        transition_to=ProspectiveLifecycleState.TRIGGERED,
        outbox_id=None,
        outbox_payload_hash=None,
        run_id="run-1",
        operation_id="prospective-operation-1",
    )
    return replace(value, **cast(Any, changes))


class _ProspectiveAuthorityPort:
    def __init__(self, authority: ProspectiveSignalAuthority) -> None:
        self.authority = authority
        self.calls = 0

    async def resolve_prospective_signal_authority(
        self, reference: ProspectiveSignalAuthorityRef
    ) -> ProspectiveSignalAuthority:
        del reference
        self.calls += 1
        return self.authority


def _prospective_authority() -> tuple[
    ProspectiveSignalIntent,
    ProspectiveSignalAuthority,
    ProspectiveSignalAuthorityRef,
]:
    intent = _prospective_intent()
    authority = issue_prospective_signal_authority(
        intent,
        authority_id="prospective-authority-1",
        issued_at=10.0,
        expires_at=20.0,
        nonce="prospective-nonce-1",
        issuer_ref="host-prospective-authority:v1",
    )
    return intent, authority, ProspectiveSignalAuthorityRef.from_authority(authority)


def test_prospective_authority_is_ref_only_exact_once_and_round_trips_strictly() -> None:
    intent, authority, reference = _prospective_authority()
    port = _ProspectiveAuthorityPort(authority)
    assert PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION == 1
    assert prospective_trigger_hash(intent.trigger) == intent.trigger_hash
    assert asyncio.run(
        verify_prospective_signal_authority(reference, port, current_time=10.0)
    ) is authority
    assert port.calls == 1
    assert ProspectiveSignalIntent.from_json(intent.to_json()) == intent
    assert ProspectiveSignalAuthority.from_json(authority.to_json()) == authority
    assert ProspectiveSignalAuthorityRef.from_json(reference.to_json()) == reference
    assert len(intent.occurrence_key) == len(authority.replay_identity) == 64


@pytest.mark.parametrize(
    "changed",
    (
        {"subject": "user-2"},
        {"scope": MemoryScopeRef.family("household-1")},
        {"target_revision": 3},
        {"trigger": ProspectiveTimeTrigger(101.0, "Asia/Shanghai"), "observed_at": 101.0},
        {"signal_receipt_hash": "2" * 64},
        {"registration_revision": 2},
        {"run_id": "run-2"},
        {"operation_id": "prospective-operation-2"},
    ),
)
def test_prospective_intent_hash_binds_identity_scope_trigger_receipt_and_operation(
    changed: dict[str, object],
) -> None:
    original = _prospective_intent()
    assert replace(original, **cast(Any, changed)).intent_hash != original.intent_hash


def test_prospective_protocol_binds_event_and_registration_ack_shapes() -> None:
    condition = "deployment.completed"
    event = _prospective_intent(
        signal_kind=ProspectiveSignalKind.EVENT_OCCURRED,
        trigger=ProspectiveEventTrigger(
            "host-event-bus:v1", condition, fingerprint_json(condition)
        ),
    )
    assert event.transition_to is ProspectiveLifecycleState.TRIGGERED
    ack = _prospective_intent(
        signal_kind=ProspectiveSignalKind.REGISTRATION_ACCEPTED,
        transition_to=ProspectiveLifecycleState.PENDING,
        outbox_id="prospective-registration-outbox-1",
        outbox_payload_hash="3" * 64,
    )
    assert ProspectiveSignalIntent.from_json(ack.to_json()) == ack
    with pytest.raises(ValueError, match="acknowledgement requires exact outbox"):
        _prospective_intent(
            signal_kind=ProspectiveSignalKind.REGISTRATION_ACCEPTED,
            transition_to=ProspectiveLifecycleState.PENDING,
        )
    with pytest.raises(ValueError, match="time_due requires"):
        _prospective_intent(
            trigger=ProspectiveEventTrigger(
                "host-event-bus:v1", condition, fingerprint_json(condition)
            )
        )


def test_prospective_authority_rejects_mismatch_time_boundary_and_strict_wire() -> None:
    intent, authority, reference = _prospective_authority()
    changed_authority = issue_prospective_signal_authority(
        replace(intent, operation_id="other-operation"),
        authority_id=authority.authority_id,
        issued_at=authority.issued_at,
        expires_at=authority.expires_at,
        nonce=authority.nonce,
        issuer_ref=authority.issuer_ref,
    )
    with pytest.raises(ValueError, match="differs from reference"):
        asyncio.run(
            verify_prospective_signal_authority(
                reference, _ProspectiveAuthorityPort(changed_authority), current_time=15.0
            )
        )
    with pytest.raises(ValueError, match="not yet valid"):
        asyncio.run(
            verify_prospective_signal_authority(
                reference, _ProspectiveAuthorityPort(authority), current_time=9.0
            )
        )
    with pytest.raises(ValueError, match="expired"):
        asyncio.run(
            verify_prospective_signal_authority(
                reference, _ProspectiveAuthorityPort(authority), current_time=20.0
            )
        )
    for decoder, wire in (
        (ProspectiveSignalIntent.from_json, intent.to_json()),
        (ProspectiveSignalAuthority.from_json, authority.to_json()),
        (ProspectiveSignalAuthorityRef.from_json, reference.to_json()),
    ):
        unknown = deepcopy(wire)
        unknown["unknown"] = "rejected"
        with pytest.raises(ValueError, match="fields differ"):
            decoder(unknown)
        legacy = deepcopy(wire)
        legacy["schema_version"] = 0
        with pytest.raises(ValueError, match="unsupported"):
            decoder(legacy)


def test_lifecycle_authority_contracts_are_public() -> None:
    names = (
        "PROCEDURE_APPLICABILITY_FINGERPRINT_VERSION",
        "PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION",
        "PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION",
        "ProcedureObservationAuthority",
        "ProcedureObservationAuthorityPort",
        "ProcedureObservationAuthorityRef",
        "ProcedureObservationIntent",
        "ProspectiveSignalAuthority",
        "ProspectiveSignalAuthorityPort",
        "ProspectiveSignalAuthorityRef",
        "ProspectiveSignalIntent",
        "issue_procedure_observation_authority",
        "issue_prospective_signal_authority",
        "verify_procedure_observation_authority",
        "verify_prospective_signal_authority",
    )
    for name in names:
        assert getattr(simple_harness, name) is getattr(runtime, name)
        assert name in simple_harness.__all__
        assert name in runtime.__all__
