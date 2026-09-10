# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Ports a caller injects to assemble a BaseAgent runtime (no user Memory anywhere)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from uuid import uuid4

from simple_harness.runtime.consumer_adapter import ConsumerRuntimePolicies
from simple_harness.runtime.ports import (
    AuthorizationPort,
    AuthorizationRequest,
    AuthorizationResult,
    ProviderPort,
    ToolExecutorPort,
)
from simple_harness.runtime.termination import TerminationLimits

from .context.budget import ContextPolicy
from .context.tokenizer import TokenizerPort

DEFAULT_CHILD_INSTRUCTIONS = "你是被委派的工作 Agent。只处理交给你的目标，给出简洁、可核对的结论。"


class AllowAllAuthorization:
    """Explicit opt-in authorization port that allows every tool call (tests / demos)."""

    async def request_authorization(self, request: AuthorizationRequest) -> AuthorizationResult:
        del request
        return AuthorizationResult.allow()


@dataclass(frozen=True, slots=True)
class AgentRuntimePorts:
    provider: ProviderPort
    authorization: AuthorizationPort
    database_path: str
    tool_executor: ToolExecutorPort | None = None
    tool_names: tuple[str, ...] = ()
    tool_schemas: Mapping[str, dict] = field(default_factory=dict)
    model: str = "agent-model"
    owner_id: str = field(default_factory=lambda: f"base-agent-runtime-{uuid4().hex}")
    lease_ttl_seconds: float = 30.0
    policies: ConsumerRuntimePolicies = field(default_factory=ConsumerRuntimePolicies.local_default)
    # Runtime-level lifetime termination totals shared by every Agent in this runtime
    # (a driver carries exactly one frozen policy fingerprint).
    termination_limits: TerminationLimits = field(
        default_factory=lambda: TerminationLimits(
            max_turns=10_000,
            max_tool_calls=20_000,
            max_wall_seconds=365.0 * 86_400.0,
            max_cost_micros=10_000_000_000,
        )
    )
    child_instructions_template: str = DEFAULT_CHILD_INSTRUCTIONS
    # Every BaseAgent request carries max_output_tokens so the budget reservation is
    # an estimated upper bound instead of UNKNOWN (which would refuse the next turn).
    default_max_output_tokens: int = 4096
    # Batch creation caps (BA02/BA04): checked before any write.
    max_agents: int = 1000
    max_batch_size: int = 200
    # Bounded working Context (Slice 3).  Inject the model's real tokenizer; the
    # default is an explicit upper bound whose fingerprint marks its counts.
    context_policy: ContextPolicy = field(default_factory=ContextPolicy)
    tokenizer: TokenizerPort | None = None
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if not callable(getattr(self.provider, "invoke", None)):
            raise TypeError("provider must implement invoke")
        if not callable(getattr(self.authorization, "request_authorization", None)) and not (
            callable(getattr(self.authorization, "prepare", None))
            and callable(getattr(self.authorization, "bind_decision", None))
        ):
            raise TypeError(
                "authorization must implement request_authorization (consumer port) "
                "or prepare/bind_decision (SDK port)"
            )
        if not isinstance(self.database_path, str) or not self.database_path.strip():
            raise TypeError("database_path must be a non-empty string")
        if self.tool_names and self.tool_executor is None:
            raise TypeError("tool_names require a tool_executor")
        if isinstance(self.tool_names, str):
            raise TypeError("tool_names must be a tuple of strings")
        object.__setattr__(self, "tool_names", tuple(self.tool_names))
        if not isinstance(self.policies, ConsumerRuntimePolicies):
            raise TypeError("policies must use ConsumerRuntimePolicies")
        if not isinstance(self.termination_limits, TerminationLimits):
            raise TypeError("termination_limits must use TerminationLimits")
        if not isinstance(self.child_instructions_template, str):
            raise TypeError("child_instructions_template must be a string")
        if (
            isinstance(self.default_max_output_tokens, bool)
            or not isinstance(self.default_max_output_tokens, int)
            or self.default_max_output_tokens < 1
        ):
            raise ValueError("default_max_output_tokens must be a positive integer")
        if not isinstance(self.context_policy, ContextPolicy):
            raise TypeError("context_policy must use ContextPolicy")
        if self.tokenizer is not None and not callable(getattr(self.tokenizer, "count_text", None)):
            raise TypeError("tokenizer must implement count_text and fingerprint")
        for name in ("max_agents", "max_batch_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


__all__ = ("DEFAULT_CHILD_INSTRUCTIONS", "AgentRuntimePorts", "AllowAllAuthorization")
