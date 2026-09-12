# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""A05 actual SDK chain: failed A -> independent B -> scope-consuming C.

Scripted Provider proves dispatch/tool/receipt boundaries, not model quality.
Every accepted doc Task executes its own Critic; no PASS row is fabricated.
"""

import asyncio
import json

from fixtures_provider import RoleScriptedProvider, envelope_step, package_of
from test_g_source_instructions_runtime import _drive

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import Budget, TaskStatus
from agent_orchestrator.contracts.fragments import FragmentProposalV1
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.changes import TaskGraphChange
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.fragments import current_task_revision, revision_for_result
from agent_orchestrator.runtime.assembly import OrchestratorConfig

PATH = "sources/observations.md"
QUOTE = "本次记录只覆盖离线实验。"


def test_failed_origin_actual_independent_verification_and_downstream_scope(tmp_path):
    async def run():
        version = ""
        received = []
        used = []

        def amend(body):
            body["claims"][0]["citations"] = [
                {"path": PATH, "version": version, "start_line": 1, "end_line": 1, "quote": QUOTE}
            ]
            body["used_knowledge"] = list(used)
            body["claims"][0]["dependencies"] = list(used)
            return body

        def read_original(request):
            # Full original file must be in the real new workspace, not a summary.
            assert "good.md" in package_of(request)["tools_and_permissions"]["workspace_files"]
            if received:
                task = orch.store.get_task(package_of(request)["task_contract"]["task_id"])
                revision = current_task_revision(orch.store, task)
                consumed = orch.commit.fragment_input(
                    receipt["fragment_id"], consumer_task_revision_id=revision.revision_id
                )
                assert consumed["validation_result_id"] == accepted.envelope.id
                assert all(item["artifact_id"] != artifact.id for item in consumed["material_refs"])
            received.append(package_of(request)["task_contract"]["task_id"])
            return "workspace_read_file", {"path": "good.md"}

        def critic(request):
            messages = [json.loads(m.content) for m in request.messages if str(m.role) == "tool"]
            assert any(QUOTE in str(item) for item in messages)
            return (
                "<critic_verdict>"
                + json.dumps(
                    {
                        "verdict": "PASS",
                        "findings": [],
                        "mission_criteria": [
                            {
                                "criterion": criterion,
                                "met": criterion != "file:missing.md",
                                "reason": "局部独立核对，缺失文件的原范围仍未通过",
                            }
                            for criterion in package_of(request)["mission_success_criteria"]
                        ],
                    }
                )
                + "</critic_verdict>"
            )

        def final(output):
            return envelope_step(
                summary="独立核对来源记录", artifacts=[output], claims=[QUOTE], override=amend
            )

        provider = RoleScriptedProvider(
            {
                "worker": [
                    ("workspace_read_file", {"path": PATH}),
                    ("workspace_write_file", {"path": "good.md", "content": "原始完整材料\n"}),
                    final("good.md"),
                    read_original,
                    ("workspace_read_file", {"path": PATH}),
                    (
                        "workspace_write_file",
                        {"path": "good.md", "content": "独立复核后的完整材料\n"},
                    ),
                    final("good.md"),
                    read_original,
                    ("workspace_read_file", {"path": PATH}),
                    (
                        "workspace_write_file",
                        {"path": "final.md", "content": "仅复用已独立复核的范围\n"},
                    ),
                    final("final.md"),
                ],
                "critic": [
                    ("workspace_read_file", {"path": PATH}),
                    ("workspace_read_file", {"path": "good.md"}),
                    critic,
                    ("workspace_read_file", {"path": PATH}),
                    ("workspace_read_file", {"path": "final.md"}),
                    critic,
                ],
            }
        )
        async with Orchestrator(
            OrchestratorConfig(
                evidence_root=tmp_path, max_concurrency=1, attempt_reserve_tokens=4000
            ),
            provider,
        ) as orch:
            mission = await orch.submit_mission(
                MissionSpec(
                    goal="核对记录并保留未满足范围",
                    success_criteria=("file:good.md", "file:missing.md", "cite:" + PATH),
                    tenant_id="tenant",
                    idempotency_key="fragment-runtime",
                    domain=DOC_DOMAIN,
                    allowed_tools=("workspace_read_file", "workspace_write_file"),
                    budget=Budget(max_tokens=300000, max_attempts=20),
                )
            )
            api = MissionControlV1(orch, tenant_id="tenant", principal=Principal("source-importer"))
            api.register_source(
                {
                    "mission_id": mission.id,
                    "path": PATH,
                    "content": QUOTE + "\n",
                    "kind": "markdown",
                    "idempotency_key": "source",
                }
            )
            version = orch.store.get_source(mission.id, PATH)["version_hash"]
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            {
                                "key": "A",
                                "goal": mission.goal,
                                "rationale": "原完整验收",
                                "dependencies": [],
                                "success_criteria": list(mission.success_criteria),
                                "verification_policy": [
                                    "format_check",
                                    "rule_check",
                                    "critic_review",
                                ],
                                "allowed_tools": list(mission.allowed_tools),
                                "outputs": ["good.md"],
                                "budget": {"max_tokens": 30000, "max_attempts": 3},
                            }
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "oracle"},
            )
            origin = tasks[0]
            old_attempt, old = await _drive(orch, origin)
            assert old.verdict == "FAIL"
            assert any(
                row["layer"] == "rule_check" and row["status"] == "FAIL"
                for row in orch.store.list_verifications(old.envelope.id)
            )
            origin_before = orch.store.get_task(origin.id).to_json()
            attempt_before = orch.store.get_attempt(old_attempt.id).to_json()
            revision = revision_for_result(orch.store, orch.commit._source_cas(), old.envelope.id)
            artifact = orch.store.get_artifact(old.artifacts[0])
            proposal = FragmentProposalV1.from_json(
                {
                    "schema_version": 1,
                    "origin": {
                        "mission_id": mission.id,
                        "task_id": origin.id,
                        "attempt_id": old_attempt.id,
                        "result_id": old.envelope.id,
                        "task_revision_id": revision.revision_id,
                    },
                    "criterion_ids": [
                        row["id"] for row in revision.criteria if row["text"] != "file:missing.md"
                    ],
                    "claim_refs": [
                        {"claim_id": claim.id, "claim_revision": claim.version}
                        for claim in orch.store.list_claims(old.envelope.id)
                    ],
                    "material_refs": [
                        {
                            "kind": "artifact",
                            "artifact_id": artifact.id,
                            "content_hash": artifact.content_hash,
                            "byte_start": 0,
                            "byte_end_exclusive": artifact.size_bytes,
                        }
                    ],
                    "rationale": "只验证完整可投影准则",
                }
            )
            receipt = orch.commit.commit_fragment_validation(
                proposal, command_id="validate", base_graph_version=1, source={"manager": "oracle"}
            )
            validation = orch.store.get_task(receipt["validation_task_id"])
            assert validation.dependency_ids == ()
            new_attempt, accepted = await _drive(orch, validation)
            assert accepted.verdict == "PASS"
            assert orch.store.get_task(validation.id).accepted_result_id == accepted.envelope.id
            proof = next(
                row
                for row in orch.store.list_verifications(accepted.envelope.id)
                if row["layer"] == "critic_review"
            )
            assert proof["detail"]["critic_intent_id"]
            assert orch.store.get_intent(proof["detail"]["critic_intent_id"]).state == "SETTLED"
            child, _ = orch.commit.commit_graph_change(
                mission.id,
                TaskGraphChange.from_json(
                    {
                        "base_graph_version": 2,
                        "basis": {"trigger": "validated_fragment"},
                        "rationale": "复用新独立验证的范围",
                        "operations": [
                            {
                                "op": "add_task",
                                "key": "C",
                                "goal": mission.goal + "：仅复用已核对范围",
                                "rationale": "新的交付仍需独立核验",
                                "dependencies": [validation.id],
                                "success_criteria": list(validation.success_criteria),
                                "verification_policy": list(validation.verification_policy),
                                "allowed_tools": list(validation.allowed_tools),
                                "outputs": ["final.md"],
                                "budget": {"max_tokens": 30000, "max_attempts": 3},
                            }
                        ],
                    }
                ),
                source={"manager": "oracle"},
            )
            consumer = child[0]
            # Existing actual dependency materialization reads B, never failed A.
            consumer_attempt, completed = await _drive(orch, consumer)
            assert completed.verdict == "PASS"
            assert received == [validation.id, consumer.id]
            assert orch.store.get_task(origin.id).to_json() == origin_before
            assert orch.store.get_attempt(old_attempt.id).to_json() == attempt_before
            assert orch.store.get_result(old.envelope.id).verdict == "FAIL"
            assert new_attempt.id != old_attempt.id != consumer_attempt.id
            assert all("file:missing.md" != row["text"] for row in receipt["criterion_mapping"])
            assert (
                len([intent for intent in orch.store.list_intents() if intent.kind == "critic"])
                == 2
            )
            # Reading historical receipts never changes original Mission denominator.
            assert orch.store.get_mission(mission.id).success_criteria == mission.success_criteria
            assert orch.store.get_task(consumer.id).status is TaskStatus.COMPLETED

    asyncio.run(run())
