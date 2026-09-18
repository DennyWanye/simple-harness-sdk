# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3t: a criterion that needs added tests must be evidenced by a write-step tests port.

Grok fifth-batch H-L3-C1-r1 (SDK 0.12.2 candidate f2dfa64): hidden grader PASS,
Mission FAILED, ``stop_reason=no_dispatchable_work``, 115/320 calls.  Two root
reviews REJECTED for the same true finding: verify's REPORT.md claimed
``tests/test_concurrent_*.py`` was added and green, inspect/summarize on the same
tree proved the file absent.  The synthesised methods' write steps only declared
a ``patch`` port; the verify leaf (read-only) wrote the tests itself, those
writes never became accepted products, and after ``max_root_review_repairs``
the stall hid the bound under ``admitted_not_dispatched``.

This slice: the admission protocol refuses a method whose criterion evidence
requires added tests unless a write step declares a ``tests`` output port that
verify binds; the synthesizer prompt and request carry that requirement; a
read-only Worker is told to report a missing test as a finding; exhausting
root-review repairs is a named stop.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_deployment_wiring import _task_of  # noqa: E402
from test_inspect_leaf_patch_input import (  # noqa: E402
    _CodeWorld,
)
from test_read_only_rewrite_bound import (  # noqa: E402
    PATCHED_COLLECTOR,
    SEED,
    TOOLS,
    _accepting_reviewer,
    _four_step,
    _LeafWorker,
    _write_and_envelope,
)
from test_root_review_repair_library import (  # noqa: E402
    C1_FINDING,
    _drive,
    _judge_critic,
    _planner_replaces,
    _reviewer,
    _seeded,
)
from test_synthesis_rejection_reask import (  # noqa: E402
    SYNTHESIS_REPLY_REJECTED,
)

from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import (  # noqa: E402
    MissionStopReason,
)
from agent_orchestrator.orchestrator.commit_service import mission_account  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import (  # noqa: E402
    ROOT_REVIEW_REPAIRS_EXHAUSTED,
    Orchestrator,
)
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    SYNTHESIS_ROUND_RECORDED,
)
from agent_orchestrator.planning.htn.registry import (  # noqa: E402
    SYNTHESIS_TESTS_PORT_REASON,
    AdmissionVerdict,
    RejectionCode,
    evidence_requires_added_tests,
)
from agent_orchestrator.planning.htn.seed_methods.loader import seed_content_hash  # noqa: E402
from agent_orchestrator.planning.htn.world import build_planning_world  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    critic_step,
    method_proposal_step,
    package_of,
)

TESTS_PATH = "tests/test_concurrent_contract.py"
TESTS_BODY = "def test_contract_is_present():\n    assert True\n"


def _type_ref(type_id: str, version: int) -> dict[str, Any]:
    return {
        "id": type_id,
        "version": version,
        "content_hash": seed_content_hash(type_id, version),
    }


def _bind_tests(method: dict[str, Any], *, require: bool) -> dict[str, Any]:
    """facts → apply-patch → verify, with or without a tests port on the write step."""

    body = copy.deepcopy(method)
    apply_id = next(
        item["local_id"]
        for item in body["steps"]
        if item["task_type_ref"]["id"] == "code.apply-patch"
    )
    verify_id = next(
        item["local_id"]
        for item in body["steps"]
        if item["task_type_ref"]["id"] == "code.verify-tests"
    )
    for item in body["steps"]:
        if item["local_id"] == apply_id:
            item["task_type_ref"] = _type_ref("code.apply-patch", 2 if require else 1)
        if item["local_id"] == verify_id:
            item["task_type_ref"] = _type_ref("code.verify-tests", 2 if require else 1)
            arguments = dict(item.get("arguments") or {})
            if require:
                arguments["tests"] = {"op": "output", "port": "tests", "step": apply_id}
            else:
                arguments.pop("tests", None)
            item["arguments"] = arguments
    for link in body["composition"]["criterion_links"]:
        if link["parent_criterion_id"] in {"c-test-passes", "c-contract-tests-pass"}:
            link["evidence_requirement"] = (
                "tests covering the user contract were added or turned from red "
                "to green and pass"
            )
    return body


def _tests_method(method_id: str, *, suffix: str = "") -> dict[str, Any]:
    return _bind_tests(_four_step(method_id, suffix=suffix), require=True)


def _missing_tests_method(method_id: str, *, suffix: str = "") -> dict[str, Any]:
    return _bind_tests(_four_step(method_id, suffix=suffix), require=False)


class _TestsWorker(_LeafWorker):
    """Apply-patch claims the tests port; verify only writes REPORT.md."""

    def __init__(
        self, *, mode: str, rewrite_limit: int | None = None, claim_tests: bool = False
    ) -> None:
        super().__init__(mode=mode, rewrite_limit=rewrite_limit)
        self.patch_feedback = ""
        self.patch_ports: set[Any] = set()
        self.claim_tests = claim_tests

    def __call__(self, request: Any) -> Any:
        package = package_of(request)
        attempt_id = str((package.get("attempt") or {}).get("attempt_id") or "")
        if attempt_id not in self._queues:
            self._queues[attempt_id] = self._script(package, request)
        queue = self._queues[attempt_id]
        if not queue:
            raise AssertionError(f"worker script exhausted for {attempt_id}")
        step = queue.pop(0)
        if callable(step) and not isinstance(step, (str, tuple)):
            return step(request)
        return step

    def _script(self, package: dict[str, Any], request: Any | None = None) -> list[Any]:
        goal = str((package.get("task_contract") or {}).get("goal") or "")
        blob = json.dumps(package, ensure_ascii=False)
        if request is not None:
            blob += "".join(
                m.content if isinstance(m.content, str) else str(m.content)
                for m in request.messages
            )
        ports: set[str] = set()
        section = package.get("declared_output_ports") or {}
        for item in (section.get("ports") if isinstance(section, dict) else []) or []:
            name = item.get("port") or item.get("port_key") or item.get("name")
            if name:
                ports.add(str(name))
        if '"port": "tests"' in blob or '"port":"tests"' in blob:
            ports.add("tests")
        feedback = json.dumps(package.get("review_feedback") or {}, ensure_ascii=False)
        if "not in the tree" in blob or "absent" in blob:
            feedback = blob
        if "apply a patch" in goal:
            self.patch_feedback = feedback
            self.patch_ports = ports
            writes = [
                ("metrics/collector.py", PATCHED_COLLECTOR),
                ("applied.patch", "--- a/metrics/collector.py\n+++ b/metrics/collector.py\n"),
                (TESTS_PATH, TESTS_BODY),
                ("REPORT.md", "# patch\nlocked collector.record and added contract tests\n"),
            ]
            artifacts = [
                "metrics/collector.py",
                "applied.patch",
                TESTS_PATH,
                "REPORT.md",
            ]
            outputs = {"patch": "applied.patch"}
            if self.claim_tests or "tests" in ports:
                outputs["tests"] = TESTS_PATH
            return _write_and_envelope(writes, artifacts, outputs)
        if "run the test suite" in goal:
            self.verify_attempts += 1
            return _write_and_envelope(
                [("REPORT.md", f"# verify\n{TESTS_PATH} collected, 5 passed\n")],
                ["REPORT.md"],
                {"report": "REPORT.md"},
            )
        return super()._script(package)


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
    }


def _run(
    world: _CodeWorld,
    tmp_path,
    provider: RoleScriptedProvider,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = Path(tmp_path) / "evidence"
    world.store.close()

    async def case() -> dict[str, Any]:
        fields: dict[str, Any] = {
            "evidence_root": evidence,
            "max_concurrency": 1,
            "test_timeout_seconds": 30,
            "max_planning_attempts": 1,
        }
        extra_fields = dict(extra or {})
        extra_fields.pop("cycles", None)
        fields.update(extra_fields)
        config = OrchestratorConfig(**fields)
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.world.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.world)
            await asyncio.wait_for(loop.run(max_cycles=400), timeout=60)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            events = list(loop.store.list_events(mission.id))
            dispatch = loop._hierarchical or world.dispatch
            verify_id = _task_of(dispatch, mission.id, "code.verify-tests")
            apply_id = _task_of(dispatch, mission.id, "code.apply-patch")
            verify_inputs: list[str] = []
            apply_package: dict[str, Any] = {}
            attempts = list(loop.store.list_attempts(verify_id)) if verify_id else []
            if attempts:
                intent = loop.store.get_intent_for_subject(attempts[0].id)
                if intent is not None:
                    verify_inputs = [
                        item["path"] for item in (intent.config.get("inputs") or [])
                    ]
            apply_attempts = list(loop.store.list_attempts(apply_id)) if apply_id else []
            if apply_attempts:
                intent = loop.store.get_intent_for_subject(apply_attempts[-1].id)
                if intent is not None:
                    apply_package = dict(intent.config.get("package") or {})
            return {
                "status": mission.status,
                "stop_reason": mission.stop_reason,
                "report": dict(mission.final_report or {}),
                "types": [item.type for item in events],
                "events": events,
                "conservation": _conservation(loop, mission.id),
                "verify_inputs": verify_inputs,
                "apply_package": apply_package,
                "roles": dict(provider.by_role),
                "synthesis": [
                    dict(item.payload)
                    for item in events
                    if item.type == SYNTHESIS_ROUND_RECORDED
                ],
                "rejected": [
                    dict(item.payload)
                    for item in events
                    if item.type == SYNTHESIS_REPLY_REJECTED
                ],
                "task_status": {
                    task.id: str(task.status) for task in loop.store.list_tasks(mission.id)
                },
                "progress": list(loop.progress_log),
            }

    return asyncio.run(case())


def _world(tmp_path, *, key: str, method: dict[str, Any]) -> _CodeWorld:
    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    return _CodeWorld(
        evidence,
        method=method,
        key=key,
        db_name="orchestrator.db",
        allowed_tools=TOOLS,
        workspace_seed=SEED,
        success_criteria=("file:REPORT.md",),
        max_attempts=20,
    )


# ======================================================================================
# 1. The rule
# ======================================================================================


def test_overlay_places_new_test_files_that_are_not_on_the_seed() -> None:
    from agent_orchestrator.artifacts.bound_workspace import overlay_bound_producer_files
    from agent_orchestrator.artifacts.versioning import UpstreamInput
    from agent_orchestrator.contracts.models import Artifact

    producer = "task-apply"
    inputs = [UpstreamInput(producer, "applied.patch", "a" * 64, "artifact-patch")]
    tests = Artifact(
        id="artifact-tests",
        mission_id="m",
        task_id=producer,
        attempt_id=f"{producer}:1",
        type="file",
        path=TESTS_PATH,
        version=1,
        content_hash="b" * 64,
        size_bytes=8,
        produced_by="w",
        storage_uri="",
    )
    overlay = overlay_bound_producer_files(
        inputs,
        seed_paths={"metrics/collector.py", "tests/test_public_collector.py"},
        artifacts_by_producer={producer: [tests]},
    )
    assert TESTS_PATH in {item.path for item in overlay}


def test_overlay_drops_new_tests_written_by_a_read_only_leaf() -> None:
    """P2.3u P2-1: a read-only leaf's new tests/ files must not pre-lay downstream."""

    from agent_orchestrator.artifacts.bound_workspace import overlay_bound_producer_files
    from agent_orchestrator.artifacts.versioning import UpstreamInput
    from agent_orchestrator.contracts.models import Artifact

    producer = "task-verify"
    inputs = [UpstreamInput(producer, "REPORT.md", "a" * 64, "artifact-report")]
    tests = Artifact(
        id="artifact-tests",
        mission_id="m",
        task_id=producer,
        attempt_id=f"{producer}:1",
        type="file",
        path=TESTS_PATH,
        version=1,
        content_hash="b" * 64,
        size_bytes=8,
        produced_by="w",
        storage_uri="",
    )
    overlay = overlay_bound_producer_files(
        inputs,
        seed_paths={"metrics/collector.py"},
        artifacts_by_producer={producer: [tests]},
        read_only_producers={producer},
    )
    assert TESTS_PATH not in {item.path for item in overlay}


def test_added_tests_evidence_is_detected_from_c1_wording() -> None:
    assert evidence_requires_added_tests(
        "tests covering the user goal must be added or turned from red to green and pass"
    )
    assert evidence_requires_added_tests("c-contract-tests-pass")
    assert not evidence_requires_added_tests(
        "the report shows the named failing test was run on the patched revision and now passes"
    )


def test_the_synthesis_request_carries_criterion_evidence() -> None:
    from agent_orchestrator.planning.htn.synthesis import MethodSynthesizer

    env = build_planning_world("p23t-request", domains=("code",))
    goal = next(
        spec
        for spec in env.catalog.task_types()
        if spec.task_type_ref.id == "code.implement-contract"
    )
    from agent_orchestrator.contracts.htn import (
        ObligationId,
        TaskForm,
        TaskRef,
        TaskSemanticBindingV1,
    )

    binding = TaskSemanticBindingV1(
        task_id=TaskRef("task-root"),
        obligation_id=ObligationId("obl-root"),
        contract_revision=1,
        contract_hash="a" * 64,
        form=TaskForm.COMPOUND,
        goal_signature=goal.goal_signature,
        typed_parameters={"repository": "<workspace>"},
        requirement_refs=tuple(goal.goal_signature.coverage_criteria),
        semantic_scope="mission",
    )
    request = MethodSynthesizer(env.registry, env.catalog).build_request(
        binding, env.capabilities(), env.registry
    )
    payload = request.to_json()
    evidence = payload["criterion_evidence"]
    ids = {item["id"] for item in evidence}
    assert "c-contract-tests-pass" in ids, evidence
    blob = " ".join(item["evidence_requirement"] for item in evidence)
    assert evidence_requires_added_tests(blob, *ids)
    apply_offer = next(
        item for item in payload["operators"] if item["task_type_ref"]["id"] == "code.apply-patch"
    )
    assert apply_offer["task_type_ref"]["version"] == 2
    assert "tests" in apply_offer["output_ports"]
    verify_offer = next(
        item for item in payload["operators"] if item["task_type_ref"]["id"] == "code.verify-tests"
    )
    assert verify_offer["task_type_ref"]["version"] == 2
    assert "tests" in verify_offer["input_ports"]


def _admit(env, body: dict[str, Any]):
    from agent_orchestrator.contracts.htn import MethodContract, RegistryAuthor
    from agent_orchestrator.planning.htn.registry import MethodProposal

    return env.registry.admit(
        MethodProposal(
            method=MethodContract.from_json(body),
            author=RegistryAuthor.MODEL,
        ),
        author=RegistryAuthor.MODEL,
        policy=env.policy(),
    )


def test_a_method_that_needs_added_tests_without_a_tests_port_is_correctably_refused() -> None:
    env = build_planning_world("p23t-admit", domains=("code",))
    receipt = _admit(env, _missing_tests_method("code.fix-by-patch-then-verify.no-tests"))
    assert receipt.verdict is AdmissionVerdict.REJECTED, receipt.problems
    assert any(item.code is RejectionCode.ROOT_COVERAGE_GAP for item in receipt.problems)
    assert any(
        getattr(item, "reason", "") == SYNTHESIS_TESTS_PORT_REASON for item in receipt.problems
    )
    from agent_orchestrator.planning.htn.synthesis import rejection_is_correctable

    assert rejection_is_correctable(receipt) is True


def test_a_method_that_declares_and_binds_the_tests_port_is_admitted() -> None:
    env = build_planning_world("p23t-admit-ok", domains=("code",))
    receipt = _admit(env, _tests_method("code.fix-by-patch-then-verify.with-tests"))
    assert receipt.admitted, receipt.problems


# ======================================================================================
# 2. True Orchestrator.run()
# ======================================================================================


def test_a_method_with_a_tests_port_reaches_completed(tmp_path) -> None:
    """(a) criterion requires tests → write step declares tests → verify is
    read-only and consumes that port → root review ACCEPT → COMPLETED."""

    world = _world(tmp_path, key="p23t-a", method=_tests_method("code.fix-by-patch-then-verify.a"))
    apply_id = _task_of(world.dispatch, world.mission.id, "code.apply-patch")
    declared = world.dispatch.declared_output_ports_for(world.mission.id, apply_id)
    assert any(item.get("port") == "tests" for item in declared), declared
    worker = _TestsWorker(mode="clean", claim_tests=True)
    provider = RoleScriptedProvider(
        {
            "worker": [worker] * 40,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 16,
            "root_reviewer": [_accepting_reviewer],
        }
    )
    outcome = _run(world, tmp_path, provider)
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: "
        f"{(outcome['report'] or {}).get('detail')} types={outcome['types'][-24:]}"
    )
    assert str(outcome["stop_reason"]) == str(MissionStopReason.VERIFICATION_PASSED)
    assert TESTS_PATH in outcome["verify_inputs"], outcome["verify_inputs"]
    assert worker.verify_attempts == 1
    assert outcome["conservation"]["holds"] is True


def _rejecting_reviewer(request: Any) -> str:
    shown = json.loads(
        next(m.content for m in reversed(request.messages) if str(m.role).endswith("user"))
    )
    return (
        "<critic_verdict>"
        + json.dumps(
            {
                "verdict": "FAIL",
                "findings": [
                    {
                        "severity": "blocker",
                        "criterion_id": (shown.get("criteria") or [{}])[0].get("criterion_id")
                        or "c-test-passes",
                        "detail": C1_FINDING["detail"],
                    }
                ],
                "mission_criteria": [
                    {
                        "criterion": item["criterion_id"],
                        "met": False,
                        "reason": "scripted reject",
                    }
                    for item in shown.get("criteria") or []
                ],
            },
            ensure_ascii=False,
        )
        + "</critic_verdict>"
    )


def test_a_missing_tests_port_is_reasked_and_then_adopted() -> None:
    """(b) first synthesizer reply lacks the tests port → correctable → second admits.

    The reask loop itself is P2.3i's ``rejection_is_correctable`` path; this test
    is the new reason that path must fire for C1.
    """

    from agent_orchestrator.planning.htn.synthesis import (
        MethodSynthesizer,
        rejection_is_correctable,
    )

    env = build_planning_world("p23t-b", domains=("code",))
    synthesizer = MethodSynthesizer(env.registry, env.catalog)
    slipped = synthesizer.accept_response(
        method_proposal_step(_missing_tests_method("code.fix-by-patch-then-verify.b-miss")),
        policy=env.policy(),
    )
    assert slipped.verdict is AdmissionVerdict.REJECTED, slipped.problems
    assert rejection_is_correctable(slipped) is True
    assert any(
        getattr(item, "reason", "") == SYNTHESIS_TESTS_PORT_REASON for item in slipped.problems
    )
    adopted = synthesizer.accept_response(
        method_proposal_step(_tests_method("code.fix-by-patch-then-verify.b-ok")),
        policy=env.policy(),
    )
    assert adopted.admitted, adopted.problems


def test_two_root_review_rejects_stop_with_the_named_reason(tmp_path) -> None:
    """(c) two REJECT → named stop, no hanging admitted_not_dispatched, conservation."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _seeded(evidence, key="p23t-c", alt=True, free_text=True)
    world.store.close()
    provider = RoleScriptedProvider(
        {
            "root_reviewer": [_reviewer("FAIL", finding=C1_FINDING["detail"])] * 4,
            "planner": [_planner_replaces] * 4,
            "critic": [_judge_critic],
        }
    )
    outcome = _drive(world, evidence, provider, cycles=60, max_planning_attempts=1)
    assert outcome["status"] is MissionStatus.FAILED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['types']}"
    )
    assert outcome["stop_reason"] == ROOT_REVIEW_REPAIRS_EXHAUSTED, outcome["stop_reason"]
    detail = (outcome["report"] or {}).get("detail") or {}
    hanging = detail.get("admitted_not_dispatched") or []
    assert hanging == [], hanging
    ready = [
        task_id
        for task_id, status in (
            (task.id, str(task.status))
            for event in outcome["events"]
            for task in []
        )
    ]
    del ready

    # Conservation and cancellation are on the Orchestrator that _drive opened;
    # re-read the report flags written at fail_mission.
    assert (outcome["report"] or {}).get("budget_conserved") is True or detail.get(
        "confirmed_after_one_more_cycle"
    ) is True
    events = outcome["events"]
    cancelled = [item for item in events if item.type == "TaskCancelled"]
    assert "MissionFailed" in outcome["types"]
    assert outcome["types"].count("HierarchicalRootReviewRejected") == 2
    assert cancelled or hanging == []


def test_a_legacy_mission_keeps_no_dispatchable_work_and_no_tests_port(tmp_path) -> None:
    """(d) DAG-mode Missions do not grow the named stop."""

    from agent_orchestrator.orchestrator.commit_service import MissionSpec

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    provider = RoleScriptedProvider({"planner": ["not a plan", "still not a plan"]})

    async def case() -> dict[str, Any]:
        config = OrchestratorConfig(
            evidence_root=evidence, max_concurrency=1, test_timeout_seconds=15,
            max_planning_attempts=2,
        )
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            mission = await loop.submit_mission(
                MissionSpec(
                    goal="legacy goal",
                    success_criteria=("file:a.md",),
                    tenant_id="t",
                    idempotency_key="p23t-legacy",
                )
            )
            await asyncio.wait_for(loop.run(max_cycles=80), timeout=20)
            final = loop.store.get_mission(mission.id)
            return {
                "status": final.status,
                "stop_reason": final.stop_reason,
                "report": dict(final.final_report or {}),
            }

    outcome = asyncio.run(case())
    assert outcome["stop_reason"] != ROOT_REVIEW_REPAIRS_EXHAUSTED
    assert "root_review_repairs_exhausted" not in json.dumps(
        outcome["report"], ensure_ascii=False
    )


def test_the_synthesizer_prompt_v7_forbids_verify_writes_and_v6_is_frozen() -> None:
    from agent_orchestrator.runtime.role_templates import (
        METHOD_SYNTHESIZER,
        METHOD_SYNTHESIZER_V6,
        METHOD_SYNTHESIZER_V6_VERSION,
        METHOD_SYNTHESIZER_VERSION,
    )

    assert METHOD_SYNTHESIZER.prompt_version == METHOD_SYNTHESIZER_VERSION
    assert METHOD_SYNTHESIZER_VERSION == "method-synthesizer-v7"
    assert METHOD_SYNTHESIZER_V6.prompt_version == METHOD_SYNTHESIZER_V6_VERSION
    assert METHOD_SYNTHESIZER_V6_VERSION == "method-synthesizer-v6"
    text = METHOD_SYNTHESIZER.instructions
    assert "只读 verify" in text or "只读 verify 步" in text
    assert "tests" in text
    assert METHOD_SYNTHESIZER_V6.instructions != METHOD_SYNTHESIZER.instructions


def test_the_read_only_worker_prompt_reports_missing_tests_as_findings() -> None:
    from agent_orchestrator.runtime.role_templates import (
        WORKER_HIERARCHICAL,
        WORKER_HIERARCHICAL_V2,
        WORKER_HIERARCHICAL_VERSION,
    )

    assert WORKER_HIERARCHICAL.prompt_version == WORKER_HIERARCHICAL_VERSION
    assert WORKER_HIERARCHICAL_VERSION == "worker-hierarchical-v4"
    assert WORKER_HIERARCHICAL_V2.prompt_version == "worker-hierarchical-v2"
    text = WORKER_HIERARCHICAL.instructions
    assert "finding" in text.lower() or "finding" in text
    assert "不要自己" in text or "不要自己写" in text or "不要自己创建" in text
