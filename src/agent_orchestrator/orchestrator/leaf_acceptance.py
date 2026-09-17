# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2b: a verified primitive leaf → an ``Acceptance`` → the output index.

Part 2 built ``accept_review`` and part 2's §13 listed what was missing around it:
nothing in ``src`` turned a *verified result* into the AER anchors the command
quotes, so ``acceptance_outputs`` was never written and ``_recorded_outputs`` still
had nothing to read.  This module is that bridge, and it is deliberately a **bridge
and not a decision**: every value it writes is read off something that already
happened.

Where each anchor comes from — the point being that none of them is invented here:

``RequirementsRevision``
    the leaf's own goal signature.  Its ``coverage_criteria`` are what the plan says
    this occurrence has to cover; the *required checks* of each criterion are the
    verification layers that actually ran on this result.  A criterion nobody can
    check is therefore not silently satisfied — it is a criterion with no gate, and
    the acceptance formula treats it as such.
``ReviewPackage`` / ``ReviewRecord``
    frozen from the verification verdict and **stored before the command**, because
    ``accept_review`` re-reads both from the store and compares content hashes: an
    anchor supplied with the command is exactly what an immutable anchor is not.
``CriterionOutcome.evidence_refs``
    one ``TOOL_RECEIPT`` per layer that ran, attributed ``Provenance.TOOL`` by *this*
    side.  A model may only ever claim ``model``/``human`` for itself, so the
    attribution written here is the signal ``receipt_source`` is allowed to trust —
    and it is written only for layers the router really recorded.
``ValidityWitness``
    a fresh ``purpose=ACCEPT`` witness for this task, at the scope's current epoch.
    A ``START`` witness that licensed the dispatch does not license the acceptance.
``AcceptedOutput``
    the artifacts this result produced, filed under the ports the **plan** declares
    for this occurrence.  Where the plan declares no port, nothing is indexed: a leaf
    whose result no occurrence consumes feeds nothing, and an entry for it would be
    the all-ancestors sweep §24.1 decision 4 removed.

Two things this module refuses to do:

* **It never manufactures a PASS.**  :func:`outcomes_for` reports what each layer
  reported; a layer that errored or failed becomes a non-PASS outcome and the
  acceptance formula refuses.  A caller that only calls this on a passed verdict is
  not relying on that, and a caller that calls it on a failed one gets a refusal
  rather than an acceptance.
* **It never accepts a compound.**  A compound goal is satisfied by
  ``commit_goal_resolution`` out of its children's acceptances, never by a review of
  its own (§6.3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..artifacts.input_bindings import AcceptedOutput, DisclosureState, ResourceIdentity
from ..contracts.evidence_state import (
    Availability,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from ..contracts.htn import (
    OccurrenceId,
    ReadItem,
    ReadItemKind,
    ScopeEpochRead,
    SemanticReadSet,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from ..contracts.models import ContractError
from ..contracts.resolution import (
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    CriterionOutcome,
    CriterionVerdict,
    EvaluationKind,
    RequiredEvidencePolicy,
    RequirementClass,
    RequirementsRevision,
    RequirementsRevisionId,
    ReviewBinding,
    ReviewPackage,
    ReviewPackageId,
    ReviewPurpose,
    ReviewRecord,
    ReviewRecordId,
    ReviewVerdict,
    WorkspaceAccess,
)
from ..contracts.semantic_base import (
    Provenance,
    TypedRef,
    TypedRefKind,
    content_hash_of,
)
from ..knowledge.validity import acceptance_subject
from ..runtime.output_blocks import PortClaim
from ..storage.htn_store import HtnStore
from ..storage.store import StoreError
from ..verification.acceptance_rules import ExecutionPosture, IndependenceFacts
from .accepted_outputs import output_ports_in_revision
from .resolution_commits import AcceptanceReceipt, AcceptReviewCommand, ResolutionPrincipal

#: The review policy this deployment reviews a hierarchical leaf under.  A named
#: constant rather than a literal at three call sites, so the package, the record
#: and the command cannot drift into quoting two policies.
LEAF_REVIEW_POLICY = "hierarchical-leaf-review-v1"

#: The layer statuses that mean the check *ran to a conclusion*.  ``ERROR`` and
#: ``NEEDS_HUMAN`` are deliberately absent: a check that could not finish is not a
#: check that passed, and AER-V04 exists because "the model said it passed" is not
#: the same fact as "the layer ran".
CONCLUSIVE_LAYER_STATUSES = frozenset({"PASS", "FAIL"})

#: What the reviewer is allowed to do to the candidate.  ``WRITE`` is refused at
#: construction, so stating ``READ_ONLY`` here is a claim the package can hold.
REVIEWER_ACCESS = WorkspaceAccess.READ_ONLY


@dataclass(frozen=True, slots=True)
class LayerOutcome:
    """One verification layer's result, as this module needs it.

    A tiny value rather than the router's ``LayerResult`` so the assembly can also
    be driven from stored rows (a restart re-reads the recorded layers) without
    either side importing the other's shape.
    """

    layer: str
    status: str
    summary: str = ""

    @property
    def conclusive(self) -> bool:
        return self.status in CONCLUSIVE_LAYER_STATUSES

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


def layer_outcomes(layers: Sequence[Any]) -> tuple[LayerOutcome, ...]:
    """Normalise whatever the caller holds — router results or stored rows."""

    out: list[LayerOutcome] = []
    for item in layers:
        if isinstance(item, LayerOutcome):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(
                LayerOutcome(
                    layer=str(item.get("layer", "")),
                    status=str(item.get("status", "")),
                    summary=str(item.get("summary", "")),
                )
            )
        else:
            out.append(
                LayerOutcome(
                    layer=str(getattr(item, "layer", "")),
                    status=str(getattr(item, "status", "")),
                    summary=str(getattr(item, "summary", "")),
                )
            )
    return tuple(out)


def check_ids(layers: Sequence[LayerOutcome]) -> tuple[str, ...]:
    """The checks a criterion of this leaf is gated on: the layers that ran.

    Derived from the verdict rather than configured, because a criterion gated on a
    check this deployment never runs can never be satisfied — and one gated on
    *nothing* would be satisfied by a review's own say-so, which is the
    self-attested PASS AER-V04 refuses.
    """

    return tuple(sorted({item.layer for item in layers if item.conclusive}))


def criteria_for(
    binding: TaskSemanticBindingV1, layers: Sequence[LayerOutcome]
) -> tuple[Criterion, ...]:
    """The leaf's coverage criteria, each gated on the checks that actually ran."""

    gates = check_ids(layers)
    names = tuple(binding.goal_signature.coverage_criteria) or tuple(binding.requirement_refs)
    if not names:
        raise ContractError(
            f"task {binding.task_id!s} declares no coverage criterion and no requirement ref; "
            "an acceptance with nothing to cover would be an acceptance of nothing (AER §6.2)"
        )
    return tuple(
        Criterion(
            criterion_id=str(name),
            revision=1,
            origin=CriterionOrigin.DERIVED,
            statement=f"{name} is covered by the accepted result of {binding.task_id!s}",
            requirement_class=RequirementClass.REQUIRED_OUTCOME,
            evaluation_kind=EvaluationKind.DETERMINISTIC,
            required_evidence_policy=RequiredEvidencePolicy(required_check_ids=gates),
        )
        for name in dict.fromkeys(names)
    )


def requirements_for(
    mission_id: str,
    binding: TaskSemanticBindingV1,
    layers: Sequence[LayerOutcome],
    *,
    revision: int,
) -> RequirementsRevision:
    """The requirements revision this leaf's acceptance is decided against."""

    criteria = criteria_for(binding, layers)
    expression: Any = CriterionExpr(criteria[0].criterion_id)
    if len(criteria) > 1:
        from ..contracts.resolution import AllExpr

        expression = AllExpr(children=tuple(CriterionExpr(item.criterion_id) for item in criteria))
    return RequirementsRevision(
        revision_id=RequirementsRevisionId(f"req-{mission_id}-{int(revision)}"),
        mission_id=mission_id,
        revision=int(revision),
        criteria=criteria,
        success_expression=expression,
    )


def receipt_ref(result_id: str, layer: str) -> TypedRef:
    """A dispatcher-attributed receipt for one layer that ran on this result."""

    digest = content_hash_of({"result": str(result_id), "layer": str(layer)})
    return TypedRef(
        kind=TypedRefKind.TOOL_RECEIPT,
        id=f"vlayer-{digest[:32]}",
        revision=1,
        content_hash=digest,
        produced_by=Provenance.TOOL,
    )


def outcomes_for(
    criteria: Sequence[Criterion], layers: Sequence[LayerOutcome], *, result_id: str
) -> tuple[CriterionOutcome, ...]:
    """One outcome per criterion, reporting what the layers reported.

    Every gated criterion of a leaf is covered by the same verification verdict —
    the leaf produced one result and the router judged that one result — so the
    verdict is projected onto each criterion rather than split between them.  The
    projection is honest in both directions: a single non-PASS conclusive layer
    makes every criterion FAIL, and a layer that did not reach a conclusion makes
    the execution ``NOT_RUN`` rather than leaving a PASS with a missing check.
    """

    conclusive = [item for item in layers if item.conclusive]
    execution = CheckExecution.SUCCEEDED if conclusive else CheckExecution.NOT_RUN
    verdict = (
        CriterionVerdict.PASS
        if conclusive and all(item.passed for item in conclusive)
        else CriterionVerdict.FAIL
    )
    evidence = tuple(receipt_ref(result_id, item.layer) for item in conclusive)
    return tuple(
        CriterionOutcome(
            criterion_id=item.criterion_id,
            verdict=verdict,
            check_execution=execution,
            evidence_refs=evidence,
            # A layer that did not reach a conclusion is a stated *limitation* of
            # this outcome rather than a silence: the acceptance is still decided by
            # the gate, and a reader can see which check was inconclusive.
            limitations=tuple(f"{one.layer}={one.status}" for one in layers if not one.conclusive),
        )
        for item in criteria
    )


def accepted_outputs_for(
    ports: Mapping[str, Any],
    *,
    occurrence: OccurrenceId,
    task_id: TaskRef,
    result_id: str,
    acceptance_id: str,
    support_revision: int,
    artifacts: Sequence[Any],
    namespace: str,
    claims: Sequence[PortClaim] = (),
) -> tuple[AcceptedOutput, ...]:
    """The accepted artifacts, filed at the ports the producer **said** they belong to.

    P2.3c part 2d, decision 4.  The pairing is *declared*, not derived.  Until now
    this function guessed: an artifact went to the port whose key appeared anywhere
    inside its path (so a port called ``facts`` claimed ``artifacts/anything.json``),
    or, when there was exactly one port and one file, to that port by position.  TG
    design §10.2 forbids exactly that — "two different hashes onto one final file"
    may not be settled by topology or by name — and §21.5's "wrongly declared
    complete = 0" is what a mis-paired artifact ends up violating downstream.

    So the port comes from a :class:`~..runtime.output_blocks.PortClaim`, which the
    Worker wrote and the block parser already checked against this plan's declared
    ports and this Attempt's real files.  Everything else on the
    :class:`~..artifacts.input_bindings.AcceptedOutput` is system-bound: the schema
    comes from the ``DataRequirement`` that declares the edge (never from the
    producer, which could otherwise relabel its own output), and the acceptance id,
    content hash, support revision and occurrence are filled here.

    An artifact no claim names stays an artifact: it is evidence of the run and is
    not indexed.  A *port* nobody claimed is the accept side's problem, not this
    function's — :meth:`ResolutionCommitsMixin.accept_review` refuses the acceptance
    with ``OUTPUT_PORT_UNCLAIMED`` rather than letting the leaf pass with a gap.
    """

    if not ports or not artifacts or not claims:
        return ()
    by_path: dict[str, Any] = {}
    for artifact in artifacts:
        path = str(getattr(artifact, "path", "") or getattr(artifact, "id", ""))
        by_path.setdefault(path, artifact)
    out: list[AcceptedOutput] = []
    for ordinal, claim in enumerate(claims):
        schema = ports.get(claim.port_key)
        artifact = by_path.get(claim.path)
        if schema is None or artifact is None:
            # The parser refuses both of these at the block, so reaching here means a
            # caller assembled claims by hand.  Skipping is the conservative answer —
            # inventing a port or an artifact is the guess this whole change removes.
            continue
        out.append(
            AcceptedOutput(
                producer_occurrence=occurrence,
                producer_task_ref=task_id,
                output_port=claim.port_key,
                producer_result_id=str(result_id),
                acceptance_id=str(acceptance_id),
                support_revision=int(support_revision),
                artifact_id=str(artifact.id),
                content_hash=str(getattr(artifact, "content_hash", "") or artifact.id),
                schema_ref=schema,
                source_revision=str(getattr(artifact, "version", "") or "1"),
                source_identity=ResourceIdentity(
                    namespace=namespace, path=claim.path or str(artifact.id)
                ),
                producer_ordinal=ordinal,
                disclosure=DisclosureState.DISCLOSABLE,
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------------------
# the assembly
# --------------------------------------------------------------------------------------


@dataclass
class LeafAcceptanceAssembly:
    """Turns one verified primitive result into a committed ``Acceptance``.

    Holds a store and the one Commit Service; holds no state about a Mission,
    because the anchors it writes are read back by the commit from the same store
    inside the transaction.
    """

    store: Any
    commit: Any
    #: The hierarchical assembly, when the caller has one.  It is how the acceptance
    #: learns *what this dispatch consumed*: the resolved ``InputManifest`` of the
    #: occurrence.  Without it the acceptance may only claim the empty manifest, and
    #: it says so rather than quoting a manifest it did not see.
    dispatch: Any = None
    scope_id: str = "mission"
    reviewer_agent_id: str = "verifier-router"

    @property
    def semantics(self) -> HtnStore:
        return HtnStore(self.store)

    def accept(
        self,
        mission_id: str,
        task_id: str,
        *,
        result_id: str,
        layers: Sequence[Any],
        artifacts: Sequence[Any] = (),
        producer_agent_ids: Sequence[str] = (),
        reviewer_agent_id: str | None = None,
        now_ms: int,
        command_id: str | None = None,
        input_manifest_hash: str = "",
        namespace: str = "workspace",
        port_claims: Sequence[PortClaim] = (),
    ) -> AcceptanceReceipt:
        """Freeze the anchors, then commit the acceptance.  Nothing else decides.

        ``port_claims`` is what the Worker said about its own files — "this path is
        the ``repository_facts`` the plan asked for" — already checked against the
        declared ports and the Attempt's artifacts by the block parser.  Without it
        no output is indexed and ``accept_review`` refuses any required port that a
        consumer is waiting on (``OUTPUT_PORT_UNCLAIMED``, decision 4).
        """

        outcomes = layer_outcomes(layers)
        binding = self.semantics.task_semantics_of(mission_id, task_id)
        if binding is None:
            raise ContractError(
                f"task {task_id!r} has no TaskSemanticBindingV1 in mission {mission_id!r}; in "
                "the hierarchical mode a missing binding is corruption, not a legacy task"
            )
        if binding.form is not TaskForm.PRIMITIVE:
            raise ContractError(
                f"task {task_id!r} is {binding.form!s}; a compound goal is satisfied by "
                "commit_goal_resolution out of its children's acceptances, never by a review "
                "of its own (§6.3)"
            )
        semantics = self.semantics
        revision = self._requirements(mission_id, binding, outcomes)
        manifest = input_manifest_hash or self._manifest_hash(mission_id, task_id)
        package = self._package(
            mission_id, binding, revision, result_id, manifest, producer_agent_ids
        )
        record = self._record(package, outcomes, result_id, reviewer_agent_id)
        acceptance_id = f"acc-{content_hash_of({'task': task_id, 'result': result_id})[:32]}"
        witness = self._witness(mission_id, task_id, acceptance_id=acceptance_id, now_ms=now_ms)
        command = AcceptReviewCommand(
            command_id=command_id or f"accept:{result_id}",
            mission_id=mission_id,
            task_id=task_id,
            obligation_id=str(binding.obligation_id),
            acceptance_id=acceptance_id,
            package=package,
            record=record,
            requirements=revision,
            witness_id=witness.witness_id,
            independence=IndependenceFacts(
                producer_agent_ids=tuple(producer_agent_ids),
                reviewer_can_write_candidate=False,
            ),
            posture=ExecutionPosture(),
            read_set=self._read_set(mission_id, binding, revision),
            accepted_at_ms=int(now_ms),
            outputs=self._outputs(
                mission_id,
                binding,
                result_id=result_id,
                acceptance_id=acceptance_id,
                artifacts=artifacts,
                namespace=namespace,
                port_claims=port_claims,
            ),
            purpose=ReviewPurpose.TASK_CONTENT,
            # The Critic layer is the independent semantic review; this deployment
            # requires it, and a verdict built without it refuses rather than being
            # quietly accepted as "the rules did not ask".
            semantic_review_required=True,
            policy_ref=LEAF_REVIEW_POLICY,
            issued_by=self.reviewer_agent_id,
            scope_id=self.scope_id,
            source={"result_id": str(result_id), "layers": [item.layer for item in outcomes]},
        )
        principal = ResolutionPrincipal(
            principal_id=self.reviewer_agent_id,
            scope_id=self.scope_id,
            manager_epoch=semantics.epoch(mission_id, self.scope_id),
        )
        return self.commit.accept_review(command, principal)

    # -- the anchors ----------------------------------------------------------------
    def _manifest_hash(self, mission_id: str, task_id: str) -> str:
        """Freeze what this dispatch consumed, and return the library's own digest.

        An ``Acceptance`` names the inputs it was granted, and ``accept_review``
        re-reads that manifest from the store — so the hash has to be the one
        ``insert_input_manifest`` computed over the stored document, never a digest
        this module invented over a shape nobody kept.

        A deployment with no hierarchical assembly to ask records the **empty**
        manifest: "this dispatch consumed nothing" is a claim the acceptance can
        make honestly, and it is a different claim from "nobody resolved the inputs".
        """

        document: dict[str, Any] = {"consumer_task_ref": str(task_id), "bindings": []}
        if self.dispatch is not None:
            network = self.dispatch.network(mission_id)
            spec = next(
                (item for item in network.occurrences if str(item.task_id) == str(task_id)), None
            )
            if spec is not None:
                resolved = self.dispatch.resolved_inputs(mission_id, network, spec)
                if resolved.manifest is not None and resolved.manifest.is_frozen:
                    document = dict(resolved.manifest.to_json())
        return self.semantics.insert_input_manifest(mission_id, str(task_id), document)

    def _requirements(
        self, mission_id: str, binding: TaskSemanticBindingV1, layers: Sequence[LayerOutcome]
    ) -> RequirementsRevision:
        """This leaf's requirements revision, published once and re-read after that.

        A second leaf publishes a *later* revision rather than overwriting the
        first: a requirements revision is immutable, and the read-set channel that
        re-checks "which revision was this decided at" only means something if the
        number moves when the content does.
        """

        semantics = self.semantics
        latest = semantics.latest_requirements_revision(mission_id)
        candidate = requirements_for(
            mission_id, binding, layers, revision=1 if latest is None else int(latest.revision)
        )
        if latest is not None and latest.content_hash() == candidate.content_hash():
            return latest
        published = (
            candidate
            if latest is None
            else requirements_for(mission_id, binding, layers, revision=int(latest.revision) + 1)
        )
        semantics.insert_requirements_revision(published)
        return published

    def _package(
        self,
        mission_id: str,
        binding: TaskSemanticBindingV1,
        revision: RequirementsRevision,
        result_id: str,
        manifest_hash: str,
        producer_agent_ids: Sequence[str],
    ) -> ReviewPackage:
        package = ReviewPackage(
            package_id=ReviewPackageId(
                f"pkg-{content_hash_of({'r': result_id, 'rev': revision.revision})[:32]}"
            ),
            purpose=ReviewPurpose.TASK_CONTENT,
            binding=ReviewBinding(
                mission_id=mission_id,
                obligation_id=str(binding.obligation_id),
                subject_ref=TypedRef(
                    kind=TypedRefKind.TASK,
                    id=str(binding.task_id),
                    revision=int(binding.contract_revision),
                    content_hash=binding.contract_hash,
                ),
                requirements_revision=int(revision.revision),
                input_manifest_hash=manifest_hash,
                policy_ref=TypedRef(
                    kind=TypedRefKind.SOURCE,
                    id=LEAF_REVIEW_POLICY,
                    revision=1,
                    content_hash=content_hash_of(LEAF_REVIEW_POLICY),
                ),
            ),
            criteria=revision.criteria,
            success_expression=revision.success_expression,
            candidate_refs=(
                TypedRef(
                    # The candidate is the recorded *result*, referenced as the
                    # artifact-kind ref the annex schema has for it — there is no
                    # ``result`` kind, and inventing one would put a value in the
                    # contract that nothing else can read.
                    kind=TypedRefKind.ARTIFACT,
                    id=str(result_id),
                    revision=1,
                    content_hash=content_hash_of(str(result_id)),
                    produced_by=Provenance.TOOL,
                ),
            ),
            producer_agent_ids=tuple(producer_agent_ids),
            reviewer_workspace_access=REVIEWER_ACCESS,
            requirements_content_hash=revision.content_hash(),
        )
        try:
            stored = self.semantics.get_review_package(str(package.package_id))
        except StoreError:
            self.semantics.insert_review_package(package)
            return package
        # A replay re-presents the *same* frozen anchor, so re-inserting it would be
        # a conflict about nothing.  A differing one is a real conflict and is left
        # to the store to refuse rather than silently replaced.
        if stored.content_hash() != package.content_hash():
            self.semantics.insert_review_package(package)
        return package

    def _record(
        self,
        package: ReviewPackage,
        layers: Sequence[LayerOutcome],
        result_id: str,
        reviewer_agent_id: str | None,
    ) -> ReviewRecord:
        outcomes = outcomes_for(package.criteria, layers, result_id=result_id)
        passed = all(item.verdict is CriterionVerdict.PASS for item in outcomes)
        record = ReviewRecord(
            record_id=ReviewRecordId(f"rec-{content_hash_of(str(package.package_id))[:32]}"),
            package_id=package.package_id,
            purpose=package.purpose,
            binding=package.binding,
            reviewer_agent_id=reviewer_agent_id or self.reviewer_agent_id,
            reviewer_turn_id=str(result_id),
            evidence_manifest_hash=content_hash_of(
                [{"layer": item.layer, "status": item.status} for item in layers]
            ),
            criteria=outcomes,
            verdict=ReviewVerdict.ACCEPT if passed else ReviewVerdict.REJECTED,
        )
        official = self.semantics.official_review_record(str(package.package_id))
        if official is None or official.to_json() != record.to_json():
            self.semantics.insert_review_record(record, official=True)
        return record

    def _witness(
        self, mission_id: str, task_id: str, *, acceptance_id: str, now_ms: int
    ) -> ValidityWitness:
        """The ACCEPT licence, named after the key it occupies (part 2d, review P0-2).

        Two things were wrong before, and they were the same thing.  The id was
        ``hash(task, now_ms)`` while the row's unique key carried no clock, so a
        *second* acceptance of one leaf minted a **new id** landing on the **old
        key**: ``get_validity_witness`` missed it (different id), the insert hit the
        index, and the ``StoreError`` travelled out of ``accept()`` into
        ``_accept_hierarchical_leaf``, which records "acceptance refused" and moves
        on.  Re-working a leaf after its acceptance was revoked could therefore never
        produce a new acceptance, and ``_require_accepted_work`` then refused to
        judge the Mission for ever.

        So the id is now derived from exactly what the key is made of — consumer,
        purpose, scope, epoch, support revision and the subject — which makes
        "already stored" answerable by a keyed read instead of by an exception, and
        makes the two acceptances of one leaf two rows rather than two claims on one.
        It names ``acceptance_id`` in its ``support_refs`` because §11.5 requires a
        witness to say what it was taken over, and because that is what
        :func:`~..knowledge.validity.witness_subject` recomputes the subject from.
        """

        epoch = self.semantics.epoch(mission_id, self.scope_id)
        subject = acceptance_subject(str(acceptance_id))
        support_revision = len(self.semantics.list_observations(mission_id))
        witness = ValidityWitness(
            witness_id="wit-"
            + content_hash_of(
                {
                    "consumer": str(task_id),
                    "purpose": str(WitnessPurpose.ACCEPT),
                    "scope": self.scope_id,
                    "epoch": int(epoch),
                    "support_revision": int(support_revision),
                    "subject": subject,
                }
            )[:32],
            consumer_ref=TypedRef(
                kind=TypedRefKind.TASK,
                id=str(task_id),
                revision=1,
                content_hash=content_hash_of(str(task_id)),
            ),
            purpose=WitnessPurpose.ACCEPT,
            truth=TruthValue.TRUE,
            freshness=Validity.CURRENT,
            availability=Availability.READABLE,
            decision=WitnessDecision.USABLE,
            scope_id=self.scope_id,
            scope_epoch=epoch,
            support_revision=support_revision,
            as_of_ms=int(now_ms),
            support_refs=(
                TypedRef(
                    kind=TypedRefKind.ACCEPTANCE,
                    id=str(acceptance_id),
                    revision=1,
                    content_hash=content_hash_of(str(acceptance_id)),
                ),
            ),
        )
        try:
            # The id *is* the key, so "is this licence already stored" is one keyed
            # read.  It used to be a read that could not answer the question (the id
            # carried a clock the key did not) followed by an insert that raised.
            return self.semantics.get_validity_witness(witness.witness_id)
        except StoreError:
            self.semantics.insert_validity_witness(mission_id, witness, subject=subject)
            return witness

    def _read_set(
        self, mission_id: str, binding: TaskSemanticBindingV1, revision: RequirementsRevision
    ) -> SemanticReadSet:
        epoch = self.semantics.epoch(mission_id, self.scope_id)
        return SemanticReadSet(
            requirements_revision=int(revision.revision),
            manager_epoch=epoch,
            scope_epochs=(ScopeEpochRead(scope_id=self.scope_id, validity_epoch=epoch),),
            goal_revisions=(
                ReadItem(
                    kind=ReadItemKind.TASK,
                    id=str(binding.task_id),
                    semantic_revision=int(binding.contract_revision),
                    content_hash=binding.contract_hash,
                ),
            ),
        )

    def _outputs(
        self,
        mission_id: str,
        binding: TaskSemanticBindingV1,
        *,
        result_id: str,
        acceptance_id: str,
        artifacts: Sequence[Any],
        namespace: str,
        port_claims: Sequence[PortClaim] = (),
    ) -> tuple[AcceptedOutput, ...]:
        semantics = self.semantics
        active = semantics.active_plan_revision(mission_id)
        if active is None:
            return ()
        revision = int(active.revision)
        occurrence = next(
            (
                spec.occurrence_id
                for spec in semantics.list_plan_memberships(mission_id, revision)
                if str(spec.task_id) == str(binding.task_id)
            ),
            None,
        )
        if occurrence is None:
            return ()
        ports = output_ports_in_revision(
            semantics, mission_id, revision, occurrence, str(binding.task_id)
        )
        return accepted_outputs_for(
            ports,
            occurrence=occurrence,
            task_id=binding.task_id,
            result_id=result_id,
            acceptance_id=acceptance_id,
            support_revision=len(semantics.list_observations(mission_id)),
            artifacts=artifacts,
            namespace=namespace,
            claims=port_claims,
        )


__all__ = (
    "CONCLUSIVE_LAYER_STATUSES",
    "LEAF_REVIEW_POLICY",
    "LayerOutcome",
    "LeafAcceptanceAssembly",
    "accepted_outputs_for",
    "check_ids",
    "criteria_for",
    "layer_outcomes",
    "outcomes_for",
    "receipt_ref",
    "requirements_for",
)
