# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3k / defect N3: the legacy artifact merge must not overturn a root resolution.

The Grok acceptance episodes H-L3-C3-r0 and H-L3-C3-r1 both ended:

    HierarchicalRootReviewCut → review record ACCEPT (both root criteria PASS)
    → ObligationDemandWithdrawn → GoalResolutionCommitted{is_mission_root, SATISFIED}
    → MissionFailed{stop_reason: artifact_conflict,
         "REPORT.md is produced by both task-544f… and task-68e2… (independent branches)"}

with the hidden grader PASS on both.  ``_judge`` → ``_evaluate_criteria`` built the
Mission Judge's integrated tree with
:func:`~agent_orchestrator.artifacts.versioning.merge_accepted`,
whose "independent branches" is read off ``Task.dependency_ids`` — and in the
hierarchical mode that field is empty by design (ordering is the typed network's
``order_constraints``, §18.5 constraint 4).  So any two leaves that wrote the same path
with different bytes — every leaf writes ``REPORT.md`` when the user's goal says
"write what you did in REPORT.md" — were a conflict, and a Mission whose root
``GoalResolution`` already stood was failed for it.

The fix is a mode branch in the same shape as P2.3d's D4: on a hierarchical Mission
the judgment tree is read from the root resolution's own contributions (the CURRENT
acceptances of the adopted plan) and the accepted outputs P2.3h indexed on declared
ports, later acceptances overriding earlier ones and a criterion-linked step's output
overriding the rest — never from ``dependency_ids``, and never raising
``ArtifactConflict``.  The legacy merge is untouched and still the legacy Mission's rule.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_htn_end_to_end as e2e  # noqa: E402
from test_htn_end_to_end import (  # noqa: E402
    World,
    _leaf_task,
    _review_task,
    committed,
)
from test_root_review_evidence import _accept_with  # noqa: E402

from agent_orchestrator.artifacts.versioning import ArtifactConflict, merge_accepted  # noqa: E402
from agent_orchestrator.contracts import Budget, Task, TaskStatus  # noqa: E402
from agent_orchestrator.contracts.models import Artifact, Attempt, MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import (
    TERMINAL_MISSION,  # noqa: E402
    AttemptStatus,  # noqa: E402
)
from agent_orchestrator.orchestrator.commit_service import (  # noqa: E402
    ARTIFACT_MERGE_NOT_APPLICABLE,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.state_machine import next_task  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider  # noqa: E402

REPORT = "REPORT.md"
LEAF_REPORT = b"# facts leaf\n\nI read the repository and wrote nothing else.\n"
REVIEW_REPORT = b"# verify leaf\n\nThe named test now passes: 2 passed.\n"


# ======================================================================================
# 1. The defect, pinned on the legacy function with the C3 shape
# ======================================================================================


def _c3_shaped_task(task_id: str, artifacts: tuple[str, ...]) -> Task:
    """A materialised occurrence row: ``dependency_ids=()`` and its accepted artifacts."""

    return Task(
        id=task_id,
        mission_id="mission-c3",
        parent_task_ids=(),
        dependency_ids=(),
        goal="g",
        rationale="r",
        success_criteria=("c-test-passes",),
        verification_policy=("format_check",),
        allowed_tools=(),
        budget=Budget(max_tokens=10),
        priority=1,
        status=TaskStatus.COMPLETED,
        version=1,
        accepted_artifacts=artifacts,
    )


def test_the_legacy_merge_reads_two_htn_leaves_writing_report_md_as_a_conflict() -> None:
    """What C3-r0 hit, reduced to the function: same path, different bytes, no
    ``dependency_ids`` — and that is still the legacy rule, deliberately unchanged."""

    reproduce = _c3_shaped_task("task-68e2", ("artifact-reproduce-report",))
    patch = _c3_shaped_task("task-544f", ("artifact-patch-report",))
    by_task = {
        reproduce.id: [
            Artifact(
                id="artifact-reproduce-report",
                mission_id="mission-c3",
                task_id=reproduce.id,
                attempt_id="a-1",
                type="file",
                path=REPORT,
                version=1,
                content_hash="7e95c3a2af" + "0" * 54,
                size_bytes=1,
                produced_by="w",
                storage_uri="",
            )
        ],
        patch.id: [
            Artifact(
                id="artifact-patch-report",
                mission_id="mission-c3",
                task_id=patch.id,
                attempt_id="a-2",
                type="file",
                path=REPORT,
                version=1,
                content_hash="439741f549" + "0" * 54,
                size_bytes=1,
                produced_by="w",
                storage_uri="",
            )
        ],
    }
    with pytest.raises(ArtifactConflict, match="independent branches"):
        merge_accepted(
            [reproduce, patch], by_task, tasks_by_id={reproduce.id: reproduce, patch.id: patch}
        )


# ======================================================================================
# 2. End to end on a real ``Orchestrator``: two leaves, one path, root review ACCEPT
# ======================================================================================


def _world_with_report_criterion(tmp_path, *, key: str) -> World:
    """The shared ``plan.goal`` fixture, but the Mission's own criterion is the file both
    leaves write — the C3 shape (``success_criteria = ["file:REPORT.md"]``)."""

    original = e2e._spec

    def spec(*args: Any, **kwargs: Any):
        return dataclasses.replace(
            original(*args, **kwargs), success_criteria=(f"file:{REPORT}",)
        )

    e2e._spec = spec
    try:
        return committed(tmp_path, key=key, demand=True)
    finally:
        e2e._spec = original


def _stored_file(
    world: Any, task_id: str, *, artifact_id: str, path: str, data: bytes, version: int = 1
) -> Artifact:
    """One Attempt row per leaf, any number of artifact rows on it, bytes in the store.

    ``world`` is anything with ``store``, ``mission`` and ``path`` (the library file)."""

    import hashlib

    store = world.store
    attempt_id = f"{task_id}:attempt-1"
    if store.get_attempt(attempt_id) is None:
        store.insert_attempt(
            Attempt(
                id=attempt_id,
                task_id=task_id,
                mission_id=world.mission.id,
                role="worker",
                model="fixture",
                prompt_version="worker-hierarchical-v2",
                context_version="ctx",
                budget_reserved=Budget(max_tokens=1),
                lease_owner=None,
                lease_expires_at=None,
                status=AttemptStatus.COMPLETED,
                retry_of=None,
                created_at=1.0,
                version=1,
                ordinal=1,
                creation_key=f"k-{attempt_id}",
                input_id="i",
                failure=None,
            )
        )
    digest = hashlib.sha256(data).hexdigest()
    blob = world.path.parent / "artifacts" / "sha256" / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(data)
    artifact = Artifact(
        id=artifact_id,
        mission_id=world.mission.id,
        task_id=task_id,
        attempt_id=attempt_id,
        type="file",
        path=path,
        version=version,  # Mission-wide lineage per path (D3-8'): C3's REPORT.md ran 1..4
        content_hash=digest,
        size_bytes=len(data),
        produced_by="agent-worker",
        storage_uri=str(blob),
    )
    store.upsert_artifact(artifact)
    return artifact


def _complete_leaf(
    world: Any,
    task_id: str,
    *,
    port_path: str,
    port_data: bytes,
    report: bytes,
    report_version: int,
    now_ms: int,
    port_version: int = 1,
) -> None:
    """A Worker's PASS as the loop leaves it: the legacy ``accept_result`` lifecycle
    (Task COMPLETED with its ``accepted_artifacts``) plus the hierarchical ``Acceptance``
    with the declared port claimed.  Both files are the leaf's; only the port file is
    indexed as an accepted output — exactly what C3's leaves did with ``REPORT.md``."""

    port_artifact = _stored_file(
        world,
        task_id,
        artifact_id=f"artifact-{task_id}-port",
        path=port_path,
        data=port_data,
        version=port_version,
    )
    report_artifact = _stored_file(
        world,
        task_id,
        artifact_id=f"artifact-{task_id}-report",
        path=REPORT,
        data=report,
        version=report_version,
    )
    task = world.store.get_task(task_id)
    assert task is not None
    # READY → ACTIVE → VERIFYING → COMPLETED: the lifecycle ``accept_result`` closes,
    # stepped through the same transition table, then stored in one update.
    completed = next_task(
        next_task(next_task(task, TaskStatus.ACTIVE), TaskStatus.VERIFYING),
        TaskStatus.COMPLETED,
        accepted_result_id=f"result-{task_id}",
        accepted_artifacts=(port_artifact.id, report_artifact.id),
    )
    world.store.update_task(completed, expected_version=task.version)
    _accept_with(
        world.service,
        world.dispatch,
        world.mission.id,
        task_id,
        artifacts=(port_artifact, report_artifact),
        now_ms=now_ms,
    )


def _two_leaves_wrote_report(tmp_path, *, key: str) -> World:
    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _world_with_report_criterion(evidence, key=key)
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    _complete_leaf(
        world,
        _leaf_task(world),
        port_path="out/result.json",
        port_data=b'{"result": "alpha"}',
        report=LEAF_REPORT,
        report_version=1,
        now_ms=1_000_000,
    )
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_050_000)
    _complete_leaf(
        world,
        _review_task(world),
        port_path="out/verdict.json",
        port_data=b"verdict: PASS",
        report=REVIEW_REPORT,
        report_version=2,
        now_ms=1_100_000,
    )
    return world


def _accepting_reviewer(request: Any) -> str:
    """PASS on every root criterion the package names (rule 2: ids copied verbatim)."""

    shown = json.loads(
        next(m.content for m in reversed(request.messages) if str(m.role).endswith("user"))
    )
    return (
        "<critic_verdict>"
        + json.dumps(
            {
                "verdict": "PASS",
                "findings": [],
                "mission_criteria": [
                    {"criterion": item["criterion_id"], "met": True, "reason": "scripted"}
                    for item in shown["criteria"]
                ],
            }
        )
        + "</critic_verdict>"
    )


def _run(world: Any, tmp_path, *, cycles: int = 30, planning: Any = None) -> dict[str, Any]:
    """``run()``'s body a cycle at a time over the fixture's own library file.

    ``planning`` is the object ``install_hierarchical`` is given; the shared fixture's
    ``Env`` by default, a ``PlanningWorld`` for the code-domain worlds."""

    evidence = Path(tmp_path) / "evidence"
    world.store.close()
    provider = RoleScriptedProvider({"root_reviewer": [_accepting_reviewer]})
    planning = world.env if planning is None else planning

    async def case() -> dict[str, Any]:
        config = OrchestratorConfig(
            evidence_root=evidence, max_concurrency=1, test_timeout_seconds=30
        )
        async with Orchestrator(config, provider) as loop:
            planning.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=planning)
            for _ in range(cycles):
                progressed = await loop._cycle()
                await asyncio.sleep(0.02)
                mission = loop.store.get_mission(world.mission.id)
                if mission is not None and mission.status in TERMINAL_MISSION:
                    break
                if not progressed and not loop._has_inflight():
                    await loop._record_hierarchical_stall()
                    await loop._confirm_and_stop_stalled()
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            events = list(loop.store.list_events(world.mission.id))
            # The tree itself, rebuilt read-only (the record is keyed once per Mission,
            # so a second build appends nothing).
            live = [
                t for t in loop.store.list_tasks(mission.id) if t.status is not TaskStatus.CANCELLED
            ]
            tree = {
                item.path: item.artifact_id
                for item in loop._hierarchical_judgment_inputs(
                    mission, loop._new_mode(mission), live
                )
            }
            reviewer_seen: dict[str, Any] = {}
            for intent in loop.store.list_intents("SETTLED"):
                if intent.mission_id == mission.id and ":root-review:" in str(intent.subject_id):
                    reviewer_seen = json.loads(intent.config["message"]["content"])
            return {
                "status": mission.status,
                "tree": tree,
                "reviewer_seen": reviewer_seen,
                "stop_reason": mission.stop_reason,
                "report": dict(mission.final_report or {}),
                "types": [item.type for item in events],
                "events": events,
                "roles": dict(provider.by_role),
                "progress": list(loop.progress_log),
            }

    return asyncio.run(case())


def test_two_leaves_writing_report_md_no_longer_fail_a_resolved_mission(tmp_path) -> None:
    """Before the fix: ``GoalResolutionCommitted`` → ``MissionFailed{artifact_conflict}``.
    After: the root resolution stands and the Mission is judged COMPLETED with
    ``file:REPORT.md`` met on the tree built from its contributions."""

    world = _two_leaves_wrote_report(tmp_path, key="p23k-n3-e2e")
    outcome = _run(world, tmp_path)
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report'].get('detail')} "
        f"types={outcome['types']} progress={outcome['progress'][-10:]}"
    )
    assert e2e.GOAL_RESOLUTION_COMMITTED in outcome["types"]
    assert "MissionFailed" not in outcome["types"]
    assert outcome["roles"].get("root_reviewer") == 1
    judged = outcome["report"].get("success_criteria")
    assert judged, outcome["report"]
    assert all(item.get("met") for item in judged), judged


def test_the_hierarchical_tree_is_recorded_and_names_the_superseded_writer(tmp_path) -> None:
    """The mode branch writes down what it did, in the shape D4 established: the legacy
    merge was not applied, the tree came from the resolution's contributions, and the
    path two leaves both wrote is listed with the writer that was kept."""

    world = _two_leaves_wrote_report(tmp_path, key="p23k-n3-event")
    review_task = _review_task(world)
    leaf_task = _leaf_task(world)
    outcome = _run(world, tmp_path)
    assert outcome["status"] is MissionStatus.COMPLETED, outcome["stop_reason"]
    recorded = [item for item in outcome["events"] if item.type == ARTIFACT_MERGE_NOT_APPLICABLE]
    assert len(recorded) == 1, outcome["types"]
    payload = recorded[0].payload
    assert payload["reason"] == "SEMANTICS_IS_HIERARCHICAL"
    assert payload["redirect"] == "root_resolution_contributions"
    assert payload["contributions"] == 2
    superseded = {item["path"]: item for item in payload["superseded"]}
    assert set(superseded) == {REPORT}
    # The finalizer (``review``) carries the root criterion, so its REPORT.md is the
    # one the tree keeps; the facts-style leaf's copy is named as superseded.
    assert superseded[REPORT]["kept_task_id"] == review_task
    assert superseded[REPORT]["superseded_task_ids"] == [leaf_task]
    assert superseded[REPORT]["kept_by"] == "criterion_link"


def test_the_criterion_linked_output_wins_even_when_it_was_accepted_first(tmp_path) -> None:
    """Order of acceptance is the tie-break, not the rule: the step the plan made
    answerable for the root criterion keeps the path whatever the clock says."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _world_with_report_criterion(evidence, key="p23k-n3-order")
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    leaf_task = _leaf_task(world)
    review_task = _review_task(world)
    # The leaf must be accepted first (review consumes its ``result``), so the only
    # way to put the review *earlier* on the clock is to stamp it so.
    _complete_leaf(
        world,
        leaf_task,
        port_path="out/result.json",
        port_data=b'{"result": "alpha"}',
        report=LEAF_REPORT,
        report_version=1,
        now_ms=2_000_000,
    )
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=2_050_000)
    _complete_leaf(
        world,
        review_task,
        port_path="out/verdict.json",
        port_data=b"verdict: PASS",
        report=REVIEW_REPORT,
        report_version=2,
        now_ms=1_500_000,
    )
    outcome = _run(world, tmp_path)
    assert outcome["status"] is MissionStatus.COMPLETED, outcome["stop_reason"]
    recorded = next(
        item for item in outcome["events"] if item.type == ARTIFACT_MERGE_NOT_APPLICABLE
    )
    superseded = {item["path"]: item for item in recorded.payload["superseded"]}
    assert superseded[REPORT]["kept_task_id"] == review_task
    assert superseded[REPORT]["kept_by"] == "criterion_link"


def test_a_path_only_one_leaf_wrote_is_kept_without_a_superseded_entry(tmp_path) -> None:
    world = _two_leaves_wrote_report(tmp_path, key="p23k-n3-single")
    outcome = _run(world, tmp_path)
    recorded = next(
        item for item in outcome["events"] if item.type == ARTIFACT_MERGE_NOT_APPLICABLE
    )
    paths = {item["path"] for item in recorded.payload["superseded"]}
    assert "out/result.json" not in paths and "out/verdict.json" not in paths
    assert recorded.payload["artifacts"] == 3, "two port files and one REPORT.md kept"


# ======================================================================================
# 3. Verification P1-1 (I05): two criterion-linked leaves writing one path — C1-r1
# ======================================================================================

C1_PORT_FILES: dict[str, tuple[str, bytes]] = {
    # step → (port file path, bytes); ``verify.report`` and ``summarize.summary`` are
    # both REPORT.md, exactly as the real C1-r1 leaves wrote them (v5 / v7).
    "code.read-repository-facts": ("facts.json", b'{"tests": ["tests/test_kv.py"]}'),
    "code.reproduce-failure": ("diagnosis.json", b'{"failing": "test_get"}'),
    "code.apply-patch": ("patch.diff", b"--- a/kv.py\n+++ b/kv.py\n"),
    "code.verify-tests": (REPORT, b"# verify\n\ntests/test_kv.py::test_get: 1 passed\n"),
    "code.inspect-changeset": ("findings.json", b'{"changed": ["kv.py"]}'),
    "code.summarize-review": (REPORT, b"# summary\n\nkv.py: get() now returns the value\n"),
}


def _c1_world(tmp_path, *, key: str):
    """The C1-r1 method bound through the @2 ports, six leaves completed for real."""

    from test_inspect_leaf_patch_input import _c1_method, _CodeWorld, _rebound

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _CodeWorld(
        evidence,
        method=_rebound(_c1_method()),
        key=key,
        db_name="orchestrator.db",
        success_criteria=(f"file:{REPORT}",),
    )
    report_version = 0
    for index, (step, (path, data)) in enumerate(C1_PORT_FILES.items()):
        task_id = world.task(step)
        if path == REPORT:
            report_version += 1
        artifact = _stored_file(
            world,
            task_id,
            artifact_id=f"artifact-{step}",
            path=path,
            data=data,
            version=report_version if path == REPORT else 1,
        )
        task = world.store.get_task(task_id)
        assert task is not None
        completed = next_task(
            next_task(next_task(task, TaskStatus.ACTIVE), TaskStatus.VERIFYING),
            TaskStatus.COMPLETED,
            accepted_result_id=f"result-{task_id}",
            accepted_artifacts=(artifact.id,),
        )
        world.store.update_task(completed, expected_version=task.version)
        world.clock += 1_000
        _accept_with(
            world.service,
            world.dispatch,
            world.mission.id,
            task_id,
            artifacts=(artifact,),
            now_ms=1_000_000 + index * 1_000,
        )
        world.dispatch.issue_input_witnesses(
            world.mission.id, world.dispatch.network(world.mission.id), now_ms=world.clock
        )
    return world


def test_c1_shape_keeps_the_report_the_reviewer_judged_c_test_passes_on(tmp_path) -> None:
    """Both ``verify`` and ``summarize`` carry a root criterion and both wrote
    ``REPORT.md``.  The reviewer's ``c-test-passes`` verdict was read off ``verify``'s
    report; that artifact has to be in the delivered tree, whichever one holds the
    canonical path (verification P1-1, AER I05)."""

    world = _c1_world(tmp_path, key="p23k-p11-c1")
    verify = world.task("code.verify-tests")
    summarize = world.task("code.summarize-review")
    outcome = _run(world, tmp_path, planning=world.world, cycles=40)
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report'].get('detail')} "
        f"progress={outcome['progress'][-8:]}"
    )
    shown = outcome["reviewer_seen"]
    assert shown, "the root reviewer was asked"
    covered = {item["criterion_id"]: item["covered_by"] for item in shown["criteria"]}
    by_acceptance = {item["acceptance_id"]: item for item in shown["contributions"]}
    judged_on = {
        name: [
            output["artifact_id"]
            for cover in covers
            for output in by_acceptance[cover["acceptance_id"]]["accepted_outputs"]
            if name in output["covers_root_criteria"]
        ]
        for name, covers in covered.items()
    }
    assert judged_on["c-test-passes"] == ["artifact-code.verify-tests"]
    assert judged_on["c-change-explained"] == ["artifact-code.summarize-review"]
    # Every artifact a root criterion was judged on is in the tree.
    delivered = set(outcome["tree"].values())
    for name, artifacts in judged_on.items():
        assert set(artifacts) <= delivered, (name, artifacts, outcome["tree"])
    # The canonical path holds the later linked writer; the other linked writer's
    # bytes are kept under accepted-outputs/<task>/<port>/.
    assert outcome["tree"][REPORT] == "artifact-code.summarize-review"
    assert outcome["tree"][f"accepted-outputs/{verify}/report/{REPORT}"] == (
        "artifact-code.verify-tests"
    )
    recorded = next(
        item for item in outcome["events"] if item.type == ARTIFACT_MERGE_NOT_APPLICABLE
    )
    entry = {item["path"]: item for item in recorded.payload["superseded"]}[REPORT]
    assert entry["kept_task_id"] == summarize
    assert entry["kept_artifact_id"] == "artifact-code.summarize-review"
    assert entry["kept_by"] == "acceptance_order_between_linked"
    assert entry["superseded_task_ids"] == [verify]
    [loser] = entry["superseded"]
    assert loser["task_id"] == verify
    assert loser["artifact_id"] == "artifact-code.verify-tests"
    assert loser["linked"] is True and loser["port"] == "report"
    assert loser["kept_at"] == f"accepted-outputs/{verify}/report/{REPORT}"
    assert len(loser["content_hash"]) == 64


def test_a_non_linked_loser_is_recorded_with_its_artifact_but_not_kept(tmp_path) -> None:
    """The plan.goal shape again: the facts-style leaf's REPORT.md is superseded by the
    finalizer's.  It is not evidence a criterion was judged on, so it is not kept in
    the tree — but the record now names the artifact and its hash (P2-2)."""

    world = _two_leaves_wrote_report(tmp_path, key="p23k-p11-nonlinked")
    leaf_task = _leaf_task(world)
    outcome = _run(world, tmp_path)
    assert outcome["status"] is MissionStatus.COMPLETED
    recorded = next(
        item for item in outcome["events"] if item.type == ARTIFACT_MERGE_NOT_APPLICABLE
    )
    entry = {item["path"]: item for item in recorded.payload["superseded"]}[REPORT]
    assert entry["kept_by"] == "criterion_link"
    [loser] = entry["superseded"]
    assert loser["task_id"] == leaf_task
    assert loser["artifact_id"] == f"artifact-{leaf_task}-report"
    assert loser["linked"] is False and loser["port"] is None and loser["kept_at"] is None
    assert not any(path.startswith("accepted-outputs/") for path in outcome["tree"])


def test_a_port_output_outranks_a_plain_accepted_artifact_of_an_earlier_leaf(tmp_path) -> None:
    """Weight is (linked, port, order): an output the reviewer read at a declared port
    beats a file merely left in an unlinked leaf's workspace, whatever the clock."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _world_with_report_criterion(evidence, key="p23k-p11-port")
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    leaf_task = _leaf_task(world)
    review_task = _review_task(world)
    # The leaf claims ``out/verdict.json``-shaped bytes at *no* port (a plain file),
    # the review claims its verdict at the ``verdict`` port on the same path.
    _complete_leaf(
        world,
        leaf_task,
        port_path="out/result.json",
        port_data=b'{"result": "alpha"}',
        report=LEAF_REPORT,
        report_version=1,
        now_ms=2_000_000,
    )
    stray = _stored_file(
        world, leaf_task, artifact_id="artifact-leaf-stray", path="out/verdict.json", data=b"draft"
    )
    task = world.store.get_task(leaf_task)
    assert task is not None
    world.store.update_task(
        next_task(task, accepted_artifacts=(*task.accepted_artifacts, stray.id)),
        expected_version=task.version,
    )
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=2_050_000)
    _complete_leaf(
        world,
        review_task,
        port_path="out/verdict.json",
        port_data=b"verdict: PASS",
        report=REVIEW_REPORT,
        report_version=2,
        now_ms=1_500_000,
        port_version=2,  # the stray holds version 1 of the same path
    )
    outcome = _run(world, tmp_path)
    assert outcome["status"] is MissionStatus.COMPLETED, outcome["stop_reason"]
    assert outcome["tree"]["out/verdict.json"] == f"artifact-{review_task}-port"


# ======================================================================================
# 4. Verification P2-1 (mutant M7): only CURRENT contributions build the tree
# ======================================================================================


def test_a_revoked_acceptance_contributes_nothing_to_the_tree(tmp_path) -> None:
    """``root_contributions`` reads validity; an Acceptance nobody holds any more is
    history, and its files are neither delivered nor listed as superseded."""

    import asyncio

    world = _two_leaves_wrote_report(tmp_path, key="p23k-p21-revoked")
    leaf_task = _leaf_task(world)
    review_task = _review_task(world)
    revoked = [
        item
        for item in world.semantics.list_acceptances(world.mission.id)
        if str(item.task_id) == leaf_task
    ]
    assert len(revoked) == 1
    world.store.connection.execute(
        "UPDATE acceptances SET validity = 'REVOKED',"
        " acceptance_json = json_set(acceptance_json, '$.validity', 'REVOKED')"
        " WHERE acceptance_id = ?",
        (str(revoked[0].acceptance_id),),
    )
    world.store.connection.commit()
    evidence = Path(tmp_path) / "evidence"
    world.store.close()

    async def case() -> dict[str, Any]:
        config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1)
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            tasks = loop.store.list_tasks(mission.id)
            tree = loop._hierarchical_judgment_inputs(mission, loop._new_mode(mission), tasks)
            recorded = [
                item.payload
                for item in loop.store.list_events(mission.id)
                if item.type == ARTIFACT_MERGE_NOT_APPLICABLE
            ]
            return {"tree": {item.path: item.task_id for item in tree}, "recorded": recorded}

    outcome = asyncio.run(case())
    assert set(outcome["tree"]) == {"out/verdict.json", REPORT}
    assert set(outcome["tree"].values()) == {review_task}
    [payload] = outcome["recorded"]
    assert payload["contributions"] == 1
    assert payload["superseded"] == []
