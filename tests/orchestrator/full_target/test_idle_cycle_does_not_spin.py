# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-26 (Host 真机): a cycle that did nothing must not report progress.

A progressing cycle does not sleep.  One Mission waiting for its completion
mapping made ``_start_planning`` return without doing anything while the cycle
still counted it as progress: the loop never slept and kept the Host event loop at
100% CPU.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent_orchestrator.orchestrator import event_handler
from agent_orchestrator.orchestrator.event_handler import Orchestrator


def _planner(gate_open: bool, assembly_missing: bool = False):
    calls: list[str] = []
    fake = SimpleNamespace(
        _assembly_missing=lambda mission, at: assembly_missing,
        _planning_start_gate=lambda mission: gate_open,
        commit=SimpleNamespace(begin_planning=lambda mission_id: calls.append("begin")),
        _dispatch_for=lambda mission_id: None,
        _try_planner_intent=lambda mission_id, ordinal: asyncio.sleep(0, calls.append("planner")),
    )
    return fake, calls


def test_a_closed_start_gate_is_not_progress(monkeypatch) -> None:
    monkeypatch.setattr(event_handler, "is_hierarchical", lambda mission: True)
    mission = SimpleNamespace(id="m1")
    fake, calls = _planner(gate_open=False)
    assert asyncio.run(Orchestrator._start_planning(fake, mission)) is False and calls == []
    fake, calls = _planner(gate_open=True, assembly_missing=True)
    assert asyncio.run(Orchestrator._start_planning(fake, mission)) is False and calls == []


def test_an_open_start_gate_starts_planning(monkeypatch) -> None:
    monkeypatch.setattr(event_handler, "is_hierarchical", lambda mission: True)
    fake, calls = _planner(gate_open=True)
    assert asyncio.run(Orchestrator._start_planning(fake, SimpleNamespace(id="m1"))) is True
    assert calls == ["begin", "planner"]
