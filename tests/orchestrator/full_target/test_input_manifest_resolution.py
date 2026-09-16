# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.2b: DataRequirement -> BoundInput -> InputManifest, resolved as a pure function.

Every test builds contract values, calls one of the three entry points of
:mod:`agent_orchestrator.artifacts.input_bindings` and inspects the result.  No
database, no workspace, no file is touched: the slice is the *decision*, and the
decision has to be reproducible from the recorded values alone.

The facts this suite exists to pin down (plan §24.1 decisions 3 and 4, annex TG
§4.3 / §10.1-10.3, implementation annex §4.3):

* a single-valued port that has two live candidates is **ambiguous**, and the
  resolver refuses instead of preferring "the one further down the topological
  order";
* a set-valued port is ordered by its *declared* rule, and a set port with no
  declared rule is refused rather than filled in "whoever wrote last";
* schema agreement is exact identity or a *registered* declaration — containment
  is never inferred;
* ``PINNED`` and ``FOLLOW_AUTHORIZED_REVISION`` are different answers to the same
  index, and neither buys a way past revocation, deletion or a purpose limit;
* an ORDER-only predecessor never appears in the manifest, however many accepted
  artifacts it has (T015 / T066, pure part);
* ``attempt-A/report.md`` and ``attempt-B/report.md`` are two artifacts, and two
  different hashes aimed at one final path is a conflict the caller must resolve
  explicitly — never a merge, never a silent pick.
"""

from __future__ import annotations

import ast
import random
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from agent_orchestrator.artifacts.input_bindings import (
    AcceptedOutput,
    AcceptedOutputsIndex,
    DisclosureState,
    ExplicitPortOrder,
    InputManifest,
    ManifestNotFrozen,
    MaterialisationPlan,
    ResolutionPolicy,
    ResolutionProblemKind,
    ResolutionResult,
    ResolvedInputBinding,
    ResourceIdentity,
    SchemaCompatibilityRegistry,
    SchemaCompatibilityRule,
    TargetRules,
    explain,
    materialise_plan,
    resolve_declared_inputs,
)
from agent_orchestrator.contracts.evidence_state import (
    Availability,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from agent_orchestrator.contracts.htn import (
    BoundInput,
    ContractRevision,
    DataRequirement,
    GoalSignature,
    ObligationId,
    OccurrenceId,
    PortCardinality,
    PortOrdering,
    PortSpec,
    SourceRevisionPolicy,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind, VersionedRef

# --------------------------------------------------------------------------------------
# Builders.  Deliberately explicit: a test that has to read a factory to know what it
# asserts is not a counterexample anyone can check.
# --------------------------------------------------------------------------------------

CONSUMER = TaskRef("mission-1:task-9")
CONSUMER_OCC = OccurrenceId("occ-consumer")


def digest(seed: str) -> str:
    return sha256_hex({"seed": seed})


def schema(name: str, version: int = 1) -> VersionedRef:
    return VersionedRef(id=name, version=version, content_hash=digest(f"{name}/{version}"))


REPORT_SCHEMA = schema("report-schema")
REPORT_SCHEMA_V2 = schema("report-schema", 2)
OTHER_SCHEMA = schema("table-schema")


def port(
    key: str,
    *,
    cardinality: PortCardinality = PortCardinality.SINGLE,
    required: bool = True,
    ordering: PortOrdering | None = None,
    order_key: str | None = None,
    port_schema: VersionedRef = REPORT_SCHEMA,
) -> PortSpec:
    return PortSpec(
        port_key=key,
        schema_ref=port_schema,
        cardinality=cardinality,
        required=required,
        ordering=ordering,
        order_key=order_key,
    )


def consumer_binding(*ports: PortSpec, task_id: TaskRef = CONSUMER) -> TaskSemanticBindingV1:
    return TaskSemanticBindingV1(
        task_id=task_id,
        obligation_id=ObligationId("ob-1"),
        contract_revision=ContractRevision(1),
        contract_hash=digest("contract"),
        form=TaskForm.PRIMITIVE,
        goal_signature=GoalSignature(
            signature_id="sig-synthesise",
            version=1,
            parameter_schema_ref=schema("params"),
            output_schema_ref=schema("outputs"),
            statement="synthesise the reports",
        ),
        input_ports=ports,
        output_ports=(port("summary"),),
        operator_ref=schema("operator-synthesise"),
    )


def requirement(
    rid: str,
    *,
    producer: str,
    output_port: str = "report",
    input_port: str = "report",
    req_schema: VersionedRef = REPORT_SCHEMA,
    revision_policy: SourceRevisionPolicy = SourceRevisionPolicy.PINNED,
    consumer: OccurrenceId = CONSUMER_OCC,
) -> DataRequirement:
    return DataRequirement(
        requirement_id=rid,
        producer_occurrence=OccurrenceId(producer),
        output_port=output_port,
        consumer_occurrence=consumer,
        input_port=input_port,
        schema_ref=req_schema,
        assurance_policy_ref="assurance-standard",
        freshness_policy_ref="freshness-standard",
        source_revision_policy=revision_policy,
    )


def output(
    *,
    producer: str,
    output_port: str = "report",
    result: str = "",
    acceptance: str = "",
    artifact: str = "",
    content: str = "content-a",
    out_schema: VersionedRef = REPORT_SCHEMA,
    revision: str = "r1",
    namespace: str = "workspace-main",
    path: str = "report.md",
    producer_ordinal: int = 0,
    order_keys: Mapping[str, str] | None = None,
    disclosure: DisclosureState = DisclosureState.DISCLOSABLE,
    provisional: bool = False,
    support_revision: int = 1,
) -> AcceptedOutput:
    stem = f"{producer}-{output_port}-{revision}-{content}"
    return AcceptedOutput(
        producer_occurrence=OccurrenceId(producer),
        producer_task_ref=TaskRef(f"mission-1:task-{producer}"),
        output_port=output_port,
        producer_result_id=result or f"result-{stem}",
        acceptance_id=acceptance or f"acceptance-{stem}",
        support_revision=support_revision,
        artifact_id=artifact or f"artifact-{stem}",
        content_hash=digest(content),
        schema_ref=out_schema,
        source_revision=revision,
        source_identity=ResourceIdentity(namespace=namespace, path=path),
        producer_ordinal=producer_ordinal,
        order_keys=dict(order_keys or {}),
        disclosure=disclosure,
        provisional=provisional,
    )


def witness_for(
    acceptance_id: str,
    *,
    decision: WitnessDecision = WitnessDecision.USABLE,
    scope_epoch: int = 7,
    not_after_ms: int | None = None,
    scope_id: str = "scope-mission",
    purpose: WitnessPurpose = WitnessPurpose.START,
    consumer_id: str = str(CONSUMER),
    consumer_kind: TypedRefKind = TypedRefKind.TASK,
    support_revision: int = 1,
) -> ValidityWitness:
    truth = TruthValue.TRUE if decision is WitnessDecision.USABLE else TruthValue.UNKNOWN
    return ValidityWitness(
        witness_id=f"witness-{acceptance_id}",
        consumer_ref=TypedRef(
            kind=consumer_kind, id=consumer_id, revision=1, content_hash=digest("consumer")
        ),
        purpose=purpose,
        truth=truth,
        freshness=Validity.CURRENT,
        availability=Availability.READABLE,
        decision=decision,
        scope_id=scope_id,
        scope_epoch=scope_epoch,
        support_revision=support_revision,
        as_of_ms=1_000,
        not_after_ms=not_after_ms,
    )


def witnesses_for(*outputs: AcceptedOutput) -> dict[str, ValidityWitness]:
    return {item.acceptance_id: witness_for(item.acceptance_id) for item in outputs}


def index_of(
    *outputs: AcceptedOutput,
    completed: Sequence[str] | None = None,
    authorized: Mapping[tuple[str, str], str] | None = None,
) -> AcceptedOutputsIndex:
    producers = (
        frozenset(OccurrenceId(p) for p in completed)
        if completed is not None
        else frozenset(item.producer_occurrence for item in outputs)
    )
    return AcceptedOutputsIndex(
        outputs=tuple(outputs),
        completed_producers=producers,
        authorized_revisions={
            (OccurrenceId(occ), port_key): revision
            for (occ, port_key), revision in (authorized or {}).items()
        },
    )


def resolve(
    consumer: TaskSemanticBindingV1,
    requirements: Sequence[DataRequirement],
    accepted: AcceptedOutputsIndex,
    *,
    policy: ResolutionPolicy | None = None,
    witnesses: Mapping[str, ValidityWitness] | None = None,
) -> ResolutionResult:
    if witnesses is None:
        witnesses = {
            item.acceptance_id: witness_for(item.acceptance_id) for item in accepted.outputs
        }
    return resolve_declared_inputs(
        consumer,
        requirements,
        accepted,
        witnesses=witnesses,
        policy=policy or ResolutionPolicy(scope_epochs={"scope-mission": 7}),
        consumer_occurrence=CONSUMER_OCC,
    )


def default_policy(**kwargs: object) -> ResolutionPolicy:
    fields: dict[str, object] = {"scope_epochs": {"scope-mission": 7}}
    fields.update(kwargs)
    return ResolutionPolicy(**fields)  # type: ignore[arg-type]


def only(result: ResolutionResult) -> ResolvedInputBinding:
    assert result.manifest is not None, result.problems
    assert len(result.manifest.bindings) == 1
    return result.manifest.bindings[0]


# --------------------------------------------------------------------------------------
# 1. Single-valued ports: exactly one binding, or an explicit refusal
# --------------------------------------------------------------------------------------


def test_single_port_with_one_candidate_resolves() -> None:
    produced = output(producer="occ-a")
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(produced),
    )
    assert result.ok
    binding = only(result)
    assert binding.input_port == "report"
    assert binding.requirement_id == "req-1"


def test_binding_freezes_the_exact_identity_rather_than_latest() -> None:
    """Implementation annex §4.3: no ``latest`` may be left for the Worker."""

    produced = output(producer="occ-a", revision="r3")
    binding = only(
        resolve(
            consumer_binding(port("report")),
            [requirement("req-1", producer="occ-a")],
            index_of(produced),
        )
    )
    assert binding.producer_result_id == produced.producer_result_id
    assert binding.acceptance_id == produced.acceptance_id
    assert binding.support_revision == produced.support_revision
    assert binding.artifact_id == produced.artifact_id
    assert binding.content_hash == produced.content_hash
    assert binding.source_revision == "r3"
    assert binding.producer_task_ref == produced.producer_task_ref
    assert binding.consumer_task_ref == CONSUMER


def test_binding_records_the_read_freshness_and_disclosure_policies() -> None:
    binding = only(
        resolve(
            consumer_binding(port("report")),
            [requirement("req-1", producer="occ-a")],
            index_of(output(producer="occ-a")),
        )
    )
    assert binding.read_policy == "assurance-standard"
    assert binding.freshness_policy == "freshness-standard"
    assert binding.disclosure_scope


def test_single_port_with_two_candidates_at_one_producer_is_ambiguous() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(
            output(producer="occ-a", content="attempt-a"),
            output(producer="occ-a", content="attempt-b"),
        ),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.AMBIGUOUS_SINGLE_PORT in result.kinds


def test_ambiguous_single_port_never_prefers_the_later_producer() -> None:
    """TG §10.2: "B is further down the topological order" is not a selection rule."""

    early = output(producer="occ-a", content="early", producer_ordinal=0)
    late = output(producer="occ-a", content="late", producer_ordinal=99)
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(early, late),
    )
    assert result.manifest is None
    problem = result.of_kind(ResolutionProblemKind.AMBIGUOUS_SINGLE_PORT)[0]
    assert set(problem.candidates) == {early.artifact_id, late.artifact_id}


def test_two_requirements_aimed_at_one_single_port_are_ambiguous() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [
            requirement("req-1", producer="occ-a"),
            requirement("req-2", producer="occ-b"),
        ],
        index_of(
            output(producer="occ-a", content="a"),
            output(producer="occ-b", content="b"),
        ),
    )
    assert result.manifest is None
    problem = result.of_kind(ResolutionProblemKind.AMBIGUOUS_SINGLE_PORT)[0]
    assert set(problem.requirement_ids) == {"req-1", "req-2"}
    assert problem.input_port == "report"


def test_single_port_is_not_ambiguous_when_the_revision_policy_selects_one() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(
            output(producer="occ-a", revision="r1", content="v1"),
            output(producer="occ-a", revision="r2", content="v2"),
        ),
        policy=default_policy(pinned_revisions={"req-1": "r1"}),
    )
    assert result.ok
    assert only(result).source_revision == "r1"


# --------------------------------------------------------------------------------------
# 2. Set-valued ports: the declared order, or a refusal
# --------------------------------------------------------------------------------------


def set_port_case(
    ordering: PortOrdering | None,
    *,
    order_key: str | None = None,
    outputs: Sequence[AcceptedOutput] = (),
    requirements: Sequence[DataRequirement] = (),
    policy: ResolutionPolicy | None = None,
) -> ResolutionResult:
    return resolve(
        consumer_binding(
            port(
                "reports",
                cardinality=PortCardinality.SET,
                ordering=ordering,
                order_key=order_key,
            )
        ),
        list(requirements),
        index_of(*outputs),
        policy=policy,
    )


def test_set_port_by_producer_ordinal_orders_the_bindings() -> None:
    result = set_port_case(
        PortOrdering.BY_PRODUCER_ORDINAL,
        outputs=[
            output(producer="occ-b", content="b", producer_ordinal=2),
            output(producer="occ-a", content="a", producer_ordinal=1),
        ],
        requirements=[
            requirement("req-b", producer="occ-b", input_port="reports"),
            requirement("req-a", producer="occ-a", input_port="reports"),
        ],
    )
    assert result.ok
    assert result.manifest is not None
    assert [b.requirement_id for b in result.manifest.bindings] == ["req-a", "req-b"]
    assert [b.port_ordinal for b in result.manifest.bindings] == [0, 1]


def test_set_port_by_key_orders_the_bindings() -> None:
    result = set_port_case(
        PortOrdering.BY_KEY,
        order_key="chapter",
        outputs=[
            output(producer="occ-b", content="b", order_keys={"chapter": "02"}),
            output(producer="occ-a", content="a", order_keys={"chapter": "01"}),
        ],
        requirements=[
            requirement("req-b", producer="occ-b", input_port="reports"),
            requirement("req-a", producer="occ-a", input_port="reports"),
        ],
    )
    assert result.ok
    assert result.manifest is not None
    assert [b.requirement_id for b in result.manifest.bindings] == ["req-a", "req-b"]


def test_set_port_explicit_order_is_honoured_over_every_other_signal() -> None:
    result = set_port_case(
        PortOrdering.EXPLICIT,
        outputs=[
            output(producer="occ-a", content="a", producer_ordinal=1),
            output(producer="occ-b", content="b", producer_ordinal=2),
        ],
        requirements=[
            requirement("req-a", producer="occ-a", input_port="reports"),
            requirement("req-b", producer="occ-b", input_port="reports"),
        ],
        policy=default_policy(explicit_orders=(ExplicitPortOrder("reports", ("req-b", "req-a")),)),
    )
    assert result.ok
    assert result.manifest is not None
    assert [b.requirement_id for b in result.manifest.bindings] == ["req-b", "req-a"]


def test_set_port_without_a_declared_order_is_refused() -> None:
    """TG §4.3: a set port may not fall back to "whoever wrote last wins"."""

    result = set_port_case(
        None,
        outputs=[output(producer="occ-a", content="a"), output(producer="occ-b", content="b")],
        requirements=[
            requirement("req-a", producer="occ-a", input_port="reports"),
            requirement("req-b", producer="occ-b", input_port="reports"),
        ],
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SET_PORT_UNORDERED in result.kinds


def test_set_port_by_key_with_a_missing_key_is_incomplete() -> None:
    result = set_port_case(
        PortOrdering.BY_KEY,
        order_key="chapter",
        outputs=[
            output(producer="occ-a", content="a", order_keys={"chapter": "01"}),
            output(producer="occ-b", content="b"),
        ],
        requirements=[
            requirement("req-a", producer="occ-a", input_port="reports"),
            requirement("req-b", producer="occ-b", input_port="reports"),
        ],
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE in result.kinds


def test_set_port_by_key_with_a_duplicate_key_is_incomplete() -> None:
    result = set_port_case(
        PortOrdering.BY_KEY,
        order_key="chapter",
        outputs=[
            output(producer="occ-a", content="a", order_keys={"chapter": "01"}),
            output(producer="occ-b", content="b", order_keys={"chapter": "01"}),
        ],
        requirements=[
            requirement("req-a", producer="occ-a", input_port="reports"),
            requirement("req-b", producer="occ-b", input_port="reports"),
        ],
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE in result.kinds


def test_set_port_by_producer_ordinal_with_a_tie_is_incomplete() -> None:
    result = set_port_case(
        PortOrdering.BY_PRODUCER_ORDINAL,
        outputs=[
            output(producer="occ-a", content="a", producer_ordinal=4),
            output(producer="occ-b", content="b", producer_ordinal=4),
        ],
        requirements=[
            requirement("req-a", producer="occ-a", input_port="reports"),
            requirement("req-b", producer="occ-b", input_port="reports"),
        ],
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE in result.kinds


def test_set_port_explicit_order_that_omits_a_requirement_is_incomplete() -> None:
    result = set_port_case(
        PortOrdering.EXPLICIT,
        outputs=[output(producer="occ-a", content="a"), output(producer="occ-b", content="b")],
        requirements=[
            requirement("req-a", producer="occ-a", input_port="reports"),
            requirement("req-b", producer="occ-b", input_port="reports"),
        ],
        policy=default_policy(explicit_orders=(ExplicitPortOrder("reports", ("req-a",)),)),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE in result.kinds


def test_set_port_explicit_order_naming_an_unknown_requirement_is_incomplete() -> None:
    result = set_port_case(
        PortOrdering.EXPLICIT,
        outputs=[output(producer="occ-a", content="a")],
        requirements=[requirement("req-a", producer="occ-a", input_port="reports")],
        policy=default_policy(
            explicit_orders=(ExplicitPortOrder("reports", ("req-a", "req-ghost")),)
        ),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE in result.kinds


def test_set_port_order_does_not_depend_on_the_input_order() -> None:
    outputs = [
        output(producer=f"occ-{letter}", content=letter, producer_ordinal=position)
        for position, letter in enumerate("abcd", start=1)
    ]
    requirements = [
        requirement(f"req-{letter}", producer=f"occ-{letter}", input_port="reports")
        for letter in "abcd"
    ]
    baseline = set_port_case(
        PortOrdering.BY_PRODUCER_ORDINAL, outputs=outputs, requirements=requirements
    )
    assert baseline.manifest is not None
    expected = [b.requirement_id for b in baseline.manifest.bindings]
    for seed in range(5):
        rng = random.Random(seed)
        shuffled_outputs = list(outputs)
        shuffled_requirements = list(requirements)
        rng.shuffle(shuffled_outputs)
        rng.shuffle(shuffled_requirements)
        shuffled = set_port_case(
            PortOrdering.BY_PRODUCER_ORDINAL,
            outputs=shuffled_outputs,
            requirements=shuffled_requirements,
        )
        assert shuffled.manifest is not None
        assert [b.requirement_id for b in shuffled.manifest.bindings] == expected


def test_a_single_port_may_not_declare_an_ordering_at_all() -> None:
    """The contract itself refuses it; the resolver never has to guess."""

    with pytest.raises(Exception):
        port("report", ordering=PortOrdering.BY_PRODUCER_ORDINAL)


# --------------------------------------------------------------------------------------
# 3. Schema agreement: exact identity or a registered declaration
# --------------------------------------------------------------------------------------


def test_exact_schema_identity_binds() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", out_schema=REPORT_SCHEMA)),
    )
    assert result.ok
    assert only(result).converter_ref is None


def test_a_newer_schema_version_without_a_declaration_is_a_mismatch() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", out_schema=REPORT_SCHEMA_V2)),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SCHEMA_MISMATCH in result.kinds


def test_a_registered_compatibility_declaration_binds_and_records_the_converter() -> None:
    registry = SchemaCompatibilityRegistry(
        rules=(
            SchemaCompatibilityRule(
                produced=REPORT_SCHEMA_V2,
                required=REPORT_SCHEMA,
                declaration_ref="decl-report-v2-to-v1",
                converter_ref="converter-report-downgrade",
            ),
        )
    )
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", out_schema=REPORT_SCHEMA_V2)),
        policy=default_policy(schema_registry=registry),
    )
    assert result.ok
    binding = only(result)
    assert binding.converter_ref == "converter-report-downgrade"
    assert binding.produced_schema_ref == REPORT_SCHEMA_V2
    assert binding.schema_ref == REPORT_SCHEMA


def test_a_declaration_in_the_other_direction_does_not_bind() -> None:
    """Compatibility is directed; the registry is not read symmetrically."""

    registry = SchemaCompatibilityRegistry(
        rules=(
            SchemaCompatibilityRule(
                produced=REPORT_SCHEMA,
                required=REPORT_SCHEMA_V2,
                declaration_ref="decl-forward-only",
            ),
        )
    )
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", out_schema=REPORT_SCHEMA_V2)),
        policy=default_policy(schema_registry=registry),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SCHEMA_MISMATCH in result.kinds


def test_a_same_id_same_version_different_hash_schema_is_a_mismatch() -> None:
    """Identity is (id, version, content_hash); a re-published body is a new schema."""

    forged = VersionedRef(id="report-schema", version=1, content_hash=digest("rewritten"))
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", out_schema=forged)),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SCHEMA_MISMATCH in result.kinds


def test_containment_is_never_inferred_from_the_schema_reference() -> None:
    """An unrelated schema is refused even though nothing proves it incompatible."""

    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", out_schema=OTHER_SCHEMA)),
    )
    assert result.manifest is None
    problem = result.of_kind(ResolutionProblemKind.SCHEMA_MISMATCH)[0]
    assert "table-schema" in problem.detail


def test_a_requirement_that_contradicts_the_port_schema_is_a_mismatch() -> None:
    result = resolve(
        consumer_binding(port("report", port_schema=REPORT_SCHEMA)),
        [requirement("req-1", producer="occ-a", req_schema=OTHER_SCHEMA)],
        index_of(output(producer="occ-a", out_schema=OTHER_SCHEMA)),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.SCHEMA_MISMATCH in result.kinds


# --------------------------------------------------------------------------------------
# 4. PINNED vs FOLLOW_AUTHORIZED_REVISION
# --------------------------------------------------------------------------------------


def two_revisions() -> AcceptedOutputsIndex:
    return index_of(
        output(producer="occ-a", revision="r1", content="v1"),
        output(producer="occ-a", revision="r2", content="v2"),
        authorized={("occ-a", "report"): "r2"},
    )


def test_pinned_keeps_the_old_revision_when_a_newer_one_is_authorised() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a", revision_policy=SourceRevisionPolicy.PINNED)],
        two_revisions(),
        policy=default_policy(pinned_revisions={"req-1": "r1"}),
    )
    assert result.ok
    binding = only(result)
    assert binding.source_revision == "r1"
    assert binding.requires_reacceptance is False


def test_follow_authorized_revision_takes_the_current_authorised_one() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [
            requirement(
                "req-1",
                producer="occ-a",
                revision_policy=SourceRevisionPolicy.FOLLOW_AUTHORIZED_REVISION,
            )
        ],
        two_revisions(),
        policy=default_policy(pinned_revisions={"req-1": "r1"}),
    )
    assert result.ok
    assert only(result).source_revision == "r2"


def test_follow_authorized_revision_flags_reacceptance_when_the_revision_moved() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [
            requirement(
                "req-1",
                producer="occ-a",
                revision_policy=SourceRevisionPolicy.FOLLOW_AUTHORIZED_REVISION,
            )
        ],
        two_revisions(),
        policy=default_policy(pinned_revisions={"req-1": "r1"}),
    )
    assert only(result).requires_reacceptance is True


def test_follow_authorized_revision_does_not_flag_reacceptance_when_unchanged() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [
            requirement(
                "req-1",
                producer="occ-a",
                revision_policy=SourceRevisionPolicy.FOLLOW_AUTHORIZED_REVISION,
            )
        ],
        two_revisions(),
        policy=default_policy(pinned_revisions={"req-1": "r2"}),
    )
    assert only(result).requires_reacceptance is False


def test_a_first_follow_binding_is_flagged_for_reacceptance() -> None:
    """A first binding is a first version of the input, not a neutral starting point."""

    result = resolve(
        consumer_binding(port("report")),
        [
            requirement(
                "req-1",
                producer="occ-a",
                revision_policy=SourceRevisionPolicy.FOLLOW_AUTHORIZED_REVISION,
            )
        ],
        two_revisions(),
    )
    assert result.ok
    assert only(result).requires_reacceptance is True


def test_a_first_pinned_binding_is_not_flagged_for_reacceptance() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a", revision_policy=SourceRevisionPolicy.PINNED)],
        index_of(output(producer="occ-a", revision="r1")),
    )
    assert result.ok
    assert only(result).requires_reacceptance is False


def test_pinned_revision_that_is_no_longer_in_the_index_is_reported() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        two_revisions(),
        policy=default_policy(pinned_revisions={"req-1": "r0"}),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.REVISION_NOT_AVAILABLE in result.kinds


def test_the_revision_policy_is_recorded_on_the_binding() -> None:
    for policy_value in SourceRevisionPolicy:
        result = resolve(
            consumer_binding(port("report")),
            [requirement("req-1", producer="occ-a", revision_policy=policy_value)],
            index_of(output(producer="occ-a", revision="r1")),
            policy=default_policy(pinned_revisions={"req-1": "r1"}),
        )
        assert only(result).source_revision_policy is policy_value


def test_pinned_does_not_buy_a_way_past_revocation() -> None:
    """TG §4.3: revocation, deletion and purpose limits are still checked now."""

    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", revision="r1", disclosure=DisclosureState.REVOKED)),
        policy=default_policy(pinned_revisions={"req-1": "r1"}),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.NOT_DISCLOSABLE in result.kinds


# --------------------------------------------------------------------------------------
# 5. Disclosure: revoked / deleted / purpose-restricted
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [DisclosureState.REVOKED, DisclosureState.DELETED, DisclosureState.PURPOSE_RESTRICTED],
)
def test_an_undisclosable_output_never_binds(state: DisclosureState) -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", disclosure=state)),
    )
    assert result.manifest is None
    problem = result.of_kind(ResolutionProblemKind.NOT_DISCLOSABLE)[0]
    assert str(state) in problem.detail


def test_an_undisclosable_candidate_does_not_make_a_single_port_ambiguous() -> None:
    """One live candidate plus one revoked candidate still has exactly one answer."""

    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(
            output(producer="occ-a", content="live"),
            output(producer="occ-a", content="gone", disclosure=DisclosureState.DELETED),
        ),
    )
    assert ResolutionProblemKind.AMBIGUOUS_SINGLE_PORT not in result.kinds
    assert ResolutionProblemKind.NOT_DISCLOSABLE in result.kinds


# --------------------------------------------------------------------------------------
# 6. Validity witnesses
# --------------------------------------------------------------------------------------


def test_a_usable_witness_is_recorded_on_the_binding() -> None:
    produced = output(producer="occ-a")
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(produced),
        witnesses=witnesses_for(produced),
    )
    assert only(result).witness_id == f"witness-{produced.acceptance_id}"


def test_a_missing_witness_is_reported() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a")),
        witnesses={},
    )
    assert result.manifest is None
    assert ResolutionProblemKind.WITNESS_MISSING in result.kinds


@pytest.mark.parametrize(
    "decision",
    [WitnessDecision.NEEDS_REVIEW, WitnessDecision.BLOCKED, WitnessDecision.UNAVAILABLE],
)
def test_a_witness_that_is_not_usable_is_reported(decision: WitnessDecision) -> None:
    produced = output(producer="occ-a")
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(produced),
        witnesses={produced.acceptance_id: witness_for(produced.acceptance_id, decision=decision)},
    )
    assert result.manifest is None
    assert ResolutionProblemKind.WITNESS_NOT_USABLE in result.kinds


def test_an_expired_witness_is_not_usable() -> None:
    produced = output(producer="occ-a")
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(produced),
        policy=default_policy(now_ms=9_000),
        witnesses={produced.acceptance_id: witness_for(produced.acceptance_id, not_after_ms=5_000)},
    )
    assert result.manifest is None
    problem = result.of_kind(ResolutionProblemKind.WITNESS_NOT_USABLE)[0]
    assert "stale" in problem.detail or "expired" in problem.detail


def test_a_scope_epoch_bump_invalidates_a_cached_witness() -> None:
    produced = output(producer="occ-a")
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(produced),
        policy=default_policy(scope_epochs={"scope-mission": 8}),
        witnesses={produced.acceptance_id: witness_for(produced.acceptance_id, scope_epoch=7)},
    )
    assert result.manifest is None
    assert ResolutionProblemKind.WITNESS_NOT_USABLE in result.kinds


def test_the_witness_requirement_can_be_waived_by_policy() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a")),
        policy=default_policy(require_witness=False),
        witnesses={},
    )
    assert result.ok
    assert only(result).witness_id is None


def witness_case(**witness_kwargs: object) -> ResolutionResult:
    produced = output(producer="occ-a")
    return resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(produced),
        witnesses={
            produced.acceptance_id: witness_for(produced.acceptance_id, **witness_kwargs)  # type: ignore[arg-type]
        },
    )


@pytest.mark.parametrize(
    "purpose",
    [WitnessPurpose.PLAN, WitnessPurpose.CONTEXT, WitnessPurpose.ACCEPT, WitnessPurpose.DISCLOSE],
)
def test_a_witness_issued_for_another_purpose_does_not_license_the_binding(
    purpose: WitnessPurpose,
) -> None:
    """§11.5: permission for one purpose is not permission for another."""

    result = witness_case(purpose=purpose)
    assert result.manifest is None
    problem = result.of_kind(ResolutionProblemKind.WITNESS_PURPOSE_MISMATCH)[0]
    assert str(purpose) in problem.detail


def test_the_accepted_purposes_can_be_widened_deliberately() -> None:
    produced = output(producer="occ-a")
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(produced),
        policy=default_policy(
            witness_purposes=frozenset({WitnessPurpose.START, WitnessPurpose.PLAN})
        ),
        witnesses={
            produced.acceptance_id: witness_for(produced.acceptance_id, purpose=WitnessPurpose.PLAN)
        },
    )
    assert result.ok


def test_a_witness_issued_to_another_consumer_does_not_license_the_binding() -> None:
    result = witness_case(consumer_id="mission-1:task-other")
    assert result.manifest is None
    problem = result.of_kind(ResolutionProblemKind.WITNESS_CONSUMER_MISMATCH)[0]
    assert "mission-1:task-other" in problem.detail


def test_a_witness_whose_consumer_ref_is_not_a_task_does_not_license_the_binding() -> None:
    result = witness_case(consumer_kind=TypedRefKind.METHOD)
    assert result.manifest is None
    assert ResolutionProblemKind.WITNESS_CONSUMER_MISMATCH in result.kinds


def test_a_witness_over_another_support_revision_does_not_license_the_binding() -> None:
    """The artifact carries support revision 1; a witness over 99 read other evidence."""

    result = witness_case(support_revision=99)
    assert result.manifest is None
    problem = result.of_kind(ResolutionProblemKind.WITNESS_SUPPORT_MISMATCH)[0]
    assert "99" in problem.detail


def test_an_unknown_scope_epoch_is_refused_rather_than_assumed_fresh() -> None:
    """I19 is a comparison; having nothing to compare against is not a pass."""

    produced = output(producer="occ-a")
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(produced),
        policy=ResolutionPolicy(),
        witnesses={produced.acceptance_id: witness_for(produced.acceptance_id)},
    )
    assert result.manifest is None
    assert ResolutionProblemKind.WITNESS_EPOCH_UNKNOWN in result.kinds


def test_an_unknown_scope_epoch_can_be_accepted_explicitly() -> None:
    produced = output(producer="occ-a")
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(produced),
        policy=ResolutionPolicy(allow_unknown_scope=True),
        witnesses={produced.acceptance_id: witness_for(produced.acceptance_id)},
    )
    assert result.ok


# --------------------------------------------------------------------------------------
# 6b. The identity half is the contract's own BoundInput
# --------------------------------------------------------------------------------------


def test_the_binding_embeds_the_contract_bound_input() -> None:
    produced = output(producer="occ-a", revision="r3")
    binding = only(
        resolve(
            consumer_binding(port("report")),
            [requirement("req-1", producer="occ-a")],
            index_of(produced),
        )
    )
    bound = binding.as_bound_input()
    assert isinstance(bound, BoundInput)
    assert bound == BoundInput(
        requirement_id="req-1",
        producer_result_id=produced.producer_result_id,
        acceptance_id=produced.acceptance_id,
        artifact_id=produced.artifact_id,
        content_hash=produced.content_hash,
        schema_ref=REPORT_SCHEMA,
        source_revision="r3",
    )


def test_the_binding_reads_its_identity_only_through_the_bound_input() -> None:
    """One home for the identity: the accessors cannot drift from the record."""

    binding = only(
        resolve(
            consumer_binding(port("report")),
            [requirement("req-1", producer="occ-a")],
            index_of(output(producer="occ-a")),
        )
    )
    bound = binding.bound
    for name in (
        "requirement_id",
        "producer_result_id",
        "acceptance_id",
        "artifact_id",
        "content_hash",
        "schema_ref",
        "source_revision",
    ):
        assert getattr(binding, name) == getattr(bound, name), name
    assert binding.to_json()["bound_input"] == bound.to_json()


# --------------------------------------------------------------------------------------
# 7. Pending producers: a symbolic binding, and a manifest that cannot be frozen
# --------------------------------------------------------------------------------------


def pending_result() -> ResolutionResult:
    return resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(completed=[]),
    )


def test_a_pending_producer_keeps_a_symbolic_binding() -> None:
    result = pending_result()
    assert result.manifest is not None
    assert result.manifest.bindings == ()
    assert [p.requirement_id for p in result.manifest.pending] == ["req-1"]
    assert ResolutionProblemKind.PENDING_PRODUCER in result.kinds


def test_a_manifest_with_a_pending_binding_is_not_frozen() -> None:
    manifest = pending_result().manifest
    assert manifest is not None
    assert manifest.is_frozen is False


def test_a_pending_manifest_refuses_to_produce_a_hash() -> None:
    manifest = pending_result().manifest
    assert manifest is not None
    with pytest.raises(ManifestNotFrozen):
        manifest.manifest_hash()


def test_a_pending_manifest_cannot_be_materialised() -> None:
    manifest = pending_result().manifest
    assert manifest is not None
    with pytest.raises(ManifestNotFrozen):
        materialise_plan(manifest, TargetRules(namespace="workspace-consumer"))


def test_a_resolved_port_survives_beside_a_pending_one() -> None:
    result = resolve(
        consumer_binding(port("report"), port("table", port_schema=REPORT_SCHEMA)),
        [
            requirement("req-1", producer="occ-a"),
            requirement("req-2", producer="occ-b", output_port="table", input_port="table"),
        ],
        index_of(output(producer="occ-a"), completed=["occ-a"]),
    )
    assert result.manifest is not None
    assert [b.input_port for b in result.manifest.bindings] == ["report"]
    assert [p.input_port for p in result.manifest.pending] == ["table"]


def test_a_completed_producer_with_no_accepted_output_leaves_the_port_unbound() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(completed=["occ-a"]),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.UNBOUND_REQUIRED_PORT in result.kinds


def test_a_required_port_with_no_requirement_at_all_is_unbound() -> None:
    result = resolve(consumer_binding(port("report")), [], index_of())
    assert result.manifest is None
    assert ResolutionProblemKind.UNBOUND_REQUIRED_PORT in result.kinds


def test_an_optional_port_with_no_candidate_is_not_a_problem() -> None:
    result = resolve(consumer_binding(port("report", required=False)), [], index_of())
    assert result.ok
    assert result.manifest is not None
    assert result.manifest.bindings == ()


# --------------------------------------------------------------------------------------
# 8. ORDER-only predecessors never enter the manifest (T015 / T066, pure part)
# --------------------------------------------------------------------------------------


def test_an_order_only_predecessor_never_enters_the_manifest() -> None:
    """It has accepted artifacts; it has no DataRequirement; it contributes nothing."""

    result = resolve(
        consumer_binding(port("report", required=False)),
        [],
        index_of(output(producer="occ-order-only", path="lint-report.txt")),
    )
    assert result.ok
    assert result.manifest is not None
    assert result.manifest.bindings == ()


def test_only_the_data_predecessor_of_a_mixed_pair_enters_the_manifest() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-data")],
        index_of(
            output(producer="occ-data", content="wanted"),
            output(producer="occ-order-only", content="not-wanted", path="lint.txt"),
        ),
    )
    assert result.ok
    assert result.manifest is not None
    producers = {b.producer_occurrence for b in result.manifest.bindings}
    assert producers == {OccurrenceId("occ-data")}


def test_ancestor_artifacts_are_not_swept_in_by_count() -> None:
    """The legacy "all accepted artifacts of all ancestors" semantics is gone."""

    many = [output(producer=f"occ-{n}", content=f"c{n}", path=f"file-{n}.md") for n in range(6)]
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-3")],
        index_of(*many),
    )
    assert result.ok
    assert result.manifest is not None
    assert len(result.manifest.bindings) == 1


def test_a_requirement_for_an_undeclared_port_is_rejected() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a", input_port="ghost")],
        index_of(output(producer="occ-a", output_port="report")),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.UNDECLARED_PORT in result.kinds


def test_consumer_occurrence_is_a_required_argument() -> None:
    """Without it there is nothing to compare a requirement's consumer against."""

    with pytest.raises(TypeError):
        resolve_declared_inputs(  # type: ignore[call-arg]
            consumer_binding(port("report", required=False)),
            [],
            index_of(),
            witnesses={},
            policy=default_policy(),
        )


def test_a_requirement_addressed_to_another_consumer_is_rejected() -> None:
    result = resolve(
        consumer_binding(port("report", required=False)),
        [requirement("req-1", producer="occ-a", consumer=OccurrenceId("occ-someone-else"))],
        index_of(output(producer="occ-a")),
    )
    assert result.manifest is None
    assert ResolutionProblemKind.FOREIGN_REQUIREMENT in result.kinds


# --------------------------------------------------------------------------------------
# 9. Provisional (speculative) bindings
# --------------------------------------------------------------------------------------


def provisional_case(authorised: bool) -> ResolutionResult:
    return resolve(
        consumer_binding(port("report", cardinality=PortCardinality.SINGLE)),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", provisional=True)),
        policy=default_policy(
            provisional_ports=frozenset({"report"}) if authorised else frozenset()
        ),
    )


def test_a_provisional_candidate_on_an_authorised_port_is_marked() -> None:
    result = provisional_case(True)
    assert result.ok
    binding = only(result)
    assert binding.provisional is True
    assert result.manifest is not None
    assert result.manifest.has_provisional is True


def test_a_provisional_candidate_outside_the_authorised_scope_is_refused() -> None:
    result = provisional_case(False)
    assert result.manifest is None
    assert ResolutionProblemKind.PROVISIONAL_NOT_AUTHORIZED in result.kinds


def test_a_provisional_binding_does_not_satisfy_a_required_port() -> None:
    """TG §4.3: speculation may run, but may not claim the required DATA is met."""

    consumer = consumer_binding(port("report"))
    result = provisional_case(True)
    assert result.manifest is not None
    assert result.manifest.required_ports_satisfied(consumer) is False


def test_a_firm_binding_does_satisfy_a_required_port() -> None:
    consumer = consumer_binding(port("report"))
    result = resolve(
        consumer, [requirement("req-1", producer="occ-a")], index_of(output(producer="occ-a"))
    )
    assert result.manifest is not None
    assert result.manifest.required_ports_satisfied(consumer) is True


# --------------------------------------------------------------------------------------
# 10. Manifest hash: stable, order-independent, content-sensitive
# --------------------------------------------------------------------------------------


def two_port_manifest(*, content_b: str = "b") -> InputManifest:
    result = resolve(
        consumer_binding(
            port("report"),
            port("table"),
        ),
        [
            requirement("req-1", producer="occ-a"),
            requirement("req-2", producer="occ-b", output_port="table", input_port="table"),
        ],
        index_of(
            output(producer="occ-a", content="a"),
            output(producer="occ-b", output_port="table", content=content_b, path="table.csv"),
        ),
    )
    assert result.manifest is not None, result.problems
    return result.manifest


def test_the_manifest_hash_is_stable_across_calls() -> None:
    assert two_port_manifest().manifest_hash() == two_port_manifest().manifest_hash()


def test_the_manifest_hash_does_not_depend_on_the_requirement_input_order() -> None:
    ports = (port("report"), port("table"))
    requirements = [
        requirement("req-1", producer="occ-a"),
        requirement("req-2", producer="occ-b", output_port="table", input_port="table"),
    ]
    outputs = [
        output(producer="occ-a", content="a"),
        output(producer="occ-b", output_port="table", content="b", path="table.csv"),
    ]
    hashes = set()
    for seed in range(5):
        rng = random.Random(seed)
        shuffled_requirements = list(requirements)
        shuffled_outputs = list(outputs)
        rng.shuffle(shuffled_requirements)
        rng.shuffle(shuffled_outputs)
        result = resolve(
            consumer_binding(*ports), shuffled_requirements, index_of(*shuffled_outputs)
        )
        assert result.manifest is not None
        hashes.add(result.manifest.manifest_hash())
    assert len(hashes) == 1


def test_the_manifest_hash_changes_with_the_content() -> None:
    assert two_port_manifest().manifest_hash() != two_port_manifest(content_b="b2").manifest_hash()


def test_the_manifest_hash_changes_with_the_declared_set_port_order() -> None:
    def ordered(sequence: tuple[str, ...]) -> str:
        result = set_port_case(
            PortOrdering.EXPLICIT,
            outputs=[output(producer="occ-a", content="a"), output(producer="occ-b", content="b")],
            requirements=[
                requirement("req-a", producer="occ-a", input_port="reports"),
                requirement("req-b", producer="occ-b", input_port="reports"),
            ],
            policy=default_policy(explicit_orders=(ExplicitPortOrder("reports", sequence),)),
        )
        assert result.manifest is not None
        return result.manifest.manifest_hash()

    assert ordered(("req-a", "req-b")) != ordered(("req-b", "req-a"))


def test_the_manifest_is_immutable() -> None:
    manifest = two_port_manifest()
    with pytest.raises(Exception):
        manifest.bindings = ()  # type: ignore[misc]


def test_the_manifest_round_trips_through_json() -> None:
    manifest = two_port_manifest()
    payload = manifest.to_json()
    assert payload["manifest_hash"] == manifest.manifest_hash()
    assert len(payload["bindings"]) == 2


# --------------------------------------------------------------------------------------
# 11. Materialisation plan: identity, normalisation and conflict
# --------------------------------------------------------------------------------------


def two_attempt_manifest(*, same_content: bool = False) -> InputManifest:
    result = resolve(
        consumer_binding(
            port(
                "reports",
                cardinality=PortCardinality.SET,
                ordering=PortOrdering.BY_PRODUCER_ORDINAL,
            )
        ),
        [
            requirement("req-a", producer="occ-a", input_port="reports"),
            requirement("req-b", producer="occ-b", input_port="reports"),
        ],
        index_of(
            output(
                producer="occ-a",
                content="a",
                namespace="attempt-A",
                path="report.md",
                producer_ordinal=1,
            ),
            output(
                producer="occ-b",
                content="a" if same_content else "b",
                namespace="attempt-B",
                path="report.md",
                producer_ordinal=2,
            ),
        ),
    )
    assert result.manifest is not None, result.problems
    return result.manifest


def test_attempt_a_and_attempt_b_are_two_artifact_references() -> None:
    """TG §10.2: the same relative path in two namespaces is two artifacts."""

    manifest = two_attempt_manifest()
    assert len(manifest.bindings) == 2
    identities = {b.source_identity for b in manifest.bindings}
    assert identities == {
        ResourceIdentity("attempt-A", "report.md"),
        ResourceIdentity("attempt-B", "report.md"),
    }


def test_two_hashes_aimed_at_one_target_path_is_a_conflict() -> None:
    plan = materialise_plan(two_attempt_manifest(), TargetRules(namespace="workspace-consumer"))
    assert plan.ok is False
    problem = plan.problems[0]
    assert problem.kind is ResolutionProblemKind.TARGET_PATH_CONFLICT
    assert "report.md" in problem.detail


def test_the_conflicting_plan_does_not_pick_a_winner() -> None:
    plan = materialise_plan(two_attempt_manifest(), TargetRules(namespace="workspace-consumer"))
    targets = {entry.target.path for entry in plan.entries}
    assert "report.md" not in targets


def test_preserving_the_source_namespace_separates_the_two_attempts() -> None:
    plan = materialise_plan(
        two_attempt_manifest(),
        TargetRules(namespace="workspace-consumer", preserve_source_namespace=True),
    )
    assert plan.ok
    assert {entry.target.path for entry in plan.entries} == {
        "attempt-A/report.md",
        "attempt-B/report.md",
    }


def test_the_same_hash_at_one_target_path_is_not_a_conflict() -> None:
    plan = materialise_plan(
        two_attempt_manifest(same_content=True), TargetRules(namespace="workspace-consumer")
    )
    assert plan.ok
    assert len(plan.entries) == 1
    assert len(plan.entries[0].binding_ids) == 2


def test_a_port_prefix_places_the_bindings_under_the_port() -> None:
    plan = materialise_plan(
        two_attempt_manifest(),
        TargetRules(
            namespace="workspace-consumer",
            port_prefixes={"reports": "inputs/reports"},
            preserve_source_namespace=True,
        ),
    )
    assert {entry.target.path for entry in plan.entries} == {
        "inputs/reports/attempt-A/report.md",
        "inputs/reports/attempt-B/report.md",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("./docs/./report.md", "docs/report.md"),
        ("docs//report.md", "docs/report.md"),
        ("docs\\report.md", "docs/report.md"),
        ("/docs/report.md", "docs/report.md"),
    ],
)
def test_the_target_path_is_normalised(raw: str, expected: str) -> None:
    assert ResourceIdentity("ns", raw).path == expected


def test_paths_that_differ_only_by_separator_collide_on_one_target() -> None:
    manifest_bindings = two_attempt_manifest().bindings
    assert manifest_bindings  # sanity: the fixture produced something to place
    plan = materialise_plan(
        two_attempt_manifest(same_content=True), TargetRules(namespace="workspace-consumer")
    )
    assert len(plan.entries) == 1


def case_variant_manifest(*, same_content: bool = False, swap: bool = False) -> InputManifest:
    result = resolve(
        consumer_binding(
            port(
                "reports",
                cardinality=PortCardinality.SET,
                ordering=PortOrdering.BY_PRODUCER_ORDINAL,
            )
        ),
        [
            requirement("req-a", producer="occ-a", input_port="reports"),
            requirement("req-b", producer="occ-b", input_port="reports"),
        ],
        index_of(
            output(
                producer="occ-a",
                content="a",
                namespace="ns",
                path="report.MD" if swap else "Report.md",
                producer_ordinal=1,
            ),
            output(
                producer="occ-b",
                content="a" if same_content else "b",
                namespace="ns",
                path="Report.md" if swap else "report.MD",
                producer_ordinal=2,
            ),
        ),
    )
    assert result.manifest is not None, result.problems
    return result.manifest


def test_case_insensitive_rules_report_a_case_only_collision() -> None:
    plan = materialise_plan(
        case_variant_manifest(),
        TargetRules(namespace="workspace-consumer", case_insensitive=True),
    )
    assert plan.ok is False
    assert plan.problems[0].kind is ResolutionProblemKind.TARGET_PATH_CONFLICT


def test_case_insensitive_rules_fold_one_agreed_file_to_a_canonical_spelling() -> None:
    """The folded spelling is written down, not whichever binding was visited first."""

    plan = materialise_plan(
        case_variant_manifest(same_content=True),
        TargetRules(namespace="workspace-consumer", case_insensitive=True),
    )
    assert plan.ok
    assert [entry.target.path for entry in plan.entries] == ["report.md"]


def test_the_canonical_spelling_does_not_depend_on_the_binding_order() -> None:
    plan = materialise_plan(
        case_variant_manifest(same_content=True, swap=True),
        TargetRules(namespace="workspace-consumer", case_insensitive=True),
    )
    assert [entry.target.path for entry in plan.entries] == ["report.md"]


def test_a_target_path_conflict_names_every_port_that_aimed_at_the_place() -> None:
    result = resolve(
        consumer_binding(port("report"), port("table")),
        [
            requirement("req-1", producer="occ-a"),
            requirement("req-2", producer="occ-b", output_port="table", input_port="table"),
        ],
        index_of(
            output(producer="occ-a", content="a", namespace="ns-a", path="shared.md"),
            output(
                producer="occ-b",
                output_port="table",
                content="b",
                namespace="ns-b",
                path="shared.md",
            ),
        ),
    )
    assert result.manifest is not None, result.problems
    plan = materialise_plan(result.manifest, TargetRules(namespace="workspace-consumer"))
    problem = plan.problems[0]
    assert problem.kind is ResolutionProblemKind.TARGET_PATH_CONFLICT
    assert problem.input_ports == ("report", "table")
    assert explain((problem,))[0].input_ports == ("report", "table")


def test_case_sensitive_rules_keep_the_two_targets_apart() -> None:
    plan = materialise_plan(
        case_variant_manifest(),
        TargetRules(namespace="workspace-consumer", case_insensitive=False),
    )
    assert plan.ok
    assert {entry.target.path for entry in plan.entries} == {"Report.md", "report.MD"}


def test_a_parent_traversal_target_is_invalid() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", path="../escape.md")),
    )
    assert result.manifest is not None, result.problems
    plan = materialise_plan(result.manifest, TargetRules(namespace="workspace-consumer"))
    assert plan.ok is False
    assert plan.problems[0].kind is ResolutionProblemKind.TARGET_PATH_INVALID


def test_the_plan_carries_the_consumer_namespace() -> None:
    plan = materialise_plan(
        two_attempt_manifest(same_content=True), TargetRules(namespace="workspace-consumer")
    )
    assert {entry.target.namespace for entry in plan.entries} == {"workspace-consumer"}


def test_the_plan_hash_is_stable_and_order_independent() -> None:
    rules = TargetRules(namespace="workspace-consumer", preserve_source_namespace=True)
    first = materialise_plan(two_attempt_manifest(), rules)
    second = materialise_plan(two_attempt_manifest(), rules)
    assert first.plan_hash() == second.plan_hash()


def test_materialise_plan_writes_nothing_to_disk(tmp_path: Path) -> None:
    materialise_plan(
        two_attempt_manifest(),
        TargetRules(namespace=str(tmp_path), preserve_source_namespace=True),
    )
    assert list(tmp_path.iterdir()) == []


def test_a_materialisation_plan_is_immutable() -> None:
    plan: MaterialisationPlan = materialise_plan(
        two_attempt_manifest(same_content=True), TargetRules(namespace="workspace-consumer")
    )
    with pytest.raises(Exception):
        plan.entries = ()  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# 12. explain(), purity and isolation
# --------------------------------------------------------------------------------------


def test_explain_gives_one_structured_reason_per_problem() -> None:
    result = resolve(
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a", content="a"), output(producer="occ-a", content="b")),
    )
    reasons = explain(result.problems)
    assert len(reasons) == len(result.problems)
    assert reasons[0].kind is ResolutionProblemKind.AMBIGUOUS_SINGLE_PORT
    assert reasons[0].summary and reasons[0].remedy


def test_explain_covers_every_problem_kind() -> None:
    from agent_orchestrator.artifacts.input_bindings import ResolutionProblem

    for kind in ResolutionProblemKind:
        reason = explain((ResolutionProblem(kind=kind, detail="d"),))[0]
        assert reason.remedy.strip(), kind


def test_explain_never_proposes_an_automatic_winner() -> None:
    from agent_orchestrator.artifacts.input_bindings import ResolutionProblem

    remedy = explain(
        (ResolutionProblem(kind=ResolutionProblemKind.AMBIGUOUS_SINGLE_PORT, detail="d"),)
    )[0].remedy.lower()
    assert "topolog" not in remedy
    assert "last" not in remedy


def test_resolution_is_a_pure_function() -> None:
    args = (
        consumer_binding(port("report")),
        [requirement("req-1", producer="occ-a")],
        index_of(output(producer="occ-a")),
    )
    first = resolve(*args)
    second = resolve(*args)
    assert first == second


def module_imports() -> set[str]:
    """Every module name ``input_bindings`` imports, read from its AST.

    A substring scan over the source text would be fooled by a docstring that
    mentions ``storage`` and would miss ``importlib.import_module("os")``; the
    import nodes are the thing the rule is actually about.
    """

    import agent_orchestrator.artifacts.input_bindings as module

    tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections.abc",
        "dataclasses",
        "enum",
        "typing",
        "..contracts.evidence_state",
        "..contracts.htn",
        "..contracts.models",
        "..contracts.semantic_base",
        ".paths",
    }
)


def test_the_module_imports_nothing_outside_the_allowlist() -> None:
    assert module_imports() <= ALLOWED_IMPORTS, sorted(module_imports() - ALLOWED_IMPORTS)


def test_the_module_imports_no_forbidden_layer() -> None:
    for name in module_imports():
        assert "storage" not in name, name
        assert "scheduling" not in name, name
        assert "graph" not in name, name
        assert not name.endswith(".versioning"), name
        assert not name.endswith(".workspace"), name
        assert not name.endswith(".store"), name


def test_the_module_imports_nothing_that_touches_the_filesystem() -> None:
    for name in module_imports():
        assert name.split(".")[0] not in {"os", "io", "pathlib", "shutil", "sqlite3"}, name


def test_the_legacy_versioning_helpers_are_untouched_by_this_module() -> None:
    from agent_orchestrator.artifacts import versioning

    assert hasattr(versioning, "collect_upstream_inputs")
    assert hasattr(versioning, "merge_accepted")
    assert hasattr(versioning, "ArtifactConflict")
