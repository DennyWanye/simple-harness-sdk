# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Accounting-only receipts over immutable original Provider invocation records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace
from hashlib import sha256

from simple_harness.contracts import canonical_json
from simple_harness.providers import (
    ProviderAccountingIdentity,
    ProviderAccountingObservation,
    ProviderAccountingState,
    ProviderUsage,
)

from .budget import BudgetCharge, FrozenPriceEstimator
from .provider_invocations import ProviderInvocationRecord, ProviderInvocationState
from .uow import UnitOfWorkConflict

ACCOUNTING_KIND = "provider.accounting_resolved.v1"


def accounting_event_id(record: ProviderInvocationRecord) -> str:
    return f"provider-accounting:{record.invocation_id}:{record.handoff_attempt}"


def needs_accounting(record: ProviderInvocationRecord) -> bool:
    if record.state not in {ProviderInvocationState.SUCCEEDED, ProviderInvocationState.FAILED}:
        return False
    if record.handoff_attempt < 1:
        return False
    usage = record.usage_json
    values = usage.get("usage") if isinstance(usage, Mapping) else None
    # This slice repairs missing usage; correcting existing known counts or
    # an independently disputed price is a different authority operation.
    return values is None


def accounting_payload(
    record: ProviderInvocationRecord, observation: ProviderAccountingObservation
):
    if not isinstance(observation, ProviderAccountingObservation):
        raise TypeError("accounting requires typed observation")
    if observation.identity != ProviderAccountingIdentity.from_record(record):
        raise UnitOfWorkConflict("provider accounting original identity differs")
    if observation.state is not ProviderAccountingState.KNOWN or observation.usage is None:
        raise ValueError("only known accounting can create a receipt")
    if not needs_accounting(record):
        raise UnitOfWorkConflict("provider accounting requires terminal missing usage")
    snapshot = record.estimator_snapshot
    charge = BudgetCharge.unknown()
    if snapshot is not None:
        if not isinstance(snapshot, Mapping):
            raise UnitOfWorkConflict("provider accounting frozen price is invalid")
        fields = dict(snapshot)
        if fields.pop("protocol", None) != "simple-harness-price-estimator-v1":
            raise UnitOfWorkConflict("provider accounting frozen price protocol differs")
        estimator = FrozenPriceEstimator(**fields)
        if estimator.snapshot_digest != record.estimator_digest:
            raise UnitOfWorkConflict("provider accounting frozen price digest differs")
        estimator.bind(record.target)
        charge = estimator.charge_usage(observation.usage)
    payload = dict(
        schema_version=1,
        identity=observation.identity.to_json(),
        evidence_ref=observation.evidence_ref,
        usage=asdict(observation.usage),
        budget=charge.to_json(),
    )
    return {**payload, "receipt_hash": sha256(canonical_json(payload).encode()).hexdigest()}


def effective_accounting(record: ProviderInvocationRecord, payload) -> ProviderInvocationRecord:
    """Validate the receipt against raw identity/price, then expose effective facts.

    No raw row field changes: response, execution state/version, request and
    original price remain exactly as recorded at the original handoff.
    """
    try:
        observation = ProviderAccountingObservation(
            ProviderAccountingState.KNOWN,
            ProviderAccountingIdentity(**payload["identity"]),
            payload["evidence_ref"],
            ProviderUsage(**payload["usage"]),
        )
        expected = accounting_payload(record, observation)
        if canonical_json(expected) != canonical_json(payload):
            raise UnitOfWorkConflict("provider accounting receipt content differs")
    except (KeyError, TypeError, ValueError) as exc:
        raise UnitOfWorkConflict("provider accounting receipt is invalid") from exc
    charge = BudgetCharge.from_json(payload["budget"])
    return replace(
        record,
        usage_json={"usage": payload["usage"], "budget": payload["budget"]},
        budget_charge=charge,
    )


__all__ = (
    "ACCOUNTING_KIND",
    "accounting_event_id",
    "accounting_payload",
    "effective_accounting",
    "needs_accounting",
)
