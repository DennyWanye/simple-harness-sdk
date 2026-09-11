# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 3 leftover L3-2 (D4-15): two candidates of one Task that write the same path get
distinct versions along the (mission, path) lineage — the version is assigned inside the
``record_result`` transaction and guarded by a UNIQUE index, never by the caller."""

from __future__ import annotations

import sqlite3

import pytest
from knowledge_helpers import (
    HASH_A,
    HASH_B,
    artifact,
    claim,
    drive_to_running,
    envelope,
    two_branch_service,
)


def test_same_path_candidates_get_distinct_versions(tmp_path):
    service, mission, (task_a, _) = two_branch_service(tmp_path)
    first = drive_to_running(service, task_a)
    # a second candidate on the same Task (candidates_per_task=2)
    from agent_orchestrator.orchestrator.commit_service import Reservation

    second, intent = service.create_attempt(
        task_a.id,
        role="worker",
        model="agent-model",
        prompt_version="worker-v2",
        context_version="ctx",
        reservation=Reservation(tokens=4_000, cost_micros=0),
        intent_config={"agent_config": {}, "message": "do"},
        input_hash="h2",
        candidates_per_task=2,
    )
    service.claim_intent(intent.intent_id, owner="orch-1", lease_seconds=60)
    service.record_agent_created(intent.intent_id, agent_id="agent-2", expected_turn_id="turn-2")
    service.record_submitted(intent.intent_id, receipt={"turn_id": "turn-2", "seq": 2})
    second = service.store.get_attempt(second.id)
    path = "tests/probe/test_impl_a.py"
    # both candidates snapshot the same provisional version 1 (they read the lineage at the same time)
    s1 = service.record_result(
        first.id,
        envelope=envelope(first, claims=[claim("c1")]),
        turn_id="turn-1",
        artifacts=[artifact(first, path, HASH_A, version=1)],
        usage_refs=(),
    )
    s2 = service.record_result(
        second.id,
        envelope=envelope(second, claims=[claim("c2")]),
        turn_id="turn-2",
        artifacts=[artifact(second, path, HASH_B, version=1)],
        usage_refs=(),
    )
    versions = sorted(
        (a.version, a.attempt_id)
        for a in service.store.list_mission_artifacts(mission.id)
        if a.path == path
    )
    assert versions == [(1, first.id), (2, second.id)]
    assert service.store.get_artifact(s1.artifacts[0]).version == 1
    assert service.store.get_artifact(s2.artifacts[0]).version == 2
    with pytest.raises(sqlite3.IntegrityError):  # the lineage is guarded by the schema itself
        with service.store.transaction() as connection:
            connection.execute(
                "INSERT INTO artifacts(artifact_id,mission_id,task_id,attempt_id,path,content_hash,version,json,created_at)"
                " VALUES ('dup', ?, ?, ?, ?, ?, 2, '{}', 1.0)",
                (mission.id, task_a.id, second.id, path, HASH_B),
            )


def test_duplicate_delivery_of_the_same_turn_keeps_the_version(tmp_path):
    service, mission, (task_a, _) = two_branch_service(tmp_path)
    first = drive_to_running(service, task_a)
    path = "tests/probe/test_impl_a.py"
    s1 = service.record_result(
        first.id,
        envelope=envelope(first, claims=[claim("c1")]),
        turn_id="turn-1",
        artifacts=[artifact(first, path, HASH_A, version=1)],
        usage_refs=(),
    )
    again = service.record_result(
        first.id,
        envelope=envelope(first, claims=[claim("c1")]),
        turn_id="turn-1",
        artifacts=[artifact(first, path, HASH_A, version=1)],
        usage_refs=(),
    )
    assert again == s1
    versions = [
        a.version for a in service.store.list_mission_artifacts(mission.id) if a.path == path
    ]
    assert versions == [1]
