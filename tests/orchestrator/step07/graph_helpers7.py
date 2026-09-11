# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Drivers for the step-5 unit tests: the §7.4 graph A → B → C, A → D on a bare Commit
Service, plus change-proposal builders."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_orchestrator.contracts import Artifact, Budget, ClaimProposal, ResultEnvelope, ids
from agent_orchestrator.graph.changes import TaskGraphChange
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec, Reservation
from agent_orchestrator.storage.store import Store

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")
HASH = "c" * 64


def node(
    key: str, deps: Sequence[str] = (), tokens: int = 20_000, **overrides: Any
) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        key=key,
        goal=f"任务 {key}",
        rationale=f"{key} 服务 Mission 根目标",
        dependencies=list(deps),
        success_criteria=[f"file:{key.lower()}.md"],
        verification_policy=["format_check", "rule_check"],
        allowed_tools=list(TOOLS),
        budget={"max_tokens": tokens, "max_attempts": 3},
        outputs=[f"{key.lower()}.md"],
    )
    base.update(overrides)
    return base


DIAMOND = [node("A"), node("B", ["A"]), node("C", ["B"]), node("D", ["A"])]


def spec(key: str = "g-1", **overrides: Any) -> MissionSpec:
    base: dict[str, Any] = dict(
        goal="实现记录器并验证",
        success_criteria=("file:c.md",),
        tenant_id="tenant-5",
        idempotency_key=key,
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=200_000, max_attempts=12),
    )
    base.update(overrides)
    return MissionSpec(**base)


def graph_service(tmp_path, *, key: str = "g-1", nodes=None, clock=None, **spec_overrides):
    store = (
        Store.open(tmp_path / "orchestrator.db")
        if clock is None
        else Store.open(tmp_path / "orchestrator.db", clock=clock)
    )
    service = CommitService(store)
    mission, _ = service.create_mission(spec(key, **spec_overrides))
    planning = service.begin_planning(mission.id)
    tasks, _ = service.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json({"tasks": nodes or DIAMOND}),
        base_version=planning.version,
        source={"planner": "fixture"},
    )
    return service, service.store.get_mission(mission.id), {t.goal.split()[-1]: t for t in tasks}


def drive_to_running(
    service: CommitService, task, *, owner="orch-1", agent="agent-1", turn="turn-1", role="worker"
):
    attempt, intent = service.create_attempt(
        task.id,
        role=role,
        model="agent-model",
        prompt_version="worker-v2",
        context_version="ctx",
        reservation=Reservation(tokens=4_000, cost_micros=0),
        intent_config={"agent_config": {}, "message": "do"},
        input_hash="h",
    )
    service.claim_intent(intent.intent_id, owner=owner, lease_seconds=60)
    service.record_agent_created(intent.intent_id, agent_id=agent, expected_turn_id=turn)
    service.record_submitted(intent.intent_id, receipt={"turn_id": turn, "seq": 1})
    return service.store.get_attempt(attempt.id)


def complete(service: CommitService, task, *, agent="agent-1", turn="turn-1"):
    """Drive a Task to COMPLETED through a candidate result (file criterion)."""

    attempt = drive_to_running(service, task, agent=agent, turn=turn)
    path = task.outputs[0] if task.outputs else "out.md"
    envelope = ResultEnvelope(
        id=f"result-{attempt.id}",
        mission_id=task.mission_id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="done",
        claims=(ClaimProposal(content="done", confidence=0.9),),
        evidence=(path,),
        artifacts=(path,),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    artifact = Artifact(
        id=ids.artifact_id(attempt.id, path, HASH),
        mission_id=task.mission_id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=path,
        version=1,
        content_hash=HASH,
        size_bytes=3,
        produced_by=agent,
    )
    stored = service.record_result(
        attempt.id, envelope=envelope, turn_id=turn, artifacts=[artifact], usage_refs=()
    )
    service.start_verification(stored.envelope.id)
    return service.accept_result(
        stored.envelope.id, verifier_results=[{"layer": "rule_check", "status": "PASS"}]
    )


def change(
    base: int,
    operations: Sequence[Mapping[str, Any]],
    *,
    basis: Mapping[str, Any] | None = None,
    rationale="按证据调整",
) -> TaskGraphChange:
    return TaskGraphChange.from_json(
        {
            "base_graph_version": base,
            "basis": dict(basis or {"trigger": "test"}),
            "rationale": rationale,
            "operations": list(operations),
        }
    )


def add(key: str, deps: Sequence[str], **overrides: Any) -> dict[str, Any]:
    body = node(key, deps, **overrides)
    body.pop("outputs", None)
    return {"op": "add_task", **body, "outputs": overrides.get("outputs", [f"{key.lower()}.md"])}
