# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""AC: a public Mission consumes an accepted fragment across branches and re-verifies synthesis."""

import asyncio
import json
from pathlib import Path

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step, graph_proposal_step, package_of
from graph_helpers7 import node, spec

from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.commit_service import task_account
from agent_orchestrator.orchestrator.event_handler import InjectedCrash, Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.testing.fixtures import MODEL


class Counter:
    fingerprint = "fragment-fixture-count-v1"
    bound_protocol = "fixture-text-only-v1"
    requires_prior_output_reserve = True

    def estimate_input_tokens(self, request):
        return 1000


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize(
    "case,crash_point",
    [
        ("accepted", None),
        ("accepted", "after_accept"),
        ("accepted", "after_graph"),
        ("retry", None),
        ("partial_coverage", None),
        ("hijack", None),
        ("stale", None),
        ("started", None),
        ("hash_drift", None),
    ],
)
def test_accepted_fragment_retargets_blocked_consumer_and_synthesis_reads_new_result(
    tmp_path, case, crash_point, guarded
):
    reads: list[tuple[str, str]] = []
    manager_packages: list[dict] = []
    steps: dict[str, int] = {}
    mapped_path = ""
    active_orch = []
    first_settlement = {}

    def worker(request):
        nonlocal mapped_path
        package = package_of(request)
        attempt = package["attempt"]["attempt_id"]
        goal = package["task_contract"]["goal"]
        index = steps.get(attempt, 0)
        steps[attempt] = index + 1
        if "fragment_scope" in package:
            mapping = package["fragment_scope"]["output_path_mapping"]
            mapped_path = mapping["good.md"]
            if index == 0:
                reads.append(("F", "good.md"))
                return "workspace_read_file", {"path": "good.md"}
            if case == "partial_coverage" and index == 1:
                return "workspace_read_file", {"path": "extra.md"}
            if case == "partial_coverage" and index == 2:
                return "workspace_write_file", {"path": mapped_path, "content": "validated F\n"}
            if case == "partial_coverage" and index == 3:
                return "workspace_write_file", {
                    "path": mapping["extra.md"],
                    "content": "validated extra F\n",
                }
            if index == 1:
                return "workspace_write_file", {"path": mapped_path, "content": "validated F\n"}
            return envelope_step(
                summary="F independently checked",
                artifacts=[
                    mapped_path,
                    *([mapping["extra.md"]] if case == "partial_coverage" else []),
                ],
                claims=["validated the selected good.md fragment"],
            )(request)
        if goal == "origin A":
            if index == 0:
                return "workspace_write_file", {"path": "good.md", "content": "partial A\n"}
            if case == "partial_coverage" and index == 1:
                return "workspace_write_file", {"path": "extra.md", "content": "partial extra\n"}
            return envelope_step(
                summary="A partial",
                artifacts=["good.md", *(["extra.md"] if case == "partial_coverage" else [])],
                claims=["good.md is partial"],
            )(request)
        if goal == "independent B":
            if index == 0:
                return "workspace_write_file", {"path": "b.md", "content": "branch B\n"}
            return envelope_step(
                summary="B independent", artifacts=["b.md"], claims=["independent B exists"]
            )(request)
        assert goal == "consumer C"
        frozen = package["validated_fragment_input"]
        assert frozen["material_refs"][0]["path"] == mapped_path
        assert frozen["validation_result_id"]
        if index == 0:
            if case == "retry" and attempt.endswith(":attempt-2"):
                first_id = attempt.rsplit(":", 1)[0] + ":attempt-1"
                first_settlement.update(active_orch[-1].commit.ledger.reservation(first_id))
                assert first_settlement["state"] == "SETTLED"
                assert first_settlement["settled_tokens"] > 0
            reads.append(("C", mapped_path))
            return "workspace_read_file", {"path": mapped_path}
        if index == 1:
            tool_messages = [
                json.loads(message.content)
                for message in request.messages
                if str(message.role) == "tool"
            ]
            assert any("validated F" in str(item) for item in tool_messages)
            reads.append(("C", "b.md"))
            return "workspace_read_file", {"path": "b.md"}
        if index == 2:
            tool_messages = [
                json.loads(message.content)
                for message in request.messages
                if str(message.role) == "tool"
            ]
            assert any("branch B" in str(item) for item in tool_messages)
            return "workspace_write_file", {
                "path": "good.md",
                "content": (
                    "C bad first\n"
                    if case == "retry" and attempt.endswith(":attempt-1")
                    else "C used F and B\n"
                ),
            }
        if index == 3:
            return "workspace_write_file", {"path": "consumer.md", "content": "C complete\n"}
        if index == 4:
            probe = (
                "from pathlib import Path\n\n"
                "def test_consumer_used_both_validated_inputs():\n"
                f"    assert Path({mapped_path!r}).read_text() == 'validated F\\n'\n"
                "    assert Path('b.md').read_text() == 'branch B\\n'\n"
                "    assert Path('good.md').read_text() == 'C used F and B\\n'\n"
                "    assert Path('consumer.md').read_text() == 'C complete\\n'\n"
            )
            return "workspace_write_file", {"path": "tests/test_consumer.py", "content": probe}
        if index == 5:
            return "run_tests", {"path": "tests/test_consumer.py"}

        def cite_actual_test(body):
            body["claims"][0]["evidence"] = ["pytest:tests/test_consumer.py"]
            body["evidence"].append("pytest:tests/test_consumer.py")
            return body

        return envelope_step(
            summary="C consumed both branches",
            artifacts=["good.md", "consumer.md", "tests/test_consumer.py"],
            claims=["C incorporated accepted F and independent B"],
            override=cite_actual_test,
        )(request)

    def manager(request):
        package = package_of(request)
        manager_packages.append(package)
        if "validated_fragment" not in package:
            catalog = package["fragment_validation"]
            selected_paths = {"good.md", "extra.md"} if case == "partial_coverage" else {"good.md"}
            selected_artifacts = [
                item
                for item in catalog["materials"]
                if item["kind"] == "artifact" and item["path"] in selected_paths
            ]
            assert {item["path"] for item in selected_artifacts} == selected_paths
            body = {
                "schema_version": 1,
                "base_graph_version": package["graph_version"],
                "proposal": {
                    "schema_version": 1,
                    "origin": catalog["origin"],
                    "criterion_ids": [
                        item["id"]
                        for item in catalog["criteria"]
                        if item["text"] in {f"file:{path}" for path in selected_paths}
                    ],
                    "claim_refs": [],
                    "material_refs": [
                        {
                            "kind": "artifact",
                            "artifact_id": artifact["artifact_id"],
                            "content_hash": artifact["content_hash"],
                            "byte_start": 0,
                            "byte_end_exclusive": artifact["size_bytes"],
                        }
                        for artifact in selected_artifacts
                    ],
                    "rationale": "independently check the partial file",
                },
            }
            return (
                "<fragment_validation_decision>"
                + json.dumps(body)
                + "</fragment_validation_decision>"
            )
        accepted = package["validated_fragment"]
        assert accepted["validation_result_id"]
        assert accepted["material_refs"][0]["path"] == mapped_path
        if case == "hash_drift":
            artifact = active_orch[0].store.get_artifact(
                accepted["material_refs"][0]["artifact_id"]
            )
            cas_path = Path(artifact.storage_uri)
            cas_path.chmod(0o600)  # production CAS is read-only; mutate only this test copy
            try:
                cas_path.write_bytes(b"tampered after F acceptance")
            finally:
                cas_path.chmod(0o444)
        candidates = {row["goal"]: row for row in accepted["tasks"]}
        target = (
            candidates["origin A"]
            if case == "started"
            else candidates["decoy D"]
            if case == "hijack"
            else candidates["consumer C"]
        )
        body = {
            "base_graph_version": package["graph_version"] - (case == "stale"),
            "rationale": "C must wait for independent B and accepted F",
            "operations": [
                {
                    "op": "retarget_dependencies",
                    "task_id": target["task_id"],
                    "dependencies": [
                        candidates["independent B"]["task_id"],
                        accepted["validation_task_id"],
                    ],
                },
                *(
                    []
                    if case in {"started", "hijack"}
                    else [
                        {
                            "op": "cancel_task",
                            "task_id": candidates["origin A"]["task_id"],
                            "reason": "whole A remained incomplete; retain failed evidence",
                        },
                    ]
                ),
            ],
        }
        return "<graph_change_proposal>" + json.dumps(body) + "</graph_change_proposal>"

    def synthesis(request):
        package = package_of(request)
        attempt = package["attempt"]["attempt_id"]
        index = steps.get(attempt, 0)
        steps[attempt] = index + 1
        if index == 0:
            reads.append(("S", "consumer.md"))
            return "workspace_read_file", {"path": "consumer.md"}
        if index == 1:
            tool_messages = [
                json.loads(message.content)
                for message in request.messages
                if str(message.role) == "tool"
            ]
            assert any("C complete" in str(item) for item in tool_messages)
            return "workspace_write_file", {"path": "final.md", "content": "synthesized C\n"}
        verified_consumer = [
            item["id"]
            for item in package["verified_knowledge"]
            if item.get("type") == "test_observation"
            and "tests/test_consumer.py" in item.get("content", "")
        ]
        assert len(verified_consumer) == 1  # scoped execution observation, not free-form entailment

        def cite_consumer(body):
            body["used_knowledge"] = verified_consumer
            return body

        return envelope_step(
            summary="S independently synthesized",
            artifacts=["final.md"],
            claims=["final.md reflects the independently verified consumer"],
            override=cite_consumer,
        )(request)

    tasks = [
        node(
            "A",
            tokens=120_000,
            goal="origin A",
            priority=0.5,
            success_criteria=[
                "file:good.md",
                *(["file:extra.md"] if case == "partial_coverage" else []),
                "file:missing.md",
            ],
            outputs=[
                "good.md",
                *(["extra.md"] if case == "partial_coverage" else []),
                "missing.md",
            ],
            budget={"max_tokens": 120_000, "max_attempts": 2},
        ),
        node(
            "B",
            tokens=30_000,
            goal="independent B",
            priority=0.1,
            success_criteria=["file:b.md"],
            outputs=["b.md"],
        ),
        node(
            "C",
            deps=["A", "B"],
            tokens=30_000,
            goal="consumer C",
            success_criteria=["file:good.md", "file:consumer.md", "pytest:tests/test_consumer.py"],
            verification_policy=["format_check", "rule_check", "code_test"],
            outputs=["good.md", "consumer.md", "tests/test_consumer.py"],
        ),
        *(
            [
                node("X", deps=["A"], goal="origin X", outputs=["x.md"]),
                node(
                    "D",
                    deps=["X", "B"],
                    goal="decoy D",
                    success_criteria=["file:good.md"],
                    outputs=["d.md"],
                ),
            ]
            if case == "hijack"
            else []
        ),
    ]
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step(tasks)],
            "worker": [worker] * 32,
            "manager": [manager] * 4,
            "synthesizer": [synthesis] * 5,
        }
    )
    config = OrchestratorConfig(
        evidence_root=tmp_path / "crossbranch",
        max_concurrency=1,
        candidates_per_task=1,
        manager_after_failures=1,
        max_manager_rounds=2,
        lease_seconds=0.3,
    )
    runtime = (
        {
            "profiles": {"default": RuntimeProfile(
                "default", provider, MODEL,
                default_max_output_tokens=1000, max_output_tokens_ceiling=1000,
            )},
            "provider_token_estimator": Counter(),
        }
        if guarded else {}
    )

    def assert_finished(orch, mission):
        assert len(manager_packages) == 2  # replay does not call Manager again
        rows = orch.store.list_fragment_validations(mission.id)
        assert len(rows) == 1
        receipt = orch.store.get_receipt(rows[0]["projection_receipt_id"])
        validation = orch.store.get_task(receipt["validation_task_id"])
        assert validation.status is TaskStatus.COMPLETED
        assert orch.store.get_result(validation.accepted_result_id).verdict == "PASS"
        tasks_by_goal = {task.goal: task for task in orch.store.list_tasks(mission.id)}
        consumer = tasks_by_goal["consumer C"]
        independent = tasks_by_goal["independent B"]
        assert set(consumer.dependency_ids) == {validation.id, independent.id}
        assert consumer.status is TaskStatus.COMPLETED
        assert orch.store.get_result(consumer.accepted_result_id).verdict == "PASS"
        if case == "retry":
            consumer_attempts = orch.store.list_attempts(consumer.id)
            assert [item.status for item in consumer_attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            first_result = orch.store.find_result_for_attempt(consumer_attempts[0].id)
            assert first_result is not None and first_result.verdict == "FAIL"
            assert consumer_attempts[1].retry_of == consumer_attempts[0].id
            assert first_settlement["settled_tokens"] > 0
            assert orch.commit.ledger.reservation(consumer_attempts[0].id) == first_settlement
            frozen_inputs = [
                orch.store.get_intent_for_subject(item.id).config["validated_fragment_input"]
                for item in consumer_attempts
            ]
            for key in (
                "fragment_id",
                "validation_result_id",
                "projection_receipt_id",
                "material_refs",
                "consumer_dependency_ids",
                "consumer_task_contract_revision",
                "criterion_mapping",
            ):
                assert frozen_inputs[0][key] == frozen_inputs[1][key]
            assert len(orch.store.list_attempts(validation.id)) == 1
            assert reads.count(("C", mapped_path)) == 2
            budget = orch.commit.ledger.account(task_account(consumer.id))
            assert budget.attempts_created == 2
            assert budget.limits.max_tokens == 30_000 and budget.settled_tokens > 0
        final = next(task for task in tasks_by_goal.values() if task.kind == "synthesis")
        assert final.status is TaskStatus.COMPLETED
        assert orch.store.get_result(final.accepted_result_id).verdict == "PASS"
        assert orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED
        assert ("C", mapped_path) in reads and ("C", "b.md") in reads
        assert ("S", "consumer.md") in reads
        assert len(orch.store.list_graph_changes(mission.id)) == 2

    def assert_rejected(orch, mission):
        assert len(manager_packages) == (1 if case == "partial_coverage" else 2)
        assert len(orch.store.list_graph_changes(mission.id)) == 1
        by_goal = {task.goal: task for task in orch.store.list_tasks(mission.id)}
        consumer = by_goal["consumer C"]
        assert not orch.store.list_attempts(consumer.id)
        assert set(consumer.dependency_ids) == {by_goal["origin A"].id, by_goal["independent B"].id}
        if case == "partial_coverage":
            fragment = next(
                task for task in by_goal.values() if "fragment_validation" in task.context
            )
            assert fragment.status is TaskStatus.COMPLETED
            assert len(orch.store.list_attempts(fragment.id)) == 1
        if case == "hijack":
            decoy = by_goal["decoy D"]
            assert "file:good.md" in decoy.success_criteria
            assert set(decoy.dependency_ids) == {
                by_goal["origin X"].id,
                by_goal["independent B"].id,
            }
            assert not orch.store.list_attempts(decoy.id)
            assert any(
                event.type == "ManagementDecided"
                and event.payload.get("decision") == "rejected"
                and event.payload.get("detail") == {
                    "kind": "validated_fragment",
                    "error": "validated fragment consumer is stale or has started",
                }
                for event in orch.store.list_events(mission.id)
            )
        assert orch.store.get_mission(mission.id).status is not MissionStatus.COMPLETED

    async def exercise():
        async with Orchestrator(config, provider, owner="crossbranch-first", **runtime) as first:
            active_orch.append(first)
            mission = await first.submit_mission(
                spec(
                    key="fragment-crossbranch",
                    success_criteria=("file:final.md",),
                    budget=Budget(max_tokens=450_000, max_attempts=20),
                    synthesis={
                        "goal": "independently synthesize consumer C using pytest:tests/test_consumer.py",
                        "success_criteria": ["file:final.md"],
                        "verification_policy": ["format_check", "rule_check"],
                        "budget": {"max_tokens": 30_000, "max_attempts": 2},
                        "outputs": ["final.md"],
                    },
                )
            )
            if crash_point is not None:
                if crash_point == "after_accept":
                    # B is the first accepted Task; crash only after F is accepted.
                    first.arm_fault("after_task_completed", kind="attempt", skip=1)
                else:
                    first.arm_fault("after_validated_fragment_graph_commit", kind="manager")
                with pytest.raises(InjectedCrash):
                    await asyncio.wait_for(first.run(), 20)
                assert len(first.store.list_graph_changes(mission.id)) == (
                    1 if crash_point == "after_accept" else 2
                )
            else:
                await asyncio.wait_for(first.run(), 20)
                if case in {"accepted", "retry"}:
                    assert_finished(first, mission)
                else:
                    assert_rejected(first, mission)
        if crash_point is not None:
            await asyncio.sleep(0.35)
            async with Orchestrator(
                config, provider, owner="crossbranch-second", **runtime
            ) as second:
                await asyncio.wait_for(second.run(), 20)
                assert_finished(second, mission)

    asyncio.run(exercise())
