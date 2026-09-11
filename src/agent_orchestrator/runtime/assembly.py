# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

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
from ..contracts import Budget
from .tool_gateway import TOOL_NAMES, TOOL_SCHEMAS, WorkspaceToolGateway

CONSUMER_PRICING_KEY = "consumer"
OWNER_SCOPE = "agent-orchestrator"  # D3-10': one scope shared by every orchestrator instance


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
    candidates_per_task: int = (
        1  # D3-5': explorative candidates per Task (each counts as an attempt)
    )
    max_planning_attempts: int = 2  # D3-2': Planner proposals before planning_failed
    lease_seconds: float = 60.0
    sdk_lease_ttl_seconds: float | None = None  # D3-10': SDK Run lease; default lease_seconds / 2
    stall_seconds: float = 180.0
    test_timeout_seconds: float = 120.0
    default_max_output_tokens: int = 4096
    max_output_tokens_ceiling: int = 8192  # SDK empty-response escalation cap (F-BA-1)
    empty_response_retries: int = 2
    price_table: PriceTable | None = None
    hard_cap_micros: int | None = None
    planner_reserve_tokens: int = 4_000
    critic_reserve_tokens: int = 6_000
    attempt_reserve_tokens: int = 20_000
    turn_deadline_seconds: float = 900.0
    max_model_calls_per_turn: int = 24
    max_tool_calls_per_turn: int = 48
    knowledge_sharing: bool = True  # step 4 (D4-19): the layer's kill switch
    on_retrieval_failure: str = "block"  # step 4 (D4-11'): block | degrade
    max_retrieval_failures: int = 3
    max_knowledge_items: int = 12
    # step 5 (D5-2 / D5-6 / D5-7 / D5-8 / D5-15)
    dynamic_graph: bool = True  # False: no Manager decisions; non-candidate outcomes just retry
    max_graph_depth: int = 6
    max_proposals_per_agent: int = 3
    max_supersede_chain: int = 2
    manager_after_failures: int = 2
    no_progress_limit: int = 2
    max_manager_rounds: int = 4
    manager_reserve_tokens: int = 6_000
    aging_window_seconds: float = 300.0
    # step 6 (D6-1 / D6-8)
    global_budget: Budget | None = None  # §18.2 Global Budget above every Mission; None = uncapped
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.candidates_per_task < 1 or self.max_concurrency < 1:
            raise ValueError("candidates_per_task and max_concurrency must be >= 1")
        if self.max_planning_attempts < 1:
            raise ValueError("max_planning_attempts must be >= 1")
        if self.on_retrieval_failure not in {"block", "degrade"}:
            raise ValueError("on_retrieval_failure must be 'block' or 'degrade'")
        if self.max_retrieval_failures < 1 or self.max_knowledge_items < 1:
            raise ValueError("max_retrieval_failures and max_knowledge_items must be >= 1")
        if self.sdk_lease_ttl_seconds is None:
            object.__setattr__(self, "sdk_lease_ttl_seconds", self.lease_seconds / 2)
        elif self.lease_seconds < 2 * self.sdk_lease_ttl_seconds:
            raise ValueError(
                "lease_seconds must be at least twice sdk_lease_ttl_seconds (D3-10': an "
                "orchestration lease may only be taken over after the SDK Run lease lapsed)"
            )
        # D3-4': the model-call semaphore must admit every open candidate or the
        # waiting turns look stalled
        needed = self.max_concurrency * self.candidates_per_task
        if self.max_concurrent_model_calls < needed:
            object.__setattr__(self, "max_concurrent_model_calls", needed)

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
            "candidates_per_task": self.candidates_per_task,
            "max_planning_attempts": self.max_planning_attempts,
            "lease_seconds": self.lease_seconds,
            "sdk_lease_ttl_seconds": self.sdk_lease_ttl_seconds,
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
            "dynamic_graph": {
                "enabled": self.dynamic_graph,
                "max_graph_depth": self.max_graph_depth,
                "max_proposals_per_agent": self.max_proposals_per_agent,
                "max_supersede_chain": self.max_supersede_chain,
                "manager_after_failures": self.manager_after_failures,
                "no_progress_limit": self.no_progress_limit,
                "max_manager_rounds": self.max_manager_rounds,
                "aging_window_seconds": self.aging_window_seconds,
            },
            "global_budget": None if self.global_budget is None else self.global_budget.to_json(),
            "knowledge": {
                "knowledge_sharing": self.knowledge_sharing,
                "on_retrieval_failure": self.on_retrieval_failure,
                "max_retrieval_failures": self.max_retrieval_failures,
                "max_knowledge_items": self.max_knowledge_items,
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
        lease_ttl_seconds=float(config.sdk_lease_ttl_seconds or 30.0),
        policies=config.policies(),
        default_max_output_tokens=config.default_max_output_tokens,
        max_output_tokens_ceiling=max(
            config.max_output_tokens_ceiling, config.default_max_output_tokens
        ),
        empty_response_retries=config.empty_response_retries,
        max_concurrent_model_calls=config.max_concurrent_model_calls,
        max_concurrent_tool_calls=config.max_concurrency,
    )
    runtime = build_agent_runtime(ports, owner_scope=OWNER_SCOPE)
    return AssembledOrchestratorRuntime(
        runtime=runtime, gateway=gateway, workspaces=workspaces, config=config
    )


__all__ = (
    "CONSUMER_PRICING_KEY",
    "OWNER_SCOPE",
    "AssembledOrchestratorRuntime",
    "OrchestratorConfig",
    "PriceTable",
    "assemble_orchestrator_runtime",
)
