# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Decisive oracle for FIRST Critic input/output capacity."""

import pytest

from agent_orchestrator.runtime.first_request_budget import (
    INPUT_CAP_PROTOCOL,
    FirstRequestBudget,
    FirstRequestBudgetUnknown,
    FirstRequestInputCapExceeded,
    actual_output_ceiling,
    enforce_final_wire_input_cap,
    first_request_budget,
    frozen_provider_input_cap,
)


def frozen_context(*, max_input_tokens=32_768, max_total_tokens=None):
    policy = {
        "max_input_tokens": max_input_tokens,
        "max_total_tokens": max_total_tokens,
        "output_reserve": 4096,
        "safety_margin": 256,
        "max_tool_result_tokens": 2048,
        "tool_result_preview_chars": 1024,
        "render_slack_tokens": 64,
    }
    return {"schema": 1, "policy": policy, "fingerprint": "context-v1"}


def test_first_critic_floor_uses_frozen_effective_input_cap_and_actual_output_ceiling():
    cap = frozen_provider_input_cap(
        profile_id="critic-astra",
        model="astra",
        runtime_context=frozen_context(),
        estimator_fingerprint="provider-estimator-v2",
    )
    budget = first_request_budget(
        provider_input_cap=cap,
        output_ceiling=8192,
        guard_input_cap_protocol=INPUT_CAP_PROTOCOL,
    )

    assert isinstance(budget, FirstRequestBudget)
    payload = {
        "schema_version": 1,
        "profile_id": "critic-astra",
        "model": "astra",
        "max_input_tokens": 32_768,
        "context_fingerprint": "context-v1",
        "estimator_fingerprint": "provider-estimator-v2",
    }
    assert budget.provider_input_cap.to_json() == payload
    assert type(budget.provider_input_cap).from_json(payload) == budget.provider_input_cap
    assert budget.minimum_tokens == 40_960
    assert 6000 < budget.minimum_tokens


def test_actual_output_ceiling_matches_runtime_assembly_and_never_drops_below_default():
    assert actual_output_ceiling(
        profile_default_max_output_tokens=None,
        profile_max_output_tokens_ceiling=None,
        config_default_max_output_tokens=4096,
        config_max_output_tokens_ceiling=8192,
    ) == 8192
    assert actual_output_ceiling(
        profile_default_max_output_tokens=12_000,
        profile_max_output_tokens_ceiling=8192,
        config_default_max_output_tokens=4096,
        config_max_output_tokens_ceiling=8192,
    ) == 12_000


def test_first_floor_uses_shared_window_budget_not_raw_context_maximum():
    cap = frozen_provider_input_cap(
        profile_id="critic",
        model="astra",
        runtime_context=frozen_context(max_input_tokens=32_768, max_total_tokens=24_000),
        estimator_fingerprint="price-and-count-v1",
    )
    budget = first_request_budget(
        provider_input_cap=cap,
        output_ceiling=8192,
        guard_input_cap_protocol=INPUT_CAP_PROTOCOL,
    )

    assert isinstance(budget, FirstRequestBudget)
    assert budget.provider_input_cap.max_input_tokens == 19_648
    assert budget.minimum_tokens == 27_840


@pytest.mark.parametrize(
    ("runtime_context", "estimator", "protocol", "reason"),
    [
        (None, "estimator", INPUT_CAP_PROTOCOL, "runtime_context_missing"),
        (frozen_context(), None, INPUT_CAP_PROTOCOL, "estimator_fingerprint_missing"),
        (frozen_context(), "estimator", None, "guard_input_cap_protocol_unknown"),
    ],
)
def test_unknown_inputs_remain_explicit_and_do_not_fall_back_to_legacy_tail(
    runtime_context, estimator, protocol, reason
):
    cap = frozen_provider_input_cap(
        profile_id="critic", model="astra", runtime_context=runtime_context,
        estimator_fingerprint=estimator,
    )
    result = first_request_budget(
        provider_input_cap=cap, output_ceiling=8192, guard_input_cap_protocol=protocol
    )

    assert isinstance(result, FirstRequestBudgetUnknown)
    assert result.reason == reason


def test_final_wire_cap_refuses_before_admission_instead_of_trusting_an_estimate():
    cap = frozen_provider_input_cap(
        profile_id="critic", model="astra", runtime_context=frozen_context(),
        estimator_fingerprint="estimator-v1",
    )
    assert not isinstance(cap, FirstRequestBudgetUnknown)

    enforce_final_wire_input_cap(provider_input_cap=cap, actual_input_tokens=32_768)
    with pytest.raises(FirstRequestInputCapExceeded) as raised:
        enforce_final_wire_input_cap(provider_input_cap=cap, actual_input_tokens=32_769)
    assert raised.value.detail["reason_code"] == "final_wire_input_exceeded"
    assert raised.value.detail["max_input_tokens"] == 32_768
