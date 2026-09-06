"""Independent wire/identity oracle; not a real Memory authorization substitute."""

import dataclasses
import hashlib
import json

import pytest

from simple_harness import ProviderContextUseAttemptV1


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def attempt(**changes):
    values = dict(
        authority_scope_ref="host-store-1",
        subject="subject-1",
        run_id="run-1",
        turn_id="turn-1",
        continuation_id=None,
        provider_request_id="provider-turn-1",
        provider_turn_ordinal=1,
        handoff_ordinal=1,
        context_snapshot_id="snapshot-1",
        context_snapshot_revision=1,
        request_fingerprint="a" * 64,
        requested_at=10.0,
        intents=(),
    )
    return ProviderContextUseAttemptV1(**(values | changes))


def test_attempt_namespace_is_complete_and_time_is_not_an_identity_escape():
    original = attempt()
    identity = {
        k: original.to_json()[k]
        for k in (
            "authority_scope_ref",
            "subject",
            "run_id",
            "turn_id",
            "continuation_id",
            "provider_request_id",
            "handoff_ordinal",
        )
    }
    expected = hashlib.sha256(
        canonical({"domain": "simple-harness/provider-memory-attempt/v1", "payload": identity})
    ).hexdigest()
    assert original.provider_attempt_id == expected
    assert attempt(requested_at=11.0).provider_attempt_id == expected
    assert attempt(requested_at=11.0).intent_hash != original.intent_hash
    for key, value in [
        ("subject", "subject-2"),
        ("authority_scope_ref", "store-2"),
        ("run_id", "run-2"),
        ("turn_id", "turn-2"),
        ("continuation_id", "continuation-2"),
        ("provider_request_id", "provider-turn-2"),
        ("handoff_ordinal", 2),
    ]:
        assert attempt(**{key: value}).provider_attempt_id != expected
    assert ProviderContextUseAttemptV1.from_json(original.to_json()) == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("handoff_ordinal", True),
        ("provider_turn_ordinal", 0),
        ("requested_at", float("nan")),
        ("intents", None),
        ("continuation_id", ""),
        ("request_fingerprint", "a" * 63),
    ],
)
def test_invalid_carrier_cannot_be_an_empty_attestation(field, value):
    with pytest.raises((ValueError, TypeError)):
        attempt(**{field: value})


def test_unknown_wire_field_and_mutable_alias_are_rejected():
    value = attempt().to_json()
    with pytest.raises((ValueError, TypeError)):
        ProviderContextUseAttemptV1.from_json(value | {"receipt": "forged"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        attempt().requested_at = 100.0
