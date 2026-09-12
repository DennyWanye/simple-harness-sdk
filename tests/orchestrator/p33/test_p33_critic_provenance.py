# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""切片 A：旧 Critic intent 重启复用的版本必须来自真正产生 verdict 的 ordinal。

先固定回归 oracle：在旧 critic-v2 已 SETTLED、验证层尚未写入时崩溃；
新进程按缺字段旧 doc snapshot 解释出 doc 提示，但必须重用旧 intent。
记录和人工恢复都绑定那个 intent；失败 ordinal、错版本、错 intent 不可冒充。
使用确定性 Provider 和真实 SQLite/SDK/验证路由，不调用真实模型或读取 secret。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step, graph_proposal_step, package_of
from graph_helpers7 import node, spec
from helpers_step07 import ALICE

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import TaskStatus
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.role_templates import CRITIC
from agent_orchestrator.storage.store import InjectedCrash


@pytest.mark.parametrize("failed_first,needs_human", [(False, False), (True, True)])
def test_old_critic_intent_restart_records_and_reuses_actual_ordinal(
    tmp_path, monkeypatch, failed_first, needs_human
):
    source_path = "sources/reference.md"
    source_quote = "资料记载甲方案不支持离线。"
    source_version = ""

    def with_citation(body):
        return {
            **body,
            "claims": [
                {
                    "content": source_quote,
                    "confidence": 0.8,
                    "citations": [
                        {
                            "path": source_path,
                            "version": source_version,
                            "start_line": 1,
                            "end_line": 1,
                            "quote": source_quote,
                        }
                    ],
                }
            ],
        }

    def verdict(request):
        body = {
            "verdict": "PASS",
            "findings": [],
            "needs_human": needs_human,
            "mission_criteria": [
                {"criterion": c, "met": True, "reason": "确定性资料核对"}
                for c in package_of(request)["mission_success_criteria"]
            ],
        }
        return "<critic_verdict>" + json.dumps(body, ensure_ascii=False) + "</critic_verdict>"

    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step(
                    [
                        node(
                            "A",
                            tokens=60_000,
                            success_criteria=["file:a.md", "cite:" + source_path],
                            verification_policy=[
                                "format_check",
                                "rule_check",
                                "critic_review",
                                "human_review",
                            ],
                        )
                    ]
                )
            ],
            "worker": [
                ("workspace_write_file", {"path": "a.md", "content": "# 核对记录\n"}),
                envelope_step(
                    summary="已写记录",
                    artifacts=["a.md"],
                    claims=[source_quote],
                    override=with_citation,
                ),
            ],
            "critic": (["malformed critic verdict"] if failed_first else []) + [verdict],
        }
    )
    config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)

    async def run_bounded(orchestrator):
        try:
            await asyncio.wait_for(orchestrator.run(), timeout=30)
        except TimeoutError as error:
            # 前置失败不能被误报为 provenance 的预期 red；输出准确停点。
            intents = [
                (intent.subject_id, intent.state) for intent in orchestrator.store.list_intents()
            ]
            raise AssertionError(
                f"fixture 未到预期停点；calls={provider.by_role}; intents={intents}; "
                f"progress={orchestrator.progress_log[-12:]}"
            ) from error

    async def case():
        nonlocal source_version
        async with Orchestrator(config, provider, owner="p33-provenance") as first:
            with monkeypatch.context() as patch:
                bind = first.store.bind_mission_domain

                def bind_legacy(mission_id, *, domain_id, domain_version, snapshot):
                    legacy = dict(snapshot)
                    legacy["version"] = "1"
                    legacy.pop("role_templates")
                    legacy.pop("context_wording")
                    legacy.pop("adapters")
                    return bind(
                        mission_id, domain_id=domain_id, domain_version="1", snapshot=legacy
                    )

                patch.setattr(first.store, "bind_mission_domain", bind_legacy)
                mission = await first.submit_mission(
                    spec("critic-provenance", domain=DOC_DOMAIN, success_criteria=("file:a.md",))
                )

            api = MissionControlV1(
                first, tenant_id=mission.tenant_id, principal=Principal("importer")
            )
            api.register_source(
                {
                    "mission_id": mission.id,
                    "path": source_path,
                    "content": source_quote + "\n",
                    "kind": "markdown",
                    "idempotency_key": "source",
                }
            )
            source_version = first.store.get_source(mission.id, source_path)["version_hash"]

            # 模拟升级前已创建的 code Critic；失败 ordinal 故意使用不同版本，
            # 防止回归仅固定读取 :critic:1 而误通过。
            original_template = first._template
            critic_selections = 0

            def old_critic(template, mission_id):
                nonlocal critic_selections
                if template.name == "critic":
                    critic_selections += 1
                    if not (failed_first and critic_selections == 1):
                        return CRITIC
                return original_template(template, mission_id)

            settle = first._settle_intent

            def crash_after_settled(intent, state):
                settle(intent, state)
                if intent.kind == "critic" and state == "SETTLED":
                    raise InjectedCrash("critic-settled-before-layer")

            with monkeypatch.context() as patch:
                patch.setattr(first, "_template", old_critic)
                patch.setattr(first, "_settle_intent", crash_after_settled)
                with pytest.raises(InjectedCrash, match="critic-settled-before-layer"):
                    await run_bounded(first)

            [task] = first.store.list_tasks(mission.id)
            [attempt] = first.store.list_attempts(task.id)
            [stored] = first.store.list_results_by_verification("RUNNING")
            result_id = stored.envelope.id
            successful = first.store.get_intent_for_subject(
                f"{attempt.id}:critic:{2 if failed_first else 1}"
            )
            assert successful.state == "SETTLED"
            assert successful.config["prompt_version"] == "critic-v2"
            assert successful.config["agent_config"]["instructions"] == CRITIC.instructions
            assert not any(
                row["layer"] == "critic_review" for row in first.store.list_verifications(result_id)
            )
            if failed_first:
                failed = first.store.get_intent_for_subject(f"{attempt.id}:critic:1")
                assert failed.state == "FAILED"
                assert failed.config["prompt_version"] == "critic-doc-research-v1"
            frozen_intent = successful.to_json()
            frozen_domain = first.store.get_mission_domain(mission.id)
            critic_calls = provider.by_role["critic"]

        # 真正关闭并重新打开数据库和 SDK；无需留存进程内执行 metadata。
        async with Orchestrator(config, provider, owner="p33-provenance") as second:
            assert second._template(CRITIC, mission.id).prompt_version == "critic-doc-research-v1"
            await run_bounded(second)
            [request] = second.store.list_approvals(mission.id)
            assert second.store.get_result(result_id).verification_state == "SUSPENDED"
            rows = second.store.list_verifications(result_id)
            [critic_row] = [row for row in rows if row["layer"] == "critic_review"]
            assert critic_row["status"] == ("NEEDS_HUMAN" if needs_human else "PASS")
            assert critic_row["detail"]["verifier_version"] == "critic-v2"
            assert critic_row["detail"]["critic_intent_id"] == successful.intent_id
            assert second.store.get_intent(successful.intent_id).to_json() == frozen_intent
            assert second.store.get_mission_domain(mission.id) == frozen_domain
            assert provider.by_role["critic"] == critic_calls

            second.commit.review_result(
                request["request_id"], principal=ALICE, verdict="pass", note="已核对", nonce="p33"
            )
            task = second.store.get_task(task.id)
            _, reuse, _ = second._human_inputs(result_id, task)
            assert reuse["critic_review"].detail["verifier_version"] == "critic-v2"

            # 历史层没有新增 intent id 时，仍能通过准确的 durable ordinal 兼容。
            legacy_detail = dict(critic_row["detail"])
            legacy_detail.pop("critic_intent_id")
            for detail, expected in (
                (legacy_detail, True),
                ({**legacy_detail, "verifier_version": "critic-doc-research-v1"}, False),
                ({**legacy_detail, "critic_intent_id": "another-intent"}, False),
                ({k: v for k, v in legacy_detail.items() if k != "verifier_version"}, False),
            ):
                changed_rows = [
                    {**row, "detail": detail} if row["layer"] == "critic_review" else row
                    for row in rows
                ]
                with monkeypatch.context() as patch:
                    patch.setattr(second.store, "list_verifications", lambda _: changed_rows)
                    _, candidate, _ = second._human_inputs(result_id, task)
                    assert ("critic_review" in candidate) is expected
                    assert "rule_check" in candidate

            lookup = second.store.get_intent_for_subject

            def no_success(subject):
                intent = lookup(subject)
                return (
                    replace(intent, state="FAILED")
                    if intent is not None and intent.intent_id == successful.intent_id
                    else intent
                )

            with monkeypatch.context() as patch:
                patch.setattr(second.store, "get_intent_for_subject", no_success)
                _, candidate, _ = second._human_inputs(result_id, task)
                assert "critic_review" not in candidate
                assert "rule_check" in candidate

            async def must_reuse(*args, **kwargs):
                raise AssertionError("人工恢复不得重新调用已完成的 Critic")

            # C05: a pending legacy rule PASS has no assessment. It must be really
            # rerun from the frozen source, while the settled Critic remains reusable.
            second.store.upsert_verification(
                result_id=result_id,
                attempt_id=attempt.id,
                layer="rule_check",
                status="PASS",
                detail={"summary": "legacy rule PASS", "verifier_version": "verifier-v1"},
            )
            with monkeypatch.context() as patch:
                patch.setattr(second, "_run_critic", must_reuse)
                assert await second._verify(result_id)
            assert second.store.get_task(task.id).status is TaskStatus.COMPLETED
            assert provider.by_role["critic"] == critic_calls
            assert second.store.get_intent(successful.intent_id).to_json() == frozen_intent
            [recorded] = [
                row
                for row in second.store.list_verifications(result_id)
                if row["layer"] == "critic_review"
            ]
            assert recorded["detail"]["verifier_version"] == "critic-v2"
            [rule] = [
                row
                for row in second.store.list_verifications(result_id)
                if row["layer"] == "rule_check"
            ]
            assert rule["detail"]["criterion_assessments"]
            [knowledge] = second.store.list_knowledge(mission.id)
            assert knowledge.key == f"attribution:{source_version}:1-1"

    asyncio.run(case())
