# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3j: the repair round after a root review REJECT has something to repair *with*.

The Grok acceptance episode H-L3-C1-r1 (and H-L3-C2-r0, identical in shape) ended:

    MethodSynthesisRoundRecorded{TRIAL_ADMITTED}      ← code.fix-by-reproduce-patch-verify-explain
    → PlanRevisionCommitted{revision 1}, six leaves COMPLETED, six ports delivered
    → HierarchicalRootReviewRejected{c-change-explained: FAIL, blocker}   ← the reviewer was right
    → PlanningRejected{ordinal 4, root_review_rejected}                    ← D5-A opened the repair
    → planner ordinal 5: ``method_library []`` / ``applicability []`` / ``open_compound_goals []``
    → PlanningRejected{ordinal 5, no_applicable_method}
    → HierarchicalMissionStalled{hierarchical_no_dispatchable_work} → MissionFailed

The repair round's package listed methods and applicability only for compound goals
*nobody had refined yet*, and the root was refined — by the very instance the review
had just rejected.  So the Planner was told what the reviewer said and given nothing
to say back: no library, no applicability, no way to name the rejected instance, and
no route by which the findings reached a new synthesis round.

Three things change here, none of them in ``contracts/``:

(a) the package carries a ``rejected_refinements`` section for the occurrence whose
    adopted instance the root review rejected, and ``method_library`` /
    ``applicability`` are computed for that occurrence too, with the rejected method
    flagged;
(b) a proposal may carry ``retire_method`` (the rejected instance) together with the
    ``refine`` of the same goal — the replacement §9.1 calls "选择替代方法" — compiled
    through the compiler's existing ``retire_instance_ids`` and the commit's existing
    retirement checks;
(c) when the Planner, shown that package, still declares ``no_applicable_method``,
    the system's own applicability judgment (with the rejected method excluded) may
    open a *second* synthesis round for that goal, once per rejected plan revision,
    with the findings handed to the synthesiser as ``review_feedback``.

The fixture under ``fixtures/htn/c1_repair_round/`` is the real ordinal-5 package and
the real findings; the first test pins the defect shape so the repair is measured
against what actually happened.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_htn_end_to_end as e2e  # noqa: E402
from htn_world import method, out, param, step  # noqa: E402
from test_htn_end_to_end import (  # noqa: E402
    ROOT_DUTY,
    ROOT_TASK,
    World,
    _accept_every_child,
    _Artifact,
    _passing_layers,
    committed,
)
from test_root_review_coordinator import coordinator, review  # noqa: E402

from agent_orchestrator.contracts.htn import TaskForm  # noqa: E402
from agent_orchestrator.contracts.models import ContractError, MissionStatus  # noqa: E402
from agent_orchestrator.contracts.resolution import (  # noqa: E402
    CriterionVerdict,
    ReviewVerdict,
)
from agent_orchestrator.contracts.state_machines import TERMINAL_MISSION  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import (  # noqa: E402
    ROOT_REVIEW_REPAIR_REASON,
    Orchestrator,
)
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    SYNTHESIS_ROUND_RECORDED,
)
from agent_orchestrator.orchestrator.leaf_acceptance import LeafAcceptanceAssembly  # noqa: E402
from agent_orchestrator.planning.htn.planner_package import (  # noqa: E402
    HIERARCHICAL_PACKAGE_VERSION,
)
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.runtime.output_blocks import PortClaim  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    method_proposal_step,
    package_of,
    plan_revision_proposal_step,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "htn" / "c1_repair_round"
NOW_MS = 2_000_000
ROOT_CRITERION = "c-root"
FREE_TEXT_CRITERION = "根目标经根评审通过并形成 GoalResolution"


def _c1() -> dict[str, Any]:
    return {
        "ord4": json.loads((FIXTURE / "package_ord4.json").read_text(encoding="utf-8")),
        "ord5": json.loads((FIXTURE / "package_ord5.json").read_text(encoding="utf-8")),
        "rejected": json.loads((FIXTURE / "root_review_rejected.json").read_text(encoding="utf-8")),
        "planning": json.loads((FIXTURE / "planning_rejected.json").read_text(encoding="utf-8")),
    }


C1_FINDING = _c1()["rejected"]["findings"][0]
BLOCKER = ({"severity": "blocker", "criterion_id": ROOT_CRITERION, "detail": C1_FINDING["detail"]},)


# ======================================================================================
# The worlds
# ======================================================================================


def _alt_method(method_id: str = "plan.alt"):
    """A second registered method for ``plan.goal``: the replacement the Planner may pick."""

    return method(
        method_id,
        "plan.goal",
        parameter_schema="plan.goal.params",
        steps=(
            step(
                "leaf2",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
            step(
                "review2",
                "plan.review",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "result": out("leaf2", "result")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "review2", "c-reviewed"),),
        finalizer="review2",
    )


def _register(world: World, contract: Any) -> None:
    receipt = world.env.admit(contract)
    assert receipt.admitted, receipt.problems
    HtnStore(world.store).register_method(
        contract, world.env.registry.registration(contract.method_ref())
    )


def _seeded(tmp_path, *, key: str, alt: bool, free_text: bool = False) -> World:
    """A committed plan whose gating children are accepted; the store is left open."""

    original = e2e._spec
    if free_text:
        # The Mission Judge checks ``file:`` criteria on the integrated artifact tree,
        # which leaves accepted through the assembly do not populate; a free-text
        # criterion goes to the (scripted) independent Critic instead.
        import dataclasses

        def spec(*args: Any, **kwargs: Any):
            return dataclasses.replace(
                original(*args, **kwargs), success_criteria=(FREE_TEXT_CRITERION,)
            )

        e2e._spec = spec
    try:
        world = committed(tmp_path, key=key, demand=True)
    finally:
        e2e._spec = original
    if alt:
        _register(world, _alt_method())
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    _accept_every_child(world)
    return world


def _rejected(tmp_path, *, key: str, alt: bool, findings=BLOCKER) -> World:
    """A Mission whose root review has concluded REJECT, on disk and closed.

    Built under ``<tmp>/evidence`` — the directory the Orchestrator is then opened
    over — so the loop reads the same library file the fixture wrote.
    """

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _seeded(evidence, key=key, alt=alt)
    coordinator(world).cut(world.mission.id, now_ms=NOW_MS)
    review(
        world,
        verdict=ReviewVerdict.REJECTED,
        verdicts={ROOT_CRITERION: CriterionVerdict.FAIL},
        findings=findings,
    )
    world.store.close()
    return world


def _config(evidence: Path, **overrides: Any) -> OrchestratorConfig:
    base: dict[str, Any] = dict(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=30,
        max_root_review_repairs=1,
        max_planning_attempts=4,
    )
    base.update(overrides)
    return OrchestratorConfig(**base)


def _sections(message: Any) -> dict[str, Any]:
    """The JSON sections of one sealed Planner package."""

    import re

    text = message["content"] if isinstance(message, dict) else str(message)
    found: dict[str, Any] = {}
    for header, body in re.findall(r"## ([a-z_]+)\n(.*?)(?=\n## |\Z)", text, re.DOTALL):
        try:
            found[header] = json.loads(body.strip())
        except json.JSONDecodeError:
            found[header] = body.strip()
    return found


def _planner_packages(loop: Orchestrator, mission_id: str) -> list[dict[str, Any]]:
    return [
        _sections(item.config.get("message", ""))
        for item in loop.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED")
        if item.mission_id == mission_id
        and item.kind == "plan"
        and str(item.config.get("role", "")) not in {"method_synthesizer", "root_reviewer"}
    ]


def _open_repair_round(world: World, evidence: Path) -> dict[str, Any]:
    """Run ``_advance_root_review`` once on a real Orchestrator and return the package."""

    async def case() -> dict[str, Any]:
        async with Orchestrator(_config(evidence), RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            moved = await loop._advance_root_review(mission, loop._new_mode(mission))
            packages = _planner_packages(loop, world.mission.id)
            adopted = loop.hierarchical.network(world.mission.id).adopted_instance_for(
                loop.hierarchical.network(world.mission.id).root_occurrence_ids[0]
            )
            return {
                "moved": moved,
                "packages": packages,
                "adopted_instance": None if adopted is None else str(adopted.instance_id),
                "events": [
                    dict(item.payload)
                    for item in loop.store.list_events(world.mission.id)
                    if item.type == "PlanningRejected"
                    and item.payload.get("reason") == ROOT_REVIEW_REPAIR_REASON
                ],
            }

    return asyncio.run(case())


# ======================================================================================
# 1. The defect, pinned by the real package
# ======================================================================================


def test_the_c1_repair_round_package_was_empty_before_the_fix() -> None:
    """What ordinal 5 was actually handed: nothing to choose from, nothing to name."""

    c1 = _c1()
    before = c1["ord4"]
    after = c1["ord5"]
    assert before["plan"]["plan_revision"] == 0 and after["plan"]["plan_revision"] == 1
    assert len(before["method_library"]) == 4, "three seed methods and one synthesised"
    assert len(before["applicability"]) == 3
    assert after["method_library"] == []
    assert after["applicability"] == []
    assert after["plan"]["open_compound_goals"] == []
    assert len(after["plan"]["committed_primitives"]) == 6
    assert after["package_version"] == "planner-package-hierarchical-v3"
    # The record that opened the round says why, and the answer says what it could see.
    repair = next(item for item in c1["planning"] if item["reason"] == ROOT_REVIEW_REPAIR_REASON)
    assert repair["ordinal"] == 5 and repair["detail"]["plan_revision"] == 1
    assert repair["detail"]["findings"][0]["severity"] == "blocker"
    answer = next(
        item
        for item in c1["planning"]
        if item["ordinal"] == 5 and item["reason"] == "no_applicable_method"
    )
    assert "method_library is empty and applicability is empty" in answer["detail"]["rationale"]
    assert c1["rejected"]["criteria"] == {"c-change-explained": "FAIL", "c-test-passes": "PASS"}


# ======================================================================================
# 2. (a) the repaired package
# ======================================================================================


def test_the_repair_round_package_now_offers_the_root_library_and_marks_the_rejected_method(
    tmp_path,
) -> None:
    """The same shape as ordinal 5, after the fix.

    **Mutation**: build the package without ``rejected=`` and the section is gone and
    ``method_library`` is empty again — the C1 shape.
    """

    world = _rejected(tmp_path, key="p23j-package", alt=True)
    outcome = _open_repair_round(world, Path(tmp_path) / "evidence")
    assert outcome["moved"] is True
    assert len(outcome["packages"]) == 1
    package = outcome["packages"][0]
    assert package["package_version"] == HIERARCHICAL_PACKAGE_VERSION
    assert package["plan"]["open_compound_goals"] == [], "the root is still refined"
    rejected = package["rejected_refinements"]
    assert len(rejected) == 1
    entry = rejected[0]
    assert entry["occurrence_id"] == ROOT_TASK
    assert entry["goal_id"] == ROOT_TASK and entry["obligation_id"] == ROOT_DUTY
    assert entry["goal_signature_id"] == "plan.goal"
    assert entry["rejected_method_instance_id"] == outcome["adopted_instance"]
    assert entry["rejected_method_ref"]["method_id"] == "plan.outer"
    assert entry["plan_revision"] == 1
    assert entry["review_package_id"]
    assert [item["severity"] for item in entry["findings"]] == ["blocker"]
    assert "does not explain an applied changeset" in entry["findings"][0]["detail"]
    library = {item["method_id"]: item for item in package["method_library"]}
    assert set(library) == {"plan.outer", "plan.alt"}, "both methods for the root's signature"
    assert library["plan.outer"]["rejected_by_root_review"] is True
    assert library["plan.alt"]["rejected_by_root_review"] is False
    assert library["plan.alt"]["refine_method_ref"]["id"] == "plan.alt"
    # Both methods apply here, so the refusal section stays empty — the rejected one
    # is *not* smuggled in as a refusal: its reason is the review, stated above.
    assert package["applicability"] == []
    # The repair record itself now names the instance, which is how the package (and
    # the synthesis judgment) find it after a restart.
    assert outcome["events"][0]["detail"]["method_instance_id"] == outcome["adopted_instance"]
    assert outcome["events"][0]["detail"]["method_ref"]["method_id"] == "plan.outer"


def test_a_repair_round_with_only_the_rejected_method_still_lists_it_as_rejected(
    tmp_path,
) -> None:
    """One method, and it is the rejected one: the Planner is shown *why* it cannot reuse it."""

    world = _rejected(tmp_path, key="p23j-package-lonely", alt=False)
    outcome = _open_repair_round(world, Path(tmp_path) / "evidence")
    package = outcome["packages"][0]
    assert [item["method_id"] for item in package["method_library"]] == ["plan.outer"]
    assert package["method_library"][0]["rejected_by_root_review"] is True
    assert package["rejected_refinements"][0]["rejected_method_ref"]["method_id"] == "plan.outer"


def test_a_package_for_a_plan_nobody_rejected_has_no_rejected_refinements(tmp_path) -> None:
    """The section is empty — not absent — on an ordinary round, so the shape is stable."""

    world = committed(tmp_path, key="p23j-package-clean")
    package = e2e.hierarchical_planner_package(
        world.mission, world.network(), registry=world.env.registry
    )
    assert package["rejected_refinements"] == []
    assert all("rejected_by_root_review" in item for item in package["method_library"])


# ======================================================================================
# 3. (b) retire + refine as one replacement
# ======================================================================================


def _replacement(
    world: World,
    contract: Any,
    *,
    instance_id: str,
    revision: int,
    proposal_id: str = "p-replace",
) -> str:
    reference = contract.method_ref()
    return plan_revision_proposal_step(
        proposal_id=proposal_id,
        expected_plan_revision=revision,
        read_set=[
            {
                "kind": "method",
                "id": reference.method_id,
                "semantic_revision": reference.version,
                "content_hash": reference.content_hash,
            }
        ],
        operations=[
            {
                "op": "retire_method",
                "method_instance_id": instance_id,
                "reason": "the root review rejected this instance's result",
            },
            {
                "op": "refine",
                "goal_id": ROOT_TASK,
                "obligation_id": ROOT_DUTY,
                "method_ref": {
                    "id": reference.method_id,
                    "version": reference.version,
                    "content_hash": reference.content_hash,
                },
                "bindings": {},
            },
        ],
        rationale="replace the rejected method with the alternative",
    )


def _adopted_root(world: World) -> str:
    network = world.network()
    draft = network.adopted_instance_for(network.root_occurrence_ids[0])
    assert draft is not None
    return str(draft.instance_id)


def test_retire_and_refine_replaces_the_root_method_in_one_revision(tmp_path) -> None:
    """§9.1 "选择替代方法", through the compiler's ``retire_instance_ids`` and the commit."""

    world = _seeded(tmp_path, key="p23j-replace", alt=True)
    alt = _alt_method()
    old = _adopted_root(world)
    outcome = world.plan(
        _replacement(world, alt, instance_id=old, revision=1), command_id="cmd-replace"
    )
    assert outcome.committed, outcome.last_reason
    network = world.network()
    assert int(network.plan_revision) == 2
    new = network.adopted_instance_for(network.root_occurrence_ids[0])
    assert new is not None and str(new.instance_id) != old
    assert str(new.method_ref.method_id) == "plan.alt"
    assert old not in {str(item) for item in network.adopted_instance_ids}
    states = {
        str(item.instance_id): item
        for item in HtnStore(world.store).list_method_instances(world.mission.id, state="RETIRED")
    }
    assert old in states, "the rejected instance is RETIRED, not deleted"
    # The old branch's occurrences left the plan; the new branch's are on the board.
    kinds = sorted(
        network.binding_for_occurrence(spec.occurrence_id).goal_signature.signature_id
        for spec in network.occurrences
        if spec.form is TaskForm.PRIMITIVE
    )
    assert kinds == ["plan.leaf", "plan.review"]
    committed_event = world.events(e2e.PLAN_REVISION_COMMITTED)[-1].payload
    assert committed_event["retired_method_instances"] == [old]
    assert committed_event["budget_conservation"]["holds"] is True
    new_tasks = {
        str(spec.task_id)
        for spec in network.occurrences
        if spec.form is TaskForm.PRIMITIVE
    }
    assert new_tasks <= set(world.tasks()), "the replacement's leaves are materialised"


def test_a_retirement_must_name_the_instance_adopted_at_the_refined_occurrence(tmp_path) -> None:
    world = _seeded(tmp_path, key="p23j-replace-stranger", alt=True)
    with pytest.raises(ContractError, match="not the adopted method instance"):
        world.plan(
            _replacement(world, _alt_method(), instance_id="mi-stranger", revision=1),
            command_id="cmd-stranger",
        )


def test_a_bare_retirement_is_refused(tmp_path) -> None:
    """Retiring without refining would leave the duty with nobody working on it."""

    world = _seeded(tmp_path, key="p23j-replace-bare", alt=True)
    text = plan_revision_proposal_step(
        proposal_id="p-bare",
        expected_plan_revision=1,
        read_set=[
            {
                "kind": "task",
                "id": ROOT_TASK,
                "semantic_revision": 1,
                "content_hash": "0" * 64,
            }
        ],
        operations=[
            {
                "op": "retire_method",
                "method_instance_id": _adopted_root(world),
                "reason": "give up",
            }
        ],
    )
    with pytest.raises(ContractError, match="exactly one refine"):
        world.plan(text, command_id="cmd-bare")


def test_refining_a_refined_goal_without_retiring_is_still_refused(tmp_path) -> None:
    """The control: the replacement is explicit, never implied by a second refine."""

    world = _seeded(tmp_path, key="p23j-replace-implicit", alt=True)
    alt = _alt_method()
    reference = alt.method_ref()
    text = plan_revision_proposal_step(
        proposal_id="p-implicit",
        expected_plan_revision=1,
        read_set=[
            {
                "kind": "method",
                "id": reference.method_id,
                "semantic_revision": reference.version,
                "content_hash": reference.content_hash,
            }
        ],
        operations=[
            {
                "op": "refine",
                "goal_id": ROOT_TASK,
                "obligation_id": ROOT_DUTY,
                "method_ref": {
                    "id": reference.method_id,
                    "version": reference.version,
                    "content_hash": reference.content_hash,
                },
                "bindings": {},
            }
        ],
    )
    with pytest.raises(ContractError, match="two adopted method instances"):
        world.plan(text, command_id="cmd-implicit")
    assert int(world.network().plan_revision) == 1


# ======================================================================================
# 4. End to end on a real Orchestrator, cycle by cycle
# ======================================================================================


def _reviewer(verdict: str, *, finding: str = "") -> str:
    findings = [] if verdict == "PASS" else [{"severity": "blocker", "detail": finding}]
    return (
        "<critic_verdict>"
        + json.dumps(
            {
                "verdict": verdict,
                "findings": findings,
                "mission_criteria": [
                    {
                        "criterion": ROOT_CRITERION,
                        "met": verdict == "PASS",
                        "reason": "scripted",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "</critic_verdict>"
    )


def _judge_critic(request: Any) -> str:
    return (
        "<critic_verdict>"
        + json.dumps(
            {
                "verdict": "PASS",
                "findings": [],
                "mission_criteria": [
                    {"criterion": FREE_TEXT_CRITERION, "met": True, "reason": "scripted"}
                ],
            },
            ensure_ascii=False,
        )
        + "</critic_verdict>"
    )


def _planner_replaces(request: Any) -> str:
    """A Planner that reads the package: retire the rejected instance, refine with another."""

    package = package_of(request)
    rejected = package["rejected_refinements"]
    assert rejected, "the repair package must name what was rejected"
    entry = rejected[0]
    library = [
        item
        for item in package["method_library"]
        if item["goal_signature_id"] == entry["goal_signature_id"]
        and not item["rejected_by_root_review"]
    ]
    assert library, "the repair package must offer a replacement"
    chosen = library[0]["refine_method_ref"]
    return plan_revision_proposal_step(
        proposal_id=f"p-replace-{package['plan']['plan_revision']}",
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
                "reason": entry["findings"][0]["detail"][:120],
            },
            {
                "op": "refine",
                "goal_id": entry["goal_id"],
                "obligation_id": entry["obligation_id"],
                "method_ref": dict(chosen),
                "bindings": {},
            },
        ],
        rationale="the review rejected the adopted method; switching to the alternative",
    )


def _planner_declines(request: Any) -> str:
    package = package_of(request)
    return plan_revision_proposal_step(
        proposal_id=f"p-none-{package['plan']['plan_revision']}-{package['planning_attempt']}",
        expected_plan_revision=int(package["plan"]["plan_revision"]),
        read_set=[
            {
                "kind": "task",
                "id": ROOT_TASK,
                "semantic_revision": 1,
                "content_hash": "0" * 64,
            }
        ],
        operations=[],
        rationale="no_applicable_method: every other method is refused or rejected",
    )


def _synthesised_method():
    return _alt_method("plan.synthesised")


def _accept_open_leaves(loop: Orchestrator, mission_id: str) -> int:
    """The Worker + Critic stand-in: accept every primitive of the active plan that
    has no CURRENT acceptance yet, producers before consumers, through the real
    ``LeafAcceptanceAssembly`` — exactly what a scripted Worker reply plus a PASS
    verdict would leave behind, without a second copy of the runtime."""

    dispatch = loop.hierarchical
    network = dispatch.network(mission_id)
    accepted = set(dispatch.root_contributions(mission_id))
    pending = [
        spec
        for spec in network.occurrences
        if spec.form is TaskForm.PRIMITIVE and str(spec.occurrence_id) not in accepted
    ]
    if not pending:
        return 0
    order = {"plan.leaf": 0, "plan.review": 1}
    pending.sort(
        key=lambda spec: order.get(
            network.binding_for_occurrence(spec.occurrence_id).goal_signature.signature_id, 9
        )
    )
    now_ms = int(loop.store.now * 1000)
    dispatch.issue_input_witnesses(mission_id, network, now_ms=now_ms)
    assembly = LeafAcceptanceAssembly(loop.store, loop.commit, dispatch=dispatch)
    done = 0
    for spec in pending:
        task_id = str(spec.task_id)
        declared = dispatch.declared_output_ports_for(mission_id, task_id)
        path = f"out/{task_id}.json"
        assembly.accept(
            mission_id,
            task_id,
            result_id=f"result-{task_id}",
            layers=_passing_layers(),
            artifacts=(_Artifact(f"artifact-{task_id}", path),),
            producer_agent_ids=("agent-worker",),
            reviewer_agent_id="agent-critic",
            now_ms=now_ms + done,
            port_claims=tuple(PortClaim(port_key=item["port"], path=path) for item in declared[:1]),
        )
        done += 1
        network = dispatch.network(mission_id)
        dispatch.issue_input_witnesses(mission_id, network, now_ms=now_ms + done)
    return done


def _drive(
    world: World,
    evidence: Path,
    provider: RoleScriptedProvider,
    *,
    cycles: int = 40,
    accept_leaves: bool = True,
    **config: Any,
) -> dict[str, Any]:
    """Cycle a real Orchestrator until the Mission ends or the budget of cycles is spent."""

    async def case() -> dict[str, Any]:
        async with Orchestrator(_config(evidence, **config), provider) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            original = loop._decide
            accepted: list[int] = []

            async def decide(mission: Any) -> bool:
                if accept_leaves and mission.status is MissionStatus.ACTIVE:
                    accepted.append(_accept_open_leaves(loop, mission.id))
                return await original(mission)

            loop._decide = decide  # type: ignore[method-assign]
            # ``run()``'s body, entered a cycle at a time (the saturation suite's
            # reason: a scripted turn in flight reports heartbeats as progress, so a
            # free-running ``run()`` spends ``max_cycles`` before it can answer).  The
            # idle branch is ``run()``'s own too: record the stall, confirm it once.
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
            events = list(loop.store.list_events(world.mission.id))
            semantics = HtnStore(loop.store)
            synth_requests = [
                json.loads(
                    next(
                        m.content
                        for m in reversed(item.messages)
                        if str(m.role).endswith("user") or str(m.role) == "user"
                    )
                )
                for item in provider.requests
                if any(
                    "[role:method_synthesizer]" in str(m.content) for m in item.messages
                )
            ]
            return {
                "status": mission.status,
                "stop_reason": mission.stop_reason,
                "report": dict(mission.final_report or {}),
                "types": [item.type for item in events],
                "events": events,
                "roles": dict(provider.by_role),
                "revisions": [
                    int(item.payload["plan_revision"])
                    for item in events
                    if item.type == e2e.PLAN_REVISION_COMMITTED
                ],
                "instances": {
                    str(item.instance_id): (str(item.method_ref.method_id))
                    for item in semantics.list_method_instances(world.mission.id, state="ADOPTED")
                },
                "synthesis": [
                    dict(item.payload) for item in events if item.type == SYNTHESIS_ROUND_RECORDED
                ],
                "synth_requests": synth_requests,
                "accepted": accepted,
                "progress": list(loop.progress_log),
            }

    return asyncio.run(case())


def test_the_repair_round_can_switch_the_root_to_another_method_and_complete(tmp_path) -> None:
    """(b) end to end: REJECT → repair round → retire+refine → new leaves → ACCEPT → COMPLETED.

    The reviewer, the Planner and the Mission Judge's Critic all answer through the
    real dispatch path; the Worker and its Critic are stood in for at the accept side.
    """

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _seeded(evidence, key="p23j-e2e-switch", alt=True, free_text=True)
    world.store.close()
    provider = RoleScriptedProvider(
        {
            "root_reviewer": [_reviewer("FAIL", finding=C1_FINDING["detail"]), _reviewer("PASS")],
            "planner": [_planner_replaces],
            "critic": [_judge_critic],
        }
    )
    outcome = _drive(world, evidence, provider, max_planning_attempts=1)
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['types']} "
        f"roles={outcome['roles']} progress={outcome['progress'][-12:]}"
    )
    assert outcome["revisions"] == [1, 2], "one replacement revision on top of the first"
    assert list(outcome['instances'].values()) == ["plan.alt"]
    assert outcome["types"].count("HierarchicalRootReviewRejected") == 1
    assert outcome["types"].count("HierarchicalRootReviewCut") == 2, "re-cut over the new leaves"
    assert outcome["roles"].get("root_reviewer") == 2
    assert outcome["roles"].get("planner") == 1
    assert e2e.GOAL_RESOLUTION_COMMITTED in outcome["types"]
    assert outcome["synthesis"] == [], "no synthesis was needed: the library had a replacement"
    assert sum(outcome["accepted"]) == 2, "the replacement's two leaves"


def test_a_declared_no_method_after_rejection_opens_a_synthesis_round_with_the_findings(
    tmp_path,
) -> None:
    """(c) end to end, the C1 shape: REJECT → repair round → no_applicable_method →
    synthesis round carrying the findings → TRIAL_ADMITTED → Planner retires the rejected
    instance and refines with the new method → leaves → ACCEPT → COMPLETED."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _seeded(evidence, key="p23j-e2e-synth", alt=False, free_text=True)
    world.store.close()
    invented = _synthesised_method()
    provider = RoleScriptedProvider(
        {
            "root_reviewer": [_reviewer("FAIL", finding=C1_FINDING["detail"]), _reviewer("PASS")],
            "planner": [_planner_declines, _planner_replaces],
            "method_synthesizer": [method_proposal_step(invented.to_json())],
            "critic": [_judge_critic],
        }
    )
    # One rung: the repair round is the ladder, so a declined round is followed by the
    # synthesis question rather than by the same question again (P2.3g's ladder is
    # unchanged; H-L3-C1-r1's repair round was its last rung too).
    outcome = _drive(world, evidence, provider, max_planning_attempts=1)
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['types']} "
        f"roles={outcome['roles']} progress={outcome['progress'][-12:]}"
    )
    assert outcome["synthesis"] and outcome["synthesis"][0]["admitted"] is True
    assert outcome["synthesis"][0]["goal_task_id"] == ROOT_TASK
    assert outcome["synthesis"][0]["synthesis_round"] == 2, "round 1 is the pre-plan round"
    request = outcome["synth_requests"][0]
    feedback = "\n".join(request["review_feedback"])
    assert "plan.outer" in feedback, "the rejected method is named"
    assert "does not explain an applied changeset" in feedback, "the findings travel"
    assert request["schema_feedback"] == [], "a review finding is not a decode problem"
    assert outcome["revisions"] == [1, 2]
    assert list(outcome["instances"].values()) == ["plan.synthesised"]
    assert outcome["roles"].get("planner") == 2
    assert outcome["types"].count("PlanningRejected") == 2, "the repair record and the declaration"
    assert e2e.GOAL_RESOLUTION_COMMITTED in outcome["types"]


def test_repeated_rejections_end_honestly_with_the_reason_written_down(tmp_path) -> None:
    """Boundedness: no replacement, no synthesised method that works → FAILED, once each.

    The repair round is opened once per plan revision (``max_root_review_repairs``), the
    synthesis round for a rejected goal once per rejected revision, and the stop report
    says the root review rejected the plan rather than hiding it under "no work".
    """

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _seeded(evidence, key="p23j-e2e-bounded", alt=False)
    world.store.close()
    provider = RoleScriptedProvider(
        {
            "root_reviewer": [_reviewer("FAIL", finding=C1_FINDING["detail"])] * 4,
            "planner": [_planner_declines] * 8,
            # A method naming an operator nobody registered is refused at admission.
            "method_synthesizer": [
                method_proposal_step(
                    method(
                        "plan.bogus",
                        "plan.goal",
                        parameter_schema="plan.goal.params",
                        steps=(
                            step(
                                "ghost",
                                "plan.ghost",
                                TaskForm.PRIMITIVE,
                                {"subject": param("subject")},
                            ),
                        ),
                        links=(("c-root", "ghost", "c-done"),),
                        finalizer="ghost",
                    ).to_json()
                )
            ]
            * 4,
        }
    )
    outcome = _drive(
        world, evidence, provider, cycles=60, accept_leaves=False, max_planning_attempts=1
    )
    assert outcome["status"] is MissionStatus.FAILED, (
        f"{outcome['status']}: {outcome['types']} roles={outcome['roles']}"
    )
    assert outcome["stop_reason"] == "no_dispatchable_work"
    detail = outcome["report"]["detail"]
    assert detail["root_review"]["reason"] == ROOT_REVIEW_REPAIR_REASON
    assert detail["root_review"]["status"] == "REVIEW_REJECTED"
    assert detail["root_review"]["repairs_used"] == 1
    assert "does not explain an applied changeset" in detail["root_review"]["findings"][0]["detail"]
    assert outcome["types"].count("HierarchicalRootReviewRejected") == 1
    repairs = [
        item
        for item in outcome["events"]
        if item.type == "PlanningRejected"
        and item.payload.get("reason") == ROOT_REVIEW_REPAIR_REASON
    ]
    assert len(repairs) == 1, "one repair round per plan revision"
    assert len(outcome["synthesis"]) == 1, "one synthesis round per rejected revision"
    assert outcome["synthesis"][0]["admitted"] is False
    assert outcome["synthesis"][0]["synthesis_round"] == 2
    assert outcome["revisions"] == [1], "the first plan, and nothing after it"
    # P2.3i (merged): ``UNKNOWN_OPERATOR`` is a correctable refusal, so the one round
    # asks twice — bounded by ``MAX_SYNTHESIS_ASKS`` — and concludes on the second.
    assert outcome["roles"].get("method_synthesizer") == 2
    assert outcome["synthesis"][0]["asks"] == 2
    assert outcome["synthesis"][0]["retry_refused"] == ""
    assert outcome["types"].count("MethodSynthesisReplyRejected") == 1
    assert "MissionFailed" in outcome["types"]


def test_the_synthesis_judgment_excludes_the_rejected_method_but_keeps_i18(tmp_path) -> None:
    """``goals_needing_method`` on a rejected root: the system judges, the Planner's
    declaration only sequences.

    * with a usable alternative in the library the goal does **not** need a method;
    * with only the rejected method, it does — but only once the repair round has been
      put to the Planner and answered, never before.
    """

    world = _rejected(tmp_path, key="p23j-judgment", alt=True)
    evidence = Path(tmp_path) / "evidence"

    async def case() -> dict[str, Any]:
        async with Orchestrator(_config(evidence), RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            before = loop.hierarchical.goals_needing_method(mission.id)
            assert await loop._advance_root_review(mission, loop._new_mode(mission))
            in_flight = loop.hierarchical.goals_needing_method(mission.id)
            intent = next(
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if item.mission_id == mission.id and item.kind == "plan"
            )
            await loop._planning_rejected(
                intent, reason="no_applicable_method", detail={"rationale": "none"}
            )
            # The ladder opened the next rung; settle it so nothing is in flight.
            for item in loop.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"):
                if item.mission_id == mission.id and item.kind == "plan":
                    loop._settle_intent(item, "FAILED")
            with_alternative = loop.hierarchical.goals_needing_method(mission.id)
            return {
                "before": before,
                "in_flight": in_flight,
                "with_alternative": with_alternative,
            }

    outcome = asyncio.run(case())
    assert outcome["before"] == ()
    assert outcome["in_flight"] == ()
    assert outcome["with_alternative"] == (), "plan.alt applies; nothing to synthesise"


def test_the_synthesis_judgment_names_the_rejected_root_once_the_planner_declined(tmp_path) -> None:
    world = _rejected(tmp_path, key="p23j-judgment-lonely", alt=False)
    evidence = Path(tmp_path) / "evidence"

    async def case() -> dict[str, Any]:
        async with Orchestrator(_config(evidence), RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            assert await loop._advance_root_review(mission, loop._new_mode(mission))
            in_flight = loop.hierarchical.goals_needing_method(mission.id)
            intent = next(
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if item.mission_id == mission.id and item.kind == "plan"
            )
            await loop._planning_rejected(
                intent, reason="no_applicable_method", detail={"rationale": "none"}
            )
            for item in loop.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"):
                if item.mission_id == mission.id and item.kind == "plan":
                    loop._settle_intent(item, "FAILED")
            after = loop.hierarchical.goals_needing_method(mission.id)
            rejected = loop.hierarchical.rejected_refinements(mission.id)
            return {"in_flight": in_flight, "after": after, "rejected": rejected}

    outcome = asyncio.run(case())
    assert outcome["in_flight"] == (), "not while the Planner is being asked"
    assert outcome["after"] == (ROOT_TASK,)
    assert [str(item.goal_signature_id) for item in outcome["rejected"]] == ["plan.goal"]
