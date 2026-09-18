# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-B red tests: the persisted planning-request binding (V2 §34, §36; B.2 R1–R3).

The binding is the request's identity card: which protocol and package produced it,
which plan revision it was cut against, and the digests of everything it may refer
to.  It is written once, read back byte for byte, and it is *never* reconstructed
from the environment — a recovery that guessed the mode would silently reinterpret
a legacy mission as a new-protocol one.

Storage side (migration head, idempotency, conflicts, recovery) lives in
``test_planning_decision_store.py``; this file pins the binding itself.
"""

from __future__ import annotations

import os

import pytest

from agent_orchestrator.contracts import Budget, Mission, MissionStatus
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import PlanningRequestBinding
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import Store

MISSION = "mission-1"
PROTOCOL = "planning-decision-v1"
PROMPT = "planner-hierarchical-v8"
PACKAGE_VERSION = 4

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def _mission(mission_id: str = MISSION) -> Mission:
    return Mission(
        id=mission_id,
        goal="g",
        success_criteria=("ok",),
        stop_conditions=(),
        allowed_tools=(),
        risk_level="sandbox",
        budget=Budget(max_tokens=1000, max_attempts=2),
        tenant_id="t",
        status=MissionStatus.CREATED,
        created_at=1.0,
        version=1,
        idempotency_key=mission_id,
    )


def binding(
    *,
    request_id: str = "request-1",
    base_plan_revision: int = 7,
    package_hash: str = HASH_A,
    prompt_hash: str = HASH_B,
) -> PlanningRequestBinding:
    return PlanningRequestBinding(
        request_id=request_id,
        mission_id=MISSION,
        protocol_version=PROTOCOL,
        package_version=PACKAGE_VERSION,
        package_hash=package_hash,
        base_plan_revision=base_plan_revision,
        requirements_revision=11,
        scope_epoch_digest=HASH_C,
        subject_bindings_hash=HASH_D,
        visible_refs_digest=HASH_E,
        prompt_version=PROMPT,
        prompt_hash=prompt_hash,
        created_at=10.0,
        intent_id="intent-1",
    )


@pytest.fixture
def decisions(tmp_path) -> PlanningDecisionStore:
    store = Store.open(tmp_path / "orchestrator.db")
    store.insert_mission(_mission(), spec_hash="h")
    return PlanningDecisionStore(store)


def test_r1_base_plan_revision_persists_verbatim(decisions: PlanningDecisionStore) -> None:
    """R1: the revision the request was cut against is data, not a default."""

    for revision in (0, 1, 7, 4096):
        request_id = f"request-{revision}"
        stored = decisions.insert_planning_request(
            binding(request_id=request_id, base_plan_revision=revision)
        )
        assert stored.base_plan_revision == revision
        assert decisions.get_planning_request(request_id).base_plan_revision == revision


def test_r2_the_binding_hashes_must_be_sha256_hex(decisions: PlanningDecisionStore) -> None:
    """R2: a short, upper-case or non-hex digest never reaches the table."""

    for bad in ("not-a-hash", "A" * 64, "a" * 63, "z" * 64, ""):
        with pytest.raises(ContractError):
            binding(package_hash=bad)
        with pytest.raises(ContractError):
            binding(prompt_hash=bad)
    assert decisions.get_planning_request("request-1") is None


def test_r3_the_store_never_reads_the_environment_for_the_mode(
    decisions: PlanningDecisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3: recovery reads the persisted row, never ``os.environ`` (§8.2)."""

    monkeypatch.setenv("PLANNING_PROTOCOL", PROTOCOL)
    monkeypatch.setenv("PLANNING_PROTOCOL_VERSION", PROTOCOL)
    monkeypatch.setenv("SIMPLE_HARNESS_PLANNING_PROTOCOL", PROTOCOL)
    assert decisions.get_mission_protocol(MISSION) is None
    assert decisions.get_planning_request("request-1") is None
    assert os.environ["PLANNING_PROTOCOL"] == PROTOCOL  # the test read it, the store did not


def test_the_whole_binding_survives_the_record(decisions: PlanningDecisionStore) -> None:
    """Every §34/§36 field round-trips, ``intent_id`` included (BL-7)."""

    original = binding()
    stored = decisions.insert_planning_request(original)
    assert stored.to_json() == original.to_json()
    assert stored.intent_id == "intent-1"
    again = decisions.get_planning_request("request-1")
    assert again is not None
    assert again.to_json() == original.to_json()
