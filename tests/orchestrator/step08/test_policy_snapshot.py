# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 8 · slice B (plan D8-5'; S8-07): the policy snapshot names where a run's
behaviour comes from; two snapshots differ exactly where the configuration or a
version constant differs, and each difference names its source."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from agent_orchestrator.__main__ import main
from agent_orchestrator.governance.policies import SNAPSHOT_FIELDS, policy_snapshot, snapshot_diff
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RoutingRules


def _config(tmp_path, name="a", **overrides):
    return OrchestratorConfig(evidence_root=Path(tmp_path) / name, **overrides)


def test_s8_07_every_configuration_field_is_classified(tmp_path):
    names = {f.name for f in dataclasses.fields(OrchestratorConfig)}
    assert names == set(SNAPSHOT_FIELDS), sorted(names ^ set(SNAPSHOT_FIELDS))
    snapshot = policy_snapshot(_config(tmp_path))
    assert set(snapshot["config"]) == {n for n, c in SNAPSHOT_FIELDS.items() if c == "include"}
    # P3.2 (plan D4): workspace housekeeping is left out on purpose — it never changes how a
    # Mission is planned, run or verified
    assert set(snapshot["excluded"]) == {
        "evidence_root",
        "owner_id",
        "workspace_retention_seconds",
        "sandbox_executor",  # P3.2 (plan D2): a runtime object, digest in every receipt
    }


def test_s8_07_the_same_configuration_has_the_same_hash_wherever_it_runs(tmp_path):
    first = policy_snapshot(_config(tmp_path, "one", owner_id="orch-1"))
    second = policy_snapshot(_config(tmp_path, "two", owner_id="orch-2"))
    assert first["hash"] == second["hash"] and snapshot_diff(first, second) == []
    assert "api" not in json.dumps(first).lower() or "apikey" not in json.dumps(first).lower()


def test_s8_07_model_profile_and_configuration_differences_name_their_source(tmp_path):
    base = policy_snapshot(_config(tmp_path), routing=RoutingRules(default="small"))
    other = policy_snapshot(
        _config(tmp_path, model="deepseek-flash", candidates_per_task=2, knowledge_sharing=False),
        routing=RoutingRules(default="large"),
    )
    diff = {d["key"]: d for d in snapshot_diff(base, other)}
    assert (
        diff["config.model"]["source"] == "OrchestratorConfig.model"
        and diff["config.model"]["b"] == "deepseek-flash"
    )
    assert diff["config.candidates_per_task"]["source"] == "OrchestratorConfig.candidates_per_task"
    assert (
        diff["config.knowledge_sharing"]["a"] is True
        and diff["config.knowledge_sharing"]["b"] is False
    )
    assert diff["routing.default"]["a"] == "small" and diff["routing.default"]["b"] == "large"
    assert base["hash"] != other["hash"]


def test_s8_07_a_changed_version_constant_is_listed_with_its_module(tmp_path, monkeypatch):
    import agent_orchestrator.context.retrieval as retrieval
    import agent_orchestrator.runtime.role_templates as templates

    before = policy_snapshot(_config(tmp_path))
    monkeypatch.setattr(retrieval, "RETRIEVAL_VERSION", "retrieval-v2")  # a new build of the code
    monkeypatch.setitem(
        templates.ROLES,
        "worker",
        dataclasses.replace(templates.ROLES["worker"], prompt_version="worker-v3"),
    )
    after = policy_snapshot(_config(tmp_path))
    diff = {d["key"]: d for d in snapshot_diff(before, after)}
    assert diff["versions.retrieval"] == {
        "key": "versions.retrieval",
        "a": "retrieval-v1",
        "b": "retrieval-v2",
        "source": "agent_orchestrator.context.retrieval.RETRIEVAL_VERSION",
    }
    assert diff["role_templates.worker"]["b"] == "worker-v3"
    assert diff["role_templates.worker"]["source"].startswith(
        "agent_orchestrator.runtime.role_templates.ROLES"
    )


def test_an_unclassified_field_is_refused(tmp_path):
    @dataclasses.dataclass(frozen=True)
    class Wider(OrchestratorConfig):  # type: ignore[misc]
        brand_new_knob: int = 1

    with pytest.raises(ValueError, match="brand_new_knob"):
        policy_snapshot(Wider(evidence_root=Path(tmp_path) / "w"))


def test_the_demo_evidence_carries_the_snapshot_and_no_drift(tmp_path, capsys):
    evidence = Path(tmp_path) / "s2"
    assert (
        main(
            [
                "demo",
                "--scenario",
                "single-task",
                "--provider",
                "fixtures",
                "--evidence-dir",
                str(evidence),
                "--idempotency-key",
                "snap",
            ]
        )
        == 0
    )
    capsys.readouterr()
    written = json.loads((evidence / "policy_snapshot.json").read_text(encoding="utf-8"))
    baseline = json.loads((evidence / "baseline.json").read_text(encoding="utf-8"))
    assert (
        written["drift"] is False
        and written["drift_detail"] == []
        and written["start_hash"] == baseline["policy_snapshot"]["hash"] == written["end_hash"]
    )
    assert written["snapshot"]["provider"]["class"] == "RoleScriptedProvider"
    assert written["snapshot"]["role_templates"]["worker"] == "worker-v2"
