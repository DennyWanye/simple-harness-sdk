# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""One-to-one bridge between the public ``AgentId`` and the kernel ``RunId``.

Slice 1 uses the same opaque text for both (BA-v1.0 §9.1); the mapping is kept
behind functions so a later version can change the encoding without touching
callers.
"""

from __future__ import annotations

from simple_harness.contracts import RunId

from .contracts import AgentId


def run_id_for_agent(agent_id: AgentId) -> RunId:
    if not isinstance(agent_id, AgentId):
        raise TypeError("agent_id must use AgentId")
    return RunId(agent_id.value)


def agent_id_for_run(run_id: RunId) -> AgentId:
    if not isinstance(run_id, RunId):
        raise TypeError("run_id must use RunId")
    return AgentId(run_id.value)


__all__ = ("agent_id_for_run", "run_id_for_agent")
