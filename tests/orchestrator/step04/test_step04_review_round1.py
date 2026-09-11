# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 4 · code review round 1 dispositions (journal §3): P0-1 / P1-2 system-task budgets
inherit every Mission-bounded dimension and a budget problem never rolls an accept back,
P1-3 / P1-4 one canonical workspace path for the untrusted-source rule, P1-5 a v1 library
with duplicate artifact lineage rows still upgrades, P2-13 supersession keeps the record's
version, real-run finding: SDK error payloads with tuples enter the formal record."""

from __future__ import annotations

import sqlite3

import pytest
from knowledge_helpers import (
    claim,
    drive_to_running,
    envelope,
    node,
    passed_layers,
    submit,
    two_branch_service,
)

from agent_orchestrator.artifacts.paths import normalise_workspace_path, under_prefix
from agent_orchestrator.contracts import Budget, ClaimStatus, TaskStatus, jsonable
from agent_orchestrator.graph.task_graph import GraphRejected, TaskGraphProposal, validate_graph
from agent_orchestrator.memory.claims import grade_claim
from agent_orchestrator.planning.manager import conflict_task, inherit_limits
from agent_orchestrator.runtime.tool_gateway import is_untrusted
from agent_orchestrator.storage import schema
from agent_orchestrator.storage.store import Store
from agent_orchestrator.testing.fixtures import COMPARE_SYNTHESIS

KEY = "impl_a.empty_input"


# ------------------------------------------------------------------ P0-1 / P1-2
def test_conflict_and_synthesis_budgets_inherit_every_bounded_dimension(tmp_path):
    parent = Budget(
        max_tokens=100_000, max_attempts=6, max_cost_micros=1_000_000, max_runtime_seconds=600
    )
    child = inherit_limits(Budget(max_tokens=20_000, max_attempts=2), parent)
    assert (
        child.fits_within(parent)
        and child.max_cost_micros == 1_000_000
        and child.max_runtime_seconds == 600
    )
    service, mission, (task_a, task_b, _synthesis) = two_branch_service(
        tmp_path,
        conflict_reserve_tokens=20_000,
        synthesis=COMPARE_SYNTHESIS,
        budget=Budget(max_tokens=200_000, max_attempts=6, max_cost_micros=1_000_000),
    )
    assert (
        service.store.list_tasks(mission.id)[-1].kind == "synthesis"
    )  # P1-2: committed under the cost bound
    a1 = drive_to_running(service, task_a)
    sa = submit(
        service,
        a1,
        envelope(
            a1,
            claims=[
                claim(
                    "抛错",
                    key=KEY,
                    stance="refutes",
                    evidence=["pytest:tests/probe/test_impl_a.py"],
                )
            ],
        ),
    )
    service.accept_result(
        sa.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    b1 = drive_to_running(service, task_b, agent="agent-2", turn="turn-2")
    sb = submit(
        service,
        b1,
        envelope(
            b1,
            claims=[
                claim(
                    "返回 {}",
                    key=KEY,
                    stance="affirms",
                    evidence=["pytest:tests/probe/test_impl_b.py"],
                )
            ],
            artifacts=("tests/probe/test_impl_b.py",),
        ),
        artifact_paths=("tests/probe/test_impl_b.py",),
        turn="turn-2",
    )
    completed = service.accept_result(
        sb.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_b.py")
    )
    assert completed.status is TaskStatus.COMPLETED  # the accept landed
    conflict = service.store.list_tasks(mission.id)[-1]
    assert conflict.kind == "conflict" and conflict.budget.max_cost_micros == 1_000_000
    assert service.store.list_conflicts(mission.id)[0]["state"] == "OPEN"


def test_a_budget_problem_defers_the_conflict_instead_of_rolling_the_accept_back(
    tmp_path, monkeypatch
):
    from agent_orchestrator.governance.budgets import BudgetError, BudgetLedger

    service, mission, (task_a, task_b) = two_branch_service(
        tmp_path, conflict_reserve_tokens=20_000
    )
    a1 = drive_to_running(service, task_a)
    sa = submit(
        service,
        a1,
        envelope(
            a1,
            claims=[
                claim(
                    "抛错",
                    key=KEY,
                    stance="refutes",
                    evidence=["pytest:tests/probe/test_impl_a.py"],
                )
            ],
        ),
    )
    service.accept_result(
        sa.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    original = BudgetLedger.open_account

    def refuse(self, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs["scope"] == "task" and ":task-3" in kwargs["account_id"]:
            raise BudgetError("simulated ledger refusal")
        return original(self, **kwargs)

    monkeypatch.setattr(BudgetLedger, "open_account", refuse)
    b1 = drive_to_running(service, task_b, agent="agent-2", turn="turn-2")
    sb = submit(
        service,
        b1,
        envelope(
            b1,
            claims=[
                claim(
                    "返回 {}",
                    key=KEY,
                    stance="affirms",
                    evidence=["pytest:tests/probe/test_impl_b.py"],
                )
            ],
            artifacts=("tests/probe/test_impl_b.py",),
        ),
        artifact_paths=("tests/probe/test_impl_b.py",),
        turn="turn-2",
    )
    completed = service.accept_result(
        sb.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_b.py")
    )
    assert completed.status is TaskStatus.COMPLETED
    assert len(service.store.list_tasks(mission.id)) == 2
    conflict = service.store.list_conflicts(mission.id)[0]
    assert conflict["state"] == "DEFERRED" and conflict["deferred_reason"].startswith(
        "budget_unavailable"
    )
    assert service.store.list_claims(sb.envelope.id)[0].status is ClaimStatus.DISPUTED
    assert service.store.count_events(mission.id, "ConflictOpenDeferred") == 1


def test_a_synthesis_template_over_the_mission_budget_is_a_graph_rejection(tmp_path):
    service, mission, _ = two_branch_service(tmp_path / "ok", key="ok", synthesis=COMPARE_SYNTHESIS)
    over = Store.open(tmp_path / "over.db")
    from knowledge_helpers import spec

    from agent_orchestrator.orchestrator.commit_service import CommitService

    other = CommitService(over)
    created, _ = other.create_mission(
        spec(  # more attempts than the Mission allows: not a sum problem, the template's own bound
            "over",
            synthesis={**COMPARE_SYNTHESIS, "budget": {"max_tokens": 10_000, "max_attempts": 99}},
        )
    )
    planning = other.begin_planning(created.id)
    with pytest.raises(GraphRejected) as rejected:
        validate_graph(planning, TaskGraphProposal.from_json({"tasks": [node("A"), node("B")]}))
    assert rejected.value.reason == "budget" and "synthesis" in str(rejected.value)


# ------------------------------------------------------------------ P1-3 / P1-4
def test_one_canonical_path_backs_the_untrusted_rule():
    assert normalise_workspace_path("./docs/./vendor_notes.md") == "docs/vendor_notes.md"
    assert (
        normalise_workspace_path("/docs//x.md") == "docs/x.md"
        and normalise_workspace_path("docs\\x.md") == "docs/x.md"
    )
    for path in (
        "docs/vendor_notes.md",
        "./docs/vendor_notes.md",
        "./docs/./vendor_notes.md",
        "docs//vendor_notes.md",
        "/docs/vendor_notes.md",
    ):
        assert is_untrusted(path, ("docs/",)) and under_prefix(path, ["docs"])
    assert not is_untrusted("docsx/vendor_notes.md", ("docs/",)) and not is_untrusted(
        "notes/docs/x.md", ("docs/",)
    )
    for evidence in (
        ["pytest:./docs/vendor_notes.md"],
        ["./docs/vendor_notes.md"],
        ["docs/../docs/vendor_notes.md"],
    ):
        grade = grade_claim(
            "c",
            evidence,
            verifier_results=[],
            artifact_paths=["docs/vendor_notes.md", "notes/x.md"],
            untrusted_prefixes=["docs/"],
        )
        assert grade.status is ClaimStatus.UNDER_REVIEW and grade.basis["grade"] == "unsupported", (
            evidence
        )


# ------------------------------------------------------------------ P1-5
def test_v1_library_with_duplicate_lineage_rows_upgrades_and_renumbers(tmp_path):
    path = tmp_path / "orchestrator.db"
    connection = sqlite3.connect(path)
    for statement in schema.MIGRATIONS[0].ddl.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute(
        "INSERT INTO orch_schema_migrations VALUES (?,?,?,?)",
        (1, schema.MIGRATIONS[0].name, schema.MIGRATIONS[0].checksum, 1.0),
    )
    connection.execute("INSERT INTO missions VALUES ('m1','t','k','ACTIVE',1,'h','{}',1.0,1.0)")
    connection.execute("INSERT INTO tasks VALUES ('m1:task-1','m1',1,'COMPLETED',1,'{}',1.0)")
    for ordinal in (1, 2):
        connection.execute(
            "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"m1:task-1:attempt-{ordinal}",
                "m1:task-1",
                "m1",
                ordinal,
                "COMPLETED",
                1,
                None,
                None,
                None,
                None,
                "{}",
                1.0,
            ),
        )
    for ordinal, digest in ((1, "a" * 64), (2, "b" * 64)):  # the L3-2 defect: both got version 1
        artifact_id = f"artifact-{ordinal}"
        document = {
            "id": artifact_id,
            "mission_id": "m1",
            "task_id": "m1:task-1",
            "attempt_id": f"m1:task-1:attempt-{ordinal}",
            "type": "file",
            "path": "x.py",
            "version": 1,
            "content_hash": digest,
            "size_bytes": 1,
            "produced_by": "a",
        }
        import json

        connection.execute(
            "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)",
            (
                artifact_id,
                "m1",
                "m1:task-1",
                f"m1:task-1:attempt-{ordinal}",
                "x.py",
                digest,
                1,
                json.dumps(document),
                float(ordinal),
            ),
        )
    connection.commit()
    connection.close()
    store = Store.open(path)
    versions = {a.id: a.version for a in store.list_mission_artifacts("m1")}
    assert versions == {"artifact-1": 1, "artifact-2": 2}
    assert (
        store.connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='artifacts_lineage_idx'"
        ).fetchone()[0]
        == 1
    )
    store.close()


# ------------------------------------------------------------------ P2-13 / real-run finding
def test_supersession_keeps_the_record_version_and_sdk_errors_are_jsonable(tmp_path):
    service, mission, (t1, t2) = two_branch_service(tmp_path)
    a1 = drive_to_running(service, t1)
    s1 = submit(
        service,
        a1,
        envelope(a1, claims=[claim("v1", key="k", evidence=["pytest:tests/probe/test_impl_a.py"])]),
    )
    service.accept_result(
        s1.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    old = service.store.list_knowledge(mission.id)[0]
    service._supersede_knowledge(old.id, by="result-x:claim-9")
    assert service.store.get_knowledge(old.id).version == old.version == 1
    assert jsonable({"error": {"output_cap_escalations": (1, 2), "kinds": {"b", "a"}}}) == {
        "error": {"output_cap_escalations": [1, 2], "kinds": ["a", "b"]}
    }
    b1 = drive_to_running(service, t2, agent="agent-2", turn="turn-2")
    rejected = service.reject_result(
        b1.id,
        turn_id="turn-2",
        reason="turn_failed",
        detail={"error": {"output_cap_escalations": (4096, 8192)}},
    )
    assert rejected.failure["error"]["output_cap_escalations"] == [4096, 8192]
    assert conflict_task  # imported for the type; the budget path is covered above
