# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-S red tests: the Mission planning-protocol switch and durable binding."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from agent_orchestrator.contracts import Budget, ContractError
from agent_orchestrator.orchestrator.commit_service import (
    LEGACY_PLANNING_PROTOCOL,
    PLANNING_DECISION_V1,
    CommitRejected,
    CommitService,
    MissionConflict,
    MissionSpec,
)
from agent_orchestrator.orchestrator.planning_protocol_binding import (
    planning_protocol_for_mission,
)
from agent_orchestrator.storage.store import Store


def _spec(key: str, **kwargs: object) -> MissionSpec:
    return MissionSpec(
        goal="g",
        success_criteria=("ok",),
        tenant_id="tenant",
        idempotency_key=key,
        budget=Budget(max_tokens=1000, max_attempts=1),
        **kwargs,
    )


def test_default_spec_json_bytes_and_hash_are_unchanged() -> None:
    spec = _spec("bytes")
    expected = {
        "goal": "g",
        "success_criteria": ["ok"],
        "tenant_id": "tenant",
        "idempotency_key": "bytes",
        "stop_conditions": ["verification_passed", "budget_exhausted"],
        "allowed_tools": [],
        "risk_level": "sandbox",
        "budget": {"max_tokens": 1000, "max_attempts": 1},
        "task_kind": "code",
        "workspace_seed": {},
    }
    assert spec.to_json() == expected
    encoded = json.dumps(expected, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert hashlib.sha256(encoded.encode()).hexdigest() == hashlib.sha256(
        json.dumps(spec.to_json(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def test_new_protocol_round_trips_and_unknown_is_rejected() -> None:
    spec = _spec("round-trip", planning_protocol_version=PLANNING_DECISION_V1)
    assert MissionSpec.from_json(spec.to_json()).to_json() == spec.to_json()
    with pytest.raises(ContractError, match="planning protocol"):
        _spec("unknown", planning_protocol_version="planning-decision-v99")


def test_new_protocol_creation_writes_one_binding_with_frozen_hash(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    mission, created = service.create_mission(
        _spec("new", planning_protocol_version=PLANNING_DECISION_V1)
    )
    assert created
    binding = planning_protocol_for_mission(store, mission.id)
    assert binding is not None
    assert binding["protocol_version"] == PLANNING_DECISION_V1
    assert binding["package_version"] == 4
    assert binding["prompt_version"] == "planner-hierarchical-v8"
    expected = {"protocol_version": PLANNING_DECISION_V1, "package_version": 4, "prompt_version": "planner-hierarchical-v8"}
    assert binding["binding_hash"] == hashlib.sha256(
        json.dumps(expected, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert store.connection.execute(
        "SELECT count(*) FROM mission_planning_protocols WHERE mission_id = ?", (mission.id,)
    ).fetchone()[0] == 1


def test_legacy_creation_has_no_binding(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    mission, _ = CommitService(store).create_mission(_spec("legacy"))
    assert planning_protocol_for_mission(store, mission.id) is None
    assert LEGACY_PLANNING_PROTOCOL == "legacy-plan-proposal-v1"


def test_binding_is_transactional_on_creation_failure(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store, mission_profile_validator=lambda _profile, _params: (_ for _ in ()).throw(CommitRejected("boom")))
    with pytest.raises(CommitRejected, match="boom"):
        service.create_mission(
            _spec("rollback", planning_protocol_version=PLANNING_DECISION_V1, runtime_profile_id="p")
        )
    assert store.connection.execute("SELECT count(*) FROM mission_planning_protocols").fetchone()[0] == 0
    assert store.find_mission("tenant", "rollback") is None


def test_replay_is_idempotent_and_protocol_cannot_change(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    spec = _spec("idem", planning_protocol_version=PLANNING_DECISION_V1)
    mission, created = service.create_mission(spec)
    again, replayed = service.create_mission(spec)
    assert again == mission and not replayed
    with pytest.raises(MissionConflict):
        service.create_mission(replace(spec, planning_protocol_version=LEGACY_PLANNING_PROTOCOL))


def test_binding_survives_a_new_connection(tmp_path) -> None:
    path = tmp_path / "orchestrator.db"
    first = Store.open(path)
    mission, _ = CommitService(first).create_mission(
        _spec("restart", planning_protocol_version=PLANNING_DECISION_V1)
    )
    first.close()
    second = Store.open(path)
    assert planning_protocol_for_mission(second, mission.id)["protocol_version"] == PLANNING_DECISION_V1
