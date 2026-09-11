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
import json
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
    assert mission["stop_conditions"] == ["verification_passed", "budget_exhausted"]
    assert mission["budget"]["max_tokens"] == 200_000 and mission["budget"]["max_attempts"] == 4


SYNTHESIS = {
    "goal": "把两份笔记合成一份",
    "success_criteria": ["file:SUMMARY.md"],
    "verification_policy": ["format_check", "rule_check", "critic_review"],
    "outputs": ["SUMMARY.md"],
    "budget": {"max_tokens": 20_000, "max_attempts": 2},
}


def test_a_synthesis_template_round_trips(tmp_path):
    def body(orchestrator, control):
        receipt = control.create(_command("k-synth", synthesis=SYNTHESIS))
        return control.snapshot(receipt["mission_id"])["snapshot"]["mission"]["final_report"]

    assert _with(tmp_path, body)["synthesis"] == SYNTHESIS


@pytest.mark.parametrize(
    ("template", "fragment"),
    [
        ({**SYNTHESIS, "allowed_tools": [*TOOLS3, "run_tests"]}, "synthesis.allowed_tools"),
        ({**SYNTHESIS, "surprise": 1}, "synthesis.surprise"),
        ({**SYNTHESIS, "budget": {"max_cost_micros": 5}}, "synthesis.budget.max_cost_micros"),
        ({**SYNTHESIS, "verification_policy": ["formal_check"]}, "undeployed"),
        ({**SYNTHESIS, "verification_policy": ["format_check", "code_test"]}, "code_test"),
        ({**SYNTHESIS, "budget": {"max_tokens": 10**9}}, "exceeds the Mission budget"),
        ({**SYNTHESIS, "goal": " "}, "synthesis.goal"),
        ({**SYNTHESIS, "success_criteria": "file:SUMMARY.md"}, "synthesis.success_criteria"),
    ],
)
def test_a_synthesis_template_meets_the_door(tmp_path, template, fragment):
    """Review round 2 P1-A: the template is no side door past the strict fields."""

    def body(orchestrator, control):
        with pytest.raises(FacadeError) as refused:
            control.create(_command("k-synth-bad", synthesis=template))
        return refused.value, len(orchestrator.store.list_missions())

    error, count = _with(tmp_path, body)
    assert error.code == "invalid_request" and fragment in str(error)
    assert count == 0


@pytest.mark.parametrize("name", ["success_criteria", "stop_conditions", "untrusted_sources"])
def test_a_string_is_not_a_list_of_strings(tmp_path, name):
    """Review round 2 P2-1: a string would split into characters."""

    def body(orchestrator, control):
        with pytest.raises(FacadeError) as refused:
            control.create(_command("k-str", **{name: "file:NOTES.md"}))
        return refused.value.code, len(orchestrator.store.list_missions())

    assert _with(tmp_path, body) == ("invalid_request", 0)


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


def _accounts(tmp_path) -> int:
    with sqlite3.connect(Path(tmp_path) / "evidence" / "orchestrator.db") as db:
        return int(db.execute("SELECT COUNT(*) FROM budget_accounts").fetchone()[0])


def test_a_repeated_create_reserves_nothing_twice(tmp_path):
    def body(orchestrator, control):
        control.create(_command("k-once"))
        before = _accounts(tmp_path)
        control.create(_command("k-once"))
        return before, _accounts(tmp_path)

    before, after = _with(tmp_path, body)
    assert before == after


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


def _needs_human_critic(request):  # type: ignore[no-untyped-def]
    from agent_orchestrator.testing.fixtures import package_of

    criteria = package_of(request).get("mission_success_criteria", [])
    body = {
        "verdict": "PASS",
        "needs_human": True,
        "findings": [],
        "mission_criteria": [{"criterion": c, "met": True, "reason": "scripted"} for c in criteria],
    }
    return "<critic_verdict>" + json.dumps(body, ensure_ascii=False) + "</critic_verdict>"


def _review_provider() -> RoleScriptedProvider:
    return RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([TASK])],
            "worker": [
                ("workspace_write_file", {"path": "NOTES.md", "content": "- 一\n- 二\n- 三\n"}),
                envelope_step(summary="写好了", artifacts=["NOTES.md"], claims=["三个要点"]),
            ],
            "critic": [_needs_human_critic, critic_step(verdict="PASS", criteria_met=True)],
        }
    )


def test_a_foreign_tenant_cannot_decide_take_over_comment_or_read(tmp_path):
    """Review round 2 P1-C: every object kind, and nothing changes."""

    async def body(orchestrator, control):
        mission_id = control.create(_command("k-review"))["mission_id"]
        await orchestrator.run()
        view = control.snapshot(mission_id)["snapshot"]
        request_id = control.approvals(mission_id)[0]["request_id"]
        task_id = view["tasks"][0]["id"]
        artifact_id = view["artifacts"][0]["id"]
        events_before = orchestrator.store.count_events(mission_id)
        stranger = MissionControlV1(
            orchestrator, tenant_id="other", principal=Principal("other:x", "x")
        )
        messages = set()
        for call in (
            lambda: stranger.decide(request_id, "approve"),
            lambda: stranger.decide(request_id, "reject", reason="不要"),
            lambda: stranger.takeover(task_id, "stop", basis="我想停"),
            lambda: stranger.comment(task_id, "看看"),
            lambda: stranger.comment(request_id, "看看"),
            lambda: stranger.artifact_read(artifact_id),
        ):
            with pytest.raises(FacadeError) as refused:
                call()
            assert refused.value.code == "not_found"
            messages.add(str(refused.value))
        return (
            messages,
            stranger.approvals(None),
            control.approvals(mission_id)[0]["state"],
            events_before,
            orchestrator.store.count_events(mission_id),
        )

    messages, foreign_list, state, before, after = _with(tmp_path, body, _review_provider())
    assert len(messages) == 1  # one wording, no id
    assert foreign_list == [] and state == "PENDING" and before == after


def test_cancel_is_idempotent_and_leaves_an_ended_mission_alone(tmp_path):
    async def body(orchestrator, control):
        running = control.create(_command("k-cancel"))["mission_id"]
        first = control.cancel(running)
        second = control.cancel(running)
        done = control.create(_command("k-done"))["mission_id"]
        orchestrator_provider_note = None
        return first, second, done, orchestrator_provider_note

    first, second, _done, _ = _with(tmp_path, body)
    assert first == {**first, "status": "CANCELLED", "changed": True}
    assert second["status"] == "CANCELLED" and second["changed"] is False


def test_a_completed_mission_is_not_cancelled(tmp_path):
    async def body(orchestrator, control):
        mission_id = control.create(_command("k-completed"))["mission_id"]
        await orchestrator.run()
        return control.cancel(mission_id)

    result = _with(tmp_path, body)
    assert result["status"] == "COMPLETED" and result["changed"] is False


# ------------------------------------------------------------------ SB-4
def test_the_snapshot_cursor_comes_from_the_same_read(tmp_path, monkeypatch):
    """Review round 2 P1-C: an event committed by another connection *between* the
    snapshot's SELECTs is not counted in its ``through_seq`` (it would be without the
    read view)."""

    def body(orchestrator, control):
        mission_id = control.create(_command("k-race"))["mission_id"]
        store = orchestrator.store
        original = store.list_tasks
        path = Path(tmp_path) / "evidence" / "orchestrator.db"
        inserted: list[int] = []

        def list_tasks_then_write(mid):  # type: ignore[no-untyped-def]
            rows = original(mid)
            if not inserted:
                with sqlite3.connect(path) as other:
                    cursor = other.execute(
                        "INSERT INTO events(event_id, idempotency_key, type, trace_id, mission_id,"
                        " task_id, attempt_id, actor_type, actor_id, payload_json, created_at,"
                        " schema_version) VALUES (?, ?, 'HumanCommentAdded', 'trace', ?, NULL,"
                        " NULL, 'user', 'probe', '{}', 0, 1)",
                        ("event-race-probe", "race-probe", mid),
                    )
                    inserted.append(int(cursor.lastrowid))
            return rows

        monkeypatch.setattr(store, "list_tasks", list_tasks_then_write)
        view = control.snapshot(mission_id)
        return view["through_seq"], inserted[0]

    through, inserted = _with(tmp_path, body)
    assert through < inserted


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
    assert read["truncated"] is False and read["encoding"] == "utf-8"
    assert tampered == "integrity_error" and missing == "not_found" and path_like == "not_found"


def test_a_large_artifact_is_truncated_and_says_so(tmp_path, monkeypatch):
    import agent_orchestrator.api.facade as facade

    monkeypatch.setattr(facade, "MAX_ARTIFACT_BYTES", 5)  # "- " + one 3-byte character

    async def body(orchestrator, control):
        mission_id = control.create(_command("k-large"))["mission_id"]
        await orchestrator.run()
        artifact = control.snapshot(mission_id)["snapshot"]["artifacts"][0]
        return control.artifact_read(artifact["id"])

    read = _with(tmp_path, body)
    assert read["truncated"] is True and read["size_bytes"] > 5
    assert read["content"] == "- 一" and read["encoding"] == "utf-8"
