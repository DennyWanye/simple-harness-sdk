# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3v: record upstream write-leaf products after retire, and bound identical
verification failures.

Grok fifth-batch H-L4-M2-r1: seed method ``code.fix-by-patch`` was root-review
REJECTED and retired; the adopted ``code.fix-by-diagnose-patch-verify-explain``
apply leaf (``code.apply-patch``) wrote ``net/retry.py`` to the *same* hash the
retired apply had already accepted, listed it on the envelope, and P2.3m's
same-hash filter dropped it from recorded artifacts.  ``rule_check`` then failed
42 times with ``artifact 'net/retry.py' is not a recorded workspace file`` until
``budget_exhausted(attempts=48)``.  Hidden grader PASS.

H-L4-M3-r0: the ``code.reproduce-failure`` leaf failed ``code_test`` 21 times
with the same SyntaxError until the task token account died.  Not the same
recorded-file hole, but the same unbounded retry of one identical failure.

Two repairs, no new config, no 20th ``_new_mode`` site:

* a write leaf's listed / changed files are recorded even when the bytes match a
  retired method's accepted artifact; a producer that only delivered a unified
  diff has that diff applied onto the seed and the patched paths recorded.
* N identical verification failures on one occurrence (same layer + same
  problems hash) escalate to ``PlanningRejected{repeated_verification_failure}``
  instead of burning the attempts wall.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_deployment_wiring import _task_of  # noqa: E402
from test_read_only_rewrite_bound import (  # noqa: E402
    _accepting_reviewer,
    _four_step,
    _planner_picks_named,
)
from test_root_review_repair_library import (  # noqa: E402
    _planner_declines,
)
from test_verify_workspace_inputs import (  # noqa: E402
    NOT_RECORDED,
    PATCH_DIFF,
    PATCH_TEXT,
    PATCHED_WINDOW,
    REPORT,
    SEED_WINDOW,
    WINDOW,
    _write_and_envelope,
)
from test_verify_workspace_inputs import _world as _c3_world  # noqa: E402

from agent_orchestrator.artifacts.bound_workspace import (  # noqa: E402
    UnifiedDiffApplyError,
    decode_unified_diff_text,
    files_patched_by_unified_diff,
)
from agent_orchestrator.contracts import Budget  # noqa: E402
from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import (  # noqa: E402
    MissionStopReason,
)
from agent_orchestrator.orchestrator.commit_service import (  # noqa: E402
    MissionSpec,
    mission_account,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    REPEATED_VERIFICATION_FAILURE_REASON,
)
from agent_orchestrator.orchestrator.occurrence_tasks import (  # noqa: E402
    MAX_IDENTICAL_VERIFICATION_FAILURES,
    verification_failure_fingerprint,
)
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    DEMO_PROPOSAL,
    DEMO_SEED,
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    method_proposal_step,
    package_of,
)

NO_CLAIMS = "no claims were submitted"


# ======================================================================================
# 1. Constants and the fingerprint
# ======================================================================================


def test_the_identical_failure_bound_is_three() -> None:
    assert MAX_IDENTICAL_VERIFICATION_FAILURES == 3


def test_the_fingerprint_is_layer_plus_problems_not_timing() -> None:
    """M2's 42 rule_check rows share one problems list.  M3's code_test summaries
    differ by ``0.06s`` / ``0.07s`` and by the attempt workspace path."""

    m2 = {
        "layer": "rule_check",
        "summary": NOT_RECORDED,
        "detail": {
            "problems": [NOT_RECORDED],
            "checked_artifacts": [WINDOW, PATCH_DIFF, REPORT],
        },
    }
    assert verification_failure_fingerprint([m2]) == verification_failure_fingerprint(
        [{**m2, "detail": {**m2["detail"], "checked_artifacts": [REPORT, WINDOW]}}]
    )
    a = {
        "layer": "code_test",
        "summary": "pytest failed: 1 error in 0.06s",
        "detail": {},
    }
    b = {
        "layer": "code_test",
        "summary": "pytest failed: 1 error in 0.08s",
        "detail": {},
    }
    assert verification_failure_fingerprint([a]) == verification_failure_fingerprint([b])
    other = {
        "layer": "code_test",
        "summary": "pytest failed: 2 failed in 0.06s",
        "detail": {},
    }
    assert verification_failure_fingerprint([a]) != verification_failure_fingerprint([other])
    assert verification_failure_fingerprint([m2]) != verification_failure_fingerprint([a])


def _code_test_stdout(exc: str, *, attempt: str, seconds: str) -> dict[str, Any]:
    """M3-r0 shape: summary is only the last pytest line; the exception is in stdout."""

    stdout = (
        "==================================== ERRORS ====================================\n"
        "___________________ ERROR collecting tests/test_dispatch.py ____________________\n"
        f"E     File \"/tmp/workspaces/task-d6d9:{attempt}-verify/service/beta/broken.py\""
        ", line 5\n"
        "E       def dispatch(queue, sink)\n"
        "E                                ^\n"
        f"E   {exc}\n"
        "ERROR tests/test_dispatch.py\n"
        f"1 error in {seconds}\n"
    )
    return {
        "layer": "code_test",
        "summary": f"pytest failed: 1 error in {seconds}",
        "detail": {"runs": [{"stdout": stdout, "passed": False, "returncode": 2}]},
    }


def test_code_test_fingerprint_uses_the_exception_not_just_error_count() -> None:
    """P1-2: M3-r0's 21 SyntaxError rows stay identical; SyntaxError vs ImportError
    must not share a fingerprint just because both say ``1 error in Xs``."""

    syntax_a = _code_test_stdout("SyntaxError: expected ':'", attempt="attempt-1", seconds="0.06s")
    syntax_b = _code_test_stdout("SyntaxError: expected ':'", attempt="attempt-21", seconds="0.08s")
    imported = _code_test_stdout(
        "ImportError: cannot import name 'dispatch'", attempt="attempt-2", seconds="0.06s"
    )
    assert verification_failure_fingerprint([syntax_a]) == verification_failure_fingerprint(
        [syntax_b]
    )
    assert verification_failure_fingerprint([syntax_a]) != verification_failure_fingerprint(
        [imported]
    )


def test_c3_unified_diff_patches_the_seed_window() -> None:
    """P2.3o's leftover: a producer that only delivered ``patch.diff`` still
    names the patched seed file after the diff is applied."""

    patched = files_patched_by_unified_diff(PATCH_TEXT, {WINDOW: SEED_WINDOW})
    assert patched[WINDOW] == PATCHED_WINDOW
    assert "end - 1" not in patched[WINDOW]


OTHER = "stats/other.py"
OTHER_SEED = "def n():\n    return 1\n"
OTHER_PATCHED = "def n():\n    return 2\n"
TWO_FILE_DIFF = PATCH_TEXT.rstrip() + (
    "\n--- a/stats/other.py\n"
    "+++ b/stats/other.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def n():\n"
    "-    return 1\n"
    "+    return 2\n"
)


def test_a_two_file_unified_diff_patches_both_seed_files() -> None:
    """P1-1: switching to the next ``--- `` must flush the current hunk."""

    patched = files_patched_by_unified_diff(
        TWO_FILE_DIFF, {WINDOW: SEED_WINDOW, OTHER: OTHER_SEED}
    )
    assert patched[WINDOW] == PATCHED_WINDOW
    assert patched[OTHER] == OTHER_PATCHED


def test_a_hunk_mismatch_on_one_file_rejects_the_whole_diff() -> None:
    """P1-1: one failed file must not leave the other file registered."""

    broken = TWO_FILE_DIFF.replace("-    return 1\n", "-    return 99\n")
    with pytest.raises(UnifiedDiffApplyError) as caught:
        files_patched_by_unified_diff(broken, {WINDOW: SEED_WINDOW, OTHER: OTHER_SEED})
    assert caught.value.path == OTHER
    assert caught.value.reason == "hunk_mismatch"


def test_a_parent_directory_path_in_the_diff_is_rejected() -> None:
    """P1-1: ``../`` is fail-closed, not skipped."""

    text = (
        "--- a/../secret.py\n"
        "+++ b/../secret.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x\n"
        "+y\n"
    )
    with pytest.raises(UnifiedDiffApplyError) as caught:
        files_patched_by_unified_diff(text, {"secret.py": "x\n", WINDOW: SEED_WINDOW})
    assert ".." in caught.value.path or caught.value.reason == "unsafe_path"


def test_a_binary_diff_document_is_rejected_as_not_utf8() -> None:
    """P1-1: a non-utf8 patch document is a named refusal, not a silent skip."""

    with pytest.raises(UnifiedDiffApplyError) as caught:
        decode_unified_diff_text(b"\xff\xfe--- a/x\n", path="patch.diff")
    assert caught.value.path == "patch.diff"
    assert caught.value.reason == "not_utf8"


# ======================================================================================
# 2. Scripted workers
# ======================================================================================


class _SwitchWorker:
    """facts / reproduce / apply / verify.  ``apply`` writes the patched source
    (same bytes every time, matching the retired leaf after a method switch) and
    lists it.  ``verify`` claims the upstream path the way M2-r1's envelope did."""

    def __init__(self, *, apply_mode: str = "write-source") -> None:
        self.apply_mode = apply_mode
        self.apply_attempts = 0
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
                [("FACTS.md", '{"tests": ["tests/test_public_window.py"]}\n')],
                ["FACTS.md"],
                {"facts": "FACTS.md"},
            )
        if "reproduce" in goal:
            return _write_and_envelope(
                [("diagnosis.md", "# diagnosis\nwindow_sum drops the last sample\n")],
                ["diagnosis.md"],
                {"diagnosis": "diagnosis.md"},
            )
        if "apply a patch" in goal:
            self.apply_attempts += 1
            if self.apply_mode == "diff-only":
                return _write_and_envelope(
                    [
                        (PATCH_DIFF, PATCH_TEXT),
                        (REPORT, "# patch\nwindow_sum now covers the exclusive end\n"),
                    ],
                    [PATCH_DIFF, REPORT],
                    {"patch": PATCH_DIFF},
                )
            if self.apply_mode == "empty-claims":
                steps = [
                    ("workspace_write_file", {"path": PATCH_DIFF, "content": PATCH_TEXT}),
                    ("workspace_write_file", {"path": REPORT, "content": "# patch\n"}),
                    envelope_step(
                        summary="scripted apply without claims",
                        artifacts=[PATCH_DIFF, REPORT],
                        claims=[],
                        override=lambda body: {**body, "outputs": {"patch": PATCH_DIFF}},
                    ),
                ]
                return steps
            return _write_and_envelope(
                [
                    (WINDOW, PATCHED_WINDOW),
                    (PATCH_DIFF, PATCH_TEXT),
                    (REPORT, "# patch\nwindow_sum now covers the exclusive end\n"),
                ],
                [WINDOW, PATCH_DIFF, REPORT],
                {"patch": PATCH_DIFF},
            )
        if "run the test suite" in goal:
            return _write_and_envelope(
                [(REPORT, "# verify\n2 passed\nwindow_sum covers the requested range\n")],
                [WINDOW, REPORT],
                {"report": REPORT},
            )
        raise AssertionError(f"unexpected leaf goal: {goal!r}")


class _RejectThenAccept:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, request: Any) -> str:
        self.n += 1
        if self.n == 1:
            shown = json.loads(
                next(
                    m.content
                    for m in reversed(request.messages)
                    if str(m.role).endswith("user")
                )
            )
            return (
                "<critic_verdict>"
                + json.dumps(
                    {
                        "verdict": "FAIL",
                        "findings": [
                            {
                                "severity": "blocker",
                                "detail": "c-change-explained is not met by the covering report",
                            }
                        ],
                        "mission_criteria": [
                            {
                                "criterion": item["criterion_id"],
                                "met": False,
                                "reason": "scripted reject",
                            }
                            for item in shown["criteria"]
                        ],
                    }
                )
                + "</critic_verdict>"
            )
        return _accepting_reviewer(request)


def _conservation(loop: Orchestrator, mission_id: str) -> dict[str, Any]:
    report = loop.commit.ledger.costs_report(mission_id)
    account = next(
        item for item in report["accounts"] if item["account_id"] == mission_account(mission_id)
    )
    remaining = int(account["remaining_tokens"] or 0)
    reserved = int(account["reserved_tokens"])
    settled = int(account["settled_tokens"])
    pool = int(account["limits"]["max_tokens"])
    return {
        "holds": remaining + reserved + settled == pool,
        "remaining": remaining,
        "reserved": reserved,
        "settled": settled,
        "pool": pool,
        "held_reservations": list(report["held_reservations"]),
        "attempts_created": int(account["attempts_created"]),
    }


def _run(
    world: Any,
    tmp_path: Any,
    provider: RoleScriptedProvider,
    *,
    cycles: int = 500,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = Path(tmp_path) / "evidence"
    world.store.close()

    async def case() -> dict[str, Any]:
        config = OrchestratorConfig(
            evidence_root=evidence,
            max_concurrency=1,
            test_timeout_seconds=30,
            max_planning_attempts=1,
            **(extra or {}),
        )
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.world.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.world)
            await asyncio.wait_for(loop.run(max_cycles=cycles), timeout=45)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            events = list(loop.store.list_events(mission.id))
            apply_id = _task_of(
                loop._hierarchical or world.dispatch, mission.id, "code.apply-patch"
            )
            verify_id = _task_of(
                loop._hierarchical or world.dispatch, mission.id, "code.verify-tests"
            )
            apply_attempts = list(loop.store.list_attempts(apply_id))
            verify_attempts = list(loop.store.list_attempts(verify_id))
            intent_inputs: list[str] = []
            if verify_attempts:
                intent = loop.store.get_intent_for_subject(verify_attempts[0].id)
                if intent is not None:
                    intent_inputs = [
                        item["path"] for item in (intent.config.get("inputs") or [])
                    ]
            apply_paths = sorted(
                {
                    artifact.path
                    for attempt in apply_attempts
                    for artifact in loop.store.list_artifacts(attempt.id)
                }
            )
            return {
                "status": mission.status,
                "stop_reason": mission.stop_reason,
                "report": dict(mission.final_report or {}),
                "types": [item.type for item in events],
                "events": events,
                "conservation": _conservation(loop, mission.id),
                "verify_inputs": intent_inputs,
                "verify_attempts": len(verify_attempts),
                "apply_id": apply_id,
                "apply_attempts": len(apply_attempts),
                "apply_paths": apply_paths,
                "apply_statuses": [str(item.status) for item in apply_attempts],
                "task_status": {
                    task.id: (str(task.status), task.goal)
                    for task in loop.store.list_tasks(mission.id)
                },
                "roles": dict(provider.by_role),
                "progress": list(loop.progress_log),
                "attempts": [
                    (task.id, attempt.ordinal, str(attempt.status))
                    for task in loop.store.list_tasks(mission.id)
                    for attempt in loop.store.list_attempts(task.id)
                ],
            }

    return asyncio.run(case())


# ======================================================================================
# 3. True Orchestrator.run()
# ======================================================================================


def test_apply_leaf_that_only_delivers_a_diff_prelays_the_patched_source(tmp_path) -> None:
    """Producer accepts ``patch.diff`` only.  Downstream verify still starts from
    the patched ``stats/window.py`` and the Mission COMPLETED."""

    world = _c3_world(tmp_path, key="p23v-diff-only")
    worker = _SwitchWorker(apply_mode="diff-only")
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 40,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 16,
            "root_reviewer": [_accepting_reviewer],
        }
    )
    outcome = _run(world, tmp_path, provider)
    assert WINDOW in outcome["apply_paths"], outcome["apply_paths"]
    assert WINDOW in outcome["verify_inputs"], outcome["verify_inputs"]
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report'].get('detail')} "
        f"types={outcome['types'][-24:]}"
    )


def test_root_review_reject_then_new_apply_records_the_same_source_path(tmp_path) -> None:
    """(a) REJECT → retire+refine → new apply writes the retired apply's bytes
    and lists them → recorded / prelaid on verify → COMPLETED."""

    world = _c3_world(tmp_path, key="p23v-switch")
    worker = _SwitchWorker()
    invented = _four_step("code.fix-by-patch.p23v-repair", suffix="-v2")
    admitted = world.dispatch.apply_synthesizer_reply(
        world.mission.id, method_proposal_step(invented)
    )
    assert admitted.admitted, admitted.problems

    def _pick_repair(request: Any) -> str:
        package = package_of(request)
        if not package.get("rejected_refinements"):
            return _planner_declines(request)
        try:
            return _planner_picks_named("p23v-repair")(request)
        except AssertionError:
            return _planner_declines(request)

    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 80,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 24,
            "planner": [_pick_repair] * 6,
            "root_reviewer": [_RejectThenAccept()] * 6,
        }
    )
    outcome = _run(world, tmp_path, provider)
    assert "HierarchicalRootReviewRejected" in outcome["types"], outcome["types"]
    revisions = [
        int(item.payload["plan_revision"])
        for item in outcome["events"]
        if item.type == "PlanRevisionCommitted"
    ]
    assert 2 in revisions, revisions
    assert WINDOW in outcome["apply_paths"], outcome["apply_paths"]
    problems = [
        problem
        for item in outcome["events"]
        if item.type == "VerificationFailed"
        for failure in item.payload.get("failures") or []
        for problem in (failure.get("detail") or {}).get("problems") or [failure.get("summary")]
    ]
    assert NOT_RECORDED not in problems, problems
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report'].get('detail')} "
        f"types={outcome['types'][-24:]} roles={outcome['roles']}"
    )
    assert WINDOW in outcome["verify_inputs"], outcome["verify_inputs"]


def test_identical_rule_check_failures_escalate_instead_of_burning_attempts(
    tmp_path,
) -> None:
    """(b) Inject the same rule_check failure N times → planning, named stop,
    not ``budget_exhausted``.  Reservations released, conservation holds."""

    world = _c3_world(tmp_path, key="p23v-bound")
    worker = _SwitchWorker(apply_mode="empty-claims")
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 80,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 16,
            "planner": [_planner_declines] * 8,
            "method_synthesizer": ["not a method proposal"] * 4,
            "root_reviewer": [_accepting_reviewer] * 2,
        }
    )
    outcome = _run(world, tmp_path, provider)
    assert outcome["status"] is MissionStatus.FAILED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report']} "
        f"types={outcome['types']} roles={outcome['roles']}"
    )
    assert outcome["stop_reason"] == str(MissionStopReason.PLANNING_FAILED), (
        outcome["stop_reason"],
        outcome["report"],
    )
    blob = json.dumps(outcome["report"].get("detail") or {}, ensure_ascii=False)
    repairs = [
        item
        for item in outcome["events"]
        if item.type == "PlanningRejected"
        and item.payload.get("reason") == REPEATED_VERIFICATION_FAILURE_REASON
    ]
    assert repairs, (outcome["types"], blob)
    assert REPEATED_VERIFICATION_FAILURE_REASON in blob or repairs, blob
    assert worker.apply_attempts == MAX_IDENTICAL_VERIFICATION_FAILURES, worker.apply_attempts
    assert outcome["apply_attempts"] == MAX_IDENTICAL_VERIFICATION_FAILURES, outcome[
        "apply_attempts"
    ]
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]
    assert outcome["conservation"]["reserved"] == 0, outcome["conservation"]
    failed = [
        item
        for item in outcome["events"]
        if item.type == "VerificationFailed"
        for failure in item.payload.get("failures") or []
        if failure.get("layer") == "rule_check"
        and NO_CLAIMS
        in str((failure.get("detail") or {}).get("problems") or failure.get("summary"))
    ]
    assert len(failed) == MAX_IDENTICAL_VERIFICATION_FAILURES, len(failed)


def test_legacy_identical_failures_do_not_open_a_planning_repair(tmp_path) -> None:
    """(c) A legacy Mission still retries a failed verification and never emits
    ``PlanningRejected{repeated_verification_failure}``.  The demo script fails
    once then passes — the escalate door is hierarchical-only."""

    from agent_orchestrator.testing.fixtures import demo_single_task_provider

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    provider = demo_single_task_provider()

    async def case() -> dict[str, Any]:
        config = OrchestratorConfig(
            evidence_root=evidence,
            max_concurrency=1,
            test_timeout_seconds=30,
        )
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            mission = await loop.submit_mission(
                MissionSpec(
                    goal=str(DEMO_PROPOSAL["root_goal"]),
                    success_criteria=tuple(DEMO_PROPOSAL["success_criteria"]),
                    tenant_id="tenant-p23v-legacy",
                    idempotency_key="p23v-legacy",
                    allowed_tools=tuple(DEMO_PROPOSAL["allowed_tools"]),
                    workspace_seed=dict(DEMO_SEED),
                    budget=Budget(max_tokens=200_000, max_attempts=12),
                )
            )
            await asyncio.wait_for(loop.run(max_cycles=200), timeout=30)
            final = loop.store.get_mission(mission.id)
            assert final is not None
            events = list(loop.store.list_events(mission.id))
            return {
                "status": final.status,
                "stop_reason": final.stop_reason,
                "types": [item.type for item in events],
                "reasons": [
                    item.payload.get("reason")
                    for item in events
                    if item.type == "PlanningRejected"
                ],
                "failed": sum(1 for item in events if item.type == "VerificationFailed"),
                "attempts": sum(
                    1
                    for task in loop.store.list_tasks(mission.id)
                    for _attempt in loop.store.list_attempts(task.id)
                ),
            }

    outcome = asyncio.run(case())
    assert outcome["status"] is MissionStatus.COMPLETED, outcome
    assert outcome["failed"] >= 1, outcome
    assert outcome["attempts"] >= 2, outcome
    assert REPEATED_VERIFICATION_FAILURE_REASON not in outcome["reasons"], outcome
