# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""BaseAgent public surface (Slice 1).

Importing this package must stay cheap: only contracts are imported eagerly.
Runtime assembly (``build_agent_runtime``) is resolved lazily on attribute access.
"""

from __future__ import annotations

from typing import Any

from .codec import agent_id_for_run, run_id_for_agent
from .config import AGENT_CONFIG_FIELDS, AgentConfig, AgentLimits, config_hash
from .contracts import (
    AgentBatchIdentityConflict,
    AgentBatchRejected,
    AgentCancelReceipt,
    AgentClosed,
    AgentClosingReceipt,
    AgentDelegationResult,
    AgentError,
    AgentId,
    AgentInputConflict,
    AgentNotFound,
    AgentPendingInputsExhausted,
    AgentTurnId,
    AgentTurnNotFound,
    AgentTurnReceipt,
    AgentTurnResult,
    AgentTurnSnapshot,
    AgentTurnState,
    AgentTurnTimeout,
)

_LAZY_EXPORTS = {
    "AgentRuntime": ".runtime",
    "AgentRuntimePorts": ".ports",
    "BaseAgent": ".base",
    "build_agent_runtime": ".runtime",
}


def __getattr__(name: str) -> Any:
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'simple_harness.agents' has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module, __name__), name)


__all__ = (
    "AGENT_CONFIG_FIELDS",
    "AgentBatchIdentityConflict",
    "AgentBatchRejected",
    "AgentCancelReceipt",
    "AgentClosed",
    "AgentClosingReceipt",
    "AgentConfig",
    "AgentDelegationResult",
    "AgentError",
    "AgentId",
    "AgentInputConflict",
    "AgentLimits",
    "AgentNotFound",
    "AgentPendingInputsExhausted",
    "AgentRuntime",
    "AgentRuntimePorts",
    "AgentTurnId",
    "AgentTurnNotFound",
    "AgentTurnReceipt",
    "AgentTurnResult",
    "AgentTurnSnapshot",
    "AgentTurnState",
    "AgentTurnTimeout",
    "BaseAgent",
    "agent_id_for_run",
    "config_hash",
    "run_id_for_agent",
)
