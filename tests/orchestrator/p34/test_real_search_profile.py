# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""P34 production-equivalent profile wiring; no credentials or Provider calls."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from test_real_search_value import (
    DOCS_BUDGET,
    MODEL,
    _admission_identity,
    _official_runtime_options,
    _preflight_budget_profile,
    _search_runtime_config,
)

from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.deepseek_tokens import TOKENIZER_SHA256


class NoCallProvider:
    async def invoke(self, request, *, cancel):
        raise AssertionError("pure profile checks must never call a Provider")


def _options(tmp_path, *, base_url="https://api.deepseek.com", model=MODEL, path=None,
             budget_profile="original-v2"):
    config = _search_runtime_config(tmp_path / "evidence", budget_profile)
    return _official_runtime_options(
        config, NoCallProvider(), base_url=base_url, model=model, tokenizer_path=path,
        budget_profile=budget_profile,
    )


def test_official_profile_requires_explicit_absolute_tokenizer(tmp_path):
    with pytest.raises(ValueError, match="absolute SH_TOKENIZER_PATH"):
        _options(tmp_path, path=None)
    with pytest.raises(ValueError, match="absolute SH_TOKENIZER_PATH"):
        _options(tmp_path, path="relative/tokenizer.json")
    with pytest.raises(ValueError, match="file is missing"):
        _options(tmp_path, path=tmp_path / "missing-tokenizer.json")
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("base_url", [
    "https://api.deepseek.com.evil.example",
    "http://api.deepseek.com",
    "https://user:secret@api.deepseek.com",
])
def test_official_profile_rejects_other_endpoint_without_echoing_credentials(
    tmp_path, base_url,
):
    with pytest.raises(ValueError, match="official HTTPS DeepSeek endpoint") as error:
        _options(tmp_path, base_url=base_url)
    assert "secret" not in str(error.value)
    assert not (tmp_path / "evidence").exists()


def test_official_profile_rejects_other_model_before_loading_tokenizer(tmp_path):
    with pytest.raises(ValueError, match="deepseek-flash"):
        _options(tmp_path, model="other-model")
    assert not (tmp_path / "evidence").exists()


def test_official_profile_rejects_wrong_tokenizer_hash_before_runtime_start(tmp_path):
    wrong = tmp_path / "tokenizer.json"
    wrong.write_bytes(b"not the pinned tokenizer")
    with pytest.raises(ValueError, match="tokenizer SHA-256"):
        _options(tmp_path, path=wrong)
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("budget_profile,input_tokens,output_ceiling", [
    ("original-v2", 32768, 8192),
    ("context256-8m-out32k-v7", 262144, 32768),
])
def test_official_profile_uses_one_pinned_counter_for_context_and_admission(
    tmp_path, budget_profile, input_tokens, output_ceiling,
):
    raw_path = os.environ.get("SH_TOKENIZER_PATH")
    if not raw_path:
        pytest.skip("optional pinned tokenizer not supplied for pure profile check")
    path = Path(raw_path)
    runtime = _options(tmp_path, path=path, budget_profile=budget_profile)
    counter = runtime["provider_token_estimator"]
    profile = next(iter(runtime["profiles"].values()))
    assert profile.tokenizer is counter
    assert profile.context_policy is not None
    assert profile.context_policy.max_input_tokens == input_tokens
    assert profile.context_policy.max_tool_result_tokens == 16384
    config = _search_runtime_config(tmp_path / "fresh", budget_profile)
    assert config.default_max_output_tokens == 8192
    assert config.max_output_tokens_ceiling == output_ceiling
    if budget_profile == "context256-8m-out32k-v7":
        assert profile.context_policy.render_slack_tokens == 0
        assert profile.context_policy.output_reserve == 32768
        assert profile.profile_id == "deepseek-context-256k-v1"
        assert profile.default_max_output_tokens == 8192
        assert profile.max_output_tokens_ceiling == 32768
        legacy = _options(tmp_path / "legacy", path=path)
        legacy_profile = legacy["profiles"]["default"]
        assert legacy_profile.context_snapshot()["fingerprint"] != (
            profile.context_snapshot()["fingerprint"]
        )
        assert legacy["provider_token_estimator"].fingerprint == counter.fingerprint
    assert counter.requires_prior_output_reserve is True
    identity = _admission_identity(profile, counter, grants=0)
    assert identity["tokenizer_sha256"] == TOKENIZER_SHA256
    assert identity["runtime_context_tokenizer_fingerprint"] == counter.fingerprint
    assert identity["estimator_fingerprint"] == counter.fingerprint
    assert identity["provider_grants"] == 0  # construction alone is not a paid-path claim
    assert not (tmp_path / "evidence").exists()


def test_search_output_ceiling_fits_fixed_task_verification_reserve(tmp_path):
    # Regression from paid pair v2: 32K context + 32K output required 65536,
    # but 240K Task - 120K synthesis - 60K Worker leaves only 60K.
    # Match the Host 8K output ceiling; never add Task or Mission budget.
    config = _search_runtime_config(tmp_path / "evidence")
    defaults = OrchestratorConfig(evidence_root=tmp_path / "defaults")
    assert config.max_output_tokens_ceiling == defaults.max_output_tokens_ceiling
    assert config.default_max_output_tokens <= config.max_output_tokens_ceiling
    remaining = DOCS_BUDGET.max_tokens - 120_000 - config.attempt_reserve_tokens
    assert 32768 + 32768 > remaining
    assert 32768 + config.max_output_tokens_ceiling < remaining
    assert DOCS_BUDGET.max_tokens == 240_000


def test_real_pair_budget_preflight_rejects_historical_overcommit_before_provider_use():
    assert _preflight_budget_profile("audit400-docs480-s240-v6") == 1_920_000
    with pytest.raises(ValueError, match=r"audit480-docs480-s240-v5.*2,080,000.*2,000,000"):
        _preflight_budget_profile("audit480-docs480-s240-v5")


def test_context256_static_preflight_counts_fragment_and_system_headroom(tmp_path):
    profile = "context256-8m-out32k-v7"
    config = _search_runtime_config(tmp_path / "p34-pure-preflight", profile)
    assert (config.default_max_output_tokens, config.max_output_tokens_ceiling) == (
        8192, 32768,
    )
    assert _preflight_budget_profile(profile) == (
        1_400_000 + 1_400_000 + 1_600_000 + 1_600_000 + 1_200_000
    ) == 7_200_000
    assert 8_000_000 - _preflight_budget_profile(profile) == 800_000


def test_unknown_context_profile_stops_pair_before_credentials(monkeypatch):
    import test_real_search_value as real

    monkeypatch.setenv("SH_P34_BUDGET_PROFILE", "context256-unapproved")
    monkeypatch.delenv("SH_APIKEY", raising=False)
    with pytest.raises(ValueError, match="unknown P34 budget profile"):
        real.test_real_first_vs_approved_compare_search_value()


def test_new_pair_binds_actual_context_floor_under_approved_policy(tmp_path):
    from dataclasses import replace

    from test_real_search_value import _approve_fixture_policy, mission_spec

    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    path = os.environ.get("SH_TOKENIZER_PATH")
    if not path:
        pytest.skip("requires pinned tokenizer")
    profile = "context256-8m-out32k-v7"
    config = _search_runtime_config(tmp_path / "actual-pool", profile)
    provider = NoCallProvider()
    options = _official_runtime_options(
        config, provider, base_url="https://api.deepseek.com", model=MODEL,
        tokenizer_path=path, budget_profile=profile,
    )

    async def case():
        async with Orchestrator(config, provider, **options) as orch:
            version = _approve_fixture_policy(orch)
            spec = replace(mission_spec(profile), search_policy_version_id=version)
            mission = await orch.submit_mission(spec)
            assert mission.final_report["runtime_profile_id"] == "deepseek-context-256k-v1"
            floor = orch._task_floor_for_mission(mission.id)
            assert floor.base == floor.critic == 262144 + 32768
            assert orch.store.get_mission(mission.id).budget.max_tokens == 8000000

    asyncio.run(case())


def test_overcommitted_profile_stops_actual_pair_entry_before_credentials(monkeypatch):
    import test_real_search_value as real

    monkeypatch.setenv("SH_P34_BUDGET_PROFILE", "audit480-docs480-s240-v5")
    monkeypatch.delenv("SH_APIKEY", raising=False)
    with pytest.raises(ValueError, match="2,080,000"):
        real.test_real_first_vs_approved_compare_search_value()
