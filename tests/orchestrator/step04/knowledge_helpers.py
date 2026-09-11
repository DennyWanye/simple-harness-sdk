# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Shared drivers for the step-4 unit tests: a two-branch graph (A ‖ B) on a bare
Commit Service, Attempts driven to RUNNING, results with typed claims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_orchestrator.contracts import Artifact, Budget, ClaimProposal, ResultEnvelope
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec, Reservation
from agent_orchestrator.storage.store import Store

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")
HASH_A = "a" * 64
HASH_B = "b" * 64


def spec(key: str = "k-1", **overrides: Any) -> MissionSpec:
    base: dict[str, Any] = dict(
        goal="比较两个实现对同一输入合同的支持程度",
        success_criteria=("pytest:tests/test_comparison.py",),
        tenant_id="tenant-4",
        idempotency_key=key,
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=100_000, max_attempts=6),
        untrusted_sources=("docs/",),
    )
    base.update(overrides)
    return MissionSpec(**base)


def node(key: str, deps: Sequence[str] = (), **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        key=key,
        goal=f"检查 impl_{key.lower()} 的边界行为",
        rationale=f"{key} 服务 Mission 的比较目标",
        dependencies=list(deps),
        success_criteria=[f"pytest:tests/probe/test_impl_{key.lower()}.py"],
        verification_policy=["format_check", "rule_check", "code_test"],
        allowed_tools=list(TOOLS),
        budget={"max_tokens": 20_000, "max_attempts": 3},
    )
    base.update(overrides)
    return base


def two_branch_service(tmp_path, *, key: str = "k-1", nodes=None, **spec_overrides):
    """Mission with the parallel graph A ‖ B committed; returns (service, mission, [tasks])."""

    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(spec(key, **spec_overrides))
    planning = service.begin_planning(mission.id)
    proposal = TaskGraphProposal.from_json({"tasks": nodes or [node("A"), node("B")]})
    tasks, _ = service.commit_task_graph(
        mission.id, proposal, base_version=planning.version, source={"planner": "fixture"}
    )
    return service, service.store.get_mission(mission.id), tasks


def drive_to_running(
    service: CommitService,
    task,
    *,
    owner: str = "orch-1",
    agent: str = "agent-1",
    turn: str = "turn-1",
):
    attempt, intent = service.create_attempt(
        task.id,
        role="worker",
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


def claim(content: str, **fields: Any) -> ClaimProposal:
    return ClaimProposal(content=content, confidence=fields.pop("confidence", 0.8), **fields)


def envelope(
    attempt,
    *,
    claims: Sequence[ClaimProposal],
    artifacts: Sequence[str] = ("tests/probe/test_impl_a.py",),
    evidence: Sequence[str] | None = None,
    used_knowledge: Sequence[str] = (),
    summary: str = "探针完成",
) -> ResultEnvelope:
    return ResultEnvelope(
        id=f"result-{attempt.id}",
        mission_id=attempt.mission_id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary=summary,
        claims=tuple(claims),
        evidence=tuple(artifacts if evidence is None else evidence),
        artifacts=tuple(artifacts),
        proposed_tasks=(),
        used_knowledge=tuple(used_knowledge),
        risks=(),
        cost={},
    )


def artifact(attempt, path: str, content_hash: str = HASH_A, version: int = 1) -> Artifact:
    from agent_orchestrator.contracts import ids

    return Artifact(
        id=ids.artifact_id(attempt.id, path, content_hash),
        mission_id=attempt.mission_id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        type="file",
        path=path,
        version=version,
        content_hash=content_hash,
        size_bytes=10,
        produced_by=attempt.agent_id or attempt.id,
    )


def passed_layers(*targets: str) -> list[Mapping[str, Any]]:
    """What the Verifier Router hands to ``accept_result``: PASS layers with detail."""

    runs = [{"target": target, "passed": True, "stdout": "1 passed"} for target in targets]
    return [
        {"layer": "format_check", "status": "PASS", "summary": "ok", "detail": {}},
        {"layer": "rule_check", "status": "PASS", "summary": "ok", "detail": {}},
        {"layer": "code_test", "status": "PASS", "summary": "ok", "detail": {"runs": runs}},
    ]


def submit(
    service,
    attempt,
    env: ResultEnvelope,
    *,
    artifact_paths=("tests/probe/test_impl_a.py",),
    turn="turn-1",
    hashes=None,
):
    hashes = hashes or {}
    stored = service.record_result(
        attempt.id,
        envelope=env,
        turn_id=turn,
        artifacts=[artifact(attempt, path, hashes.get(path, HASH_A)) for path in artifact_paths],
        usage_refs=(),
    )
    service.start_verification(stored.envelope.id)
    return stored
