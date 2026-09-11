# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""S2 · P3.1 external control facade (SB-1 … SB-5; user's Phase3 plan §3.3–§3.4).

``MissionControlV1(orchestrator, tenant_id=…, principal=…)`` is the one surface a product
(the Host) talks to: strict request fields, persistent create receipts, ownership on every
read and write (a foreign object is ``not_found`` — its existence is not revealed), a
snapshot whose ``through_seq`` comes from the same read as the snapshot, gap-free event
pages and content-addressed artifact reads.

Draft (moved into tests/orchestrator/host_support/ when S2 starts).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent_orchestrator.api.facade import FacadeError, MissionControlV1
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


def _command(key: str, **overrides):
    command = {
        "goal": "写一份 NOTES.md，列出三个要点",
        "success_criteria": ["file:NOTES.md"],
        "idempotency_key": key,
        "budget": {"max_tokens": 200_000, "max_attempts": 4},
    }
    command.update(overrides)
    return command


def _with(tmp_path, body, provider=None):  # type: ignore[no-untyped-def]
    async def run():
        config = OrchestratorConfig(
            evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, deployment_policy=OFF
        )
        async with Orchestrator(config, provider or _provider()) as orchestrator:
            control = MissionControlV1(orchestrator, tenant_id="local", principal=ME)
            result = body(orchestrator, control)
            if asyncio.iscoroutine(result):
                result = await result
            return result

    return asyncio.run(run())


# ------------------------------------------------------------------ SB-1
def test_open_fields_round_trip(tmp_path):
    def body(orchestrator, control):
        receipt = control.create(
            _command(
                "k-fields",
                untrusted_sources=["docs/"],
                conflict_reserve_tokens=12_000,
                workspace_seed={"docs/brief.md": "资料"},
                stop_conditions=["verification_passed", "budget_exhausted"],
            )
        )
        return control.snapshot(receipt["mission_id"])["snapshot"]["mission"]

    mission = _with(tmp_path, body)
    report = mission["final_report"]
    assert report["untrusted_sources"] == ["docs/"]
    assert report["conflict_reserve_tokens"] == 12_000
    assert report["workspace_seed"] == {"docs/brief.md": "资料"}


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"surprise": 1}, "surprise"),
        ({"allowed_tools": list(TOOLS3)}, "allowed_tools"),
        ({"risk_level": "production"}, "risk_level"),
        ({"task_kind": "research"}, "task_kind"),
        ({"budget": {"max_tokens": 1000, "max_cost_micros": 5}}, "budget.max_cost_micros"),
    ],
)
def test_unknown_and_closed_fields_are_refused_by_name(tmp_path, overrides, field):
    def body(orchestrator, control):
        with pytest.raises(FacadeError) as refused:
            control.create(_command("k-closed", **overrides))
        return refused.value, len(orchestrator.store.list_missions())

    error, count = _with(tmp_path, body)
    assert error.code == "invalid_request" and field in str(error)
    assert count == 0


# ------------------------------------------------------------------ SB-2
def test_the_create_receipt_is_persistent_and_a_different_body_conflicts(tmp_path):
    def body(orchestrator, control):
        first = control.create(_command("k-receipt"))
        again = control.create(_command("k-receipt"))
        with pytest.raises(FacadeError) as conflict:
            control.create(_command("k-receipt", goal="另一件事"))
        return first, again, conflict.value.code, len(orchestrator.store.list_missions())

    first, again, code, count = _with(tmp_path, body)
    assert first["created"] is True and again["created"] is False
    assert again["mission_id"] == first["mission_id"] and again["spec_hash"] == first["spec_hash"]
    assert code == "conflict" and count == 1


# ------------------------------------------------------------------ SB-3
def test_a_foreign_tenant_sees_nothing(tmp_path):
    def body(orchestrator, control):
        mission_id = control.create(_command("k-mine"))["mission_id"]
        stranger = MissionControlV1(
            orchestrator, tenant_id="other", principal=Principal("other:x", "x")
        )
        codes = []
        for call in (
            lambda: stranger.snapshot(mission_id),
            lambda: stranger.events(mission_id, after_seq=0),
            lambda: stranger.cancel(mission_id),
            lambda: stranger.comment(mission_id, "hi"),
            lambda: stranger.snapshot("mission-does-not-exist"),
        ):
            with pytest.raises(FacadeError) as refused:
                call()
            codes.append((refused.value.code, str(refused.value)))
        return mission_id, codes

    mission_id, codes = _with(tmp_path, body)
    assert all(code == "not_found" for code, _ in codes)
    # a foreign id and a missing id read the same — existence is not revealed
    assert (
        len(
            {
                message.replace(mission_id, "<id>").replace("mission-does-not-exist", "<id>")
                for _, message in codes
            }
        )
        == 1
    )


# ------------------------------------------------------------------ SB-4
def test_snapshot_cursor_and_event_pages_agree(tmp_path):
    async def body(orchestrator, control):
        mission_id = control.create(_command("k-cursor"))["mission_id"]
        await orchestrator.run()
        view = control.snapshot(mission_id)
        with sqlite3.connect(Path(tmp_path) / "evidence" / "orchestrator.db") as db:
            max_seq = db.execute(
                "SELECT MAX(seq) FROM events WHERE mission_id = ?", (mission_id,)
            ).fetchone()[0]
        pages, after = [], 0
        while True:
            page = control.events(mission_id, after_seq=after, limit=3)
            pages.append(page)
            after = page["through_seq"]
            if not page["has_more"]:
                break
        return view, max_seq, pages

    view, max_seq, pages = _with(tmp_path, body)
    assert view["through_seq"] == max_seq and view["graph_version"] >= 1
    seqs = [e["seq"] for page in pages for e in page["events"]]
    assert seqs == sorted(set(seqs)) and seqs[-1] == max_seq
    assert all(len(page["events"]) <= 3 for page in pages)


def test_event_page_size_is_bounded(tmp_path):
    def body(orchestrator, control):
        mission_id = control.create(_command("k-bound"))["mission_id"]
        with pytest.raises(FacadeError) as refused:
            control.events(mission_id, after_seq=0, limit=10_000)
        return refused.value.code

    assert _with(tmp_path, body) == "invalid_request"


# ------------------------------------------------------------------ SB-5
def test_artifacts_are_read_by_id_and_checked_against_their_hash(tmp_path):
    async def body(orchestrator, control):
        mission_id = control.create(_command("k-artifact"))["mission_id"]
        await orchestrator.run()
        artifact = control.snapshot(mission_id)["snapshot"]["artifacts"][0]
        read = control.artifact_read(artifact["id"])
        Path(artifact["storage_uri"]).write_text("被改过", encoding="utf-8")
        with pytest.raises(FacadeError) as tampered:
            control.artifact_read(artifact["id"])
        with pytest.raises(FacadeError) as missing:
            control.artifact_read("artifact-does-not-exist")
        with pytest.raises(FacadeError) as path_like:
            control.artifact_read("/etc/passwd")
        return artifact, read, tampered.value.code, missing.value.code, path_like.value.code

    artifact, read, tampered, missing, path_like = _with(tmp_path, body)
    assert (
        read["content"] == "- 一\n- 二\n- 三\n" and read["content_hash"] == artifact["content_hash"]
    )
    assert read["truncated"] is False
    assert tampered == "integrity_error" and missing == "not_found" and path_like == "not_found"
