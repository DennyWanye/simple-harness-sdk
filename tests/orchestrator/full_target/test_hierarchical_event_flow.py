# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3b red tests: the hierarchical mode, wired from the Planner's reply to the plan.

Six properties, each of which would be silently wrong if the wiring were merely
"plausible":

1. **One reply, one revision.**  A scripted ``<plan_revision_proposal>`` becomes a
   committed ``PlanRevision`` through ``parse → compile → commit_plan_revision``,
   and the ``PlanRevisionCommitted`` event lands in the store.
2. **A refusal is recompiled once, not forever.**  ``READ_SET_STALE`` on the first
   delivery is answered by compiling again *against the freshly read snapshot*;
   still refused after the bound, the round records ``PlanCommitRefused`` and
   stops.  Nothing is rebased (ADR-13 / C19).
3. **A compound never enters the Worker path.**  The interception is ``form`` from
   the semantic binding, so writing ``TaskStatus.READY`` onto the compound Task
   changes nothing, and ``NEEDS_REFINEMENT`` is recorded.
4. **Reading goes through the projection.**  ``list`` / ready / running / terminal /
   root review are computed from ``TaskNetworkSnapshot`` + ``evaluate_readiness``;
   the suite mutates ``Task.status`` to ``READY`` and asserts every one of those
   answers is unchanged (§18.5 rule 4, TG §7).
5. **A missing semantic binding is corruption.**  ``GraphIntegrityError``, never a
   legacy fallback (§18.5).
6. **Legacy is untouched.**  The same legacy Mission run with the assembly absent
   and with it installed produces byte-identical event payloads.

The last section is the mutation self-check: each mutant is a plausible wrong
implementation, and an assertion the real tests make must catch it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))
_ORCH_TESTS = Path(__file__).resolve().parents[1]
if str(_ORCH_TESTS) not in sys.path:
    sys.path.insert(0, str(_ORCH_TESTS))

from htn_world import Env, method, out, param, step, task_binding  # noqa: E402

from agent_orchestrator.artifacts.versioning import UpstreamInput  # noqa: E402
from agent_orchestrator.contracts import (  # noqa: E402
    Budget,
    MissionStatus,
    Task,
    TaskStatus,
)
from agent_orchestrator.contracts.htn import (  # noqa: E402
    OccurrenceId,
    OccurrenceSpec,
    Requiredness,
    TaskForm,
    TaskRef,
)
from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.contracts.obligations import Obligation  # noqa: E402
from agent_orchestrator.graph.eligibility import (  # noqa: E402
    OccurrenceOutcome,
    ReadinessReason,
)
from agent_orchestrator.graph.projection_validation import GraphIntegrityError  # noqa: E402
from agent_orchestrator.orchestrator import hierarchical_dispatch as module  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    COMPOUND_DISPLAY_STATUS,
    COMPOUND_PHASE_CHANGED,
    DISPATCH_INTERCEPTED,
    PLAN_COMMIT_REFUSED,
    PLAN_INTEGRITY_FAILED,
    RECOMPILABLE_REFUSALS,
    CompoundPhase,
    HierarchicalDispatch,
    PlanIntegrityError,
    is_hierarchical,
    next_compound_phase,
)
from agent_orchestrator.orchestrator.plan_commits import (  # noqa: E402
    HIERARCHICAL_SEMANTICS,
    LEGACY_SEMANTICS,
    PLAN_REVISION_COMMITTED,
    PlanCommitRejected,
    PlanPrincipal,
)
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.obligation_store import ObligationStore  # noqa: E402
from agent_orchestrator.storage.store import Store  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    DEMO_PROPOSAL,
    DEMO_SEED,
    RoleScriptedProvider,
    demo_single_task_provider,
    plan_revision_proposal_step,
    proposal_step,
)
from simple_harness.agents import AgentTurnState  # noqa: E402

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")
ROOT_TASK = "task-root"
ROOT_DUTY = "obl-root"
FUEL = 8
HEX_A = "a" * 64
HEX_OTHER = "f" * 64
HIERARCHICAL_GOAL = "交付一个可验收的层次计划"


# ------------------------------------------------------------------ the world builders
def _env(mission: str) -> Env:
    env = Env(mission=mission)
    env.register_type(
        "plan.goal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-root",),
        domain="plan",
    )
    env.register_type(
        "plan.leaf",
        parameters=(("subject", "string"),),
        outputs=(("result", "plan.result"),),
        capabilities=("plan.read",),
        domain="plan",
    )
    env.register_type(
        "plan.review",
        parameters=(("subject", "string"),),
        inputs=(("result", "plan.result", True),),
        outputs=(("verdict", "plan.verdict"),),
        capabilities=("plan.read",),
        domain="plan",
    )
    return env


def _outer(method_id: str = "plan.outer"):
    return method(
        method_id,
        "plan.goal",
        parameter_schema="plan.goal.params",
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
            step(
                "review",
                "plan.review",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "result": out("leaf", "result")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "review", "c-reviewed"),),
        finalizer="review",
    )


def _spec(key: str, *, mode: str) -> MissionSpec:
    return MissionSpec(
        goal=HIERARCHICAL_GOAL,
        success_criteria=("file:a.md",),
        tenant_id="tenant-p23b",
        idempotency_key=key,
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=200_000, max_attempts=12),
        orchestration_semantics_version=mode,
    )


@dataclass
class World:
    service: CommitService
    mission: Any
    env: Env
    contract: Any
    binding: Any
    dispatch: HierarchicalDispatch
    principal: PlanPrincipal

    @property
    def store(self) -> Store:
        return self.service.store

    @property
    def semantics(self) -> HtnStore:
        return HtnStore(self.service.store)

    def reply(self, **changes: Any) -> str:
        return _proposal_text(self.contract, **changes)

    def plan(self, text: str | None = None, *, command_id: str = "cmd-a"):
        return self.dispatch.apply_planner_reply(
            self.mission.id,
            text if text is not None else self.reply(),
            principal=self.principal,
            command_id=command_id,
        )

    def events(self, kind: str) -> list[Any]:
        return [e for e in self.store.list_events(self.mission.id) if e.type == kind]


def _proposal_text(contract: Any, **changes: Any) -> str:
    reference = contract.method_ref()
    base: dict[str, Any] = {
        "proposal_id": "prop-1",
        "expected_plan_revision": 0,
        "read_set": [
            {
                "kind": "method",
                "id": reference.method_id,
                "semantic_revision": reference.version,
                "content_hash": reference.content_hash,
            }
        ],
        "operations": [
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
    }
    base.update(changes)
    return plan_revision_proposal_step(**base)


def _world(tmp_path, *, mode: str = HIERARCHICAL_SEMANTICS, key: str = "p23b", **kwargs) -> World:
    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(_spec(key, mode=mode))
    env = _env(mission.id)
    contract = _outer()
    receipt = env.admit(contract)
    assert receipt.admitted, receipt.problems
    binding = task_binding(
        env,
        "plan.goal",
        task_id=ROOT_TASK,
        obligation=ROOT_DUTY,
        parameters={"subject": "alpha"},
    )
    if mode == HIERARCHICAL_SEMANTICS:
        ObligationStore(service.store).register(
            Obligation(
                obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
                mission_id=mission.id,
                requirement_refs=("req-1",),
                goal_signature_id="plan.goal",
            ),
            recursion_fuel=FUEL,
        )
        HtnStore(service.store).put_task_semantics(mission.id, binding)
        HtnStore(service.store).register_method(
            contract, env.registry.registration(contract.method_ref())
        )
    return World(
        service=service,
        mission=mission,
        env=env,
        contract=contract,
        binding=binding,
        dispatch=HierarchicalDispatch(service.store, service, planning=env, **kwargs),
        principal=PlanPrincipal("manager-1", "mission", 0),
    )


def _stale(compile_calls: list[Any], *, until: int = 1):
    """A ``compile_refinement_bundle`` wrapper that poisons the first ``until`` reads.

    The poison is a read-set item the store cannot possibly agree with, which is
    exactly what a genuinely stale read looks like to ``_check_read_set``.  The
    wrapper records the ``current`` network it was handed, so the suite can prove
    the recompilation happened against a *re-read* snapshot and not the old one.
    """

    import dataclasses

    from agent_orchestrator.contracts.htn import ReadItem, ReadItemKind

    real = module.compile_refinement_bundle

    def wrapper(draft, current, **kwargs):
        compile_calls.append(current)
        compilation = real(draft, current, **kwargs)
        if len(compile_calls) > until:
            return compilation
        poisoned = dataclasses.replace(
            compilation.delta.read_set,
            method_revisions=(
                ReadItem(
                    kind=ReadItemKind.METHOD,
                    id="plan.outer",
                    semantic_revision=1,
                    content_hash=HEX_OTHER,
                ),
            ),
        )
        delta = dataclasses.replace(compilation.delta, read_set=poisoned)
        return dataclasses.replace(compilation, delta=delta)

    return wrapper


# ====================================================================== one plan round
def test_a_scripted_planner_reply_commits_one_plan_revision(tmp_path):
    world = _world(tmp_path)
    outcome = world.plan()
    assert outcome.committed, outcome.refusals
    assert outcome.receipt.new_plan_revision == 1


def test_the_committed_revision_becomes_the_active_one(tmp_path):
    world = _world(tmp_path)
    world.plan()
    active = world.semantics.active_plan_revision(world.mission.id)
    assert active is not None and active.state == "ACTIVE"


def test_the_round_appends_the_plan_revision_committed_event(tmp_path):
    world = _world(tmp_path)
    world.plan()
    events = world.events(PLAN_REVISION_COMMITTED)
    assert len(events) == 1
    assert events[0].payload["proposal_id"] == "prop-1"


def test_the_round_records_the_proposal_id_in_the_commit_source(tmp_path):
    world = _world(tmp_path)
    outcome = world.plan()
    assert outcome.receipt.detail["source"]["proposal_id"] == "prop-1"


def test_the_first_round_compiles_against_the_seed_root_network(tmp_path):
    world = _world(tmp_path)
    seed = world.dispatch.network(world.mission.id)
    assert [str(spec.occurrence_id) for spec in seed.occurrences] == [ROOT_TASK]
    assert int(seed.plan_revision) == 0


def test_the_committed_network_is_read_back_from_the_store(tmp_path):
    world = _world(tmp_path)
    world.plan()
    network = world.dispatch.network(world.mission.id)
    assert int(network.plan_revision) == 1
    assert len(network.occurrences) == 3  # the root plus the method's two slots
    assert len(network.adopted_instance_ids) == 1


def test_the_read_back_network_keeps_the_root_as_the_only_root(tmp_path):
    world = _world(tmp_path)
    world.plan()
    network = world.dispatch.network(world.mission.id)
    assert [str(item) for item in network.root_occurrence_ids] == [ROOT_TASK]


def test_a_reply_without_the_typed_block_is_a_contract_error(tmp_path):
    world = _world(tmp_path)
    with pytest.raises(ContractError):
        world.plan("这是一段自然语言回复，没有任何标签块。")


def test_a_reply_claiming_an_authority_field_is_refused_at_the_boundary(tmp_path):
    world = _world(tmp_path)
    with pytest.raises(ContractError) as caught:
        world.plan(world.reply(extras={"manager_epoch": 3}))
    assert "manager_epoch" in str(caught.value)


def test_a_proposal_with_two_operations_is_refused_rather_than_partly_applied(tmp_path):
    world = _world(tmp_path)
    reference = world.contract.method_ref()
    refine = {
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
    with pytest.raises(ContractError) as caught:
        world.plan(world.reply(operations=[refine, refine]))
    assert "one refinement per round" in str(caught.value)
    assert world.events(PLAN_REVISION_COMMITTED) == []


def test_a_legacy_mission_is_not_an_entry_point_for_the_assembly(tmp_path):
    world = _world(tmp_path, mode=LEGACY_SEMANTICS, key="legacy-door")
    with pytest.raises(ContractError) as caught:
        world.plan()
    assert "legacy" in str(caught.value)


def test_a_deployment_without_a_planning_world_refuses_visibly(tmp_path):
    world = _world(tmp_path)
    world.dispatch.planning = None
    with pytest.raises(ContractError) as caught:
        world.plan()
    assert "PlanningWorld" in str(caught.value)


# ============================================================ the bounded recompilation
def test_a_stale_read_set_is_recompiled_once_and_then_commits(tmp_path, monkeypatch):
    world = _world(tmp_path)
    calls: list[Any] = []
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale(calls, until=1))
    outcome = world.plan()
    assert outcome.committed, outcome.refusals
    assert [item.reason for item in outcome.refusals] == ["READ_SET_STALE"]
    assert len(calls) == 2


def test_the_recompilation_uses_a_freshly_read_snapshot(tmp_path, monkeypatch):
    world = _world(tmp_path)
    calls: list[Any] = []
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale(calls, until=1))
    world.plan()
    assert calls[0] is not calls[1]  # two distinct reads, not the cached one


def test_a_successful_recompilation_appends_no_refusal_event(tmp_path, monkeypatch):
    world = _world(tmp_path)
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale([], until=1))
    world.plan()
    assert world.events(PLAN_COMMIT_REFUSED) == []


def test_a_refusal_that_survives_the_bound_records_plan_commit_refused(tmp_path, monkeypatch):
    world = _world(tmp_path)
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale([], until=99))
    outcome = world.plan()
    assert not outcome.committed
    events = world.events(PLAN_COMMIT_REFUSED)
    assert len(events) == 1
    assert events[0].payload["reason"] == "READ_SET_STALE"
    assert events[0].payload["attempts"] == 2


def test_the_bound_stops_the_round_and_does_not_keep_retrying(tmp_path, monkeypatch):
    world = _world(tmp_path)
    calls: list[Any] = []
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale(calls, until=99))
    world.plan()
    assert len(calls) == 2  # the default bound, not three and not forever


def test_the_bound_is_configurable_and_respected(tmp_path, monkeypatch):
    world = _world(tmp_path, compile_attempts=3)
    calls: list[Any] = []
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale(calls, until=99))
    world.plan()
    assert len(calls) == 3


def test_nothing_is_written_when_the_round_is_refused(tmp_path, monkeypatch):
    world = _world(tmp_path)
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale([], until=99))
    world.plan()
    assert world.semantics.active_plan_revision(world.mission.id) is None
    assert world.events(PLAN_REVISION_COMMITTED) == []


def test_the_refusal_record_states_that_nothing_was_rebased(tmp_path, monkeypatch):
    world = _world(tmp_path)
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale([], until=99))
    world.plan()
    assert world.events(PLAN_COMMIT_REFUSED)[0].payload["rebased"] is False


def test_the_round_never_calls_the_legacy_graph_change_path(tmp_path, monkeypatch):
    world = _world(tmp_path)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a hierarchical proposal reached the legacy graph change path")

    monkeypatch.setattr(CommitService, "commit_graph_change", _explode, raising=False)
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale([], until=99))
    world.plan()
    assert not world.plan(command_id="cmd-b").committed


def test_a_non_recompilable_refusal_stops_after_one_attempt(tmp_path, monkeypatch):
    """A cancelled Mission is not something compiling again can fix (§9.4)."""

    world = _world(tmp_path)
    world.service.cancel_mission(world.mission.id)
    calls: list[Any] = []
    real = module.compile_refinement_bundle

    def wrapper(draft, current, **kwargs):
        calls.append(current)
        return real(draft, current, **kwargs)

    monkeypatch.setattr(module, "compile_refinement_bundle", wrapper)
    outcome = world.plan()
    assert [item.reason for item in outcome.refusals] == ["MISSION_NOT_WRITABLE"]
    assert len(calls) == 1
    assert outcome.refusals[0].recompilable is False


def test_the_recompilable_set_does_not_contain_an_identity_refusal(tmp_path):
    del tmp_path
    assert "PRINCIPAL_MISMATCH" not in RECOMPILABLE_REFUSALS
    assert "SCOPE_NOT_AUTHORIZED" not in RECOMPILABLE_REFUSALS
    assert "SEMANTICS_NOT_HIERARCHICAL" not in RECOMPILABLE_REFUSALS
    assert "MISSING_SEMANTIC_BINDING" not in RECOMPILABLE_REFUSALS


def test_a_structural_refusal_against_the_plan_is_recompilable(tmp_path):
    """``PLAN_NOT_PRESERVED`` compares the increment with the plan it came from."""

    del tmp_path
    assert "PLAN_NOT_PRESERVED" in RECOMPILABLE_REFUSALS
    assert "STRUCTURE_INVALID" in RECOMPILABLE_REFUSALS


def test_a_defect_in_the_compiled_delta_itself_is_not_recompilable(tmp_path):
    """A newer snapshot does not make a non-commit-ready delta ready."""

    del tmp_path
    assert "DELTA_NOT_COMMIT_READY" not in RECOMPILABLE_REFUSALS
    assert "OR_NOT_RESOLVED" not in RECOMPILABLE_REFUSALS


def test_every_recompilable_reason_is_one_the_commit_service_can_raise(tmp_path):
    """The classification may not name a refusal that does not exist."""

    del tmp_path
    import inspect
    import re

    from agent_orchestrator.orchestrator import plan_commits

    raised = set(re.findall(r'PlanCommitRejected\(\s*"([A-Z_]+)"', inspect.getsource(plan_commits)))
    assert RECOMPILABLE_REFUSALS <= raised, RECOMPILABLE_REFUSALS - raised


def test_a_plan_commit_does_not_advance_the_integer_graph_version(tmp_path):
    """P2.3a: the serialisation point of this mode is the plan revision.

    The assembly passes ``base_graph_version`` because ADR-13 keeps the integer as
    the coarse gate, and must not treat it as a concurrency token: a Mission whose
    graph version never moves has to keep accepting plan revisions.
    """

    world = _world(tmp_path)
    before = int((world.mission.final_report or {}).get("graph_version") or 1)
    assert world.plan().committed
    after = world.store.get_mission(world.mission.id)
    assert int((after.final_report or {}).get("graph_version") or 1) == before


def test_a_malformed_block_produces_a_bounded_repair_hint(tmp_path):
    """§18.5 C8: the parse failure carries the one instruction the repair needs."""

    del tmp_path
    from agent_orchestrator.contracts.models import ContractError as _ContractError
    from agent_orchestrator.planning.planner import parse_plan_proposal
    from agent_orchestrator.runtime.output_blocks import BlockError, repair_hint
    from agent_orchestrator.runtime.role_templates import PLAN_REVISION_PROPOSAL_TAG

    with pytest.raises(_ContractError) as caught:
        parse_plan_proposal(
            "<plan_revision_proposal>{ not json </plan_revision_proposal>", mission_id="m"
        )
    cause = caught.value.__cause__
    assert isinstance(cause, BlockError)
    hint = repair_hint(cause, PLAN_REVISION_PROPOSAL_TAG)
    assert PLAN_REVISION_PROPOSAL_TAG in hint


def test_the_event_handler_puts_the_repair_hint_into_the_planning_rejection(tmp_path):
    del tmp_path
    import inspect

    from agent_orchestrator.orchestrator import event_handler

    source = inspect.getsource(event_handler.Orchestrator._collect_plan_hierarchical)
    assert 'detail["repair_hint"] = repair_hint(cause, PLAN_REVISION_PROPOSAL_TAG)' in source
    assert 'reason="proposal_unreadable"' in source


def test_a_second_identical_reply_does_not_produce_a_second_revision(tmp_path):
    """Re-refining an already refined goal is refused by the compiler, not committed.

    The idempotency of one *command* is P2.3a's property.  What this slice has to
    hold is the round above it: the same reply delivered twice recompiles against
    the *new* snapshot, where the occurrences it wants already exist, and that is a
    structural refusal — never a second plan revision that duplicates the work.
    """

    from agent_orchestrator.planning.htn.compiler import CompilationRefused

    world = _world(tmp_path)
    assert world.plan().committed
    with pytest.raises(CompilationRefused):
        world.plan(command_id="cmd-b")
    assert len(world.events(PLAN_REVISION_COMMITTED)) == 1
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1


# ======================================================== the compound dispatch gate
def _committed(tmp_path, *, demand: bool = False, **kwargs) -> World:
    world = _world(tmp_path, **kwargs)
    assert world.plan().committed
    if demand:
        _admit_demand(world)
    return world


def _admit_demand(world: World) -> None:
    """TG decision 9: an occurrence is selected only while a demand for its duty lives."""

    duties = ObligationStore(world.store)
    seen: set[str] = set()
    for spec in world.dispatch.network(world.mission.id).occurrences:
        duty = str(spec.obligation_id)
        if duty in seen or not duties.exists(world.mission.id, spec.obligation_id):
            continue
        seen.add(duty)
        account = duties.account(world.mission.id, spec.obligation_id)
        if not account.has_admitted_demand:
            world.service.admit_obligation_demand(
                world.mission.id,
                spec.obligation_id,
                principal="mission-submitter",
                requester={"kind": "mission_root"},
                evidence={"occurrence": str(spec.occurrence_id)},
            )


def _make_task(world: World, task_id: str, *, status: TaskStatus) -> None:
    """Give a semantic task a legacy Task row in a chosen display state.

    The point of every test that calls this is that the row is *display*: the new
    mode's answers have to be identical whatever it says.
    """

    from agent_orchestrator.contracts import Budget as TaskBudget
    from agent_orchestrator.contracts import Task

    existing = world.store.get_task(task_id)
    ordinal = 1 + len(world.store.list_tasks(world.mission.id))
    task = Task(
        id=task_id,
        mission_id=world.mission.id,
        parent_task_ids=(),
        dependency_ids=(),
        goal=f"goal of {task_id}",
        rationale="display row",
        success_criteria=("file:a.md",),
        verification_policy=("format_check",),
        allowed_tools=TOOLS,
        budget=TaskBudget(max_tokens=1_000, max_attempts=2),
        priority=1.0,
        status=status,
        version=1 if existing is None else existing.version + 1,
    )
    if existing is None:
        world.store.insert_task(task, ordinal=ordinal)
    else:
        world.store.update_task(task, expected_version=existing.version)


def test_a_compound_is_intercepted_before_any_worker_dispatch(tmp_path):
    world = _committed(tmp_path)
    intercepted = world.dispatch.intercept_worker_dispatch(world.mission.id, ROOT_TASK)
    assert intercepted is not None
    assert intercepted.reason == str(ReadinessReason.NEEDS_REFINEMENT)


def test_the_interception_is_recorded_as_an_event(tmp_path):
    world = _committed(tmp_path)
    world.dispatch.intercept_worker_dispatch(world.mission.id, ROOT_TASK)
    events = world.events(DISPATCH_INTERCEPTED)
    assert len(events) == 1
    assert events[0].payload["reason"] == "NEEDS_REFINEMENT"
    assert events[0].payload["form"] == "compound"


def test_the_interception_does_not_depend_on_the_legacy_status(tmp_path):
    world = _committed(tmp_path)
    _make_task(world, ROOT_TASK, status=TaskStatus.READY)
    intercepted = world.dispatch.intercept_worker_dispatch(world.mission.id, ROOT_TASK)
    assert intercepted is not None and intercepted.reason == "NEEDS_REFINEMENT"


def test_a_primitive_is_not_intercepted_by_this_gate(tmp_path):
    world = _committed(tmp_path)
    network = world.dispatch.network(world.mission.id)
    leaf = next(spec for spec in network.occurrences if spec.form is TaskForm.PRIMITIVE)
    assert world.dispatch.intercept_worker_dispatch(world.mission.id, str(leaf.task_id)) is None


def test_the_interception_creates_no_attempt_for_the_compound(tmp_path):
    world = _committed(tmp_path)
    _make_task(world, ROOT_TASK, status=TaskStatus.READY)
    world.dispatch.intercept_worker_dispatch(world.mission.id, ROOT_TASK)
    assert world.store.list_attempts(ROOT_TASK) == []


def test_a_task_without_a_semantic_binding_is_corruption_at_the_gate(tmp_path):
    world = _committed(tmp_path)
    with pytest.raises(GraphIntegrityError):
        world.dispatch.intercept_worker_dispatch(world.mission.id, "task-nowhere")


# ========================================================== the projection is the read
def test_the_ready_set_comes_from_the_execution_frontier(tmp_path):
    world = _committed(tmp_path)
    ready = world.dispatch.ready_occurrences(world.mission.id)
    assert ROOT_TASK not in {str(item) for item in ready}  # a compound is never ready


def test_writing_ready_onto_the_compound_task_changes_no_answer(tmp_path):
    world = _committed(tmp_path)
    before = (
        world.dispatch.ready_occurrences(world.mission.id),
        world.dispatch.running_occurrences(world.mission.id),
        world.dispatch.root_review_ready(world.mission.id),
        world.dispatch.terminal(world.mission.id),
    )
    _make_task(world, ROOT_TASK, status=TaskStatus.READY)
    after = (
        world.dispatch.ready_occurrences(world.mission.id),
        world.dispatch.running_occurrences(world.mission.id),
        world.dispatch.root_review_ready(world.mission.id),
        world.dispatch.terminal(world.mission.id),
    )
    assert before == after


def test_writing_completed_onto_every_task_does_not_make_the_mission_terminal(tmp_path):
    world = _committed(tmp_path)
    for spec in world.dispatch.network(world.mission.id).occurrences:
        _make_task(world, str(spec.task_id), status=TaskStatus.COMPLETED)
    assert world.dispatch.terminal(world.mission.id) is False


def test_the_compound_occurrence_is_never_counted_as_running_work(tmp_path):
    world = _committed(tmp_path)
    _make_task(world, ROOT_TASK, status=TaskStatus.ACTIVE)
    running = {str(item) for item in world.dispatch.running_occurrences(world.mission.id)}
    assert ROOT_TASK not in running


def test_the_typed_view_carries_the_legacy_status_as_diagnostics_only(tmp_path):
    world = _committed(tmp_path)
    _make_task(world, ROOT_TASK, status=TaskStatus.READY)
    views = {str(view.task_id): view for view in world.dispatch.occurrences(world.mission.id)}
    assert views[ROOT_TASK].legacy_status == "READY"
    report = world.dispatch.read(world.mission.id).reports[OccurrenceId(ROOT_TASK)]
    assert report.reason is ReadinessReason.NEEDS_REFINEMENT


def test_the_planning_frontier_holds_the_compound_that_still_needs_a_method(tmp_path):
    world = _world(tmp_path)
    view = world.dispatch.read(world.mission.id)
    assert view.planning_frontier.by_reason[ReadinessReason.NEEDS_REFINEMENT] == (
        OccurrenceId(ROOT_TASK),
    )


def test_the_occurrence_listing_is_the_projection_not_the_task_table(tmp_path):
    world = _committed(tmp_path)

    def listing() -> set[str]:
        return {str(view.occurrence_id) for view in world.dispatch.occurrences(world.mission.id)}

    listed = listing()
    _make_task(world, "task-extra", status=TaskStatus.READY)
    assert listing() == listed


def test_a_membership_naming_an_unbound_task_is_a_graph_integrity_error(tmp_path):
    world = _committed(tmp_path)
    world.semantics.insert_plan_membership(
        world.mission.id,
        1,
        OccurrenceSpec(
            occurrence_id=OccurrenceId("occ-orphan"),
            task_id=TaskRef("task-orphan"),
            obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
            form=TaskForm.PRIMITIVE,
            requiredness=Requiredness.OPTIONAL_AUTHORIZED,
        ),
        instance_id=None,
        adopted=True,
    )
    with pytest.raises(GraphIntegrityError):
        world.dispatch.network(world.mission.id)


def test_a_mission_with_no_semantic_binding_at_all_is_corruption(tmp_path):
    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(_spec("bare", mode=HIERARCHICAL_SEMANTICS))
    dispatch = HierarchicalDispatch(service.store, service)
    with pytest.raises(GraphIntegrityError):
        dispatch.network(mission.id)


def test_the_root_review_waits_for_every_gating_child(tmp_path):
    world = _committed(tmp_path)
    assert world.dispatch.root_review_ready(world.mission.id) is False


def test_the_root_review_is_not_unlocked_by_completing_the_task_rows(tmp_path):
    world = _committed(tmp_path)
    for spec in world.dispatch.network(world.mission.id).occurrences:
        _make_task(world, str(spec.task_id), status=TaskStatus.COMPLETED)
    assert world.dispatch.root_review_ready(world.mission.id) is False


def test_the_root_review_is_unlocked_when_the_children_are_accepted(tmp_path):
    world = _committed(tmp_path)
    _accept_children(world)
    assert world.dispatch.root_review_ready(world.mission.id) is True


def test_accepting_only_one_child_does_not_unlock_the_root_review(tmp_path):
    world = _committed(tmp_path)
    _accept_children(world, limit=1)
    assert world.dispatch.root_review_ready(world.mission.id) is False


def _accept_children(world: World, *, limit: int | None = None) -> list[str]:
    """Record a CURRENT Acceptance (with its review chain) for the child occurrences.

    The package and the record are not decoration: AER §6.1 makes an Acceptance
    point at the official review that produced it, and the schema enforces it.  A
    test that faked the acceptance row would be testing a state the system cannot
    reach.
    """

    from agent_orchestrator.contracts.resolution import (
        Acceptance,
        AllExpr,
        CheckExecution,
        Criterion,
        CriterionExpr,
        CriterionOrigin,
        CriterionOutcome,
        CriterionVerdict,
        EvaluationKind,
        RequirementClass,
        ReviewBinding,
        ReviewPackage,
        ReviewPurpose,
        ReviewRecord,
        ReviewVerdict,
    )
    from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind

    def binding_for(task_id: str) -> ReviewBinding:
        return ReviewBinding(
            mission_id=world.mission.id,
            obligation_id=ROOT_DUTY,
            subject_ref=TypedRef(
                kind=TypedRefKind.TASK, id=task_id, revision=1, content_hash=HEX_A
            ),
            requirements_revision=0,
            input_manifest_hash=HEX_A,
            policy_ref=TypedRef(
                kind=TypedRefKind.REQUIREMENTS, id="policy-1", revision=1, content_hash=HEX_A
            ),
        )

    def criterion() -> Criterion:
        return Criterion(
            criterion_id="c-1",
            revision=1,
            origin=CriterionOrigin.POLICY_REQUIRED,
            statement="the child produced its declared output",
            requirement_class=RequirementClass.REQUIRED_OUTCOME,
            evaluation_kind=EvaluationKind.SEMANTIC,
        )

    def chain(task_id: str) -> str:
        package_id = f"pkg-{task_id}"
        record_id = f"rev-{task_id}"
        world.semantics.insert_review_package(
            ReviewPackage(
                package_id=package_id,  # type: ignore[arg-type]
                purpose=ReviewPurpose.TASK_CONTENT,
                binding=binding_for(task_id),
                criteria=(criterion(),),
                success_expression=AllExpr((CriterionExpr("c-1"),)),
            )
        )
        world.semantics.insert_review_record(
            ReviewRecord(
                record_id=record_id,  # type: ignore[arg-type]
                package_id=package_id,  # type: ignore[arg-type]
                purpose=ReviewPurpose.TASK_CONTENT,
                binding=binding_for(task_id),
                reviewer_agent_id="independent-agent",
                reviewer_turn_id=f"turn-{task_id}",
                evidence_manifest_hash=HEX_A,
                criteria=(
                    CriterionOutcome(
                        criterion_id="c-1",
                        verdict=CriterionVerdict.PASS,
                        check_execution=CheckExecution.SUCCEEDED,
                    ),
                ),
                verdict=ReviewVerdict.ACCEPT,
            ),
            official=True,
        )
        return record_id

    network = world.dispatch.network(world.mission.id)
    children = [
        spec
        for spec in network.occurrences
        if spec.occurrence_id not in set(network.root_occurrence_ids)
    ]
    accepted: list[str] = []
    for index, spec in enumerate(sorted(children, key=lambda item: str(item.occurrence_id))):
        if limit is not None and index >= limit:
            break
        record_id = chain(str(spec.task_id))
        world.semantics.insert_acceptance(
            Acceptance(
                acceptance_id=f"acc-{spec.task_id}",  # type: ignore[arg-type]
                mission_id=world.mission.id,
                task_id=spec.task_id,
                obligation_id=spec.obligation_id,
                requirements_revision=1,
                contract_revision=1,
                input_manifest_hash=HEX_A,
                review_record_id=record_id,  # type: ignore[arg-type]
                accepted_at_ms=1_000,
            )
        )
        accepted.append(str(spec.occurrence_id))
    return accepted


def test_an_accepted_child_reads_as_accepted_in_the_outcome_projection(tmp_path):
    world = _committed(tmp_path)
    accepted = set(_accept_children(world))
    outcomes = world.dispatch.read(world.mission.id).outcomes
    for occurrence, outcome in outcomes.items():
        if str(occurrence) in accepted:
            assert outcome is OccurrenceOutcome.ACCEPTED


def test_a_completed_task_without_an_acceptance_is_not_accepted(tmp_path):
    world = _committed(tmp_path)
    network = world.dispatch.network(world.mission.id)
    leaf = next(spec for spec in network.occurrences if spec.form is TaskForm.PRIMITIVE)
    _make_task(world, str(leaf.task_id), status=TaskStatus.COMPLETED)
    outcomes = world.dispatch.read(world.mission.id).outcomes
    assert outcomes[leaf.occurrence_id] is OccurrenceOutcome.SETTLED_OTHER


# ================================================================ the compound phases
def test_an_unrefined_compound_is_planning_ready(tmp_path):
    world = _world(tmp_path)
    phases = world.dispatch.advance_compound_phases(world.mission.id)
    assert phases[OccurrenceId(ROOT_TASK)] is CompoundPhase.PLANNING_READY


def test_a_refined_compound_waits_for_its_children(tmp_path):
    world = _committed(tmp_path)
    phases = world.dispatch.advance_compound_phases(world.mission.id)
    assert phases[OccurrenceId(ROOT_TASK)] is CompoundPhase.WAITING_CHILDREN


def test_a_compound_whose_children_are_accepted_enters_composition_review(tmp_path):
    world = _committed(tmp_path)
    _accept_children(world)
    phases = world.dispatch.advance_compound_phases(world.mission.id)
    assert phases[OccurrenceId(ROOT_TASK)] is CompoundPhase.COMPOSITION_REVIEW


def test_the_phase_pass_records_an_event_and_creates_no_attempt(tmp_path):
    world = _committed(tmp_path)
    world.dispatch.advance_compound_phases(world.mission.id)
    events = world.events(COMPOUND_PHASE_CHANGED)
    assert len(events) == 1
    assert events[0].payload["phase"] == "waiting_children"
    assert world.store.list_attempts(ROOT_TASK) == []


def test_the_phase_reducer_ignores_the_legacy_status_entirely(tmp_path):
    world = _committed(tmp_path)
    before = world.dispatch.advance_compound_phases(world.mission.id)
    for status in (TaskStatus.READY, TaskStatus.COMPLETED, TaskStatus.BLOCKED):
        _make_task(world, ROOT_TASK, status=status)
        assert world.dispatch.advance_compound_phases(world.mission.id) == before


def test_the_phase_reducer_refuses_a_primitive_occurrence(tmp_path):
    world = _committed(tmp_path)
    view = world.dispatch.read(world.mission.id)
    leaf = next(spec for spec in view.network.occurrences if spec.form is TaskForm.PRIMITIVE)
    with pytest.raises(ContractError):
        next_compound_phase(
            leaf,
            view.network,
            view.reports[leaf.occurrence_id],
            child_outcomes={},
            resolved=False,
        )


def test_every_phase_has_a_display_status(tmp_path):
    del tmp_path
    assert set(COMPOUND_DISPLAY_STATUS) == set(CompoundPhase)


def test_a_resolved_compound_is_resolution_committed(tmp_path):
    world = _committed(tmp_path)
    view = world.dispatch.read(world.mission.id)
    root = view.network.occurrence(OccurrenceId(ROOT_TASK))
    assert (
        next_compound_phase(
            root,
            view.network,
            view.reports[root.occurrence_id],
            child_outcomes={},
            resolved=True,
        )
        is CompoundPhase.RESOLUTION_COMMITTED
    )


# ======================================================== legacy zero regression
#: Fields whose value is a fresh identifier or a hash over one, generated once per
#: process run: a subprocess execution id, the hash of a pytest run, and everything
#: derived from them.  They differ between *any* two runs of the same Mission and say
#: nothing about behaviour, so the comparison normalises them by name instead of
#: pretending the bytes are reproducible.
#: A liveness *sample*: how far a running turn had got when the poll fired.  Two runs
#: of the same Mission sample at different moments, so the row exists in both and its
#: numbers differ.  Excluded by type rather than field by field, because the whole
#: event is a measurement and none of it is a decision.
_SAMPLED_EVENTS = frozenset({"HeartbeatReceived"})

#: The fields that carry a *per-run* identifier, named one by one.  This list was not
#: guessed: it is every field that actually differs between two runs of the same
#: Mission, and nothing else is forgiven.  An earlier version of this comparison
#: normalised "any number ≥ 1e9", which silently also covered deterministic
#: configuration values such as a receipt's ``max_rss_bytes`` — a byte ceiling is a
#: decision, and a comparison that forgives it is not checking anything there.
_VOLATILE_KEYS = frozenset(
    {
        # a fresh uuid per subprocess run, and the hashes computed over one
        "execution_id",
        "run_hash",
        "knowledge_id",
        "claim_id",
        "context_version",
        # A measured duration, not a decision: the allocator's waiting-age input is
        # wall clock and moves by microseconds between two runs — and ``score`` is a
        # rounded function of it (weight 0.10), so it inherits the jitter whenever the
        # difference crosses the fourth decimal.  ``tier`` is *not* forgiven: it is a
        # threshold at one full second, nowhere near these sub-millisecond readings,
        # so if it ever flips the comparison should fail and be looked at.
        "waiting_age",
        "score",
    }
)

#: The same per-run identifiers where they appear *inside* a string or a list of
#: strings (a knowledge id in ``final_report.knowledge``, in ``lineage.knowledge[].id``
#: and in ``lineage.edges[].produced``) rather than as their own field.
_VOLATILE_PATTERNS = (
    (re.compile(r"observation:[0-9a-f]{64}"), "observation:<id>"),
    # Third-round review P2-5: ``VerificationLayerRecorded.summary`` quotes pytest's
    # own one-line report, which ends in the wall-clock time the run took.  Under CPU
    # contention the two runs of this golden legitimately differ by hundredths of a
    # second, and the suite's one hard gate on "the DAG mode's bytes did not change"
    # was failing about a third of the time under parallel load.  How long a test took
    # is not behaviour; that it ran and what it reported is, and both survive this.
    (re.compile(r"\bin \d+(?:\.\d+)?s\b"), "in <duration>"),
)


def _redact(value: Any, root: Path) -> Any:
    """Normalise what legitimately differs between two runs of the same Mission.

    The evidence root and the per-run identifiers above are *environment*, not
    behaviour.  Everything else — every task and attempt id, artifact content hash,
    criterion verdict, ordinal, budget figure, byte ceiling and stop reason — is
    compared byte for byte, which is what §18.5 rule 3 asks for.
    """

    if isinstance(value, str):
        text = value.replace(str(root), "<root>")
        for pattern, replacement in _VOLATILE_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
    if isinstance(value, dict):
        return {
            key: ("<env>" if key in _VOLATILE_KEYS else _redact(item, root))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, root) for item in value]
    return value


def _legacy_events(tmp_path, *, install: bool) -> list[tuple[str, str]]:
    """Run the step-2 single-task Mission end to end; return (type, payload) per event.

    The single-task fixture is the deterministic one: the step-3 parallel DAG
    interleaves two Workers, so its *event order* is a scheduling fact and not a
    behavioural one.  What this comparison is for is the wiring — five branches were
    added to the event handler, and a legacy Mission must walk past every one of them
    without a single byte moving.
    """

    from agent_orchestrator.contracts import MissionStatus  # noqa: PLC0415
    from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: PLC0415
    from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: PLC0415
    from agent_orchestrator.testing.fixtures import (  # noqa: PLC0415
        DEMO_PROPOSAL,
        DEMO_SEED,
        demo_single_task_provider,
    )

    provider = demo_single_task_provider()
    config = OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
    )
    rows: list[tuple[str, str]] = []

    async def case() -> None:
        async with Orchestrator(config, provider) as orchestrator:
            if install:
                orchestrator.install_hierarchical()
            mission = await orchestrator.submit_mission(
                MissionSpec(
                    goal=str(DEMO_PROPOSAL["root_goal"]),
                    success_criteria=tuple(DEMO_PROPOSAL["success_criteria"]),
                    tenant_id="tenant-p23b-legacy",
                    idempotency_key="legacy-bytes",
                    allowed_tools=tuple(DEMO_PROPOSAL["allowed_tools"]),
                    budget=Budget(max_tokens=200_000, max_attempts=12),
                    workspace_seed=DEMO_SEED,
                )
            )
            await orchestrator.run()
            final = orchestrator.store.get_mission(mission.id)
            assert final is not None and final.status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            for event in orchestrator.store.list_events(mission.id):
                if event.type in _SAMPLED_EVENTS:
                    continue
                payload = _redact(dict(event.payload), Path(tmp_path))
                # The subject travels in the key, not only in the payload: two events
                # of one type that name different Tasks are two different facts.
                rows.append(
                    (
                        f"{event.type}|{event.task_id or ''}|{event.attempt_id or ''}",
                        json.dumps(payload, sort_keys=True),
                    )
                )

    asyncio.run(case())
    return rows


def test_a_legacy_mission_produces_identical_event_bytes_with_the_assembly_installed(tmp_path):
    without = _legacy_events(tmp_path / "off", install=False)
    with_it = _legacy_events(tmp_path / "on", install=True)
    assert [kind for kind, _ in without] == [kind for kind, _ in with_it]
    assert without == with_it


class _ExplodingDispatch(HierarchicalDispatch):
    """Every hierarchical entry point, wired to fail loudly."""

    def _boom(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a legacy Mission reached the hierarchical assembly")

    network = seed_network = read = plan_view = _boom  # type: ignore[assignment]
    apply_planner_reply = intercept_worker_dispatch = _boom  # type: ignore[assignment]
    advance_compound_phases = root_review_ready = terminal = _boom  # type: ignore[assignment]
    attempt_inputs = occurrences = ready_occurrences = _boom  # type: ignore[assignment]
    running_occurrences = record_integrity_failure = _boom  # type: ignore[assignment]
    admit_method_proposal = compile_proposal = build_command = _boom  # type: ignore[assignment]


def _single_task_case() -> tuple[Any, MissionSpec, int]:
    """Step 2: one Task, a wrong first submission, a repair, a Critic."""

    from agent_orchestrator.testing.fixtures import (
        DEMO_PROPOSAL,
        DEMO_SEED,
        demo_single_task_provider,
    )

    return (
        demo_single_task_provider(),
        MissionSpec(
            goal=str(DEMO_PROPOSAL["root_goal"]),
            success_criteria=tuple(DEMO_PROPOSAL["success_criteria"]),
            tenant_id="tenant-p23b-explode",
            idempotency_key="explode-single",
            allowed_tools=tuple(DEMO_PROPOSAL["allowed_tools"]),
            budget=Budget(max_tokens=200_000, max_attempts=12),
            workspace_seed=DEMO_SEED,
        ),
        1,
    )


def _static_dag_case() -> tuple[Any, MissionSpec, int]:
    """Step 3: a five-Task DAG run in parallel, artifacts flowing downstream."""

    from agent_orchestrator.testing.fixtures import (
        DEMO_DAG_SPEC,
        TEXTKIT_SEED,
        demo_static_dag_provider,
    )

    return (
        demo_static_dag_provider(),
        MissionSpec(
            goal=str(DEMO_DAG_SPEC["goal"]),
            success_criteria=tuple(DEMO_DAG_SPEC["success_criteria"]),
            tenant_id="tenant-p23b-explode",
            idempotency_key="explode-dag",
            allowed_tools=tuple(DEMO_DAG_SPEC["allowed_tools"]),
            budget=Budget(max_tokens=200_000, max_attempts=12),
            workspace_seed=TEXTKIT_SEED,
        ),
        2,
    )


def _dynamic_dag_case() -> tuple[Any, MissionSpec, int]:
    """Step 5: a Worker proposal that reaches the graph only through the Manager."""

    from agent_orchestrator.testing.fixtures import (
        RECORDER_SEED,
        RECORDER_SPEC,
        demo_dynamic_dag_provider,
    )

    return (
        demo_dynamic_dag_provider(),
        MissionSpec(
            goal=str(RECORDER_SPEC["goal"]),
            success_criteria=tuple(RECORDER_SPEC["success_criteria"]),
            tenant_id="tenant-p23b-explode",
            idempotency_key="explode-dynamic",
            allowed_tools=tuple(RECORDER_SPEC["allowed_tools"]),
            budget=Budget(max_tokens=300_000, max_attempts=16),
            workspace_seed=RECORDER_SEED,
        ),
        2,
    )


EXPLODE_CASES = [
    ("one task, repair and critic", _single_task_case),
    ("a parallel DAG with artifact flow", _static_dag_case),
    ("a dynamic DAG through the Manager", _dynamic_dag_case),
]


@pytest.mark.parametrize(("name", "build"), EXPLODE_CASES, ids=[item[0] for item in EXPLODE_CASES])
def test_the_legacy_path_never_enters_the_assembly_at_all(tmp_path, name, build):
    """The strongest form of the zero-regression claim: the new code is not executed.

    A byte comparison can only say "the same events came out".  This says the legacy
    Mission never reached any hierarchical entry point, so there is no path by which
    its bytes *could* have moved — and it says it for the Planner, the allocator, the
    Worker result, the Critic, the Manager and the artifact-input path, because each
    of the three cases drives a different set of those.
    """

    del name
    from agent_orchestrator.contracts import MissionStatus

    provider, spec, concurrency = build()

    async def case() -> None:
        config = OrchestratorConfig(
            evidence_root=Path(tmp_path) / "evidence",
            max_concurrency=concurrency,
            test_timeout_seconds=60,
        )
        async with Orchestrator(config, provider) as orchestrator:
            orchestrator._hierarchical = _ExplodingDispatch(orchestrator.store, orchestrator.commit)
            mission = await orchestrator.submit_mission(spec)
            await orchestrator.run()
            final = orchestrator.store.get_mission(mission.id)
            assert final is not None and final.status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )

    asyncio.run(case())


def test_the_legacy_run_appends_none_of_the_new_event_types(tmp_path):
    rows = _legacy_events(tmp_path, install=True)
    kinds = {kind for kind, _ in rows}
    assert kinds.isdisjoint(
        {PLAN_REVISION_COMMITTED, PLAN_COMMIT_REFUSED, DISPATCH_INTERCEPTED, COMPOUND_PHASE_CHANGED}
    )


def test_the_mode_switch_defaults_to_legacy_for_a_mission_without_the_opt_in(tmp_path):
    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(
        MissionSpec(goal="g", success_criteria=("file:a.md",), tenant_id="t", idempotency_key="d")
    )
    assert is_hierarchical(mission) is False


def test_the_event_handler_asks_the_mode_before_consulting_the_assembly(tmp_path):
    """Every wiring site goes through one predicate, not several spellings of it.

    P2.3b had four sites; P2.3c part 2 added the fifth, ``_create_planner_intent``,
    which is where the *prompt* and the *package* are chosen together (blocker c: a
    Planner asked for a plan-revision proposal while holding the DAG package).  Part
    2b added two more: ``_create_synthesizer_intent`` (a MethodSynthesizer round is a
    hierarchical-only role, and asking the mode there is what stops a legacy Mission
    from being handed a method library it has no way to use) and
    ``_accept_hierarchical_leaf`` (a verified leaf becomes an ``Acceptance`` only in
    the new mode; in the legacy one ``accept_result`` is the whole lifecycle).  Part
    2c added three more — ``_gather_evidence`` (the read-only evidence round),
    ``_request_method_synthesis`` (deciding a goal has no method that could apply) and
    ``_collect_synthesizer`` (admitting the reply) — each a hierarchical-only step
    where asking the mode is what keeps it off a legacy Mission entirely.  The count
    is asserted rather than the set, so adding an eleventh site is a deliberate act
    that comes back here — which is the only way the "one predicate" property stays
    true as the wiring grows.

    ``_assembly_missing`` is deliberately **not** one of these sites (review F1): it
    exists precisely because ``_new_mode`` answers None for two different worlds, and
    it asks ``is_hierarchical`` directly to tell them apart.

    Part 2c added an eleventh, ``_record_hierarchical_stall``: when the loop goes idle
    with occurrences every gate withheld, it writes those refusals down — and asking
    the mode there is what keeps the record off a legacy Mission, whose idleness is
    the legacy scheduler's business and not this one's.

    Part 2d adds a twelfth, ``_port_claims_from``: the hierarchical Worker's envelope
    carries an ``outputs`` map naming which file went to which declared port, and
    ``ResultEnvelope`` refuses unknown keys by design — so on a *legacy* Mission that
    key has to stay an unknown field rather than being quietly accepted.  Asking the
    mode there is exactly what keeps the two contracts apart (§18.5 rule 1).

    Part 2d's stall decision adds two more: ``_stall_fingerprint`` (the identity
    of a stall, which is a hierarchical notion — it is built out of admissions, scope
    epochs and admitted demands) and ``_confirm_and_stop_stalled`` (the one place a
    Mission is ended for having nothing to dispatch).  A legacy Mission idles for the
    legacy scheduler's reasons and neither of them may touch it.

    Part 3a adds the fifteenth, ``_collect_root_review``: a ``MISSION_FINAL`` verdict
    is recorded through the hierarchical assembly's coordinator, and a legacy Mission
    has no root ``GoalResolution`` for one to feed.  ``_advance_root_review`` and
    ``_ask_root_reviewer`` are deliberately **not** extra sites — they are reached
    only from ``_decide``, which has already asked, and are handed the answer.
    """

    del tmp_path
    import inspect

    from agent_orchestrator.orchestrator import event_handler

    source = inspect.getsource(event_handler)
    assert source.count("self._new_mode(mission)") == 15
    assert "is_hierarchical(mission)" in inspect.getsource(event_handler.Orchestrator._new_mode)


def test_the_legacy_merge_call_is_still_the_only_all_ancestors_sweep(tmp_path):
    del tmp_path
    import inspect

    from agent_orchestrator.orchestrator import event_handler

    source = inspect.getsource(event_handler)
    assert source.count("merge_accepted(") == 2  # _next_attempt and _evaluate_criteria
    assert "new_mode.attempt_inputs(mission.id, task.id)" in source


# ===================================================================== inputs come from
# ===================================================================== the manifest
def test_a_task_with_no_data_requirement_starts_from_nothing(tmp_path):
    world = _committed(tmp_path)
    network = world.dispatch.network(world.mission.id)
    leaf = next(
        spec
        for spec in network.occurrences
        if spec.form is TaskForm.PRIMITIVE
        and not [
            item
            for item in network.data_requirements
            if item.consumer_occurrence == spec.occurrence_id
        ]
    )
    assert world.dispatch.attempt_inputs(world.mission.id, str(leaf.task_id)) == []


def test_a_consumer_whose_producer_has_not_finished_starts_from_nothing(tmp_path):
    world = _committed(tmp_path)
    network = world.dispatch.network(world.mission.id)
    consumers = {item.consumer_occurrence for item in network.data_requirements}
    assert consumers, "the method declares a DATA edge"
    for occurrence in consumers:
        spec = network.occurrence(occurrence)
        assert world.dispatch.attempt_inputs(world.mission.id, str(spec.task_id)) == []


def test_the_attempt_inputs_of_an_unknown_task_is_corruption(tmp_path):
    world = _committed(tmp_path)
    with pytest.raises(GraphIntegrityError):
        world.dispatch.attempt_inputs(world.mission.id, "task-nowhere")


def test_the_consumer_is_waiting_on_data_rather_than_ready(tmp_path):
    world = _committed(tmp_path, demand=True)
    view = world.dispatch.read(world.mission.id)
    consumers = {item.consumer_occurrence for item in view.network.data_requirements}
    for occurrence in consumers:
        assert view.reports[occurrence].reason is ReadinessReason.WAITING_DATA


def test_attempt_inputs_returns_the_recorded_upstream_input_shape(tmp_path):
    world = _committed(tmp_path)
    network = world.dispatch.network(world.mission.id)
    leaf = next(spec for spec in network.occurrences if spec.form is TaskForm.PRIMITIVE)
    inputs = world.dispatch.attempt_inputs(world.mission.id, str(leaf.task_id))
    assert all(isinstance(item, UpstreamInput) for item in inputs)


# ================================================================= mutation self-check
# Each entry is (a plausible wrong implementation, the assertion above that must catch
# it).  The witness is written as a small callable so the mapping between "mutant" and
# "the property it breaks" is explicit rather than implied by a large try/except pile.


def _witness_retry_is_bounded(world: World, monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale(calls, until=99))
    outcome = world.plan()
    assert not outcome.committed
    assert len(calls) == 2


def _witness_terminal_mission_is_not_retried(world: World, monkeypatch) -> None:
    world.service.cancel_mission(world.mission.id)
    calls: list[Any] = []
    real = module.compile_refinement_bundle

    def wrapper(draft, current, **kwargs):
        calls.append(current)
        return real(draft, current, **kwargs)

    monkeypatch.setattr(module, "compile_refinement_bundle", wrapper)
    world.plan()
    assert len(calls) == 1


def _witness_compound_is_intercepted(world: World, monkeypatch) -> None:
    del monkeypatch
    assert world.plan().committed
    _make_task(world, ROOT_TASK, status=TaskStatus.READY)
    intercepted = world.dispatch.intercept_worker_dispatch(world.mission.id, ROOT_TASK)
    assert intercepted is not None and intercepted.reason == "NEEDS_REFINEMENT"


def _witness_completed_rows_do_not_unlock_the_root(world: World, monkeypatch) -> None:
    del monkeypatch
    assert world.plan().committed
    for spec in world.dispatch.network(world.mission.id).occurrences:
        _make_task(world, str(spec.task_id), status=TaskStatus.COMPLETED)
    assert world.dispatch.root_review_ready(world.mission.id) is False


def _witness_phase_waits_for_children(world: World, monkeypatch) -> None:
    del monkeypatch
    assert world.plan().committed
    phases = world.dispatch.advance_compound_phases(world.mission.id)
    assert phases[OccurrenceId(ROOT_TASK)] is CompoundPhase.WAITING_CHILDREN


def _witness_missing_binding_is_corruption(world: World, monkeypatch) -> None:
    del monkeypatch
    assert world.plan().committed
    world.semantics.insert_plan_membership(
        world.mission.id,
        1,
        OccurrenceSpec(
            occurrence_id=OccurrenceId("occ-orphan"),
            task_id=TaskRef("task-orphan"),
            obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
            form=TaskForm.PRIMITIVE,
            requiredness=Requiredness.OPTIONAL_AUTHORIZED,
        ),
        instance_id=None,
        adopted=True,
    )
    with pytest.raises(GraphIntegrityError):
        world.dispatch.network(world.mission.id)


def _mutant_retry_bound_raised(monkeypatch) -> None:
    original = module.HierarchicalDispatch.__post_init__

    def patched(self) -> None:
        original(self)
        self.compile_attempts = 7

    monkeypatch.setattr(module.HierarchicalDispatch, "__post_init__", patched)


def _mutant_every_refusal_is_recompilable(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "RECOMPILABLE_REFUSALS",
        frozenset({"READ_SET_STALE", "MISSION_NOT_WRITABLE", "PRINCIPAL_MISMATCH"}),
    )


def _mutant_form_gate_off(monkeypatch) -> None:
    from agent_orchestrator.graph.eligibility import LegacyReadyVerdict

    monkeypatch.setattr(
        module,
        "legacy_ready_is_not_eligibility",
        lambda status, *, form=None: LegacyReadyVerdict(False, None, "mutant"),
    )


def _mutant_status_decides_the_root_review(monkeypatch) -> None:
    monkeypatch.setattr(
        module.HierarchicalDispatch,
        "root_review_ready",
        lambda self, mission_id: all(
            str(task.status) == "COMPLETED" for task in self.store.list_tasks(mission_id)
        ),
    )


def _mutant_review_ignores_children(monkeypatch) -> None:
    monkeypatch.setattr(
        module, "next_compound_phase", lambda *a, **k: CompoundPhase.COMPOSITION_REVIEW
    )


def _mutant_unbound_membership_is_dropped(monkeypatch) -> None:
    """The tempting wrong fix: quietly skip the occurrence whose meaning is missing."""

    original = module.HtnStore.list_plan_memberships

    def lenient(self, mission_id, revision):
        return tuple(
            spec
            for spec in original(self, mission_id, revision)
            if self.task_semantics_of(mission_id, str(spec.task_id)) is not None
        )

    monkeypatch.setattr(module.HtnStore, "list_plan_memberships", lenient)


MUTANTS = [
    ("the retry bound is raised to seven", _mutant_retry_bound_raised, _witness_retry_is_bounded),
    (
        "a terminal Mission counts as recompilable",
        _mutant_every_refusal_is_recompilable,
        _witness_terminal_mission_is_not_retried,
    ),
    (
        "the compound form gate is switched off",
        _mutant_form_gate_off,
        _witness_compound_is_intercepted,
    ),
    (
        "COMPLETED task rows decide the root review",
        _mutant_status_decides_the_root_review,
        _witness_completed_rows_do_not_unlock_the_root,
    ),
    (
        "the composition review ignores the children",
        _mutant_review_ignores_children,
        _witness_phase_waits_for_children,
    ),
    (
        "an unbound membership is quietly dropped",
        _mutant_unbound_membership_is_dropped,
        _witness_missing_binding_is_corruption,
    ),
]


@pytest.mark.parametrize(("name", "mutate", "witness"), MUTANTS, ids=[item[0] for item in MUTANTS])
def test_the_witness_assertion_passes_on_the_real_implementation(
    tmp_path, monkeypatch, name, mutate, witness
):
    del name, mutate
    witness(_world(tmp_path, key="real"), monkeypatch)


@pytest.mark.parametrize(("name", "mutate", "witness"), MUTANTS, ids=[item[0] for item in MUTANTS])
def test_each_mutant_breaks_its_witness_assertion(tmp_path, monkeypatch, name, mutate, witness):
    mutate(monkeypatch)
    with pytest.raises((AssertionError, KeyError, GraphIntegrityError, pytest.fail.Exception)):
        witness(_world(tmp_path, key="mutated"), monkeypatch)


def test_a_plan_commit_rejected_is_not_swallowed_into_a_committed_outcome(tmp_path, monkeypatch):
    """The loop must not report a commit it did not get."""

    world = _world(tmp_path)

    def _always_refuse(self, command, principal):
        raise PlanCommitRejected("READ_SET_STALE", "mutant")

    monkeypatch.setattr(CommitService, "commit_plan_revision", _always_refuse)
    outcome = world.plan()
    assert not outcome.committed and outcome.receipt is None


# ============================================ the event handler's own branch, exercised
# The wiring sites are assembly, but assembly is still behaviour: these drive the real
# ``Orchestrator`` so the settle paths, the ``PlanPrincipal`` construction, the
# COMMITTED gate and the four integrity guards are executed rather than asserted about.


def _orchestrator(tmp_path, planner_steps: Sequence[Any]) -> Orchestrator:
    config = OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
    )
    return Orchestrator(config, RoleScriptedProvider({"planner": list(planner_steps)}))


def _seed_hierarchical(orchestrator: Orchestrator, *, key: str, roots: int = 1):
    """A hierarchical Mission in one Orchestrator's store, with its declarations.

    ``roots=2`` writes a second semantic binding and no plan revision, which is the
    ``root_not_identified`` corruption: a Mission whose recorded meaning does not say
    which goal it was opened for.
    """

    mission, _ = orchestrator.commit.create_mission(_spec(key, mode=HIERARCHICAL_SEMANTICS))
    env = _env(mission.id)
    contract = _outer()
    assert env.admit(contract).admitted
    semantics = HtnStore(orchestrator.store)
    ObligationStore(orchestrator.store).register(
        Obligation(
            obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
            mission_id=mission.id,
            requirement_refs=("req-1",),
            goal_signature_id="plan.goal",
        ),
        recursion_fuel=FUEL,
    )
    for index in range(roots):
        semantics.put_task_semantics(
            mission.id,
            task_binding(
                env,
                "plan.goal",
                task_id=ROOT_TASK if index == 0 else f"{ROOT_TASK}-{index}",
                obligation=ROOT_DUTY,
                parameters={"subject": "alpha"},
            ),
        )
    semantics.register_method(contract, env.registry.registration(contract.method_ref()))
    orchestrator.install_hierarchical(planning=env)
    return mission, env, contract


def _drive(tmp_path, planner_steps: Sequence[Any], probe, *, key: str = "orch", roots: int = 1):
    """Run one Orchestrator to idle, then hand the probe the store it wrote."""

    async def case() -> None:
        async with _orchestrator(tmp_path, planner_steps) as orchestrator:
            mission, env, contract = _seed_hierarchical(orchestrator, key=key, roots=roots)
            await orchestrator.run()
            probe(orchestrator, mission, env, contract)

    asyncio.run(case())


def _events(orchestrator: Orchestrator, mission_id: str, kind: str) -> list[Any]:
    return [item for item in orchestrator.store.list_events(mission_id) if item.type == kind]


def test_the_handler_commits_a_scripted_hierarchical_planner_reply(tmp_path):
    """The reply is committed; how the cycle then ends is decision 2's business.

    Part 2d: this world has no demand admitted for its occurrences, so once the plan
    is on the board the loop goes idle with everything withheld, confirms that across
    one more cycle and ends the execution cycle with ``NO_DISPATCHABLE_WORK`` (§15: a
    Mission is a bounded cycle).  The property this test is about is the commit, so it
    asserts the commit — and that if the Mission did end, it ended for *that* reason
    and not for a planning or verification failure.
    """

    def probe(orchestrator, mission, env, contract) -> None:
        del env, contract
        assert _events(orchestrator, mission.id, PLAN_REVISION_COMMITTED), orchestrator.progress_log
        final = orchestrator.store.get_mission(mission.id)
        if final.status is MissionStatus.FAILED:
            assert final.final_report["stop_reason"] == "no_dispatchable_work"

    _drive(tmp_path, [_proposal_text(_outer())], probe)


def test_the_handler_advances_the_compound_phase_after_committing(tmp_path):
    """The commit and the phase pass are one round, not two cycles apart."""

    def probe(orchestrator, mission, env, contract) -> None:
        del env, contract
        kinds = [item.type for item in orchestrator.store.list_events(mission.id)]
        assert COMPOUND_PHASE_CHANGED in kinds
        assert kinds.index(PLAN_REVISION_COMMITTED) < kinds.index(COMPOUND_PHASE_CHANGED)

    _drive(tmp_path, [_proposal_text(_outer())], probe)


def test_the_handler_settles_the_planner_intent_after_a_commit(tmp_path):
    def probe(orchestrator, mission, env, contract) -> None:
        del env, contract
        open_intents = orchestrator.store.list_intents(
            "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
        )
        assert [item for item in open_intents if item.mission_id == mission.id] == []

    _drive(tmp_path, [_proposal_text(_outer())], probe)


def test_a_malformed_block_reaches_the_planning_rejection_with_its_repair_hint(tmp_path):
    bad = "<plan_revision_proposal>{ this is not json </plan_revision_proposal>"

    def probe(orchestrator, mission, env, contract) -> None:
        del env, contract
        rejections = _events(orchestrator, mission.id, "PlanningRejected")
        assert rejections, orchestrator.progress_log
        assert rejections[0].payload["reason"] == "proposal_unreadable"
        detail = rejections[0].payload["detail"]
        assert detail["block_defect"] == "invalid_json"
        assert "plan_revision_proposal" in detail["repair_hint"]

    _drive(tmp_path, [bad, bad], probe, key="orch-bad")


def test_a_malformed_block_rides_the_existing_attempt_ladder(tmp_path):
    """§18.5 C8: bounded repair, no extra request — two scripted turns, then a stop."""

    bad = "<plan_revision_proposal>{ nope </plan_revision_proposal>"

    def probe(orchestrator, mission, env, contract) -> None:
        del env, contract
        assert len(_events(orchestrator, mission.id, "PlanningRejected")) == 2
        assert orchestrator.store.get_mission(mission.id).status is MissionStatus.FAILED

    _drive(tmp_path, [bad, bad], probe, key="orch-bad2")


def test_a_refused_plan_commit_reaches_the_planning_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "compile_refinement_bundle", _stale([], until=99))

    def probe(orchestrator, mission, env, contract) -> None:
        del env, contract
        assert _events(orchestrator, mission.id, PLAN_COMMIT_REFUSED)
        rejections = _events(orchestrator, mission.id, "PlanningRejected")
        assert rejections and rejections[0].payload["reason"] == "plan_commit_refused"
        assert rejections[0].payload["detail"]["reason"] == "READ_SET_STALE"

    _drive(tmp_path, [_proposal_text(_outer())] * 2, probe, key="orch-refused")


def test_a_damaged_plan_stops_that_mission_through_the_planner_branch(tmp_path):
    """Two root bindings and no plan revision: there is no root to seed a network from."""

    def probe(orchestrator, mission, env, contract) -> None:
        del env, contract
        integrity = _events(orchestrator, mission.id, PLAN_INTEGRITY_FAILED)
        assert integrity, [item.type for item in orchestrator.store.list_events(mission.id)]
        assert integrity[0].payload["code"] == "root_not_identified"
        assert "cycle" not in integrity[0].payload["diagnose"]
        assert orchestrator.store.get_mission(mission.id).status is MissionStatus.FAILED
        # Corruption is not a bad proposal: the Planner is not asked again.
        assert _events(orchestrator, mission.id, "PlanningRejected") == []

    _drive(tmp_path, [_proposal_text(_outer())], probe, key="orch-damaged", roots=2)


class _UncommittedTurn:
    """A planner turn that did not commit; the shape ``_collect_plan`` is handed."""

    state = AgentTurnState.FAILED
    turn_id = "turn-uncommitted"
    error = {"kind": "transport", "retryable": True}
    public_output = None
    usage_refs: tuple[Any, ...] = ()


def _planner_intent(orchestrator: Orchestrator, mission_id: str):
    return next(
        item
        for item in orchestrator.store.list_intents(
            "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
        )
        if item.mission_id == mission_id
    )


def test_a_turn_that_did_not_commit_is_a_planning_rejection(tmp_path):
    """The COMMITTED gate of the new branch, executed rather than read."""

    async def case() -> None:
        async with _orchestrator(tmp_path, []) as orchestrator:
            mission, _env, _contract = _seed_hierarchical(orchestrator, key="orch-nocommit")
            orchestrator.commit.begin_planning(mission.id)
            await orchestrator._try_planner_intent(mission.id, ordinal=1)
            current = orchestrator.store.get_mission(mission.id)
            new_mode = orchestrator._new_mode(current)
            assert new_mode is not None
            await orchestrator._collect_plan_hierarchical(
                _planner_intent(orchestrator, mission.id),
                _UncommittedTurn(),
                current,
                "",
                new_mode,
            )
            rejections = _events(orchestrator, mission.id, "PlanningRejected")
            assert rejections and rejections[0].payload["reason"] == "proposal_unreadable"
            assert "planner turn failed" in rejections[0].payload["detail"]["error"]

    asyncio.run(case())


def _force_active(orchestrator: Orchestrator, mission_id: str):
    """Take a hierarchical Mission from PLANNING to ACTIVE without a legacy graph.

    P2.3b commits plan revisions but writes no legacy Task rows, so nothing moves the
    Mission to ACTIVE yet (that bridge is P2.3c's).  The two guards below are about
    what happens *while* ACTIVE, so the transition is made explicitly here rather
    than asserted into existence.
    """

    from agent_orchestrator.orchestrator.state_machine import next_mission

    mission = orchestrator.store.get_mission(mission_id)
    if mission.status is MissionStatus.FAILED:
        # Part 2d: an idle hierarchical cycle now *ends* (``NO_DISPATCHABLE_WORK``),
        # so a guard about what happens while ACTIVE has to put it back there.  §25.1
        # has no FAILED→ACTIVE edge — correctly — so the row is rewritten directly
        # rather than transitioned; this is a test putting a world back, not the
        # product reviving a Mission.
        assert mission.final_report["stop_reason"] == "no_dispatchable_work"
        revived = dataclasses.replace(
            mission,
            status=MissionStatus.ACTIVE,
            stop_reason=None,
            version=mission.version + 1,
        )
        orchestrator.store.update_mission(revived, expected_version=mission.version)
        return revived
    if mission.status is MissionStatus.PLANNING:
        updated = next_mission(mission, MissionStatus.ACTIVE)
        orchestrator.store.update_mission(updated, expected_version=mission.version)
        return updated
    return mission


def _corrupt_plan(store: Store, mission_id: str) -> None:
    """Add a plan member whose task has no semantic binding (§18.5 corruption)."""

    semantics = HtnStore(store)
    revision = int(semantics.active_plan_revision(mission_id).revision)
    semantics.insert_plan_membership(
        mission_id,
        revision,
        OccurrenceSpec(
            occurrence_id=OccurrenceId("occ-orphan"),
            task_id=TaskRef("task-orphan"),
            obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
            form=TaskForm.PRIMITIVE,
            requiredness=Requiredness.OPTIONAL_AUTHORIZED,
        ),
        instance_id=None,
        adopted=True,
    )


def _insert_display_task(store: Store, mission_id: str, task_id: str) -> Task:
    task = Task(
        id=task_id,
        mission_id=mission_id,
        parent_task_ids=(),
        dependency_ids=(),
        goal=f"goal of {task_id}",
        rationale="display row",
        success_criteria=("file:a.md",),
        verification_policy=("format_check",),
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=1_000, max_attempts=2),
        priority=1.0,
        status=TaskStatus.READY,
        version=1,
    )
    store.insert_task(task, ordinal=1 + len(store.list_tasks(mission_id)))
    return task


def test_the_decide_branch_stops_one_mission_on_a_damaged_plan(tmp_path):
    """Guard 3: the terminal judgement never raises out of the shared loop."""

    async def case() -> None:
        async with _orchestrator(tmp_path, [_proposal_text(_outer())]) as orchestrator:
            mission, _env, _contract = _seed_hierarchical(orchestrator, key="orch-decide")
            await orchestrator.run()
            assert HtnStore(orchestrator.store).active_plan_revision(mission.id) is not None
            _corrupt_plan(orchestrator.store, mission.id)
            _insert_display_task(orchestrator.store, mission.id, "task-display")
            active = _force_active(orchestrator, mission.id)
            assert await orchestrator._decide(active) is True
            integrity = _events(orchestrator, mission.id, PLAN_INTEGRITY_FAILED)
            assert integrity and integrity[0].payload["code"] == "semantic_binding_missing"
            assert orchestrator.store.get_mission(mission.id).status is MissionStatus.FAILED

    asyncio.run(case())


def test_the_dispatch_branch_stops_one_mission_on_a_missing_binding(tmp_path):
    """Guard 4: a Task with no meaning stops the Mission, it does not end the run."""

    async def case() -> None:
        async with _orchestrator(tmp_path, [_proposal_text(_outer())]) as orchestrator:
            mission, _env, _contract = _seed_hierarchical(orchestrator, key="orch-dispatch")
            await orchestrator.run()
            task = _insert_display_task(orchestrator.store, mission.id, "task-unbound")
            active = _force_active(orchestrator, mission.id)
            assert await orchestrator._next_attempt(active, task, []) is True
            integrity = _events(orchestrator, mission.id, PLAN_INTEGRITY_FAILED)
            assert integrity and integrity[0].payload["code"] == "semantic_binding_missing"
            assert orchestrator.store.get_mission(mission.id).status is MissionStatus.FAILED

    asyncio.run(case())


def _planner_by_goal(*, hierarchical: str, legacy: str):
    """One planner script for two Missions: pick the reply that fits the package.

    Two Missions in one run share the provider, and which of them the loop drives
    first is a scheduling fact.  A positional script would hand one Mission the
    other's proposal, so the step reads the package it was actually given.
    """

    def step(request: Any) -> str:
        text = " ".join(
            message.content if isinstance(message.content, str) else str(message.content)
            for message in request.messages
        )
        return hierarchical if HIERARCHICAL_GOAL in text else legacy

    return step


def test_one_damaged_mission_does_not_take_another_down_with_it(tmp_path):
    """The whole point of the guards: ``run()`` survives one Mission's corruption.

    ``GraphIntegrityError`` is a ``RuntimeError`` and ``_cycle`` does not forgive
    those, so before the guards this configuration ended the run and the healthy
    Mission never finished.
    """

    async def case() -> None:
        provider = demo_single_task_provider()
        provider.scripts["planner"] = [
            _planner_by_goal(
                hierarchical=_proposal_text(_outer()), legacy=proposal_step(DEMO_PROPOSAL)
            )
            for _ in range(8)
        ]
        config = OrchestratorConfig(
            evidence_root=Path(tmp_path) / "evidence", max_concurrency=2, test_timeout_seconds=60
        )
        async with Orchestrator(config, provider) as orchestrator:
            damaged, _env, _contract = _seed_hierarchical(orchestrator, key="pair-damaged", roots=2)
            healthy = await orchestrator.submit_mission(
                MissionSpec(
                    goal=str(DEMO_PROPOSAL["root_goal"]),
                    success_criteria=tuple(DEMO_PROPOSAL["success_criteria"]),
                    tenant_id="tenant-p23b-pair",
                    idempotency_key="pair-healthy",
                    allowed_tools=tuple(DEMO_PROPOSAL["allowed_tools"]),
                    budget=Budget(max_tokens=200_000, max_attempts=12),
                    workspace_seed=DEMO_SEED,
                )
            )
            await orchestrator.run(max_cycles=400)
            assert orchestrator.store.get_mission(healthy.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            assert orchestrator.store.get_mission(damaged.id).status is MissionStatus.FAILED
            assert _events(orchestrator, damaged.id, PLAN_INTEGRITY_FAILED)
            assert _events(orchestrator, healthy.id, PLAN_INTEGRITY_FAILED) == []

    asyncio.run(case())


# ================================================== the carried-integrity reading path
def test_the_default_read_of_a_damaged_plan_raises(tmp_path):
    world = _committed(tmp_path)
    _corrupt_plan(world.store, world.mission.id)
    with pytest.raises(GraphIntegrityError):
        world.dispatch.read(world.mission.id)


def test_the_tolerant_read_carries_the_error_instead_of_raising(tmp_path):
    world = _committed(tmp_path)
    _corrupt_plan(world.store, world.mission.id)
    view = world.dispatch.read(world.mission.id, tolerate_integrity=True)
    assert isinstance(view.plan.integrity_error, PlanIntegrityError)
    assert view.plan.integrity_error.code == "semantic_binding_missing"


def test_every_occurrence_of_a_damaged_plan_reports_graph_integrity(tmp_path):
    world = _committed(tmp_path)
    _corrupt_plan(world.store, world.mission.id)
    view = world.dispatch.read(world.mission.id, tolerate_integrity=True)
    assert view.reports
    assert {report.reason for report in view.reports.values()} == {ReadinessReason.GRAPH_INTEGRITY}


def test_both_frontiers_of_a_damaged_plan_are_empty(tmp_path):
    world = _committed(tmp_path)
    _corrupt_plan(world.store, world.mission.id)
    view = world.dispatch.read(world.mission.id, tolerate_integrity=True)
    assert view.execution_frontier.occurrences == ()
    assert view.planning_frontier.occurrences == ()


def test_the_missing_binding_message_does_not_talk_about_a_cycle(tmp_path):
    world = _committed(tmp_path)
    _corrupt_plan(world.store, world.mission.id)
    with pytest.raises(GraphIntegrityError) as caught:
        world.dispatch.network(world.mission.id)
    assert "cycle" not in str(caught.value)
    assert "cycle" not in caught.value.diagnose()
    assert "task-orphan" in str(caught.value)


def test_the_plan_integrity_error_reports_no_topological_order(tmp_path):
    """TG §14.3: a damaged projection's healthy prefix is not handed out as a plan."""

    world = _committed(tmp_path)
    _corrupt_plan(world.store, world.mission.id)
    with pytest.raises(GraphIntegrityError) as caught:
        world.dispatch.network(world.mission.id)
    assert caught.value.cycle == ()
    assert not hasattr(caught.value, "order")
