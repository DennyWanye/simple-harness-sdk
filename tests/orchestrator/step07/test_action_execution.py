# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 7 · slice B (D7-5 / D7-5'): hand-off as an outbox, receipts, UNKNOWN,
reconciliation by idempotency key, the budget of an action and the human exit."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass

import pytest
from helpers_step07 import ALICE, ENABLED, candidate, ledger_service

from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.action_commits import ActionCommitError
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.runtime.actions import ActionExecutor
from agent_orchestrator.runtime.connectors import ConnectorTransportError, TestConfigService
from agent_orchestrator.storage.store import Store

HASH_A = "a" * 64
HASH_B = "b" * 64


@dataclass
class FlakyService(TestConfigService):
    """The test service behind a network that can fail: ``down`` drops the call before it
    reaches the service; ``lookup_fails`` makes the service unable to answer."""

    down: bool = False
    lookup_fails: bool = False
    delay: float = 0.0

    def execute(self, operation, target, params, *, idempotency_key):  # type: ignore[no-untyped-def]
        if self.delay:
            time.sleep(self.delay)
        if self.down:
            self.calls.append(
                {
                    "operation": operation,
                    "target": target,
                    "idempotency_key": idempotency_key,
                    "dropped": True,
                }
            )
            raise ConnectorTransportError("service unreachable")
        return super().execute(operation, target, params, idempotency_key=idempotency_key)

    def lookup(self, idempotency_key):  # type: ignore[no-untyped-def]
        if self.lookup_fails:
            raise ConnectorTransportError("lookup unreachable")
        return super().lookup(idempotency_key)


def _propose(service, mission, task, connectors, deployment, cand, artifact_hash=HASH_A):
    return service.propose_action(
        cand,
        mission_id=mission.id,
        task_id=task.id,
        result_id=f"result-{artifact_hash[:4]}",
        attempt_id=f"{task.id}:attempt-1",
        artifact_id=f"artifact-{artifact_hash[:8]}",
        artifact_hash=artifact_hash,
        connectors=connectors,
        deployment=deployment,
    )


def _approve(service, action, deployment=ENABLED, nonce="n-1"):
    if action.get("approval_request_id"):
        service.decide_approval(
            action["approval_request_id"],
            principal=ALICE,
            decision="grant",
            nonce=nonce,
            deployment=deployment,
        )


def _approved(tmp_path, *, clock=None, deployment=ENABLED, cand=None, flaky=False):
    service, mission, t, config, connectors, _ = ledger_service(
        tmp_path, deployment=deployment, clock=clock
    )
    if flaky:
        config = FlakyService(config.path)
        connectors["test_config"] = config
    action = _propose(service, mission, t["A"], connectors, deployment, cand or candidate())
    _approve(service, action, deployment)
    return service, mission, t, config, connectors, action


def _reservation(service, action_key):
    row = service.store.connection.execute(
        "SELECT state, reserved_tool_calls, settled_tool_calls FROM budget_reservations WHERE subject_id = ?",
        (f"action:{action_key}",),
    ).fetchone()
    return None if row is None else tuple(row)


def _events(service, mission, kind):
    return [dict(e.payload) for e in service.store.list_events(mission.id) if e.type == kind]


# ------------------------------------------------------------------ S7-02
def test_s7_02_the_approved_version_runs_once_and_its_receipt_is_checked(tmp_path):
    service, mission, _t, config, connectors, action = _approved(tmp_path)
    key = action["action_key"]
    executor = ActionExecutor(service, connectors, ENABLED, owner="orch-1")
    done = asyncio.run(executor.hand_off(key))
    assert done["state"] == "SUCCEEDED" and done["handoffs"] == 1
    receipt = done["receipt"]
    assert receipt["idempotency_key"] == action["idempotency_key"] == f"{action['action_id']}:v1"
    assert (
        receipt["params_hash"] == action["params_hash"]
        and receipt["target"] == "feature_flags.new_ui"
    )
    assert (
        config.state()["config"] == {"feature_flags.new_ui": "on"}
        and config.state()["applied_count"] == 1
    )
    grants = [
        d["receipt_hash"] for d in service.store.list_decisions(action["approval_request_id"])
    ]
    assert done["decision_receipts"] == grants  # decision receipt → hand-off → service receipt
    # the loop coming round again and the approval delivered again change nothing
    assert asyncio.run(executor.hand_off(key)) is None
    service.decide_approval(
        action["approval_request_id"],
        principal=ALICE,
        decision="grant",
        nonce="n-1",
        deployment=ENABLED,
    )
    assert asyncio.run(executor.hand_off(key)) is None
    assert len(config.calls) == 1
    assert _reservation(service, key) == ("SETTLED", 1, 1)
    types = [e.type for e in service.store.list_events(mission.id)]
    assert types.index("ActionHandedOff") < types.index("ActionSucceeded")
    assert [e["reason"] for e in _events(service, mission, "ActionHandoffRefused")] == [
        "not_ready:SUCCEEDED"
    ]
    # (a) of S7-06: the ledger only moves forward — never back to APPROVED after the hand-off
    assert [h["state"] for h in service.store.get_action(key)["history"]] == [
        "APPROVED",
        "HANDED_OFF",
        "SUCCEEDED",
    ]


def test_a_refused_call_is_failed_and_its_call_is_counted(tmp_path):
    service, mission, _t, config, connectors, action = _approved(tmp_path)
    config.reject_next = 1
    failed = asyncio.run(
        ActionExecutor(service, connectors, ENABLED, owner="orch-1").hand_off(action["action_key"])
    )
    assert failed["state"] == "FAILED" and "refused" in failed["error"]
    assert config.state()["applied_count"] == 0 and _reservation(service, action["action_key"]) == (
        "SETTLED",
        1,
        1,
    )


def test_hand_off_rechecks_everything_the_approval_was_bound_to(tmp_path):
    clock = {"now": 1_000.0}
    deployment = DeploymentPolicy(enabled_connectors=("test_config",), approval_ttl_seconds=60.0)
    service, mission, t, config, connectors, _ = ledger_service(
        tmp_path, deployment=deployment, clock=lambda: clock["now"]
    )
    executor = ActionExecutor(service, connectors, deployment, owner="orch-1")
    pending = _propose(service, mission, t["A"], connectors, deployment, candidate(target="a"))
    assert asyncio.run(executor.hand_off(pending["action_key"])) is None  # not approved
    late = _propose(service, mission, t["A"], connectors, deployment, candidate(target="b"))
    _approve(service, late, deployment)
    clock["now"] += 61.0
    assert asyncio.run(executor.hand_off(late["action_key"])) is None
    assert service.store.get_action(late["action_key"])["state"] == "EXPIRED"
    v1 = _propose(service, mission, t["A"], connectors, deployment, candidate(target="c"))
    _approve(service, v1, deployment, nonce="n-c1")
    _propose(
        service,
        mission,
        t["A"],
        connectors,
        deployment,
        candidate(target="c", value="beta"),
        artifact_hash=HASH_B,
    )
    assert asyncio.run(executor.hand_off(v1["action_key"])) is None  # the approval was for v1 only
    live = _propose(service, mission, t["A"], connectors, deployment, candidate())
    _approve(service, live, deployment, nonce="n-live")
    switched_off = ActionExecutor(service, connectors, DeploymentPolicy(), owner="orch-1")
    assert asyncio.run(switched_off.hand_off(live["action_key"])) is None  # the deployment changed
    reasons = [e["reason"] for e in _events(service, mission, "ActionHandoffRefused")]
    assert reasons == [
        "not_ready:AWAITING_APPROVAL",
        "approval_expired",
        "not_ready:SUPERSEDED",
        "connector_not_enabled",
    ]
    assert config.calls == []
    service.cancel_mission(mission.id)
    assert asyncio.run(executor.hand_off(live["action_key"])) is None
    assert _events(service, mission, "ActionHandoffRefused")[-1]["reason"] == "mission_not_active"


def test_the_deployment_caps_how_many_actions_a_mission_hands_off(tmp_path):
    capped = DeploymentPolicy(
        enabled_connectors=("test_config",), max_action_handoffs_per_mission=1
    )
    service, mission, t, config, connectors, _ = ledger_service(tmp_path, deployment=capped)
    first = _propose(service, mission, t["A"], connectors, capped, candidate(target="a"))
    second = _propose(service, mission, t["A"], connectors, capped, candidate(target="b"))
    _approve(service, first, capped, nonce="n-a")
    _approve(service, second, capped, nonce="n-b")
    executor = ActionExecutor(service, connectors, capped, owner="orch-1")
    assert asyncio.run(executor.hand_off(first["action_key"]))["state"] == "SUCCEEDED"
    assert asyncio.run(executor.hand_off(second["action_key"])) is None
    assert _events(service, mission, "ActionHandoffRefused")[-1]["reason"] == "handoff_cap_reached"
    assert len(config.calls) == 1


# ------------------------------------------------------------------ S7-06
def test_s7_06_a_lost_receipt_is_unknown_holds_the_reservation_and_is_reconciled_not_resent(
    tmp_path,
):
    service, mission, _t, config, connectors, action = _approved(tmp_path)
    key = action["action_key"]
    config.lose_receipt_after_apply = 1
    executor = ActionExecutor(service, connectors, ENABLED, owner="orch-1")
    unknown = asyncio.run(executor.hand_off(key))
    assert unknown["state"] == "UNKNOWN" and "ConnectorTransportError" in unknown["error"]
    assert config.state()["applied_count"] == 1  # it did happen; we just do not know yet
    assert _reservation(service, key)[0] != "SETTLED"
    assert [e["subject_id"] for e in _events(service, mission, "ReservationHeld")] == [
        f"action:{key}"
    ]
    assert asyncio.run(executor.hand_off(key)) is None  # UNKNOWN is never handed off blindly
    reconciled = asyncio.run(executor.reconcile())
    assert [a["state"] for a in reconciled] == ["SUCCEEDED"]
    assert config.state()["applied_count"] == 1 and len(config.calls) == 1  # asked, not re-sent
    assert [e["verdict"] for e in _events(service, mission, "ActionReconciled")] == ["COMPLETED"]
    assert _reservation(service, key) == ("SETTLED", 1, 1)


def test_s7_06_a_crash_between_hand_off_and_outcome_is_reconciled_after_the_lease(tmp_path):
    clock = {"now": 1_000.0}
    service, mission, _t, config, connectors, action = _approved(
        tmp_path, clock=lambda: clock["now"]
    )
    key = action["action_key"]
    # the crashed instance wrote the outbox record and died before calling the service
    handed, _ = service.begin_handoff(
        key, owner="dead-1", lease_seconds=30.0, connectors=connectors, deployment=ENABLED
    )
    assert handed["state"] == "HANDED_OFF" and handed["owner"] == "dead-1"
    survivor = ActionExecutor(service, connectors, ENABLED, owner="orch-2")
    assert asyncio.run(survivor.reconcile()) == []  # the lease is live: the call may be on its way
    clock["now"] += 31.0
    done = asyncio.run(survivor.reconcile())
    assert [(a["state"], a["handoffs"]) for a in done] == [("SUCCEEDED", 2)]
    assert [c["idempotency_key"] for c in config.calls] == [
        action["idempotency_key"]
    ]  # the same key
    assert config.state()["applied_count"] == 1
    assert [e["verdict"] for e in _events(service, mission, "ActionReconciled")] == [
        "CONFIRMED_NOT_STARTED"
    ]
    late = service.record_action_outcome(key, owner="dead-1", outcome="failed", error="late answer")
    assert late["state"] == "SUCCEEDED"  # a late answer from the old owner changes nothing


def test_s7_06_a_crash_after_the_service_applied_is_found_by_the_lookup(tmp_path):
    clock = {"now": 1_000.0}
    service, _mission, _t, config, connectors, action = _approved(
        tmp_path, clock=lambda: clock["now"]
    )
    key = action["action_key"]
    service.begin_handoff(
        key, owner="dead-1", lease_seconds=30.0, connectors=connectors, deployment=ENABLED
    )
    config.execute(
        "set", "feature_flags.new_ui", {"value": "on"}, idempotency_key=action["idempotency_key"]
    )
    clock["now"] += 31.0
    done = asyncio.run(ActionExecutor(service, connectors, ENABLED, owner="orch-2").reconcile())
    assert [(a["state"], a["handoffs"]) for a in done] == [("SUCCEEDED", 1)]
    assert len(config.calls) == 1 and config.state()["applied_count"] == 1


def test_s7_06_restoring_an_old_library_does_not_undo_reality(tmp_path):
    """Review P1-11 (b): ORCH §12.6 — an old library replays the same key; the service
    deduplicates and the ledger reconciles to the receipt it already gave."""

    service, _mission, _t, config, connectors, action = _approved(tmp_path)
    key = action["action_key"]
    backup = sqlite3.connect(tmp_path / "before-hand-off.db")
    service.store.connection.backup(backup)
    backup.close()
    first = asyncio.run(ActionExecutor(service, connectors, ENABLED, owner="orch-1").hand_off(key))
    assert first["state"] == "SUCCEEDED" and config.state()["applied_count"] == 1
    service.store.close()
    live = sqlite3.connect(tmp_path / "orchestrator.db")
    old = sqlite3.connect(tmp_path / "before-hand-off.db")
    old.backup(live)
    old.close()
    live.close()
    restored = CommitService(Store.open(tmp_path / "orchestrator.db"))
    assert restored.store.get_action(key)["state"] == "APPROVED"  # the old library forgot
    again = asyncio.run(ActionExecutor(restored, connectors, ENABLED, owner="orch-2").hand_off(key))
    assert again["state"] == "SUCCEEDED" and again["idempotency_key"] == action["idempotency_key"]
    assert again["receipt"]["receipt_hash"] == first["receipt"]["receipt_hash"]
    assert config.state()["applied_count"] == 1 and len(config.calls) == 2  # one application
    restored.store.close()


def test_s7_06_cancelling_the_mission_does_not_turn_unknown_into_failed(tmp_path):
    service, mission, _t, config, connectors, action = _approved(tmp_path)
    key = action["action_key"]
    config.lose_receipt_after_apply = 1
    executor = ActionExecutor(service, connectors, ENABLED, owner="orch-1")
    assert asyncio.run(executor.hand_off(key))["state"] == "UNKNOWN"
    service.cancel_mission(mission.id)
    assert service.store.get_action(key)["state"] == "UNKNOWN"
    done = asyncio.run(executor.reconcile())  # reconciliation covers an ended Mission too
    assert [a["state"] for a in done] == ["SUCCEEDED"]
    assert _events(service, mission, "ActionFailed") == []


def test_a_call_that_outlives_its_timeout_is_unknown_and_then_reconciled(tmp_path):
    slow = DeploymentPolicy(enabled_connectors=("test_config",), connector_timeout_seconds=0.05)
    service, _mission, _t, config, connectors, action = _approved(
        tmp_path, deployment=slow, flaky=True
    )
    config.delay = 0.3
    executor = ActionExecutor(service, connectors, slow, owner="orch-1")

    async def scenario():
        unknown = await executor.hand_off(action["action_key"])
        await asyncio.sleep(0.5)  # the call finishes in its thread after we stopped waiting
        config.delay = 0.0
        return unknown, await executor.reconcile()

    unknown, done = asyncio.run(scenario())
    assert unknown["state"] == "UNKNOWN" and "TimeoutError" in unknown["error"]
    assert [a["state"] for a in done] == ["SUCCEEDED"] and config.state()["applied_count"] == 1


def test_a_second_not_started_after_the_re_hand_off_ends_failed(tmp_path):
    service, mission, _t, config, connectors, action = _approved(tmp_path, flaky=True)
    key = action["action_key"]
    config.down = True
    executor = ActionExecutor(service, connectors, ENABLED, owner="orch-1")
    assert asyncio.run(executor.hand_off(key))["state"] == "UNKNOWN"
    once = asyncio.run(executor.reconcile())  # not started → one re-hand-off, dropped again
    assert [(a["state"], a["handoffs"]) for a in once] == [("UNKNOWN", 2)]
    twice = asyncio.run(executor.reconcile())  # not started again, no hand-off left
    assert [(a["state"], a["error"]) for a in twice] == [
        ("FAILED", "not_started:rehandoff_exhausted")
    ]
    assert config.state()["applied_count"] == 0 and len(config.calls) == 2
    assert _reservation(service, key) == ("SETTLED", 1, 2)


def test_an_unknown_the_service_cannot_answer_waits_for_a_person_with_evidence(tmp_path):
    service, mission, _t, config, connectors, action = _approved(tmp_path, flaky=True)
    key = action["action_key"]
    config.lose_receipt_after_apply = 1
    config.lookup_fails = True
    executor = ActionExecutor(service, connectors, ENABLED, owner="orch-1")
    asyncio.run(executor.hand_off(key))
    asyncio.run(executor.reconcile())
    asyncio.run(executor.reconcile())
    waiting = service.store.get_action(key)
    assert waiting["state"] == "UNKNOWN" and waiting["needs_human"] is True
    assert [e["verdict"] for e in _events(service, mission, "ActionReconciled")] == [
        "STILL_UNKNOWN"
    ]  # once
    with pytest.raises(ActionCommitError):
        service.override_action_outcome(
            key, principal=ALICE, outcome="succeeded", basis="", evidence={}
        )
    ruled = service.override_action_outcome(
        key,
        principal=ALICE,
        outcome="succeeded",
        basis="在测试服务控制台看到 new_ui=on",
        evidence={"console": "new_ui=on"},
    )
    assert ruled["state"] == "SUCCEEDED"
    [override] = service.store.list_overrides(mission.id)
    assert override["principal"]["principal_id"] == "alice" and override["basis"].startswith(
        "在测试服务"
    )
    [event] = [e for e in service.store.list_events(mission.id) if e.type == "HumanOverride"]
    assert event.actor_type == "user" and event.actor_id == "alice"
    assert _reservation(service, key)[0] == "SETTLED"
