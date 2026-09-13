# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""A bounded failed selection may yield a separately verified fragment, never a PASS."""

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import replace

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step, graph_proposal_step, package_of
from graph_helpers7 import node, spec
from test_candidate_selection_runtime import approve

from agent_orchestrator.contracts import Budget, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.event_handler import InjectedCrash, Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig


@pytest.mark.parametrize("mode", [
    "complete", "invalid", "deadline", "budget", "disabled", "cold_fragment", "cold_retarget",
    "deadline_at_commit",
])
def test_failed_compare_round_preserves_history_and_recovers_only_verified_fragment(
    tmp_path, mode, monkeypatch,
):
    async def exercise():
        recovery = mode in {"complete", "cold_fragment", "cold_retarget"}
        steps, managers, reads = {}, [], []
        mapped = []

        def worker(request):
            package = package_of(request)
            identity = package["attempt"]["attempt_id"]
            index = steps.get(identity, 0)
            steps[identity] = index + 1
            goal = package["task_contract"]["goal"]
            if "fragment_scope" in package:
                path = package["fragment_scope"]["output_path_mapping"]["good.md"]
                if path not in mapped:
                    mapped.append(path)
                commands = [
                    ("workspace_read_file", {"path": "good.md"}),
                    ("workspace_write_file", {"path": path, "content": "checked fragment\n"}),
                ]
                outputs = [path]
            elif goal == "A":
                commands = [("workspace_write_file", {"path": "good.md", "content": "partial\n"})]
                outputs = ["good.md"]  # missing.md is genuinely absent in BOTH candidates.
            elif goal == "B":
                commands = [("workspace_write_file", {"path": "b.md", "content": "independent\n"})]
                outputs = ["b.md"]
            else:
                assert goal == "C"
                assert package["validated_fragment_input"]["validation_result_id"]
                commands = [
                    ("workspace_read_file", {"path": mapped[0]}),
                    ("workspace_read_file", {"path": "b.md"}),
                    ("workspace_write_file", {"path": "good.md", "content": "checked fragment\n"}),
                    ("workspace_write_file", {"path": "complete.md", "content": "F plus B\n"}),
                ]
                outputs = ["good.md", "complete.md"]
            if index < len(commands):
                command = commands[index]
                if command[0] == "workspace_read_file":
                    reads.append((goal, command[1]["path"]))
                return command
            return envelope_step(summary=goal, artifacts=outputs, claims=["file produced"])(request)

        def manager(request):
            package = package_of(request)
            managers.append(package)
            if mode == "invalid":
                return "<graph_change_proposal>{}</graph_change_proposal>"
            if "validated_fragment" in package:
                f = package["validated_fragment"]
                tasks = {t["goal"]: t for t in f["tasks"]}
                change = {"base_graph_version": package["graph_version"], "rationale": "reuse F+B",
                          "operations": [
                              {"op": "retarget_dependencies", "task_id": tasks["C"]["task_id"],
                               "dependencies": [f["validation_task_id"], tasks["B"]["task_id"]]},
                              {"op": "cancel_task", "task_id": tasks["A"]["task_id"],
                               "reason": "retain failures; independently reuse fragment"},
                          ]}
                return "<graph_change_proposal>" + json.dumps(change) + "</graph_change_proposal>"
            catalog = package["fragment_validation"]
            material = next(m for m in catalog["materials"] if m.get("path") == "good.md")
            body = {"schema_version": 1, "base_graph_version": package["graph_version"],
                    "proposal": {"schema_version": 1, "origin": catalog["origin"],
                                 "criterion_ids": [c["id"] for c in catalog["criteria"]
                                                   if c["text"] == "file:good.md"],
                                 "claim_refs": [], "material_refs": [{
                                     "kind": "artifact", "artifact_id": material["artifact_id"],
                                     "content_hash": material["content_hash"], "byte_start": 0,
                                     "byte_end_exclusive": material["size_bytes"],
                                 }], "rationale": "independently verify useful partial file"}}
            return ("<fragment_validation_decision>" + json.dumps(body)
                    + "</fragment_validation_decision>")

        tasks = [
            node("A", goal="A", tokens=100_000, priority=2,
                 success_criteria=["file:good.md", "file:missing.md"],
                 outputs=["good.md", "missing.md"],
                 budget={"max_tokens": 100_000, "max_attempts": 3}),
            node("B", goal="B", tokens=100_000, success_criteria=["file:b.md"], outputs=["b.md"]),
            node("C", goal="C", deps=["A", "B"], tokens=100_000,
                 success_criteria=["file:good.md", "file:complete.md"],
                 outputs=["good.md", "complete.md"]),
        ]
        provider = RoleScriptedProvider({
            "planner": [graph_proposal_step(tasks)], "worker": [worker] * 100,
            "synthesizer": [worker] * 100, "manager": [manager] * 3,
        })
        config = OrchestratorConfig(
            evidence_root=tmp_path, max_concurrency=1, candidates_per_task=2,
            attempt_reserve_tokens=4000, manager_after_failures=1, max_manager_rounds=2,
            dynamic_graph=mode != "disabled",
            manager_reserve_tokens=500_000 if mode == "budget" else 6000,
        )
        async with AsyncExitStack() as libraries:
            orch = await libraries.enter_async_context(Orchestrator(config, provider))
            version = approve(orch)
            if mode == "deadline":
                collect = orch._collect_manager

                async def collect_after_deadline(intent, result):
                    assert str(result.state) == "committed"
                    round_ = orch.commit.selection_round(str(intent.config["task_id"]))
                    assert round_ is not None
                    monkeypatch.setattr(orch.store, "_clock", lambda: round_["deadline_at"] + 1)
                    await collect(intent, result)

                monkeypatch.setattr(orch, "_collect_manager", collect_after_deadline)
            if mode == "deadline_at_commit":
                commit = orch.commit.commit_fragment_validation

                def commit_after_deadline(proposal, **kwargs):
                    round_ = orch.commit.selection_round(proposal.origin["task_id"])
                    assert round_ is not None and orch.store.now < round_["deadline_at"]
                    monkeypatch.setattr(orch.store, "_clock", lambda: round_["deadline_at"] + 1)
                    return commit(proposal, **kwargs)

                monkeypatch.setattr(
                    orch.commit, "commit_fragment_validation", commit_after_deadline,
                )
            mission = await orch.submit_mission(replace(
                spec("selection-fragment", success_criteria=("file:complete.md",),
                     budget=Budget(max_tokens=500_000,
                                   max_attempts=24)),
                search_policy_version_id=version,
            ))
            if mode.startswith("cold_"):
                point = ("after_fragment_commit" if mode == "cold_fragment"
                         else "after_validated_fragment_graph_commit")
                orch.arm_fault(point, kind="manager")
                with pytest.raises(InjectedCrash):
                    await asyncio.wait_for(orch.run(), 20)
                initial = orch
                origin_before = next(t for t in orch.store.list_tasks(mission.id) if t.goal == "A")
                frozen_round = orch.commit.selection_round(origin_before.id)
                manager_calls = provider.by_role["manager"]
                assert manager_calls == (1 if mode == "cold_fragment" else 2)
                await libraries.aclose()
                assert initial._store is None and initial._assembled is None
                orch = await libraries.enter_async_context(Orchestrator(config, provider))
                current_round = orch.commit.selection_round(origin_before.id)
                assert current_round == frozen_round
            await asyncio.wait_for(orch.run(), 20)
            origin = next(t for t in orch.store.list_tasks(mission.id) if t.goal == "A")
            attempts = orch.store.list_attempts(origin.id)
            assert len(attempts) == 2
            assert all(orch.store.find_result_for_attempt(a.id).verdict == "FAIL" for a in attempts)
            assert origin.accepted_result_id is None
            round_ = orch.commit.selection_round(origin.id)
            assert len(round_["attempt_ids"]) == 2
            assert round_["synthesis_attempt_id"] is None
            if mode.startswith("cold_"):
                for key in ("round_id", "deadline_at", "attempt_ids", "decision_id", "policy"):
                    assert round_[key] == frozen_round[key]
            assert len(managers) == (2 if recovery else 0 if mode in {"budget", "disabled"} else 1)
            finished = orch.store.get_mission(mission.id)
            if recovery:
                assert finished.status is MissionStatus.COMPLETED
                assert origin.status is TaskStatus.CANCELLED
                assert len(orch.store.list_fragment_validations(mission.id)) == 1
                assert ("C", mapped[0]) in reads and ("C", "b.md") in reads
                assert len(orch.store.list_graph_changes(mission.id)) == 2
            else:
                assert finished.status is MissionStatus.FAILED
                assert finished.stop_reason == ("budget_exhausted" if mode == "budget"
                                                else "no_progress")
                assert not orch.store.list_fragment_validations(mission.id)
            assert all(orch.commit.ledger.reservation(a.id)["settled_tokens"] > 0 for a in attempts)
            calls, events = provider.calls, len(orch.store.list_events(mission.id))
            await asyncio.wait_for(orch.run(), 5)
            assert provider.calls == calls and len(orch.store.list_events(mission.id)) == events

    asyncio.run(exercise())
