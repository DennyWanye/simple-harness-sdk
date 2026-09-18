# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3k / defect N4: a read-only leaf does not carry the ``code_test`` layer.

Grok C3-r0 / C3-r1: the ``facts`` and ``reproduce`` leaves — ``side_effect_kind =
external_read``, ``repo.read`` / ``tests.run`` only — each failed their first Attempt
with ``VerificationFailed(code_test: 1 failed, 1 passed)``: the layer ran the whole
suite on a workspace whose baseline is red by construction, and the model then had to
patch ``stats/window.py`` inside a *read-only* leaf to get through.  C1-r1's
``reproduce`` leaf was sent back the same way for the red reproduction test it had
itself written.  Two extra Attempts and roughly a third more tokens per episode, for a
check that can only measure the patch step's work.

``occurrence_policy`` copied the system default (``format_check, rule_check,
code_test``) onto every occurrence.  It now reads the leaf's binding: a leaf whose
type declares a read-only side effect, no write capability and no resource writes is
not given ``code_test`` — unless one of its own criteria names a ``pytest:`` target,
in which case the criterion nobody would check wins, as before.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_deployment_wiring import _both_lane_world, _task_of  # noqa: E402

from agent_orchestrator.contracts.htn import (  # noqa: E402
    GoalSignature,
    ObligationId,
    ResourceRef,
    SideEffectKind,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from agent_orchestrator.contracts.models import STEP2_IMPLEMENTED_LAYERS  # noqa: E402
from agent_orchestrator.contracts.semantic_base import VersionedRef  # noqa: E402
from agent_orchestrator.orchestrator.occurrence_tasks import (  # noqa: E402
    occurrence_policy,
    read_only_leaf,
)

DEPLOYED = frozenset(STEP2_IMPLEMENTED_LAYERS)


def _binding(
    *,
    side_effect: SideEffectKind | None,
    capabilities: tuple[str, ...] = ("repo.read",),
    writes: tuple[ResourceRef, ...] = (),
) -> TaskSemanticBindingV1:
    return TaskSemanticBindingV1(
        task_id=TaskRef("task-leaf"),
        obligation_id=ObligationId("obl-leaf"),
        contract_revision=1,
        contract_hash="0" * 64,
        form=TaskForm.PRIMITIVE,
        goal_signature=GoalSignature(
            signature_id="code.read-repository-facts",
            version=1,
            parameter_schema_ref=VersionedRef(
                id="code.repository-only", version=1, content_hash="a" * 64
            ),
            output_schema_ref=VersionedRef(id="code.outputs", version=1, content_hash="b" * 64),
            statement="read",
            coverage_criteria=(),
        ),
        operator_ref=VersionedRef(
            id="code.op-read-repository-facts", version=1, content_hash="c" * 64
        ),
        capability_requirements=capabilities,
        resource_writes=writes,
        side_effect_kind=side_effect,
    )


# ======================================================================================
# 1. The rule itself
# ======================================================================================


def test_a_read_only_leaf_is_not_given_code_test() -> None:
    assert "code_test" not in occurrence_policy(("c-facts",), DEPLOYED, read_only=True)
    assert occurrence_policy(("c-facts",), DEPLOYED, read_only=True) == (
        "format_check",
        "rule_check",
    )


def test_a_writing_leaf_keeps_the_system_default() -> None:
    assert occurrence_policy(("c-patch",), DEPLOYED) == ("format_check", "rule_check", "code_test")
    assert occurrence_policy(("c-patch",), DEPLOYED, read_only=False) == (
        "format_check",
        "rule_check",
        "code_test",
    )


def test_a_pytest_criterion_on_a_read_only_leaf_still_runs_code_test() -> None:
    """A criterion nobody would check is worse than a redundant layer (host 0.9.8)."""

    assert "code_test" in occurrence_policy(
        ("pytest:tests/test_x.py",), DEPLOYED, read_only=True
    )


def test_a_declared_policy_is_narrowed_but_not_rewritten_for_a_read_only_leaf() -> None:
    """A deployment that states a policy said what it meant; the rule only drops the
    layer it would otherwise have added by default."""

    assert occurrence_policy(
        ("c-facts",), DEPLOYED, ("format_check", "code_test"), read_only=True
    ) == ("format_check", "code_test")


# ======================================================================================
# 2. What makes a leaf read-only, read off its binding
# ======================================================================================


def test_read_only_is_side_effect_plus_no_write_capability_plus_no_resource_writes() -> None:
    assert read_only_leaf(_binding(side_effect=SideEffectKind.EXTERNAL_READ)) is True
    assert read_only_leaf(_binding(side_effect=SideEffectKind.NONE)) is True
    assert read_only_leaf(_binding(side_effect=SideEffectKind.LOCAL_WRITE)) is False
    assert read_only_leaf(_binding(side_effect=SideEffectKind.EXTERNAL_STATE_WRITE)) is False
    assert (
        read_only_leaf(
            _binding(side_effect=SideEffectKind.EXTERNAL_READ, capabilities=("repo.write",))
        )
        is False
    )
    assert (
        read_only_leaf(
            _binding(
                side_effect=SideEffectKind.EXTERNAL_READ,
                writes=(ResourceRef(namespace="repo", object_id="workspace"),),
            )
        )
        is False
    )


def test_a_binding_that_declares_no_side_effect_is_not_assumed_read_only() -> None:
    """Silence is not a declaration: a leaf whose type said nothing keeps the default."""

    assert read_only_leaf(_binding(side_effect=None)) is False


# ======================================================================================
# 3. The shipped code domain, materialised for real (the C3 plan)
# ======================================================================================


def test_the_c3_plans_read_only_leaves_are_materialised_without_code_test(tmp_path) -> None:
    service, mission, _semantics, _world, dispatch = _both_lane_world(tmp_path)
    tasks = {task.id: task for task in service.store.list_tasks(mission.id)}
    policies = {
        step: tasks[_task_of(dispatch, mission.id, type_id)].verification_policy
        for step, type_id in (
            ("facts", "code.read-repository-facts"),
            ("reproduce", "code.reproduce-failure"),
            ("patch", "code.apply-patch"),
            ("verify", "code.verify-tests"),
        )
    }
    for step in ("facts", "reproduce"):
        assert "code_test" not in policies[step], (step, policies[step])
        assert policies[step] == ("format_check", "rule_check"), (step, policies[step])
    assert policies["patch"] == ("format_check", "rule_check", "code_test")
    # Verification P1-2: ``verify`` is ``external_read`` too, but it is the step the
    # plan's criterion_links point at — the deterministic layer stays on it.
    assert policies["verify"] == ("format_check", "rule_check", "code_test")


def test_a_criterion_linked_leaf_keeps_code_test_whatever_its_side_effect_says() -> None:
    from agent_orchestrator.contracts import Budget, Mission, MissionStatus
    from agent_orchestrator.contracts.htn import OccurrenceId, OccurrenceSpec
    from agent_orchestrator.orchestrator.occurrence_tasks import occurrence_task

    mission = Mission(
        id="mission-x",
        tenant_id="t",
        goal="g",
        success_criteria=("c",),
        status=MissionStatus.PLANNING,
        allowed_tools=(),
        budget=Budget(max_tokens=1000),
        idempotency_key="k",
        version=1,
        stop_conditions=(),
        risk_level="sandbox",
        created_at=0.0,
    )
    import dataclasses

    binding = dataclasses.replace(
        _binding(side_effect=SideEffectKind.EXTERNAL_READ, capabilities=("tests.run",)),
        requirement_refs=("c-green",),
    )
    spec = OccurrenceSpec(
        occurrence_id=OccurrenceId("occ-verify"),
        task_id=TaskRef("task-leaf"),
        obligation_id=ObligationId("obl-leaf"),
        form=TaskForm.PRIMITIVE,
    )
    common = dict(plan_revision=1, budget=Budget(max_tokens=100), ordinal=1, deployed=DEPLOYED)
    plain = occurrence_task(mission, spec, binding, **common).task.verification_policy
    linked = occurrence_task(
        mission, spec, binding, criterion_linked=True, **common
    ).task.verification_policy
    assert plain == ("format_check", "rule_check")
    assert linked == ("format_check", "rule_check", "code_test")


def test_the_task_committed_proposal_says_the_same(tmp_path) -> None:
    """The durable record a Worker's verification is later read from (C3's
    ``TaskCommitted.proposal.verification_policy`` carried ``code_test`` on every leaf)."""

    service, mission, _semantics, _world, dispatch = _both_lane_world(tmp_path)
    facts = _task_of(dispatch, mission.id, "code.read-repository-facts")
    patch = _task_of(dispatch, mission.id, "code.apply-patch")
    committed = {
        event.task_id: event.payload["proposal"]["verification_policy"]
        for event in service.store.list_events(mission.id)
        if event.type == "TaskCommitted"
    }
    assert "code_test" not in committed[facts]
    assert "code_test" in committed[patch]


# ======================================================================================
# 4. Verification P1-2: the declaration is enforced where the files come in
# ======================================================================================


class _File:
    def __init__(self, path: str, content_hash: str) -> None:
        self.path = path
        self.content_hash = content_hash


def test_read_only_rewrites_names_a_changed_starting_file_and_nothing_else() -> None:
    from agent_orchestrator.orchestrator.occurrence_tasks import read_only_rewrites

    binding = _binding(side_effect=SideEffectKind.EXTERNAL_READ)
    initial = {"stats/window.py": "a" * 64, "README.md": "b" * 64, "tests/t.py": "c" * 64}
    artifacts = [
        _File("stats/window.py", "f" * 64),  # changed: the C3 facts leaf's write
        _File("README.md", "b" * 64),  # unchanged, merely cited
        _File("facts.json", "d" * 64),  # new: the leaf's own output
        _File("tests/t.py", "e" * 64),  # changed but guarded: reported elsewhere
    ]
    assert read_only_rewrites(binding, artifacts, initial, guarded=("tests/t.py",)) == [
        "stats/window.py"
    ]
    assert read_only_rewrites(binding, artifacts, initial) == ["stats/window.py", "tests/t.py"]


def test_read_only_rewrites_is_empty_for_a_writing_leaf_or_no_change() -> None:
    from agent_orchestrator.orchestrator.occurrence_tasks import read_only_rewrites

    initial = {"stats/window.py": "a" * 64}
    changed = [_File("stats/window.py", "f" * 64)]
    writer = _binding(side_effect=SideEffectKind.LOCAL_WRITE)
    assert read_only_rewrites(writer, changed, initial) == []
    assert (
        read_only_rewrites(
            _binding(side_effect=SideEffectKind.EXTERNAL_READ, capabilities=("repo.write",)),
            changed,
            initial,
        )
        == []
    )
    assert read_only_rewrites(_binding(side_effect=None), changed, initial) == []
    assert (
        read_only_rewrites(
            _binding(side_effect=SideEffectKind.EXTERNAL_READ),
            [_File("stats/window.py", "a" * 64), _File("REPORT.md", "9" * 64)],
            initial,
        )
        == []
    )


SEED = {
    "stats/window.py": (
        "def window_sum(values, start, end):\n    return sum(values[start:end - 1])\n"
    )
}
TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list")


def _collect_facts_leaf(
    tmp_path,
    *,
    key: str,
    writes: list[tuple[str, str]],
    artifacts: list[str],
    mutate: Any | None = None,
):
    """Dispatch the C1-r1 method's ``read-facts`` leaf to a scripted Worker that writes
    ``writes`` and submits ``artifacts`` with ``facts.json`` claimed at its port, then
    collect the result through the real ``_collect_attempt``.

    ``mutate(loop, intent)`` runs after the Worker returns and before collection, so a
    test can change workspace bytes without going through the write tool (P2.3u
    fallback).
    """

    import asyncio

    from test_inspect_leaf_patch_input import _c1_method, _CodeWorld, _rebound

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.storage.htn_store import HtnStore
    from agent_orchestrator.testing.fixtures import RoleScriptedProvider, envelope_step

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _CodeWorld(
        evidence,
        method=_rebound(_c1_method()),
        key=key,
        db_name="orchestrator.db",
        allowed_tools=TOOLS,
        workspace_seed=SEED,
    )
    facts = world.task("code.read-repository-facts")
    world.store.close()
    steps: list[Any] = [
        ("workspace_write_file", {"path": path, "content": text}) for path, text in writes
    ]
    steps.append(
        envelope_step(
            summary="repository facts",
            artifacts=artifacts,
            claims=["facts recorded"],
            override=lambda body: {**body, "outputs": {"facts": "facts.json"}},
        )
    )
    provider = RoleScriptedProvider({"worker": steps})
    config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1, test_timeout_seconds=5)

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, provider) as loop:
            world.world.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.world)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            await loop._decide(mission)
            intent = next(
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if item.mission_id == mission.id
                and item.kind == "attempt"
                and str(item.subject_id).startswith(facts)
            )
            assert await loop._dispatch(intent)
            intent = loop.store.get_intent(intent.intent_id)

            async def completed():
                while True:
                    result = await loop.bridge_for(intent).result(
                        agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                    )
                    if result is not None:
                        return result
                    await asyncio.sleep(0.01)

            result = await asyncio.wait_for(completed(), timeout=10)
            if mutate is not None:
                mutate(loop, intent)
            await loop._collect_attempt(intent, result)
            events = loop.store.list_events(mission.id)
            return {
                "rejections": [e.payload for e in events if e.type == "ResultRejected"],
                "submitted": [e.type for e in events if e.type == "ResultSubmitted"],
                "artifacts": sorted(a.path for a in loop.store.list_mission_artifacts(mission.id)),
            }

    return asyncio.run(case())


def test_a_read_only_leaf_that_rewrites_a_seed_file_is_refused_at_collection(tmp_path) -> None:
    """C3's ``facts`` leaf, replayed at collection: bytes on ``stats/window.py``
    changed outside the write tool (P2.3u's gateway now blocks the tool itself).
    The result is refused with the reason written down; nothing is registered."""

    rewritten = (
        "def window_sum(values, start, end):\n    return sum(values[start:end])\n"
    )

    def mutate(loop: Any, intent: Any) -> None:
        workspace = loop.assembled.workspaces.get(str(intent.config["attempt_id"]))
        workspace.write_text("stats/window.py", rewritten)

    outcome = _collect_facts_leaf(
        tmp_path,
        key="p23k-p12-rewrite",
        writes=[
            ("facts.json", '{"tests": ["tests/test_public_window.py"]}'),
        ],
        artifacts=["stats/window.py", "facts.json"],
        mutate=mutate,
    )
    assert outcome["rejections"], outcome
    last = outcome["rejections"][-1]
    assert last["reason"] == "read_only_leaf_rewrote_workspace"
    assert last["detail"]["paths"] == ["stats/window.py"]
    assert last["detail"]["side_effect_kind"] == "external_read"
    assert outcome["submitted"] == []
    assert outcome["artifacts"] == []


def test_a_read_only_leaf_that_only_adds_its_outputs_is_collected(tmp_path) -> None:
    """The control: new files (its port output, a report) are how a read-only leaf
    delivers; nothing it started from changed, so the result goes through."""

    outcome = _collect_facts_leaf(
        tmp_path,
        key="p23k-p12-clean",
        writes=[
            ("facts.json", '{"tests": ["tests/test_public_window.py"]}'),
            ("REPORT.md", "# facts\n\nread only\n"),
        ],
        artifacts=["facts.json", "REPORT.md"],
    )
    assert [item["reason"] for item in outcome["rejections"]] == []
    assert outcome["submitted"] == ["ResultSubmitted"]
    assert outcome["artifacts"] == ["REPORT.md", "facts.json"]
