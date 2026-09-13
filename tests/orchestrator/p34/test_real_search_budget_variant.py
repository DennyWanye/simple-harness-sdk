"""Separate approved budget experiment; no Provider or production changes."""

from copy import deepcopy

import pytest
from test_real_search_value import native_ui_materials

from agent_orchestrator.contracts import Budget


def test_legacy_contract_is_byte_equivalent_to_the_archived_paid_pair():
    assert native_ui_materials()["contract_hash"] == (
        "b482cc0452e1855e62854025d92dc99bf9b5228072735ea956654dd7d48738e3"
    )


def test_new_experiment_changes_only_declared_sub_budgets_and_identity():
    original = native_ui_materials()
    revised = native_ui_materials("docs480-s240-v3")
    spec = revised["mission_spec"]
    assert spec["budget"]["max_tokens"] == 2_000_000
    assert revised["docs_budget"]["max_tokens"] == 480_000
    assert spec["synthesis"]["budget"] == {"max_tokens": 240_000, "max_attempts": 2}
    assert revised["contract_hash"] != original["contract_hash"]
    assert revised["scenario"] != original["scenario"]
    assert spec["idempotency_key"] != original["mission_spec"]["idempotency_key"]
    # Compare the whole exported contract, not just a few selected fields.
    normalized = deepcopy(revised)
    normalized["scenario"] = original["scenario"]
    normalized["contract_hash"] = original["contract_hash"]
    normalized["docs_budget"]["max_tokens"] = 240_000
    normalized["mission_spec"]["idempotency_key"] = original["mission_spec"]["idempotency_key"]
    normalized["mission_spec"]["synthesis"]["budget"]["max_tokens"] = 120_000
    normalized["mission_spec"]["goal"] = spec["goal"].replace(
        "预算480000 tokens/3 attempts", "预算240000 tokens/3 attempts",
    )
    assert normalized == original
    assert native_ui_materials() == original  # no mutable global contract drift


def test_unknown_budget_profile_is_rejected_before_any_provider_configuration():
    with pytest.raises(ValueError, match="unknown P34 budget profile"):
        native_ui_materials("unapproved")


def test_audit_headroom_variant_preserves_the_preceding_experiment():
    preceding = native_ui_materials("docs480-s240-v3")
    assert preceding["contract_hash"] == (
        "c572b501aa4e3624945f53d4014446cd7da8966654bd0950ee3be59f3796fc36"
    )
    revised = native_ui_materials("audit320-docs480-s240-v4")
    assert revised["audit_budget"]["max_tokens"] == 320_000
    assert revised["mission_spec"]["budget"]["max_tokens"] == 2_000_000
    assert revised["contract_hash"] != preceding["contract_hash"]
    normalized = deepcopy(revised)
    for key in ("scenario", "contract_hash"):
        normalized[key] = preceding[key]
    normalized["mission_spec"]["idempotency_key"] = preceding["mission_spec"]["idempotency_key"]
    normalized["audit_budget"]["max_tokens"] = 240_000
    normalized["mission_spec"]["goal"] = revised["mission_spec"]["goal"].replace(
        "预算320000 tokens/4 attempts", "预算240000 tokens/4 attempts",
    )
    assert normalized == preceding
    assert native_ui_materials("docs480-s240-v3") == preceding


def test_audit_two_candidate_headroom_preserves_all_other_frozen_inputs():
    preceding = native_ui_materials("audit320-docs480-s240-v4")
    assert preceding["contract_hash"] == (
        "b81272cdbf3620579b38ebffd86aad0ffbd4d58a19871a37aadfc7d7bcb4c2f2"
    )
    revised = native_ui_materials("audit480-docs480-s240-v5")
    assert revised["contract_hash"] == (
        "0ef5bc3c8d3af2ef58a12a8aa148e365015a003001274c8da2956b8ecd19edf3"
    )
    assert revised["audit_budget"]["max_tokens"] == 480_000
    assert revised["mission_spec"]["budget"]["max_tokens"] == 2_000_000
    normalized = deepcopy(revised)
    for key in ("scenario", "contract_hash"):
        normalized[key] = preceding[key]
    normalized["mission_spec"]["idempotency_key"] = preceding["mission_spec"]["idempotency_key"]
    normalized["audit_budget"]["max_tokens"] = 320_000
    normalized["mission_spec"]["goal"] = revised["mission_spec"]["goal"].replace(
        "预算480000 tokens/4 attempts", "预算320000 tokens/4 attempts",
    )
    assert normalized == preceding
    assert native_ui_materials("audit320-docs480-s240-v4") == preceding


def test_audit400_profile_changes_only_the_declared_audit_budget_and_identity():
    preceding = native_ui_materials("audit480-docs480-s240-v5")
    revised = native_ui_materials("audit400-docs480-s240-v6")
    assert revised["audit_budget"] == Budget(max_tokens=400_000, max_attempts=4).to_json()
    assert revised["docs_budget"] == Budget(max_tokens=480_000, max_attempts=3).to_json()
    assert revised["consumer_budget"] == Budget(max_tokens=400_000, max_attempts=4).to_json()
    assert revised["mission_spec"]["budget"] == Budget(
        max_tokens=2_000_000, max_attempts=24,
    ).to_json()
    assert revised["mission_spec"]["synthesis"]["budget"] == {
        "max_tokens": 240_000, "max_attempts": 2,
    }
    normalized = deepcopy(revised)
    for key in ("scenario", "contract_hash"):
        normalized[key] = preceding[key]
    normalized["mission_spec"]["idempotency_key"] = preceding["mission_spec"]["idempotency_key"]
    normalized["audit_budget"]["max_tokens"] = 480_000
    normalized["mission_spec"]["goal"] = revised["mission_spec"]["goal"].replace(
        "预算400000 tokens/4 attempts", "预算480000 tokens/4 attempts", 1,
    )
    assert normalized == preceding
