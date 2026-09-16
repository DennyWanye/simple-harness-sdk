# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Builders shared by the FULL-TARGET-1.4 P1.1 tests.

The point of the helpers is that a test says *what truth a proposition has* and
never has to spell out the evidence plumbing — while still going through the real
``(t, f)`` merge, so a test that asks for FALSE gets a FALSE backed by a real
counter-observation rather than by a convenient constructor argument.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_orchestrator.contracts.evidence_state import (
    EvidenceEntry,
    EvidenceSnapshot,
    SupportCount,
    TruthValue,
)
from agent_orchestrator.contracts.htn import (
    CriterionLink,
    GoalSignature,
    MethodComposition,
    MethodContract,
    MethodStep,
    ObligationRelation,
    TaskForm,
    TaskSemanticBindingV1,
    parse_condition,
)
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind, VersionedRef
from agent_orchestrator.knowledge.predicates import (
    ArgumentType,
    PredicateParameter,
    PredicateRegistry,
    PredicateSignature,
    WorldAssumption,
    proposition_key,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64

_PACK_SUFFIX = (
    "plans",
    "taskSys2",
    "升级planV1",
    "v1.4",
    "simpleharness-full-target-1.4",
)

#: Byte-for-byte copies of the plan pack's schemas and fixtures.  They live in this
#: repository because CI checks out only the SDK: a test that reached across to the
#: plan repo would quietly verify nothing there.  ``fixtures/SOURCE.md`` records the
#: upstream path and the SHA-256 of every file, and
#: ``test_repo_copies_match_the_upstream_plan_pack`` fails on drift wherever the
#: upstream pack is present.
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
PLAN_PACK_FIXTURES = FIXTURE_ROOT / "plan_pack"
AER_FIXTURES = FIXTURE_ROOT / "aer"

#: Upstream path to the plan pack, when this machine has it.  Used only by the drift
#: check — never by the fixture tests themselves.
UPSTREAM_ROOT = Path(
    os.environ.get("SIMPLEHARNESS_PLAN_PACK")
    or Path(__file__).resolve().parents[4].joinpath("simple_harness", *_PACK_SUFFIX)
)


def upstream_plan_pack_available() -> bool:
    return UPSTREAM_ROOT.is_dir()


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_plan_fixtures() -> list[dict[str, Any]]:
    payload = _read_json(PLAN_PACK_FIXTURES / "fixtures.json")
    assert isinstance(payload, list)
    return payload


def load_aer_fixture_index() -> list[dict[str, Any]]:
    payload = _read_json(AER_FIXTURES / "index.json")
    assert isinstance(payload, list)
    return payload


def load_aer_fixture(name: str) -> dict[str, Any]:
    payload = _read_json(AER_FIXTURES / name)
    assert isinstance(payload, dict)
    return payload


def source_manifest() -> tuple[tuple[str, str, str], ...]:
    """Parse ``fixtures/SOURCE.md`` into ``(local path, upstream path, sha256)`` rows."""

    rows: list[tuple[str, str, str]] = []
    for line in (FIXTURE_ROOT / "SOURCE.md").read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        if len(cells) == 3 and len(cells[2]) == 64:
            rows.append((cells[0], cells[1], cells[2]))
    return tuple(rows)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vref(identifier: str, *, version: int = 1, content_hash: str = HASH_A) -> VersionedRef:
    return VersionedRef(id=identifier, version=version, content_hash=content_hash)


def tref(kind: TypedRefKind, identifier: str, *, revision: int = 1) -> TypedRef:
    return TypedRef(kind=kind, id=identifier, revision=revision, content_hash=HASH_A)


def observation_ref(name: str) -> TypedRef:
    return tref(TypedRefKind.OBSERVATION, f"obs-{name}")


def predicate_signature(
    name: str, *, world: WorldAssumption = WorldAssumption.OPEN
) -> PredicateSignature:
    return PredicateSignature(
        predicate_ref=vref(f"pred-{name}"),
        parameters=(PredicateParameter(name="subject", type=ArgumentType.STRING),),
        world_assumption=world,
        observer_ids=("observer-1",),
        statement=f"the {name} precondition holds for the subject",
    )


def entry_for(key: str, truth: TruthValue, name: str) -> EvidenceEntry | None:
    """Build the evidence that *produces* ``truth``; UNKNOWN is the absence of any."""

    if truth is TruthValue.UNKNOWN:
        return None
    if truth is TruthValue.TRUE:
        support, authoritative = SupportCount(1, 0), False
    elif truth is TruthValue.FALSE:
        support, authoritative = SupportCount(0, 1), True
    else:
        support, authoritative = SupportCount(1, 1), True
    return EvidenceEntry(
        proposition_key=key,
        support=support,
        observation_refs=(observation_ref(name),),
        authoritative_negative=authoritative,
    )


class World:
    """A registry plus a matching snapshot, addressed by short atom names."""

    def __init__(
        self,
        truths: Mapping[str, TruthValue],
        *,
        closed: frozenset[str] = frozenset(),
        declared_only: frozenset[str] = frozenset(),
    ) -> None:
        self.registry = PredicateRegistry()
        self.signatures: dict[str, PredicateSignature] = {}
        self.keys: dict[str, str] = {}
        entries: list[EvidenceEntry] = []
        for name in sorted(set(truths) | set(closed) | set(declared_only)):
            world = WorldAssumption.CLOSED if name in closed else WorldAssumption.OPEN
            signature = predicate_signature(name, world=world)
            self.registry.register(signature)
            self.signatures[name] = signature
            key = proposition_key(signature, {"subject": name})
            self.keys[name] = key
            truth = truths.get(name, TruthValue.UNKNOWN)
            entry = entry_for(key, truth, name)
            if entry is not None:
                entries.append(entry)
        self.snapshot = EvidenceSnapshot(
            snapshot_id="snapshot-1",
            as_of_ms=1_000,
            scope_id="mission-1",
            scope_epoch=1,
            support_revision=1,
            entries=tuple(entries),
        )

    def atom(self, name: str) -> dict[str, Any]:
        return {
            "op": "predicate",
            "predicate_ref": self.signatures[name].predicate_ref.to_json(),
            "arguments": {"subject": {"op": "constant", "value": name}},
        }

    def condition(self, payload: dict[str, Any]) -> Any:
        return parse_condition(payload)


def all_of(*items: dict[str, Any]) -> dict[str, Any]:
    return {"op": "all", "items": list(items)}


def any_of(*items: dict[str, Any]) -> dict[str, Any]:
    return {"op": "any", "items": list(items)}


def not_of(item: dict[str, Any]) -> dict[str, Any]:
    return {"op": "not", "item": item}


def goal_signature(identifier: str = "compare-sources") -> GoalSignature:
    return GoalSignature(
        signature_id=identifier,
        version=1,
        parameter_schema_ref=vref("parameters"),
        output_schema_ref=vref("outputs"),
        statement="compare the named sources and report the differences",
        coverage_criteria=("c-complete",),
    )


def task_binding(
    *,
    task_id: str = "task-1",
    obligation: str = "obligation-1",
    form: TaskForm = TaskForm.COMPOUND,
    parameters: Mapping[str, Any] | None = None,
    capabilities: tuple[str, ...] = (),
) -> TaskSemanticBindingV1:
    return TaskSemanticBindingV1(
        task_id=task_id,  # type: ignore[arg-type]
        obligation_id=obligation,  # type: ignore[arg-type]
        contract_revision=1,  # type: ignore[arg-type]
        contract_hash=HASH_B,
        form=form,
        goal_signature=goal_signature(),
        typed_parameters=dict(parameters or {"subject": "alpha"}),
        requirement_refs=("c-complete",),
        semantic_scope="mission-1",
        capability_requirements=capabilities,
        operator_ref=vref("operator-1") if form is TaskForm.PRIMITIVE else None,
    )


def method_contract(
    world: World | None = None,
    *,
    method_id: str = "compare-two-sources",
    applicable_when: tuple[dict[str, Any], ...] = (),
    required_capabilities: tuple[str, ...] = (),
    step_capabilities: tuple[str, ...] = ("sources.read",),
) -> MethodContract:
    del world
    return MethodContract(
        method_id=method_id,
        method_version=1,
        goal_type_ref=vref("compare-sources"),
        parameter_schema_ref=vref("parameters"),
        output_schema_ref=vref("outputs"),
        applicable_when=tuple(parse_condition(item) for item in applicable_when),
        exploration_assumptions=(),
        steps=(
            MethodStep(
                local_id="extract",
                task_type_ref=vref("extract-evidence"),
                form=TaskForm.PRIMITIVE,
                arguments={},
                required_capabilities=step_capabilities,
                obligation_relation=ObligationRelation.REFINES_PARENT,
            ),
        ),
        ordering=(),
        required_capabilities=required_capabilities,
        expected_effects=(),
        composition=_composition(),
        basis_refs=(),
    )


def _composition() -> MethodComposition:
    return MethodComposition(
        criterion_links=(
            CriterionLink(
                parent_criterion_id="c-complete",
                child_step="extract",
                child_criterion_id="c-extracted",
                evidence_requirement="every named source version is covered",
            ),
        ),
        outputs={},
        finalizer_step="extract",
        independent_review_required=True,
    )
