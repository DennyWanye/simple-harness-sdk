# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""P34 production-equivalent profile wiring; no credentials or Provider calls."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_real_search_value import (
    DOCS_BUDGET,
    MODEL,
    _admission_identity,
    _official_runtime_options,
    _search_runtime_config,
)

from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.deepseek_tokens import TOKENIZER_SHA256


class NoCallProvider:
    async def invoke(self, request, *, cancel):
        raise AssertionError("pure profile checks must never call a Provider")


def _options(tmp_path, *, base_url="https://api.deepseek.com", model=MODEL, path=None):
    config = OrchestratorConfig(evidence_root=tmp_path / "evidence", model=model)
    return _official_runtime_options(
        config, NoCallProvider(), base_url=base_url, model=model, tokenizer_path=path,
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


def test_official_profile_uses_one_pinned_counter_for_context_and_admission(tmp_path):
    raw_path = os.environ.get("SH_TOKENIZER_PATH")
    if not raw_path:
        pytest.skip("optional pinned tokenizer not supplied for pure profile check")
    path = Path(raw_path)
    runtime = _options(tmp_path, path=path)
    counter = runtime["provider_token_estimator"]
    profile = runtime["profiles"]["default"]
    assert profile.tokenizer is counter
    assert profile.context_policy is not None
    assert profile.context_policy.max_input_tokens == 32768
    assert profile.context_policy.max_tool_result_tokens == 16384
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
