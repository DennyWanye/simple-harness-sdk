# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""NEXT-TG-1.0 2B: one idle verdict for the stall record and its confirmation.

Real run 2026-09-28 (mission-e5f82ae8c9f24ff8): the root was resolved, success
judged and the assured closeout was converging, yet the stall *record* (which did
not know that wait) wrote HierarchicalMissionStalled. §3.5 also routes an UNKNOWN
action under reconciliation and a pending approval to WAIT, which neither list knew.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.progress import IdleFacts, Route, idle_verdict

WAITS = (
    ("closeout_pending", "CLOSEOUT_CONVERGING"),
    ("root_resolved", "ROOT_RESOLVED_AWAITING_JUDGMENT"),
    ("running_rows", "WORK_RUNNING"),
    ("unknown_actions", "OPERATION_RECONCILIATION"),
    ("approvals_pending", "APPROVAL_PENDING"),
    ("operation_completion", "OPERATION_OUTCOME_PENDING"),
    ("assurance_work", "ASSURANCE_WORK_QUEUED"),
    ("repair_continuation_waiting", "REPAIR_CONTINUATION_WAITING"),
    ("planning_wait", "PLANNING_WAIT"),
    ("taskgraph_sources", "TASKGRAPH_SOURCES_PENDING"),
)


@pytest.mark.parametrize(("fact", "reason"), WAITS)
def test_every_legal_wait_waits_on_a_named_source(fact, reason):
    decision = idle_verdict(IdleFacts("m1", plan_has_work=True, **{fact: True}))
    assert decision.route is Route.WAIT
    assert decision.reason_code == reason and decision.wake


def test_only_a_plan_with_withheld_work_and_no_wait_is_a_stop_candidate():
    assert idle_verdict(IdleFacts("m1", plan_has_work=True)).route is Route.STOP


def test_unclassifiable_is_a_diagnosis_wait_never_slow_or_stop():
    for facts in (IdleFacts("m1"), IdleFacts("m1", plan_has_work=False)):
        decision = idle_verdict(facts)
        assert decision.route is Route.WAIT
        assert decision.reason_code == "SYSTEM_DIAGNOSIS_REQUIRED"


def test_finished_work_is_the_finalizers_business():
    decision = idle_verdict(IdleFacts("m1", all_rows_terminal=True, plan_has_work=True))
    assert decision.route is Route.FAST and decision.action == "FINALIZE"


def _loop(*, closeout: bool, actions=()):
    notes = []
    mission = SimpleNamespace(id="m1")
    fake = SimpleNamespace(
        _new_mode=lambda mission: SimpleNamespace(
            admissions=lambda mission_id: SimpleNamespace(refusals=("withheld",), readiness=()),
            network=lambda mission_id: SimpleNamespace(root_occurrence_ids=()),
        ),
        store=SimpleNamespace(
            list_tasks=lambda mission_id: [SimpleNamespace(status="READY")],
            list_actions=lambda mission_id: list(actions),
        ),
        commit=SimpleNamespace(assured_closeout_pending=lambda mission_id: closeout),
        _awaiting_retry_decision=lambda mission_id, task: False,
        _has_pending_operation_completion=lambda mission: False,
        _has_pending_assurance_work=lambda mission_id: False,
        _repair_continuation_waiting=lambda mission_id: False,
        _has_pending_planning_waits=lambda mission_id: False,
        _taskgraph_notifications=None,
        _note=notes.append,
    )
    fake._root_resolved = lambda mission, new_mode: False
    return fake, mission


def test_a_judged_mission_converging_its_closeout_is_not_a_stall_candidate():
    fake, mission = _loop(closeout=True)
    facts, admissions, _rows = Orchestrator._idle_facts(fake, mission)
    assert idle_verdict(facts).reason_code == "CLOSEOUT_CONVERGING"
    assert admissions is None  # the plan is not even read


def test_an_unknown_action_or_a_pending_approval_waits():
    for state, reason in (("UNKNOWN", "OPERATION_RECONCILIATION"),
                          ("AWAITING_APPROVAL", "APPROVAL_PENDING")):
        fake, mission = _loop(closeout=False, actions=[{"state": state}])
        facts, _, _ = Orchestrator._idle_facts(fake, mission)
        assert idle_verdict(facts).reason_code == reason


def test_without_a_wait_the_plan_is_read_and_withheld_work_is_a_candidate():
    fake, mission = _loop(closeout=False, actions=[{"state": "SUCCEEDED"}])
    facts, admissions, _ = Orchestrator._idle_facts(fake, mission)
    assert admissions is not None and idle_verdict(facts).route is Route.STOP
