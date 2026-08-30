# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from simple_harness.runtime.disclosure_protocol import (
    DeliveryRecipient,
    DisclosureContext,
    DisclosureGeneration,
    DisclosurePurpose,
    DisclosureReasonCode,
    DisclosureSource,
    DisclosureTrust,
    IntendedAudience,
)
from simple_harness.runtime.evidence_protocol import (
    AnalysisBudget,
    EvidenceRef,
    MemoryAnalysisDeliveryReceipt,
    MemoryAnalysisRequest,
    MemoryAnalysisResult,
    MemoryAnalysisResultEnvelope,
)


def _request(**changes: object) -> MemoryAnalysisRequest:
    disclosure = DisclosureContext(
        run_id="run-1",
        subject="actor-1",
        recipient=DeliveryRecipient.USER_SELF,
        recipient_id="actor-1",
        intended_audience=IntendedAudience.USER_SELF,
        purpose=DisclosurePurpose.PERSONALIZATION,
        source=DisclosureSource.AUTHENTICATED_HOST,
        trust=DisclosureTrust.TRUSTED_AUTHORITY,
        generation=DisclosureGeneration.CURRENT,
        authority_ref="host-decision-1",
        reason_codes=(DisclosureReasonCode.MINIMUM_NECESSARY,),
    )
    values: dict[str, object] = {
        "job_id": "analysis-job-1",
        "run_id": "run-1",
        "subject": "actor-1",
        "ordered_evidence_refs": (EvidenceRef("evidence-1", "a" * 64, 1),),
        "prompt_version": "memory-analysis/v1",
        "result_schema_version": "memory-mutation/v1",
        "policy_version": "memory-policy/v1",
        "provider_id": "provider-1",
        "model_id": "model-1",
        "model_config_hash": "b" * 64,
        "attempt": 1,
        "budget": AnalysisBudget(4096, 1024, 3000, 100_000),
        "disclosure_context": disclosure,
        "idempotency_key": "analysis-idem-1",
    }
    values.update(changes)
    return MemoryAnalysisRequest(**values)  # type: ignore[arg-type]


def _result(
    request: MemoryAnalysisRequest,
    *,
    outcome: str = "no_mutation",
    provider_response_id: str | None = "provider-response-1",
) -> MemoryAnalysisResult:
    return MemoryAnalysisResult(
        request.job_id,
        request.run_id,
        request.request_hash,
        provider_response_id,
        {"outcome": outcome},
        500,
        30,
        200,
        250,
    )


def _receipt(
    request: MemoryAnalysisRequest,
    result: MemoryAnalysisResult,
    *,
    issuer_id: str = "host-analysis-executor-1",
) -> MemoryAnalysisDeliveryReceipt:
    return MemoryAnalysisDeliveryReceipt(
        "delivery-1",
        issuer_id,
        request.run_id,
        request.job_id,
        request.request_hash,
        result.result_hash,
        request.attempt,
        result.provider_response_id,
        "c" * 64,
        12.0,
        "host-delivery-record-1",
        "d" * 64,
    )


def _envelope(
    request: MemoryAnalysisRequest,
    *,
    provider_response_id: str | None = "provider-response-1",
) -> MemoryAnalysisResultEnvelope:
    result = _result(request, provider_response_id=provider_response_id)
    return MemoryAnalysisResultEnvelope(result, _receipt(request, result))


class DurableAnalysisHost:
    """A strict consumer fixture using durable lookup, not public hash trust."""

    issuer_id = "host-analysis-executor-1"

    def __init__(self, provider_response_id: str | None = "provider-response-1") -> None:
        self._deliveries: dict[tuple[str, int], MemoryAnalysisResultEnvelope] = {}
        self._provider_response_id = provider_response_id

    async def analyze_memory(self, request: MemoryAnalysisRequest) -> MemoryAnalysisResultEnvelope:
        key = (request.request_hash, request.attempt)
        existing = self._deliveries.get(key)
        if existing is not None:
            return existing
        envelope = _envelope(request, provider_response_id=self._provider_response_id)
        self._deliveries[key] = envelope
        return envelope

    async def verify_analysis_delivery(
        self, request: MemoryAnalysisRequest, envelope: MemoryAnalysisResultEnvelope
    ) -> None:
        envelope.verify_request(request)
        if envelope.delivery_receipt.issuer_id != self.issuer_id:
            raise ValueError("analysis delivery issuer differs")
        durable = self._deliveries.get((request.request_hash, request.attempt))
        if durable != envelope:
            raise ValueError("durable Host analysis delivery differs")


def test_delivery_roundtrip_exact_schema_and_validation_receipt_is_distinct() -> None:
    request = _request()
    envelope = _envelope(request)
    envelope.verify_request(request)
    assert MemoryAnalysisResultEnvelope.from_json(envelope.to_json()) == envelope
    payload = envelope.delivery_receipt.to_json()
    payload["validator_version"] = "model-claimed-validator"
    with pytest.raises(ValueError, match="extra"):
        MemoryAnalysisDeliveryReceipt.from_json(payload)


def test_same_request_replays_exact_durable_delivery_after_consumer_crash() -> None:
    request = _request()
    host = DurableAnalysisHost()
    before_crash = asyncio.run(host.analyze_memory(request))
    # Consumer crashes before applying the result and reopens with the same request.
    reopened_request = MemoryAnalysisRequest.from_json(request.to_json())
    after_reopen = asyncio.run(host.analyze_memory(reopened_request))
    assert after_reopen == before_crash
    assert after_reopen.envelope_hash == before_crash.envelope_hash
    asyncio.run(host.verify_analysis_delivery(request, after_reopen))


def test_divergent_or_forged_delivery_for_same_request_is_rejected() -> None:
    request = _request()
    host = DurableAnalysisHost()
    durable = asyncio.run(host.analyze_memory(request))

    divergent_result = _result(request, outcome="append_memory")
    divergent = MemoryAnalysisResultEnvelope(
        divergent_result,
        _receipt(request, divergent_result),
    )
    with pytest.raises(ValueError, match="durable Host analysis delivery differs"):
        asyncio.run(host.verify_analysis_delivery(request, divergent))

    forged_receipt = replace(
        durable.delivery_receipt,
        provider_response_hash="e" * 64,
        host_receipt_id="model-forged-record",
        host_receipt_hash="f" * 64,
    )
    forged = MemoryAnalysisResultEnvelope(durable.result, forged_receipt)
    with pytest.raises(ValueError, match="durable Host analysis delivery differs"):
        asyncio.run(host.verify_analysis_delivery(request, forged))


def test_wrong_issuer_attempt_request_result_and_provider_ref_are_rejected() -> None:
    request = _request()
    host = DurableAnalysisHost()
    durable = asyncio.run(host.analyze_memory(request))

    wrong_issuer = MemoryAnalysisResultEnvelope(
        durable.result,
        replace(durable.delivery_receipt, issuer_id="model-claimed-host"),
    )
    with pytest.raises(ValueError, match="issuer differs"):
        asyncio.run(host.verify_analysis_delivery(request, wrong_issuer))

    wrong_attempt = MemoryAnalysisResultEnvelope(
        durable.result,
        replace(durable.delivery_receipt, attempt=2),
    )
    with pytest.raises(ValueError, match="differs from request"):
        asyncio.run(host.verify_analysis_delivery(request, wrong_attempt))

    with pytest.raises(ValueError, match="differs from result"):
        MemoryAnalysisResultEnvelope(
            durable.result,
            replace(durable.delivery_receipt, result_hash="0" * 64),
        )
    with pytest.raises(ValueError, match="differs from result"):
        MemoryAnalysisResultEnvelope(
            durable.result,
            replace(durable.delivery_receipt, provider_response_id="provider-response-2"),
        )

    other_request = _request(job_id="analysis-job-2", idempotency_key="analysis-idem-2")
    with pytest.raises(ValueError, match="differs from request"):
        asyncio.run(host.verify_analysis_delivery(other_request, durable))


def test_missing_provider_response_id_still_has_durable_host_record_and_replays() -> None:
    request = _request()
    host = DurableAnalysisHost(provider_response_id=None)
    before_crash = asyncio.run(host.analyze_memory(request))
    reopened_request = MemoryAnalysisRequest.from_json(request.to_json())
    after_reopen = asyncio.run(host.analyze_memory(reopened_request))
    asyncio.run(host.verify_analysis_delivery(request, after_reopen))
    assert after_reopen == before_crash
    assert after_reopen.delivery_receipt.provider_response_id is None
    assert after_reopen.delivery_receipt.host_receipt_id == "host-delivery-record-1"
    assert MemoryAnalysisResultEnvelope.from_json(after_reopen.to_json()) == after_reopen
