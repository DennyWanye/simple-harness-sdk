# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib

import pytest

from simple_harness import HostControlAuthorityV1, HostControlRunStartV1
from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.execution.context_authority import ContextRouteOrigin, ContextRouteReceipt
from simple_harness.runtime.start_snapshot import RunStart, StartSnapshot, bind_start_snapshot
from simple_harness.runtime.task_scope_protocol import TaskScopeRoute


def _control() -> HostControlRunStartV1:
    return HostControlRunStartV1(
        ExecutionSessionId("host-session"),
        RunId("host-run"),
        RequestId("host-request"),
        "host-turn",
        {"opaque": "value"},
        3,
        HostControlAuthorityV1(
            "skill.install.verify",
            "attempt:one",
            hashlib.sha256(b"attempt:one").hexdigest(),
            2,
        ),
        "host-user",
        "a" * 64,
        "b" * 64,
    )


def _initial_route(run_id: str = "run-1") -> ContextRouteReceipt:
    return ContextRouteReceipt(
        "route-initial-1",
        run_id,
        None,
        None,
        TaskScopeRoute.RESUME_EXISTING,
        "task-1",
        3,
        schema_version=3,
        binding_set_receipt_id="binding-set-3",
        binding_set_receipt_hash="c" * 64,
        origin=ContextRouteOrigin.HOST_INITIAL,
        host_authority_ref="host-execution:claim-1",
        host_authority_hash="d" * 64,
    )


def test_v6_host_control_snapshot_strict_roundtrip() -> None:
    control = _control()
    snapshot = bind_start_snapshot(
        control.to_run_start(), profile_key="agent.general", driver_kind="react"
    )
    raw = snapshot.to_json()
    assert raw["schema_version"] == 6
    assert StartSnapshot.from_json(raw) == snapshot
    ordinary = StartSnapshot("agent.general", "react", "ordinary", 1, {}).to_json()
    assert ordinary["schema_version"] == 7
    assert "start_mode" not in ordinary
    assert ordinary["initial_route_receipt"] is None
    assert ordinary["initial_route_receipt_hash"] is None


@pytest.mark.parametrize(
    "mutation",
    (
        {"start_mode": "ordinary"},
        {"host_control_authority": None},
        {"host_control_authority": "not-an-object"},
        {"host_control_authority": []},
        {"host_control_user_id": None},
        {"conversation": {"unexpected": True}},
    ),
)
def test_v6_rejects_malformed_host_control_mixes(mutation) -> None:  # type: ignore[no-untyped-def]
    raw = bind_start_snapshot(
        _control().to_run_start(), profile_key="agent.general", driver_kind="react"
    ).to_json()
    raw.update(mutation)
    with pytest.raises((KeyError, TypeError, ValueError)):
        StartSnapshot.from_json(raw)


@pytest.mark.parametrize("schema_version", (1, 2, 3, 4, 5))
def test_pre_v6_snapshots_are_always_ordinary(schema_version: int) -> None:
    raw = {
        "schema_version": schema_version,
        "profile_key": "agent.general",
        "driver_kind": "react",
        "turn_id": "legacy-turn",
        "tool_catalog_generation": 1,
        "input": {},
    }
    parsed = StartSnapshot.from_json(raw)
    assert parsed.start_mode == "ordinary"
    assert parsed.host_control_authority is None
    assert parsed.initial_route_receipt is None
    assert parsed.initial_route_receipt_hash is None


def test_v7_ordinary_snapshot_freezes_initial_route_and_rejects_tamper() -> None:
    route = _initial_route()
    start = RunStart(
        ExecutionSessionId("session-1"),
        RunId("run-1"),
        RequestId("request-1"),
        "turn-1",
        {},
        1,
        initial_route_receipt=route,
        initial_route_receipt_hash=route.receipt_hash,
    )
    snapshot = bind_start_snapshot(start, profile_key="agent.general", driver_kind="react")
    raw = snapshot.to_json()
    assert raw["schema_version"] == 7
    assert raw["initial_route_receipt"] == route.to_json()
    assert raw["initial_route_receipt_hash"] == route.receipt_hash
    assert StartSnapshot.from_json(raw) == snapshot

    for field, replacement in (
        ("binding_set_receipt_id", "binding-set-tampered"),
        ("host_authority_ref", "host-execution:tampered"),
    ):
        tampered = dict(raw)
        route_json = dict(route.to_json())
        route_json[field] = replacement
        tampered["initial_route_receipt"] = route_json
        with pytest.raises(ValueError, match="hash differs"):
            StartSnapshot.from_json(tampered)


def test_run_start_rejects_initial_route_from_another_run() -> None:
    route = _initial_route("run-other")
    with pytest.raises(ValueError, match="another Run"):
        RunStart(
            ExecutionSessionId("session-1"),
            RunId("run-1"),
            RequestId("request-1"),
            "turn-1",
            {},
            1,
            initial_route_receipt=route,
            initial_route_receipt_hash=route.receipt_hash,
        )


def test_v7_rejects_host_control_downgrade_mix() -> None:
    raw = StartSnapshot("agent.general", "react", "ordinary", 1, {}).to_json()
    raw["start_mode"] = "host_control"
    raw["host_control_authority"] = _control().authority.to_json()
    raw["host_control_user_id"] = "host-user"
    with pytest.raises(ValueError, match="ordinary"):
        StartSnapshot.from_json(raw)


@pytest.mark.parametrize("generation", (True, 1.0, "1", None))
def test_host_control_authority_generation_requires_non_bool_int(generation) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError)):
        HostControlAuthorityV1("purpose", "ref", "a" * 64, generation)
