# SPDX-License-Identifier: Apache-2.0
"""The other five review purposes on the one Assurance round transport (BW03).

Each builder starts from its original production entry (method admission,
composition assembly, T0 proposal draft, outcome readback, root cut) and freezes
that entry's own package and subject into an ``AssuranceReviewBinding``. The
shared preparation below is the same bounded read the TASK_CONTENT builder
performs: exact metadata, complete query sets, current authority, pins and a
final-lock validator that runs inside ``ensure_review_invocation``'s UoW.

Nothing here parses a model reply, writes an official record or licenses a
terminal writer; the collector, the REVIEW consumer and the purpose's original
consumer remain the only paths for those.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..assurance.certificates import UseIdentity
from ..assurance.checks import CriterionPolicy
from ..assurance.codec import AssuranceError, decode, fingerprint, text
from ..assurance.evidence import build_catalogue
from ..assurance.policy_domain import policy_domain_hash
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.review_input import REVIEW_INSTRUCTIONS, render_review_input
from ..assurance.reviews import REVIEW_CODEC_VERSION, AssuranceReviewBinding
from ..contracts.resolution import (
    AllExpr,
    CriterionExpr,
    RequirementClass,
    RequirementsRevision,
    ReviewBinding,
    ReviewPackage,
    ReviewPackageId,
    ReviewPurpose,
    WorkspaceAccess,
)
from ..contracts.semantic_base import Provenance, TypedRef, TypedRefKind, content_hash_of
from ..storage.assurance_blobs import read_pinned_blob
from ..storage.assurance_reads import (
    AssuranceReader,
    read_complete_evidence_snapshot,
    read_epochs_locked,
    require_epochs_locked,
)
from ..storage.assurance_store import AssuranceStore
from ..storage.htn_store import HtnStore
from .assurance_check_use import CurrentAuthority, _merge_reads, _permission, _require_same_permission
from .assurance_review_pins import ensure_review_blob_pins, release_failed_preparation
from .assurance_review_transport import assurance_formula, read_review_invocation_locked

METHOD_PLAN_REVIEW_POLICY = "assurance-method-plan-review-v1"
OUTPUT_MANIFEST_KIND = "assurance-output-manifest-v1"


def output_manifest_hash(purpose: str, body: Mapping[str, Any]) -> str:
    """The durable outputs a content review judges, hashed as one canonical document."""
    return fingerprint({"kind": OUTPUT_MANIFEST_KIND, "purpose": text(purpose), **dict(body)})


def purpose_review_key(purpose: str, mission_id: str, package_id: str) -> str:
    """One round key per (purpose, Mission, frozen package); replay-safe by design."""
    return f"assurance-{purpose.lower().replace('_', '-')}:" + fingerprint(
        {"mission_id": mission_id, "package": str(package_id)}
    )


def assurance_review_runtime(commit: Any) -> Any:
    """The installed review runtime; a builder never invents a consumer."""
    handoff = getattr(commit, "_assurance_review_handoff", None)
    runtime = None if handoff is None else getattr(handoff, "runtime", None)
    if runtime is None:
        raise AssuranceError("ASSURANCE_REVIEW_RUNTIME_UNBOUND")
    return runtime


@dataclass(frozen=True, slots=True)
class PurposeSubject:
    """What one purpose builder froze from its original entry."""

    purpose: str
    target: AssuranceRef
    owner_task: Pin
    occurrence_id: str | None
    scope_ref: AssuranceRef | None
    method_instance_ref: Pin | None
    input_manifest_hash: str
    output_manifest_hash: str | None
    #: Exact references exposed to the reviewer beside the fixed ones.
    material_refs: frozenset[AssuranceRef]
    #: Identity/complete-snapshot scope; the completion Scope id when there is one.
    scope_id: str
    #: OPERATION_OUTCOME only: the effect slot this outcome is judged for.
    effect_key: str | None = None

    @property
    def policy_domain_hash(self) -> str:
        return policy_domain_hash(
            self.purpose,
            scope_hash=None if self.scope_ref is None else self.scope_ref.pin.content_hash,
            task_hash=self.owner_task.content_hash,
            effect_key=self.effect_key,
        )


def _expression(criteria: Iterable[Any]) -> Any:
    ids = tuple(str(item.criterion_id) for item in criteria)
    if not ids:
        raise AssuranceError("SOURCE_UNAVAILABLE", "no criteria")
    if len(ids) == 1:
        return CriterionExpr(ids[0])
    return AllExpr(tuple(CriterionExpr(name) for name in ids))


def _exact_pin(reader: AssuranceReader, ref: AssuranceRef) -> AssuranceRef:
    reader.read_exact_metadata(ref)
    return ref


def _acceptance_refs(store: Any, acceptance_ids: Iterable[str]) -> set[AssuranceRef]:
    htn = HtnStore(store)
    refs: set[AssuranceRef] = set()
    for acceptance_id in sorted({str(item) for item in acceptance_ids}):
        body = htn.get_acceptance(acceptance_id).to_json()
        refs.add(AssuranceRef("acceptance", Pin(acceptance_id, 0, fingerprint(body))))
    return refs


def _scope_ref_for(store: Any, mission_id: str, occurrence_id: str) -> AssuranceRef:
    row = store.connection.execute(
        "SELECT scope_id,scope_hash,plan_revision FROM operation_completion_scopes "
        "WHERE mission_id=? AND occurrence_id=? ORDER BY plan_revision DESC LIMIT 1",
        (mission_id, occurrence_id),
    ).fetchone()
    if row is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "completion scope")
    active = HtnStore(store).active_plan_revision(mission_id)
    if active is None or int(active.revision) != int(row["plan_revision"]):
        raise AssuranceError("SOURCE_UNAVAILABLE", "completion scope is not the active plan's")
    return AssuranceRef("completion_scope", Pin(row["scope_id"], 0, row["scope_hash"]))


def _method_instance_pin(store: Any, mission_id: str, instance_id: str) -> Pin:
    row = store.connection.execute(
        "SELECT plan_revision,draft_json FROM method_instances WHERE mission_id=? AND instance_id=?",
        (mission_id, instance_id),
    ).fetchone()
    if row is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "method instance")
    return Pin(instance_id, int(row["plan_revision"]), fingerprint(decode(row["draft_json"])))


# ------------------------------------------------------------------ shared core
def prepare_purpose_review(
    commit: Any,
    *,
    tenant_id: str,
    mission_id: str,
    package: ReviewPackage,
    subject: PurposeSubject,
    requirements: RequirementsRevision,
    principal_id: str,
    authority: CurrentAuthority,
    cas: Any,
    config: Mapping[str, Any],
    context_policy_ref: Pin,
    reservation: Any,
    request_command_id: str,
    maximum_blob_bytes: int = 32 * 1024 * 1024,
    allow_in_transaction: bool = False,
):
    """Freeze one purpose's binding and ensure its first invocation.

    ``allow_in_transaction`` is for the original entries that own an outer UoW
    (T0 materialization, outcome persistence, method admission); the preparation
    then joins that UoW and its writes roll back with it. Blob pins and CAS bytes
    are still read through the same pin/authority path.
    """
    store = commit.store
    if store.connection.in_transaction and not allow_in_transaction:
        raise AssuranceError("REVIEW_PREPARATION_INSIDE_TRANSACTION")
    gate = commit._assurance_root_gate
    if gate is None:
        raise AssuranceError("ROOT_AUTHORITY_UNAVAILABLE")
    root = gate.require_execution()
    agent_config = config.get("agent_config", {})
    if agent_config.get("instructions") != REVIEW_INSTRUCTIONS:
        raise AssuranceError("REVIEW_TEMPLATE_UNREGISTERED")
    if str(package.purpose) != subject.purpose or package.binding.mission_id != mission_id:
        raise AssuranceError("REVIEW_PACKAGE_BINDING_MISMATCH")
    if not package.producer_agent_ids:
        raise AssuranceError("SOURCE_UNAVAILABLE", "no provable authors")
    purpose = subject.purpose
    with store.read_view():
        if AssuranceStore(store).lane(mission_id) != "ASSURANCE_1_1":
            raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
        reader = AssuranceReader(store, tenant_id=tenant_id, mission_id=mission_id)
        review_key = purpose_review_key(purpose, mission_id, str(package.package_id))
        package_ref = Pin(str(package.package_id), 0, fingerprint(package.to_json()))
        existing = store.connection.execute(
            "SELECT dispatch_intent_id FROM assurance_review_invocations "
            "WHERE mission_id=? AND review_key=? AND ordinal=1",
            (mission_id, review_key),
        ).fetchone()
        identity = UseIdentity(
            mission_id,
            "REVIEW",
            review_key,
            subject.scope_id,
            principal_id,
            "DISCLOSE",
            root.root_incarnation_id,
        )
        if existing is not None:
            invocation, prior_binding = read_review_invocation_locked(commit, reader, existing[0])
            if prior_binding.to_json()["package_ref"] != package_ref.to_json():
                raise AssuranceError("REVIEW_PACKAGE_BINDING_MISMATCH")
            _permission(authority, identity, subject.target, int(store.now * 1000))
            return invocation
        requirements_ref = AssuranceRef(
            "requirements",
            Pin(
                str(requirements.revision_id),
                int(requirements.revision),
                requirements.content_hash(),
            ),
        )
        if (
            int(package.binding.requirements_revision) != int(requirements.revision)
            or package.requirements_content_hash != requirements.content_hash()
        ):
            raise AssuranceError("REVIEW_PACKAGE_BINDING_MISMATCH")
        policy = store.connection.execute(
            "SELECT * FROM assurance_criterion_policies WHERE mission_id=? "
            "AND requirements_hash=? AND scope_hash=?",
            (mission_id, requirements.content_hash(), subject.policy_domain_hash),
        ).fetchone()
        if policy is None:
            raise AssuranceError("CHECK_POLICY_UNRESOLVED", purpose)
        policy_ref = AssuranceRef(
            "check_policy", Pin(policy["policy_id"], 0, policy["policy_hash"])
        )
        policies = decode(reader.read_exact_metadata(policy_ref).body_json)["criteria"]
        if {p["criterion_id"] for p in policies} != set(package.criterion_catalogue()):
            raise AssuranceError("POLICY_CATALOGUE_MISMATCH")
        task_ref = AssuranceRef("task", subject.owner_task)
        refs: set[AssuranceRef] = {
            subject.target,
            task_ref,
            requirements_ref,
            policy_ref,
            AssuranceRef(
                "input_manifest",
                Pin(subject.input_manifest_hash, 0, subject.input_manifest_hash),
            ),
        }
        if subject.scope_ref is not None:
            refs.add(subject.scope_ref)
        if subject.method_instance_ref is not None:
            refs.add(AssuranceRef("method_instance", subject.method_instance_ref))
        refs.update(subject.material_refs)
        for document in policies:
            policy_contract = CriterionPolicy.from_json(document)
            refs.update(ref for group in policy_contract.any_check_sets for ref in group)
        epochs = read_epochs_locked(store.connection, mission_id)
        metadata = tuple(
            reader.read_exact_metadata(ref) for ref in sorted(refs, key=lambda ref: ref.key)
        )
        complete = read_complete_evidence_snapshot(reader, scope_id=subject.scope_id)
        now_ms = int(store.now * 1000)
        permissions = tuple(
            (ref, _permission(authority, identity, ref, now_ms))
            for ref in sorted(refs, key=lambda ref: ref.key)
        )
        reads = [item.read_item for item in metadata] + [item.read_item for item in complete]
        for _, permission in permissions:
            reads.extend((permission.access, permission.policy))
        catalogue = build_catalogue(review_key, refs)
        binding = AssuranceReviewBinding.from_json(
            {
                "schema_version": 2,
                "mission_id": mission_id,
                "review_key": review_key,
                "package_ref": package_ref.to_json(),
                "round_no": 1,
                "subject": {
                    "purpose": purpose,
                    "target": subject.target.to_json(),
                    "owner_task_ref": subject.owner_task.to_json(),
                    "occurrence_id": subject.occurrence_id,
                    "completion_scope_ref": (
                        None if subject.scope_ref is None else subject.scope_ref.pin.to_json()
                    ),
                    "method_instance_ref": (
                        None
                        if subject.method_instance_ref is None
                        else subject.method_instance_ref.to_json()
                    ),
                    "input_manifest_hash": subject.input_manifest_hash,
                    "output_manifest_hash": subject.output_manifest_hash,
                },
                "requirements_ref": requirements_ref.pin.to_json(),
                "criterion_ids": sorted(package.criterion_catalogue()),
                "mandatory_ids": sorted(
                    str(c.criterion_id)
                    for c in package.criteria
                    if c.requirement_class is RequirementClass.HARD_CONSTRAINT
                ),
                "formula": assurance_formula(package.success_expression),
                "check_requirements": policies,
                "evidence_catalogue": [item.to_json() for item in catalogue],
                "producer_agent_ids": list(package.producer_agent_ids),
                "reviewer_policy_ref": Pin(
                    REVIEW_CODEC_VERSION,
                    1,
                    fingerprint(
                        {"instructions": REVIEW_INSTRUCTIONS, "codec": REVIEW_CODEC_VERSION}
                    ),
                ).to_json(),
                "context_policy_ref": context_policy_ref.to_json(),
                "criterion_policy_ref": policy_ref.to_json(),
                "read_set": [item.to_json() for item in _merge_reads(reads)],
                "catalogue_hash": fingerprint([item.to_json() for item in catalogue]),
            }
        )
    blob_refs = {ref for ref in refs if ref.kind in {"source", "artifact"}}
    pins = ensure_review_blob_pins(commit, tenant_id=tenant_id, binding=binding, refs=blob_refs)
    try:
        materials = {
            item.ref: item.body_json.encode() for item in metadata if item.ref not in blob_refs
        }
        remaining, blobs = maximum_blob_bytes, []
        for ref in sorted(blob_refs, key=lambda ref: ref.key):
            blob = read_pinned_blob(
                reader,
                ref,
                pin_id=pins[ref],
                review_key=review_key,
                cas=cas,
                maximum_bytes=remaining,
                authorize=lambda r: (
                    _permission(authority, identity, r, int(store.now * 1000)).access
                ),
                now_ms=lambda: int(store.now * 1000),
            )
            remaining -= len(blob.data)
            materials[ref] = blob.data
            blobs.append(blob)
        frozen_config = dict(config)
        frozen_config["message"] = render_review_input(
            binding, package=package.to_json(), materials=materials
        )

        def require_current_locked() -> None:
            current_ms = int(store.now * 1000)
            if gate.require_execution() != root:
                raise AssuranceError("RECHECK_REQUIRED")
            require_epochs_locked(store.connection, mission_id, epochs, now_ms=current_ms)
            for item in metadata:
                if reader.read_exact_metadata(item.ref) != item:
                    raise AssuranceError("RECHECK_REQUIRED")
            for ref, previous in permissions:
                _require_same_permission(authority, identity, ref, previous, current_ms)
            for blob in blobs:
                blob.require_current_locked(
                    reader,
                    now_ms=current_ms,
                    authorize=lambda ref: _permission(authority, identity, ref, current_ms).access,
                )

        return commit.ensure_assurance_review_invocation(
            tenant_id=tenant_id,
            binding=binding,
            package=package,
            request_command_id=request_command_id,
            config=frozen_config,
            reservation=reservation,
            require_current_locked=require_current_locked,
        )
    except Exception:
        release_failed_preparation(commit, binding)
        raise


# --------------------------------------------------------------- METHOD_PLAN
def method_plan_package(
    store: Any,
    *,
    mission_id: str,
    task_id: str,
    method_ref: Pin,
    producer_agent_ids: tuple[str, ...],
) -> tuple[ReviewPackage, PurposeSubject, RequirementsRevision]:
    """The original planning subject and registered method, frozen as one package.

    Criteria are the requirements criteria the subject's goal signature covers;
    a method that serves a goal covering nothing in the requirements has nothing
    an independent reviewer can be asked about (SOURCE_UNAVAILABLE, not a
    synthetic criterion). Authors must be provable by the caller: the planning
    intent that proposed the method.
    """
    htn = HtnStore(store)
    binding = htn.task_semantics_of(mission_id, task_id)
    if binding is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "planning subject")
    stored = htn.get_method(method_ref.id, int(method_ref.revision))
    reference = stored.contract.method_ref()
    if reference.content_hash != method_ref.content_hash:
        raise AssuranceError("REF_BODY_CONFLICT", method_ref.id)
    if str(stored.contract.goal_type_ref.id) != str(binding.goal_signature.signature_id):
        # A method for another goal is not a plan for this subject; reviewing it
        # against this subject's criteria would judge the wrong question.
        raise AssuranceError("SUBJECT_BINDING_INVALID", "method goal differs from subject goal")
    if (
        stored.registration.trial_scope_mission is not None
        and stored.registration.trial_scope_mission != mission_id
    ):
        raise AssuranceError("REF_SCOPE_MISMATCH", method_ref.id)
    if not producer_agent_ids or any(not str(item) for item in producer_agent_ids):
        raise AssuranceError("SOURCE_UNAVAILABLE", "no provable method author")
    requirements = htn.latest_requirements_revision(mission_id)
    if requirements is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "requirements")
    covered = set(binding.goal_signature.coverage_criteria)
    criteria = tuple(item for item in requirements.criteria if item.criterion_id in covered)
    if not criteria:
        raise AssuranceError("SOURCE_UNAVAILABLE", "method covers no requirements criterion")
    owner_task = Pin(str(binding.task_id), int(binding.contract_revision), binding.content_hash())
    manifest = htn.insert_input_manifest(
        mission_id,
        str(binding.task_id),
        {
            "consumer_task_ref": str(binding.task_id),
            "bindings": [],
            "method_ref": method_ref.to_json(),
            "registration_hash": content_hash_of(stored.registration.to_json()),
        },
        request_id="assurance-method-plan:" + method_ref.id,
    )
    occurrence_id = None
    scope_ref = None
    active = htn.active_plan_revision(mission_id)
    if active is not None:
        row = store.connection.execute(
            "SELECT occurrence_id FROM plan_memberships WHERE mission_id=? AND revision=? "
            "AND task_id=? LIMIT 2",
            (mission_id, int(active.revision), str(binding.task_id)),
        ).fetchall()
        if len(row) == 1:
            occurrence_id = str(row[0][0])
            try:
                scope_ref = _scope_ref_for(store, mission_id, occurrence_id)
            except AssuranceError:
                occurrence_id, scope_ref = None, None
    digest = content_hash_of(
        {
            "mission": mission_id,
            "task": str(binding.task_id),
            "method": method_ref.to_json(),
            "requirements": int(requirements.revision),
        }
    )[:32]
    package = ReviewPackage(
        package_id=ReviewPackageId(f"pkg-method-{digest}"),
        purpose=ReviewPurpose.METHOD_PLAN,
        binding=ReviewBinding(
            mission_id=mission_id,
            obligation_id=str(binding.obligation_id),
            subject_ref=TypedRef(
                kind=TypedRefKind.METHOD,
                id=method_ref.id,
                revision=int(method_ref.revision),
                content_hash=method_ref.content_hash,
            ),
            requirements_revision=int(requirements.revision),
            input_manifest_hash=manifest,
            policy_ref=TypedRef(
                kind=TypedRefKind.SOURCE,
                id=METHOD_PLAN_REVIEW_POLICY,
                revision=1,
                content_hash=content_hash_of(METHOD_PLAN_REVIEW_POLICY),
            ),
        ),
        criteria=criteria,
        success_expression=_expression(criteria),
        candidate_refs=(
            TypedRef(
                kind=TypedRefKind.METHOD,
                id=method_ref.id,
                revision=int(method_ref.revision),
                content_hash=method_ref.content_hash,
                produced_by=Provenance.TOOL,
            ),
        ),
        producer_agent_ids=tuple(str(item) for item in producer_agent_ids),
        reviewer_workspace_access=WorkspaceAccess.READ_ONLY,
        requirements_content_hash=requirements.content_hash(),
    )
    subject = PurposeSubject(
        purpose="METHOD_PLAN",
        target=AssuranceRef("method", method_ref),
        owner_task=owner_task,
        occurrence_id=occurrence_id,
        scope_ref=scope_ref,
        method_instance_ref=None,
        input_manifest_hash=manifest,
        output_manifest_hash=None,
        material_refs=frozenset(),
        scope_id="mission" if scope_ref is None else scope_ref.pin.id,
    )
    return package, subject, requirements


# --------------------------------------------------------------- COMPOSITION
def composition_subject(
    store: Any,
    *,
    mission_id: str,
    package: ReviewPackage,
    occurrence_id: str,
    accepted: Mapping[str, tuple[str, ...]],
) -> tuple[PurposeSubject, RequirementsRevision]:
    """The compound's own frozen scope, adopted method instance and child acceptances."""
    htn = HtnStore(store)
    task_id = str(package.binding.subject_ref.id)
    binding = htn.task_semantics_of(mission_id, task_id)
    if binding is None or package.method_instance_id is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "compound subject")
    scope_ref = _scope_ref_for(store, mission_id, occurrence_id)
    requirements = htn.get_requirements_revision(
        mission_id, int(package.binding.requirements_revision)
    )
    contributions = {key: sorted(str(item) for item in ids) for key, ids in accepted.items()}
    subject = PurposeSubject(
        purpose="COMPOSITION",
        target=AssuranceRef(
            "task", Pin(task_id, int(binding.contract_revision), binding.content_hash())
        ),
        owner_task=Pin(task_id, int(binding.contract_revision), binding.content_hash()),
        occurrence_id=str(occurrence_id),
        scope_ref=scope_ref,
        method_instance_ref=_method_instance_pin(
            store, mission_id, str(package.method_instance_id)
        ),
        input_manifest_hash=package.binding.input_manifest_hash,
        output_manifest_hash=output_manifest_hash(
            "COMPOSITION", {"occurrence_id": str(occurrence_id), "contributions": contributions}
        ),
        material_refs=frozenset(
            _acceptance_refs(store, (item for ids in accepted.values() for item in ids))
        ),
        scope_id=scope_ref.pin.id,
    )
    return subject, requirements


# ----------------------------------------------------------- ACTION_PROPOSAL
def action_proposal_subject(
    store: Any, *, mission_id: str, package: ReviewPackage, sources: Any, payloads: Any
) -> tuple[PurposeSubject, RequirementsRevision]:
    """T0's frozen payload objects and candidate bytes, as the reviewer's material."""
    proposal = payloads.action_proposal
    htn = HtnStore(store)
    # The proposal names the effect owner's Scope; the review's owner Task is that
    # Scope's Task (the producer may be another occurrence of the same plan).
    scope_row = store.connection.execute(
        "SELECT scope_hash, document_json FROM operation_completion_scopes "
        "WHERE mission_id=? AND scope_id=?",
        (mission_id, proposal.completion_scope_id),
    ).fetchone()
    if scope_row is None or scope_row["scope_hash"] != proposal.completion_scope_hash:
        raise AssuranceError("SOURCE_UNAVAILABLE", "owner scope")
    task_id = str(decode(scope_row["document_json"])["task_ref"]["id"])
    binding = htn.task_semantics_of(mission_id, task_id)
    if binding is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "owner task")
    del sources  # the producer Scope is frozen in the package, not a subject field
    scope_ref = AssuranceRef(
        "completion_scope", Pin(proposal.completion_scope_id, 0, proposal.completion_scope_hash)
    )
    requirements = htn.get_requirements_revision(
        mission_id, int(package.binding.requirements_revision)
    )
    materials = {
        AssuranceRef("artifact", Pin(ref.id, int(ref.revision), ref.content_hash))
        for ref in (
            payloads.parameters_ref,
            payloads.effect_contract_ref,
            proposal.candidate_artifact_ref,
        )
    }
    # The accepted file the frozen parameters publish (the reviewer judges
    # "parameters and candidate" against it; the freeze already refused any other
    # id or hash, so this only exposes it — NEXT-TG-1.0, 2026-09-27).
    published_id = payloads.parameters.effective_params.get("artifact_id")
    if published_id is not None:
        artifact = store.get_artifact(str(published_id))
        if artifact is None or artifact.content_hash != payloads.parameters.effective_params.get(
            "content_hash"
        ):
            raise AssuranceError("SOURCE_UNAVAILABLE", "published artifact")
        materials.add(
            AssuranceRef("artifact", Pin(artifact.id, int(artifact.version), artifact.content_hash))
        )
    # The four system check receipts T0 persisted for this package: the facts the
    # code gates enforced (registered adapter profile, conditional-write policy, ...).
    # Without them the reviewer could only answer UNKNOWN on the two capability
    # points (real run 2026-09-27: INCONCLUSIVE); the legacy reviewer saw them as
    # ``deterministic_checks``.
    checks = store.connection.execute(
        "SELECT commit_id, receipt_json FROM commit_receipts "
        "WHERE kind='operation_proposal_check' AND subject_id=? ORDER BY commit_id",
        (proposal.intent_id,),
    ).fetchall()
    for row in checks:
        receipt = decode(row[1])
        if receipt.get("package_id") == str(package.package_id):
            materials.add(AssuranceRef("commit_receipt", Pin(str(row[0]), 0, fingerprint(receipt))))
    subject = PurposeSubject(
        purpose="ACTION_PROPOSAL",
        target=AssuranceRef(
            "artifact",
            Pin(
                payloads.action_proposal_ref.id,
                int(payloads.action_proposal_ref.revision),
                payloads.action_proposal_ref.content_hash,
            ),
        ),
        owner_task=Pin(task_id, int(binding.contract_revision), binding.content_hash()),
        occurrence_id=str(proposal.completion_owner_occurrence_id),
        scope_ref=scope_ref,
        method_instance_ref=None,
        input_manifest_hash=package.binding.input_manifest_hash,
        output_manifest_hash=None,
        material_refs=frozenset(materials),
        scope_id=scope_ref.pin.id,
    )
    return subject, requirements


# -------------------------------------------------------- OPERATION_OUTCOME
def operation_outcome_subject(
    store: Any, *, mission_id: str, prepared: Any
) -> tuple[PurposeSubject, RequirementsRevision]:
    """The executed operation envelope, its observation and preparation receipts."""
    binding = prepared.binding
    htn = HtnStore(store)
    scope_row = store.connection.execute(
        "SELECT scope_id,scope_hash,occurrence_id,document_json FROM operation_completion_scopes "
        "WHERE mission_id=? AND scope_id=?",
        (mission_id, binding.completion_scope_id),
    ).fetchone()
    if scope_row is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "owner scope")
    scope_document = decode(scope_row["document_json"])
    task_ref = scope_document["task_ref"]
    task_binding = htn.task_semantics_of(mission_id, str(task_ref["id"]))
    if task_binding is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "owner task")
    operation = store.connection.execute(
        "SELECT envelope_json FROM operation_bindings WHERE operation_occurrence_id=? AND mission_id=?",
        (binding.operation_occurrence_id, mission_id),
    ).fetchone()
    if operation is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "operation binding")
    requirements = htn.get_requirements_revision(
        mission_id, int(prepared.package.binding.requirements_revision)
    )
    producer = {
        "kind": "operation_outcome_review_prepared",
        "mission_id": binding.mission_id,
        "subject_id": prepared.binding_id,
        "binding_hash": binding.content_hash(),
        "source_manifest_hash": content_hash_of(prepared.manifest),
        "review_package_id": str(prepared.package.package_id),
    }
    materials = {
        AssuranceRef(
            "commit_receipt",
            Pin(binding.source_receipt_refs[0].id, 0, fingerprint(prepared.observation)),
        ),
        AssuranceRef(
            "commit_receipt", Pin("prepare:" + prepared.binding_id, 0, fingerprint(producer))
        ),
    }
    scope_ref = AssuranceRef(
        "completion_scope", Pin(scope_row["scope_id"], 0, scope_row["scope_hash"])
    )
    subject = PurposeSubject(
        purpose="OPERATION_OUTCOME",
        target=AssuranceRef(
            "operation",
            Pin(
                binding.operation_occurrence_id, 0, fingerprint(decode(operation["envelope_json"]))
            ),
        ),
        owner_task=Pin(
            str(task_binding.task_id),
            int(task_binding.contract_revision),
            task_binding.content_hash(),
        ),
        occurrence_id=str(scope_row["occurrence_id"]),
        scope_ref=scope_ref,
        method_instance_ref=None,
        input_manifest_hash=prepared.package.binding.input_manifest_hash,
        output_manifest_hash=None,
        material_refs=frozenset(materials),
        scope_id=scope_ref.pin.id,
        effect_key=str(binding.effect_key),
    )
    return subject, requirements


# ------------------------------------------------------------ MISSION_FINAL
def mission_final_subject(
    store: Any, *, mission_id: str, package: ReviewPackage, dispatch: Any
) -> tuple[PurposeSubject, RequirementsRevision]:
    """The root Task, its frozen Scope and the contributions the cut was made over."""
    htn = HtnStore(store)
    network = dispatch.network(mission_id)
    roots = tuple(network.root_occurrence_ids)
    if len(roots) != 1:
        raise AssuranceError("SOURCE_UNAVAILABLE", "no unique root occurrence")
    root_occurrence = str(roots[0])
    task_id = str(package.binding.subject_ref.id)
    binding = htn.task_semantics_of(mission_id, task_id)
    if binding is None or str(network.occurrence(roots[0]).task_id) != task_id:
        raise AssuranceError("SOURCE_UNAVAILABLE", "root task")
    scope_ref = _scope_ref_for(store, mission_id, root_occurrence)
    requirements = htn.get_requirements_revision(
        mission_id, int(package.binding.requirements_revision)
    )
    contributions = tuple(sorted(str(ref.id) for ref in package.child_acceptance_refs))
    adopted = network.adopted_instance_for(roots[0])
    instance = (
        None
        if adopted is None
        else _method_instance_pin(store, mission_id, str(adopted.instance_id))
    )
    subject = PurposeSubject(
        purpose="MISSION_FINAL",
        target=AssuranceRef(
            "task", Pin(task_id, int(binding.contract_revision), binding.content_hash())
        ),
        owner_task=Pin(task_id, int(binding.contract_revision), binding.content_hash()),
        occurrence_id=root_occurrence,
        scope_ref=scope_ref,
        method_instance_ref=instance,
        input_manifest_hash=package.binding.input_manifest_hash,
        output_manifest_hash=output_manifest_hash(
            "MISSION_FINAL", {"contributions": list(contributions)}
        ),
        material_refs=frozenset(_acceptance_refs(store, contributions)),
        scope_id=scope_ref.pin.id,
    )
    return subject, requirements


__all__ = (
    "METHOD_PLAN_REVIEW_POLICY",
    "PurposeSubject",
    "action_proposal_subject",
    "assurance_review_runtime",
    "composition_subject",
    "method_plan_package",
    "mission_final_subject",
    "operation_outcome_subject",
    "output_manifest_hash",
    "policy_domain_hash",
    "prepare_purpose_review",
    "purpose_review_key",
)
