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

    def __post_init__(self) -> None:
        unknown = set(self.allowed_tools) - set(TOOL_NAMES)
        if unknown:
            raise ValueError(f"deployment policy names unknown tools: {sorted(unknown)}")

    def to_json(self) -> dict[str, Any]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "denied_path_prefixes": list(self.denied_path_prefixes),
            "version": POLICY_VERSION,
        }


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


__all__ = ("POLICY_VERSION", "DeploymentPolicy", "effective_tools")
