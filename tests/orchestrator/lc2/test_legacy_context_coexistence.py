"""LC2 original libraries and production physical admission. Parent-run only."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from legacy_fixture import (
    LARGER,
    LONG,
    SOURCE_TEXT,
    frozen,
    orchestrator,
    planner,
    records,
    until,
)

from agent_orchestrator.context.context_builder import ContextRejected
from agent_orchestrator.contracts import Budget, ContractError, MissionStatus
from agent_orchestrator.runtime.legacy_provider_slots import (
    TABLE,
    LegacyProviderSlots,
    held_legacy_slots,
    profile_has_frozen_admission,
)
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    envelope_step,
    graph_proposal_step,
)
from simple_harness.execution.budget import BudgetCharge
from simple_harness.execution.provider_admission import ProviderAdmissionDenied
from simple_harness.execution.provider_invocations import provider_request_fingerprint

CHILD = Path(__file__).with_name("legacy_fixture.py")


def old_library(tmp_path, mode="pre"):
    root, marker = tmp_path / "library", tmp_path / "boundary.json"
    with (tmp_path / "warm.log").open("w") as log:
        child = subprocess.Popen(
            [sys.executable, str(CHILD), mode, str(root), str(marker)],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not marker.exists():
                assert child.poll() is None, (tmp_path / "warm.log").read_text()[-5000:]
                assert time.monotonic() < deadline, "warm boundary timed out"
                time.sleep(0.01)
            if mode == "compat-cas":
                assert child.wait(timeout=3) == 79
            else:
                os.killpg(child.pid, signal.SIGKILL)
                assert child.wait(timeout=3) == -signal.SIGKILL
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=3)
    receipt = json.loads(marker.read_text())
    assert not (root / "execution.db.context.json").exists()
    # Wait for real expiry; never edit a lease or clock in the old library.
    with sqlite3.connect(root / "execution.db") as db:
        expiry = db.execute("SELECT MAX(expires_at) FROM workflow_leases").fetchone()[0]
    delay = max(0, (expiry or 0) - time.time()) + 0.05
    assert delay < 2
    time.sleep(delay)
    return root, receipt


def slots(orch):
    return [
        dict(row)
        for row in orch.store.connection.execute(
            f"SELECT * FROM {TABLE} ORDER BY profile_id,invocation_id,handoff_ordinal"
        )
    ]


def terminal(orch, profile="default"):
    return records(orch, profile) and all(
        row["state"] in {"succeeded", "failed", "unknown"} for row in records(orch, profile)
    )


def finished(orch, intent):
    current = orch.store.get_intent(intent.intent_id)
    return (
        current.expected_turn_id is not None
        and orch.assembled.pool(
            current.config.get("runtime_profile_id", "default")
        ).runtime.uow.read_agent_turn_result(current.expected_turn_id)
        is not None
    )


def test_old_zero_call_frozen_request_and_source_resume_without_identity_change(
    tmp_path, monkeypatch
):
    root, old = old_library(tmp_path)
    assert old["calls"] == 0
    provider = RoleScriptedProvider({"planner": ["legacy answer"]})

    async def run():
        release, entered = asyncio.Event(), asyncio.Event()
        original = LegacyProviderSlots.acquire

        async def hold(self, **kwargs):
            entered.set()
            await release.wait()
            return await original(self, **kwargs)

        monkeypatch.setattr(LegacyProviderSlots, "acquire", hold)
        async with orchestrator(root, provider, upgraded=True) as orch:
            await asyncio.wait_for(entered.wait(), 5)
            assert provider.calls == 0
            assert frozen(orch, old["intent_id"]) == old["frozen"]
            assert orch._admission_for("default") is None
            assert orch.assembled.pool("default").runtime.ports.provider_admission is None
            assert held_legacy_slots(orch.store) == 0
            source = old["frozen"]["sources"][0]
            assert (
                orch.assembled.workspaces.artifact_store.path_for(
                    source["version_hash"]
                ).read_text()
                == SOURCE_TEXT
            )
            for profile, capacity in ((LONG, 262_144), (LARGER, 524_288)):
                mission, intent = await planner(orch, profile, profile=profile)
                assert intent.config["runtime_context"]["policy"]["max_input_tokens"] == capacity
                assert (
                    intent.config["provider_admission_fingerprint"]
                    == orch._admission_for(profile).fingerprint
                )
                assert orch._router_for(mission.id).route(role="critic").profile_id == profile
            release.set()
            await until(lambda: terminal(orch))
            assert provider.calls == 1
            assert provider_request_fingerprint(provider.requests[0]) == old["wire_hash"]
            assert frozen(orch, old["intent_id"]) == old["frozen"]
            assert slots(orch)[0]["state"] == "SETTLED"

    asyncio.run(run())


def test_old_handed_off_unknown_retains_slot_and_never_rehands_off_on_two_restarts(tmp_path):
    root, old = old_library(tmp_path, "handed-off")
    assert old["calls"] == 1
    for _ in range(2):
        provider = RoleScriptedProvider({"planner": ["long answer", "larger answer"]})

        async def reopen():
            async with orchestrator(root, provider, upgraded=True) as orch:
                assert frozen(orch, old["intent_id"]) == old["frozen"]
                assert provider.calls == 0
                assert held_legacy_slots(orch.store) == 1
                assert slots(orch)[0]["state"] == "UNKNOWN"
                (row,) = records(orch)
                assert row["handoff_attempt"] == 1 and row["rehandoff_count"] == 0
                usage = None if row["usage_json"] is None else json.loads(row["usage_json"])
                assert usage is None or usage == {"budget": BudgetCharge.unknown().to_json()}
                assert row["response_json"] is None
                assert row["state"] in {"handed_off", "unknown"}
                # Both long pools can still be selected while the old call is unresolved.
                if _ == 0:
                    for profile in (LONG, LARGER):
                        new_mission, intent = await planner(orch, profile, profile=profile)
                        await orch._dispatch(intent)
                        await until(lambda: finished(orch, intent))
                    assert provider.calls == 2
                    assert held_legacy_slots(orch.store) == 1
                    assert records(orch)[0]["handoff_attempt"] == 1

        asyncio.run(reopen())


@pytest.mark.parametrize("cancel_waiter", [False, True])
@pytest.mark.parametrize("legacy_first", [False, True])
def test_two_long_pools_and_legacy_share_one_global_cap(tmp_path, cancel_waiter, legacy_first):
    async def run():
        # Produce the old pool before adding either modern context sidecar.
        async with orchestrator(tmp_path, RoleScriptedProvider({})):
            pass
        gate = asyncio.Event()
        provider = RoleScriptedProvider({"planner": ["one", "two", "three"]}, gate=gate)
        async with orchestrator(tmp_path, provider, upgraded=True, slots=2) as orch:
            legacy, old_intent = await planner(orch, "legacy")
            first, long_intent = await planner(orch, "long", profile=LONG)
            second, large_intent = await planner(orch, "larger", profile=LARGER)
            order = [
                ("default", legacy, old_intent),
                (LONG, first, long_intent),
                (LARGER, second, large_intent),
            ]
            if not legacy_first:
                order = order[1:] + order[:1]
            await orch._dispatch(order[0][2])
            await until(lambda: provider.calls == 1)
            await orch._dispatch(order[1][2])
            await until(lambda: provider.calls == 2)
            await orch._dispatch(order[2][2])
            guard = orch.assembled.pool(order[2][0]).runtime.effective_provider_admission
            await until(lambda: bool(guard._waiting))
            assert provider.calls == 2
            assert held_legacy_slots(orch.store) == int(legacy_first)
            assert orch.store.connection.execute(
                "SELECT COUNT(*) FROM provider_token_grants "
                "WHERE state IN ('RESERVED','HANDED_OFF','UNKNOWN')"
            ).fetchone()[0] == 2 - int(legacy_first)
            assert records(orch, order[2][0])[0]["handoff_attempt"] == 0
            if cancel_waiter:
                orch.commit.cancel_mission(order[2][1].id)
            gate.set()
            await until(
                lambda: all(
                    finished(orch, intent) for intent in (old_intent, long_intent, large_intent)
                )
            )
            assert provider.calls == (2 if cancel_waiter else 3)
            assert held_legacy_slots(orch.store) == 0
            assert all(row["state"] == "SETTLED" for row in slots(orch))

    asyncio.run(run())


def test_cancel_legacy_between_slot_reservation_and_sdk_cas(tmp_path):
    async def run():
        provider = RoleScriptedProvider({})
        async with orchestrator(tmp_path, provider, upgraded=True) as orch:
            mission, intent = await planner(orch, "cancel-legacy")
            admission = orch.assembled.pool("default").runtime.effective_provider_admission
            original = admission.handoff

            @contextmanager
            def cancel(ticket, **kwargs):
                orch.commit.cancel_mission(mission.id)
                with original(ticket, **kwargs):
                    yield

            admission.handoff = cancel
            await orch._dispatch(intent)
            await until(lambda: bool(slots(orch)) and slots(orch)[0]["state"] == "RELEASED")
            assert provider.calls == 0
            assert records(orch)[0]["handoff_attempt"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["compat-reserved", "compat-cas"])
def test_crash_reservation_before_and_after_actual_sdk_cas(tmp_path, mode, monkeypatch):
    root, old = old_library(tmp_path, mode)

    async def run():
        provider = RoleScriptedProvider({})
        release = asyncio.Event()
        original = LegacyProviderSlots.acquire

        async def hold(self, **kwargs):
            await release.wait()
            return await original(self, **kwargs)

        monkeypatch.setattr(LegacyProviderSlots, "acquire", hold)
        async with orchestrator(root, provider, upgraded=True) as orch:
            (row,) = slots(orch)
            assert row["state"] == ("RELEASED" if mode == "compat-reserved" else "UNKNOWN")
            assert provider.calls == 0
            if mode == "compat-reserved":
                assert frozen(orch, old["intent_id"]) == old["frozen"]
            else:
                (original_call,) = records(orch)
                assert original_call["handoff_attempt"] == 1
                assert original_call["rehandoff_count"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["fingerprint", "request", "binding", "missing-record"])
def test_bad_legacy_identity_is_rejected_before_any_pool_calls(tmp_path, damage):
    root, old = old_library(tmp_path, "handed-off")

    async def import_slots():
        async with orchestrator(root, RoleScriptedProvider({}), upgraded=True):
            pass

    asyncio.run(import_slots())
    if damage == "fingerprint":
        with sqlite3.connect(root / "orchestrator.db") as db:
            row = db.execute(
                "SELECT config_json FROM dispatch_intents WHERE intent_id=?", (old["intent_id"],)
            ).fetchone()
            config = json.loads(row[0])
            config["provider_admission_fingerprint"] = "foreign-guard"
            db.execute(
                "UPDATE dispatch_intents SET config_json=? WHERE intent_id=?",
                (json.dumps(config), old["intent_id"]),
            )
    else:
        with sqlite3.connect(root / "execution.db") as db:
            if damage == "request":
                db.execute("UPDATE provider_invocations SET request_json='{}'")
            elif damage == "binding":
                db.execute("UPDATE base_agent_bindings_v1 SET creation_key='foreign'")
            else:
                db.execute("DELETE FROM provider_invocations")

    async def rejected():
        provider = RoleScriptedProvider({})
        error_type = (
            RuntimeError if damage == "missing-record" else (ValueError, ProviderAdmissionDenied)
        )
        message = (
            "SDK execution database failed integrity validation"
            if damage == "missing-record"
            else None
        )
        with pytest.raises(error_type, match=message):
            async with orchestrator(root, provider, upgraded=True):
                pytest.fail("corrupt legacy identity accepted")
        assert provider.calls == 0

    asyncio.run(rejected())


def test_old_completed_mission_and_new_capacities_survive_cold_open(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step(
                    [
                        {
                            "key": "A",
                            "goal": "Write NOTES.md",
                            "rationale": "Deliver the requested file",
                            "dependencies": [],
                            "success_criteria": ["file:NOTES.md"],
                            "verification_policy": ["format_check", "rule_check"],
                            "allowed_tools": ["workspace_write_file"],
                            "budget": {"max_tokens": 50_000, "max_attempts": 2},
                            "outputs": ["NOTES.md"],
                        }
                    ]
                )
            ],
            "worker": [
                ("workspace_write_file", {"path": "NOTES.md", "content": "original\n"}),
                envelope_step(
                    summary="Delivered NOTES.md", artifacts=["NOTES.md"], claims=["File exists"]
                ),
            ],
        }
    )

    async def warm_completed():
        async with orchestrator(tmp_path, provider) as orch:
            from agent_orchestrator.orchestrator.commit_service import MissionSpec

            mission = await orch.submit_mission(
                MissionSpec(
                    goal="Write NOTES.md",
                    success_criteria=("file:NOTES.md",),
                    tenant_id="lc2",
                    idempotency_key="completed",
                    allowed_tools=("workspace_write_file",),
                    budget=Budget(max_tokens=4_000_000, max_attempts=12),
                )
            )
            await asyncio.wait_for(orch.run(), 15)
            assert orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED
            identity = {row["invocation_id"]: row["request_fingerprint"] for row in records(orch)}
            return mission.id, identity

    mission_id, identity = asyncio.run(warm_completed())

    async def reopen():
        cold = RoleScriptedProvider({})
        async with orchestrator(tmp_path, cold, upgraded=True) as orch:
            assert orch.store.get_mission(mission_id).status is MissionStatus.COMPLETED
            assert {
                row["invocation_id"]: row["request_fingerprint"] for row in records(orch)
            } == identity
            assert held_legacy_slots(orch.store) == 0
            assert all(row["state"] == "SETTLED" for row in slots(orch))
            assert cold.calls == 0
            assert profile_has_frozen_admission(orch.config, "default") is False

    asyncio.run(reopen())
    asyncio.run(reopen())


def test_existing_source_corruption_is_not_hidden_by_coexistence(tmp_path):
    async def create():
        async with orchestrator(tmp_path, RoleScriptedProvider({})) as orch:
            mission, intent = await planner(orch, "source-integrity", sources=True)
            return mission.id, dict(intent.config)

    mission_id, original_config = asyncio.run(create())

    async def reopen():
        provider = RoleScriptedProvider({})
        async with orchestrator(tmp_path, provider, upgraded=True) as orch:
            source = orch.store.list_sources(mission_id, True)[0]
            path = orch.assembled.workspaces.artifact_store.path_for(source["version_hash"])
            path.chmod(0o600)
            path.write_text("changed source bytes")  # negative corruption only
            with pytest.raises(ContextRejected, match="source workload"):
                await orch._create_planner_intent(mission_id, ordinal=2)
            intents = orch.store.connection.execute(
                "SELECT config_json FROM dispatch_intents WHERE mission_id=?",
                (mission_id,),
            ).fetchall()
            assert len(intents) == 1 and json.loads(intents[0][0]) == original_config
            assert provider.calls == 0

    asyncio.run(reopen())


def test_missing_new_context_identity_cannot_reinterpret_pool_as_legacy(tmp_path):
    async def create():
        async with orchestrator(tmp_path, RoleScriptedProvider({}), upgraded=True) as orch:
            await planner(orch, "frozen-long", profile=LONG)

    asyncio.run(create())
    # Explicit negative corruption of a NEW pool, never the old-library fixture.
    (tmp_path / f"execution-{LONG}.db.context.json").unlink()

    async def rejected():
        provider = RoleScriptedProvider({})
        with pytest.raises(ValueError, match="context identity missing"):
            async with orchestrator(tmp_path, provider, upgraded=True):
                pytest.fail("missing context identity was accepted")
        assert provider.calls == 0

    asyncio.run(rejected())


def test_new_capacities_and_legacy_routing_stay_frozen_when_creation_default_changes(tmp_path):
    async def create():
        async with orchestrator(tmp_path, RoleScriptedProvider({})) as orch:
            old, intent = await planner(orch, "old-route")
            old_id = old.id
            old_intent_id, old_config = intent.intent_id, dict(intent.config)
        snapshots = {}
        async with orchestrator(tmp_path, RoleScriptedProvider({}), upgraded=True) as orch:
            for profile in (LONG, LARGER):
                mission, intent = await planner(orch, profile, profile=profile)
                snapshots[mission.id] = (profile, intent.intent_id, dict(intent.config))
        return old_id, old_intent_id, old_config, snapshots

    old_id, old_intent_id, old_config, snapshots = asyncio.run(create())

    async def reopen():
        provider = RoleScriptedProvider({})
        async with orchestrator(tmp_path, provider, upgraded=True, default=LARGER) as orch:
            assert orch._router_for(old_id).route(role="planner").profile_id == "default"
            for mission_id, (profile, intent_id, config) in snapshots.items():
                assert orch._router_for(mission_id).route(role="critic").profile_id == profile
                assert dict(orch.store.get_intent(intent_id).config) == config
            new_mission, new_intent = await planner(orch, "new-default-route")
            assert new_intent.config["runtime_profile_id"] == LARGER
            assert orch.policy_version_of(new_mission.id) == orch.policy_version_of(old_id)
            assert orch._router_for(new_mission.id).route(role="planner").profile_id == LARGER
            assert orch._router_for(old_id).route(role="planner").profile_id == "default"
            assert dict(orch.store.get_intent(old_intent_id).config) == old_config
            assert provider.calls == 0

    asyncio.run(reopen())


def test_none_estimator_cannot_disable_admission_on_a_fresh_long_pool(tmp_path):
    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    provider = RoleScriptedProvider({})
    candidate = orchestrator(tmp_path, provider, upgraded=True)
    with pytest.raises(ValueError, match="new context pool requires"):
        Orchestrator(
            candidate.config,
            profiles=candidate._profiles,
            provider_token_estimators={"default": None, LONG: None, LARGER: None},
        )
    assert not tmp_path.joinpath("orchestrator.db").exists()
    assert provider.calls == 0


def test_old_mission_without_dispatch_has_no_proven_historical_default(tmp_path):
    from agent_orchestrator.orchestrator.commit_service import MissionSpec

    async def create():
        async with orchestrator(tmp_path, RoleScriptedProvider({})) as orch:
            mission = await orch.submit_mission(
                MissionSpec(
                    goal="Write NOTES.md",
                    success_criteria=("file:NOTES.md",),
                    tenant_id="lc2",
                    idempotency_key="no-prior-dispatch",
                    budget=Budget(max_tokens=4_000_000, max_attempts=12),
                )
            )
            assert orch._selected_profile(mission.id) is None
            assert "default" not in orch.policy_for(mission.id)["routing"]
            return mission.id, orch.store.get_mission_policy(mission.id)

    mission_id, binding = asyncio.run(create())

    async def reopen():
        provider = RoleScriptedProvider({})
        async with orchestrator(tmp_path, provider, upgraded=True, default=LARGER) as orch:
            assert (
                orch.store.connection.execute(
                    "SELECT COUNT(*) FROM dispatch_intents WHERE mission_id=?",
                    (mission_id,),
                ).fetchone()[0]
                == 0
            )
            assert orch.store.get_mission_policy(mission_id) == binding
            assert "default" not in orch.policy_for(mission_id)["routing"]
            assert orch._frozen_default_route(mission_id) is None
            # Explicit limitation: this is current deployment routing, NOT proof
            # that the old Mission's original default was frozen or preserved.
            assert orch._router_for(mission_id).rules.default == LARGER
            assert orch._selected_profile(mission_id) is None
            assert orch.store.get_mission_policy(mission_id) == binding
            assert provider.calls == 0

    asyncio.run(reopen())


@pytest.mark.parametrize("damage", ["decision-profile", "agent-profile", "conflicting-defaults"])
def test_frozen_default_conflict_or_identity_mismatch_rejects_before_calls(tmp_path, damage):
    async def run():
        provider = RoleScriptedProvider({})
        async with orchestrator(tmp_path, provider, upgraded=True) as orch:
            mission, first = await planner(orch, "bad-default")
            target = first
            if damage == "conflicting-defaults":
                target = await orch._create_planner_intent(mission.id, ordinal=2)
            config = json.loads(json.dumps(dict(target.config)))
            if damage in {"decision-profile", "conflicting-defaults"}:
                config["routing"]["profile_id"] = LONG
            if damage in {"agent-profile", "conflicting-defaults"}:
                config["agent_config"]["model_profile_ref"] = LONG
            if damage == "conflicting-defaults":
                config["runtime_profile_id"] = LONG
            # Negative corruption of actual frozen intents. This is not a
            # fabricated valid history and never changes production records.
            with orch.store.transaction():
                orch.store.connection.execute(
                    "UPDATE dispatch_intents SET config_json=? WHERE intent_id=?",
                    (json.dumps(config), target.intent_id),
                )
            expected = (
                "conflicting frozen default routes"
                if damage == "conflicting-defaults"
                else "frozen default routing identities differ"
            )
            with pytest.raises(ContractError, match=expected):
                orch._router_for(mission.id)
            assert provider.calls == 0

    asyncio.run(run())


def test_actual_role_override_is_not_evidence_of_the_deployment_default(tmp_path):
    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.model_router import RoutingRules

    async def run():
        provider = RoleScriptedProvider({})
        candidate = orchestrator(tmp_path, provider, upgraded=True)
        async with Orchestrator(
            candidate.config,
            profiles=candidate._profiles,
            provider_token_estimators=candidate._provider_token_estimators,
            routing=RoutingRules(default=LARGER, by_role={"planner": LONG}),
        ) as orch:
            mission, intent = await planner(orch, "role-override")
            assert intent.config["runtime_profile_id"] == LONG
            assert intent.config["routing"]["reason"] == "by_role:planner"
            assert orch._frozen_default_route(mission.id) is None
            assert orch._router_for(mission.id).rules.default == LARGER
            assert orch._router_for(mission.id).route(role="planner").profile_id == LONG
            assert provider.calls == 0

    asyncio.run(run())
