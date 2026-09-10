# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Assemble the one BaseAgent runtime the orchestrator drives (D10', D13').

``OrchestratorConfig`` is the deployment binding ORCH §2 demands: explicit
concurrency, explicit pricing mode (``unpriced`` or a real price table with
``pricing_key="consumer"``), and one runtime-wide hard cap that, because every
Attempt is its own Run, acts as the per-Attempt hard limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simple_harness.agents import build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.runtime import AgentRuntime
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.runtime.consumer_adapter import ConsumerRuntimePolicies

from ..artifacts.workspace import WorkspaceManager
from .tool_gateway import TOOL_NAMES, TOOL_SCHEMAS, WorkspaceToolGateway

CONSUMER_PRICING_KEY = "consumer"


@dataclass(frozen=True, slots=True)
class PriceTable:
    """Real prices in micros per million tokens (host-certified, §18.4)."""

    snapshot_id: str
    input_micros_per_million_tokens: int
    output_micros_per_million_tokens: int

    def estimator(self) -> FrozenPriceEstimator:
        return FrozenPriceEstimator(
            snapshot_id=self.snapshot_id,
            pricing_key=CONSUMER_PRICING_KEY,
            input_micros_per_million_tokens=self.input_micros_per_million_tokens,
            output_micros_per_million_tokens=self.output_micros_per_million_tokens,
        )


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    evidence_root: Path
    model: str = "agent-model"
    owner_id: str = "agent-orchestrator"
    max_concurrency: int = 2
    max_concurrent_model_calls: int = 2
    lease_seconds: float = 60.0
    stall_seconds: float = 180.0
    test_timeout_seconds: float = 120.0
    default_max_output_tokens: int = 4096
    price_table: PriceTable | None = None
    hard_cap_micros: int | None = None
    planner_reserve_tokens: int = 4_000
    critic_reserve_tokens: int = 6_000
    attempt_reserve_tokens: int = 20_000
    turn_deadline_seconds: float = 900.0
    max_model_calls_per_turn: int = 24
    max_tool_calls_per_turn: int = 48
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def unpriced(self) -> bool:
        return self.price_table is None

    @property
    def orchestrator_db(self) -> Path:
        return self.evidence_root / "orchestrator.db"

    @property
    def execution_db(self) -> Path:
        return self.evidence_root / "execution.db"

    @property
    def workspaces_root(self) -> Path:
        return self.evidence_root / "workspaces"

    def policies(self) -> ConsumerRuntimePolicies:
        if self.price_table is None:
            if self.hard_cap_micros is not None:
                raise ValueError(
                    "a hard cap needs a price table (unpriced runs cannot enforce money)"
                )
            return ConsumerRuntimePolicies.local_default()
        return ConsumerRuntimePolicies(
            "consumer_supplied",
            False,
            "fail_closed",
            estimator=self.price_table.estimator(),
            budget_policy=BudgetPolicy(
                hard_cap_micros=self.hard_cap_micros, refuse_on_unknown=True
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "owner_id": self.owner_id,
            "max_concurrency": self.max_concurrency,
            "max_concurrent_model_calls": self.max_concurrent_model_calls,
            "lease_seconds": self.lease_seconds,
            "stall_seconds": self.stall_seconds,
            "test_timeout_seconds": self.test_timeout_seconds,
            "pricing": "unpriced_local"
            if self.price_table is None
            else {
                "snapshot_id": self.price_table.snapshot_id,
                "input_micros_per_million_tokens": self.price_table.input_micros_per_million_tokens,
                "output_micros_per_million_tokens": self.price_table.output_micros_per_million_tokens,
                "hard_cap_micros": self.hard_cap_micros,
            },
            "reserves": {
                "planner_tokens": self.planner_reserve_tokens,
                "critic_tokens": self.critic_reserve_tokens,
                "attempt_tokens": self.attempt_reserve_tokens,
            },
        }


@dataclass(frozen=True, slots=True)
class AssembledOrchestratorRuntime:
    runtime: AgentRuntime
    gateway: WorkspaceToolGateway
    workspaces: WorkspaceManager
    config: OrchestratorConfig


def assemble_orchestrator_runtime(
    config: OrchestratorConfig, provider
) -> AssembledOrchestratorRuntime:  # type: ignore[no-untyped-def]
    config.evidence_root.mkdir(parents=True, exist_ok=True)
    workspaces = WorkspaceManager(config.workspaces_root)
    gateway = WorkspaceToolGateway(workspaces, test_timeout=config.test_timeout_seconds)
    ports = AgentRuntimePorts(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(config.execution_db),
        tool_executor=gateway,
        tool_names=TOOL_NAMES,
        tool_schemas=dict(TOOL_SCHEMAS),
        model=config.model,
        owner_id=config.owner_id,
        policies=config.policies(),
        default_max_output_tokens=config.default_max_output_tokens,
        max_concurrent_model_calls=config.max_concurrent_model_calls,
        max_concurrent_tool_calls=config.max_concurrency,
    )
    runtime = build_agent_runtime(ports, owner_scope="agent-orchestrator")
    return AssembledOrchestratorRuntime(
        runtime=runtime, gateway=gateway, workspaces=workspaces, config=config
    )


__all__ = (
    "CONSUMER_PRICING_KEY",
    "AssembledOrchestratorRuntime",
    "OrchestratorConfig",
    "PriceTable",
    "assemble_orchestrator_runtime",
)
