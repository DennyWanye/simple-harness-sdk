# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-support 0.9.8 tests reuse the step-4 knowledge helpers (Commit-Service-level
dispute construction) and share one spy on every way the orchestrator reaches pytest."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_orchestrator.orchestrator import event_handler as event_handler_module
from agent_orchestrator.runtime import tool_gateway
from agent_orchestrator.verification import deterministic_checks

STEP04 = Path(__file__).resolve().parents[1] / "step04"
if str(STEP04) not in sys.path:
    sys.path.insert(0, str(STEP04))


@pytest.fixture(name="pytest_spy")  # a conftest function named pytest_* would be a hook
def spy_on_pytest(monkeypatch):
    """Every way the orchestrator reaches pytest, spied — the real runner still runs, so a
    call would also leave a marker file behind."""

    calls: list[dict] = []
    real = tool_gateway.run_pytest

    async def spy(root, *, path, timeout):
        calls.append({"root": str(root), "path": path})
        return await real(root, path=path, timeout=timeout)

    for module in (tool_gateway, deterministic_checks, event_handler_module):
        monkeypatch.setattr(module, "run_pytest", spy)
    return calls
