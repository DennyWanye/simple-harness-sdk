# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-S red tests: the Mission planning-protocol switch and durable binding."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from agent_orchestrator.contracts import Budget, ContractError
from agent_orchestrator.contracts.planning_decisions import (
    LEGACY_PLANNING_PROTOCOL as CONTRACT_LEGACY_PLANNING_PROTOCOL,
)
from agent_orchestrator.contracts.planning_decisions import (
    PLANNING_DECISION_V1 as CONTRACT_PLANNING_DECISION_V1,
)
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
        "budget": {
            "max_tokens": 1000,
            "max_cost_micros": None,
            "max_attempts": 1,
            "max_runtime_seconds": None,
            "max_concurrency": None,
            "max_tool_calls": None,
        },
        "task_kind": "code",
        "workspace_seed": {},
    }
    assert spec.to_json() == expected
    encoded = json.dumps(expected, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert hashlib.sha256(encoded.encode()).hexdigest() == (
        "d0903d35e0062418f244304251be0f3f87a8885e3bef5170c5b6a30f3b0dd161"
    )


def test_new_protocol_json_key_and_unknown_values_are_rejected() -> None:
    spec = _spec("round-trip", planning_protocol_version=PLANNING_DECISION_V1)
    assert spec.to_json()["planning_protocol_version"] == PLANNING_DECISION_V1
    with pytest.raises(ContractError, match="planning protocol"):
        _spec("unknown", planning_protocol_version="planning-decision-v99")
    for invalid in ([], {}):
        with pytest.raises(ContractError, match="planning protocol"):
            _spec("invalid-type", planning_protocol_version=invalid)


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
    expected = {
        "protocol_version": PLANNING_DECISION_V1,
        "package_version": 4,
        "prompt_version": "planner-hierarchical-v8",
    }
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
    assert LEGACY_PLANNING_PROTOCOL == CONTRACT_LEGACY_PLANNING_PROTOCOL
    assert PLANNING_DECISION_V1 == CONTRACT_PLANNING_DECISION_V1


def test_binding_is_transactional_on_creation_failure(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    observed: dict[str, object] = {}

    def fail_after_binding(mission: object) -> None:
        observed["binding"] = planning_protocol_for_mission(store, mission.id)  # type: ignore[attr-defined]
        raise CommitRejected("boom")

    service._reserve_mission_system_pools = fail_after_binding  # type: ignore[method-assign]
    with pytest.raises(CommitRejected, match="boom"):
        service.create_mission(
            _spec(
                "rollback",
                planning_protocol_version=PLANNING_DECISION_V1,
            )
        )
    assert observed["binding"] is not None
    assert (
        store.connection.execute("SELECT count(*) FROM mission_planning_protocols").fetchone()[0]
        == 0
    )
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


def test_legacy_mission_cannot_be_replayed_as_new_protocol(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    spec = _spec("legacy-first")
    service.create_mission(spec)
    with pytest.raises(MissionConflict):
        service.create_mission(replace(spec, planning_protocol_version=PLANNING_DECISION_V1))


def test_policy_snapshot_digest_does_not_include_planning_protocol(tmp_path) -> None:
    from agent_orchestrator.governance.policies import policy_snapshot
    from agent_orchestrator.runtime.assembly import OrchestratorConfig

    legacy = _spec("policy-legacy")
    enabled = replace(
        legacy,
        idempotency_key="policy-enabled",
        planning_protocol_version=PLANNING_DECISION_V1,
    )
    assert enabled.to_json() != legacy.to_json()
    first = policy_snapshot(OrchestratorConfig(evidence_root=tmp_path / "legacy"))
    second = policy_snapshot(OrchestratorConfig(evidence_root=tmp_path / "enabled"))
    assert first["hash"] == second["hash"]
    assert "planning_protocol_version" not in first["config"]
    assert "planning_protocol_version" not in json.dumps(first)

    store = Store.open(tmp_path / "policy.db")
    service = CommitService(store)
    legacy_mission, _ = service.create_mission(legacy)
    enabled_mission, _ = service.create_mission(enabled)
    legacy_binding = store.get_mission_policy(legacy_mission.id)
    enabled_binding = store.get_mission_policy(enabled_mission.id)
    assert legacy_binding is not None and enabled_binding is not None
    legacy_policy = store.get_policy_version(legacy_binding["version_id"])
    enabled_policy = store.get_policy_version(enabled_binding["version_id"])
    assert legacy_policy is not None and enabled_policy is not None
    assert legacy_policy["params_hash"] == enabled_policy["params_hash"]
    assert legacy_policy["params"] == enabled_policy["params"]
    assert "planning_protocol_version" not in legacy_policy["params"]
    assert "planning_protocol_version" not in enabled_policy["params"]


def test_binding_survives_a_new_connection(tmp_path) -> None:
    path = tmp_path / "orchestrator.db"
    first = Store.open(path)
    mission, _ = CommitService(first).create_mission(
        _spec("restart", planning_protocol_version=PLANNING_DECISION_V1)
    )
    first.close()
    second = Store.open(path)
    assert (
        planning_protocol_for_mission(second, mission.id)["protocol_version"]
        == PLANNING_DECISION_V1
    )
