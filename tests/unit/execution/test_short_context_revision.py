"""Independent nullable-source wire oracle; no synthesized product grant."""
import hashlib
import json
from dataclasses import replace

import pytest
import simple_harness as h


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def fragment(kind=h.ContextFragmentType.SHORT_HORIZON, revision=None):
    payload = {"text": "retained short discussion"}
    payload_hash = hashlib.sha256(canonical(payload)).hexdigest()
    disclosure = h.DisclosureContext("run", "subject", h.DeliveryRecipient.USER_SELF,
        "subject", h.IntendedAudience.USER_SELF, h.DisclosurePurpose.PERSONALIZATION,
        h.DisclosureSource.AUTHENTICATED_HOST, h.DisclosureTrust.TRUSTED_AUTHORITY,
        h.DisclosureGeneration.CURRENT, "host-proof", (h.DisclosureReasonCode.MINIMUM_NECESSARY,))
    binding = h.RecallFragmentAuthorityBindingV1("decision", "d"*64, "result", "e"*64,
        "item", "a"*64, None, None, None, "page", "b"*64, None, None, payload_hash)
    return h.ContextFragmentV2("fragment", "run", "subject", kind, "actual-source", revision,
        payload, payload_hash, 10, len(canonical(payload)), disclosure, (), binding)


def test_short_null_exact_wire_hash_and_intent_roundtrip():
    value = fragment()
    raw = value.to_json()
    assert raw["source_revision"] is None and raw["schema_version"] == 2
    assert b'"source_revision":null' in canonical(raw)
    assert value.fragment_hash == hashlib.sha256(canonical(
        {"domain": "simple-harness/context-fragment/v2", "payload": raw})).hexdigest()
    assert h.ContextFragmentV2.from_json(raw) == value
    intent = h.RecallContextUseIntentV1((value,), ((1, "c"*64),))
    assert h.RecallContextUseIntentV1.from_json(intent.to_json()) == intent


@pytest.mark.parametrize("revision", [1, 0, True, False, -1, "1"])
def test_short_fake_revision_constructor_and_wire_refuse(revision):
    with pytest.raises(ValueError, match="source_revision"):
        fragment(revision=revision)
    with pytest.raises(ValueError, match="source_revision"):
        h.ContextFragmentV2.from_json(fragment().to_json() | {"source_revision": revision})


def test_long_null_and_discriminant_only_forgery_refuse():
    long = fragment(h.ContextFragmentType.RECALLED_MEMORY, 1)
    assert h.ContextFragmentV2.from_json(long.to_json()) == long
    for raw in (
        long.to_json() | {"source_revision": None},
        long.to_json() | {"fragment_type": "short_horizon"},
        fragment().to_json() | {"fragment_type": "recalled_memory"},
    ):
        with pytest.raises((TypeError, ValueError)):
            h.ContextFragmentV2.from_json(raw)
    with pytest.raises((TypeError, ValueError)):
        replace(long, source_revision=None)
