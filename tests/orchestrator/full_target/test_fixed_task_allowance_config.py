# SPDX-License-Identifier: Apache-2.0
"""2026-09-25 user decision: a deployment can give every leaf a fixed token allowance.

The setting reaches the commit service through the Orchestrator config and is part
of the config record only when set, so every earlier configuration keeps its record.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "step02"))
from test_recovery_matrix import _provider  # noqa: E402

from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig


def test_the_fixed_allowance_is_recorded_only_when_set(tmp_path) -> None:
    assert "task_max_tokens" not in OrchestratorConfig(evidence_root=tmp_path).to_json()
    config = OrchestratorConfig(evidence_root=tmp_path, task_max_tokens=1_000_000)
    assert config.to_json()["task_max_tokens"] == 1_000_000


def test_the_orchestrator_hands_the_allowance_to_the_commit_service(tmp_path) -> None:
    config = OrchestratorConfig(evidence_root=tmp_path / "evidence", max_concurrency=1,
                                task_max_tokens=1_000_000)

    async def case() -> int | None:
        async with Orchestrator(config, _provider(), owner="allowance") as orchestrator:
            return orchestrator.commit._task_max_tokens

    assert asyncio.run(case()) == 1_000_000
