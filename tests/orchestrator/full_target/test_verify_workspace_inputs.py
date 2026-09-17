# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3o: a downstream leaf's workspace must hold the upstream accepted patch.

Grok H-L3-C3-r0 (third batch): seed method ``code.fix-by-patch@2``, facts / diagnosis
/ patch accepted, hidden grader PASS on the patched ``stats/window.py``.  The verify
leaf then failed ``rule_check`` nine times with
``artifact 'stats/window.py' is not a recorded workspace file`` and the Mission
died ``budget_exhausted``.

The InputManifest bound only ``patch.diff``.  The verify workspace started from the
unpatched seed.  The Worker applied the accepted bytes itself, listed
``stats/window.py`` on the envelope, and P2.3m's same-hash filter dropped the path
from the Attempt's recorded artifacts — so ``rule_check`` could not see it, and
``code_test`` rebuilt from seed + ``patch.diff`` + ``REPORT.md`` ran against the
red baseline.

The fixture under ``fixtures/htn/c3_verify_workspace/`` is the real
``VerificationFailed`` payload and the seed / patched bytes.  After the fix the
verify leaf is dispatched on the patched snapshot, bound files count as recorded
workspace files, ``rule_check`` / ``code_test`` pass, and the root review ACCEPT
completes the Mission.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_deployment_wiring import _task_of  # noqa: E402
from test_inspect_leaf_patch_input import (  # noqa: E402
    INSPECT,
    _c1_method,
    _CodeWorld,
    _rebound,
)
from test_read_only_rewrite_bound import (  # noqa: E402
    _accepting_reviewer,
    _four_step,
)

from agent_orchestrator.artifacts.bound_workspace import (  # noqa: E402
    bound_artifacts_named_in_envelope,
    overlay_bound_producer_files,
)
from agent_orchestrator.artifacts.versioning import UpstreamInput  # noqa: E402
from agent_orchestrator.contracts.models import Artifact, MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import MissionStopReason  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import mission_account  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    critic_step,
    envelope_step,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "htn" / "c3_verify_workspace"
NOT_RECORDED = "artifact 'stats/window.py' is not a recorded workspace file"
WINDOW = "stats/window.py"
PATCH_DIFF = "patch.diff"
REPORT = "REPORT.md"
TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list")

SEED_WINDOW = (FIXTURE / "seed_window.py").read_text(encoding="utf-8")
PATCHED_WINDOW = (FIXTURE / "patched_window.py").read_text(encoding="utf-8")
PATCH_TEXT = (FIXTURE / "patch.diff").read_text(encoding="utf-8")
TEST_TEXT = (FIXTURE / "seed_test_public_window.py").read_text(encoding="utf-8")
SEED_HASH = hashlib.sha256(SEED_WINDOW.encode("utf-8")).hexdigest()
PATCHED_HASH = hashlib.sha256(PATCHED_WINDOW.encode("utf-8")).hexdigest()

SEED = {
    "README-task.md": "Make the named failing test pass.\n",
    "stats/__init__.py": (FIXTURE / "seed_init.py").read_text(encoding="utf-8"),
    WINDOW: SEED_WINDOW,
    "tests/test_public_window.py": TEST_TEXT,
}


def _artifact(task_id: str, path: str, data: bytes, *, artifact_id: str) -> Artifact:
    digest = hashlib.sha256(data).hexdigest()
    return Artifact(
        id=artifact_id,
        mission_id="mission-c3",
        task_id=task_id,
        attempt_id=f"{task_id}:attempt-1",
        type="file",
        path=path,
        version=1,
        content_hash=digest,
        size_bytes=len(data),
        produced_by="w",
        storage_uri="",
    )


# ======================================================================================
# 1. The defect, pinned on the real VerificationFailed payload
# ======================================================================================


def test_c3_r0_failed_rule_check_because_window_py_was_not_a_recorded_workspace_file() -> None:
    """The fixture is the defect, verbatim: verify listed the patched source and
    ``REPORT.md``, and ``rule_check`` refused the source as unrecorded."""

    payload = json.loads((FIXTURE / "verification_failed.json").read_text())
    failure = payload["failures"][0]
    assert failure["layer"] == "rule_check"
    assert failure["status"] == "FAIL"
    assert failure["summary"] == NOT_RECORDED
    assert failure["detail"]["problems"] == [NOT_RECORDED]
    assert failure["detail"]["checked_artifacts"] == [WINDOW, REPORT]
    assert SEED_HASH != PATCHED_HASH
    assert "end - 1" in SEED_WINDOW
    assert "end - 1" not in PATCHED_WINDOW


# ======================================================================================
# 2. Overlay: a patch binding also places the producer's accepted seed files
# ======================================================================================


def test_overlay_adds_the_producer_seed_file_next_to_the_patch_document() -> None:
    """The manifest places ``patch.diff``.  The producer also accepted the patched
    ``stats/window.py``.  The consumer workspace has to start from that file."""

    patch_task = "task-patch"
    inputs = [
        UpstreamInput(patch_task, PATCH_DIFF, "d" * 64, "artifact-diff"),
    ]
    window = _artifact(patch_task, WINDOW, PATCHED_WINDOW.encode("utf-8"), artifact_id="artifact-w")
    report = _artifact(patch_task, REPORT, b"# patch\n", artifact_id="artifact-r")
    overlay = overlay_bound_producer_files(
        inputs,
        seed_paths=set(SEED),
        artifacts_by_producer={patch_task: [window, report]},
    )
    assert [item.path for item in overlay] == [PATCH_DIFF, WINDOW]
    added = next(item for item in overlay if item.path == WINDOW)
    assert added.artifact_id == "artifact-w"
    assert added.content_hash == PATCHED_HASH
    assert added.task_id == patch_task


def test_overlay_does_not_sweep_an_order_only_predecessor() -> None:
    """§24.1 decision 4: no DATA binding, no files.  Overlay must not invent a
    producer that the manifest did not name."""

    overlay = overlay_bound_producer_files(
        [],
        seed_paths=set(SEED),
        artifacts_by_producer={
            "task-facts": [
                _artifact("task-facts", WINDOW, b"x", artifact_id="artifact-facts-w"),
            ]
        },
    )
    assert overlay == []


def test_inspect_and_summarize_optional_patch_bindings_get_the_same_overlay() -> None:
    """P2.3k's inspect/summarize@2 optional ``patch`` port is the same DATA edge."""

    patch_task = "task-apply"
    inputs = [UpstreamInput(patch_task, "out/patch.json", "a" * 64, "artifact-port")]
    window = _artifact(patch_task, WINDOW, PATCHED_WINDOW.encode("utf-8"), artifact_id="artifact-w")
    overlay = overlay_bound_producer_files(
        inputs,
        seed_paths={WINDOW, "kv.py"},
        artifacts_by_producer={patch_task: [window]},
    )
    assert {item.path for item in overlay} == {"out/patch.json", WINDOW}


# ======================================================================================
# 3. rule_check: a bound input the envelope named is a recorded workspace file
# ======================================================================================


def test_an_envelope_path_that_names_a_bound_input_is_a_recorded_workspace_file() -> None:
    """C3's envelope listed ``stats/window.py``.  After overlay that path is a bound
    input whose artifact already lives on the producer Attempt."""

    recorded = [
        _artifact("task-verify", REPORT, b"# verify\n", artifact_id="artifact-report"),
    ]
    bound = [
        UpstreamInput("task-patch", PATCH_DIFF, "d" * 64, "artifact-diff"),
        UpstreamInput("task-patch", WINDOW, PATCHED_HASH, "artifact-w"),
    ]
    window = _artifact(
        "task-patch", WINDOW, PATCHED_WINDOW.encode("utf-8"), artifact_id="artifact-w"
    )
    by_id = {window.id: window}

    def lookup(artifact_id: str) -> Artifact | None:
        return by_id.get(artifact_id)

    combined = bound_artifacts_named_in_envelope([WINDOW, REPORT], recorded, bound, lookup)
    assert {item.path for item in combined} == {WINDOW, REPORT}
    assert next(item for item in combined if item.path == WINDOW).id == "artifact-w"


def test_an_envelope_path_that_is_neither_recorded_nor_bound_stays_missing() -> None:
    recorded = [_artifact("task-verify", REPORT, b"# v\n", artifact_id="artifact-report")]
    combined = bound_artifacts_named_in_envelope(
        [WINDOW, REPORT],
        recorded,
        [UpstreamInput("task-patch", PATCH_DIFF, "d" * 64, "artifact-diff")],
        lambda _artifact_id: None,
    )
    assert [item.path for item in combined] == [REPORT]


# ======================================================================================
# 4. True Orchestrator.run(): patch accepted → verify workspace is patched → COMPLETED
# ======================================================================================


class _C3Worker:
    """Scripted Worker for ``code.fix-by-patch@2`` in the C3 envelope shape.

    The verify leaf lists ``stats/window.py`` *and* ``REPORT.md``, the way the
    real episode did, and writes the patched source (same bytes the patch leaf
    already accepted).  Before the fix that is the ``rule_check`` failure; after,
    the path is a bound baseline file and verification passes.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[Any]] = {}
        self.verify_attempts = 0

    def __call__(self, request: Any) -> Any:
        from agent_orchestrator.testing.fixtures import package_of

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
            self.verify_attempts += 1
            return _write_and_envelope(
                [
                    (WINDOW, PATCHED_WINDOW),
                    (REPORT, "# verify\n2 passed\nwindow_sum covers the requested range\n"),
                ],
                [WINDOW, REPORT],
                {"report": REPORT},
            )
        raise AssertionError(f"unexpected leaf goal: {goal!r}")


def _write_and_envelope(
    writes: list[tuple[str, str]], artifacts: list[str], outputs: dict[str, str]
) -> list[Any]:
    steps: list[Any] = [
        ("workspace_write_file", {"path": path, "content": text}) for path, text in writes
    ]
    steps.append(
        envelope_step(
            summary="scripted leaf",
            artifacts=artifacts,
            claims=["scripted"],
            override=lambda body: {**body, "outputs": dict(outputs)},
        )
    )
    return steps


def _world(tmp_path, *, key: str) -> _CodeWorld:
    """The four-step shape of shipped ``code.fix-by-patch@2`` (facts → reproduce
    → apply-patch → verify, both root criteria on verify) on the real code
    domain, with the C3 seed.  The synthesizer path is the same harness P2.3m
    uses; the seed row itself is ``ALREADY_REGISTERED``."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    return _CodeWorld(
        evidence,
        method=_four_step("code.fix-by-patch.p23o"),
        key=key,
        db_name="orchestrator.db",
        success_criteria=(f"file:{REPORT}",),
        allowed_tools=TOOLS,
        workspace_seed=dict(SEED),
        max_attempts=12,
    )


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
    }


def _run(world: _CodeWorld, tmp_path, provider: RoleScriptedProvider) -> dict[str, Any]:
    evidence = Path(tmp_path) / "evidence"
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
            await asyncio.wait_for(loop.run(max_cycles=400), timeout=60)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            events = list(loop.store.list_events(mission.id))
            verify_id = _task_of(
                loop._hierarchical or world.dispatch, mission.id, "code.verify-tests"
            )
            attempts = list(loop.store.list_attempts(verify_id))
            intent_inputs: list[str] = []
            intent_raw: Any = None
            if attempts:
                intent = loop.store.get_intent_for_subject(attempts[0].id)
                if intent is not None:
                    intent_raw = intent.config.get("inputs")
                    intent_inputs = [item["path"] for item in (intent.config.get("inputs") or [])]
            layers = [
                item.payload
                for item in events
                if item.type == "VerificationLayerRecorded" and item.task_id == verify_id
            ]
            workspaces = [
                row
                for row in loop.store.list_workspaces()
                if str(row.get("attempt_id") or "").startswith(verify_id)
            ]
            return {
                "status": mission.status,
                "stop_reason": mission.stop_reason,
                "report": dict(mission.final_report or {}),
                "types": [item.type for item in events],
                "events": events,
                "conservation": _conservation(loop, mission.id),
                "verify_inputs": intent_inputs,
                "verify_intent_raw": intent_raw,
                "verify_layers": layers,
                "verify_attempts": len(attempts),
                "verify_id": verify_id,
                "workspaces": [
                    {
                        "id": row["workspace_id"],
                        "detail": row["detail"],
                    }
                    for row in workspaces
                ],
                "progress": list(loop.progress_log),
                "task_status": {
                    task.id: (str(task.status), task.goal)
                    for task in loop.store.list_tasks(mission.id)
                },
                "roles": dict(provider.by_role),
                "provider_calls": provider.calls,
            }

    return asyncio.run(case())


def test_fix_by_patch_verify_leaf_reaches_completed_on_the_prelaid_patch(tmp_path) -> None:
    """The C3 shape, end to end: patch accepted, verify workspace carries the
    patched source, ``rule_check`` / ``code_test`` pass, root review ACCEPT,
    Mission COMPLETED.  Before the fix this is the ``not a recorded workspace
    file`` failure."""

    world = _world(tmp_path, key="p23o-c3-e2e")
    worker = _C3Worker()
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 40,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 16,
            "root_reviewer": [_accepting_reviewer],
        }
    )
    outcome = _run(world, tmp_path, provider)
    problems = [
        problem
        for item in outcome["events"]
        if item.type == "VerificationFailed"
        for failure in item.payload.get("failures") or []
        for problem in (failure.get("detail") or {}).get("problems") or [failure.get("summary")]
    ]
    assert NOT_RECORDED not in problems, (
        problems,
        outcome["status"],
        outcome["stop_reason"],
        outcome["types"][-24:],
    )
    assert WINDOW in outcome["verify_inputs"], outcome["verify_inputs"]
    assert PATCH_DIFF in outcome["verify_inputs"], outcome["verify_inputs"]
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report'].get('detail')} "
        f"types={outcome['types'][-20:]}"
    )
    assert str(outcome["stop_reason"]) == str(MissionStopReason.VERIFICATION_PASSED)
    assert outcome["conservation"]["holds"] is True
    layer_names = {item.get("layer") for item in outcome["verify_layers"]}
    assert "rule_check" in layer_names and "code_test" in layer_names
    assert all(
        item.get("status") == "PASS"
        for item in outcome["verify_layers"]
        if item.get("layer") in {"rule_check", "code_test"}
    ), outcome["verify_layers"]


def test_inspect_at_2_still_receives_the_patch_port_after_overlay(tmp_path) -> None:
    """P2.3k N1 still holds: inspect@2 waits for, then receives, the patch port.
    Overlay adds seed files only when the producer recorded them; ``_accept``
    records the port document alone, so the manifest path is unchanged."""

    world = _CodeWorld(
        tmp_path,
        method=_rebound(_c1_method()),
        key="p23o-inspect-port",
        workspace_seed={WINDOW: SEED_WINDOW},
    )
    world.accept("code.read-repository-facts")
    world.accept("code.reproduce-failure")
    world.accept("code.apply-patch")
    assert world.inputs(INSPECT) == ["out/patch.json"]
