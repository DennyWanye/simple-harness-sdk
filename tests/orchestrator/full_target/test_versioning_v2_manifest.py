# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3b red tests: the new mode materialises a manifest, the old one still sweeps.

Five properties (plan §24.1 decisions 3, 4 and 11; annex TG §10.1-10.3):

1. **Only the manifest.**  ``resolve_input_manifest`` consults the consumer's own
   declared ``DataRequirement`` rows and nothing else, and ``materialise_v2``
   places exactly the entries the manifest resolved to.
2. **ORDER grants no read.**  A predecessor connected only by an ``OrderConstraint``
   contributes no file however many artifacts it accepted — the same world run
   through the *legacy* ``collect_upstream_inputs`` does hand that file over, which
   is what makes the two modes' difference a fact and not a claim (T015 / T066).
3. **One path, two hashes, no merge.**  A collision is ``ArtifactConflict`` with
   both candidates named; the same hash at one place is one file two bindings agree
   on and is not a conflict.  The old exact-path protection is therefore not
   relaxed by the new path existing.
4. **A damaged projection stops the work.**  On the execution and materialisation
   path an unorderable projection raises ``GraphIntegrityError``; the diagnostic
   readers keep calling the legacy functions and keep tolerating it.
5. **The legacy functions did not move.**  The source text of ``merge_accepted``,
   ``collect_upstream_inputs`` and ``topological`` is hash-locked, and their
   behaviour is re-checked against the step-3 / step-4 shapes.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from agent_orchestrator.artifacts import versioning
from agent_orchestrator.artifacts.input_bindings import (
    AcceptedOutput,
    AcceptedOutputsIndex,
    InputManifest,
    ManifestNotFrozen,
    ResolutionPolicy,
    ResolutionProblemKind,
    ResourceIdentity,
    TargetRules,
    resolve_declared_inputs,
)
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.versioning import (
    ArtifactConflict,
    collect_upstream_inputs,
    manifest_upstream_inputs,
    materialise_v2,
    merge_accepted,
    resolve_input_manifest,
    topological,
)
from agent_orchestrator.artifacts.workspace import WorkspaceError, WorkspaceManager
from agent_orchestrator.contracts import Artifact, Budget, Task, TaskStatus
from agent_orchestrator.contracts.evidence_state import (
    Availability,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from agent_orchestrator.contracts.htn import (
    ContractRevision,
    DataRequirement,
    GoalSignature,
    MissionRef,
    ObligationId,
    OccurrenceId,
    OccurrenceSpec,
    OrderConstraint,
    PlanRevision,
    PortCardinality,
    PortOrdering,
    PortSpec,
    ReleaseCondition,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind, VersionedRef
from agent_orchestrator.graph.projection_validation import GraphIntegrityError
from agent_orchestrator.graph.task_network import TaskNetworkSnapshot

# --------------------------------------------------------------------------------------
# The world: one producer, one ORDER-only predecessor, one consumer
# --------------------------------------------------------------------------------------

MISSION = MissionRef("m-v2")
OCC_PRODUCER = OccurrenceId("occ-producer")
OCC_ORDER_ONLY = OccurrenceId("occ-order-only")
OCC_CONSUMER = OccurrenceId("occ-consumer")
T_PRODUCER = TaskRef("t-producer")
T_ORDER_ONLY = TaskRef("t-order-only")
T_CONSUMER = TaskRef("t-consumer")
DUTY = ObligationId("o-1")
SCHEMA = VersionedRef(id="report", version=1, content_hash="a" * 64)
ASSURANCE = "assurance.default"
FRESHNESS = "freshness.default"
NOW_MS = 1_000


def digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def signature(name: str) -> GoalSignature:
    return GoalSignature(
        signature_id=name,
        version=1,
        parameter_schema_ref=VersionedRef(id="params", version=1, content_hash="b" * 64),
        output_schema_ref=VersionedRef(id="out", version=1, content_hash="c" * 64),
        statement=f"{name} is produced",
    )


def binding(
    task_id: TaskRef,
    *,
    inputs: Sequence[PortSpec] = (),
    outputs: Sequence[PortSpec] = (),
) -> TaskSemanticBindingV1:
    return TaskSemanticBindingV1(
        task_id=task_id,
        obligation_id=DUTY,
        contract_revision=ContractRevision(1),
        contract_hash=digest(str(task_id)),
        form=TaskForm.PRIMITIVE,
        goal_signature=signature(f"sig-{task_id}"),
        requirement_refs=("req-1",),
        input_ports=tuple(inputs),
        output_ports=tuple(outputs),
        operator_ref=VersionedRef(id="op", version=1, content_hash="d" * 64),
        semantic_scope="mission",
    )


def port(
    key: str,
    *,
    required: bool = True,
    cardinality: PortCardinality = PortCardinality.SINGLE,
    ordering: PortOrdering | None = None,
    order_key: str | None = None,
) -> PortSpec:
    return PortSpec(
        port_key=key,
        schema_ref=SCHEMA,
        cardinality=cardinality,
        required=required,
        ordering=ordering,
        order_key=order_key,
    )


def requirement(requirement_id: str = "req-data") -> DataRequirement:
    return DataRequirement(
        requirement_id=requirement_id,
        producer_occurrence=OCC_PRODUCER,
        output_port="report",
        consumer_occurrence=OCC_CONSUMER,
        input_port="source",
        schema_ref=SCHEMA,
        assurance_policy_ref=ASSURANCE,
        freshness_policy_ref=FRESHNESS,
    )


def output(
    *,
    acceptance_id: str = "acc-1",
    artifact_id: str = "art-1",
    content: bytes = b"# report\n",
    path: str = "report.md",
    namespace: str = "workspace:producer",
    producer: OccurrenceId = OCC_PRODUCER,
    producer_task: TaskRef = T_PRODUCER,
    port_key: str = "report",
) -> AcceptedOutput:
    return AcceptedOutput(
        producer_occurrence=producer,
        producer_task_ref=producer_task,
        output_port=port_key,
        producer_result_id=f"result-{artifact_id}",
        acceptance_id=acceptance_id,
        support_revision=1,
        artifact_id=artifact_id,
        content_hash=hashlib.sha256(content).hexdigest(),
        schema_ref=SCHEMA,
        source_revision="rev-1",
        source_identity=ResourceIdentity(namespace=namespace, path=path),
    )


def witness_for(*outputs: AcceptedOutput) -> dict[str, ValidityWitness]:
    return {
        item.acceptance_id: ValidityWitness(
            witness_id=f"w-{item.acceptance_id}",
            consumer_ref=TypedRef(
                kind=TypedRefKind.TASK,
                id=str(T_CONSUMER),
                revision=1,
                content_hash="e" * 64,
            ),
            purpose=WitnessPurpose.START,
            truth=TruthValue.TRUE,
            freshness=Validity.CURRENT,
            availability=Availability.READABLE,
            decision=WitnessDecision.USABLE,
            scope_id="scope-mission",
            scope_epoch=1,
            support_revision=1,
            as_of_ms=NOW_MS,
        )
        for item in outputs
    }


def network(
    *,
    requirements: Sequence[DataRequirement] = (),
    order: Sequence[OrderConstraint] = (),
    include_order_only: bool = True,
) -> TaskNetworkSnapshot:
    """The plan: producer → consumer by DATA, order-only → consumer by ORDER."""

    occurrences = [
        OccurrenceSpec(
            occurrence_id=OCC_PRODUCER,
            task_id=T_PRODUCER,
            obligation_id=DUTY,
            form=TaskForm.PRIMITIVE,
        ),
        OccurrenceSpec(
            occurrence_id=OCC_CONSUMER,
            task_id=T_CONSUMER,
            obligation_id=DUTY,
            form=TaskForm.PRIMITIVE,
        ),
    ]
    bindings = [
        binding(T_PRODUCER, outputs=(port("report"),)),
        binding(T_CONSUMER, inputs=(port("source"),)),
    ]
    if include_order_only:
        occurrences.append(
            OccurrenceSpec(
                occurrence_id=OCC_ORDER_ONLY,
                task_id=T_ORDER_ONLY,
                obligation_id=DUTY,
                form=TaskForm.PRIMITIVE,
            )
        )
        bindings.append(binding(T_ORDER_ONLY, outputs=(port("report"),)))
    return TaskNetworkSnapshot(
        mission_id=MISSION,
        plan_revision=PlanRevision(1),
        occurrences=tuple(occurrences),
        task_bindings=tuple(bindings),
        root_occurrence_ids=(OCC_PRODUCER, OCC_CONSUMER)
        + ((OCC_ORDER_ONLY,) if include_order_only else ()),
        order_constraints=tuple(order),
        data_requirements=tuple(requirements),
        required_obligations=(DUTY,),
    )


def order_edge(before: OccurrenceId, after: OccurrenceId) -> OrderConstraint:
    return OrderConstraint(before=before, after=after, release_condition=ReleaseCondition.ACCEPTED)


def resolve(
    snapshot: TaskNetworkSnapshot,
    index: AcceptedOutputsIndex,
    witnesses: Mapping[str, ValidityWitness],
    **kwargs: object,
):
    return resolve_input_manifest(
        snapshot.binding_for_occurrence(OCC_CONSUMER),
        snapshot,
        index,
        consumer_occurrence=OCC_CONSUMER,
        witnesses=witnesses,
        policy=ResolutionPolicy(now_ms=NOW_MS, scope_epochs={"scope-mission": 1}),
        **kwargs,  # type: ignore[arg-type]
    )


def index_of(
    *outputs: AcceptedOutput, completed: Sequence[OccurrenceId] = ()
) -> AcceptedOutputsIndex:
    return AcceptedOutputsIndex(
        outputs=tuple(outputs),
        completed_producers=frozenset(completed or {item.producer_occurrence for item in outputs}),
    )


def rules(**kwargs: object) -> TargetRules:
    base: dict[str, object] = {"namespace": "workspace:consumer"}
    base.update(kwargs)
    return TargetRules(**base)  # type: ignore[arg-type]


# ====================================================================== only the manifest
def test_the_declared_requirement_resolves_to_one_binding() -> None:
    produced = output()
    result = resolve(
        network(requirements=(requirement(),)), index_of(produced), witness_for(produced)
    )
    assert result.ok, [problem.to_json() for problem in result.problems]
    assert result.manifest is not None
    assert [item.artifact_id for item in result.manifest.bindings] == ["art-1"]


def test_a_required_port_with_no_requirement_is_reported_unbound_not_swept() -> None:
    """Decision 4 in one assertion: the ancestors' accepted files do not fill the gap."""

    produced = output()
    result = resolve(network(), index_of(produced), witness_for(produced))
    assert result.manifest is None
    assert result.kinds == {ResolutionProblemKind.UNBOUND_REQUIRED_PORT}


def test_the_manifest_freezes_the_exact_content_hash() -> None:
    produced = output(content=b"exact bytes\n")
    result = resolve(
        network(requirements=(requirement(),)), index_of(produced), witness_for(produced)
    )
    only = result.manifest.bindings[0]
    assert only.content_hash == hashlib.sha256(b"exact bytes\n").hexdigest()


def test_a_producer_that_has_not_finished_leaves_the_binding_symbolic() -> None:
    produced = output()
    result = resolve(
        network(requirements=(requirement(),)),
        AcceptedOutputsIndex(outputs=(produced,), completed_producers=frozenset()),
        witness_for(produced),
    )
    assert result.manifest is not None and not result.manifest.is_frozen
    assert result.manifest.provisional_ports == frozenset()


def test_a_requirement_aimed_at_another_consumer_is_not_absorbed() -> None:
    produced = output()
    foreign = DataRequirement(
        requirement_id="req-foreign",
        producer_occurrence=OCC_PRODUCER,
        output_port="report",
        consumer_occurrence=OCC_ORDER_ONLY,
        input_port="source",
        schema_ref=SCHEMA,
        assurance_policy_ref=ASSURANCE,
        freshness_policy_ref=FRESHNESS,
    )
    snapshot = network(requirements=(foreign,))
    result = resolve(snapshot, index_of(produced), witness_for(produced))
    # The network-level filter means it is never even considered for this consumer…
    assert result.manifest is None
    assert result.kinds == {ResolutionProblemKind.UNBOUND_REQUIRED_PORT}
    # …and it is not the only line of defence: handed in directly, the resolver
    # refuses it by name rather than absorbing it as one of this task's inputs.
    direct = resolve_declared_inputs(
        snapshot.binding_for_occurrence(OCC_CONSUMER),
        (foreign,),
        index_of(produced),
        witnesses=witness_for(produced),
        policy=ResolutionPolicy(now_ms=NOW_MS, scope_epochs={"scope-mission": 1}),
        consumer_occurrence=OCC_CONSUMER,
    )
    assert ResolutionProblemKind.FOREIGN_REQUIREMENT in direct.kinds


# ============================================================== ORDER grants no read
def test_an_order_only_predecessor_contributes_no_file() -> None:
    produced = output()
    stranger = output(
        acceptance_id="acc-2",
        artifact_id="art-2",
        content=b"# other\n",
        producer=OCC_ORDER_ONLY,
        producer_task=T_ORDER_ONLY,
    )
    snapshot = network(
        requirements=(requirement(),), order=(order_edge(OCC_ORDER_ONLY, OCC_CONSUMER),)
    )
    result = resolve(snapshot, index_of(produced, stranger), witness_for(produced, stranger))
    assert [item.artifact_id for item in result.manifest.bindings] == ["art-1"]


def test_the_same_world_does_hand_that_file_over_through_the_legacy_sweep() -> None:
    """The contrast that makes the property above a fact rather than a claim."""

    def task(task_id: str, *, deps: tuple[str, ...], artifact_ids: tuple[str, ...]) -> Task:
        return Task(
            id=task_id,
            mission_id=str(MISSION),
            parent_task_ids=(),
            dependency_ids=deps,
            goal=f"goal {task_id}",
            rationale="legacy shape",
            success_criteria=("file:report.md",),
            verification_policy=("format_check",),
            allowed_tools=("workspace_read_file",),
            budget=Budget(max_tokens=1_000, max_attempts=1),
            priority=1.0,
            status=TaskStatus.COMPLETED,
            version=1,
            accepted_artifacts=artifact_ids,
        )

    def artifact(artifact_id: str, task_id: str, path: str, content: bytes) -> Artifact:
        return Artifact(
            id=artifact_id,
            mission_id=str(MISSION),
            task_id=task_id,
            attempt_id=f"{task_id}:attempt-1",
            type="file",
            path=path,
            version=1,
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            produced_by="worker",
        )

    tasks = {
        "task-1": task("task-1", deps=(), artifact_ids=("art-1",)),
        "task-2": task("task-2", deps=(), artifact_ids=("art-2",)),
        "task-3": task("task-3", deps=("task-1", "task-2"), artifact_ids=()),
    }
    artifacts = {
        "task-1": [artifact("art-1", "task-1", "report.md", b"# report\n")],
        "task-2": [artifact("art-2", "task-2", "other.md", b"# other\n")],
    }
    legacy = collect_upstream_inputs(tasks["task-3"], tasks, artifacts)
    # The ORDER-only predecessor (task-2) *is* swept in by the old semantics…
    assert sorted(item.path for item in legacy) == ["other.md", "report.md"]


def test_an_order_edge_does_not_add_a_requirement_to_the_manifest() -> None:
    produced = output()
    with_order = network(
        requirements=(requirement(),), order=(order_edge(OCC_ORDER_ONLY, OCC_CONSUMER),)
    )
    without = network(requirements=(requirement(),))
    a = resolve(with_order, index_of(produced), witness_for(produced))
    b = resolve(without, index_of(produced), witness_for(produced))
    assert a.manifest.manifest_hash() == b.manifest.manifest_hash()


def test_an_order_only_predecessor_with_the_same_path_is_still_excluded() -> None:
    produced = output()
    shadow = output(
        acceptance_id="acc-2",
        artifact_id="art-2",
        content=b"# different content, same name\n",
        producer=OCC_ORDER_ONLY,
        producer_task=T_ORDER_ONLY,
    )
    snapshot = network(
        requirements=(requirement(),), order=(order_edge(OCC_ORDER_ONLY, OCC_CONSUMER),)
    )
    manifest = resolve(snapshot, index_of(produced, shadow), witness_for(produced, shadow)).manifest
    inputs = manifest_upstream_inputs(manifest, rules(), network=snapshot)
    assert [item.artifact_id for item in inputs] == ["art-1"]


# ====================================================== one path, two hashes, no merge
def _two_hashes_at_one_place() -> InputManifest:
    first = output(content=b"# one\n")
    second = output(
        acceptance_id="acc-2", artifact_id="art-2", content=b"# two\n", path="report.md"
    )
    snapshot = network(
        requirements=(
            requirement(),
            DataRequirement(
                requirement_id="req-data-2",
                producer_occurrence=OCC_PRODUCER,
                output_port="report",
                consumer_occurrence=OCC_CONSUMER,
                input_port="extra",
                schema_ref=SCHEMA,
                assurance_policy_ref=ASSURANCE,
                freshness_policy_ref=FRESHNESS,
            ),
        )
    )
    consumer = binding(T_CONSUMER, inputs=(port("source"), port("extra")))
    return InputManifest(
        consumer_task_ref=T_CONSUMER,
        bindings=(
            _resolved(consumer, snapshot, first, "req-data", "source"),
            _resolved(consumer, snapshot, second, "req-data-2", "extra"),
        ),
    )


def _resolved(consumer, snapshot, produced, requirement_id, input_port):
    from agent_orchestrator.artifacts.input_bindings import ResolvedInputBinding
    from agent_orchestrator.contracts.htn import BoundInput, SourceRevisionPolicy

    return ResolvedInputBinding(
        binding_id=f"bind-{requirement_id}",
        bound=BoundInput(
            requirement_id=requirement_id,
            producer_result_id=produced.producer_result_id,
            acceptance_id=produced.acceptance_id,
            artifact_id=produced.artifact_id,
            content_hash=produced.content_hash,
            schema_ref=SCHEMA,
            source_revision=produced.source_revision,
        ),
        producer_task_ref=produced.producer_task_ref,
        producer_occurrence=produced.producer_occurrence,
        output_port=produced.output_port,
        support_revision=1,
        consumer_task_ref=consumer.task_id,
        input_port=input_port,
        port_ordinal=0,
        source_identity=produced.source_identity,
        produced_schema_ref=SCHEMA,
        read_policy=ASSURANCE,
        freshness_policy=FRESHNESS,
        disclosure_scope="mission",
        source_revision_policy=SourceRevisionPolicy.PINNED,
    )


def test_two_different_hashes_aimed_at_one_place_is_a_conflict() -> None:
    with pytest.raises(ArtifactConflict) as caught:
        manifest_upstream_inputs(_two_hashes_at_one_place(), rules())
    assert str(ResolutionProblemKind.TARGET_PATH_CONFLICT) in str(caught.value)


def test_the_conflict_names_both_candidates() -> None:
    with pytest.raises(ArtifactConflict) as caught:
        manifest_upstream_inputs(_two_hashes_at_one_place(), rules())
    assert "art-1" in str(caught.value) and "art-2" in str(caught.value)


def test_the_conflict_is_not_resolved_by_a_topological_preference() -> None:
    """TG §10.2: "B is further down the order" is not a reason to pick B."""

    with pytest.raises(ArtifactConflict):
        manifest_upstream_inputs(
            _two_hashes_at_one_place(),
            rules(),
            network=network(requirements=(requirement(),)),
        )


def test_two_bindings_agreeing_on_one_hash_are_one_file() -> None:
    produced = output()
    consumer = binding(T_CONSUMER, inputs=(port("source"), port("extra")))
    snapshot = network(requirements=(requirement(),))
    manifest = InputManifest(
        consumer_task_ref=T_CONSUMER,
        bindings=(
            _resolved(consumer, snapshot, produced, "req-data", "source"),
            _resolved(consumer, snapshot, produced, "req-data-2", "extra"),
        ),
    )
    inputs = manifest_upstream_inputs(manifest, rules())
    assert len(inputs) == 1


def test_two_namespaces_with_one_relative_name_stay_apart_under_preserve() -> None:
    a = output(namespace="workspace:attempt-a", artifact_id="art-a", content=b"a\n")
    b = output(
        namespace="workspace:attempt-b",
        artifact_id="art-b",
        content=b"b\n",
        acceptance_id="acc-b",
    )
    set_port = port(
        "source",
        cardinality=PortCardinality.SET,
        ordering=PortOrdering.BY_PRODUCER_ORDINAL,
    )
    consumer = binding(T_CONSUMER, inputs=(set_port,))
    snapshot = network(requirements=(requirement(),))
    manifest = InputManifest(
        consumer_task_ref=T_CONSUMER,
        bindings=(
            _resolved(consumer, snapshot, a, "req-a", "source"),
            _resolved(consumer, snapshot, b, "req-b", "source"),
        ),
    )
    inputs = manifest_upstream_inputs(manifest, rules(preserve_source_namespace=True))
    assert sorted(item.path for item in inputs) == [
        "workspace:attempt-a/report.md",
        "workspace:attempt-b/report.md",
    ]


def test_an_unfrozen_manifest_cannot_be_materialised_at_all() -> None:
    from agent_orchestrator.artifacts.input_bindings import SymbolicBinding

    manifest = InputManifest(
        consumer_task_ref=T_CONSUMER,
        pending=(
            SymbolicBinding(
                requirement_id="req-data",
                producer_occurrence=OCC_PRODUCER,
                output_port="report",
                consumer_task_ref=T_CONSUMER,
                input_port="source",
            ),
        ),
    )
    with pytest.raises(ManifestNotFrozen):
        manifest_upstream_inputs(manifest, rules())


# ================================================== a damaged projection stops the work
def _cyclic_network() -> TaskNetworkSnapshot:
    return network(
        requirements=(requirement(),),
        order=(
            order_edge(OCC_PRODUCER, OCC_CONSUMER),
            order_edge(OCC_CONSUMER, OCC_PRODUCER),
        ),
    )


def test_resolving_against_a_cyclic_projection_raises_graph_integrity() -> None:
    produced = output()
    with pytest.raises(GraphIntegrityError):
        resolve(_cyclic_network(), index_of(produced), witness_for(produced))


def test_the_diagnostic_read_may_still_resolve_a_damaged_network() -> None:
    """Decision 11: the execution path raises; an explanation keeps rendering."""

    produced = output()
    result = resolve(
        _cyclic_network(), index_of(produced), witness_for(produced), check_topology=False
    )
    assert result.manifest is not None


def test_the_integrity_error_names_the_cycle_and_reports_no_order() -> None:
    produced = output()
    with pytest.raises(GraphIntegrityError) as caught:
        resolve(_cyclic_network(), index_of(produced), witness_for(produced))
    assert caught.value.cycle
    assert "not a plan" in caught.value.diagnose()


def test_materialising_against_a_cyclic_projection_raises_graph_integrity(tmp_path) -> None:
    produced = output()
    manifest = resolve(
        network(requirements=(requirement(),)), index_of(produced), witness_for(produced)
    ).manifest
    workspace, _ = _workspace(tmp_path)
    with pytest.raises(GraphIntegrityError):
        materialise_v2(workspace, manifest, {}, target_rules=rules(), network=_cyclic_network())


def test_the_input_set_of_a_cyclic_projection_is_refused_too() -> None:
    produced = output()
    manifest = resolve(
        network(requirements=(requirement(),)), index_of(produced), witness_for(produced)
    ).manifest
    with pytest.raises(GraphIntegrityError):
        manifest_upstream_inputs(manifest, rules(), network=_cyclic_network())


def test_the_legacy_sweep_is_not_affected_by_a_damaged_task_network() -> None:
    """``topological`` still returns an order for a corrupted library, as it always did."""

    def task(task_id: str, deps: tuple[str, ...]) -> Task:
        return Task(
            id=task_id,
            mission_id=str(MISSION),
            parent_task_ids=(),
            dependency_ids=deps,
            goal=f"goal {task_id}",
            rationale="cycle",
            success_criteria=("file:a.md",),
            verification_policy=("format_check",),
            allowed_tools=("workspace_read_file",),
            budget=Budget(max_tokens=1_000, max_attempts=1),
            priority=1.0,
            status=TaskStatus.BLOCKED,
            version=1,
        )

    tasks = {"task-1": task("task-1", ("task-2",)), "task-2": task("task-2", ("task-1",))}
    order = topological(list(tasks.values()), tasks)
    assert {item.id for item in order} == {"task-1", "task-2"}


# ============================================================== materialising the files
def _workspace(tmp_path) -> tuple[object, ArtifactStore]:
    store = ArtifactStore(Path(tmp_path) / "cas")
    manager = WorkspaceManager(Path(tmp_path) / "workspaces", artifact_store=store)
    return manager.create("attempt-1", seed={}), store


def _stored(store: ArtifactStore, content: bytes, *, artifact_id: str = "art-1") -> Artifact:
    content_hash = store.put_bytes(content)
    return Artifact(
        id=artifact_id,
        mission_id=str(MISSION),
        task_id=str(T_PRODUCER),
        attempt_id="attempt-0",
        type="file",
        path="report.md",
        version=1,
        content_hash=content_hash,
        size_bytes=len(content),
        produced_by="worker",
        storage_uri=str(store.path_for(content_hash)),
    )


def test_materialise_v2_writes_exactly_the_manifest_entry(tmp_path) -> None:
    workspace, store = _workspace(tmp_path)
    produced = output(content=b"# report\n")
    manifest = resolve(
        network(requirements=(requirement(),)), index_of(produced), witness_for(produced)
    ).manifest
    written = materialise_v2(
        workspace,
        manifest,
        {"art-1": _stored(store, b"# report\n")},
        target_rules=rules(),
    )
    assert written == ["report.md"]
    assert workspace.list_files() == ["report.md"]
    assert workspace.read_bytes("report.md") == b"# report\n"


def test_materialise_v2_places_nothing_else_in_the_workspace(tmp_path) -> None:
    workspace, store = _workspace(tmp_path)
    produced = output(content=b"# report\n")
    manifest = resolve(
        network(requirements=(requirement(),)), index_of(produced), witness_for(produced)
    ).manifest
    materialise_v2(
        workspace,
        manifest,
        {
            "art-1": _stored(store, b"# report\n"),
            "art-9": _stored(store, b"# unrelated\n", artifact_id="art-9"),
        },
        target_rules=rules(),
    )
    assert workspace.list_files() == ["report.md"]


def test_materialise_v2_refuses_an_artifact_whose_bytes_changed(tmp_path) -> None:
    workspace, store = _workspace(tmp_path)
    produced = output(content=b"# report\n")
    manifest = resolve(
        network(requirements=(requirement(),)), index_of(produced), witness_for(produced)
    ).manifest
    with pytest.raises(ArtifactConflict):
        materialise_v2(
            workspace,
            manifest,
            {"art-1": _stored(store, b"# tampered\n")},
            target_rules=rules(),
        )


def test_materialise_v2_refuses_a_missing_artifact(tmp_path) -> None:
    workspace, _ = _workspace(tmp_path)
    produced = output()
    manifest = resolve(
        network(requirements=(requirement(),)), index_of(produced), witness_for(produced)
    ).manifest
    with pytest.raises(ArtifactConflict):
        materialise_v2(workspace, manifest, {}, target_rules=rules())


def test_the_workspace_entry_point_refuses_bytes_it_was_not_given(tmp_path) -> None:
    workspace, _ = _workspace(tmp_path)
    produced = output()
    manifest = resolve(
        network(requirements=(requirement(),)), index_of(produced), witness_for(produced)
    ).manifest
    from agent_orchestrator.artifacts.input_bindings import materialise_plan

    plan = materialise_plan(manifest, rules())
    with pytest.raises(WorkspaceError):
        workspace.materialise_manifest(plan.entries, {})


def test_the_workspace_entry_point_refuses_a_read_only_copy(tmp_path) -> None:
    store = ArtifactStore(Path(tmp_path) / "cas")
    manager = WorkspaceManager(Path(tmp_path) / "workspaces", artifact_store=store)
    manager.create("attempt-1", seed={})
    read_only = manager.get("attempt-1", writable=False)
    with pytest.raises(WorkspaceError):
        read_only.materialise_manifest((), {})


# ================================================== the legacy functions did not move
#: sha256 of the source text of the three legacy functions, in this order:
#: ``topological``, ``merge_accepted``, ``collect_upstream_inputs``.  §18.2 asks for
#: them byte for byte; a diff of one character changes this digest.
LEGACY_SOURCE_DIGEST = "fe215c0d89c025dd70e79542b918adef67fafd62ba372e461f810cbfc438fea4"


def _legacy_source() -> str:
    return "".join(
        inspect.getsource(function)
        for function in (topological, merge_accepted, collect_upstream_inputs)
    )


def test_the_three_legacy_functions_are_byte_for_byte_unchanged() -> None:
    assert hashlib.sha256(_legacy_source().encode()).hexdigest() == LEGACY_SOURCE_DIGEST


def test_the_new_entry_points_are_additions_and_not_rewrites() -> None:
    exported = set(versioning.__all__)
    assert {"merge_accepted", "collect_upstream_inputs", "topological"} <= exported
    assert {"resolve_input_manifest", "manifest_upstream_inputs", "materialise_v2"} <= exported


def test_the_legacy_merge_still_overrides_along_a_dependency_chain() -> None:
    def task(task_id: str, deps: tuple[str, ...], artifact_ids: tuple[str, ...]) -> Task:
        return Task(
            id=task_id,
            mission_id=str(MISSION),
            parent_task_ids=(),
            dependency_ids=deps,
            goal=f"goal {task_id}",
            rationale="chain",
            success_criteria=("file:a.md",),
            verification_policy=("format_check",),
            allowed_tools=("workspace_read_file",),
            budget=Budget(max_tokens=1_000, max_attempts=1),
            priority=1.0,
            status=TaskStatus.COMPLETED,
            version=1,
            accepted_artifacts=artifact_ids,
        )

    def artifact(artifact_id: str, task_id: str, content: bytes) -> Artifact:
        return Artifact(
            id=artifact_id,
            mission_id=str(MISSION),
            task_id=task_id,
            attempt_id=f"{task_id}:attempt-1",
            type="file",
            path="a.md",
            version=1,
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            produced_by="worker",
        )

    tasks = {
        "task-1": task("task-1", (), ("art-1",)),
        "task-2": task("task-2", ("task-1",), ("art-2",)),
    }
    artifacts = {
        "task-1": [artifact("art-1", "task-1", b"first\n")],
        "task-2": [artifact("art-2", "task-2", b"second\n")],
    }
    merged = merge_accepted(list(tasks.values()), artifacts, tasks_by_id=tasks)
    assert [item.task_id for item in merged] == ["task-2"]  # the downstream wins


def test_the_legacy_merge_still_refuses_two_independent_branches() -> None:
    def task(task_id: str, artifact_ids: tuple[str, ...]) -> Task:
        return Task(
            id=task_id,
            mission_id=str(MISSION),
            parent_task_ids=(),
            dependency_ids=(),
            goal=f"goal {task_id}",
            rationale="branches",
            success_criteria=("file:a.md",),
            verification_policy=("format_check",),
            allowed_tools=("workspace_read_file",),
            budget=Budget(max_tokens=1_000, max_attempts=1),
            priority=1.0,
            status=TaskStatus.COMPLETED,
            version=1,
            accepted_artifacts=artifact_ids,
        )

    def artifact(artifact_id: str, task_id: str, content: bytes) -> Artifact:
        return Artifact(
            id=artifact_id,
            mission_id=str(MISSION),
            task_id=task_id,
            attempt_id=f"{task_id}:attempt-1",
            type="file",
            path="a.md",
            version=1,
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            produced_by="worker",
        )

    tasks = {"task-1": task("task-1", ("art-1",)), "task-2": task("task-2", ("art-2",))}
    artifacts = {
        "task-1": [artifact("art-1", "task-1", b"one\n")],
        "task-2": [artifact("art-2", "task-2", b"two\n")],
    }
    with pytest.raises(ArtifactConflict):
        merge_accepted(list(tasks.values()), artifacts, tasks_by_id=tasks)


def test_the_diagnostic_readers_still_call_the_legacy_functions() -> None:
    """Decision 11: ``traces`` and ``evaluation`` are not moved onto the new path."""

    from agent_orchestrator.observability import evaluation, traces

    assert "merge_accepted" in inspect.getsource(traces)
    assert "merge_accepted" in inspect.getsource(evaluation)
    assert "materialise_v2" not in inspect.getsource(traces)
    assert "materialise_v2" not in inspect.getsource(evaluation)
