# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-D red tests: the planning-decision-v1 shape of the hierarchical Planner package.

V2 §18/§19/§38/§39 plus the ruling addendum §1/§7: when — and only when — the caller
passes ``planning_protocol="planning-decision-v1"`` the package grows, on top of what
it already carried,

* ``planning_protocol`` — the protocol name plus the H1-executable decision
  types;
* ``planning_subjects`` — the per-request subject keys the model may name (§19);
* ``visible_refs`` + ``visible_refs_omitted`` — the §17 quadruples the model's
  references must byte-match, capped at 128 with the omitted count recorded;
* ``previous_feedback`` — ``PlanningFeedbackV1.to_json()`` or ``null``;
* ``decision_limits`` — the §16 limits.

``output_contract`` becomes ``<planning_decision>`` and the in-package string label
becomes ``planner-package-hierarchical-v5``.

The old protocol is *byte-identical*: two packages built without the flag are pinned
here by the canonical-JSON SHA-256 of what the previous build produced, measured
before a single line of the new shape was written.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_htn_end_to_end as e2e  # noqa: E402

from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.contracts.planning_decisions import (  # noqa: E402
    H1_DECISION_ENABLEMENT,
    MAX_PD_ALTERNATIVES,
    MAX_PD_ARGUMENTS,
    MAX_PD_ASSUMPTIONS,
    MAX_PD_BINDINGS,
    MAX_PD_BLOCKERS,
    MAX_PD_HUMAN_OPTIONS,
    MAX_PD_RATIONALE_CHARS,
    MAX_PD_REASON_REFS,
    MAX_PD_REPLAN_TRIGGERS,
    MAX_PD_UNCERTAINTIES,
    MAX_PD_WAIT_REFS,
    PLANNING_DECISION_V1,
    PlanningFeedbackV1,
    PlanningRefKind,
    PlanningRefV1,
    PlanningRetryBudgetView,
)
from agent_orchestrator.contracts.semantic_base import content_hash_of  # noqa: E402
from agent_orchestrator.planning.htn.planner_package import (  # noqa: E402
    HIERARCHICAL_DECISION_PACKAGE_VERSION,
    HIERARCHICAL_PACKAGE_VERSION,
    MAX_VISIBLE_REFS,
    hierarchical_planner_package,
    package_hash,
    subject_bindings_hash,
    visible_refs_digest,
    visible_refs_from_hierarchical_package,
    visible_refs_omitted,
)

#: Canonical-JSON SHA-256 of the package the *previous* build produced, measured on
#: the unmodified module before the decision shape existed: one deterministic fixture
#: world (``build_world(key="p23c-pkg")``) and one stub world with no registry, no
#: occurrences and no observations.
GOLDEN_FIXTURE_WORLD_SHA256 = "a9aa2e7e715596ebbec79ac3f319530ab6675272b6808a8a6e3d8f1e59ba42fd"
GOLDEN_STUB_WORLD_SHA256 = "801b8e3934fd5a77c347385a13d467157bc3e5f325d85faafc5c84877293e9d0"

#: The §32 structural system fields the model is forbidden to write.  The existing
#: package already exposes ``mission.mission_id`` and ``plan.plan_revision`` *nested*
#: — unchanged by this slice — but no *new top-level* field may be one of these.
SYSTEM_BOUND_FIELDS = frozenset(
    {
        "mission_id",
        "tenant_id",
        "principal",
        "principal_id",
        "scope",
        "scope_id",
        "manager_epoch",
        "budget_account",
        "budget_grant_revision",
        "registry_status",
        "opened_by",
        "authorization_ref",
        "grant_ref",
        "provenance",
        "authored_by",
        "dispatch_generation",
        "plan_revision",
        "expected_plan_revision",
        "operation_id",
        "acceptance_id",
        "approval_id",
        "decision_id",
        "request_id",
    }
)

DECISION_ONLY_FIELDS = frozenset(
    {
        "planning_protocol",
        "planning_subjects",
        "authoritative_refs",
        "visible_refs",
        "visible_refs_omitted",
        "previous_feedback",
        "decision_limits",
    }
)


class _Budget:
    def to_json(self) -> dict[str, Any]:
        return {
            "max_tokens": 1000,
            "max_cost_micros": None,
            "max_attempts": 3,
            "max_runtime_seconds": None,
            "max_concurrency": None,
            "max_tool_calls": None,
        }


class _Mission:
    id = "mission-stub"
    goal = "stub goal"
    success_criteria = ("file:a.md",)
    allowed_tools = ("read",)
    risk_level = "low"
    budget = _Budget()


class _Network:
    """The smallest thing the packager reads: a plan with nothing on it."""

    plan_revision = 0
    root_occurrence_ids: tuple[str, ...] = ()
    required_obligations: tuple[str, ...] = ()
    occurrences: tuple[Any, ...] = ()
    task_bindings: tuple[Any, ...] = ()

    def adopted_instance_for(self, occurrence_id: Any) -> Any:
        return None


def stub_package(**kwargs: Any) -> dict[str, Any]:
    return hierarchical_planner_package(_Mission(), _Network(), registry=None, **kwargs)


def decision_package(**kwargs: Any) -> dict[str, Any]:
    return stub_package(planning_protocol=PLANNING_DECISION_V1, **kwargs)


def empty_package(**sections: Any) -> dict[str, Any]:
    """A package skeleton carrying only the sections the collector reads."""

    package: dict[str, Any] = {
        "plan": {
            "plan_revision": 0,
            "root_occurrences": [],
            "open_compound_goals": [],
            "committed_primitives": [],
            "required_obligations": [],
        },
        "method_library": [],
        "applicability": [],
        "rejected_refinements": [],
        "facts": [],
        "accepted_results": [],
        "authoritative_refs": [],
    }
    for name, value in sections.items():
        if name in {"open_compound_goals", "committed_primitives"}:
            package["plan"][name] = value
        else:
            package[name] = value
    return package


def authority(kind: str, id: str, revision: int, digest: str) -> dict[str, Any]:
    """One sidecar quadruple: an authoritative (§5.1) digest for a task/obligation."""

    return {
        "kind": kind,
        "id": id,
        "semantic_revision": revision,
        "content_hash": digest,
    }


def by_key(package: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (item["kind"], item["id"]): dict(item)
        for item in visible_refs_from_hierarchical_package(package)
    }


class _WideBinding:
    """Enough of ``TaskSemanticBindingV1`` for the packager: id, revision, digest."""

    def __init__(self, task_id: str, obligation_id: str, digest: str) -> None:
        self.task_id = task_id
        self.obligation_id = obligation_id
        self.contract_revision = 1
        self.contract_hash = digest
        self.goal_signature = _Signature()
        self.typed_parameters: dict[str, Any] = {}
        self.requirement_refs: tuple[str, ...] = ()
        self.capability_requirements: tuple[str, ...] = ()

    def content_hash(self) -> str:
        return self.contract_hash


class _Signature:
    signature_id = "wide.signature"
    statement = "a wide goal"


class _Occurrence:
    def __init__(self, occurrence_id: str, task_id: str, obligation_id: str) -> None:
        from agent_orchestrator.contracts.htn import TaskForm

        self.occurrence_id = occurrence_id
        self.task_id = task_id
        self.obligation_id = obligation_id
        self.form = TaskForm.COMPOUND
        self.requiredness = "required"


class _WideNetwork:
    """A network with ``count`` compound goals, so the 128 cap is reachable."""

    plan_revision = 0
    root_occurrence_ids: tuple[str, ...] = ()
    required_obligations: tuple[str, ...] = ()

    def __init__(self, count: int) -> None:
        self._by_occurrence: dict[str, _WideBinding] = {}
        occurrences: list[_Occurrence] = []
        bindings: list[_WideBinding] = []
        for index in range(count):
            task_id = f"task-{index:03d}"
            occurrence_id = f"occ-{index:03d}"
            digest = hex_digest(index + 1)
            binding = _WideBinding(task_id, f"obl-{index:03d}", digest)
            self._by_occurrence[occurrence_id] = binding
            bindings.append(binding)
            occurrences.append(_Occurrence(occurrence_id, task_id, f"obl-{index:03d}"))
        self.occurrences = tuple(occurrences)
        self.task_bindings = tuple(bindings)

    def adopted_instance_for(self, occurrence_id: str) -> Any:
        return None

    def binding_for_occurrence(self, occurrence_id: str) -> _WideBinding:
        return self._by_occurrence[occurrence_id]


def wide_package(count: int, **kwargs: Any) -> dict[str, Any]:
    return hierarchical_planner_package(
        _Mission(),
        _WideNetwork(count),
        registry=None,
        planning_protocol=PLANNING_DECISION_V1,
        **kwargs,
    )


def obligation_authorities(count: int) -> list[dict[str, Any]]:
    return [
        authority("obligation", f"obl-{index:03d}", 1, hex_digest(index + 1))
        for index in range(count)
    ]


def hex_digest(index: int) -> str:
    """A stable 64-character lowercase hex digest for one small integer."""

    return f"{index % 256:02x}" * 32


def method_entry(index: int, *, version: int = 1) -> dict[str, Any]:
    digest = hex_digest(index)
    return {
        "goal_signature_id": "sig",
        "method_ref": {"method_id": f"m-{index:04d}", "version": version, "content_hash": digest},
        "refine_method_ref": {"id": f"m-{index:04d}", "version": version, "content_hash": digest},
        "method_id": f"m-{index:04d}",
        "registry_status": "UNKNOWN",
    }


# ======================================================================================
# 1. the old protocol's bytes are pinned (§7.1: "关着开关的路径两个值逐字节不变")
# ======================================================================================


def test_the_legacy_stub_package_is_byte_identical_to_the_previous_build() -> None:
    assert package_hash(stub_package()) == GOLDEN_STUB_WORLD_SHA256


def test_the_legacy_fixture_world_package_is_byte_identical_to_the_previous_build(
    tmp_path: Any,
) -> None:
    world = e2e.build_world(tmp_path, key="p23c-pkg")
    package = e2e.hierarchical_planner_package(
        world.mission, world.network(), registry=world.env.registry
    )
    assert package["package_version"] == HIERARCHICAL_PACKAGE_VERSION
    assert package_hash(package) == GOLDEN_FIXTURE_WORLD_SHA256


def test_the_legacy_package_never_grows_a_decision_field(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-pkg")
    package = e2e.hierarchical_planner_package(
        world.mission, world.network(), registry=world.env.registry
    )
    assert not (DECISION_ONLY_FIELDS & set(package))
    assert package["output_contract"] == "<plan_revision_proposal>{json}</plan_revision_proposal>"
    assert package["package_version"] == "planner-package-hierarchical-v4"


def test_an_unknown_protocol_name_is_refused() -> None:
    with pytest.raises(ContractError, match="planning_protocol"):
        stub_package(planning_protocol="planning-decision-v2")


# ======================================================================================
# 2. the new fields, present and correctly typed (§38, §39, §16)
# ======================================================================================


def test_the_decision_package_grows_exactly_the_decision_fields() -> None:
    legacy = stub_package()
    decided = decision_package()
    assert set(decided) - set(legacy) == DECISION_ONLY_FIELDS
    assert set(legacy) - set(decided) == set()


def test_the_decision_package_preserves_every_legacy_section(tmp_path: Any) -> None:
    """§38: the new fields are *added*; nothing the old package carried moves."""

    world = e2e.build_world(tmp_path, key="p23c-pkg")
    legacy = e2e.hierarchical_planner_package(
        world.mission, world.network(), registry=world.env.registry
    )
    decided = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    for name in legacy:
        if name in {"output_contract", "package_version"}:
            continue
        assert decided[name] == legacy[name], f"{name} moved"


def test_the_decision_package_states_the_protocol_and_its_enabled_types() -> None:
    protocol = decision_package()["planning_protocol"]
    assert protocol["protocol"] == PLANNING_DECISION_V1
    expected = sorted(
        name for name, enablement in H1_DECISION_ENABLEMENT.items() if enablement.executable
    )
    assert protocol["enabled_decision_types"] == expected
    # The three decode-only kinds are never advertised as executable in H1 (§12).
    for decode_only in ("REQUEST_EVIDENCE", "REQUEST_HUMAN", "PROPOSE_METHOD"):
        assert decode_only not in protocol["enabled_decision_types"]


def test_the_decision_package_switches_the_output_contract_and_its_label() -> None:
    package = decision_package()
    assert package["output_contract"] == "<planning_decision>{json}</planning_decision>"
    assert package["package_version"] == HIERARCHICAL_DECISION_PACKAGE_VERSION
    assert HIERARCHICAL_DECISION_PACKAGE_VERSION == "planner-package-hierarchical-v5"


def test_the_decision_limits_are_the_section_16_constants() -> None:
    limits = decision_package()["decision_limits"]
    assert limits == {
        "MAX_PD_RATIONALE_CHARS": MAX_PD_RATIONALE_CHARS,
        "MAX_PD_REASON_REFS": MAX_PD_REASON_REFS,
        "MAX_PD_ASSUMPTIONS": MAX_PD_ASSUMPTIONS,
        "MAX_PD_ALTERNATIVES": MAX_PD_ALTERNATIVES,
        "MAX_PD_UNCERTAINTIES": MAX_PD_UNCERTAINTIES,
        "MAX_PD_REPLAN_TRIGGERS": MAX_PD_REPLAN_TRIGGERS,
        "MAX_PD_BINDINGS": MAX_PD_BINDINGS,
        "MAX_PD_WAIT_REFS": MAX_PD_WAIT_REFS,
        "MAX_PD_BLOCKERS": MAX_PD_BLOCKERS,
        "MAX_PD_HUMAN_OPTIONS": MAX_PD_HUMAN_OPTIONS,
        "MAX_PD_ARGUMENTS": MAX_PD_ARGUMENTS,
    }


def test_previous_feedback_is_null_by_default() -> None:
    assert decision_package()["previous_feedback"] is None


def test_previous_feedback_is_written_as_plain_json_when_given() -> None:
    feedback = PlanningFeedbackV1(
        previous_decision_id="pd-0123456789abcdef01234567",
        status="REJECTED",
        rejection_codes=(),
        problems=(),
        changed_refs=(),
        budgets=PlanningRetryBudgetView(
            same_request_format_retries_remaining=1,
            planning_rounds_remaining=2,
            synthesis_asks_remaining=1,
            root_review_repairs_remaining=0,
            repeated_failure_before_escalation_remaining=None,
        ),
    )
    package = decision_package(previous_feedback=feedback)
    assert package["previous_feedback"] == feedback.to_json()
    assert json.loads(json.dumps(package))["previous_feedback"] == feedback.to_json()


def test_previous_feedback_is_validated_through_the_contract() -> None:
    with pytest.raises(ContractError):
        decision_package(previous_feedback={"previous_decision_id": ""})


# ======================================================================================
# 3. planning_subjects (§19): unique, stable, recomputable
# ======================================================================================


def test_subject_keys_are_unique_and_stable_across_two_builds(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-decided")
    build = lambda: e2e.hierarchical_planner_package(  # noqa: E731 - two calls, one shape
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )["planning_subjects"]
    first, second = build(), build()
    assert first == second, "same request, same subjects"
    keys = [item["subject_key"] for item in first]
    assert keys and len(keys) == len(set(keys)), "every subject_key is unique"
    assert all(item["subject_key"].startswith("subject-") for item in first)


def test_every_subject_carries_exactly_the_section_19_keys(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-decided")
    subjects = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )["planning_subjects"]
    for item in subjects:
        assert set(item) == {
            "subject_key",
            "occurrence_id",
            "task_id",
            "obligation_id",
            "contract_revision",
        }
        assert isinstance(item["contract_revision"], int)
        assert not isinstance(item["contract_revision"], bool)


def test_subject_keys_cover_every_occurrence_on_the_board(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-decided")
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    subjects = {item["occurrence_id"] for item in package["planning_subjects"]}
    assert subjects == {str(item.occurrence_id) for item in world.network().occurrences}


def test_a_committed_plan_still_yields_unique_subject_keys(tmp_path: Any) -> None:
    world = e2e.committed(tmp_path, key="p23c-decided-committed")
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    keys = [item["subject_key"] for item in package["planning_subjects"]]
    assert len(keys) == len(set(keys)) == len(world.network().occurrences)


# ======================================================================================
# 4. visible_refs: one case per source, fact rewritten to observation, deterministic
# ======================================================================================


def test_a_method_library_entry_becomes_a_method_ref() -> None:
    refs = visible_refs_from_hierarchical_package(
        empty_package(method_library=[method_entry(1, version=3)])
    )
    assert [dict(item) for item in refs] == [
        {
            "kind": "method",
            "id": "m-0001",
            "semantic_revision": 3,
            "content_hash": hex_digest(1),
        }
    ]


def test_an_applicability_row_also_exposes_its_method() -> None:
    package = empty_package(
        applicability=[
            {
                "goal_occurrence_id": "occ-1",
                "goal_signature_id": "sig",
                "method_ref": {"method_id": "m-0002", "version": 2, "content_hash": "2" * 64},
                "verdict": "NEEDS_EVIDENCE",
            }
        ]
    )
    assert [dict(item) for item in visible_refs_from_hierarchical_package(package)] == [
        {"kind": "method", "id": "m-0002", "semantic_revision": 2, "content_hash": "2" * 64}
    ]


def test_an_open_goal_yields_a_task_and_an_obligation_ref() -> None:
    """§5.1: the ref carries the object's *authoritative* digest, not a derived one."""

    task_hash = "a" * 64
    obligation_hash = "b" * 64
    package = empty_package(
        open_compound_goals=[
            {
                "occurrence_id": "occ-1",
                "goal_id": "task-1",
                "obligation_id": "obl-1",
                "contract_revision": 4,
            }
        ],
        authoritative_refs=[
            authority("task", "task-1", 4, task_hash),
            authority("obligation", "obl-1", 1, obligation_hash),
        ],
    )
    refs = by_key(package)
    assert refs[("task", "task-1")] == {
        "kind": "task",
        "id": "task-1",
        "semantic_revision": 4,
        "content_hash": task_hash,
    }
    assert refs[("obligation", "obl-1")] == {
        "kind": "obligation",
        "id": "obl-1",
        "semantic_revision": 1,
        "content_hash": obligation_hash,
    }


def test_a_task_or_obligation_without_authority_is_skipped_rather_than_invented() -> None:
    """§18.5: a hash this package cannot state authoritatively is *omitted*, not faked."""

    package = empty_package(
        open_compound_goals=[
            {
                "occurrence_id": "occ-1",
                "goal_id": "task-1",
                "obligation_id": "obl-1",
                "contract_revision": 4,
            }
        ]
    )
    assert visible_refs_from_hierarchical_package(package) == ()


def test_a_task_ref_takes_the_sidecars_revision_and_hash_verbatim() -> None:
    """A task entry that carries its own ``contract_revision`` still needs authority."""

    package = empty_package(
        open_compound_goals=[
            {
                "occurrence_id": "occ-1",
                "goal_id": "task-1",
                "obligation_id": "obl-1",
                "contract_revision": 9,
            }
        ],
        authoritative_refs=[authority("task", "task-1", 2, "c" * 64)],
    )
    refs = by_key(package)
    assert refs[("task", "task-1")]["semantic_revision"] == 2
    assert refs[("task", "task-1")]["content_hash"] == "c" * 64
    assert ("obligation", "obl-1") not in refs


def test_a_committed_primitive_yields_a_task_and_an_obligation_ref() -> None:
    package = empty_package(
        committed_primitives=[
            {"occurrence_id": "occ-2", "task_id": "task-2", "obligation_id": "obl-2"}
        ],
        authoritative_refs=[
            authority("task", "task-2", 1, "d" * 64),
            authority("obligation", "obl-2", 1, "e" * 64),
        ],
    )
    ids = {(item["kind"], item["id"]) for item in visible_refs_from_hierarchical_package(package)}
    assert ids == {("task", "task-2"), ("obligation", "obl-2")}


def test_a_fact_entry_is_written_as_an_observation() -> None:
    """V2 §17: there is no ``fact`` kind; a recorded fact is an ``observation``."""

    package = empty_package(
        facts=[
            {
                "proposition_key": "p-1",
                "read_set_entry": {
                    "kind": "fact",
                    "id": "obsrec-1",
                    "semantic_revision": 2,
                    "content_hash": "d" * 64,
                },
            }
        ]
    )
    refs = [dict(item) for item in visible_refs_from_hierarchical_package(package)]
    assert refs == [
        {"kind": "observation", "id": "obsrec-1", "semantic_revision": 2, "content_hash": "d" * 64}
    ]
    assert PlanningRefKind.OBSERVATION.value == "observation"


def test_a_rejected_refinement_exposes_the_method_it_named() -> None:
    package = empty_package(
        rejected_refinements=[
            {
                "occurrence_id": "occ-1",
                "goal_id": "task-1",
                "obligation_id": "obl-1",
                "rejected_method_instance_id": "mi-1",
                "rejected_method_ref": {
                    "method_id": "m-0009",
                    "version": 1,
                    "content_hash": "9" * 64,
                },
                "plan_revision": 1,
            }
        ]
    )
    refs = [dict(item) for item in visible_refs_from_hierarchical_package(package)]
    expected = {
        "kind": "method",
        "id": "m-0009",
        "semantic_revision": 1,
        "content_hash": "9" * 64,
    }
    assert expected in refs


def test_a_rejected_refinement_uses_the_instance_kind_when_the_wire_has_it() -> None:
    """Addendum §1: ``method_instance`` (instance_id / plan_revision / parameters_digest).

    The kind member arrives with the H1-A envelope slice, not this one, so the
    collector degrades to the method ref on a build that lacks it — this test pins
    whichever behaviour the enum on this build actually offers.
    """

    package = empty_package(
        rejected_refinements=[
            {
                "occurrence_id": "occ-1",
                "goal_id": "task-1",
                "obligation_id": "obl-1",
                "rejected_method_instance_id": "mi-1",
                "rejected_method_ref": {
                    "method_id": "m-0009",
                    "version": 1,
                    "content_hash": "9" * 64,
                },
                "plan_revision": 3,
                "parameters_digest": "e" * 64,
            }
        ]
    )
    refs = [dict(item) for item in visible_refs_from_hierarchical_package(package)]
    has_instance_kind = hasattr(PlanningRefKind, "METHOD_INSTANCE")
    assert ("method_instance" in {item["kind"] for item in refs}) is has_instance_kind
    if has_instance_kind:
        assert {
            "kind": "method_instance",
            "id": "mi-1",
            "semantic_revision": 3,
            "content_hash": "e" * 64,
        } in refs
    else:
        # Coarser but correct: only the method ref, and no invented hash.
        assert all(item["kind"] == "method" for item in refs)


def test_an_accepted_result_exposes_its_acceptance_ref() -> None:
    package = empty_package(
        accepted_results=[
            {"acceptance_ref": {"id": "acc-1", "semantic_revision": 5, "content_hash": "f" * 64}}
        ]
    )
    refs = [dict(item) for item in visible_refs_from_hierarchical_package(package)]
    assert refs == [
        {"kind": "acceptance", "id": "acc-1", "semantic_revision": 5, "content_hash": "f" * 64}
    ]


def test_a_non_integer_revision_is_skipped_not_coerced() -> None:
    """P2-1: ``"3"`` and ``3.5`` are not revisions; the ref is dropped (§18.5)."""

    package = empty_package(
        method_library=[
            {"refine_method_ref": {"id": "m-str", "version": "3", "content_hash": "1" * 64}},
            {"refine_method_ref": {"id": "m-float", "version": 3.5, "content_hash": "2" * 64}},
            method_entry(7, version=3),
        ]
    )
    assert [item["id"] for item in visible_refs_from_hierarchical_package(package)] == ["m-0007"]


def test_a_non_positive_revision_is_skipped_not_raised_to_one() -> None:
    """P2-1: §5.1's "无则 1" is only for the ledger; a 0 revision has no valid四元组."""

    package = empty_package(
        open_compound_goals=[{"goal_id": "task-1", "obligation_id": "obl-1"}],
        authoritative_refs=[
            authority("task", "task-1", 0, "a" * 64),
            authority("obligation", "obl-1", 0, "b" * 64),
        ],
    )
    assert visible_refs_from_hierarchical_package(package) == ()


def test_an_observation_without_a_hash_is_skipped_not_derived() -> None:
    """P2-3: §5.1 gives the observation hash as ``ReadItem.content_hash`` — no derivation."""

    package = empty_package(
        facts=[
            {"read_set_entry": {"kind": "fact", "id": "obsrec-1", "semantic_revision": 1}},
            {
                "read_set_entry": {
                    "kind": "fact",
                    "id": "obsrec-2",
                    "semantic_revision": 1,
                    "content_hash": "c" * 64,
                }
            },
        ]
    )
    assert [item["id"] for item in visible_refs_from_hierarchical_package(package)] == ["obsrec-2"]


def test_a_resolution_ref_keeps_its_own_kind() -> None:
    """P2-2: §5.1 lists ``acceptance`` and ``resolution`` as two kinds, two sources."""

    package = empty_package(
        accepted_results=[
            {"resolution_ref": {"id": "res-1", "semantic_revision": 2, "content_hash": "d" * 64}}
        ]
    )
    assert by_key(package)[("resolution", "res-1")] == {
        "kind": "resolution",
        "id": "res-1",
        "semantic_revision": 2,
        "content_hash": "d" * 64,
    }


def test_the_authority_table_keys_on_kind_and_id_not_id_alone() -> None:
    """P2-5: a task and an obligation may share an id; the ref must not cross kinds."""

    package = empty_package(
        open_compound_goals=[
            {"goal_id": "shared", "obligation_id": "shared", "contract_revision": 1}
        ],
        authoritative_refs=[
            authority("task", "shared", 1, "a" * 64),
            authority("obligation", "shared", 2, "b" * 64),
        ],
    )
    refs = by_key(package)
    assert refs[("task", "shared")] == {
        "kind": "task",
        "id": "shared",
        "semantic_revision": 1,
        "content_hash": "a" * 64,
    }
    assert refs[("obligation", "shared")] == {
        "kind": "obligation",
        "id": "shared",
        "semantic_revision": 2,
        "content_hash": "b" * 64,
    }


def test_an_authority_row_with_only_one_kind_does_not_answer_for_the_other() -> None:
    """P2-5: the obligation entry is not served by a task row that shares its id."""

    package = empty_package(
        open_compound_goals=[
            {"goal_id": "shared", "obligation_id": "shared", "contract_revision": 1}
        ],
        authoritative_refs=[authority("task", "shared", 1, "a" * 64)],
    )
    assert [item["kind"] for item in visible_refs_from_hierarchical_package(package)] == [
        "task"
    ]


def test_the_authority_sidecar_order_does_not_change_the_refs() -> None:
    """P2-6: the side table is an input set; its row order is not semantic."""

    entries = [
        {"goal_id": "task-a", "obligation_id": "obl-a", "contract_revision": 1},
        {"goal_id": "task-b", "obligation_id": "obl-b", "contract_revision": 1},
    ]
    rows = [
        authority("task", "task-a", 1, "a" * 64),
        authority("obligation", "obl-a", 1, "b" * 64),
        authority("task", "task-b", 1, "c" * 64),
        authority("obligation", "obl-b", 1, "d" * 64),
    ]
    forward = empty_package(open_compound_goals=entries, authoritative_refs=rows)
    backward = empty_package(
        open_compound_goals=entries, authoritative_refs=list(reversed(rows))
    )
    assert visible_refs_from_hierarchical_package(
        forward
    ) == visible_refs_from_hierarchical_package(backward)


def test_an_acceptance_ref_wins_over_a_resolution_ref_on_one_row() -> None:
    """P2-7: an ``accepted_results`` row reports its acceptance when both are present."""

    package = empty_package(
        accepted_results=[
            {
                "acceptance_ref": {"id": "acc-1", "semantic_revision": 1, "content_hash": "a" * 64},
                "resolution_ref": {"id": "res-1", "semantic_revision": 1, "content_hash": "b" * 64},
            }
        ]
    )
    assert [dict(item) for item in visible_refs_from_hierarchical_package(package)] == [
        {"kind": "acceptance", "id": "acc-1", "semantic_revision": 1, "content_hash": "a" * 64}
    ]


def test_a_resolution_ref_is_used_when_no_acceptance_ref_is_present() -> None:
    package = empty_package(
        accepted_results=[
            {"resolution_ref": {"id": "res-1", "semantic_revision": 1, "content_hash": "b" * 64}}
        ]
    )
    assert [item["kind"] for item in visible_refs_from_hierarchical_package(package)] == [
        "resolution"
    ]


def test_a_malformed_source_ref_is_skipped_rather_than_invented() -> None:
    package = empty_package(
        method_library=[
            {"refine_method_ref": {"id": "m-1", "version": 1, "content_hash": "not-hex"}},
            {"refine_method_ref": {"id": "", "version": 1, "content_hash": "a" * 64}},
            method_entry(2),
        ]
    )
    refs = [dict(item) for item in visible_refs_from_hierarchical_package(package)]
    assert refs == [
        {
            "kind": "method",
            "id": "m-0002",
            "semantic_revision": 1,
            "content_hash": hex_digest(2),
        }
    ]


def test_every_collected_ref_is_a_valid_planning_ref(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-decided")
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    for item in package["visible_refs"]:
        PlanningRefV1.from_json(item)


def test_the_same_package_twice_yields_the_same_refs_in_the_same_order() -> None:
    package = empty_package(method_library=[method_entry(index) for index in range(1, 6)])
    first = visible_refs_from_hierarchical_package(package)
    second = visible_refs_from_hierarchical_package(package)
    assert first == second
    assert list(first) == sorted(
        first,
        key=lambda item: (
            item["kind"],
            item["id"],
            item["semantic_revision"],
            item["content_hash"],
        ),
    )


def test_duplicate_sources_are_collapsed_to_one_ref() -> None:
    entry = method_entry(1)
    package = empty_package(method_library=[entry, dict(entry)])
    assert len(visible_refs_from_hierarchical_package(package)) == 1


def test_more_than_128_refs_are_truncated_to_the_sorted_prefix() -> None:
    entries = [method_entry(index) for index in range(MAX_VISIBLE_REFS + 7)]
    refs = visible_refs_from_hierarchical_package(empty_package(method_library=entries))
    assert MAX_VISIBLE_REFS == 128
    assert len(refs) == MAX_VISIBLE_REFS
    assert [item["id"] for item in refs] == [f"m-{index:04d}" for index in range(MAX_VISIBLE_REFS)]


def test_the_omitted_count_is_reported_for_over_long_input() -> None:
    entries = [method_entry(index) for index in range(MAX_VISIBLE_REFS + 7)]
    package = empty_package(method_library=entries)
    assert visible_refs_omitted(package) == 7


def test_the_omitted_count_is_zero_when_nothing_is_dropped() -> None:
    assert visible_refs_omitted(empty_package()) == 0
    assert visible_refs_omitted(empty_package(method_library=[method_entry(1)])) == 0


def test_the_omitted_count_describes_the_refs_actually_emitted() -> None:
    """P1-2: ``visible_refs`` and ``visible_refs_omitted`` must share one input.

    The authority side table contributes task/obligation refs, so measuring the
    omitted count on the package *without* it under-reports what the cap dropped.
    With ``count`` goals and ``count`` obligation rows the collector sees two refs
    per goal, so the dropped count is ``2 * count - 128`` — an oracle computed here
    from the inputs, not read back out of the code under test.
    """

    count = MAX_VISIBLE_REFS + 5
    package = wide_package(count, authoritative_refs=obligation_authorities(count))
    assert len(package["visible_refs"]) == MAX_VISIBLE_REFS
    assert package["visible_refs_omitted"] == 2 * count - MAX_VISIBLE_REFS
    assert package["visible_refs_omitted"] == visible_refs_omitted(package)


def test_a_sidecar_only_ref_can_push_the_package_over_the_cap() -> None:
    """A package at the cap whose last ref comes from the side table still counts it."""

    package = wide_package(
        MAX_VISIBLE_REFS,
        authoritative_refs=[authority("obligation", "obl-000", 1, hex_digest(9))],
    )
    # MAX_VISIBLE_REFS task refs plus one obligation ref: exactly one is dropped.
    assert len(package["visible_refs"]) == MAX_VISIBLE_REFS
    assert package["visible_refs_omitted"] == 1


def test_the_built_decision_package_exposes_the_collector_output(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-decided")
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
        authoritative_refs=[authority("obligation", "obl-root", 1, "b" * 64)],
    )
    assert package["visible_refs_omitted"] == 0
    assert package["visible_refs"] == list(visible_refs_from_hierarchical_package(package))
    kinds = {item["kind"] for item in package["visible_refs"]}
    assert kinds == {"method", "task", "obligation"}


def test_the_built_task_ref_carries_the_bindings_authoritative_hash(tmp_path: Any) -> None:
    """§5.1: ``task`` hash is ``task_semantics.content_hash`` — i.e. the binding's own."""

    world = e2e.build_world(tmp_path, key="p23c-decided")
    network = world.network()
    package = e2e.hierarchical_planner_package(
        world.mission,
        network,
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    emitted = by_key(package)
    for spec in network.occurrences:
        binding = network.binding_for_occurrence(spec.occurrence_id)
        ref = emitted[("task", str(spec.task_id))]
        assert ref["content_hash"] == binding.content_hash()
        # Not merely ``len == 64``: the exact canonical-JSON digest the store keeps.
        assert ref["content_hash"] == content_hash_of(binding.to_json())
        assert ref["semantic_revision"] == int(binding.contract_revision)


def test_the_built_package_omits_a_ref_it_cannot_attest(tmp_path: Any) -> None:
    """No obligation ledger was handed in, so no obligation ref is emitted — never a guess."""

    world = e2e.build_world(tmp_path, key="p23c-decided")
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    assert "obligation" not in {item["kind"] for item in package["visible_refs"]}
    assert "task" in {item["kind"] for item in package["visible_refs"]}


def test_a_supplied_obligation_authority_reaches_visible_refs(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-decided")
    from agent_orchestrator.storage.obligation_store import ObligationStore

    duty = ObligationStore(world.store).obligation(world.mission.id, "obl-root")
    authoritative = content_hash_of(duty.to_json())
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
        authoritative_refs=[authority("obligation", "obl-root", 1, authoritative)],
    )
    assert by_key(package)[("obligation", "obl-root")] == {
        "kind": "obligation",
        "id": "obl-root",
        "semantic_revision": 1,
        "content_hash": authoritative,
    }
    # The ledger's own canonical-JSON digest is the one §5.1 names, not any other.
    assert authoritative == content_hash_of(duty.to_json())


def test_the_legacy_package_never_carries_the_authority_sidecar() -> None:
    assert "authoritative_refs" not in stub_package()


def test_the_legacy_and_decision_packages_share_a_task_hash_source(tmp_path: Any) -> None:
    """The only task digest the module may emit is the binding's own ``content_hash``.

    A derived ``sha256({kind, id, revision})`` would describe the ref instead of the
    task; this pins the value to the store's authoritative column, not to a length.
    """

    import sqlite3

    world = e2e.build_world(tmp_path, key="p23c-decided")
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    stored = sqlite3.connect(world.path).execute(
        "SELECT content_hash FROM task_semantics WHERE task_id = 'task-root'"
    ).fetchone()[0]
    assert by_key(package)[("task", "task-root")]["content_hash"] == stored


def test_the_built_sidecar_is_order_independent(tmp_path: Any) -> None:
    """P2-6: the caller's ``authoritative_refs`` order must not move the package.

    The side table is an input *set*, so two calls that differ only in the order of
    the rows they pass must build the same ``visible_refs`` **and** the same
    ``authoritative_refs`` — the request binding hashes the whole package.
    """

    rows = [
        authority("obligation", "obl-root", 1, "b" * 64),
        authority("obligation", "obl-second", 1, "c" * 64),
    ]
    world = e2e.build_world(tmp_path, key="p23c-decided")
    build = lambda refs: e2e.hierarchical_planner_package(  # noqa: E731
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
        authoritative_refs=refs,
    )
    forward, backward = build(rows), build(list(reversed(rows)))
    assert forward["visible_refs"] == backward["visible_refs"]
    assert forward["authoritative_refs"] == backward["authoritative_refs"]


def test_the_authority_sidecar_is_a_plain_list_of_quadruples(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-decided")
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    rows = package["authoritative_refs"]
    assert rows, "every task binding on the board is attested"
    for row in rows:
        assert set(row) == {"kind", "id", "semantic_revision", "content_hash"}
        PlanningRefV1.from_json(row)
    kinds = {row["kind"] for row in rows}
    assert kinds == {"task"}


def test_the_builder_reads_task_authority_from_the_network_not_the_entry(tmp_path: Any) -> None:
    """A plan entry's own ``contract_revision`` never substitutes for the binding's."""

    world = e2e.build_world(tmp_path, key="p23c-decided")
    network = world.network()
    package = e2e.hierarchical_planner_package(
        world.mission,
        network,
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    binding = network.binding_for_occurrence("task-root")
    ref = by_key(package)[("task", "task-root")]
    assert ref["semantic_revision"] == int(binding.contract_revision)
    assert ref["content_hash"] == binding.content_hash()


def test_the_built_decision_package_reports_a_stub_with_no_refs() -> None:
    package = decision_package()
    assert package["visible_refs"] == []
    assert package["visible_refs_omitted"] == 0


# ======================================================================================
# 5. the three hash helpers (canonical JSON sha256; key order never matters)
# ======================================================================================


def test_package_hash_ignores_object_key_order() -> None:
    left = {"a": {"x": 1, "y": 2}, "b": [1, 2]}
    right = {"b": [1, 2], "a": {"y": 2, "x": 1}}
    assert package_hash(left) == package_hash(right)
    assert len(package_hash(left)) == 64


def test_package_hash_is_the_canonical_json_sha256() -> None:
    import hashlib

    from simple_harness.contracts import canonical_json

    value = {"b": [1, 2], "a": {"y": 2, "x": 1}}
    expected = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    assert package_hash(value) == expected


def test_package_hash_changes_when_an_array_order_changes() -> None:
    assert package_hash({"a": [1, 2]}) != package_hash({"a": [2, 1]})


def test_visible_refs_digest_ignores_object_key_order() -> None:
    left = [{"kind": "method", "id": "m-1", "semantic_revision": 1, "content_hash": "a" * 64}]
    right = [{"content_hash": "a" * 64, "semantic_revision": 1, "id": "m-1", "kind": "method"}]
    assert visible_refs_digest(left) == visible_refs_digest(right)
    assert len(visible_refs_digest(left)) == 64
    assert visible_refs_digest(left) != visible_refs_digest([])


def test_visible_refs_digest_changes_when_the_order_changes() -> None:
    first = [{"kind": "method", "id": "m-1", "semantic_revision": 1, "content_hash": "a" * 64}]
    second = [{"kind": "task", "id": "m-1", "semantic_revision": 1, "content_hash": "a" * 64}]
    assert visible_refs_digest(first) != visible_refs_digest(second)


def test_subject_bindings_hash_ignores_object_key_order() -> None:
    left = [
        {
            "subject_key": "subject-occ-1",
            "occurrence_id": "occ-1",
            "task_id": "task-1",
            "obligation_id": "obl-1",
            "contract_revision": 1,
        }
    ]
    right = [
        {
            "contract_revision": 1,
            "obligation_id": "obl-1",
            "task_id": "task-1",
            "occurrence_id": "occ-1",
            "subject_key": "subject-occ-1",
        }
    ]
    assert subject_bindings_hash(left) == subject_bindings_hash(right)
    assert subject_bindings_hash(left) != subject_bindings_hash([])


def test_the_helpers_are_independent_of_each_other() -> None:
    refs = [{"kind": "method", "id": "m-1", "semantic_revision": 1, "content_hash": "a" * 64}]
    subjects = [
        {
            "subject_key": "subject-occ-1",
            "occurrence_id": "occ-1",
            "task_id": "task-1",
            "obligation_id": "obl-1",
            "contract_revision": 1,
        }
    ]
    assert len({visible_refs_digest(refs), subject_bindings_hash(subjects), package_hash({})}) == 3


def test_the_helpers_hash_the_built_package_sections(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-decided")
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    assert package_hash(package) == package_hash(json.loads(json.dumps(package)))
    assert visible_refs_digest(package["visible_refs"]) == visible_refs_digest(
        visible_refs_from_hierarchical_package(package)
    )
    assert subject_bindings_hash(package["planning_subjects"]) == subject_bindings_hash(
        package["planning_subjects"]
    )


# ======================================================================================
# 6. the model still cannot be handed a system-authority field (§32)
# ======================================================================================


def test_no_new_top_level_field_is_a_system_bound_field() -> None:
    assert not (set(decision_package()) & SYSTEM_BOUND_FIELDS)


def test_visible_refs_are_only_kind_id_revision_and_hash(tmp_path: Any) -> None:
    world = e2e.build_world(tmp_path, key="p23c-decided")
    package = e2e.hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        planning_protocol=PLANNING_DECISION_V1,
    )
    for item in package["visible_refs"]:
        assert set(item) == {"kind", "id", "semantic_revision", "content_hash"}


def test_planning_protocol_carries_only_the_name_and_the_enabled_types() -> None:
    assert set(decision_package()["planning_protocol"]) == {
        "protocol",
        "enabled_decision_types",
    }


def test_the_new_sections_leak_no_registry_or_authority_field() -> None:
    package = decision_package()
    for name in ("planning_protocol", "planning_subjects", "visible_refs", "decision_limits"):
        body = json.dumps(package[name])
        assert "registry_status" not in body
        assert "authorization_ref" not in body
        assert "grant_ref" not in body
