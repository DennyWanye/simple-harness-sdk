# SPDX-License-Identifier: Apache-2.0
"""Canonical deployment identities shared by the H8 factory and executor."""
from __future__ import annotations

from typing import Any

from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of


def runtime_config_identity(config: Any, solver: Any, limits: Any) -> str:
    if not callable(getattr(config, "to_json", None)):
        raise ContractError("H8 runtime config has no canonical projection")
    if not isinstance(getattr(solver, "backend_id", None), str):
        raise ContractError("H8 solver has no backend identity")
    if not callable(getattr(limits, "to_json", None)):
        raise ContractError("H8 solver limits have no canonical projection")
    body = config.to_json()
    body["planning"] = {
        **body["planning"],
        "backend_id": solver.backend_id,
        "limits": limits.to_json(),
    }
    # These execution limits are intentionally explicit because older
    # OrchestratorConfig.to_json projections do not contain all of them.
    body["runtime_limits"] = {
        "default_max_output_tokens": config.default_max_output_tokens,
        "max_output_tokens_ceiling": config.max_output_tokens_ceiling,
        "turn_deadline_seconds": config.turn_deadline_seconds,
        "max_model_calls_per_turn": config.max_model_calls_per_turn,
        "max_tool_calls_per_turn": config.max_tool_calls_per_turn,
        "test_timeout_seconds": config.test_timeout_seconds,
        "physical_slots": config.max_concurrent_model_calls,
    }
    return content_hash_of(body)


def context_profile_identity(
    tokenizer: Any,
    context_policy: Any,
    provider: Any,
    extra_input_reserve_tokens: int,
) -> str:
    if type(extra_input_reserve_tokens) is not int or extra_input_reserve_tokens < 0:
        raise ContractError("H8 extra input reserve must be a nonnegative integer")
    fingerprint = getattr(tokenizer, "fingerprint", None)
    target = getattr(provider, "target", None)
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ContractError("H8 tokenizer has no frozen fingerprint")
    if not callable(getattr(context_policy, "to_json", None)):
        raise ContractError("H8 context policy has no canonical projection")
    if target is None or not all(
        isinstance(getattr(target, field, None), str) and getattr(target, field)
        for field in ("provider_id", "model")
    ):
        raise ContractError("H8 provider has no target identity")
    return content_hash_of({
        "policy": context_policy.to_json(),
        "tokenizer_fingerprint": fingerprint,
        "provider": target.provider_id,
        "model": target.model,
        "provider_adapter": getattr(target, "adapter_key", None),
        "extra_input_reserve_tokens": extra_input_reserve_tokens,
    })


__all__ = ("context_profile_identity", "runtime_config_identity")
