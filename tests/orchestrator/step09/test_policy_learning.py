# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 9 · slice C (plan D9-5' / D9-6'; S9-01, S9-07): the rule improver reads history
read-only and proposes a candidate that traces back to its training data, code and
rules — or says honestly that the history is too thin, mixed or untrustworthy, and
registers nothing.  Fixture history proves the mechanism only."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest
from fixtures_provider import RoleScriptedProvider, critic_step, proposal_step

from agent_orchestrator.contracts import Budget, MissionStatus
from agent_orchestrator.governance.learning import (
    LEARNER_VERSION,
    learn,
    learn_and_register,
    rule_weights,
)
from agent_orchestrator.governance.promotion import (
    DEPLOYMENT_TIMELINE,
    builtin_weights,
    resolve_params,
)
from agent_orchestrator.observability.evaluation import library_digest
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RoutingRules, RuntimeProfile
from agent_orchestrator.storage.store import Store
from agent_orchestrator.testing.fixtures import (
    DEMO_BAD,
    DEMO_GOOD,
    DEMO_PROPOSAL,
    DEMO_SEED,
    demo_worker_script,
)

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")
MISSIONS = 6
ROUTING = RoutingRules(default="small", escalate={"small": "large"}, escalate_after_failures=1)


def _spec(key):
    return MissionSpec(
        goal="在隔离工作区实现字符串解析函数 parse_kv，并通过 tests/test_parse_kv.py",
        success_criteria=("pytest:tests/test_parse_kv.py",),
        tenant_id="tenant-9",
        idempotency_key=key,
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=400_000, max_attempts=4),
        workspace_seed=DEMO_SEED,
    )


def _config(root):
    return OrchestratorConfig(evidence_root=Path(root), max_concurrency=1, test_timeout_seconds=60)


def _run_history(root, *, missions, provider_kind=None, first=DEMO_BAD):
    """``missions`` Missions, one after another: the cheap profile fails every first
    attempt, the escalation target passes — the pattern rule R2 is about."""

    small = RoleScriptedProvider(
        {
            "planner": [proposal_step(DEMO_PROPOSAL)] * missions,
            "worker": demo_worker_script(first) * missions,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * (2 * missions),
        },
        model="model-small",
    )
    large = RoleScriptedProvider(
        {"worker": demo_worker_script(DEMO_GOOD) * missions}, model="model-large"
    )
    profiles = {
        "small": RuntimeProfile("small", small, "model-small"),
        "large": RuntimeProfile("large", large, "model-large"),
    }

    async def run():
        async with Orchestrator(
            _config(root), profiles=profiles, routing=ROUTING, provider_kind=provider_kind
        ) as orch:
            for n in range(missions):
                mission = await orch.submit_mission(_spec(f"history-{n}"))
                await orch.run()
                assert orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                    orch.progress_log
                )

    asyncio.run(run())
    return Path(root)


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    return _run_history(tmp_path_factory.mktemp("history"), missions=MISSIONS)


def _base(root):
    return resolve_params(_config(root), routing=ROUTING)


# ------------------------------------------------------------------ S9-01
def test_s9_01_a_candidate_traces_back_to_its_training_data_code_and_rules(history, tmp_path):
    before = library_digest(history / "orchestrator.db")
    result = learn([history], base_params=_base(tmp_path), routing=ROUTING)
    assert result.outcome == "candidate", result.reasons
    assert result.params["routing"]["by_task_kind"] == {"code": "large"}  # R2: route code to large
    manifest = result.manifest
    assert manifest["learner"] == LEARNER_VERSION and manifest["kind"] == "rule_improvement"
    assert len(manifest["training"]) == MISSIONS
    for row in manifest["training"]:
        assert row["library_sha256"] == before and row["provider_kind"] == "fixtures"
        assert row["status"] == "COMPLETED" and row["task_identity"] and row["mission_id"]
    assert (
        manifest["code_versions"]["agent_orchestrator"]
        and manifest["interpreter_versions"]["allocator"]
    )
    r2 = manifest["rules"]["R2"]
    assert r2["fired"] and r2["groups"]["code"]["missions"] == MISSIONS
    assert r2["groups"]["code"]["wilson_low"] >= 0.5
    assert manifest["rules"]["R1"]["fired"] is False  # one Task per Mission: nothing contested
    reputation = {(r["role"], r["profile"]): r for r in manifest["reputation"]}
    assert reputation[("worker", "small")]["success_rate"] == 0.0
    assert reputation[("worker", "large")]["success_rate"] == 1.0
    assert "规则改进" in result.note and "不代表" in result.note  # fixture: mechanism only
    assert library_digest(history / "orchestrator.db") == before  # history only read

    store = Store.open(Path(tmp_path) / "prod" / "orchestrator.db")
    commit = CommitService(store)
    commit.seed_policy(_base(tmp_path))
    first = learn_and_register(commit, [history], base_params=_base(tmp_path), routing=ROUTING)
    again = learn_and_register(commit, [history], base_params=_base(tmp_path), routing=ROUTING)
    assert first["proposal"]["created"] is True and again["proposal"]["created"] is False
    assert first["proposal"]["proposal_id"] == again["proposal"]["proposal_id"]
    stored = store.get_policy_proposal(first["proposal"]["proposal_id"])
    assert stored["source"] == "learner" and len(stored["manifest"]["training"]) == MISSIONS
    assert [d["key"] for d in stored["diff"]] == ["routing.by_task_kind.code"]
    store.close()


# ------------------------------------------------------------------ S9-07
def test_s9_07_too_few_missions_is_insufficient_and_nothing_is_registered(history, tmp_path):
    store = Store.open(Path(tmp_path) / "prod" / "orchestrator.db")
    commit = CommitService(store)
    commit.seed_policy(_base(tmp_path))
    outcome = learn_and_register(
        commit, [history], base_params=_base(tmp_path), routing=ROUTING, min_missions=10
    )
    assert outcome["result"].outcome == "insufficient" and outcome["proposal"] is None
    assert any("6 < 10" in reason for reason in outcome["result"].reasons)
    assert store.list_policy_proposals() == []
    refused = [
        e for e in store.iter_events(DEPLOYMENT_TIMELINE) if e.type == "PolicyProposalRefused"
    ]
    assert len(refused) == 1 and refused[0].payload["reasons"] == outcome["result"].reasons
    assert "提升" not in " ".join(outcome["result"].reasons)
    store.close()


def test_s9_07_fixture_and_real_history_together_is_refused(history, tmp_path):
    real = _run_history(Path(tmp_path) / "real", missions=1, provider_kind="real")
    result = learn([history, real], base_params=_base(tmp_path), routing=ROUTING)
    assert result.outcome == "refused" and result.params is None
    assert any("fixtures" in r and "real" in r for r in result.reasons)


def test_s9_07_an_untrustworthy_record_is_refused_with_its_mission(history, tmp_path):
    damaged = Path(tmp_path) / "damaged"
    shutil.copytree(history, damaged, ignore=shutil.ignore_patterns("workspaces", "execution*"))
    connection = sqlite3.connect(damaged / "orchestrator.db")
    mission_id, event_id = connection.execute(
        "SELECT mission_id, event_id FROM events WHERE type = 'VerificationPassed' ORDER BY seq LIMIT 1"
    ).fetchone()
    connection.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
    connection.commit()
    connection.close()
    result = learn([damaged], base_params=_base(tmp_path), routing=ROUTING)
    assert result.outcome == "refused" and result.params is None
    assert any(mission_id in r and "replay" in r for r in result.reasons)


def test_enough_history_that_shows_nothing_is_no_change(tmp_path):
    passing = _run_history(Path(tmp_path) / "passing", missions=MISSIONS, first=DEMO_GOOD)
    result = learn([passing], base_params=_base(tmp_path), routing=ROUTING)
    assert result.outcome == "no_change" and result.params is None, result.reasons
    r2 = result.manifest["rules"]["R2"]
    assert r2["fired"] is False and r2["groups"]["code"]["failures"] == 0  # enough, and no signal


def test_no_applicable_rule_with_samples_is_insufficient(history, tmp_path):
    flat = RoutingRules(default="small")  # no escalation target: R2 does not apply
    result = learn(
        [history], base_params=resolve_params(_config(tmp_path), routing=flat), routing=flat
    )
    assert result.outcome == "insufficient" and result.params is None
    assert result.manifest["rules"]["R2"]["applicable"] is False


# ------------------------------------------------------------------ R1 (unit)
def test_rule_r1_moves_at_most_two_weights_one_step_on_contested_allocations():
    base = builtin_weights()

    def rows(part, path_value, exploration_value, missions=5, contested=True):
        out = []
        for m in range(missions):
            for on_path, value in ((True, path_value), (False, exploration_value)):
                parts = {name: 0.5 for name in base}
                parts[part] = value
                out.append(
                    {
                        "mission_id": f"m-{part}-{m}",
                        "parts": parts,
                        "on_path": on_path,
                        "contested": contested,
                    }
                )
        return out

    changes, stats = rule_weights(
        rows("waiting_age", 0.1, 0.9)
        + rows("unlock_value", 0.9, 0.2)
        + rows("mission_importance", 0.4, 0.8),
        base,
        min_group=5,
        margin=0.1,
    )
    assert set(changes) == {"waiting_age", "unlock_value"}  # the two largest gaps, at most two
    assert round(changes["waiting_age"], 4) == round(base["waiting_age"] - 0.05, 4)
    assert round(changes["unlock_value"], 4) == round(base["unlock_value"] + 0.05, 4)
    assert stats["fired"] and "uncertainty" not in stats["parts"]  # confounded with retries
    quiet, quiet_stats = rule_weights(
        rows("waiting_age", 0.1, 0.9, contested=False), base, min_group=5, margin=0.1
    )
    assert quiet == {} and quiet_stats["fired"] is False  # uncontested allocations decide nothing
    few, _ = rule_weights(rows("waiting_age", 0.1, 0.9, missions=3), base, min_group=5, margin=0.1)
    assert few == {}  # fewer Missions than min_group
