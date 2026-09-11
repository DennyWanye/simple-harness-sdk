# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 8 · slice A (plan D8-1' / D8-2' / D8-3'; S8-02, S8-05): Replay rebuilds facts that
already happened — a pure fold of events compared with the library — never executing,
never writing, and reporting what the record cannot decide instead of guessing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fixtures_provider import RoleScriptedProvider, critic_step, proposal_step

from agent_orchestrator.__main__ import main
from agent_orchestrator.contracts import Budget, MissionStatus
from agent_orchestrator.observability.replay import (
    Projection,
    compare,
    events_from_store,
    formal_from_snapshot,
    library_copy,
    replay_mission,
)
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.store import InjectedCrash, Store
from agent_orchestrator.testing.fixtures import (
    DEMO_BAD,
    DEMO_GOOD,
    DEMO_PROPOSAL,
    DEMO_SEED,
    demo_worker_script,
)


def _demo(tmp_path, scenario):
    evidence = Path(tmp_path) / scenario
    code = main(
        [
            "demo",
            "--scenario",
            scenario,
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(evidence),
            "--idempotency-key",
            f"r-{scenario}",
        ]
    )
    report = json.loads((evidence / "test-report.json").read_text(encoding="utf-8"))
    return code, evidence, report["mission_id"]


def _hashes(directory: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir())
        if p.is_file() and (p.name.startswith("orchestrator.db") or p.name.startswith("execution"))
    }


def _spec(key, **overrides):
    base = dict(
        goal="在隔离工作区实现字符串解析函数 parse_kv，并通过给定测试",
        success_criteria=("pytest:tests/test_parse_kv.py",),
        tenant_id="tenant-8",
        idempotency_key=key,
        allowed_tools=(
            "workspace_read_file",
            "workspace_write_file",
            "workspace_list",
            "run_tests",
        ),
        budget=Budget(max_tokens=300_000, max_attempts=4),
        workspace_seed=DEMO_SEED,
    )
    base.update(overrides)
    return MissionSpec(**base)


def _config(tmp_path, **overrides):
    return OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence",
        max_concurrency=1,
        test_timeout_seconds=60,
        **overrides,
    )


# ------------------------------------------------------------------ S8-02
@pytest.mark.parametrize(
    "scenario", ["static-dag", "knowledge-sharing", "dynamic-dag", "approval-action"]
)
def test_s8_02_replay_rebuilds_the_whole_formal_state_of_every_demo(tmp_path, capsys, scenario):
    code, evidence, mission_id = _demo(tmp_path, scenario)
    capsys.readouterr()
    assert code == 0
    library = evidence / "orchestrator.db"
    before = _hashes(evidence)
    report = replay_mission(mission_id=mission_id, library=library, failures=True)
    assert _hashes(evidence) == before  # nothing was written, not even a -wal / -shm
    comparison = report["comparison"]
    assert comparison["mismatches"] == [], comparison["mismatches"]
    assert comparison["coverage"] == 1.0, comparison["not_covered"][:10]
    assert (
        report["gaps"] == []
        and report["unknown_event_types"] == {}
        and report["missing_events"] == []
    )
    assert report["source"].startswith("library")


def test_s8_02_a_failed_mission_replays_and_its_failure_is_told_in_order(tmp_path):
    proposal = {**DEMO_PROPOSAL, "budget": {"max_tokens": 50_000, "max_attempts": 2}}
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(proposal)],
            "worker": demo_worker_script(DEMO_BAD) + demo_worker_script(DEMO_BAD),
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 2,
        }
    )

    async def case():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec("fail"))
            await orchestrator.run()
            assert orchestrator.store.get_mission(mission.id).status is MissionStatus.FAILED
            return mission.id

    mission_id = asyncio.run(case())
    report = replay_mission(
        mission_id=mission_id,
        library=Path(tmp_path) / "evidence" / "orchestrator.db",
        failures=True,
    )
    assert report["comparison"]["mismatches"] == [] and report["comparison"]["coverage"] == 1.0
    kinds = [line["type"] for line in report["failure_timeline"]]
    assert kinds.count("VerificationFailed") == 2 and kinds[-1] == "MissionFailed"
    assert kinds.index("AttemptCreated") < kinds.index("VerificationFailed")
    failed_layers = [
        line for line in report["failure_timeline"] if line["type"] == "VerificationLayerRecorded"
    ]
    assert failed_layers and all(
        line["detail"]["status"] in {"FAIL", "ERROR"} for line in failed_layers
    )


def test_s8_02_the_same_events_delivered_twice_change_nothing(tmp_path, capsys):
    _code, evidence, mission_id = _demo(tmp_path, "static-dag")
    capsys.readouterr()
    store = Store.open_readonly(library_copy(evidence / "orchestrator.db", Path(tmp_path) / "copy"))
    try:
        events = events_from_store(store, mission_id)
        baseline = formal_from_snapshot(store.snapshot(mission_id))
    finally:
        store.close()
    once = Projection().feed(events)
    twice = Projection().feed(events + list(reversed(events)))
    assert twice.duplicates == len(events) and twice.formal() == once.formal()
    assert compare(twice.formal(), baseline)["consistent"]


def test_s8_02_a_crash_record_replays_to_the_state_at_the_crash_and_then_to_the_end(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(DEMO_PROPOSAL)],
            "worker": demo_worker_script(DEMO_GOOD),
            "critic": [critic_step(verdict="PASS", criteria_met=True)],
        }
    )
    evidence = Path(tmp_path) / "evidence"
    at_crash = Path(tmp_path) / "at-crash"

    async def case():
        async with Orchestrator(
            _config(tmp_path, lease_seconds=0.3), provider, owner="orch-1"
        ) as first:
            mission = await first.submit_mission(_spec("crash"))
            first.arm_fault("after_turn_committed", kind="attempt")
            with pytest.raises(InjectedCrash):
                await first.run()
            library_copy(evidence / "orchestrator.db", at_crash)  # the record as the crash left it
        await asyncio.sleep(0.35)
        async with Orchestrator(
            _config(tmp_path, lease_seconds=0.3), provider, owner="orch-2"
        ) as second:
            await second.run()
            assert second.store.get_mission(mission.id).status is MissionStatus.COMPLETED
        return mission.id

    mission_id = asyncio.run(case())
    prefix = replay_mission(mission_id=mission_id, library=at_crash / "orchestrator.db")
    assert prefix["comparison"]["mismatches"] == [] and prefix["comparison"]["coverage"] == 1.0
    assert prefix["formal_state"]["mission"][mission_id]["status"] == "ACTIVE"
    final = replay_mission(mission_id=mission_id, library=evidence / "orchestrator.db")
    assert final["comparison"]["mismatches"] == [] and final["comparison"]["coverage"] == 1.0
    assert final["formal_state"]["mission"][mission_id]["status"] == "COMPLETED"


def test_s8_02_replay_never_writes_never_calls_out_and_imports_no_runtime(
    tmp_path, capsys, monkeypatch
):
    _code, evidence, mission_id = _demo(tmp_path, "approval-action")
    capsys.readouterr()
    import agent_orchestrator.orchestrator.event_handler as handler
    import agent_orchestrator.verification.deterministic_checks as checks
    from agent_orchestrator.runtime.connectors import TestConfigService

    calls: list[str] = []

    async def no_pytest(*args, **kwargs):
        calls.append("pytest")
        raise AssertionError("replay ran a test")

    def no_connector(self, *args, **kwargs):
        calls.append("connector")
        raise AssertionError("replay called a connector")

    monkeypatch.setattr(handler, "run_pytest", no_pytest)
    monkeypatch.setattr(checks, "run_pytest", no_pytest)
    monkeypatch.setattr(TestConfigService, "execute", no_connector)
    service_state = (evidence / "test-services" / "config.json").read_bytes()
    before = _hashes(evidence)
    modes = {
        p: p.stat().st_mode for p in [evidence, *evidence.iterdir()] if p.is_file() or p == evidence
    }
    try:  # a read-only directory: a writing replay would fail here
        for path in modes:
            path.chmod(stat.S_IREAD | stat.S_IEXEC if path == evidence else stat.S_IREAD)
        report = replay_mission(mission_id=mission_id, library=evidence / "orchestrator.db")
    finally:
        for path, mode in modes.items():
            path.chmod(mode)
    assert report["comparison"]["consistent"] and calls == []
    assert (
        _hashes(evidence) == before
        and (evidence / "test-services" / "config.json").read_bytes() == service_state
    )
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, agent_orchestrator.observability.replay; print([m for m in sys.modules if m.startswith(('agent_orchestrator.runtime', 'simple_harness.providers', 'agent_orchestrator.verification.deterministic_checks', 'agent_orchestrator.orchestrator'))])",
        ],
        capture_output=True,
        text=True,
        env={**os.environ},
        check=True,
    )
    assert probe.stdout.strip() == "[]", probe.stdout


# ------------------------------------------------------------------ S8-05
def test_s8_05_missing_events_are_reported_and_never_filled_from_the_library(tmp_path, capsys):
    _code, evidence, mission_id = _demo(tmp_path, "static-dag")
    capsys.readouterr()
    lines = (evidence / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    completed = [e for e in events if e["type"] == "TaskCompleted"]
    dropped = {
        completed[1]["id"],
        next(
            e["id"]
            for e in events
            if e["type"] == "AttemptCreated" and e["task_id"] == completed[2]["task_id"]
        ),
    }
    partial = Path(tmp_path) / "partial.jsonl"
    partial.write_text(
        "".join(
            line + "\n" for line, e in zip(lines, events, strict=True) if e["id"] not in dropped
        ),
        encoding="utf-8",
    )
    report = replay_mission(
        mission_id=mission_id, library=evidence / "orchestrator.db", events_file=partial
    )
    comparison = report["comparison"]
    assert comparison["coverage"] < 1.0
    task_gap = completed[1]["task_id"]
    assert {"object": "task", "id": task_gap, "field": "status"} in comparison["not_covered"]
    assert {"object": "task", "id": task_gap, "field": "accepted_result_id"} in comparison[
        "not_covered"
    ]
    assert report["formal_state"]["task"][task_gap].get("status") is None  # not the library's value
    rules = {gap["rule"] for gap in report["gaps"]}
    assert {"task_terminal_missing", "attempt_created_missing"} <= rules
    assert {e["id"] for e in report["missing_events"]} == dropped
    assert report["source"].startswith("evidence_file")


def test_s8_05_old_events_without_a_field_leave_it_uncovered(tmp_path, capsys):
    _code, evidence, mission_id = _demo(tmp_path, "approval-action")
    capsys.readouterr()
    events = [
        json.loads(line)
        for line in (evidence / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    for event in events:
        if event["type"] == "ActionSucceeded":
            event["payload"].pop("receipt_hash", None)  # an older build did not record it
    old = Path(tmp_path) / "old.jsonl"
    old.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8"
    )
    report = replay_mission(
        mission_id=mission_id, library=evidence / "orchestrator.db", events_file=old
    )
    uncovered = [n for n in report["comparison"]["not_covered"] if n["object"] == "action"]
    assert [n["field"] for n in uncovered] == ["receipt_hash"] and report["comparison"][
        "mismatches"
    ] == []
    shutil.rmtree(Path(tmp_path) / "approval-action" / "workspaces", ignore_errors=True)


# ------------------------------------------------------------------ step-7 states on record
def _approval_demo(evidence, key, *extra):
    return main(
        [
            "demo",
            "--scenario",
            "approval-action",
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(evidence),
            "--idempotency-key",
            key,
            *extra,
        ]
    )


def test_s8_02_a_rejected_approval_and_a_cancelled_open_action_replay_exactly(tmp_path, capsys):
    rejected = Path(tmp_path) / "rejected"
    assert _approval_demo(rejected, "r-reject", "--pause-for-approval") == 4
    capsys.readouterr()
    assert main(["approval", "list", "--evidence-dir", str(rejected), "--as", "alice"]) == 0
    [request] = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "approval",
                "reject",
                request["request_id"],
                "--evidence-dir",
                str(rejected),
                "--as",
                "alice",
                "--reason",
                "不在窗口",
            ]
        )
        == 0
    )
    assert _approval_demo(rejected, "r-reject") == 1
    capsys.readouterr()
    mission_id = json.loads((rejected / "test-report.json").read_text(encoding="utf-8"))[
        "mission_id"
    ]
    report = replay_mission(mission_id=mission_id, library=rejected / "orchestrator.db")
    assert report["comparison"]["mismatches"] == [] and report["comparison"]["coverage"] == 1.0
    assert report["formal_state"]["approval"][request["request_id"]]["state"] == "REJECTED"

    cancelled = Path(tmp_path) / "cancelled"
    assert _approval_demo(cancelled, "r-cancel", "--pause-for-approval") == 4
    capsys.readouterr()
    mission_id = json.loads((cancelled / "test-report.json").read_text(encoding="utf-8"))[
        "mission_id"
    ]
    assert main(["mission", "cancel", "--evidence-dir", str(cancelled), mission_id]) == 0
    capsys.readouterr()
    report = replay_mission(mission_id=mission_id, library=cancelled / "orchestrator.db")
    assert report["comparison"]["mismatches"] == [] and report["comparison"]["coverage"] == 1.0
    [action] = report["formal_state"]["action"].values()
    assert action["state"] == "CANCELLED"  # decided by ActionCancelled, not by the library


def test_s8_02_a_review_that_waits_and_then_passes_replays_at_both_moments(tmp_path):
    from fixtures_provider import envelope_step, graph_proposal_step

    from agent_orchestrator.governance.permissions import Principal

    tools = ["workspace_read_file", "workspace_write_file", "workspace_list", "run_tests"]
    task = {
        "key": "A",
        "goal": "写报告（A）",
        "rationale": "需要人看过",
        "dependencies": [],
        "success_criteria": ["file:REPORT.md"],
        "verification_policy": ["format_check", "rule_check", "human_review"],
        "outputs": ["REPORT.md"],
        "allowed_tools": tools,
        "budget": {"max_tokens": 30_000, "max_attempts": 2},
        "priority": 1.0,
    }
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([task])],
            "worker": [
                ("workspace_list", {}),
                ("workspace_write_file", {"path": "REPORT.md", "content": "# 报告\n"}),
                envelope_step(summary="写好了", artifacts=["REPORT.md"], claims=["报告已写好"]),
            ],
        }
    )
    library = Path(tmp_path) / "evidence" / "orchestrator.db"

    async def case():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                _spec("review", success_criteria=("file:REPORT.md",), allowed_tools=tuple(tools))
            )
            await orchestrator.run()
            waiting = replay_mission(mission_id=mission.id, library=library)
            [request] = orchestrator.store.list_approvals(mission.id)
            orchestrator.commit.review_result(
                request["request_id"],
                principal=Principal("alice"),
                verdict="pass",
                note="",
                nonce="n-1",
            )
            await orchestrator.run()
            return mission.id, request, waiting

    mission_id, request, waiting = asyncio.run(case())
    assert waiting["comparison"]["mismatches"] == [] and waiting["comparison"]["coverage"] == 1.0
    assert (
        waiting["formal_state"]["result"][request["subject_key"]]["verification_state"]
        == "SUSPENDED"
    )
    done = replay_mission(mission_id=mission_id, library=library)
    assert done["comparison"]["mismatches"] == [] and done["comparison"]["coverage"] == 1.0
    assert done["formal_state"]["mission"][mission_id]["status"] == "COMPLETED"
