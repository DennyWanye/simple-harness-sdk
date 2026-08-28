# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib

import pytest

from simple_harness import HostControlAuthorityV1, HostControlRunStartV1
from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.runtime.start_snapshot import StartSnapshot, bind_start_snapshot


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


def test_v6_host_control_snapshot_strict_roundtrip() -> None:
    control = _control()
    snapshot = bind_start_snapshot(
        control.to_run_start(), profile_key="agent.general", driver_kind="react"
    )
    raw = snapshot.to_json()
    assert raw["schema_version"] == 6
    assert StartSnapshot.from_json(raw) == snapshot
    ordinary = StartSnapshot("agent.general", "react", "ordinary", 1, {}).to_json()
    assert ordinary["schema_version"] == 5
    assert "start_mode" not in ordinary


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


@pytest.mark.parametrize("generation", (True, 1.0, "1", None))
def test_host_control_authority_generation_requires_non_bool_int(generation) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError)):
        HostControlAuthorityV1("purpose", "ref", "a" * 64, generation)
