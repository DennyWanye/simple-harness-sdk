# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · S6-04 (D6-6): two Attempts that write the same file work in separate
workspaces, nothing is overwritten, and only the verified version becomes the formal
one (§20.1–20.3); upstream inputs are read-only inside a downstream workspace."""

from __future__ import annotations

import asyncio
import hashlib

from helpers_step06 import config, events_of, only, spec

from agent_orchestrator.contracts import MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.testing.fixtures import (
    RECORDER_ANALYSIS,
    RECORDER_IMPL,
    _recorder_task,
    _write_files_then,
    demo_dynamic_dag_provider,
    graph_change_step,
    recorder_scripts,
)

WRONG = "def parse_line(line):\n    return {}\n"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _impl_task():
    return _recorder_task(
        "A",
        "实现 recorder.py 并通过 tests/test_recorder.py（独立任务）",
        [],
        ["pytest:tests/test_recorder.py"],
        3.0,
        ["recorder.py"],
        policy=["format_check", "rule_check", "code_test"],
    )


def _script(code):
    return _write_files_then(
        {"recorder.py": code},
        test_path="tests/test_recorder.py",
        summary="实现完成",
        claim="tests/test_recorder.py 通过",
    )


def test_s6_04_two_candidates_write_the_same_file_in_their_own_workspaces_and_only_the_verified_one_is_formal(
    tmp_path,
):
    provider = demo_dynamic_dag_provider(
        tasks=[_impl_task()],
        per_attempt={
            "A": [
                _script(WRONG),
                _script(RECORDER_IMPL),
                _script(RECORDER_IMPL),
                _script(RECORDER_IMPL),
            ]
        },
        manager_steps=[graph_change_step([])] * 4,
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, max_concurrency=2, candidates_per_task=2, manager_after_failures=10),
            provider,
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s6-04", success_criteria=("pytest:tests/test_recorder.py",))
            )
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            task = store.list_tasks(mission.id)[0]
            assert task.status is TaskStatus.COMPLETED
            attempts = store.list_attempts(task.id)
            assert len(attempts) >= 2
            root = orchestrator.assembled.workspaces.root
            written = {}
            for attempt in attempts:
                path = root / attempt.id / "recorder.py"
                if path.is_file():
                    written[attempt.id] = path.read_text(encoding="utf-8")
            # separate workspaces, both versions still on disk: nothing was overwritten
            assert WRONG in written.values() and RECORDER_IMPL in written.values()
            assert len({str(root / a) for a in written}) == len(written)
            artifacts = [
                a
                for attempt in attempts
                for a in store.list_artifacts(attempt.id)
                if a.path == "recorder.py"
            ]
            assert {a.content_hash for a in artifacts} >= {_sha(WRONG), _sha(RECORDER_IMPL)}
            assert all(a.workspace == a.attempt_id for a in artifacts)  # §20.2 workspace field
            # the formal version is the verified one only
            formal = [store.get_artifact(a) for a in task.accepted_artifacts]
            formal_recorder = [a for a in formal if a.path == "recorder.py"]
            assert len(formal_recorder) == 1 and formal_recorder[0].content_hash == _sha(
                RECORDER_IMPL
            )
            wrong_ids = {a.id for a in artifacts if a.content_hash == _sha(WRONG)}
            assert not (wrong_ids & set(task.accepted_artifacts))
            # "正式版本只经验证后提交": the formal version passed code_test; the wrong candidate
            # was either verified and refused or superseded as history — never accepted
            completed = [a for a in attempts if a.status.value == "COMPLETED"]
            assert len(completed) == 1
            passed = store.find_result_for_attempt(completed[0].id)
            layers = {v["layer"]: v["status"] for v in store.list_verifications(passed.envelope.id)}
            assert layers["code_test"] == "PASS" and passed.verdict == "PASS"
            wrong_attempts = {a.attempt_id for a in artifacts if a.content_hash == _sha(WRONG)}
            for attempt in attempts:
                if attempt.id in wrong_attempts:
                    assert attempt.status.value in {"RETRY_WAIT", "SUPERSEDED", "CANCELLED"}
                    result = store.find_result_for_attempt(attempt.id)
                    assert result is None or result.verdict != "PASS"
            # the Mission was judged on the integrated tree built from the formal version
            judged = root / f"{mission.id}-judge-{orchestrator.owner}-verify" / "recorder.py"
            assert judged.read_text(encoding="utf-8") == RECORDER_IMPL

    asyncio.run(case())


def test_s6_04_an_upstream_input_is_read_only_in_the_downstream_workspace(tmp_path):
    d_script = [
        ("workspace_write_file", {"path": "analysis.md", "content": "# 改写上游结论\n"})
    ] + recorder_scripts()["D"]
    provider = demo_dynamic_dag_provider(tasks=only("AD"), scripts={"D": d_script})

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s6-04b", success_criteria=("file:DOCS.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            refused = [
                c
                for c in orchestrator.assembled.gateway.calls
                if c["tool"] == "workspace_write_file"
                and c["outcome"] == "rejected:protected_input"
            ]
            assert len(refused) == 1 and refused[0]["stage"] == "policy"
            events = events_of(store, mission.id, "ToolCallRejected")
            assert (
                len(events) == 1
                and events[0].payload["reason"] == "protected_input"
                and events[0].payload["path"] == "analysis.md"
            )
            d_task = next(
                t for t in store.list_tasks(mission.id) if t.goal.startswith("独立文档检查")
            )
            d_attempt = store.list_attempts(d_task.id)[0]
            assert events[0].attempt_id == d_attempt.id
            kept = (
                orchestrator.assembled.workspaces.root / d_attempt.id / "analysis.md"
            ).read_text(encoding="utf-8")
            assert kept == RECORDER_ANALYSIS  # the upstream conclusion was not rewritten
            assert "content" not in events[0].payload  # the audit never carries file contents

    asyncio.run(case())
