# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · S6-09 (D6-9 / D6-10 / D6-11): the evidence carries no credential (a file
that would is refused, a model package that would is refused), every Result can be
traced to its prompt / model / retrieval / allocator / verifier versions under the
Mission's trace id, the metrics are filled only with what the records hold, and a
required verifier that is not deployed blocks instead of passing or retrying."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from helpers_step06 import config, only, spec

from agent_orchestrator.context.context_builder import assert_no_secrets
from agent_orchestrator.contracts import MissionStatus, ids
from agent_orchestrator.observability.evidence import write_evidence
from agent_orchestrator.observability.secrets import SecretLeak, find_secrets
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.testing.fixtures import MODEL, _recorder_task, demo_dynamic_dag_provider

FAKE_KEY = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2"  # shaped like a key, not a key


def test_s6_09_every_result_is_traceable_to_its_versions_and_the_evidence_is_clean(tmp_path):
    provider = demo_dynamic_dag_provider(tasks=only("AD"))

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s6-09", success_criteria=("file:DOCS.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            evidence = tmp_path / "out"
            write_evidence(
                directory=evidence,
                store=store,
                commit=orchestrator.commit,
                mission_id=mission.id,
                baseline={"config": orchestrator.config.to_json()},
                workspaces_root=orchestrator.assembled.workspaces.root,
                test_report={"ok": True},
                echoes=orchestrator.echoed_models_for(mission.id),
                unpriced=True,
            )
            return mission, evidence, [e.trace_id for e in store.list_events(mission.id)]

    mission, evidence, trace_ids = asyncio.run(case())
    traced = json.loads((evidence / "trace.json").read_text())
    assert traced["trace_id"] == ids.trace_id(mission.id) and set(trace_ids) == {traced["trace_id"]}
    results = [s for s in traced["spans"] if s["result_id"]]
    assert len(results) == 2
    for span in results:
        assert span["trace_id"] == traced["trace_id"] and span["agent_id"]
        assert (
            span["prompt_version"]
            and span["context_version"]
            and span["context_version"] != "pending"
        )
        assert span["retrieval_version"] and span["allocator_version"] == "allocator-v1"
        assert span["router_version"] == "model-router-v1"
        assert span["model_version"] == {
            "runtime_profile_id": "default",
            "requested_model": "agent-model",
            "echoed_models": [MODEL],
        } or span["model_version"]["echoed_models"] == [MODEL]
        assert span["verifier_version"] == {
            "format_check": "verifier-v1",
            "rule_check": "verifier-v1",
        }
    assert {s["kind"] for s in traced["services"]} >= {"plan"}
    assert all(s["prompt_version"] for s in traced["services"])
    measured = json.loads((evidence / "metrics.json").read_text())
    assert measured["verification"] == {"passed": 2, "failed": 0, "pass_rate": 1.0}
    assert (
        measured["cost"]["tokens_by_role"]["worker"] > 0
        and measured["cost"]["cost_micros_by_role"] is None
    )
    assert "unpriced" in measured["cost"]["cost_note"]
    assert set(measured["role_mix"]) >= {
        "explorer",
        "exploiter",
        "critic",
        "simplifier",
        "connector",
        "failure_analyst",
        "synthesizer",
        "verifier",
    }
    assert (
        measured["role_mix"]["connector"]["start_share"] is None
        and measured["role_mix"]["exploiter"]["start_share"] == 0.4
    )
    layers = [json.loads(line) for line in (evidence / "events.jsonl").read_text().splitlines()]
    recorded = [
        e
        for e in layers
        if e["type"] == "VerificationLayerRecorded" and e["payload"]["status"] == "PASS"
    ]
    assert recorded and all(e["payload"]["verifier_version"] == "verifier-v1" for e in recorded)
    # S6-09 "无密钥": nothing in the evidence directory looks like a credential
    pattern = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
    for path in evidence.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl"}:
            assert not pattern.search(path.read_text(encoding="utf-8")), path.name


def test_s6_09_a_file_that_would_carry_a_credential_is_refused_and_names_no_value(
    tmp_path, monkeypatch
):
    provider = demo_dynamic_dag_provider(tasks=only("A"))

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s6-09b", success_criteria=("file:analysis.md",))
            )
            await orchestrator.run()
            kwargs = dict(
                store=orchestrator.store,
                commit=orchestrator.commit,
                mission_id=mission.id,
                workspaces_root=orchestrator.assembled.workspaces.root,
                test_report={},
            )
            with pytest.raises(SecretLeak) as leaked:
                write_evidence(
                    directory=tmp_path / "e1", baseline={"note": f"key {FAKE_KEY}"}, **kwargs
                )
            assert FAKE_KEY not in str(leaked.value) and "api_key_sk" in str(leaked.value)
            assert not (tmp_path / "e1" / "baseline.json").exists()
            monkeypatch.setenv("SH_APIKEY", "fixture-env-secret-0123456789")
            with pytest.raises(SecretLeak) as env_leak:
                write_evidence(
                    directory=tmp_path / "e2",
                    baseline={"note": "fixture-env-secret-0123456789"},
                    **kwargs,
                )
            assert "env_value" in str(env_leak.value) and "fixture-env-secret" not in str(
                env_leak.value
            )
            monkeypatch.delenv("SH_APIKEY")
            write_evidence(directory=tmp_path / "e3", baseline={"note": "clean"}, **kwargs)
            assert (tmp_path / "e3" / "trace.json").is_file()

    asyncio.run(case())


def test_s6_09_a_model_package_carrying_a_credential_value_is_refused():
    assert find_secrets(f"token={FAKE_KEY}") == ["api_key_sk"]
    assert find_secrets("Authorization: Bearer abcdefghijklmnop0123") == ["bearer_token"]
    assert find_secrets("ordinary text sk-short") == []
    with pytest.raises(ValueError, match="credential-like value"):
        assert_no_secrets({"task": {"notes": [f"use {FAKE_KEY}"]}})
    with pytest.raises(ValueError, match="credential-like field"):
        assert_no_secrets({"api_key": "x"})
    assert_no_secrets({"task": {"notes": ["nothing secret here"]}})


# ------------------------------------------------------------------ D6-9' verifier routing
def test_an_undeployed_verification_layer_is_refused_at_commit_with_one_reason(tmp_path):
    from graph_helpers6 import graph_service, node

    with pytest.raises(Exception):
        graph_service(
            tmp_path, nodes=[node("A", verification_policy=["format_check", "formal_check"])]
        )
    from agent_orchestrator.storage.store import Store

    store = Store.open(tmp_path / "orchestrator.db")
    rejected = [
        e
        for m in store.list_missions()
        for e in store.list_events(m.id)
        if e.type == "TaskGraphRejected"
    ]
    assert rejected and rejected[-1].payload["reason"] == "verification_policy_undeployed"


def test_a_required_verifier_that_is_not_deployed_blocks_instead_of_passing_or_retrying(
    tmp_path, monkeypatch
):
    # an older library that let a formal_check Task in (the commit gate is patched open);
    # at verification time the layer is not deployed: the Task stops, nothing is retried
    import agent_orchestrator.graph.task_graph as task_graph
    from agent_orchestrator.contracts.models import STEP2_IMPLEMENTED_LAYERS

    monkeypatch.setattr(
        task_graph, "STEP2_IMPLEMENTED_LAYERS", STEP2_IMPLEMENTED_LAYERS | {"formal_check"}
    )
    task = _recorder_task(
        "A",
        "分析 spec/INPUT.md 并写出 analysis.md",
        [],
        ["file:analysis.md"],
        3.0,
        ["analysis.md"],
        policy=["format_check", "rule_check", "formal_check"],
    )
    provider = demo_dynamic_dag_provider(tasks=[task])

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("formal", success_criteria=("file:analysis.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert (
                final.status is MissionStatus.FAILED and final.stop_reason == "verifier_unavailable"
            ), orchestrator.progress_log
            assert final.final_report["detail"]["layers"] == ["formal_check"]
            attempts = store.list_attempts(store.list_tasks(mission.id)[0].id)
            assert len(attempts) == 1  # no retry: a retry cannot make the verifier appear
            layers = {
                v["layer"]: v
                for v in store.list_verifications(
                    store.find_result_for_attempt(attempts[0].id).envelope.id
                )
            }
            assert (
                layers["formal_check"]["status"] == "ERROR"
                and layers["formal_check"]["detail"]["undeployed"] is True
            )
            assert (
                store.count_events(mission.id, "VerificationPassed") == 0
            )  # a missing layer is never a PASS

    asyncio.run(case())
