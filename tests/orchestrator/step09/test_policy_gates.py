# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 9 · slice D (plan D9-6' / D9-7'; S9-02, S9-03, S9-07): a candidate is evaluated
against the ACTIVE version on held-out cases, each side pinned into its own new
evaluation libraries; the gates follow the step-8 statistics; a failing or insufficient
candidate stays out, a passing one still only runs in evaluation libraries until a
person approves it; a case that leaks from the training set is refused before it runs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fixtures_provider import RoleScriptedProvider, critic_step, proposal_step

from agent_orchestrator.contracts import Budget
from agent_orchestrator.governance.gates import evaluate_candidate
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.promotion import resolve_params, spec_task_identity
from agent_orchestrator.observability.evaluation import EvaluationCase, EvaluationRefused
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.orchestrator.policy_commits import PolicyCommitError
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

ALICE = Principal("alice")
TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")
ROUTING = RoutingRules(default="small", escalate={"small": "large"}, escalate_after_failures=1)
HOLDOUT_GOAL = "实现 parse_kv（留出评测 case），并通过 tests/test_parse_kv.py"


def _holdout(tenant, key, goal=HOLDOUT_GOAL):
    return MissionSpec(
        goal=goal,
        success_criteria=("pytest:tests/test_parse_kv.py",),
        tenant_id=tenant,
        idempotency_key=key,
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=400_000, max_attempts=4),
        workspace_seed=DEMO_SEED,
    )


def _profiles():
    """small fails a first attempt, large passes, flaky always fails; the Planner and
    the Critic run on the default (small) profile."""

    critic = [critic_step(verdict="PASS", criteria_met=True)] * 8
    return {
        "small": RuntimeProfile(
            "small",
            RoleScriptedProvider(
                {
                    "planner": [proposal_step(DEMO_PROPOSAL)],
                    "worker": demo_worker_script(DEMO_BAD) * 4,
                    "critic": critic,
                },
                model="model-small",
            ),
            "model-small",
        ),
        "large": RuntimeProfile(
            "large",
            RoleScriptedProvider(
                {"worker": demo_worker_script(DEMO_GOOD) * 4}, model="model-large"
            ),
            "model-large",
        ),
        "flaky": RuntimeProfile(
            "flaky",
            RoleScriptedProvider({"worker": demo_worker_script(DEMO_BAD) * 4}, model="model-flaky"),
            "model-flaky",
        ),
    }


def _case(name="holdout", kind="fixtures", **extra):
    return EvaluationCase(
        name,
        _holdout,
        provider=lambda: None,
        kind=kind,
        profiles=_profiles,
        routing=ROUTING,
        **extra,
    )


def _registry(tmp_path):
    config = OrchestratorConfig(evidence_root=Path(tmp_path) / "prod", max_concurrency=1)
    store = Store.open(config.orchestrator_db)
    commit = CommitService(store)
    commit.set_library_role("production")
    commit.seed_policy(resolve_params(config, routing=ROUTING))
    return config, store, commit


def _propose(commit, config, manifest=None, **partial):
    return commit.propose_policy(
        resolve_params(config, partial, routing=ROUTING),
        manifest=manifest or {"kind": "human", "note": "unit"},
        source="human:alice",
    )


# ------------------------------------------------------------------ S9-03
def test_s9_03_a_passing_candidate_runs_only_in_evaluation_libraries(tmp_path):
    config, store, commit = _registry(tmp_path)
    seed = store.active_policy()
    proposal = _propose(commit, config, routing={"by_task_kind": {"code": "large"}})
    outcome = evaluate_candidate(
        commit,
        proposal["proposal_id"],
        cases=[_case()],
        directory=Path(tmp_path) / "gate",
        trials=1,
    )
    assert outcome["verdict"] == "PASSED", outcome["reasons"]
    assert outcome["evidence_kind"] == "fixture"
    stored = store.get_policy_proposal(proposal["proposal_id"])
    assert (
        stored["state"] == "PASSED"
        and stored["last_evaluation"]["baseline_version_id"] == seed["version_id"]
    )
    assert "非劣" in stored["last_evaluation"]["summary"]["note"]
    runs = sorted((Path(tmp_path) / "gate").glob("runs/*/*/*/orchestrator.db"))
    assert len(runs) == 2
    for library in runs:  # shadow: every run in its own evaluation library, pinned
        connection = sqlite3.connect(library)
        role = connection.execute(
            "SELECT json FROM scheduler_state WHERE key = 'library_role'"
        ).fetchone()[0]
        [(source, bound)] = connection.execute(
            "SELECT source, version_id FROM mission_policies"
        ).fetchall()
        connection.close()
        assert "evaluation" in role and source == "sandbox"
        side = library.parts[-4]
        expected = proposal["version_id"] if side == "candidate" else seed["version_id"]
        assert bound == expected
    assert store.list_mission_policies() == []  # nothing ran in production
    mission, _created = commit.create_mission(_holdout("prod", "after-evaluation"))
    assert (
        store.get_mission_policy(mission.id)["version_id"] == seed["version_id"]
    )  # not the candidate
    with pytest.raises(PolicyCommitError, match="APPROVED"):
        commit.promote_policy(proposal["proposal_id"], principal=ALICE, cooldown_seconds=0)
    assert (Path(tmp_path) / "gate" / "gate.json").is_file()
    store.close()


# ------------------------------------------------------------------ S9-02
def test_s9_02_a_candidate_that_fails_the_gates_stays_out(tmp_path):
    config, store, commit = _registry(tmp_path)
    seed = store.active_policy()
    proposal = _propose(commit, config, routing={"by_task_kind": {"code": "flaky"}})
    outcome = evaluate_candidate(
        commit,
        proposal["proposal_id"],
        cases=[_case()],
        directory=Path(tmp_path) / "gate",
        trials=1,
    )
    assert outcome["verdict"] == "FAILED"
    assert any(reason.startswith("质量") for reason in outcome["reasons"]), outcome["reasons"]
    assert store.get_policy_proposal(proposal["proposal_id"])["state"] == "FAILED"
    with pytest.raises(PolicyCommitError, match="PASSED"):
        commit.decide_policy(
            proposal["proposal_id"], principal=ALICE, decision="approve", nonce="n"
        )
    assert store.active_policy()["version_id"] == seed["version_id"]  # the old policy keeps running
    store.close()


def test_thin_samples_or_a_broken_harness_are_insufficient_never_failed(tmp_path):
    config, store, commit = _registry(tmp_path)
    proposal = _propose(commit, config, routing={"by_task_kind": {"code": "large"}})
    thin = evaluate_candidate(
        commit,
        proposal["proposal_id"],
        cases=[_case(kind="env")],
        directory=Path(tmp_path) / "thin",
        trials=1,
    )
    assert thin["verdict"] == "INSUFFICIENT" and "至少 3" in thin["reasons"][0]
    broken = EvaluationCase(
        "broken",
        _holdout,
        provider=lambda: RoleScriptedProvider({}),  # no script: the run can only time out
    )
    outcome = evaluate_candidate(
        commit,
        proposal["proposal_id"],
        cases=[broken],
        directory=Path(tmp_path) / "broken",
        trials=1,
        timeout_seconds=8,
    )
    assert outcome["verdict"] == "INSUFFICIENT" and "脚手架错误" in outcome["reasons"][0]
    assert store.get_policy_proposal(proposal["proposal_id"])["state"] == "INSUFFICIENT"
    store.close()


# ------------------------------------------------------------------ S9-07 (leakage)
def test_s9_07_a_case_that_leaks_from_the_training_set_is_refused_before_it_runs(tmp_path):
    config, store, commit = _registry(tmp_path)
    identity = spec_task_identity(_holdout("history", "an-old-key"))  # same task, other key
    learned = _propose(
        commit,
        config,
        manifest={
            "kind": "rule_improvement",
            "training": [
                {"mission_id": "mission-old", "task_identity": identity, "created_at": 10.0}
            ],
            "window": {"start": 10.0, "end": 20.0},
        },
        routing={"by_task_kind": {"code": "large"}},
    )
    with pytest.raises(EvaluationRefused, match="任务身份相同"):
        evaluate_candidate(
            commit, learned["proposal_id"], cases=[_case()], directory=Path(tmp_path) / "a"
        )
    other_task = EvaluationCase(
        "derived",
        lambda tenant, key: _holdout(tenant, key, goal="另一个任务：实现 parse_kv 的变体"),
        provider=lambda: None,
        profiles=_profiles,
        routing=ROUTING,
        derived_from={"mission_id": "mission-old", "created_at": 15.0},
    )
    with pytest.raises(EvaluationRefused, match="派生自训练集") as refused:
        evaluate_candidate(
            commit, learned["proposal_id"], cases=[other_task], directory=Path(tmp_path) / "b"
        )
    assert "不晚于训练窗口末端" in str(refused.value)
    assert (
        not (Path(tmp_path) / "a").exists() and not (Path(tmp_path) / "b").exists()
    )  # nothing ran
    assert store.get_policy_proposal(learned["proposal_id"])["state"] == "PROPOSED"
    store.close()


# ------------------------------------------------------------------ code review round 1
def test_review_p2_3_p2_8_evaluation_libraries_are_consistent_and_closed_proposals_are_not_run(
    tmp_path,
):
    from agent_orchestrator.governance.promotion import code_versions, registry_consistency

    config, store, commit = _registry(tmp_path)
    proposal = _propose(commit, config, routing={"by_task_kind": {"code": "large"}})
    evaluate_candidate(
        commit, proposal["proposal_id"], cases=[_case()], directory=Path(tmp_path) / "gate"
    )
    for library in sorted((Path(tmp_path) / "gate").glob("runs/*/*/*/orchestrator.db")):
        sandbox = Store.open(library)
        assert registry_consistency(sandbox) == []  # a pinned version is not a promoted one
        sandbox.close()
    commit.decide_policy(
        proposal["proposal_id"], principal=ALICE, decision="reject", nonce="r", note="不用"
    )
    with pytest.raises(EvaluationRefused, match="not evaluated again"):
        evaluate_candidate(
            commit, proposal["proposal_id"], cases=[_case()], directory=Path(tmp_path) / "again"
        )
    assert not (Path(tmp_path) / "again").exists()  # refused before anything ran
    assert code_versions()  # (the registry records the code of every evaluation)
    store.close()
