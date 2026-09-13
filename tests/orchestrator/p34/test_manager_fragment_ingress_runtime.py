# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Public Mission -> real Provider Manager -> durable fragment validation."""

import asyncio
import json

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step, graph_proposal_step, package_of
from graph_helpers7 import node, spec

from agent_orchestrator.contracts import Budget
from agent_orchestrator.contracts.models import canonical_json, sha256_hex
from agent_orchestrator.orchestrator.event_handler import InjectedCrash, Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig


def _provider(decision="fragment"):
    seen: dict[str, int] = {}
    originals: list[str] = []
    manager_packages: list[dict] = []

    def worker(request):
        package = package_of(request)
        attempt_id = package["attempt"]["attempt_id"]
        count = seen.get(attempt_id, 0)
        seen[attempt_id] = count + 1
        scope = package.get("fragment_scope")
        if scope is not None:
            mapped = scope["output_path_mapping"]["good.md"]
            if count == 0:
                return "workspace_read_file", {"path": "good.md"}
            if count == 1:
                return "workspace_write_file", {"path": mapped, "content": "actual fragment\n"}
            return envelope_step(
                summary="independently wrote scoped output", artifacts=[mapped], claims=["scope"]
            )(request)
        if attempt_id not in originals:
            originals.append(attempt_id)
        if originals.index(attempt_id) == 0:
            if count == 0:
                return "workspace_write_file", {"path": "good.md", "content": "actual partial\n"}
            return envelope_step(
                summary="partial output", artifacts=["good.md"], claims=["partial"]
            )(request)
        if count == 0:
            return "workspace_write_file", {"path": "good.md", "content": "actual complete\n"}
        if count == 1:
            return "workspace_write_file", {"path": "missing.md", "content": "now present\n"}
        return envelope_step(
            summary="completed retry", artifacts=["good.md", "missing.md"], claims=["complete"]
        )(request)

    def manager(request):
        package = package_of(request)
        manager_packages.append(package)
        catalog = package["fragment_validation"]
        assert catalog["available"] is True
        artifact = next(item for item in catalog["materials"] if item["kind"] == "artifact")
        proposal = {
            "schema_version": 1,
            "origin": catalog["origin"],
            "criterion_ids": [
                item["id"] for item in catalog["criteria"] if item["text"] == "file:good.md"
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
            ],
            "rationale": "validate only the actual partial file",
        }
        if decision == "wrong_origin":
            proposal["origin"] = {**proposal["origin"], "result_id": "forged-result"}
        body = {
            "schema_version": 1,
            "base_graph_version": package["graph_version"],
            "proposal": proposal,
        }
        tagged = (
            "<fragment_validation_decision>"
            + json.dumps(body, ensure_ascii=False)
            + "</fragment_validation_decision>"
        )
        if decision == "mixed":
            tagged += (
                "<graph_change_proposal>"
                + json.dumps(
                    {
                        "base_graph_version": package["graph_version"],
                        "rationale": "mixed",
                        "operations": [],
                    }
                )
                + "</graph_change_proposal>"
            )
        return tagged

    tasks = [
        node(
            "A",
            tokens=120_000,
            budget={"max_tokens": 120_000, "max_attempts": 2},
            success_criteria=["file:good.md", "file:missing.md"],
            outputs=["good.md", "missing.md"],
            verification_policy=["format_check", "rule_check"],
        )
    ]
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step(tasks)],
            "worker": [worker] * 18,
            "manager": [manager] * 3,
        }
    )
    return provider, manager_packages


def _config(tmp_path, *, proposal_limit=3):
    return OrchestratorConfig(
        evidence_root=tmp_path / "manager-fragment",
        max_concurrency=1,
        candidates_per_task=1,
        manager_after_failures=1,
        max_manager_rounds=1,
        max_proposals_per_agent=proposal_limit,
        lease_seconds=0.3,
    )


def _mission():
    return spec(
        success_criteria=("file:good.md", "file:missing.md"),
        budget=Budget(max_tokens=300_000, max_attempts=12),
    )


@pytest.mark.parametrize(
    "decision,proposal_limit",
    [("fragment", 3), ("mixed", 3), ("wrong_origin", 3), ("fragment", 0)],
)
def test_manager_fragment_decision_uses_real_failed_result_and_rejects_bad_wire(
    tmp_path, decision, proposal_limit
):
    async def exercise():
        provider, packages = _provider(decision)
        async with Orchestrator(_config(tmp_path, proposal_limit=proposal_limit), provider) as orch:
            mission = await orch.submit_mission(_mission())
            await asyncio.wait_for(orch.run(), 15)
            assert len(packages) == 1
            catalog = packages[0]["fragment_validation"]
            assert catalog["available"] is True
            assert {item["text"] for item in catalog["criteria"]} == {
                "file:good.md",
                "file:missing.md",
            }
            assert catalog["origin"]["task_revision_id"]
            assert "claims" in catalog
            artifact_choice = next(
                item for item in catalog["materials"] if item["kind"] == "artifact"
            )
            assert artifact_choice["size_bytes"] > 0
            assert len(artifact_choice["content_hash"]) == 64
            manager_intents = [
                item
                for item in orch.store.list_intents("SETTLED", "FAILED")
                if item.kind == "manager"
            ]
            assert len(manager_intents) == 1
            assert manager_intents[0].config["prompt_version"] == "manager-v4"
            assert (
                "fragment_validation_decision"
                in manager_intents[0].config["agent_config"]["instructions"]
            )
            assert manager_intents[0].config["fragment_origin"] == catalog
            origin = orch.store.get_result(catalog["origin"]["result_id"])
            assert origin is not None and origin.verdict == "FAIL"
            rows = orch.store.list_fragment_validations(mission.id)
            if decision == "fragment" and proposal_limit:
                assert len(rows) == 1
                receipt = orch.store.get_receipt(rows[0]["projection_receipt_id"])
                validation = orch.store.get_task(receipt["validation_task_id"])
                assert orch.store.list_attempts(validation.id)
                assert validation.accepted_result_id is not None
                assert orch.store.get_result(validation.accepted_result_id).verdict == "PASS"
                assert receipt["source"]["intent_id"]
                assert receipt["command_id"] == receipt["source"]["intent_id"]
                assert orch.store.get_receipt(receipt["graph_change_id"]) is not None
                assert orch.store.get_result(origin.envelope.id).verdict == "FAIL"
            else:
                assert rows == []
                assert not any(
                    task.context.get("fragment_validation")
                    for task in orch.store.list_tasks(mission.id)
                )

    asyncio.run(exercise())


@pytest.mark.parametrize("legacy_receipt", [False, True])
def test_manager_fragment_commit_replays_after_pre_settle_crash(tmp_path, legacy_receipt):
    async def exercise():
        provider, packages = _provider()
        config = _config(tmp_path)
        async with Orchestrator(config, provider, owner="fragment-first") as first:
            mission = await first.submit_mission(_mission())
            first.arm_fault("after_fragment_commit", kind="manager")
            with pytest.raises(InjectedCrash):
                await asyncio.wait_for(first.run(), 15)
            assert len(first.store.list_fragment_validations(mission.id)) == 1
            managers = [
                item
                for item in first.store.list_intents("SUBMITTED", "AGENT_CREATED")
                if item.kind == "manager"
            ]
            assert len(managers) == 1
            if legacy_receipt:
                # Simulate a command receipt persisted by the proposal-only hash
                # implementation before this runtime was upgraded.
                command_id = "fragment-command-" + sha256_hex(
                    {"mission": mission.id, "command": managers[0].intent_id}
                )
                wrapper = first.store.get_receipt(command_id)
                body = {
                    key: value
                    for key, value in wrapper["receipt"]["proposal"].items()
                    if key != "rationale"
                }
                old_hash = sha256_hex(body)
                wrapper["proposal_hash"] = old_hash
                with first.store.transaction() as connection:
                    connection.execute(
                        "UPDATE commit_receipts SET proposal_hash=?,receipt_json=? "
                        "WHERE commit_id=?",
                        (old_hash, canonical_json(wrapper), command_id),
                    )
        await asyncio.sleep(0.35)
        async with Orchestrator(config, provider, owner="fragment-second") as second:
            await asyncio.wait_for(second.run(), 15)
            rows = second.store.list_fragment_validations(mission.id)
            assert len(rows) == 1
            receipt = second.store.get_receipt(rows[0]["projection_receipt_id"])
            validation = second.store.get_task(receipt["validation_task_id"])
            assert validation.accepted_result_id is not None
            assert second.store.get_result(validation.accepted_result_id).verdict == "PASS"
            assert second.store.get_receipt(receipt["graph_change_id"]) is not None
            assert len(packages) == 1  # no second physical Manager handoff
            managers = [
                item for item in second.store.list_intents("SETTLED") if item.kind == "manager"
            ]
            assert len(managers) == 1

    asyncio.run(exercise())
