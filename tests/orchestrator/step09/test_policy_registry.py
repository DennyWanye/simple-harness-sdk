# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 9 · slice A (plan D9-1' / D9-2' / D9-8'; S9-01, S9-02, S9-05, S9-08): the policy
registry — resolved, whitelisted, content-addressed versions; proposals with their
provenance; evaluation → approval → promotion → rollback under a closed state table;
bounded steps, a cooldown and the backpressure gate; every change on the deployment
timeline, and the folded timeline equals the tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_orchestrator.contracts import Budget
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.promotion import (
    DEPLOYMENT_TIMELINE,
    LEGACY_VERSION_ID,
    PROMOTABLE,
    PolicyError,
    code_versions,
    registry_consistency,
    resolve_params,
    task_identity_hash,
    validate_params,
    version_id,
)
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.orchestrator.policy_commits import PolicyCommitError
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage import schema
from agent_orchestrator.storage.store import Store

ALICE = Principal("alice")
BOB = Principal("bob")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _setup(tmp_path, **config):
    clock = Clock()
    store = Store.open(Path(tmp_path) / "orchestrator.db", clock=clock)
    commit = CommitService(store)
    cfg = OrchestratorConfig(evidence_root=Path(tmp_path) / "e", **config)
    seed = commit.seed_policy(resolve_params(cfg), detail={"config": "unit"})
    return clock, store, commit, cfg, seed


def _propose(commit, cfg, n=1, **partial):
    return commit.propose_policy(
        resolve_params(cfg, partial),
        manifest={"kind": "unit", "training": [f"m-{n}"], "learner": "unit-test"},
        source="learner",
    )


def _evaluate(
    commit, store, proposal, verdict="PASSED", *, baseline=None, code=None, kind="real", n=1
):
    return commit.record_policy_evaluation(
        proposal["proposal_id"],
        verdict=verdict,
        reasons=[] if verdict == "PASSED" else [f"unit {verdict}"],
        report_hash=f"report-{proposal['proposal_id']}-{n}",
        baseline_version_id=baseline or store.active_policy()["version_id"],
        code_versions=code or code_versions(),
        evidence_kind=kind,
    )


def _ready(commit, store, proposal, nonce="n-1"):
    _evaluate(commit, store, proposal)
    return commit.decide_policy(
        proposal["proposal_id"], principal=ALICE, decision="approve", nonce=nonce
    )


def _spec(key):
    return MissionSpec(
        goal="写报告",
        success_criteria=("file:REPORT.md",),
        tenant_id="t-9",
        idempotency_key=key,
        budget=Budget(max_tokens=10_000, max_attempts=2),
    )


# ------------------------------------------------------------------ D9-1'
def test_params_are_resolved_whitelisted_and_content_addressed(tmp_path):
    cfg = OrchestratorConfig(evidence_root=Path(tmp_path), max_concurrency=2)
    full = resolve_params(cfg)
    assert set(full) == set(PROMOTABLE) and full["candidates_per_task"] == 1
    assert version_id(full) == version_id(resolve_params(cfg, {"candidates_per_task": 1}))
    assert version_id(full) != version_id(resolve_params(cfg, {"candidates_per_task": 2}))
    for core in (
        {"deployment_policy": {}},
        {"hard_cap_micros": 1},
        {"ablations": ["critic"]},
        {"budgets": {}},
        {"code_test": False},
    ):
        with pytest.raises(PolicyError, match="not promotable"):
            resolve_params(cfg, core)
    problems = validate_params(
        resolve_params(
            cfg,
            {
                "allocator_weights": {"mission_importance": 0.9},
                "mission_concurrency": 3,
                "prompt_versions": {"worker": "worker-v99"},
            },
        ),
        base=full,
        max_concurrency=2,
    )
    joined = " ".join(problems)
    assert (
        "mission_importance" in joined
        and "mission_concurrency" in joined
        and "worker-v99" in joined
    )
    assert (
        validate_params(
            resolve_params(cfg, {"candidates_per_task": 2}), base=full, max_concurrency=2
        )
        == []
    )


def test_the_task_identity_ignores_who_asked_and_under_which_key():
    a = {"goal": "g", "success_criteria": ["file:x"], "tenant_id": "t1", "idempotency_key": "k1"}
    b = {**a, "tenant_id": "t2", "idempotency_key": "eval:p:s:c:1"}
    assert task_identity_hash(a) == task_identity_hash(b)
    assert task_identity_hash(a) != task_identity_hash({**a, "goal": "other"})


# ------------------------------------------------------------------ S9-01 (registry half)
def test_s9_01_the_same_input_is_the_same_proposal_and_one_version_has_many_proposals(tmp_path):
    _clock, store, commit, cfg, seed = _setup(tmp_path)
    assert seed["status"] == "ACTIVE" and store.active_policy()["version_id"] == seed["version_id"]
    first = _propose(commit, cfg, 1, candidates_per_task=2)
    again = _propose(commit, cfg, 1, candidates_per_task=2)
    other = _propose(commit, cfg, 2, candidates_per_task=2)
    assert first["proposal_id"] == again["proposal_id"] and again["created"] is False
    assert (
        other["proposal_id"] != first["proposal_id"] and other["version_id"] == first["version_id"]
    )
    version = store.get_policy_version(first["version_id"])
    assert version["params"]["candidates_per_task"] == 2 and version["status"] == "NEVER_ACTIVE"
    assert version["interpreter_versions"]["allocator"] == "allocator-v1"
    stored = store.get_policy_proposal(first["proposal_id"])
    assert stored["manifest"]["training"] == ["m-1"] and stored["state"] == "PROPOSED"
    assert any(d["key"] == "candidates_per_task" for d in stored["diff"])


# ------------------------------------------------------------------ S9-02 (registry half)
@pytest.mark.parametrize("verdict", ["FAILED", "INSUFFICIENT"])
def test_s9_02_a_proposal_that_did_not_pass_is_never_approved_or_promoted(tmp_path, verdict):
    _clock, store, commit, cfg, seed = _setup(tmp_path)
    proposal = _propose(commit, cfg, candidates_per_task=2)
    _evaluate(commit, store, proposal, verdict)
    stored = store.get_policy_proposal(proposal["proposal_id"])
    assert stored["state"] == verdict and stored["last_evaluation"]["reasons"] == [
        f"unit {verdict}"
    ]
    with pytest.raises(PolicyCommitError, match="PASSED"):
        commit.decide_policy(
            proposal["proposal_id"], principal=ALICE, decision="approve", nonce="n"
        )
    with pytest.raises(PolicyCommitError, match="APPROVED"):
        commit.promote_policy(proposal["proposal_id"], principal=ALICE, cooldown_seconds=0)
    assert store.active_policy()["version_id"] == seed["version_id"]  # the old policy keeps running


def test_the_proposal_state_table_voids_an_approval_on_re_evaluation(tmp_path):
    clock, store, commit, cfg, _seed = _setup(tmp_path)
    proposal = _propose(commit, cfg, candidates_per_task=2)
    _ready(commit, store, proposal, "n-1")
    _evaluate(commit, store, proposal, n=2)  # a newer evaluation: the approval no longer holds
    assert store.get_policy_proposal(proposal["proposal_id"])["state"] == "PASSED"
    with pytest.raises(PolicyCommitError, match="APPROVED"):
        commit.promote_policy(proposal["proposal_id"], principal=ALICE, cooldown_seconds=0)
    commit.decide_policy(proposal["proposal_id"], principal=BOB, decision="approve", nonce="n-2")
    activation = commit.promote_policy(proposal["proposal_id"], principal=BOB, cooldown_seconds=0)
    assert (
        activation["action"] == "promote"
        and store.get_policy_proposal(proposal["proposal_id"])["state"] == "PROMOTED"
    )
    with pytest.raises(PolicyCommitError, match="PROMOTED"):
        _evaluate(commit, store, proposal, n=3)
    rejected = _propose(commit, cfg, 3, exploration_slots=2)
    _evaluate(commit, store, rejected)
    commit.decide_policy(
        rejected["proposal_id"], principal=ALICE, decision="reject", nonce="n-3", note="不要"
    )
    assert store.get_policy_proposal(rejected["proposal_id"])["state"] == "REJECTED"
    with pytest.raises(PolicyCommitError, match="REJECTED"):
        _evaluate(commit, store, rejected, n=4)
    clock.now += 1


def test_promotion_needs_the_current_baseline_the_current_code_and_real_evidence(tmp_path):
    clock, store, commit, cfg, seed = _setup(tmp_path)
    stale = _propose(commit, cfg, 1, candidates_per_task=2)
    _evaluate(commit, store, stale, baseline="policy-0000000000000000")
    commit.decide_policy(stale["proposal_id"], principal=ALICE, decision="approve", nonce="a")
    with pytest.raises(PolicyCommitError, match="baseline"):
        commit.promote_policy(stale["proposal_id"], principal=ALICE, cooldown_seconds=0)
    old_code = _propose(commit, cfg, 2, candidates_per_task=2)
    _evaluate(
        commit, store, old_code, code={"agent_orchestrator": "0.0.1", "simple_harness": "0.0.1"}
    )
    commit.decide_policy(old_code["proposal_id"], principal=ALICE, decision="approve", nonce="b")
    with pytest.raises(PolicyCommitError, match="code"):
        commit.promote_policy(old_code["proposal_id"], principal=ALICE, cooldown_seconds=0)
    commit.create_mission(
        _spec("real-1"), provider_kind="real"
    )  # this deployment has run a real model
    fixture = _propose(commit, cfg, 3, candidates_per_task=2)
    _evaluate(commit, store, fixture, kind="fixture")
    commit.decide_policy(fixture["proposal_id"], principal=ALICE, decision="approve", nonce="c")
    with pytest.raises(PolicyCommitError, match="fixture"):
        commit.promote_policy(fixture["proposal_id"], principal=ALICE, cooldown_seconds=0)
    activation = commit.promote_policy(
        fixture["proposal_id"], principal=ALICE, cooldown_seconds=0, accept_fixture_evidence=True
    )
    [promoted] = [e for e in store.iter_events(DEPLOYMENT_TIMELINE) if e.type == "PolicyPromoted"]
    assert (
        promoted.payload["accept_fixture_evidence"] is True
        and activation["version_id"] != seed["version_id"]
    )
    clock.now += 1


# ------------------------------------------------------------------ S9-08 (registry half)
def test_s9_08_step_cooldown_and_backpressure_bound_every_promotion(tmp_path):
    clock, store, commit, cfg, _seed = _setup(tmp_path)
    leap = _propose(commit, cfg, 1, allocator_weights={"mission_importance": 0.45})
    _ready(commit, store, leap, "l")
    with pytest.raises(PolicyCommitError, match="moves"):
        commit.promote_policy(leap["proposal_id"], principal=ALICE, cooldown_seconds=600)
    first = _propose(commit, cfg, 2, allocator_weights={"mission_importance": 0.35})
    _ready(commit, store, first, "f")
    commit.promote_policy(first["proposal_id"], principal=ALICE, cooldown_seconds=600)
    clock.now += 100
    second = _propose(
        commit, cfg, 3, allocator_weights={"mission_importance": 0.35}, no_progress_limit=3
    )
    _ready(commit, store, second, "s")
    with pytest.raises(PolicyCommitError, match="cooldown"):
        commit.promote_policy(second["proposal_id"], principal=ALICE, cooldown_seconds=600)
    clock.now += 600
    store.put_scheduler_state("backpressure", {"level": "RAISED"})
    wider = _propose(
        commit, cfg, 4, allocator_weights={"mission_importance": 0.35}, candidates_per_task=2
    )
    _ready(commit, store, wider, "w")
    with pytest.raises(PolicyCommitError, match="backpressure"):
        commit.promote_policy(wider["proposal_id"], principal=ALICE, cooldown_seconds=600)
    commit.promote_policy(
        second["proposal_id"], principal=ALICE, cooldown_seconds=600
    )  # not widening
    assert store.active_policy()["params"]["no_progress_limit"] == 3


# ------------------------------------------------------------------ S9-05 (registry half)
def test_s9_05_rollback_goes_to_the_last_retired_version_at_once(tmp_path):
    clock, store, commit, cfg, seed = _setup(tmp_path)
    a = _propose(commit, cfg, 1, no_progress_limit=3)
    _ready(commit, store, a, "a")
    commit.promote_policy(a["proposal_id"], principal=ALICE, cooldown_seconds=600)
    clock.now += 700
    b = _propose(commit, cfg, 2, no_progress_limit=3, max_manager_rounds=5)
    _ready(commit, store, b, "b")
    commit.promote_policy(b["proposal_id"], principal=ALICE, cooldown_seconds=600)
    mission, _created = commit.create_mission(_spec("under-b"))
    assert store.get_mission_policy(mission.id)["version_id"] == b["version_id"]
    clock.now += 1  # no cooldown for a rollback
    back = commit.rollback_policy(principal=BOB, reason="失败率上升")
    assert back["version_id"] == a["version_id"] and back["from_version_id"] == b["version_id"]
    assert back["still_bound"] == [mission.id]  # in-flight Missions keep the version they had
    assert store.get_policy_version(b["version_id"])["status"] == "ROLLED_BACK"
    assert store.get_mission_policy(mission.id)["version_id"] == b["version_id"]
    again = commit.rollback_policy(principal=BOB, reason="再退一步")
    assert again["version_id"] == seed["version_id"]  # ROLLED_BACK versions are skipped
    with pytest.raises(PolicyCommitError, match="RETIRED"):
        commit.rollback_policy(principal=BOB, reason="无处可退")
    with pytest.raises(PolicyCommitError, match="RETIRED"):
        commit.rollback_policy(principal=BOB, to=b["version_id"], reason="回到坏版本")


def test_repeated_activations_keep_every_event_and_the_timeline_folds_to_the_tables(tmp_path):
    clock, store, commit, cfg, seed = _setup(tmp_path)
    for round_ in (1, 2):
        proposal = _propose(commit, cfg, round_, no_progress_limit=3)  # the same parameters twice
        _ready(commit, store, proposal, f"n-{round_}")
        commit.promote_policy(proposal["proposal_id"], principal=ALICE, cooldown_seconds=600)
        clock.now += 10
        commit.rollback_policy(principal=ALICE, reason=f"第 {round_} 次回滚")
        clock.now += 700
    kinds = [e.type for e in store.iter_events(DEPLOYMENT_TIMELINE)]
    assert kinds.count("PolicyPromoted") == 2 and kinds.count("PolicyRolledBack") == 2
    assert [a["action"] for a in store.list_policy_activations()] == [
        "seed",
        "promote",
        "rollback",
        "promote",
        "rollback",
    ]
    assert store.active_policy()["version_id"] == seed["version_id"]
    assert registry_consistency(store) == []


def test_only_a_person_decides_and_a_nonce_is_used_once(tmp_path):
    _clock, store, commit, cfg, _seed = _setup(tmp_path)
    proposal = _propose(commit, cfg, candidates_per_task=2)
    _evaluate(commit, store, proposal)
    with pytest.raises(ValueError, match="human"):
        Principal("worker-7", kind="agent")
    with pytest.raises(PolicyCommitError, match="Principal"):
        commit.decide_policy(
            proposal["proposal_id"], principal="worker-7", decision="approve", nonce="x"
        )
    commit.decide_policy(proposal["proposal_id"], principal=ALICE, decision="approve", nonce="once")
    _evaluate(commit, store, proposal, n=2)
    with pytest.raises(PolicyCommitError, match="nonce"):
        commit.decide_policy(
            proposal["proposal_id"], principal=ALICE, decision="approve", nonce="once"
        )


# ------------------------------------------------------------------ v5 → v6 (P1-2)
def test_a_v5_library_binds_its_missions_to_legacy_and_keeps_them_runnable(tmp_path):
    path = Path(tmp_path) / "orchestrator.db"
    connection = sqlite3.connect(path)
    for migration in schema.MIGRATIONS[:5]:
        for statement in migration.ddl.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT INTO orch_schema_migrations VALUES (?,?,?,?)",
            (migration.version, migration.name, migration.checksum, 1.0),
        )
    connection.execute(
        "INSERT INTO missions(mission_id,tenant_id,idempotency_key,status,version,spec_hash,json,created_at,updated_at)"
        " VALUES ('m1','t','k','ACTIVE',1,'h','{}',1.0,1.0)"
    )
    connection.commit()
    connection.close()
    store = Store.open(path)
    binding = store.get_mission_policy("m1")
    assert binding["version_id"] == LEGACY_VERSION_ID and binding["source"] == "legacy"
    assert binding["provider_kind"] == "unknown"
    legacy = store.get_policy_version(LEGACY_VERSION_ID)
    assert (
        legacy["status"] == "LEGACY" and legacy["params"] is None
    )  # "follows the deployment config"
    assert store.active_policy() is None  # legacy is never ACTIVE
    assert (Path(tmp_path) / f"orchestrator.db.pre-schema-{schema.SCHEMA_VERSION}.backup").is_file()
    store.close()


def test_review_p2_9_the_single_writer_refuses_a_partial_or_foreign_policy(tmp_path):
    _clock, store, commit, cfg, _seed = _setup(tmp_path)
    with pytest.raises(PolicyCommitError, match="exactly"):
        commit.propose_policy({"candidates_per_task": 2}, manifest={"kind": "unit"}, source="unit")
    with pytest.raises(PolicyCommitError, match="exactly"):
        commit.propose_policy(
            {**resolve_params(cfg), "hard_cap_micros": 1}, manifest={"kind": "unit"}, source="unit"
        )
    assert store.list_policy_proposals() == []
