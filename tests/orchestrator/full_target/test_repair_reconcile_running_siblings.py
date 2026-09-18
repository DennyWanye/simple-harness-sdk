# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3s: a repair round must reconcile running sibling attempts before retire+refine.

Grok fifth-batch H-L3-C1-r0 (SDK 0.12.2 candidate f2dfa64):

    seq 483 ResultRejected{read_only_leaf_rewrote_workspace} on inspect (2nd)
    → 486 TaskCancelled{read_only_leaf_needs_write}
    → 487 PlanningRejected{read_only_leaf_needs_write}
    → 500 MethodSynthesisRoundRecorded admitted
    → 512 / 520 / 539 / 549 PlanningRejected{running_work_not_reconciled}
      (proposal retires mi-8e0b… while verify attempt-1 then attempt-2 still open)
    → 525 / 559 ResultRejected on the sibling verify leaf
    → 562 TaskCancelled of that sibling
    → 565 MissionFailed{no_dispatchable_work}

The inspect leaf was cancelled; its sibling verify attempt kept RUNNING.  Compile
refused the repair four times.  Nobody cancelled those attempts, and nobody retried
the stored proposal after they ended.  The Mission died ``no_dispatchable_work``
with a pending repair sitting in ``rejected_refinements``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_inspect_leaf_patch_input import _c1_method  # noqa: E402
from test_read_only_rewrite_bound import (  # noqa: E402
    NEW_COLLECTOR,
    SEED,
    TOOLS,
    _accepting_reviewer,
    _CodeWorld,
    _conservation,
    _four_step,
    _write_and_envelope,
)
from test_root_review_repair_library import (  # noqa: E402
    FREE_TEXT_CRITERION,
    _accept_open_leaves,
    _adopted_root,
    _alt_method,
    _judge_critic,
    _rejected_open,
    _replacement,
    _running_attempt,
)
from test_second_synthesis_adoption import _planner_adopts_applicable  # noqa: E402

from agent_orchestrator.contracts.htn import TaskForm  # noqa: E402
from agent_orchestrator.contracts.models import ContractError, MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import (  # noqa: E402
    TERMINAL_MISSION,
    AttemptStatus,
    MissionStopReason,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    METHOD_RETIRED_BY_REPAIR,
    READ_ONLY_REWRITE_REPAIR_REASON,
    REPAIR_BLOCKED_BY_RUNNING_WORK,
    REPAIR_COMPILE_DEFERRED,
)
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.scheduling.allocator import OPEN_ATTEMPT_STATES  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    method_proposal_step,
    package_of,
)


class _SixLeafWorker:
    """C1 six-leaf Worker: inspect rewrites twice; verify hangs until cancelled."""

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self.inspect_attempts = 0
        self.verify_started = 0
        self.workspace_root = workspace_root
        self._queues: dict[str, list[Any]] = {}

    def __call__(self, request: Any) -> Any:
        package = package_of(request)
        attempt_id = str((package.get("attempt") or {}).get("attempt_id") or "")
        if attempt_id not in self._queues:
            self._queues[attempt_id] = self._script(package)
        queue = self._queues[attempt_id]
        if not queue:
            raise AssertionError(f"worker script exhausted for {attempt_id}")
        step = queue.pop(0)
        if callable(step) and not isinstance(step, (str, tuple)):
            return step(request)
        return step

    def _script(self, package: dict[str, Any]) -> list[Any]:
        goal = str((package.get("task_contract") or {}).get("goal") or "")
        if "read the repository" in goal:
            return _write_and_envelope(
                [("facts.json", '{"tests": ["tests/test_public_collector.py"]}')],
                ["facts.json"],
                {"facts": "facts.json"},
            )
        if "reproduce" in goal:
            return _write_and_envelope(
                [("diagnosis.md", "# diagnosis\nconcurrent record loses counts\n")],
                ["diagnosis.md"],
                {"diagnosis": "diagnosis.md"},
            )
        if "apply a patch" in goal:
            return _write_and_envelope(
                [
                    ("metrics/collector.py", NEW_COLLECTOR),
                    ("applied.patch", "--- a/metrics/collector.py\n+++ b/metrics/collector.py\n"),
                    ("REPORT.md", "# patch\nlocked collector.record\n"),
                ],
                ["metrics/collector.py", "applied.patch", "REPORT.md"],
                {"patch": "applied.patch"},
            )
        if "inspect the changeset" in goal:
            self.inspect_attempts += 1
            # P2.3u: the gateway refuses workspace_write_file on existing
            # source.  These tests still exercise the collector fallback and
            # P2.3s reconcile, so the rewrite is applied on the attempt tree.
            if self.workspace_root is None:
                raise AssertionError(
                    "inspect rewrite needs workspace_root to bypass the write guard"
                )
            attempt_id = str((package.get("attempt") or {}).get("attempt_id") or "")
            root = self.workspace_root
            artifacts = ["metrics/collector.py", "metrics/reporter.py", "findings.md"]
            outputs = {"findings": "findings.md"}

            def poke(_request: Any, *, _aid: str = attempt_id) -> Any:
                (root / _aid / "metrics/collector.py").write_text(
                    NEW_COLLECTOR, encoding="utf-8"
                )
                (root / _aid / "metrics/reporter.py").write_text(
                    NEW_COLLECTOR, encoding="utf-8"
                )
                return (
                    "workspace_write_file",
                    {
                        "path": "findings.md",
                        "content": "# inspect rewrote product files\n",
                    },
                )

            return [
                poke,
                envelope_step(
                    summary="scripted leaf",
                    artifacts=artifacts,
                    claims=["scripted"],
                    override=lambda body: {**body, "outputs": dict(outputs)},
                ),
            ]
        if "run the test suite" in goal:
            self.verify_started += 1
            hang = [("workspace_list", {"path": "."})] * 12
            hang.append(
                _write_and_envelope(
                    [("REPORT.md", "# verify still running\n")],
                    ["REPORT.md"],
                    {"report": "REPORT.md"},
                )[-1]
            )
            return hang
        if "summarise" in goal or "summarize" in goal:
            return _write_and_envelope(
                [("summary.md", "# summary\n")],
                ["summary.md"],
                {"findings": "summary.md"},
            )
        raise AssertionError(f"unexpected leaf goal: {goal!r}")


def _six_leaf_world(tmp_path, *, key: str) -> _CodeWorld:
    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    return _CodeWorld(
        evidence,
        method=_c1_method(),
        key=key,
        db_name="orchestrator.db",
        allowed_tools=TOOLS,
        workspace_seed=SEED,
        success_criteria=(FREE_TEXT_CRITERION,),
        max_attempts=20,
    )


def _repair_rejections(events: list[Any]) -> list[Any]:
    return [
        item
        for item in events
        if item.type == "PlanningRejected"
        and "running_work_not_reconciled" in json.dumps(item.payload, ensure_ascii=False)
    ]


async def _run_until_idle(
    world: _CodeWorld,
    provider: RoleScriptedProvider,
    evidence: Path,
    *,
    accept_after_revision: int | None = 2,
    max_cycles: int = 700,
) -> dict[str, Any]:
    world.store.close()
    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=2,
        test_timeout_seconds=30,
        max_planning_attempts=3,
    )
    async with Orchestrator(config, provider, poll_interval=0.02) as loop:
        world.world.semantics = HtnStore(loop.store)
        loop.install_hierarchical(planning=world.world)
        await asyncio.wait_for(
            _drive(loop, world.mission.id, accept_after_revision, max_cycles),
            timeout=90,
        )
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
            "conservation": _conservation(loop, mission.id),
            "tasks": {task.id: task for task in loop.store.list_tasks(mission.id)},
            "attempts": [
                (task.id, attempt.ordinal, str(attempt.status), dict(attempt.failure or {}))
                for task in loop.store.list_tasks(mission.id)
                for attempt in loop.store.list_attempts(task.id)
            ],
            "open_attempts": [
                attempt.id
                for task in loop.store.list_tasks(mission.id)
                for attempt in loop.store.list_attempts(task.id)
                if attempt.status in OPEN_ATTEMPT_STATES
            ],
        }


async def _drive(
    loop: Orchestrator,
    mission_id: str,
    accept_after_revision: int | None,
    max_cycles: int,
) -> None:
    for _ in range(max_cycles):
        events = list(loop.store.list_events(mission_id))
        revisions = [
            int(item.payload["plan_revision"])
            for item in events
            if item.type == "PlanRevisionCommitted"
        ]
        if accept_after_revision is not None and accept_after_revision in revisions:
            _accept_open_leaves(loop, mission_id)
        progressed = await loop._cycle()
        await asyncio.sleep(0.02)
        mission = loop.store.get_mission(mission_id)
        if mission is not None and mission.status in TERMINAL_MISSION:
            return
        if not progressed and not loop._has_inflight():
            await loop._record_hierarchical_stall()
            await loop._confirm_and_stop_stalled()
            mission = loop.store.get_mission(mission_id)
            if mission is not None and mission.status in TERMINAL_MISSION:
                return


# ======================================================================================
# 1. Compiler safety net still names the defect if nobody reconciled
# ======================================================================================


def test_compile_still_names_running_work_when_the_loop_has_not_reconciled(tmp_path) -> None:
    """Direct ``compile_proposal`` keeps the P2.3j name; the loop is what cancels."""

    world = _rejected_open(tmp_path, key="p23s-compile-guard", alt=True)
    network = world.network()
    leaf = next(
        str(spec.task_id) for spec in network.occurrences if spec.form is TaskForm.PRIMITIVE
    )
    attempt = _running_attempt(world, leaf)
    proposal_text = _replacement(
        world, _alt_method(), instance_id=_adopted_root(world), revision=1
    )
    from agent_orchestrator.planning.planner import parse_plan_proposal

    proposal = parse_plan_proposal(proposal_text, mission_id=world.mission.id)
    with pytest.raises(ContractError, match="running_work_not_reconciled") as caught:
        world.dispatch.compile_proposal(world.mission.id, proposal, network)
    assert attempt in str(caught.value)
    stored = world.store.get_attempt(attempt)
    assert stored is not None and stored.status is AttemptStatus.RUNNING


# ======================================================================================
# 2. apply_planner_reply cancels the sibling and commits the replacement
# ======================================================================================


def test_a_replacement_cancels_open_sibling_attempts_then_commits(tmp_path) -> None:
    """P2.3s: retire+refine no longer waits forever; the running leaf is cancelled."""

    world = _rejected_open(tmp_path, key="p23s-retire-running", alt=True)
    network = world.network()
    leaf = next(
        str(spec.task_id) for spec in network.occurrences if spec.form is TaskForm.PRIMITIVE
    )
    attempt = _running_attempt(world, leaf)
    outcome = world.plan(
        _replacement(world, _alt_method(), instance_id=_adopted_root(world), revision=1),
        command_id="cmd-running",
    )
    assert outcome.committed, outcome.last_reason
    stored = world.store.get_attempt(attempt)
    assert stored is not None
    assert stored.status is AttemptStatus.CANCELLED
    assert METHOD_RETIRED_BY_REPAIR in str((stored.failure or {}).get("reason", ""))
    cancelled = [
        item
        for item in world.store.list_events(world.mission.id)
        if item.type in {"TaskCancelled", "AttemptCancelled"}
        and METHOD_RETIRED_BY_REPAIR in json.dumps(item.payload, ensure_ascii=False)
    ]
    assert cancelled, [item.type for item in world.store.list_events(world.mission.id)]
    assert int(world.network().plan_revision) == 2


# ======================================================================================
# 3. True loop: six-leaf inspect cancelled, sibling verify reconciled, COMPLETED
# ======================================================================================


def test_six_leaf_repair_cancels_the_running_sibling_and_completes(tmp_path) -> None:
    """(a) inspect rewrites twice → sibling verify still RUNNING → cancel it → COMPLETED."""

    evidence = Path(tmp_path) / "evidence"
    world = _six_leaf_world(tmp_path, key="p23s-e2e-six")
    worker = _SixLeafWorker(workspace_root=evidence / "workspaces")
    invented = _four_step("code.fix-by-patch-then-verify.repair", suffix="-v2")
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 120,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 24
            + [_judge_critic] * 4,
            "planner": [_planner_adopts_applicable] * 8,
            "method_synthesizer": [method_proposal_step(invented)] * 2,
            "root_reviewer": [_accepting_reviewer] * 3,
        }
    )
    outcome = asyncio.run(_run_until_idle(world, provider, evidence))
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report']} "
        f"types={outcome['types'][-40:]} roles={outcome['roles']} "
        f"open={outcome['open_attempts']} inspect={worker.inspect_attempts} "
        f"verify_started={worker.verify_started}"
    )
    assert not _repair_rejections(outcome["events"]), [
        item.payload for item in _repair_rejections(outcome["events"])
    ]
    planner_repairs = [
        item
        for item in outcome["events"]
        if item.type == "PlanningRejected"
        and item.payload.get("reason") == READ_ONLY_REWRITE_REPAIR_REASON
    ]
    assert planner_repairs, outcome["types"]
    retired = [
        item
        for item in outcome["events"]
        if item.type in {"TaskCancelled", "AttemptCancelled"}
        and METHOD_RETIRED_BY_REPAIR in json.dumps(item.payload, ensure_ascii=False)
    ]
    assert retired, (
        "the running sibling must be cancelled under method_retired_by_repair, "
        f"got {[item.type for item in outcome['events'] if 'Cancel' in item.type]}"
    )
    assert outcome["roles"].get("planner", 0) <= 4, (
        "r3–r6 must not re-ask the same refused retire+refine "
        f"(planner={outcome['roles'].get('planner')})"
    )
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]
    completed = [
        task for task in outcome["tasks"].values() if str(task.status) == "COMPLETED"
    ]
    assert len(completed) >= 2, "accepted reusable read-only leaves stay COMPLETED"


# ======================================================================================
# 4. P2.3m: cancelling the rewriting leaf leaves no OPEN attempt
# ======================================================================================


def test_cancelling_the_rewriting_leaf_leaves_no_open_attempt(tmp_path) -> None:
    """seq 486: after TaskCancelled the cancelled leaf must not keep an OPEN attempt."""

    evidence = Path(tmp_path) / "evidence"
    world = _six_leaf_world(tmp_path, key="p23s-leaf-a-closed")
    worker = _SixLeafWorker(workspace_root=evidence / "workspaces")
    invented = _four_step("code.fix-by-patch-then-verify.repair", suffix="-v2")
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 120,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 24,
            "planner": [_planner_adopts_applicable] * 8,
            "method_synthesizer": [method_proposal_step(invented)] * 2,
            "root_reviewer": [_accepting_reviewer] * 3,
        }
    )
    outcome = asyncio.run(_run_until_idle(world, provider, evidence))
    cancelled_leaves = [
        item
        for item in outcome["events"]
        if item.type == "TaskCancelled"
        and item.payload.get("reason") == READ_ONLY_REWRITE_REPAIR_REASON
    ]
    assert cancelled_leaves, outcome["types"]
    cancelled_ids = {item.task_id for item in cancelled_leaves}
    still_open = [
        (task_id, ordinal, status)
        for task_id, ordinal, status, _failure in outcome["attempts"]
        if task_id in cancelled_ids and status in {str(item) for item in OPEN_ATTEMPT_STATES}
    ]
    assert still_open == [], still_open


# ======================================================================================
# 5. Repair budget spent → named stop, never no_dispatchable_work
# ======================================================================================


def test_spent_repair_stops_named_not_no_dispatchable_work(tmp_path) -> None:
    """(b) the repair cannot land; stop is planning_failed / repair_blocked_*, not stall."""

    evidence = Path(tmp_path) / "evidence"
    world = _six_leaf_world(tmp_path, key="p23s-named-stop")
    worker = _SixLeafWorker(workspace_root=evidence / "workspaces")
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 80,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 12,
            "planner": ["not a proposal"] * 8,
            "method_synthesizer": ["not a method proposal"] * 4,
            "root_reviewer": [_accepting_reviewer] * 2,
        }
    )
    outcome = asyncio.run(
        _run_until_idle(world, provider, evidence, accept_after_revision=None)
    )
    assert outcome["status"] is MissionStatus.FAILED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report']}"
    )
    assert outcome["stop_reason"] != str(MissionStopReason.NO_DISPATCHABLE_WORK), (
        outcome["stop_reason"],
        outcome["report"],
    )
    assert outcome["stop_reason"] == str(MissionStopReason.PLANNING_FAILED)
    blob = json.dumps(outcome["report"], ensure_ascii=False)
    assert (
        REPAIR_BLOCKED_BY_RUNNING_WORK in blob
        or READ_ONLY_REWRITE_REPAIR_REASON in blob
        or "read_only_leaf_needs_write" in blob
    ), outcome["report"]
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]
    assert outcome["conservation"]["reserved"] == 0, outcome["conservation"]


# ======================================================================================
# 6. Live foreign lease → pending, no extra Planner, resume when the attempt ends
# ======================================================================================


def test_a_live_foreign_lease_defers_compile_and_retries_without_replanning(tmp_path) -> None:
    """If the sibling cannot be cancelled yet, the proposal is stored, not re-asked."""

    world = _rejected_open(tmp_path, key="p23s-deferred", alt=True)
    network = world.network()
    leaf = next(
        str(spec.task_id) for spec in network.occurrences if spec.form is TaskForm.PRIMITIVE
    )
    attempt = _running_attempt(world, leaf)
    live = world.store.get_attempt(attempt)
    assert live is not None
    world.store.update_attempt(
        replace(
            live,
            lease_owner="other-owner",
            lease_expires_at=float(world.store.now) + 86_400.0,
        ),
        expected_version=live.version,
    )
    from agent_orchestrator.orchestrator.hierarchical_dispatch import RepairBlockedByRunningWork

    with pytest.raises(RepairBlockedByRunningWork):
        world.dispatch.apply_planner_reply(
            world.mission.id,
            _replacement(world, _alt_method(), instance_id=_adopted_root(world), revision=1),
            principal=world.principal,
            command_id="cmd-deferred",
            owner="this-orchestrator",
        )
    pending = [
        item
        for item in world.store.list_events(world.mission.id)
        if item.type == REPAIR_COMPILE_DEFERRED
    ]
    assert pending, [item.type for item in world.store.list_events(world.mission.id)]
    still = world.store.get_attempt(attempt)
    assert still is not None and still.status is AttemptStatus.RUNNING
    assert int(world.network().plan_revision) == 1
