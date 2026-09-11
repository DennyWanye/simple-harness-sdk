# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 slice C · P32-6: every Attempt workspace and every copy the system makes is
registered (plan v3 D4; plan review round 2 P2-4).

What is compared on a rebind is the registered identity — the Attempt and the hash of its
seed, upstream inputs and repair source — never the directory's content, so a Worker's
own changes and a restart never block recovery.  A tree half-made by a crash (CREATING) is
rebuilt; an unregistered tree from before 0.10 is adopted; a different identity is
refused.  Cleanup removes directories of finished Missions after the retention period and
keeps the registry rows and the content-addressed bytes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.artifacts.store import read_verified
from agent_orchestrator.artifacts.versioning import ArtifactConflict
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_proposal_step,
)

TOOLS3 = ("workspace_read_file", "workspace_write_file", "workspace_list")
OFF = DeploymentPolicy(allowed_tools=TOOLS3, local_code_execution=False)
ME = Principal("local-user:me", "我")
TASK = {
    "key": "A",
    "goal": "写 NOTES.md",
    "rationale": "唯一的工作",
    "dependencies": [],
    "success_criteria": ["file:NOTES.md"],
    "verification_policy": ["format_check", "rule_check", "critic_review"],
    "outputs": ["NOTES.md"],
    "allowed_tools": list(TOOLS3),
    "budget": {"max_tokens": 30_000, "max_attempts": 2},
    "priority": 1.0,
}
HEX = set("0123456789abcdef")


def _provider() -> RoleScriptedProvider:
    return RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([TASK])],
            "worker": [
                ("workspace_write_file", {"path": "NOTES.md", "content": "- 一\n- 二\n- 三\n"}),
                envelope_step(summary="写好了", artifacts=["NOTES.md"], claims=["三个要点"]),
            ],
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 3,
        }
    )


def _run(tmp_path, body, **config_overrides):  # type: ignore[no-untyped-def]
    async def run():
        config = OrchestratorConfig(
            evidence_root=Path(tmp_path) / "evidence",
            max_concurrency=1,
            deployment_policy=OFF,
            **config_overrides,
        )
        async with Orchestrator(config, _provider()) as orchestrator:
            control = MissionControlV1(orchestrator, tenant_id="local", principal=ME)
            mission_id = control.create(
                {
                    "goal": "写一份 NOTES.md，列出三个要点",
                    "success_criteria": ["file:NOTES.md"],
                    "idempotency_key": "k-registry",
                    "budget": {"max_tokens": 200_000, "max_attempts": 4},
                    "workspace_seed": {"docs/brief.md": "资料"},
                }
            )["mission_id"]
            await asyncio.wait_for(orchestrator.run(), timeout=60)
            result = body(orchestrator, control, mission_id)
            if asyncio.iscoroutine(result):
                result = await result
            return result

    return asyncio.run(run())


def _attempt_row(orchestrator, mission_id):
    [row] = [r for r in orchestrator.store.list_workspaces(mission_id) if r["kind"] == "attempt"]
    return row


def test_p32_6_every_attempt_and_copy_is_registered(tmp_path):
    def body(orchestrator, control, mission_id):
        assert orchestrator.store.get_mission(mission_id).status.value == "COMPLETED"
        return orchestrator.store.list_workspaces(mission_id)

    rows = _run(tmp_path, body)
    kinds = {row["kind"] for row in rows}
    assert {"attempt", "verify", "judge"} <= kinds, kinds
    attempt = next(row for row in rows if row["kind"] == "attempt")
    assert attempt["state"] == "ACTIVE"
    assert len(attempt["base_snapshot"]) == 64 and set(attempt["base_snapshot"]) <= HEX
    assert attempt["detail"]["writable_outputs"] == ["NOTES.md"]
    assert attempt["detail"]["read_only_inputs"] == []  # no upstream Task
    assert attempt["detail"]["seed"] == ["docs/brief.md"]
    for row in rows:
        assert row["workspace_id"] and row["mission_id"] == attempt["mission_id"]


def test_p32_6_rebinding_a_registered_workspace_reuses_it(tmp_path):
    def body(orchestrator, control, mission_id):
        row = _attempt_row(orchestrator, mission_id)
        attempt = orchestrator.store.get_attempt(row["attempt_id"])
        root = orchestrator.assembled.workspaces.root / row["workspace_id"]
        (root / "worker-change.md").write_text("Worker 自己的改动", encoding="utf-8")
        orchestrator._bind_workspace(attempt)  # what recover() and every dispatch do
        return root, _attempt_row(orchestrator, mission_id)

    root, row = _run(tmp_path, body)
    assert (root / "worker-change.md").read_text(encoding="utf-8") == "Worker 自己的改动"
    assert row["state"] == "ACTIVE"


def test_p32_6_a_half_made_workspace_is_rebuilt(tmp_path):
    def body(orchestrator, control, mission_id):
        row = _attempt_row(orchestrator, mission_id)
        attempt = orchestrator.store.get_attempt(row["attempt_id"])
        root = orchestrator.assembled.workspaces.root / row["workspace_id"]
        orchestrator.store.set_workspace_state(row["workspace_id"], "CREATING")
        (root / "half-made.tmp").write_text("x", encoding="utf-8")
        orchestrator._bind_workspace(attempt)
        return root, _attempt_row(orchestrator, mission_id)

    root, row = _run(tmp_path, body)
    assert not (root / "half-made.tmp").exists()
    assert (root / "docs" / "brief.md").read_text(encoding="utf-8") == "资料"
    assert row["state"] == "ACTIVE"


def test_p32_6_a_different_identity_is_refused(tmp_path):
    def body(orchestrator, control, mission_id):
        row = _attempt_row(orchestrator, mission_id)
        attempt = orchestrator.store.get_attempt(row["attempt_id"])
        with orchestrator.store.transaction() as connection:
            connection.execute(
                "UPDATE workspaces SET base_snapshot = ? WHERE workspace_id = ?",
                ("0" * 64, row["workspace_id"]),
            )
        with pytest.raises(ArtifactConflict) as refused:
            orchestrator._bind_workspace(attempt)
        return str(refused.value)

    assert "workspace_identity_mismatch" in _run(tmp_path, body)


def test_p32_6_an_unregistered_tree_from_before_0_10_is_adopted(tmp_path):
    def body(orchestrator, control, mission_id):
        row = _attempt_row(orchestrator, mission_id)
        attempt = orchestrator.store.get_attempt(row["attempt_id"])
        root = orchestrator.assembled.workspaces.root / row["workspace_id"]
        with orchestrator.store.transaction() as connection:
            connection.execute(
                "DELETE FROM workspaces WHERE workspace_id = ?", (row["workspace_id"],)
            )
        orchestrator._bind_workspace(attempt)
        return root, _attempt_row(orchestrator, mission_id)

    root, row = _run(tmp_path, body)
    assert (root / "NOTES.md").is_file()  # the Worker's tree is kept as it was
    assert row["state"] == "ACTIVE" and row["detail"]["adopted"] is True


def test_p32_6_cleanup_after_retention_keeps_records_and_bytes(tmp_path):
    def body(orchestrator, control, mission_id):
        roots = {
            row["workspace_id"]: orchestrator.assembled.workspaces.root / row["workspace_id"]
            for row in orchestrator.store.list_workspaces(mission_id)
        }
        removed = orchestrator.cleanup_workspaces()
        artifacts = control.snapshot(mission_id)["snapshot"]["artifacts"]
        read = control.artifact_read(artifacts[0]["id"])
        stored = [orchestrator.store.get_artifact(a["id"]) for a in artifacts]
        return roots, removed, orchestrator.store.list_workspaces(mission_id), read, stored

    roots, removed, rows, read, stored = _run(tmp_path, body, workspace_retention_seconds=0)
    assert sorted(removed) == sorted(roots)
    assert all(not path.exists() for path in roots.values())
    assert rows and all(row["state"] == "CLEANED" for row in rows)
    assert read["content"] == "- 一\n- 二\n- 三\n"
    assert all(read_verified(artifact) for artifact in stored)


def test_p32_6_cleanup_waits_for_the_retention_period(tmp_path):
    def body(orchestrator, control, mission_id):
        removed = orchestrator.cleanup_workspaces()
        rows = orchestrator.store.list_workspaces(mission_id)
        roots = [orchestrator.assembled.workspaces.root / row["workspace_id"] for row in rows]
        return removed, rows, roots

    removed, rows, roots = _run(tmp_path, body)  # the default retention is days, not zero
    assert removed == []
    assert all(row["state"] == "ACTIVE" for row in rows)
    assert all(path.is_dir() for path in roots)
