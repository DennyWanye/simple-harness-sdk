# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 slice D · P32-8 / P32-11 (ledger half): recovery and compensation are different
things (plan v3 D6 / D8; plan review round 2 P2-1 / P2-3 / P2-7).

Recovery re-establishes what one action version was already allowed to do — same business
key, same idempotency key.  Compensation is a *new* action: something in the world must
change again, so it has its own business key (``<action>#comp-<n>``), its own approval and
its own idempotency key, and the original fact stays exactly as it was recorded.  Without
that separation, changing the content after a success would either be silently refused
(``action_already_executed``) or, worse, ride on the old approval.

Level 3 and its two-person rule can only be tested here, at the SDK level: the Host is a
single-user machine whose ceiling is L2.
"""

from __future__ import annotations

import pytest
from helpers_step07 import ALICE, BOB, ENABLED, candidate, ledger_service

from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.action_commits import ActionCommitError, CandidateRejected
from agent_orchestrator.runtime.connectors import Receipt, params_hash

HASH_A = "a" * 64
HASH_B = "b" * 64
L3 = DeploymentPolicy(
    enabled_connectors=("test_config",), max_action_level="L3", l3_distinct_principals=True
)


def _propose(service, mission, task, connectors, deployment, cand, artifact_hash=HASH_A):
    return service.propose_action(
        cand,
        mission_id=mission.id,
        task_id=task.id,
        result_id="result-1",
        attempt_id=f"{task.id}:attempt-1",
        artifact_id=f"artifact-{artifact_hash[:8]}",
        artifact_hash=artifact_hash,
        connectors=connectors,
        deployment=deployment,
    )


def _receipt(action) -> Receipt:
    return Receipt(
        idempotency_key=str(action["idempotency_key"]),
        connector=str(action["connector"]),
        operation=str(action["operation"]),
        target=str(action["target"]),
        params_hash=params_hash(action["params"]),
        applied=True,
        after="on",
        service_ref="test-config#1",
    )


def _succeed(service, action, *, principal=ALICE, deployment=ENABLED, connectors=None):
    """Approve, hand off and record a successful outcome for one action version."""

    if action["approval_request_id"]:
        service.decide_approval(
            action["approval_request_id"],
            principal=principal,
            decision="grant",
            nonce="n1",
            deployment=deployment,
        )
    handed, _reason = service.begin_handoff(
        action["action_key"],
        owner="owner-1",
        lease_seconds=60,
        connectors=connectors or {},
        deployment=deployment,
    )
    assert handed is not None, _reason
    return service.record_action_outcome(
        action["action_key"], owner="owner-1", outcome="succeeded", receipt=_receipt(handed)
    )


@pytest.fixture
def ledger(tmp_path):
    service, mission, tasks, config, connectors, deployment = ledger_service(tmp_path)
    return service, mission, next(iter(tasks.values())), config, connectors, deployment


# ------------------------------------------------------------------ P32-8 after a success
def test_p32_8_changing_the_content_after_a_success_is_refused_as_a_plain_proposal(ledger):
    service, mission, task, _config, connectors, deployment = ledger
    first = _propose(service, mission, task, connectors, deployment, candidate())
    done = _succeed(service, first, connectors=connectors, deployment=deployment)
    assert done["state"] == "SUCCEEDED"
    again = _propose(
        service, mission, task, connectors, deployment, candidate(value="off"), artifact_hash=HASH_B
    )
    assert again["state"] == "REFUSED" and again["refused"] == "action_already_executed"
    assert service.store.get_action(first["action_key"])["state"] == "SUCCEEDED"


def test_p32_8_a_compensation_is_a_new_action_and_leaves_the_original_fact_alone(ledger):
    service, mission, task, _config, connectors, deployment = ledger
    first = _propose(service, mission, task, connectors, deployment, candidate())
    done = _succeed(service, first, connectors=connectors, deployment=deployment)
    compensation = service.propose_compensation(
        first["action_key"],
        operation="set",
        params={"value": "off"},
        reason="上一版发布错了，改回去",
        artifact_id="artifact-bbbbbbbb",
        artifact_hash=HASH_B,
        connectors=connectors,
        deployment=deployment,
    )
    assert compensation["compensates"] == first["action_key"]
    assert compensation["action_id"] == f"{first['action_id']}#comp-1"
    assert compensation["action_key"].startswith(compensation["action_id"])
    assert compensation["idempotency_key"] != first["idempotency_key"]
    assert compensation["state"] == "AWAITING_APPROVAL"  # its own approval, never the old one
    assert compensation["approval_request_id"] != first["approval_request_id"]
    unchanged = service.store.get_action(first["action_key"])
    assert unchanged["state"] == "SUCCEEDED"
    assert unchanged["receipt"] == done["receipt"]


def test_p32_8_a_second_compensation_gets_its_own_number(ledger):
    service, mission, task, _config, connectors, deployment = ledger
    first = _propose(service, mission, task, connectors, deployment, candidate())
    _succeed(service, first, connectors=connectors, deployment=deployment)
    one = service.propose_compensation(
        first["action_key"],
        operation="set",
        params={"value": "off"},
        reason="改回去",
        artifact_id="artifact-1",
        artifact_hash=HASH_B,
        connectors=connectors,
        deployment=deployment,
    )
    _succeed(service, one, connectors=connectors, deployment=deployment)
    two = service.propose_compensation(
        one["action_key"],
        operation="set",
        params={"value": "on"},
        reason="再改一次",
        artifact_id="artifact-2",
        artifact_hash=HASH_A,
        connectors=connectors,
        deployment=deployment,
    )
    assert two["action_id"].endswith("#comp-2") and two["compensates"] == one["action_key"]


def test_p32_8_only_a_settled_action_can_be_compensated(ledger):
    service, mission, task, _config, connectors, deployment = ledger
    open_action = _propose(service, mission, task, connectors, deployment, candidate())
    with pytest.raises(ActionCommitError):
        service.propose_compensation(
            open_action["action_key"],
            operation="set",
            params={"value": "off"},
            reason="还没执行就想补偿",
            artifact_id="artifact-1",
            artifact_hash=HASH_B,
            connectors=connectors,
            deployment=deployment,
        )


def test_p32_8_a_compensation_stays_inside_the_missions_action_scope(ledger):
    service, mission, task, _config, connectors, deployment = ledger
    first = _propose(service, mission, task, connectors, deployment, candidate())
    _succeed(service, first, connectors=connectors, deployment=deployment)
    with pytest.raises(CandidateRejected) as refused:
        service.propose_compensation(
            first["action_key"],
            operation="set",
            target="feature_flags.something_else",  # not in the charter's criteria
            params={"value": "off"},
            reason="越界",
            artifact_id="artifact-1",
            artifact_hash=HASH_B,
            connectors=connectors,
            deployment=deployment,
        )
    assert refused.value.reason == "action_out_of_scope"


# ------------------------------------------------------------------ P32-11 L3 and two people
def test_p32_11_an_l3_compensation_is_refused_under_an_l2_ceiling(ledger):
    service, mission, task, _config, connectors, _deployment = ledger
    capped = DeploymentPolicy(enabled_connectors=("test_config",), max_action_level="L2")
    first = _propose(service, mission, task, connectors, capped, candidate())
    _succeed(service, first, connectors=connectors, deployment=capped)
    with pytest.raises(CandidateRejected) as refused:
        service.propose_compensation(
            first["action_key"],
            operation="delete",  # L3 on the test service
            params={},
            reason="撤回",
            artifact_id="artifact-1",
            artifact_hash=HASH_B,
            connectors=connectors,
            deployment=capped,
        )
    assert refused.value.reason == "above_deployment_ceiling:L2"


def test_p32_11_an_l3_compensation_needs_two_different_people(ledger):
    service, mission, task, _config, connectors, _deployment = ledger
    first = _propose(service, mission, task, connectors, L3, candidate())
    _succeed(service, first, connectors=connectors, deployment=L3)
    retract = service.propose_compensation(
        first["action_key"],
        operation="delete",
        params={},
        reason="撤回这项配置",
        artifact_id="artifact-1",
        artifact_hash=HASH_B,
        connectors=connectors,
        deployment=L3,
    )
    request = retract["approval_request_id"]
    assert service.store.get_approval(request)["required_count"] == 2
    service.decide_approval(request, principal=ALICE, decision="grant", nonce="n1", deployment=L3)
    with pytest.raises(ActionCommitError):  # one person is not two: refused, never counted
        service.decide_approval(
            request, principal=ALICE, decision="grant", nonce="n2", deployment=L3
        )
    assert service.store.get_approval(request)["state"] == "PENDING"
    assert service.store.get_action(retract["action_key"])["state"] == "AWAITING_APPROVAL"
    service.decide_approval(request, principal=BOB, decision="grant", nonce="n3", deployment=L3)
    assert service.store.get_approval(request)["state"] == "GRANTED"
    assert service.store.get_action(retract["action_key"])["state"] == "APPROVED"
