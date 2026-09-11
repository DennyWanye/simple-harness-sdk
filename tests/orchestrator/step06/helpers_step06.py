# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Shared builders for the step-6 tests (recorder Mission specs, configs, routing)."""

from __future__ import annotations

from pathlib import Path

from agent_orchestrator.contracts import Budget
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import RECORDER_SEED, RECORDER_SPEC, RECORDER_TASKS


def spec(key, **overrides):
    base = dict(
        goal=RECORDER_SPEC["goal"],
        success_criteria=tuple(RECORDER_SPEC["success_criteria"]),
        tenant_id="tenant-6",
        idempotency_key=key,
        allowed_tools=tuple(RECORDER_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=300_000, max_attempts=16),
        workspace_seed=RECORDER_SEED,
    )
    base.update(overrides)
    return MissionSpec(**base)


def config(tmp_path, **overrides):
    base = dict(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
    )
    base.update(overrides)
    return OrchestratorConfig(**base)


def only(keys):
    return [t for t in RECORDER_TASKS if t["key"] in keys]


def events_of(store, mission_id, *types):
    return [e for e in store.list_events(mission_id) if not types or e.type in types]
