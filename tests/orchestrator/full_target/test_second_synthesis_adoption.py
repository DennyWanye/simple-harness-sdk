# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3n: a round-2 admitted method must be shown as applicable and then adopted.

Grok third-batch H-L3-C1-r0 / r1 (SDK 0.12.2 candidate 2845b7e):

    round 1 TRIAL_ADMITTED → revision 1 → leaves
    → r0: read-only rewrite bound → PlanningRejected{read_only_leaf_needs_write}
      r1: root review REJECTED → PlanningRejected{root_review_rejected}
    → repair Planner declares no_applicable_method
    → synthesis round 2 TRIAL_ADMITTED (r0: code.fix-by-facts-apply-then-explain;
      r1: code.fix-by-repro-patch-prove-landed-verify-explain)
    → planner ordinal 5: the new method *is* in method_library
      (rejected_by_root_review=false) but **not** in applicability
      (only the three seed methods, all NEEDS_EVIDENCE)
    → MethodApplicabilityAssessed exists only for plan_revision 0/1 against those
      three seeds — never against the round-2 method
    → Planner: "every remaining library method is not grounded / NEEDS_EVIDENCE"
      → no_applicable_method → HierarchicalMissionStalled → FAILED

The round-2 method has empty ``applicable_when``, so ``assess_method`` returns
APPLICABLE.  ``method_applicability`` used to skip applicable reports, and
``MethodApplicabilityAssessed`` is keyed ``(mission, plan_revision)`` so a second
assessment at the same revision (the library just grew) is a no-op.  The v5 prompt
tells the Planner that "都被 applicability 拒绝" means no_applicable_method; Grok
looked only at the three seed refusals and never treated the silent library entry
as grounded.

A Planner that only adopts a method with an explicit APPLICABLE applicability row
is the oracle: before the fix it declines after round 2; after the fix it retires
the rejected instance, refines with the new method, and the Mission COMPLETED.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_read_only_rewrite_bound import (  # noqa: E402
    SEED,
    TOOLS,
    _CodeWorld,
    _four_step,
    _LeafWorker,
)
from test_root_review_repair_library import (  # noqa: E402
    C1_FINDING,
    FREE_TEXT_CRITERION,
    _accept_open_leaves,
    _drive,
    _judge_critic,
    _planner_declines,
    _rejected,
    _reviewer,
    _seeded,
    _synthesised_method,
)
from test_root_review_repair_library import (
    FIXTURE as C1_FIXTURE,
)

from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import TERMINAL_MISSION  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import (  # noqa: E402
    READ_ONLY_REWRITE_REPAIR_REASON,
    ROOT_REVIEW_REPAIR_REASON,
    Orchestrator,
)
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    METHOD_APPLICABILITY_ASSESSED,
    SYNTHESIS_ROUND_RECORDED,
)
from agent_orchestrator.planning.htn.applicability import ApplicabilityStatus  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    method_proposal_step,
    package_of,
    plan_revision_proposal_step,
)

ROUND2_FIXTURE = C1_FIXTURE / "package_ord5_after_round2.json"
ROUND2_METHOD = "code.fix-by-facts-apply-then-explain"
ROUND1_METHOD = "code.fix-by-diagnose-patch-verify-explain"
SEED_METHODS = (
    "code.fix-by-assessed-revert",
    "code.fix-by-patch",
    "code.fix-by-revert",
)


def _ord5_after_round2() -> dict[str, Any]:
    return json.loads(ROUND2_FIXTURE.read_text(encoding="utf-8"))


def _planner_adopts_applicable(request: Any) -> str:
    """Grok ordinal 5: a method is grounded only when applicability says APPLICABLE.

    Round-1 adoption (no rejected_refinements) still copies a library triple; after a
    rejection the Planner looks at applicability the way the real model did, and
    ignores a silent library entry.
    """

    package = package_of(request)
    rejected = package.get("rejected_refinements") or []
    if not rejected:
        return _planner_declines(request)
    applicable = {
        str((item.get("method_ref") or {}).get("method_id") or item.get("method_id") or "")
        for item in package.get("applicability") or []
        if str(item.get("verdict") or "") == str(ApplicabilityStatus.APPLICABLE)
    }
    entry = rejected[0]
    library = [
        item
        for item in package["method_library"]
        if item["goal_signature_id"] == entry["goal_signature_id"]
        and not item["rejected_by_root_review"]
        and not item.get("rejected_by_read_only_leaf")
        and str(item.get("method_id") or "") in applicable
    ]
    if not library:
        return _planner_declines(request)
    chosen = library[0]["refine_method_ref"]
    return plan_revision_proposal_step(
        proposal_id=f"p-adopt-{package['plan']['plan_revision']}-{package['planning_attempt']}",
        expected_plan_revision=int(package["plan"]["plan_revision"]),
        read_set=[
            {
                "kind": "method",
                "id": chosen["id"],
                "semantic_revision": chosen["version"],
                "content_hash": chosen["content_hash"],
            }
        ],
        operations=[
            {
                "op": "retire_method",
                "method_instance_id": entry["rejected_method_instance_id"],
                "reason": (entry.get("findings") or [{"detail": "rejected"}])[0]["detail"][:120],
            },
            {
                "op": "refine",
                "goal_id": entry["goal_id"],
                "obligation_id": entry["obligation_id"],
                "method_ref": dict(chosen),
                "bindings": {},
            },
        ],
        rationale="adopting the round-2 method applicability lists as APPLICABLE",
    )


# ======================================================================================
# 1. The defect, pinned by the real ordinal-5 package
# ======================================================================================


def test_the_c1_r0_ordinal_5_package_omitted_the_round2_method_from_applicability() -> None:
    """What planner:5 actually carried after round-2 admission: the new method is in
    the library and not flagged rejected, but applicability never mentions it."""

    package = _ord5_after_round2()
    library = {item["method_id"]: item for item in package["method_library"]}
    assert ROUND2_METHOD in library, "the synthesised method did reach the library"
    assert library[ROUND2_METHOD]["rejected_by_root_review"] is False
    assert library[ROUND1_METHOD]["rejected_by_root_review"] is True
    applicability_ids = [item["method_id"] for item in package["applicability"]]
    assert applicability_ids == list(SEED_METHODS) or set(applicability_ids) == set(SEED_METHODS)
    assert ROUND2_METHOD not in applicability_ids, (
        "the defect: round-2 method is absent from applicability, so a Planner that "
        "grounds methods from that section never sees it"
    )
    assert all(item["verdict"] == "NEEDS_EVIDENCE" for item in package["applicability"])
    rejected_ids = [item["method_id"] for item in package["rejected_refinements"]]
    assert rejected_ids == [ROUND1_METHOD]
    assert ROUND2_METHOD not in rejected_ids
    assert package["plan"]["plan_revision"] == 1
    assert package["plan"]["open_compound_goals"] == []


# ======================================================================================
# 2. Live package + assessment after round 2
# ======================================================================================


def _round2_outcome(tmp_path, *, key: str, complete: bool) -> dict[str, Any]:
    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _seeded(evidence, key=key, alt=False, free_text=True)
    world.store.close()
    invented = _synthesised_method()
    planner = _planner_adopts_applicable if complete else _planner_declines
    provider = RoleScriptedProvider(
        {
            "root_reviewer": [_reviewer("FAIL", finding=C1_FINDING["detail"]), _reviewer("PASS")],
            "planner": [planner] * 6,
            "method_synthesizer": [method_proposal_step(invented.to_json())] * 2,
            "critic": [_judge_critic],
        }
    )
    return _drive(world, evidence, provider, cycles=80, max_planning_attempts=1)


def test_a_round2_admitted_method_appears_as_applicable_in_the_next_planner_package(
    tmp_path,
) -> None:
    """After TRIAL_ADMITTED on round 2 the next Planner package must list the new
    method in applicability as APPLICABLE, not just in the library, and must not
    inherit the rejected method's flag."""

    outcome = _round2_outcome(tmp_path, key="p23n-package", complete=True)
    packages = outcome["planner_packages"]
    assert len(packages) >= 1, [item.get("planning_attempt") for item in packages]
    after = packages[-1]
    library = {item["method_id"]: item for item in after["method_library"]}
    assert "plan.synthesised" in library
    assert library["plan.synthesised"]["rejected_by_root_review"] is False
    assert library["plan.outer"]["rejected_by_root_review"] is True
    applicable = [
        item
        for item in after["applicability"]
        if (item.get("method_ref") or {}).get("method_id") == "plan.synthesised"
        or item.get("method_id") == "plan.synthesised"
    ]
    assert applicable, (
        f"round-2 method missing from applicability: {after['applicability']!r}"
    )
    assert applicable[0]["verdict"] == str(ApplicabilityStatus.APPLICABLE)
    assessed = [
        item.payload
        for item in outcome["events"]
        if item.type == METHOD_APPLICABILITY_ASSESSED
    ]
    named = []
    for payload in assessed:
        for group in ("refused_methods", "applicable_methods"):
            for entry in payload.get(group) or ():
                named.append((entry.get("method_ref") or {}).get("method_id"))
    assert "plan.synthesised" in named, [item.get("plan_revision") for item in assessed]
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['types']}"
    )


def test_the_applicability_event_after_round2_is_a_new_record(tmp_path) -> None:
    """The (mission, plan_revision) key used to swallow the post-synthesis assessment."""

    outcome = _round2_outcome(tmp_path, key="p23n-assessed", complete=True)
    assessed = [
        item
        for item in outcome["events"]
        if item.type == METHOD_APPLICABILITY_ASSESSED
    ]
    keys = {item.idempotency_key for item in assessed}
    assert any("synth:" in key or ":round:" in key or "synth" in key for key in keys) or len(
        assessed
    ) >= 3, keys
    last = assessed[-1].payload
    named = [
        (entry.get("method_ref") or {}).get("method_id")
        for group in ("refused_methods", "applicable_methods")
        for entry in last.get(group) or ()
    ]
    assert "plan.synthesised" in named, last


# ======================================================================================
# 3. End to end: both rejection triggers → round 2 → adopt → COMPLETED
# ======================================================================================


def test_root_review_rejection_then_round2_method_is_adopted_and_completes(tmp_path) -> None:
    """Trigger 1: root_review_rejected → round-2 synthesis → Planner adopts → COMPLETED."""

    outcome = _round2_outcome(tmp_path, key="p23n-e2e-review", complete=True)
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['types']} "
        f"roles={outcome['roles']} progress={outcome['progress'][-12:]}"
    )
    admitted_rounds = [
        item["synthesis_round"] for item in outcome["synthesis"] if item.get("admitted")
    ]
    assert admitted_rounds[-1] == 2
    assert outcome["revisions"] == [1, 2]
    assert list(outcome["instances"].values()) == ["plan.synthesised"]
    repairs = [
        item
        for item in outcome["events"]
        if item.type == "PlanningRejected"
        and item.payload.get("reason") == ROOT_REVIEW_REPAIR_REASON
    ]
    assert len(repairs) == 1, "max_root_review_repairs stays 1"
    assert outcome["types"].count(SYNTHESIS_ROUND_RECORDED) == 1 or [
        item["synthesis_round"] for item in outcome["synthesis"]
    ][-1] == 2


def test_read_only_leaf_rejection_then_round2_method_is_adopted_and_completes(tmp_path) -> None:
    """Trigger 2: read_only_leaf_needs_write → round-2 synthesis → Planner adopts → COMPLETED.

    The first method's verify leaf is a real Worker (that is how the rewrite bound
    fires).  Once the Planner has committed the replacement revision, new leaves are
    accepted through the same assembly P2.3j uses — the C1-shape write step's
    ``rule_check`` is not this slice's subject.
    """

    from test_read_only_rewrite_bound import _accepting_reviewer, _conservation

    from agent_orchestrator.testing.fixtures import critic_step

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _CodeWorld(
        evidence,
        method=_four_step("code.fix-by-patch-then-verify"),
        key="p23n-e2e-readonly",
        db_name="orchestrator.db",
        allowed_tools=TOOLS,
        workspace_seed=SEED,
        success_criteria=(FREE_TEXT_CRITERION,),
        max_attempts=20,
    )
    worker = _LeafWorker(mode="new", rewrite_limit=2)
    invented = _four_step("code.fix-by-patch-then-verify.repair", suffix="-v2")
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 40,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 12
            + [_judge_critic] * 2,
            "planner": [_planner_adopts_applicable] * 8,
            "method_synthesizer": [method_proposal_step(invented)] * 2,
            "root_reviewer": [_accepting_reviewer] * 3,
        }
    )
    world.store.close()

    async def case() -> dict[str, Any]:
        config = OrchestratorConfig(
            evidence_root=evidence,
            max_concurrency=1,
            test_timeout_seconds=30,
            max_planning_attempts=1,
        )
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.world.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.world)
            for _ in range(400):
                events = list(loop.store.list_events(world.mission.id))
                revisions = [
                    int(item.payload["plan_revision"])
                    for item in events
                    if item.type == "PlanRevisionCommitted"
                ]
                if 2 in revisions:
                    _accept_open_leaves(loop, world.mission.id)
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
            return {
                "status": mission.status,
                "stop_reason": mission.stop_reason,
                "report": dict(mission.final_report or {}),
                "types": [item.type for item in events],
                "events": events,
                "roles": dict(provider.by_role),
                "progress": list(loop.progress_log),
                "conservation": _conservation(loop, mission.id),
            }

    outcome = asyncio.run(case())
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report']} "
        f"types={outcome['types']} roles={outcome['roles']} "
        f"progress={outcome['progress'][-16:]}"
    )
    repairs = [
        item
        for item in outcome["events"]
        if item.type == "PlanningRejected"
        and item.payload.get("reason") == READ_ONLY_REWRITE_REPAIR_REASON
    ]
    assert repairs, outcome["types"]
    synth = [
        item.payload
        for item in outcome["events"]
        if item.type == SYNTHESIS_ROUND_RECORDED
    ]
    assert synth and synth[-1].get("admitted") is True
    assert synth[-1].get("synthesis_round") == 2
    revisions = [
        int(item.payload["plan_revision"])
        for item in outcome["events"]
        if item.type == "PlanRevisionCommitted"
    ]
    assert 2 in revisions, revisions


def test_repair_and_synthesis_bounds_are_not_relaxed(tmp_path) -> None:
    """A second root-review rejection still spends the one repair; no extra synthesis."""

    world = _rejected(tmp_path, key="p23n-bounds", alt=False)
    evidence = Path(tmp_path) / "evidence"
    provider = RoleScriptedProvider(
        {
            "root_reviewer": [_reviewer("FAIL", finding=C1_FINDING["detail"])] * 4,
            "planner": [_planner_declines] * 8,
            "method_synthesizer": ["not a method"] * 4,
        }
    )
    outcome = _drive(
        world, evidence, provider, cycles=60, accept_leaves=False, max_planning_attempts=1
    )
    assert outcome["status"] is MissionStatus.FAILED
    repairs = [
        item
        for item in outcome["events"]
        if item.type == "PlanningRejected"
        and item.payload.get("reason") == ROOT_REVIEW_REPAIR_REASON
    ]
    assert len(repairs) == 1
    admitted = [item for item in outcome["synthesis"] if item.get("admitted")]
    assert admitted == []
