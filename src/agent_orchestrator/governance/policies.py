# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deployment policy and the permission intersection (§21.1–21.3, theory 13-14,
ORCH-BUILD §8.2 ``runtime/tool_gateway.py, governance/*`` row, plan D6-7).

The tools an Agent may call are the intersection of four sources — the Mission charter,
the Task Contract, the Role template and the deployment policy.  Every source can only
narrow the set; none of them, and no text a model produces, can widen it ("外部内容不能
改变系统权限", §21.3).  The intersection is computed when the Attempt is dispatched and
frozen into the dispatch intent; the Tool Gateway enforces it again on every call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..runtime.tool_gateway import TOOL_NAMES

POLICY_VERSION = "deployment-policy-v1"


@dataclass(frozen=True, slots=True)
class DeploymentPolicy:
    """What this deployment allows at all (the fourth side of the intersection)."""

    allowed_tools: tuple[str, ...] = TOOL_NAMES
    denied_path_prefixes: tuple[str, ...] = ()  # workspace paths no Agent may read or write
    # step 7 (D7-3): real actions — which connectors this deployment enables, the highest
    # level it will run at all, per-operation level overrides (only ever *raise* a level),
    # whether an L3 double approval needs two different people (this build's deployment
    # convention, not an original rule), and how long an approval stays valid
    enabled_connectors: tuple[str, ...] = ()  # D7-3': off unless a deployment enables one
    max_action_level: str = "L3"
    level_overrides: tuple[tuple[str, str], ...] = ()  # (("connector.operation", "L3"), ...)
    l3_distinct_principals: bool = True
    approval_ttl_seconds: float = 24 * 3600.0
    max_action_handoffs_per_mission: int = 8  # D7-5': the hard cap under the Mission budget
    connector_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        unknown = set(self.allowed_tools) - set(TOOL_NAMES)
        if unknown:
            raise ValueError(f"deployment policy names unknown tools: {sorted(unknown)}")

    def to_json(self) -> dict[str, Any]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "denied_path_prefixes": list(self.denied_path_prefixes),
            "enabled_connectors": list(self.enabled_connectors),
            "max_action_level": self.max_action_level,
            "level_overrides": [list(item) for item in self.level_overrides],
            "l3_distinct_principals": self.l3_distinct_principals,
            "approval_ttl_seconds": self.approval_ttl_seconds,
            "max_action_handoffs_per_mission": self.max_action_handoffs_per_mission,
            "connector_timeout_seconds": self.connector_timeout_seconds,
            "version": POLICY_VERSION,
        }


@dataclass(frozen=True, slots=True)
class ActionDecision:
    """What the policy says about one candidate action (plan D7-3)."""

    level: str
    required_approvals: int
    refused: str | None = None  # a reason when the deployment will not run it at all

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "required_approvals": self.required_approvals,
            "refused": self.refused,
        }


def action_decision(deployment: DeploymentPolicy, connector: Any, operation: str) -> ActionDecision:
    """Original §22 levels: the connector's declaration or the deployment override,
    whichever is *higher*; an unknown operation counts as L3.  A connector that is not
    enabled, lacks idempotency / reconciliation for L2+, or sits above the deployment's
    ceiling is refused (ORCH §12.3)."""

    from ..runtime.connectors import level_rank
    from .permissions import required_approvals

    spec = (getattr(connector, "operations", {}) or {}).get(operation)
    level = spec.level if spec is not None else "L3"
    override = dict(deployment.level_overrides).get(
        f"{getattr(connector, 'name', '?')}.{operation}"
    )
    if override is not None and level_rank(override) > level_rank(level):
        level = override
    refused = None
    if connector is None or getattr(connector, "name", None) not in deployment.enabled_connectors:
        refused = "connector_not_enabled"
    elif spec is None:
        refused = "unknown_operation"
    elif level_rank(level) >= level_rank("L2") and not (
        getattr(connector, "supports_idempotency", False)
        and getattr(connector, "supports_reconciliation", False)
    ):
        refused = "connector_without_idempotency_or_reconciliation"
    elif spec.mutates and spec.kind != "state":
        refused = "event_operation_not_supported"  # D7-2': one business action = one state
    elif level_rank(level) > level_rank(deployment.max_action_level):
        refused = f"above_deployment_ceiling:{deployment.max_action_level}"
    return ActionDecision(
        level=level, required_approvals=required_approvals(level), refused=refused
    )


def effective_tools(
    *,
    mission_tools: Sequence[str],
    task_tools: Sequence[str],
    role_tools: Sequence[str],
    deployment: DeploymentPolicy,
) -> tuple[str, ...]:
    """Mission ∩ Task ∩ Role ∩ Deployment, in the Role template's order (plan §6.1)."""

    allowed = set(mission_tools) & set(task_tools) & set(deployment.allowed_tools)
    return tuple(name for name in role_tools if name in allowed)


__all__ = (
    "POLICY_VERSION",
    "ActionDecision",
    "DeploymentPolicy",
    "action_decision",
    "effective_tools",
)
