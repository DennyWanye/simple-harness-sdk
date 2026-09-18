# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3u: a read-only leaf cannot rewrite files it started from — at the tool.

Grok fifth-batch H-L3-{C1-r0, C1-r1, C2-r1}: every episode had a read-only leaf
(``side_effect_kind=external_read``, capability ``tests.run`` / ``repo.read``)
rewrite product source through ``workspace_write_file``.  P2.3k/P2.3m refused
the Attempt afterwards (``ResultRejected{read_only_leaf_rewrote_workspace}``),
which burned a whole Attempt (5–8 model calls) and pushed the method round.
The Worker prompt already said not to change existing files; the model did it
anyway.

The gateway now refuses that write *before* the file changes.  The Attempt
continues; the model can still write its report.  P2.3m's collector stays as
the fallback for anything that bypasses the tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_deployment_wiring import _task_of  # noqa: E402
from test_inspect_leaf_patch_input import _CodeWorld  # noqa: E402
from test_read_only_leaf_policy import SEED as FACTS_SEED  # noqa: E402
from test_read_only_leaf_policy import _collect_facts_leaf  # noqa: E402
from test_read_only_rewrite_bound import (  # noqa: E402
    NEW_COLLECTOR,
    PATCHED_COLLECTOR,
    SEED,
    SEED_COLLECTOR,
    TOOLS,
    _accepting_reviewer,
    _four_step,
    _write_and_envelope,
)

from agent_orchestrator.artifacts.workspace import WorkspaceManager  # noqa: E402
from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import MissionStopReason  # noqa: E402
from agent_orchestrator.governance.policies import effective_tools  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import mission_account  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.occurrence_tasks import (  # noqa: E402
    read_only_existing_paths,
)
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.runtime.tool_gateway import (  # noqa: E402
    MAX_READ_ONLY_EXISTING_REJECTIONS,
    WORKER_TOOLS,
    WorkspaceBinding,
    WorkspaceToolGateway,
)
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    critic_step,
)
from simple_harness.contracts import CallId  # noqa: E402
from simple_harness.tools import ToolCall  # noqa: E402

WINDOW = "stats/window.py"
REPORT = "REPORT.md"
COLLECTOR = "metrics/collector.py"
REWRITE = (
    "def window_sum(values, start, end):\n    return sum(values[start:end])\n"
)


def _write(gateway: WorkspaceToolGateway, run_id: str, path: str, content: str) -> Any:
    call = ToolCall(
        call_id=CallId(f"c-{path.replace('/', '-')}"),
        name="workspace_write_file",
        arguments={"path": path, "content": content},
    )
    return asyncio.run(gateway.execute(call, {"run_id": run_id}))


def _gateway(tmp_path: Path, *, existing: tuple[str, ...], seed: dict[str, str]):
    workspaces = WorkspaceManager(tmp_path / "ws")
    workspaces.create("attempt-1", seed=seed)
    gateway = WorkspaceToolGateway(workspaces)
    gateway.bind(
        "run-1",
        WorkspaceBinding(
            "attempt-1",
            "work",
            True,
            WORKER_TOOLS,
            read_only_existing=existing,
        ),
    )
    return gateway, workspaces


# ======================================================================================
# 1. Gateway: refuse existing files, allow new outputs, writers unaffected
# ======================================================================================


def test_a_read_only_leaf_cannot_rewrite_an_existing_workspace_file(tmp_path) -> None:
    """The write is refused at the tool; the seed bytes do not move; no Attempt
    is spent on a ResultRejected."""

    seed = {"metrics/collector.py": SEED_COLLECTOR}
    gateway, workspaces = _gateway(
        tmp_path, existing=("metrics/collector.py",), seed=seed
    )
    result = _write(gateway, "run-1", COLLECTOR, NEW_COLLECTOR)
    assert result.error_code == "read_only_existing_file", result
    message = result.public_message or ""
    assert "read-only" in message
    assert "declared" in message or "REPORT" in message
    workspace = workspaces.get("attempt-1", writable=False)
    assert workspace.read_text(COLLECTOR) == SEED_COLLECTOR
    assert gateway.calls[-1]["outcome"] == "rejected:read_only_existing_file"
    assert gateway.calls[-1]["stage"] == "policy"


def test_a_read_only_leaf_may_write_a_declared_new_output_file(tmp_path) -> None:
    gateway, workspaces = _gateway(
        tmp_path, existing=("metrics/collector.py",), seed={"metrics/collector.py": "x"}
    )
    result = _write(gateway, "run-1", REPORT, "# verify\npassed\n")
    assert result.error_code is None, result
    assert workspaces.get("attempt-1", writable=False).read_text(REPORT) == "# verify\npassed\n"


def test_a_writing_leaf_is_not_blocked_from_rewriting_existing_files(tmp_path) -> None:
    gateway, workspaces = _gateway(
        tmp_path, existing=(), seed={"metrics/collector.py": SEED_COLLECTOR}
    )
    result = _write(gateway, "run-1", COLLECTOR, PATCHED_COLLECTOR)
    assert result.error_code is None, result
    assert workspaces.get("attempt-1", writable=False).read_text(COLLECTOR) == PATCHED_COLLECTOR


# ======================================================================================
# 2. effective_tools: hide patch/apply class tools on a read-only leaf
# ======================================================================================


def test_effective_tools_hides_patch_apply_tools_on_a_read_only_leaf() -> None:
    """``workspace_write_file`` stays (the leaf writes its report with it).
    Patch/apply names are stripped so the exposure list matches the gateway."""

    from types import SimpleNamespace

    from agent_orchestrator.governance.policies import READ_ONLY_LEAF_HIDDEN_TOOLS

    role = (*WORKER_TOOLS, "apply_patch", "workspace_apply_patch")
    # Patch/apply names are not in TOOL_SCHEMAS, so a real DeploymentPolicy would
    # refuse them; the trim still has to drop them when a caller has them in the
    # four-way intersection (selftest / a future schema).
    deployment = SimpleNamespace(allowed_tools=role)
    writing = effective_tools(
        mission_tools=role, task_tools=role, role_tools=role, deployment=deployment
    )
    reading = effective_tools(
        mission_tools=role,
        task_tools=role,
        role_tools=role,
        deployment=deployment,
        read_only_leaf=True,
    )
    assert "workspace_write_file" in writing and "workspace_write_file" in reading
    assert "apply_patch" in writing and "workspace_apply_patch" in writing
    assert "apply_patch" not in reading and "workspace_apply_patch" not in reading
    assert READ_ONLY_LEAF_HIDDEN_TOOLS == frozenset(
        {"apply_patch", "workspace_apply_patch"}
    )
    assert writing == role
    assert set(writing) - set(reading) == READ_ONLY_LEAF_HIDDEN_TOOLS


# ======================================================================================
# 3. Collect path: the tool refusal does not become ResultRejected
# ======================================================================================


def test_a_read_only_leaf_that_tries_to_rewrite_via_the_tool_is_not_result_rejected(
    tmp_path,
) -> None:
    """C3's facts leaf, replayed at the tool: the write is refused, the seed is
    intact, collection never sees a rewrite, so there is no ResultRejected."""

    original = FACTS_SEED[WINDOW]
    outcome = _collect_facts_leaf(
        tmp_path,
        key="p23u-tool-block",
        writes=[
            (WINDOW, REWRITE),
            ("facts.json", '{"tests": ["tests/test_public_window.py"]}'),
        ],
        artifacts=["facts.json"],
    )
    assert [item["reason"] for item in outcome["rejections"]] == []
    assert outcome["submitted"] == ["ResultSubmitted"]
    assert WINDOW not in outcome["artifacts"]
    assert "facts.json" in outcome["artifacts"]
    evidence = Path(tmp_path) / "evidence"
    found = list(evidence.rglob(WINDOW))
    assert found, "the attempt workspace kept the seed path"
    assert any(path.read_text(encoding="utf-8") == original for path in found), [
        path.read_text(encoding="utf-8") for path in found
    ]


def test_a_direct_workspace_rewrite_is_still_refused_at_collection(tmp_path) -> None:
    """P2.3m fallback: bytes changed outside the gateway still hit the collector."""

    def mutate(loop: Orchestrator, intent: Any) -> None:
        workspace = loop.assembled.workspaces.get(str(intent.config["attempt_id"]))
        workspace.write_text(WINDOW, REWRITE)

    outcome = _collect_facts_leaf(
        tmp_path,
        key="p23u-fallback",
        writes=[("facts.json", '{"tests": ["tests/test_public_window.py"]}')],
        artifacts=[WINDOW, "facts.json"],
        mutate=mutate,
    )
    assert outcome["rejections"], outcome
    last = outcome["rejections"][-1]
    assert last["reason"] == "read_only_leaf_rewrote_workspace"
    assert last["detail"]["paths"] == [WINDOW]
    assert outcome["submitted"] == []


# ======================================================================================
# 4. True Orchestrator.run(): try rewrite → tool refuse → write report → COMPLETED
# ======================================================================================


class _TryRewriteThenReport:
    """Verify first tries to patch source, then writes only the report."""

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
                    (COLLECTOR, PATCHED_COLLECTOR),
                    ("applied.patch", "--- a/metrics/collector.py\n+++ b/metrics/collector.py\n"),
                    (REPORT, "# patch\nlocked collector.record\n"),
                ],
                [COLLECTOR, "applied.patch", REPORT],
                {"patch": "applied.patch"},
            )
        if "run the test suite" in goal:
            self.verify_attempts += 1
            return [
                ("workspace_write_file", {"path": COLLECTOR, "content": NEW_COLLECTOR}),
                *_write_and_envelope(
                    [(REPORT, "# verify\nvisible tests passed; do not rewrite source\n")],
                    [REPORT],
                    {"report": REPORT},
                ),
            ]
        raise AssertionError(f"unexpected leaf goal: {goal!r}")


def _world(tmp_path, *, key: str) -> _CodeWorld:
    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    return _CodeWorld(
        evidence,
        method=_four_step("code.fix-by-patch-then-verify.p23u"),
        key=key,
        db_name="orchestrator.db",
        allowed_tools=TOOLS,
        workspace_seed=SEED,
        success_criteria=(f"file:{REPORT}",),
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
            patch_id = _task_of(
                loop._hierarchical or world.dispatch, mission.id, "code.apply-patch"
            )
            verify_attempts = list(loop.store.list_attempts(verify_id))
            refused = [
                call
                for call in loop.assembled.gateway.calls
                if call.get("tool") == "workspace_write_file"
                and call.get("outcome") == "rejected:read_only_existing_file"
            ]
            collector_bytes = ""
            if verify_attempts:
                root = loop.assembled.workspaces.root / verify_attempts[0].id / COLLECTOR
                if root.is_file():
                    collector_bytes = root.read_text(encoding="utf-8")
            verify_records = []
            for attempt in verify_attempts:
                report_path = loop.assembled.workspaces.root / attempt.id / REPORT
                collector_path = loop.assembled.workspaces.root / attempt.id / COLLECTOR
                verify_records.append(
                    {
                        "id": attempt.id,
                        "retry_of": attempt.retry_of,
                        "status": str(attempt.status),
                        "failure": dict(attempt.failure or {}),
                        "report": (
                            report_path.read_text(encoding="utf-8")
                            if report_path.is_file()
                            else ""
                        ),
                        "collector": (
                            collector_path.read_text(encoding="utf-8")
                            if collector_path.is_file()
                            else ""
                        ),
                    }
                )
            return {
                "status": mission.status,
                "stop_reason": mission.stop_reason,
                "report": dict(mission.final_report or {}),
                "types": [item.type for item in events],
                "events": events,
                "conservation": _conservation(loop, mission.id),
                "verify_attempts": len(verify_attempts),
                "verify_id": verify_id,
                "patch_id": patch_id,
                "refused_writes": refused,
                "collector_bytes": collector_bytes,
                "verify_records": verify_records,
                "gateway": list(loop.assembled.gateway.calls),
                "roles": dict(provider.by_role),
            }

    return asyncio.run(case())


def test_a_verify_leaf_that_tries_to_rewrite_source_then_writes_a_report_completes(
    tmp_path,
) -> None:
    """The fifth-batch shape: verify calls write on product source, the tool
    refuses, the Worker writes REPORT.md instead, the leaf is accepted, the
    Mission completes, and ``read_only_leaf_rewrote_workspace`` never fires."""

    world = _world(tmp_path, key="p23u-e2e")
    worker = _TryRewriteThenReport()
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 40,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 16,
            "root_reviewer": [_accepting_reviewer],
        }
    )
    outcome = _run(world, tmp_path, provider)
    reasons = [
        item.payload.get("reason")
        for item in outcome["events"]
        if item.type == "ResultRejected"
    ]
    assert "read_only_leaf_rewrote_workspace" not in reasons, (
        reasons,
        outcome["types"],
        outcome["status"],
        outcome["stop_reason"],
    )
    assert outcome["refused_writes"], (
        "the verify leaf must have been refused at the write tool",
        outcome["types"][-24:],
        outcome["status"],
    )
    assert NEW_COLLECTOR not in outcome["collector_bytes"]
    assert outcome["collector_bytes"] in {PATCHED_COLLECTOR, SEED_COLLECTOR, ""}
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['report'].get('detail')} "
        f"types={outcome['types'][-24:]}"
    )
    assert str(outcome["stop_reason"]) == str(MissionStopReason.VERIFICATION_PASSED)
    assert outcome["conservation"]["holds"] is True
    assert outcome["verify_attempts"] == 1, outcome["verify_attempts"]


# ======================================================================================
# 5. Prompt: a new hierarchical Worker version; v2 bytes stay frozen
# ======================================================================================


def test_the_hierarchical_worker_v4_forbids_rewriting_and_v2_is_frozen() -> None:
    from agent_orchestrator.runtime.role_templates import (
        WORKER_HIERARCHICAL,
        WORKER_HIERARCHICAL_V2,
        WORKER_HIERARCHICAL_V2_VERSION,
        WORKER_HIERARCHICAL_VERSION,
        template_for,
    )

    assert WORKER_HIERARCHICAL.prompt_version == WORKER_HIERARCHICAL_VERSION
    assert WORKER_HIERARCHICAL_VERSION == "worker-hierarchical-v4"
    v4 = WORKER_HIERARCHICAL.instructions
    v2 = WORKER_HIERARCHICAL_V2.instructions
    for sentence in ("不能改已有文件", "报告里写明建议"):
        assert sentence in v4, sentence
        assert sentence not in v2, sentence
    assert WORKER_HIERARCHICAL_V2.prompt_version == WORKER_HIERARCHICAL_V2_VERSION
    assert hashlib.sha256(v2.encode("utf-8")).hexdigest() == (
        "120372b8a49162ab1d96c6cf2725d6fcf7adec1988c7c8646f378c21649870b7"
    )
    assert (
        template_for(WORKER_HIERARCHICAL, {"worker": WORKER_HIERARCHICAL_V2_VERSION})
        is WORKER_HIERARCHICAL_V2
    )


# ======================================================================================
# 6. Legacy: the default binding still writes existing files
# ======================================================================================


def test_a_legacy_binding_without_read_only_existing_still_writes(tmp_path) -> None:
    """``read_only_existing`` defaults empty; DAG-mode binds never set it."""

    gateway, workspaces = _gateway(
        tmp_path, existing=(), seed={"a.md": "old\n"}
    )
    result = _write(gateway, "run-1", "a.md", "new\n")
    assert result.error_code is None, result
    assert workspaces.get("attempt-1", writable=False).read_text("a.md") == "new\n"


# ======================================================================================
# 7. P2.3u verification P1-1: retry snapshot is seed ∪ overlay, not the copied tree
# ======================================================================================


def test_read_only_existing_paths_is_seed_union_overlay_not_retry_outputs() -> None:
    """The gateway snapshot is the same set ``read_only_rewrites`` uses as ``initial``."""

    paths = read_only_existing_paths(
        {"metrics/collector.py": SEED_COLLECTOR, "tests/t.py": "x"},
        ("metrics/collector.py", "applied.patch"),
        (),
    )
    assert "REPORT.md" not in paths
    assert "metrics/collector.py" in paths
    assert "applied.patch" in paths
    assert "tests/t.py" in paths


def test_a_retry_workspace_may_rewrite_its_own_report_but_not_seed(tmp_path) -> None:
    """Verification reproduction: previous tree has seed + REPORT.md; retry create
    copies both; the snapshot must still let the leaf rewrite REPORT.md."""

    workspaces = WorkspaceManager(tmp_path / "ws")
    first = workspaces.create("attempt-1", seed={COLLECTOR: SEED_COLLECTOR})
    first.write_text(REPORT, "# round 1\n")
    workspaces.create(
        "attempt-2", seed={COLLECTOR: SEED_COLLECTOR}, previous=first.root
    )
    existing = read_only_existing_paths({COLLECTOR: SEED_COLLECTOR}, (), ())
    gateway = WorkspaceToolGateway(workspaces)
    gateway.bind(
        "run-2",
        WorkspaceBinding(
            "attempt-2",
            "work",
            True,
            WORKER_TOOLS,
            read_only_existing=existing,
        ),
    )
    rewritten = _write(gateway, "run-2", REPORT, "# round 2\n")
    assert rewritten.error_code is None, rewritten
    assert workspaces.get("attempt-2", writable=False).read_text(REPORT) == "# round 2\n"
    blocked = _write(gateway, "run-2", COLLECTOR, NEW_COLLECTOR)
    assert blocked.error_code == "read_only_existing_file", blocked
    assert workspaces.get("attempt-2", writable=False).read_text(COLLECTOR) == SEED_COLLECTOR


class _RewriteReportOnRetry:
    """First verify writes REPORT.md; retry rewrites it and tries to patch source."""

    def __init__(self) -> None:
        self._queues: dict[str, list[Any]] = {}
        self.verify_attempts = 0

    def __call__(self, request: Any) -> Any:
        from agent_orchestrator.testing.fixtures import package_of as _package_of

        package = _package_of(request)
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
                    (COLLECTOR, PATCHED_COLLECTOR),
                    ("applied.patch", "--- a/metrics/collector.py\n+++ b/metrics/collector.py\n"),
                    (REPORT, "# patch\nlocked collector.record\n"),
                ],
                [COLLECTOR, "applied.patch", REPORT],
                {"patch": "applied.patch"},
            )
        if "run the test suite" in goal:
            self.verify_attempts += 1
            report = f"# verify round {self.verify_attempts}\n"
            boom = "tests/test_boom.py"
            if self.verify_attempts == 1:
                return _write_and_envelope(
                    [
                        (REPORT, report),
                        (boom, "def test_boom():\n    assert False\n"),
                    ],
                    [REPORT],
                    {"report": REPORT},
                )
            return [
                ("workspace_write_file", {"path": COLLECTOR, "content": NEW_COLLECTOR}),
                *_write_and_envelope(
                    [
                        (REPORT, report),
                        (boom, "def test_boom():\n    assert True\n"),
                    ],
                    [REPORT],
                    {"report": REPORT},
                ),
            ]
        raise AssertionError(f"unexpected leaf goal: {goal!r}")


def test_a_read_only_retry_after_verification_failed_may_rewrite_its_report(
    tmp_path,
) -> None:
    """P1-1: first verify writes REPORT.md then fails code_test (verification_failed);
    retry still rewrites REPORT.md and still cannot rewrite seed/overlay source."""

    world = _world(tmp_path, key="p23u-retry-report")
    worker = _RewriteReportOnRetry()
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 40,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 16,
            "root_reviewer": [_accepting_reviewer],
        }
    )
    outcome = _run(world, tmp_path, provider)
    records = outcome["verify_records"]
    assert records[1]["retry_of"] == records[0]["id"], records
    assert "# verify round 2" in records[1]["report"], records[1]["report"]
    assert NEW_COLLECTOR not in records[1]["collector"]
    assert records[1]["collector"] in {PATCHED_COLLECTOR, SEED_COLLECTOR, ""}
    report_refused = [
        call
        for call in outcome["refused_writes"]
        if str((call.get("arguments") or {}).get("path") or "").endswith("REPORT.md")
        or str((call.get("arguments") or {}).get("path")) == REPORT
    ]
    assert report_refused == [], report_refused
    collector_refused = [
        call
        for call in outcome["refused_writes"]
        if COLLECTOR in str((call.get("arguments") or {}).get("path") or "")
    ]
    assert collector_refused, (
        "retry must still be refused when rewriting overlay/seed source",
        outcome["refused_writes"],
    )


# ======================================================================================
# 8. P2-2: consecutive read_only_existing_file refusals are bounded
# ======================================================================================


def test_consecutive_read_only_existing_refusals_are_capped(tmp_path) -> None:
    """Three consecutive existing-file writes end the streak with a named reason."""

    assert MAX_READ_ONLY_EXISTING_REJECTIONS == 3
    seed = {COLLECTOR: SEED_COLLECTOR}
    gateway, workspaces = _gateway(tmp_path, existing=(COLLECTOR,), seed=seed)
    codes = [
        _write(gateway, "run-1", COLLECTOR, NEW_COLLECTOR + f"# {index}\n").error_code
        for index in range(MAX_READ_ONLY_EXISTING_REJECTIONS + 1)
    ]
    assert codes[: MAX_READ_ONLY_EXISTING_REJECTIONS - 1] == [
        "read_only_existing_file"
    ] * (MAX_READ_ONLY_EXISTING_REJECTIONS - 1)
    assert codes[MAX_READ_ONLY_EXISTING_REJECTIONS - 1] == "read_only_leaf_kept_writing"
    assert codes[-1] == "read_only_leaf_kept_writing"
    assert workspaces.get("attempt-1", writable=False).read_text(COLLECTOR) == SEED_COLLECTOR


def test_a_new_file_write_resets_the_read_only_existing_streak(tmp_path) -> None:
    seed = {COLLECTOR: SEED_COLLECTOR}
    gateway, _workspaces = _gateway(tmp_path, existing=(COLLECTOR,), seed=seed)
    assert _write(gateway, "run-1", COLLECTOR, NEW_COLLECTOR).error_code == (
        "read_only_existing_file"
    )
    assert _write(gateway, "run-1", COLLECTOR, NEW_COLLECTOR).error_code == (
        "read_only_existing_file"
    )
    assert _write(gateway, "run-1", REPORT, "# notes\n").error_code is None
    third = _write(gateway, "run-1", COLLECTOR, NEW_COLLECTOR)
    assert third.error_code == "read_only_existing_file", third


# ======================================================================================
# 9. P2-3: a failed snapshot refuses every write (fail-closed)
# ======================================================================================


def test_a_blocked_read_only_snapshot_refuses_every_write(tmp_path) -> None:
    seed = {COLLECTOR: SEED_COLLECTOR}
    gateway, workspaces = _gateway(tmp_path, existing=(COLLECTOR,), seed=seed)
    gateway.bind(
        "run-1",
        WorkspaceBinding(
            "attempt-1",
            "work",
            True,
            WORKER_TOOLS,
            read_only_existing=(COLLECTOR,),
            read_only_writes_blocked=True,
        ),
    )
    report = _write(gateway, "run-1", REPORT, "# verify\n")
    source = _write(gateway, "run-1", COLLECTOR, NEW_COLLECTOR)
    assert report.error_code == "read_only_snapshot_unavailable", report
    assert source.error_code == "read_only_snapshot_unavailable", source
    assert workspaces.get("attempt-1", writable=False).read_text(COLLECTOR) == SEED_COLLECTOR
    assert not (workspaces.root / "attempt-1" / REPORT).exists()


def test_a_workspace_error_while_snapshotting_blocks_writes_on_the_leaf(
    tmp_path, monkeypatch
) -> None:
    from agent_orchestrator.artifacts.workspace import WorkspaceError

    real = Orchestrator._read_only_initial
    calls = {"n": 0}

    def boom(self: Orchestrator, attempt: Any) -> dict[str, str]:
        calls["n"] += 1
        # `_dispatch` binds twice (CLAIMED then AGENT_CREATED); collect uses the
        # same helper afterwards and must still see seed ∪ overlay hashes.
        if calls["n"] <= 2:
            raise WorkspaceError("snapshot failed")
        return real(self, attempt)

    monkeypatch.setattr(Orchestrator, "_read_only_initial", boom)
    outcome = _collect_facts_leaf(
        tmp_path,
        key="p23u-snapshot-fail",
        writes=[
            (WINDOW, REWRITE),
            ("facts.json", '{"tests": ["tests/test_public_window.py"]}'),
        ],
        artifacts=["facts.json"],
    )
    blocked = [
        call
        for call in outcome["gateway"]
        if call.get("tool") == "workspace_write_file"
        and "read_only_snapshot_unavailable" in str(call.get("outcome") or "")
    ]
    assert blocked, outcome["gateway"]
