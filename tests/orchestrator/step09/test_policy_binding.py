# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 9 · slice B (plan D9-3' / D9-4'; S9-04, S9-05): every Mission is bound to one
policy version when it is created and runs under that version to the end — a later
promotion or another deployment configuration never changes it silently; a rollback
leaves the events and costs of the rolled-back version exactly as they were; evaluation
pins stay out of production libraries."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import pytest
from fixtures_provider import RoleScriptedProvider, critic_step, proposal_step

import agent_orchestrator.orchestrator.event_handler as event_handler
from agent_orchestrator.contracts import Budget, MissionStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.promotion import (
    DEPLOYMENT_TIMELINE,
    code_versions,
    resolve_params,
    version_id,
    weights_hash,
)
from agent_orchestrator.observability.replay import replay_mission
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    DEMO_GOOD,
    DEMO_PROPOSAL,
    DEMO_SEED,
    demo_worker_script,
)

ALICE = Principal("alice")
TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")


def _spec(key):
    return MissionSpec(
        goal="在隔离工作区实现字符串解析函数 parse_kv，并通过 tests/test_parse_kv.py",
        success_criteria=("pytest:tests/test_parse_kv.py",),
        tenant_id="tenant-9",
        idempotency_key=key,
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=300_000, max_attempts=4),
        workspace_seed=DEMO_SEED,
    )


def _provider(missions=1, spare=1):
    """Scripts for ``missions`` Missions plus ``spare`` extra Worker runs, so a policy
    bug that starts an extra candidate fails an assertion instead of hanging."""

    return RoleScriptedProvider(
        {
            "planner": [proposal_step(DEMO_PROPOSAL)] * missions,
            "worker": demo_worker_script(DEMO_GOOD) * (missions + spare),
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * (missions + spare),
        }
    )


def _config(root, **overrides):
    values = {"max_concurrency": 2, "test_timeout_seconds": 60, **overrides}
    return OrchestratorConfig(evidence_root=Path(root) / "evidence", **values)


def _promote(commit, store, config, nonce, **partial):
    active = store.active_policy()
    proposal = commit.propose_policy(
        resolve_params(config, partial),
        manifest={"kind": "unit", "nonce": nonce},
        source="human:alice",
    )
    commit.record_policy_evaluation(
        proposal["proposal_id"],
        verdict="PASSED",
        reasons=[],
        report_hash=f"report-{nonce}",
        baseline_version_id=active["version_id"],
        code_versions=code_versions(),
        evidence_kind="fixture",
    )
    commit.decide_policy(proposal["proposal_id"], principal=ALICE, decision="approve", nonce=nonce)
    return commit.promote_policy(
        proposal["proposal_id"], principal=ALICE, cooldown_seconds=0, accept_fixture_evidence=True
    )


def _only_attempt(store, mission_id):
    [task] = store.list_tasks(mission_id)
    return store.list_attempts(task.id)


# ------------------------------------------------------------------ S9-04
def test_s9_04_a_new_mission_runs_under_the_active_version(tmp_path):
    config = _config(tmp_path)

    async def case():
        async with Orchestrator(config, _provider()) as orch:
            store = orch.store
            seed = store.active_policy()
            assert seed["source"] == "seed" and seed["detail"]["config_hash"]
            promoted = _promote(
                orch.commit,
                store,
                config,
                "p1",
                allocator_weights={"mission_importance": 0.25},
                no_progress_limit=3,
            )
            mission = await orch.submit_mission(_spec("new"))
            await orch.run()
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orch.progress_log
            )
            binding = store.get_mission_policy(mission.id)
            assert binding["version_id"] == promoted["version_id"] and binding["source"] == "active"
            assert binding["provider_kind"] == "fixtures"
            [attempt] = _only_attempt(store, mission.id)
            intent = store.get_intent_for_subject(attempt.id)
            params = store.get_policy_version(promoted["version_id"])["params"]
            assert intent.config["policy_version_id"] == promoted["version_id"]
            allocation = intent.config["allocation"]
            assert allocation["weights_hash"] == weights_hash(params["allocator_weights"])
            assert allocation["policy_version_id"] == promoted["version_id"]
            created = next(e for e in store.iter_events(mission.id) if e.type == "MissionCreated")
            assert created.payload["policy_version_id"] == promoted["version_id"]
            decided = [e for e in store.iter_events(mission.id) if e.type == "AllocationDecided"]
            assert decided and all(
                e.payload["weights_hash"] == allocation["weights_hash"] for e in decided
            )

    asyncio.run(case())


def test_s9_04_an_in_flight_mission_keeps_its_version_when_the_deployment_changes(tmp_path):
    first = _config(tmp_path, candidates_per_task=1, no_progress_limit=2)
    provider = _provider(missions=2, spare=2)

    async def case():
        async with Orchestrator(first, provider) as orch:
            old = await orch.submit_mission(_spec("in-flight"))  # created, not yet run
            seed_id = orch.store.get_mission_policy(old.id)["version_id"]
            promoted = _promote(orch.commit, orch.store, first, "p2", no_progress_limit=3)
        second = _config(tmp_path, candidates_per_task=2, no_progress_limit=5, exploration_slots=0)
        async with Orchestrator(second, provider) as orch:  # a new instance, another configuration
            store = orch.store
            new = await orch.submit_mission(_spec("after"))
            assert orch.policy_for(old.id)["candidates_per_task"] == 1
            assert orch.policy_for(old.id)["no_progress_limit"] == 2
            assert orch.policy_for(new.id)["no_progress_limit"] == 3  # the ACTIVE version …
            assert orch.policy_for(new.id)["candidates_per_task"] == 1  # … not the configuration
            drift = [
                e for e in store.iter_events(DEPLOYMENT_TIMELINE) if e.type == "PolicyConfigDrift"
            ]
            assert drift, "the configuration differs from the ACTIVE version and says so"
            keys = {d["key"] for d in drift[-1].payload["differences"]}
            assert {"candidates_per_task", "no_progress_limit", "exploration_slots"} <= keys
            await orch.run()
            for mission, version in ((old, seed_id), (new, promoted["version_id"])):
                assert store.get_mission(mission.id).status is MissionStatus.COMPLETED
                attempts = _only_attempt(store, mission.id)
                assert len(attempts) == 1  # one candidate, as the bound version says
                intent = store.get_intent_for_subject(attempts[0].id)
                assert intent.config["policy_version_id"] == version
                assert intent.config["allocation"]["candidates_per_task"] == 1
                assert intent.config["allocation"]["concurrency_limit"] == 2

    asyncio.run(case())


def test_no_decision_point_reads_a_whitelisted_value_from_the_configuration():
    """Plan D9-4' (review P1-1): the decision points go through the Mission's bound
    policy — structurally, no whitelisted item is read from ``self._config`` any more."""

    source = Path(event_handler.__file__).read_text(encoding="utf-8")
    pattern = re.compile(
        r"self\._config\.(candidates_per_task|exploration_slots|manager_after_failures"
        r"|no_progress_limit|max_manager_rounds|aging_window_seconds)\b"
    )
    assert pattern.findall(source) == []
    assert "self._model_router.route(" not in source  # routing goes through the Mission's router
    for constant in ("PLANNER.prompt_version", "MANAGER.prompt_version", "CRITIC.prompt_version"):
        assert constant not in source
    assert source.count("role_for_task(") == source.count("template_for(role_for_task(")


# ------------------------------------------------------------------ S9-05 (binding half)
def test_s9_05_a_rollback_leaves_the_events_and_costs_of_the_rolled_back_version(tmp_path):
    config = _config(tmp_path)

    async def case():
        async with Orchestrator(config, _provider(missions=2)) as orch:
            store = orch.store
            seed = store.active_policy()
            bad = _promote(orch.commit, store, config, "bad", no_progress_limit=3)
            mission = await orch.submit_mission(_spec("under-bad"))
            await orch.run()
            events = [e.to_json() for e in store.iter_events(mission.id)]

            def usage():
                return store.connection.execute(
                    "SELECT SUM(input_tokens + output_tokens), COUNT(*) FROM imported_usage WHERE mission_id = ?",
                    (mission.id,),
                ).fetchone()

            before = usage()
            receipt = orch.commit.rollback_policy(principal=ALICE, reason="失败率上升")
            assert receipt["version_id"] == seed["version_id"] and receipt["still_bound"] == []
            assert [e.to_json() for e in store.iter_events(mission.id)] == events
            assert usage() == before and before[0] > 0
            assert store.get_mission_policy(mission.id)["version_id"] == bad["version_id"]
            after = await orch.submit_mission(_spec("after-rollback"))
            assert store.get_mission_policy(after.id)["version_id"] == seed["version_id"]
            return mission.id, bad["version_id"]

    mission_id, bad_id = asyncio.run(case())
    report = replay_mission(
        mission_id=mission_id, library=Path(tmp_path) / "evidence" / "orchestrator.db"
    )
    assert report["comparison"]["mismatches"] == [] and report["comparison"]["coverage"] == 1.0
    assert report["formal_state"]["mission"][mission_id]["policy_version_id"] == bad_id


# ------------------------------------------------------------------ library roles (P1-6)
def test_the_library_role_keeps_evaluation_pins_out_of_production(tmp_path):
    pinned = resolve_params(_config(tmp_path), {"no_progress_limit": 3})
    evaluation = _config(Path(tmp_path) / "eval")
    production = _config(Path(tmp_path) / "prod")

    async def case():
        async with Orchestrator(evaluation, _provider(), policy_pin=pinned) as orch:
            mission = await orch.submit_mission(_spec("sandbox"))
            binding = orch.store.get_mission_policy(mission.id)
            assert binding["source"] == "sandbox" and binding["version_id"] == version_id(pinned)
            assert orch.store.active_policy() is None  # an evaluation library has no ACTIVE policy
        with pytest.raises(ValueError, match="evaluation library"):
            async with Orchestrator(evaluation, _provider()):
                pass
        async with Orchestrator(production, _provider()):
            pass
        with pytest.raises(ValueError, match="production library"):
            async with Orchestrator(production, _provider(), policy_pin=pinned):
                pass

    asyncio.run(case())


# ------------------------------------------------------------------ interpreter drift (P1-1)
def test_a_resumed_mission_says_when_the_code_that_reads_its_policy_changed(tmp_path):
    config = _config(tmp_path)

    async def case():
        async with Orchestrator(config, _provider()) as orch:
            mission = await orch.submit_mission(_spec("drift"))
            bound = orch.store.get_mission_policy(mission.id)["version_id"]
        connection = sqlite3.connect(
            config.orchestrator_db
        )  # an older build recorded another allocator
        row = connection.execute(
            "SELECT json FROM policy_versions WHERE version_id = ?", (bound,)
        ).fetchone()
        record = json.loads(row[0])
        record["interpreter_versions"]["allocator"] = "allocator-v0"
        connection.execute(
            "UPDATE policy_versions SET json = ? WHERE version_id = ?", (json.dumps(record), bound)
        )
        connection.commit()
        connection.close()
        async with Orchestrator(config, _provider()) as orch:
            await orch.run()
            drift = [
                e for e in orch.store.iter_events(mission.id) if e.type == "PolicyInterpreterDrift"
            ]
            assert len(drift) == 1
            assert drift[0].payload["differences"] == [
                {"key": "allocator", "bound": "allocator-v0", "running": "allocator-v1"}
            ]
            assert orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED

    asyncio.run(case())


# ------------------------------------------------------------------ code review round 1
def test_review_p1_1_a_mission_older_than_policy_binding_still_replays_completely(tmp_path):
    """A Mission migrated to ``policy-legacy`` has no ``policy_version_id`` in its
    ``MissionCreated``: the field is not formal state its events decide, so replay
    coverage stays 100 % after the upgrade."""

    config = _config(tmp_path)

    async def case():
        async with Orchestrator(config, _provider()) as orch:
            mission = await orch.submit_mission(_spec("pre-binding"))
            await orch.run()
            return mission.id

    mission_id = asyncio.run(case())
    connection = sqlite3.connect(config.orchestrator_db)  # make it look like a v5 Mission, migrated
    connection.execute(
        "INSERT INTO policy_versions(version_id,params_hash,source,status,json,created_at,updated_at)"
        " VALUES ('policy-legacy','legacy','legacy','LEGACY',?,1.0,1.0)",
        (
            json.dumps(
                {
                    "version_id": "policy-legacy",
                    "params": None,
                    "source": "legacy",
                    "status": "LEGACY",
                }
            ),
        ),
    )
    connection.execute(
        "UPDATE mission_policies SET version_id='policy-legacy', source='legacy', json=? WHERE mission_id=?",
        (
            json.dumps(
                {
                    "mission_id": mission_id,
                    "version_id": "policy-legacy",
                    "source": "legacy",
                    "provider_kind": "unknown",
                }
            ),
            mission_id,
        ),
    )
    event_id, payload = connection.execute(
        "SELECT event_id, payload_json FROM events WHERE type='MissionCreated' AND mission_id=?",
        (mission_id,),
    ).fetchone()
    old_payload = {k: v for k, v in json.loads(payload).items() if k != "policy_version_id"}
    connection.execute(
        "UPDATE events SET payload_json=? WHERE event_id=?", (json.dumps(old_payload), event_id)
    )
    connection.commit()
    connection.close()
    report = replay_mission(mission_id=mission_id, library=config.orchestrator_db)
    assert report["comparison"]["coverage"] == 1.0 and report["comparison"]["mismatches"] == []
    assert "policy_version_id" not in report["formal_state"]["mission"][mission_id]


def test_review_p2_10_a_policy_switches_the_prompt_version_at_run_time(tmp_path):
    from agent_orchestrator.runtime.role_templates import (
        TEMPLATE_VERSIONS,
        WORKER,
        RoleTemplate,
        register_template,
    )

    register_template(
        RoleTemplate("worker", "worker-v2-drill", WORKER.instructions, WORKER.tool_names)
    )
    config = _config(tmp_path)
    try:

        async def case():
            async with Orchestrator(config, _provider()) as orch:
                store = orch.store
                promoted = _promote(
                    orch.commit,
                    store,
                    config,
                    "prompt",
                    prompt_versions={"worker": "worker-v2-drill"},
                )
                mission = await orch.submit_mission(_spec("prompt-switch"))
                await orch.run()
                [attempt] = _only_attempt(store, mission.id)
                assert attempt.prompt_version == "worker-v2-drill"  # the bound version's template
                assert (
                    store.get_intent_for_subject(attempt.id).config["prompt_version"]
                    == "worker-v2-drill"
                )
                assert orch.policy_version_of(mission.id) == promoted["version_id"]

        asyncio.run(case())
    finally:
        TEMPLATE_VERSIONS["worker"].pop("worker-v2-drill", None)


def test_review_p2_10_the_learner_and_the_gates_hash_the_same_task(tmp_path):
    from agent_orchestrator.governance.learning import task_identity
    from agent_orchestrator.governance.promotion import spec_task_identity

    config = _config(tmp_path)

    async def case():
        async with Orchestrator(config, _provider()) as orch:
            mission = await orch.submit_mission(_spec("identity"))
            return orch.store.get_mission(mission.id)

    mission = asyncio.run(case())
    assert task_identity(mission) == spec_task_identity(_spec("some-other-key"))


def test_review_p2_6_a_profile_nobody_labelled_is_not_taken_for_fixtures():
    from agent_orchestrator.orchestrator.event_handler import _provider_kind
    from agent_orchestrator.runtime.model_router import RuntimeProfile

    class SomeVendorProvider:  # not a fixture class, and nobody set provider_kind
        async def invoke(self, request, *, cancel):  # pragma: no cover - never called
            raise NotImplementedError

    unlabelled = {"default": RuntimeProfile("default", SomeVendorProvider(), "vendor-model")}
    assert _provider_kind(None, unlabelled, "default") == "unknown"
    mixed = {**unlabelled, "small": RuntimeProfile("small", _provider(), "m")}
    assert _provider_kind(None, mixed, "small") == "unknown"  # one non-fixture profile is enough
    assert (
        _provider_kind(None, {"small": RuntimeProfile("small", _provider(), "m")}, "small")
        == "fixtures"
    )
