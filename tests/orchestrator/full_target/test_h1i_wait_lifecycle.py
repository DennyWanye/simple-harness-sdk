from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from test_h1i_production_entry import _config, _events, _open_planner_round, _seed_new_protocol

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts import Budget, Task, TaskStatus
from agent_orchestrator.contracts.planning_decisions import (
    PlanningDecisionEnvelopeV1,
    PlanningDecisionStatus,
)
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import deployed_layers
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.hierarchical_dispatch import append_hierarchical_event
from agent_orchestrator.planning.decision_codec import serialize_planning_decision
from agent_orchestrator.planning.htn.observers.code import code_observers
from agent_orchestrator.planning.htn.planner_package import package_hash
from agent_orchestrator.planning.htn.world import build_planning_world
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.store import InjectedCrash
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def _wait_reply(package: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    visible = next(ref for ref in package["visible_refs"] if ref["kind"] == "task")
    body = json.loads(
        (
            Path(__file__).parent / "fixtures" / "planning_decision_v1" / "valid" / "wait.json"
        ).read_text(encoding="utf-8")
    )
    body["subject_key"] = package["planning_subjects"][0]["subject_key"]
    body["payload"]["wait_for"] = [visible]
    return serialize_planning_decision(PlanningDecisionEnvelopeV1.from_json(body)), visible


def _ensure_wait_task(loop: Orchestrator, mission: Any, ref: dict[str, Any]) -> Task:
    """Create the authoritative Task row missing from the HTN-only seed fixture."""
    task = loop.store.get_task(ref["id"])
    if task is None:
        task = Task(
            id=ref["id"],
            mission_id=mission.id,
            parent_task_ids=(),
            dependency_ids=(),
            goal="WAIT target",
            rationale="deterministic WAIT state fixture",
            success_criteria=("state:completed",),
            verification_policy=("rule_check",),
            allowed_tools=(),
            budget=Budget(max_tokens=1_000, max_attempts=1),
            priority=1.0,
            status=TaskStatus.ACTIVE,
            version=1,
        )
        loop.store.insert_task(task, ordinal=999)
    assert task.mission_id == mission.id
    return task


def _mark_wait_task_terminal(loop: Orchestrator, mission: Any, ref: dict[str, Any]) -> None:
    """Store-predicate fixture: do not substitute a TargetCompleted event for a Task."""
    task = _ensure_wait_task(loop, mission, ref)
    loop.store.update_task(
        replace(task, status=TaskStatus.COMPLETED, version=task.version + 1),
        expected_version=task.version,
    )


async def _register_wait(loop: Orchestrator, mission: Any, dispatch: Any, *, ordinal: int = 1):
    opener = await _open_planner_round(loop, mission, dispatch, ordinal=ordinal)
    PlanningAuthorizationApi(
        loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
    ).issue(mission.id, command_id=f"grant-wait-{ordinal}", request_id=opener.intent_id)
    reply, visible = _wait_reply(opener.config["planning_package"])
    _ensure_wait_task(loop, mission, visible)
    await loop._collect_plan_decision(opener, object(), mission, reply, dispatch)
    return opener, reply, visible


def _planner_intents(loop: Orchestrator, mission_id: str) -> list[Any]:
    return [
        i
        for i in loop.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED")
        if i.mission_id == mission_id
        and i.kind == "plan"
        and str(i.config.get("role", "")) != "method_synthesizer"
    ]


def test_wait_registration_is_durable_and_does_not_keep_run_inflight(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-register"
            )
            _opener, _reply, visible = await _register_wait(loop, mission, dispatch)
            evaluated = _events(loop, mission.id, "PlanningDecisionEvaluated")[-1]
            registered = _events(loop, mission.id, "PlanningWaitRegistered")[-1]
            assert evaluated.payload["status"] == str(PlanningDecisionStatus.NO_STATE_CHANGE)
            assert registered.payload["wait_for"] == [visible]
            assert loop._has_pending_planning_waits(mission.id)
            assert not loop._has_inflight()

    asyncio.run(case())


def test_wait_run_returns_when_only_external_wait_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-idle"
            )
            await _register_wait(loop, mission, dispatch)
            # Keep the normal run() idle branch and _has_inflight predicate real; only
            # bypass recovery/stall work unrelated to this external WAIT regression.
            monkeypatch.setattr(loop, "recover", AsyncMock())
            monkeypatch.setattr(loop, "_cycle", AsyncMock(return_value=False))
            monkeypatch.setattr(loop, "_record_hierarchical_stall", AsyncMock())
            monkeypatch.setattr(loop, "_confirm_and_stop_stalled", AsyncMock(return_value=False))
            monkeypatch.setattr(loop.actions, "reconcile", AsyncMock(return_value=[]))
            await loop.run()
            assert loop._cycle.await_count == 2

    asyncio.run(case())


def test_wait_ignores_fake_event_and_wakes_once_from_exact_terminal_task(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-store"
            )
            _opener, _reply, visible = await _register_wait(loop, mission, dispatch)
            append_hierarchical_event(
                loop.store,
                "TargetCompleted",
                mission.id,
                key="fake-target",
                payload={"planning_ref": visible},
            )
            assert not await loop._wake_planning_waits()
            _mark_wait_task_terminal(loop, mission, visible)
            assert await loop._wake_planning_waits()
            assert (
                len(_events(loop, mission.id, "PlanningWaitWoken"))
                == len(_planner_intents(loop, mission.id))
                == 1
            )
            assert not await loop._wake_planning_waits()

    asyncio.run(case())


def test_wait_target_terminal_before_registration_wakes(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-first"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            ).issue(mission.id, command_id="grant-wait-first", request_id=opener.intent_id)
            reply, visible = _wait_reply(opener.config["planning_package"])
            _mark_wait_task_terminal(loop, mission, visible)
            await loop._collect_plan_decision(opener, object(), mission, reply, dispatch)
            assert await loop._wake_planning_waits()
            assert (
                len(_events(loop, mission.id, "PlanningWaitWoken"))
                == len(_planner_intents(loop, mission.id))
                == 1
            )

    asyncio.run(case())


def test_wait_wake_fault_rolls_back_woken_and_planner_intent_together(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-wake-fault"
            )
            _opener, _reply, visible = await _register_wait(loop, mission, dispatch)
            _mark_wait_task_terminal(loop, mission, visible)
            loop.store.arm("planning_wait_before_wake_commit")
            with pytest.raises(InjectedCrash, match="planning_wait_before_wake_commit"):
                await loop._wake_planning_waits()
            assert not _events(loop, mission.id, "PlanningWaitWoken") and not _planner_intents(
                loop, mission.id
            )
            assert loop._has_pending_planning_waits(mission.id)
            assert await loop._wake_planning_waits()
            assert (
                len(_events(loop, mission.id, "PlanningWaitWoken"))
                == len(_planner_intents(loop, mission.id))
                == 1
            )

    asyncio.run(case())


def test_wait_registration_fault_rolls_back_terminal_decision_and_registration(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-register-fault"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            ).issue(mission.id, command_id="grant-wait-fault", request_id=opener.intent_id)
            reply, visible = _wait_reply(opener.config["planning_package"])
            _ensure_wait_task(loop, mission, visible)
            loop.store.arm("planning_wait_before_registration_commit")
            with pytest.raises(InjectedCrash, match="planning_wait_before_registration_commit"):
                await loop._collect_plan_decision(opener, object(), mission, reply, dispatch)
            assert not _events(loop, mission.id, "PlanningWaitRegistered") and not _events(
                loop, mission.id, "PlanningDecisionEvaluated"
            )
            assert loop.store.get_intent(opener.intent_id).state == "PENDING"
            await loop._collect_plan_decision(opener, object(), mission, reply, dispatch)
            assert len(_events(loop, mission.id, "PlanningWaitRegistered")) == 1

    asyncio.run(case())


def test_wait_reopens_from_durable_store(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as first:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                first, tmp_path, key="h1i-wait-reopen"
            )
            _opener, _reply, visible = await _register_wait(first, mission, dispatch)
            _mark_wait_task_terminal(first, mission, visible)
            mission_id = mission.id
        async with Orchestrator(
            _config(tmp_path), RoleScriptedProvider({"planner": []})
        ) as reopened:
            # A new process must receive the deployment's explicit hierarchical
            # assembly.  Rebuild it against the original Mission/repository and the
            # durable semantic table; do not re-seed or invent a replacement Mission.
            reopened.install_hierarchical(
                planning=build_planning_world(
                    mission_id,
                    domains=("code",),
                    semantics=HtnStore(reopened.store),
                    deployed_layers=deployed_layers(reopened.config.deployment_policy),
                    observers=code_observers(tmp_path / "repo", allow_test_execution=True),
                )
            )
            assert reopened._has_pending_planning_waits(mission_id)
            assert await reopened._wake_planning_waits()
            assert (
                len(_events(reopened, mission_id, "PlanningWaitWoken"))
                == len(_planner_intents(reopened, mission_id))
                == 1
            )
            assert not await reopened._wake_planning_waits()

    asyncio.run(case())


def test_wait_rejects_unsupported_kind_without_registration(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-unsupported"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            ).issue(mission.id, command_id="grant-wait-unsupported", request_id=opener.intent_id)
            method = next(
                ref
                for ref in opener.config["planning_package"]["visible_refs"]
                if ref["kind"] == "method"
            )
            body = json.loads(
                (
                    Path(__file__).parent
                    / "fixtures"
                    / "planning_decision_v1"
                    / "valid"
                    / "wait.json"
                ).read_text(encoding="utf-8")
            )
            body["subject_key"] = opener.config["planning_package"]["planning_subjects"][0][
                "subject_key"
            ]
            # A method ref is valid protocol input, but has no active producer that a
            # WAIT may suspend for; this is admission coverage, not a codec refusal.
            body["payload"]["wait_for"] = [method]
            await loop._collect_plan_decision(
                opener,
                object(),
                mission,
                f"<planning_decision>{json.dumps(body)}</planning_decision>",
                dispatch,
            )
            assert not _events(loop, mission.id, "PlanningWaitRegistered")
            evaluated = _events(loop, mission.id, "PlanningDecisionEvaluated")[-1]
            assert evaluated.payload["status"] == str(PlanningDecisionStatus.REJECTED)
            assert evaluated.payload["rejection_codes"] == ["PARAMETER_INVALID"]

    asyncio.run(case())


def test_repeated_satisfied_wait_stops_instead_of_registering_a_busy_loop(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-repeat"
            )
            _opener, _reply, visible = await _register_wait(loop, mission, dispatch)
            _mark_wait_task_terminal(loop, mission, visible)
            assert await loop._wake_planning_waits()
            retry = _planner_intents(loop, mission.id)[0]
            PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            ).issue(mission.id, command_id="grant-wait-repeat", request_id=retry.intent_id)
            repeat_reply, _repeat_visible = _wait_reply(retry.config["planning_package"])
            await loop._collect_plan_decision(retry, object(), mission, repeat_reply, dispatch)
            assert len(_events(loop, mission.id, "PlanningWaitRegistered")) == 2
            assert await loop._wake_planning_waits()
            assert len(_planner_intents(loop, mission.id)) == 0
            assert loop.store.get_mission(mission.id).status.name == "FAILED"

    asyncio.run(case())


@pytest.mark.parametrize("terminal", (TaskStatus.FAILED, TaskStatus.CANCELLED))
def test_wait_wakes_for_each_authoritative_terminal_task_state(
    tmp_path: Path, terminal: TaskStatus
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key=f"h1i-wait-{terminal.lower()}"
            )
            _opener, _reply, visible = await _register_wait(loop, mission, dispatch)
            task = _ensure_wait_task(loop, mission, visible)
            loop.store.update_task(
                replace(task, status=terminal, version=task.version + 1),
                expected_version=task.version,
            )
            assert await loop._wake_planning_waits()
            woken = _events(loop, mission.id, "PlanningWaitWoken")[-1]
            assert woken.payload["settled_tasks"] == {visible["id"]: str(terminal)}

    asyncio.run(case())


@pytest.mark.parametrize(
    "terminal", (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
)
def test_wait_wakeup_freezes_terminal_task_outcome_in_new_protocol_package(
    tmp_path: Path, terminal: TaskStatus
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key=f"h1i-wait-package-{terminal.lower()}"
            )
            opener, _reply, visible = await _register_wait(loop, mission, dispatch)
            opener_hash = loop.store.connection.execute(
                "SELECT package_hash FROM planning_requests WHERE intent_id = ?",
                (opener.intent_id,),
            ).fetchone()[0]
            task = _ensure_wait_task(loop, mission, visible)
            loop.store.update_task(
                replace(task, status=terminal, version=task.version + 1),
                expected_version=task.version,
            )

            assert await loop._wake_planning_waits()
            intent = _planner_intents(loop, mission.id)[0]
            package = intent.config["planning_package"]
            row = next(
                item
                for item in package["plan"]["open_compound_goals"]
                if item["goal_id"] == visible["id"]
            )
            assert row["task_status"] == str(terminal)
            assert row["task_version"] == task.version + 1
            assert row["occurrence_outcome"] == (
                "SETTLED_OTHER" if terminal is TaskStatus.COMPLETED else str(terminal)
            )
            assert str(terminal) in intent.config["message"]["content"]

            bound_hash = loop.store.connection.execute(
                "SELECT package_hash FROM planning_requests WHERE intent_id = ?",
                (intent.intent_id,),
            ).fetchone()[0]
            assert bound_hash == package_hash(package)
            assert bound_hash != opener_hash

    asyncio.run(case())


def test_registered_wait_tolerates_active_to_ready_retry_without_stopping(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-retry-ready"
            )
            _opener, _reply, visible = await _register_wait(loop, mission, dispatch)
            task = _ensure_wait_task(loop, mission, visible)
            loop.store.update_task(
                replace(task, status=TaskStatus.READY, version=task.version + 1),
                expected_version=task.version,
            )
            assert not await loop._wake_planning_waits()
            assert loop._has_pending_planning_waits(mission.id)
            assert loop.store.get_mission(mission.id).status.name != "FAILED"

    asyncio.run(case())


def test_wait_rejects_blocked_target_that_has_no_inflight_work(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-blocked"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            ).issue(mission.id, command_id="grant-wait-blocked", request_id=opener.intent_id)
            reply, visible = _wait_reply(opener.config["planning_package"])
            task = _ensure_wait_task(loop, mission, visible)
            loop.store.update_task(
                replace(task, status=TaskStatus.BLOCKED, version=task.version + 1),
                expected_version=task.version,
            )
            await loop._collect_plan_decision(opener, object(), mission, reply, dispatch)
            assert not _events(loop, mission.id, "PlanningWaitRegistered")

    asyncio.run(case())


def test_wait_unknown_or_foreign_mission_ref_never_passes_from_task_existence(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-negative"
            )
            _opener, _reply, visible = await _register_wait(loop, mission, dispatch)
            _mark_wait_task_terminal(loop, mission, visible)
            semantic = HtnStore(loop.store).task_semantics_of(mission.id, visible["id"])
            assert semantic is not None
            unknown = SimpleNamespace(
                kind="unknown",
                id=visible["id"],
                semantic_revision=semantic.contract_revision,
                content_hash=semantic.content_hash(),
            )
            assert not loop._planning_wait_ref_satisfied(mission, unknown)
            assert not loop._planning_wait_ref_satisfied(
                replace(mission, id="foreign-mission"),
                SimpleNamespace(
                    kind="task",
                    id=visible["id"],
                    semantic_revision=semantic.contract_revision,
                    content_hash=semantic.content_hash(),
                ),
            )

    asyncio.run(case())
