# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 7 · slice C (D7-4' / D7-7'): the waiting view, the time a person took, and open
work that ends with the Mission."""

from __future__ import annotations

from helpers_step07 import ALICE, ENABLED, candidate, ledger_service


def _propose(service, mission, task, connectors, cand):
    return service.propose_action(
        cand,
        mission_id=mission.id,
        task_id=task.id,
        result_id="result-1",
        attempt_id=f"{task.id}:attempt-1",
        artifact_id="artifact-1",
        artifact_hash="a" * 64,
        connectors=connectors,
        deployment=ENABLED,
    )


def test_human_wait_is_the_union_of_the_open_request_spans(tmp_path):
    clock = {"now": 1_000.0}
    service, mission, t, _config, connectors, _ = ledger_service(
        tmp_path, clock=lambda: clock["now"]
    )
    a = _propose(service, mission, t["A"], connectors, candidate(target="a"))
    clock["now"] = 1_050.0
    _propose(service, mission, t["A"], connectors, candidate(target="b"))
    clock["now"] = 1_100.0
    service.decide_approval(
        a["approval_request_id"], principal=ALICE, decision="grant", nonce="n-a", deployment=ENABLED
    )
    clock["now"] = 1_200.0
    assert (
        service.store.human_wait_seconds(mission.id, 1_200.0) == 200.0
    )  # [1000, 1200] as one span
    waiting = service.store.waiting_on(mission.id)
    assert [w["kind"] for w in waiting] == ["action"] and waiting[0]["subject"].endswith(":v1")


def test_an_ending_mission_cancels_its_open_actions_but_not_what_was_handed_off(tmp_path):
    service, mission, t, _config, connectors, _ = ledger_service(tmp_path)
    open_action = _propose(service, mission, t["A"], connectors, candidate(target="a"))
    service.cancel_mission(mission.id)
    assert service.store.get_action(open_action["action_key"])["state"] == "CANCELLED"
    request = service.store.get_approval(open_action["approval_request_id"])
    assert request["state"] == "CANCELLED" and request["closed_at"] is not None
    assert service.store.waiting_on(mission.id) == []
