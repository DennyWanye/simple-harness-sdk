# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2b · a hierarchical Mission on a real model (opt in: ``--run-real-provider``).

Part 2's §10 recorded two reasons the smoke run never happened: there was no
deployment-side ``PlanningWorld``, and the endpoint that round was unusable.  Part
2b built the first; this file is the run.

What it asserts is the **closure**, never the wording of a stochastic model:

* the hierarchical Planner round is *readable* — the blocker P2.3b left was that a
  Planner asked for a ``<plan_revision_proposal>`` while holding the legacy DAG
  package had nothing to propose with, and every round came back
  ``proposal_unreadable``.  A run in which that reason appears at all is a failure
  of this slice, whatever the Mission's final status;
* the Mission ends either ``COMPLETED`` or with an **honest** stop: not completed,
  and every refusal carries a structured, machine-readable reason.

Credentials come from ``SH_BASEURL`` / ``SH_APIKEY`` / ``SH_MODEL`` and never reach
the evidence directory.  The raw receipts go to ``.local-test-evidence/`` (ignored);
the journal keeps only the text conclusion.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))
_AGENTS = Path(__file__).resolve().parents[2] / "agents"
if str(_AGENTS) not in sys.path:
    sys.path.insert(0, str(_AGENTS))

from real_provider_config import build_real_provider, resolve_real_provider  # noqa: E402

from agent_orchestrator.contracts import Budget, MissionStatus  # noqa: E402
from agent_orchestrator.contracts.htn import (  # noqa: E402
    ObligationId,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from agent_orchestrator.contracts.obligations import Obligation  # noqa: E402
from agent_orchestrator.contracts.semantic_base import content_hash_of  # noqa: E402
from agent_orchestrator.governance.policies import deployed_layers  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import MissionSpec  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.plan_commits import HIERARCHICAL_SEMANTICS  # noqa: E402
from agent_orchestrator.orchestrator.root_review import (  # noqa: E402
    ROOT_REVIEW_CUT,
    ROOT_REVIEW_CUT_BUDGET_SPENT,
    ROOT_REVIEW_REJECTED,
    ROOT_REVIEW_SUPERSEDED,
    ROOT_REVIEW_UNREADABLE,
)
from agent_orchestrator.planning.htn import evidence_round  # noqa: E402
from agent_orchestrator.planning.htn.observers.code import code_observers  # noqa: E402
from agent_orchestrator.planning.htn.planner_package import recorded_facts  # noqa: E402
from agent_orchestrator.planning.htn.seed_methods import seed_content_hash  # noqa: E402
from agent_orchestrator.planning.htn.world import build_planning_world  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.obligation_store import ObligationStore  # noqa: E402

pytestmark = pytest.mark.real_provider

ROOT_TASK = "task-root"
ROOT_DUTY = "obl-root"
GOAL_TYPE = "code.fix-failing-test"
#: The test that genuinely **fails** against the seeded implementation.  It has to
#: really fail: ``code.test-is-failing`` is an OPEN predicate, so an observer that
#: finds the test passing produces a non-authoritative negative — which §6.6 reads as
#: UNKNOWN, not FALSE, and the method then sits in NEEDS_EVIDENCE forever.  Naming a
#: test that passes would make this file a test of the wrong thing.
FAILING_TEST = "tests/test_kv.py::test_parse_kv_strips_whitespace"

#: Where the raw receipts go.  Ignored by git; the journal records only the
#: conclusion, the ids and the token count — never the key and never a transcript.
EVIDENCE_ROOT = Path(
    os.environ.get(
        "HTN_SMOKE_EVIDENCE",
        str(
            Path(__file__).resolve().parents[3]
            / ".local-test-evidence"
            / "2026-09-16"
            / "htn-smoke"
        ),
    )
)

PACKAGE = '''"""A tiny package with one failing test, for the P2.3c smoke run."""


def parse_kv(text):
    if not text:
        return {}
    out = {}
    for chunk in text.split(";"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        out[key] = value
    return out
'''

FAILING = """from kvlib import parse_kv


def test_parse_kv_splits_pairs():
    assert parse_kv("a=1;b=2") == {"a": "1", "b": "2"}


def test_parse_kv_strips_whitespace():
    assert parse_kv(" a = 1 ; b = 2 ") == {"a": "1", "b": "2"}
"""


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "kvlib.py").write_text(PACKAGE, encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_kv.py").write_text(FAILING, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=smoke@test", "-c", "user.name=smoke", "commit", "-qm", "seed"],
        cwd=root,
        check=True,
    )
    return root


def _vref(identifier: str, version: int = 1):
    from agent_orchestrator.contracts.semantic_base import VersionedRef

    return VersionedRef(
        id=identifier, version=version, content_hash=seed_content_hash(identifier, version)
    )


def _root_binding(world, repository: str) -> TaskSemanticBindingV1:
    spec = world.catalog.require(_vref(GOAL_TYPE))
    return TaskSemanticBindingV1(
        task_id=TaskRef(ROOT_TASK),
        obligation_id=ObligationId(ROOT_DUTY),
        contract_revision=1,
        contract_hash=content_hash_of([ROOT_TASK, GOAL_TYPE]),
        form=TaskForm.COMPOUND,
        goal_signature=spec.goal_signature,
        typed_parameters={"repository": repository, "failing_test": FAILING_TEST},
        requirement_refs=tuple(spec.goal_signature.coverage_criteria),
        semantic_scope="mission",
    )


def _look(world, semantics, mission_id: str, repository: str) -> dict:
    """The deployment's evidence round, run against the real worktree.

    Both preconditions of ``code.fix-by-patch`` are read by the real observers —
    ``git rev-parse`` for the checkout and the test runner for the failing test —
    so the plan the model is asked for is decided against facts, not assumptions.
    """

    from agent_orchestrator.contracts.htn import parse_conditions

    conditions = parse_conditions(
        [
            {
                "op": "predicate",
                "predicate_ref": _vref("code.repo-checked-out").to_json(),
                "arguments": {"repository": {"op": "parameter", "name": "repository"}},
            },
            {
                "op": "predicate",
                "predicate_ref": _vref("code.test-is-failing").to_json(),
                "arguments": {"test": {"op": "parameter", "name": "failing_test"}},
            },
        ],
        "preconditions",
    )
    asks = evidence_round.pending_asks(
        conditions,
        parameters={"repository": repository, "failing_test": FAILING_TEST},
        registry=world.predicates,
        snapshot=world.snapshot(),
    )
    result = evidence_round.run_round(
        world.observer_index, semantics, mission_id, asks, now_ms=1_000
    )
    return {
        "asked": [item.predicate_ref.id for item in asks],
        "recorded": [item.observation.predicate_id for item in result.recorded],
        "unavailable": [
            {"predicate": item.observation.predicate_id, "detail": item.observation.detail[:200]}
            for item in result.unavailable
        ],
    }


def _account(orchestrator, mission_id: str):
    """The Mission's budget account as the ledger holds it, or why it could not be read."""

    from agent_orchestrator.governance.budgets import BudgetError
    from agent_orchestrator.orchestrator.commit_service import mission_account

    try:
        return orchestrator.commit.ledger.account(mission_account(mission_id)).to_json()
    except BudgetError as error:  # a Mission that never opened one is a fact, not a crash
        return {"unavailable": str(error)}


def test_real_hierarchical_planner_round(tmp_path):
    config = resolve_real_provider()
    if config is None:
        pytest.skip("no real provider configured (SH_BASEURL/SH_APIKEY or Host .env)")
    provider = build_real_provider(config, timeout=240.0)
    repo = _repo(tmp_path / "repo")
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    orchestrator_config = OrchestratorConfig(
        # The library is per-run (``tmp_path``) and only the *report* is kept under
        # the evidence root: a reused library would replay the previous run's Mission
        # and duty rows, which is a fixture accident rather than a finding.
        evidence_root=tmp_path / "orchestrator",
        model=config.model,
        max_concurrency=1,
        max_concurrent_model_calls=1,
        default_max_output_tokens=4096,
        test_timeout_seconds=120,
        turn_deadline_seconds=600,
    )
    spec = MissionSpec(
        goal=(
            "修复仓库里失败的测试 tests/test_kv.py::test_parse_kv_strips_whitespace，"
            "并说明改动；不要改测试本身。"
        ),
        success_criteria=("pytest:tests/test_kv.py", "改动有说明"),
        tenant_id="real-htn",
        idempotency_key=f"htn-smoke-{os.getpid()}",
        allowed_tools=(
            "workspace_read_file",
            "workspace_write_file",
            "workspace_list",
            "run_tests",
        ),
        # Part 2d, smoke round 2: three leaves each took one Attempt and the Mission
        # then stopped on ``budget_exhausted`` before the root compound could be
        # resolved — the allowance, not the plan, was the bound.  A minimal
        # hierarchical plan is three leaves plus room for one repair each.
        budget=Budget(max_tokens=600_000, max_attempts=8),
        orchestration_semantics_version=HIERARCHICAL_SEMANTICS,
        # The observers read the real worktree; the Worker works in the Mission's own
        # isolated workspace, which starts empty unless the Mission seeds it.  The
        # first run that got this far spent all three attempts reporting, correctly,
        # that there was nothing to read — so the two views are seeded from the same
        # bytes: the evidence is about the same package the Worker is asked to fix.
        workspace_seed={"kvlib.py": PACKAGE, "tests/test_kv.py": FAILING},
    )

    async def case():
        async with Orchestrator(
            orchestrator_config, provider, critic_wait_seconds=600
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec)
            semantics = HtnStore(orchestrator.store)
            world = build_planning_world(
                mission.id,
                domains=("code",),
                semantics=semantics,
                deployed_layers=deployed_layers(orchestrator_config.deployment_policy),
                observers=code_observers(repo, allow_test_execution=True),
            )
            orchestrator.install_hierarchical(planning=world)
            binding = _root_binding(world, str(repo))
            duties = ObligationStore(orchestrator.store)
            duties.register(
                Obligation(
                    obligation_id=ObligationId(ROOT_DUTY),
                    mission_id=mission.id,
                    requirement_refs=tuple(binding.goal_signature.coverage_criteria),
                    goal_signature_id=GOAL_TYPE,
                ),
                recursion_fuel=6,
            )
            # TG decision 9: registering a duty records that it exists; *admitting a
            # demand* records that a live consumer is asking for it, and the selection
            # gate refuses an occurrence whose duty nobody is asking for
            # (``obligation_demand_not_admitted``).  The consumer here is whoever
            # submitted the Mission — the same caller that registers the root duty —
            # so the two acts belong together.  The Orchestrator deliberately does not
            # admit on its own behalf: it would be voting for its own work.  P2.3c
            # part 2d routes the bootstrap through the audited entry point, so the
            # root's admission is a record with a principal and evidence on it like
            # every child admission the commit path makes.
            orchestrator.commit.admit_obligation_demand(
                mission.id,
                ObligationId(ROOT_DUTY),
                principal="mission-submitter",
                requester={"kind": "mission_root"},
                evidence={
                    "requirement_refs": list(binding.goal_signature.coverage_criteria),
                    "mission_id": mission.id,
                },
            )
            semantics.put_task_semantics(mission.id, binding)
            looked = _look(world, semantics, mission.id, str(repo))
            await orchestrator.run()
            final = orchestrator.store.get_mission(mission.id)
            assert final is not None
            events = list(orchestrator.store.list_events(mission.id))
            rejections = [
                {"reason": item.payload.get("reason"), "detail": item.payload.get("detail")}
                for item in events
                if item.type in {"PlanningRejected", "TaskGraphRejected", "PlanCommitRefused"}
            ]
            report = {
                "model": config.model,
                "mission_id": mission.id,
                "run_id": getattr(orchestrator, "run_id", None),
                "status": str(final.status),
                "stop_reason": final.stop_reason,
                "evidence_round": looked,
                "planner_rounds": sum(
                    1 for item in events if item.type in {"DispatchIntentCreated", "IntentCreated"}
                ),
                "event_types": sorted({item.type for item in events}),
                "rejections": rejections,
                "proposal_unreadable": any(
                    item["reason"] == "proposal_unreadable" for item in rejections
                ),
                "plan_revisions": len(semantics.list_plan_revisions(mission.id)),
                # What the package *offered* the Planner to cite, so a
                # ``READ_SET_UNRESOLVED`` can be read as "the model ignored what it was
                # shown" or as "the package showed it nothing".
                "package_facts": [
                    item["read_set_entry"]
                    for item in recorded_facts(semantics.list_observations(mission.id))
                ],
                # P2.3c part 2c: a loop that goes idle with occurrences still withheld
                # records why.  It is the third honest ending beside COMPLETED and a
                # stop_reason, and the one this run has to be able to show.
                "stalled": [
                    item.payload for item in events if item.type == "HierarchicalMissionStalled"
                ],
                # P2.3c part 3a: how many times the root MISSION_FINAL review was cut,
                # and what the reviewer said.  A run that reaches COMPLETED does so
                # through exactly these records, so a report without them cannot say
                # *why* the Mission was allowed to finish.
                "root_review": {
                    "cuts": sum(1 for item in events if item.type == ROOT_REVIEW_CUT),
                    "superseded": sum(1 for item in events if item.type == ROOT_REVIEW_SUPERSEDED),
                    "rejected": [
                        item.payload
                        for item in events
                        if item.type in {ROOT_REVIEW_REJECTED, ROOT_REVIEW_UNREADABLE}
                    ],
                    "budget_spent": sum(
                        1 for item in events if item.type == ROOT_REVIEW_CUT_BUDGET_SPENT
                    ),
                    "records": [
                        {
                            "package_id": str(package.package_id),
                            "purpose": str(package.purpose),
                            "verdict": str(stored.record.verdict),
                        }
                        for package in semantics.list_review_packages(mission.id)
                        for stored in semantics.list_review_records(str(package.package_id))
                    ],
                },
                "tokens": _account(orchestrator, mission.id),
                "progress": orchestrator.progress_log[-40:],
            }
            (EVIDENCE_ROOT / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            (EVIDENCE_ROOT / "events.json").write_text(
                json.dumps(
                    [
                        {"type": item.type, "task_id": item.task_id, "payload": item.payload}
                        for item in events
                    ],
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            print("HTN_SMOKE_REPORT " + json.dumps(report, ensure_ascii=False, default=str))
            return report, final

    report, final = asyncio.run(case())

    # The property this slice owns: the hierarchical Planner round is readable.
    assert not report["proposal_unreadable"], (
        "the hierarchical Planner round came back unreadable — the package and the "
        f"prompt are still not agreeing: {report['rejections']}"
    )
    # Closure: completed, or an honest ending — a stop that names its reason, or a
    # recorded stall that names every gate still holding an occurrence.  What is
    # refused is the fourth case: a Mission left ``ACTIVE`` with nothing written down.
    if final.status is not MissionStatus.COMPLETED:
        assert all(item["reason"] for item in report["rejections"]), (
            "every refusal must carry a machine-readable reason"
        )
        if final.status is MissionStatus.FAILED:
            assert final.stop_reason is not None, "a stop without a reason is not an honest failure"
        else:
            assert report["stalled"], (
                "the Mission neither finished nor stopped and nothing said why"
            )
            for record in report["stalled"]:
                assert record["withheld"], "a stall record with no refusal explains nothing"
                assert all(
                    item["reason"] and item["detail_codes"] for item in record["withheld"]
                ), "every withheld occurrence names its gate in machine-readable form"
