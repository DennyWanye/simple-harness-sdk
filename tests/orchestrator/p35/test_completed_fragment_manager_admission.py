# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Completed-fragment authority: real acceptance, SDK request and provider guard.

Preparation stops at the public post-accept crash point. Only negative controls
alter the frozen service input or accepted verification state; no acceptance,
projection receipt, admission exception or provider grant is manufactured.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from graph_helpers7 import node, spec
from test_provider_budget_guard import ActualProvider, Counter, grants

from agent_orchestrator.context.manager_fragments import manager_validated_fragment
from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.commit_service import Reservation, mission_account
from agent_orchestrator.orchestrator.event_handler import InjectedCrash, Orchestrator
from agent_orchestrator.runtime.agent_worker import user_message_json
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    envelope_step,
    graph_proposal_step,
    package_of,
)
from simple_harness.agents import AgentConfig, AgentRuntimePorts, build_agent_runtime
from simple_harness.agents.ports import AllowAllAuthorization
from simple_harness.contracts import RunId


@asynccontextmanager
async def _accepted_fragment(tmp_path):
    steps = {}

    def worker(request):
        package = package_of(request)
        attempt_id = package["attempt"]["attempt_id"]
        index = steps.get(attempt_id, 0)
        steps[attempt_id] = index + 1
        if "fragment_scope" in package:
            path = package["fragment_scope"]["output_path_mapping"]["good.md"]
            if index == 0:
                return "workspace_read_file", {"path": "good.md"}
            if index == 1:
                return "workspace_write_file", {"path": path, "content": "validated F\n"}
        else:
            goal = package["task_contract"]["goal"]
            assert goal in {"origin A", "independent B"}
            path = "good.md" if goal == "origin A" else "b.md"
            if index == 0:
                return "workspace_write_file", {"path": path, "content": "partial A or B\n"}
        return envelope_step(
            summary="material produced", artifacts=[path], claims=["selected file exists"],
        )(request)

    def manager(request):
        package = package_of(request)
        assert "validated_fragment" not in package  # stop before the second Manager
        catalog = package["fragment_validation"]
        artifact = next(
            item for item in catalog["materials"]
            if item["kind"] == "artifact" and item["path"] == "good.md"
        )
        body = {
            "schema_version": 1,
            "base_graph_version": package["graph_version"],
            "proposal": {
                "schema_version": 1,
                "origin": catalog["origin"],
                "criterion_ids": [
                    item["id"] for item in catalog["criteria"] if item["text"] == "file:good.md"
                ],
                "claim_refs": [],
                "material_refs": [{
                    "kind": "artifact", "artifact_id": artifact["artifact_id"],
                    "content_hash": artifact["content_hash"], "byte_start": 0,
                    "byte_end_exclusive": artifact["size_bytes"],
                }],
                "rationale": "independently validate the completed part of A",
            },
        }
        return (
            "<fragment_validation_decision>" + json.dumps(body) + "</fragment_validation_decision>"
        )

    provider = RoleScriptedProvider({
        "planner": [graph_proposal_step([
            node(
                "A", goal="origin A", priority=0.5,
                success_criteria=["file:good.md", "file:missing.md"],
                outputs=["good.md", "missing.md"],
                budget={"max_tokens": 120_000, "max_attempts": 2},
            ),
            node("B", goal="independent B", priority=0.1, tokens=30_000),
            node(
                "C", deps=["A", "B"], goal="consumer C", tokens=30_000,
                success_criteria=["file:good.md", "file:consumer.md"],
                outputs=["good.md", "consumer.md"],
            ),
        ])],
        "worker": [worker] * 32,
        "manager": [manager],
    })
    config = OrchestratorConfig(
        evidence_root=tmp_path / "accepted-fragment", max_concurrency=1,
        candidates_per_task=1, manager_after_failures=1, max_manager_rounds=2,
    )
    async with Orchestrator(config, provider, owner="fragment-preparation") as orch:
        mission = await orch.submit_mission(spec(
            "completed-fragment-admission", success_criteria=("file:consumer.md",),
            budget=Budget(max_tokens=450_000, max_attempts=20),
        ))
        # Same deterministic boundary as the crossbranch recovery test: B is
        # accepted first, F second; no second Manager or consumer has run yet.
        orch.arm_fault("after_task_completed", kind="attempt", skip=1)
        with pytest.raises(InjectedCrash):
            await asyncio.wait_for(orch.run(), 20)
        assert provider.by_role["manager"] == 1
        rows = orch.store.list_fragment_validations(mission.id)
        assert len(rows) == 1
        task = orch.store.get_task(rows[0]["validation_task_id"])
        assert task.status is TaskStatus.COMPLETED
        result = orch.store.get_result(task.accepted_result_id)
        assert (result.verdict, result.verification_state) == ("PASS", "DONE")
        attempt = orch.store.get_attempt(result.envelope.attempt_id)
        assert attempt.status is AttemptStatus.COMPLETED
        assert (attempt.task_id, attempt.mission_id) == (task.id, mission.id)
        summary = manager_validated_fragment(orch.store, orch.commit, task.id)
        assert summary["available"] is True
        assert summary["projection_receipt_id"] == rows[0]["projection_receipt_id"]
        assert summary["validation_result_id"] == result.envelope.id
        assert orch.store.get_mission(mission.id).status is MissionStatus.ACTIVE
        yield orch.commit, mission, task, attempt, summary


def _budget_rows(commit):
    return {
        table: [tuple(row) for row in commit.store.connection.execute(
            f"SELECT * FROM {table} ORDER BY 1"
        )]
        for table in ("budget_accounts", "budget_reservations")
    }


@pytest.mark.parametrize("case", [
    "valid", "missing_summary", "wrong_result", "wrong_receipt",
    "failed_result", "pending_result", "cancelled_mission", "stopped_intent",
])
def test_completed_fragment_manager_actual_admission(tmp_path, case):
    async def exercise():
        async with _accepted_fragment(tmp_path) as (commit, mission, task, attempt, summary):
            guard = ProviderBudgetGuard(
                commit, owner="test-owner", estimator=Counter(100), max_slots=1,
                profile_slots={"agent.general": 1}, price_tables={"agent.general": None},
            )
            provider = ActualProvider()
            ports = AgentRuntimePorts(
                provider=provider, authorization=AllowAllAuthorization(),
                database_path=str(tmp_path / "admission-execution.db"),
                provider_admission=guard,
                default_max_output_tokens=1000, max_output_tokens_ceiling=1000,
            )
            async with build_agent_runtime(ports) as runtime:
                agent = await runtime.create(
                    AgentConfig(
                        name="fragment-manager", instructions="Answer briefly.",
                        model_profile_ref="agent.general",
                    ),
                    creation_key="fragment-manager",
                )
                frozen = dict(summary)
                if case == "wrong_result":
                    frozen["validation_result_id"] = "unrelated-result"
                elif case == "wrong_receipt":
                    frozen["projection_receipt_id"] = "unrelated-projection-receipt"
                trigger = f"validated_fragment:{summary['fragment_id']}:{task.accepted_result_id}"
                subject = f"{mission.id}:manager:{trigger}"
                config = {
                    "agent_config": agent.config.to_json(),
                    "message": user_message_json("request"),
                    "provider_admission_fingerprint": guard.fingerprint,
                    "runtime_profile_id": "agent.general", "task_id": task.id,
                    "attempt_id": attempt.id, "result_id": task.accepted_result_id,
                    "trigger": trigger,
                    **({} if case == "missing_summary" else {"validated_fragment": frozen}),
                }
                intent = commit.create_service_intent(
                    kind="manager", subject_id=subject, mission_id=mission.id,
                    account_id=mission_account(mission.id), creation_key=subject,
                    input_id="fragment-input", input_hash="controlled-request",
                    config=config, reservation=Reservation(4000, 0),
                )
                commit.claim_intent(intent.intent_id, owner="test-owner", lease_seconds=60)
                commit.record_agent_created(
                    intent.intent_id, agent_id=agent.agent_id,
                    expected_turn_id=agent.turn_id_for(intent.input_id),
                )
                if case in {"failed_result", "pending_result"}:
                    # Deliberate post-accept corruption. Keep the original PASS
                    # in the pending case to isolate the DONE check itself.
                    commit.store.set_result_verification(
                        task.accepted_result_id,
                        state="DONE" if case == "failed_result" else "PENDING",
                        verdict="FAIL" if case == "failed_result" else "PASS",
                    )
                elif case == "cancelled_mission":
                    commit.cancel_mission(mission.id)
                elif case == "stopped_intent":
                    commit.settle_intent(intent.intent_id, "FAILED")
                # Cancellation itself may change reservations. The invariant is
                # that the subsequently denied SDK request changes no budget.
                before = _budget_rows(commit)
                assert grants(commit) == []
                result = await agent.ask("request", input_id=intent.input_id, timeout=5)
                records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
                assert len(records) == 1
                if case == "valid":
                    assert str(result.state) == "committed" and provider.calls == 1
                    assert records[0].handoff_attempt == 1
                    rows = grants(commit)
                    assert len(rows) == 1 and rows[0]["state"] == "SETTLED"
                    assert rows[0]["subject_id"] == subject
                else:
                    assert str(result.state) == "failed" and provider.calls == 0
                    assert result.error["source_kind"] == "provider_admission"
                    assert result.error["detail"]["reason_code"] == "authority_rejected"
                    assert not result.error["retryable"]
                    persisted = runtime.uow.read_agent_turn_result(result.turn_id)
                    assert persisted.result_json["error"] == result.error
                    assert records[0].handoff_attempt == 0
                    assert grants(commit) == []
                    assert _budget_rows(commit) == before

    asyncio.run(exercise())
