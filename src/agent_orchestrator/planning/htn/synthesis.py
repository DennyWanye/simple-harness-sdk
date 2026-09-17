# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The MethodSynthesizer's *system* side (§7.3 source 4, §18.5 C8).

§7.3 lists four sources a new method may come from, and an LLM proposing a
candidate for a goal nobody has a method for is one of them.  It is not a special
source: it goes through the *same* six-step admission protocol as a human-written
one, and it stops at ``TRIAL_ADMITTED`` like everything else in P2.

This module is the half of that which is not a model call:

:meth:`MethodSynthesizer.build_request`
    build the **typed context** the synthesising role is given — the goal signature
    it must satisfy, the criteria it has to cover, the operators this deployment
    actually registers (with whether their capability is healthy), the applicability
    reports of the methods that were tried and did not fit, and the list of fields
    the model may not write.  A prompt assembled from prose would let a model widen
    its own options; a request assembled from the catalogue cannot.
:meth:`MethodSynthesizer.accept_response`
    take the ``<method_proposal>`` block back, parse it strictly, and hand it to
    :meth:`~.registry.MethodRegistry.admit`.  Nothing is normalised on the way in:
    §7.3 and §6.3 put the registry status *outside* the definition, so a payload
    that spells ``ADMITTED`` is refused with ``MODEL_CLAIMED_STATUS`` and the
    refusal is recorded — quietly rewriting it to ``DRAFT`` would hide the attempt.

What is deliberately **not** here:

* **the model call.**  §18.5 says the MethodSynthesizer goes through the existing
  persistent dispatch intent, ``BaseAgent`` and cost channel, and that a new role
  purpose "must not masquerade as a Task Critic and land on the wrong Task
  budget".  So this module never calls a provider; the second half of P2.3c wires
  :data:`~...runtime.role_templates.METHOD_SYNTHESIZER` through ``BaseAgent`` on the
  mission-planning account.
* **promotion.**  There is no ``promote`` on :class:`MethodSynthesizer`, by
  construction: past ``TRIAL_ADMITTED`` lies the offline multi-instance evaluation
  P8 delivers, and :meth:`~.registry.MethodRegistry.promote` answers
  ``PROMOTION_NOT_AVAILABLE`` for every target.  Succeeding once is not a promotion.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...contracts.htn import (
    GoalSignature,
    MethodRegistryStatus,
    MissionRef,
    RegistryAuthor,
    TaskForm,
    TaskSemanticBindingV1,
)
from ...contracts.models import ContractError
from ...contracts.semantic_base import VersionedRef, content_hash_of
from ...runtime.output_blocks import BlockError
from ...runtime.role_templates import METHOD_PROPOSAL_TAG, METHOD_SYNTHESIZER

# ``SYSTEM_BOUND_FIELDS`` comes from the one module that owns the list, so this
# request and ``planning.planner``'s ingress cannot disagree about what "the system
# binds this" means (§18.5).  The import is safe in this direction: the planner
# reaches the commit service, and nothing the commit service imports reaches here.
from ..planner import SYSTEM_BOUND_FIELDS, parse_method_proposal
from .applicability import ApplicabilityReport, CapabilitySnapshot
from .registry import (
    AdmissionPolicy,
    AdmissionReceipt,
    AdmissionVerdict,
    MethodCandidate,
    MethodRegistry,
    RejectionCode,
    TaskTypeCatalog,
)

#: The statuses a synthesised method may legitimately reach in P2.  Anything else
#: coming back from the registry is a bug in the registry, not a stronger method,
#: and :meth:`MethodSynthesizer.accept_response` refuses to return it.
SYNTHESIS_TERMINAL_STATUSES: frozenset[MethodRegistryStatus] = frozenset(
    {MethodRegistryStatus.TRIAL_ADMITTED, MethodRegistryStatus.REJECTED}
)

#: Sub-trees of :meth:`SynthesisRequest.to_json` that hold *domain values* rather
#: than structural claims.  ``planning.planner._refuse_authority_claims`` draws the
#: same line in the other direction: a method parameter that happens to be called
#: ``scope`` is a value, and refusing it would make the contract depend on a
#: domain's vocabulary.
VALUE_CONTAINERS: frozenset[str] = frozenset({"goal_parameters"})

#: Who the synthesis path attributes a reply to.  Not a parameter with a default:
#: see :meth:`MethodSynthesizer.accept_response`.
SYNTHESIS_AUTHOR = RegistryAuthor.MODEL

#: The maximum number of operator offers a request carries.  A context is a budget
#: as well as a description; a deployment with a thousand task types does not get a
#: thousand-entry prompt.
DEFAULT_MAX_OPERATORS = 64

#: P2.3g.  The field names the codec requires, by object, as the request states them
#: to the model (``method_shape``).  They are spelled here rather than read off
#: ``MethodContract.from_json`` because that function keeps its list inline; the
#: suite pins the two against each other (drop any name → the codec names it).
METHOD_SHAPE: Mapping[str, tuple[str, ...]] = {
    "method": (
        "schema_version",
        "method_id",
        "method_version",
        "goal_type_ref",
        "parameter_schema_ref",
        "output_schema_ref",
        "applicable_when",
        "exploration_assumptions",
        "steps",
        "ordering",
        "required_capabilities",
        "expected_effects",
        "composition",
        "basis_refs",
    ),
    "step": (
        "local_id",
        "task_type_ref",
        "form",
        "arguments",
        "required_capabilities",
        "obligation_relation",
    ),
    "composition": (
        "criterion_links",
        "outputs",
        "finalizer_step",
        "independent_review_required",
    ),
    "criterion_link": (
        "parent_criterion_id",
        "child_step",
        "child_criterion_id",
        "evidence_requirement",
    ),
    "versioned_ref": ("id", "version", "content_hash"),
}


class SynthesisReplyUnreadable(ContractError):
    """The synthesiser's reply could not be decoded into a submission (P2.3g).

    Distinct from a *rejected* method: the registry never saw this one.  ``problems``
    is what the next ask is given as ``schema_feedback`` — the codec's own words
    ("… is missing required fields: […]"), not a paraphrase — and ``block_defect``
    is the :class:`BlockError` reason when the block itself was the problem, else
    ``"schema"``.
    """

    def __init__(self, error: ContractError) -> None:
        super().__init__(str(error))
        cause = error.__cause__
        self.block_defect = cause.reason if isinstance(cause, BlockError) else "schema"
        self.problems: tuple[str, ...] = (str(error),)


def synthesis_schema_feedback(error: SynthesisReplyUnreadable) -> tuple[str, ...]:
    """What the second ask carries: the problems, verbatim, and one instruction."""

    return (
        *error.problems,
        "上一次的回复没有通过解码（见上）。按 method_shape 与提示词里的例子逐字段改正后，"
        f"重新只输出一个 <{METHOD_PROPOSAL_TAG}>…</{METHOD_PROPOSAL_TAG}> 块。",
    )


#: P2.3i.  The admission protocol's refusals a second ask can act on: every one of
#: them names a **reference or a shape** the model wrote wrongly and the request
#: package already carries the right value for — a port the consuming type does not
#: declare (``operators[].input_ports``), a task type or schema ref whose id/version/hash
#: is not the one offered, a step spelled as the wrong form, an argument reading a
#: parameter or a step output that does not exist, an ordering or criterion link naming
#: an unknown step, a cyclic order, a goal criterion left uncovered.  The first real
#: round on ``method-synthesizer-v2`` (Grok, H-L3-C1-r0) wrote a six-step method whose
#: only defect was one such slip — ``summarize`` bound ``report``, which
#: ``code.summarize-review`` does not declare, though the package said so — and was
#: concluded ``REJECTED`` on the spot.
CORRECTABLE_REJECTIONS: frozenset[RejectionCode] = frozenset(
    {
        RejectionCode.PORT_UNAVAILABLE,
        RejectionCode.UNKNOWN_TASK_TYPE,
        RejectionCode.UNKNOWN_OPERATOR,
        RejectionCode.UNKNOWN_SCHEMA,
        RejectionCode.FORM_MISMATCH,
        RejectionCode.MALFORMED_DEFINITION,
        RejectionCode.ORDERING_CYCLE,
        RejectionCode.ROOT_COVERAGE_GAP,
    }
)

#: The refusals a second ask is **not** opened for.  They are not slips of the pen:
#: a claimed registry status or a self-asserted author is the §18.5 / §6.3 boundary
#: (the prompt forbids it, the refusal is the record); a predicate the deployment does
#: not register or types wrongly is a *precondition* the request never offered (no
#: predicate list travels in the package, and I18 is not relaxed by asking again); a
#: capability nobody declares is a deployment fact; a recursion with no guard and a
#: size bound are policy limits the package does not state; ``ALREADY_REGISTERED`` is
#: registry state, not the reply's shape.
NON_CORRECTABLE_REJECTIONS: frozenset[RejectionCode] = frozenset(
    {
        RejectionCode.MODEL_CLAIMED_STATUS,
        RejectionCode.ALREADY_REGISTERED,
        RejectionCode.UNKNOWN_PREDICATE,
        RejectionCode.PREDICATE_TYPE_ERROR,
        RejectionCode.UNKNOWN_CAPABILITY,
        RejectionCode.UNBOUNDED_RECURSION,
        RejectionCode.SIZE_BOUND,
    }
)

# Every code the protocol can produce is classified exactly once; a new code added to
# the enum without a line above fails at import, which is where it should fail.
assert CORRECTABLE_REJECTIONS.isdisjoint(NON_CORRECTABLE_REJECTIONS)
assert CORRECTABLE_REJECTIONS | NON_CORRECTABLE_REJECTIONS == frozenset(RejectionCode), (
    "every RejectionCode must be classified as correctable or not (P2.3i)"
)


def rejection_is_correctable(receipt: AdmissionReceipt) -> bool:
    """Whether a *read and refused* reply is worth one more ask with its problems.

    ``True`` only when the receipt is a ``REJECTED`` verdict and **every** problem on
    it is in :data:`CORRECTABLE_REJECTIONS`: the protocol stops at the first failing
    step, so the problems it lists are the whole of that step's complaint, and a
    single one the model cannot act on (a missing capability beside a port slip) makes
    the second ask pointless — the round then concludes on the reply it has.
    """

    if not isinstance(receipt, AdmissionReceipt):
        raise ContractError("rejection_is_correctable expects an AdmissionReceipt")
    if receipt.verdict is not AdmissionVerdict.REJECTED or not receipt.problems:
        return False
    return all(problem.code in CORRECTABLE_REJECTIONS for problem in receipt.problems)


def rejection_problems(receipt: AdmissionReceipt) -> tuple[str, ...]:
    """The receipt's problems as the round records them: ``CODE: detail``, verbatim."""

    return tuple(f"{item.code!s}: {item.detail}" for item in receipt.problems)


def synthesis_rejection_feedback(receipt: AdmissionReceipt) -> tuple[str, ...]:
    """What the second ask carries after a correctable refusal (P2.3i).

    The protocol's own words — code and detail, exactly as ``MethodSynthesisRoundRecorded``
    would have recorded them — and one instruction that says which kind of refusal this
    is and what may change: the reference or shape named, nothing else, same
    ``method_id`` and ``method_version``.
    """

    return (
        *rejection_problems(receipt),
        "上一次的回复已经通过解码，但被注册协议拒绝（见上，每条以拒绝码开头）。只改正它点名的引用或形状："
        "端口名、task_type_ref、criterion_links、ordering、arguments 都必须照抄输入 operators / "
        "goal_signature 里声明的；保留 method_id 与 method_version，"
        f"重新只输出一个 <{METHOD_PROPOSAL_TAG}>…</{METHOD_PROPOSAL_TAG}> 块。",
    )


def authority_claims(payload: object, *, path: str = "") -> tuple[str, ...]:
    """Every system-bound field name that appears as a *structural* key in ``payload``.

    Used by the suite and by :meth:`SynthesisRequest.__post_init__`: a synthesis
    context that carried ``registry_status`` or ``manager_epoch`` would be telling
    the model those are its to fill in, which is the bypass §18.5 closes.
    """

    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            name = str(key)
            if name in SYSTEM_BOUND_FIELDS:
                found.append(f"{path}{name}" if path else name)
            if name in VALUE_CONTAINERS:
                continue
            found.extend(authority_claims(value, path=f"{path}{name}."))
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found.extend(authority_claims(item, path=f"{path}[{index}]."))
    return tuple(dict.fromkeys(found))


@dataclass(frozen=True, slots=True)
class OperatorOffer:
    """One task type this deployment registers, as the synthesiser may see it.

    ``available`` is the deployment fact §7.3 step 2 turns into "clearly not
    executable": an operator whose capability is unhealthy is still *offered*, with
    its unavailability stated, because a method built on it is refusable for a
    reason the model can act on — rather than silently absent from a context, which
    reads as "no such operator exists".
    """

    task_type_id: str
    version: int
    content_hash: str
    form: str
    statement: str
    input_ports: tuple[str, ...] = ()
    output_ports: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    unavailable_capabilities: tuple[str, ...] = ()
    coverage_criteria: tuple[str, ...] = ()
    side_effect_kind: str = ""
    domain: str | None = None

    @property
    def available(self) -> bool:
        return not self.unavailable_capabilities

    def to_json(self) -> dict[str, Any]:
        return {
            "task_type_ref": {
                "id": self.task_type_id,
                "version": self.version,
                "content_hash": self.content_hash,
            },
            "form": self.form,
            "statement": self.statement,
            "input_ports": list(self.input_ports),
            "output_ports": list(self.output_ports),
            "required_capabilities": list(self.required_capabilities),
            "unavailable_capabilities": list(self.unavailable_capabilities),
            "coverage_criteria": list(self.coverage_criteria),
            "side_effect_kind": self.side_effect_kind,
            "domain": self.domain,
            "available": self.available,
        }


@dataclass(frozen=True, slots=True)
class ApplicabilityNote:
    """Why one already-registered method did not fit this goal.

    Carried into the request so the model is told what was already tried and how it
    failed.  The four axes stay apart for the same reason
    :class:`~.applicability.ApplicabilityReport` keeps them apart: a missing
    capability, a false precondition, a conflict and a type error call for four
    different candidates.
    """

    method_id: str
    method_version: int
    content_hash: str
    status: str
    truth: str
    unmet_capabilities: tuple[str, ...] = ()
    needs_evidence: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    type_errors: tuple[str, ...] = ()

    @classmethod
    def of(cls, candidate: MethodCandidate, report: ApplicabilityReport) -> ApplicabilityNote:
        return cls(
            method_id=candidate.method_ref.method_id,
            method_version=int(candidate.method_ref.version),
            content_hash=candidate.method_ref.content_hash,
            status=str(report.status),
            truth=str(report.truth),
            unmet_capabilities=tuple(report.unmet_capabilities),
            needs_evidence=tuple(report.needs_evidence),
            conflicts=tuple(report.conflicts),
            type_errors=tuple(report.type_errors),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "method_ref": {
                "id": self.method_id,
                "version": self.method_version,
                "content_hash": self.content_hash,
            },
            "status": self.status,
            "truth": self.truth,
            "unmet_capabilities": list(self.unmet_capabilities),
            "needs_evidence": list(self.needs_evidence),
            "conflicts": list(self.conflicts),
            "type_errors": list(self.type_errors),
        }


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    """The typed context one synthesis round is given (§18.5 C8).

    It is data, not a prompt: :meth:`to_json` is what a role template renders, and
    nothing in it is a field the system binds — :func:`authority_claims` is asserted
    empty at construction, so a request can never hand the model the vocabulary of
    its own authority.
    """

    goal_task_id: str
    obligation_id: str
    goal_signature: Mapping[str, Any]
    required_criteria: tuple[str, ...] = ()
    goal_parameters: Mapping[str, Any] = field(default_factory=dict)
    operators: tuple[OperatorOffer, ...] = ()
    rejected_methods: tuple[ApplicabilityNote, ...] = ()
    unavailable_capabilities: tuple[str, ...] = ()
    suggested_method_refs: tuple[Mapping[str, Any], ...] = ()
    #: P2.3g: the goal type's own ``{id, version, content_hash}`` from the catalogue —
    #: ``method.goal_type_ref`` is copied from it.  ``None`` when the deployment does
    #: not declare the type (the empty-library case).
    goal_type_ref: Mapping[str, Any] | None = None
    #: P2.3g: the codec's problems with the previous reply, when this is the second
    #: ask on the same anchor.  Empty on a first ask.  P2.3i: or the admission
    #: protocol's problems (``CODE: detail`` lines) when the previous reply was read
    #: and refused for something the model can correct.
    schema_feedback: tuple[str, ...] = ()
    #: P2.3j: why a method that *applied* is nevertheless not enough — the root
    #: review rejected the result the adopted method produced, and these are the
    #: reviewer's findings plus the rejected method's identity.  Its own field: a
    #: review finding is not a decode problem, and the prompt says which is which.
    review_feedback: tuple[str, ...] = ()
    output_tag: str = METHOD_PROPOSAL_TAG
    role_prompt_version: str = METHOD_SYNTHESIZER.prompt_version
    #: What the model may not write, stated *in* the request.  §18.5 refuses such a
    #: field at the boundary rather than overwriting it, and a model that was never
    #: told cannot avoid it.
    forbidden_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "forbidden_fields",
            tuple(sorted(self.forbidden_fields or SYSTEM_BOUND_FIELDS)),
        )
        object.__setattr__(
            self, "schema_feedback", tuple(str(item) for item in self.schema_feedback)
        )
        object.__setattr__(
            self, "review_feedback", tuple(str(item) for item in self.review_feedback)
        )
        if self.goal_type_ref is not None:
            object.__setattr__(self, "goal_type_ref", dict(self.goal_type_ref))
        claims = authority_claims(self.to_json())
        if claims:
            raise ContractError(
                f"a synthesis request may not carry the system-bound fields {list(claims)}; "
                "a model proposes the shape of the work, never its authority (§18.5)"
            )

    @property
    def available_operators(self) -> tuple[OperatorOffer, ...]:
        return tuple(item for item in self.operators if item.available)

    def to_json(self) -> dict[str, Any]:
        return {
            "goal_task_ref": self.goal_task_id,
            "duty_ref": self.obligation_id,
            "goal_signature": dict(self.goal_signature),
            "required_criteria": list(self.required_criteria),
            "goal_parameters": dict(self.goal_parameters),
            "operators": [item.to_json() for item in self.operators],
            "rejected_methods": [item.to_json() for item in self.rejected_methods],
            "unavailable_capabilities": list(self.unavailable_capabilities),
            "suggested_method_refs": [dict(item) for item in self.suggested_method_refs],
            "goal_type_ref": None if self.goal_type_ref is None else dict(self.goal_type_ref),
            "method_shape": {key: list(value) for key, value in METHOD_SHAPE.items()},
            "schema_feedback": list(self.schema_feedback),
            "review_feedback": list(self.review_feedback),
            "output_tag": self.output_tag,
            "role_prompt_version": self.role_prompt_version,
            "forbidden_fields": list(self.forbidden_fields),
        }

    def content_hash(self) -> str:
        """The identity of this request, so a retry can be told from a new round."""

        return content_hash_of(self.to_json())


class MethodSynthesizer:
    """§7.3 source 4, system side.  It builds a request and admits a reply.

    It holds no provider and no store: the registry and the catalogue are the two
    declarations it reads, and every decision it makes is a function of them plus the
    text it is handed.  There is no ``promote`` — see the module docstring.
    """

    def __init__(
        self,
        registry: MethodRegistry,
        catalog: TaskTypeCatalog,
        *,
        max_operators: int = DEFAULT_MAX_OPERATORS,
    ) -> None:
        if not isinstance(registry, MethodRegistry):
            raise ContractError("MethodSynthesizer needs a MethodRegistry")
        if not isinstance(catalog, TaskTypeCatalog):
            raise ContractError("MethodSynthesizer needs a TaskTypeCatalog")
        if int(max_operators) < 1:
            raise ContractError("max_operators must be at least 1")
        self._registry = registry
        self._catalog = catalog
        self._max_operators = int(max_operators)

    @property
    def registry(self) -> MethodRegistry:
        return self._registry

    @property
    def role(self) -> Any:
        """The role template this synthesiser's requests are rendered with."""

        return METHOD_SYNTHESIZER

    # ------------------------------------------------------------------- the request
    def build_request(
        self,
        goal: TaskSemanticBindingV1,
        capabilities: CapabilitySnapshot,
        registry: MethodRegistry | None = None,
        *,
        reports: Mapping[str, ApplicabilityReport] | None = None,
        mission_id: str | None = None,
        domain: str | None = None,
        schema_feedback: Sequence[str] = (),
        review_feedback: Sequence[str] = (),
    ) -> SynthesisRequest:
        """The typed context for "this compound goal has no usable method".

        ``reports`` is keyed by ``method_id@version`` and is what turns "nothing
        applied" into something actionable: the model is told which methods exist for
        this goal type and which axis each of them failed on.  ``mission_id`` is
        accepted so a caller can scope the candidate lookup, and is deliberately
        *not* written into the request: which Mission this is belongs to the dispatch
        that carries the request, never to the payload the model reads (§18.5).
        """

        if not isinstance(goal, TaskSemanticBindingV1):
            raise ContractError("build_request expects the goal's TaskSemanticBindingV1")
        if not isinstance(capabilities, CapabilitySnapshot):
            raise ContractError("build_request expects a CapabilitySnapshot")
        if goal.form is not TaskForm.COMPOUND:
            raise ContractError(
                "a method is synthesised for a compound goal; a primitive is dispatched to "
                "its operator and needs no decomposition (§6.2)"
            )
        source = self._registry if registry is None else registry
        signature = goal.goal_signature
        offers = self._offers(capabilities, domain=domain)
        goal_type = self.goal_type_ref(signature)
        notes = self._notes(source, goal_type, reports or {}, mission_id=mission_id)
        return SynthesisRequest(
            goal_task_id=str(goal.task_id),
            obligation_id=str(goal.obligation_id),
            goal_signature=signature.to_json(),
            required_criteria=tuple(goal.requirement_refs),
            goal_parameters=dict(goal.typed_parameters),
            operators=offers,
            rejected_methods=notes,
            unavailable_capabilities=tuple(
                sorted(
                    {
                        capability
                        for offer in offers
                        for capability in offer.unavailable_capabilities
                    }
                )
            ),
            suggested_method_refs=(
                tuple(
                    {
                        "id": suggestion.method_ref.method_id,
                        "version": int(suggestion.method_ref.version),
                        "content_hash": suggestion.method_ref.content_hash,
                        "reason": str(suggestion.reason),
                        "advisory_only": True,
                    }
                    for suggestion in source.suggest_for(
                        goal_type, mission_id=MissionRef(mission_id)
                    )
                )
                if mission_id is not None and goal_type is not None
                else ()
            ),
            goal_type_ref=None if goal_type is None else goal_type.to_json(),
            schema_feedback=tuple(schema_feedback),
            review_feedback=tuple(review_feedback),
        )

    def goal_type_ref(self, signature: GoalSignature) -> VersionedRef | None:
        """The task type ref the registry keys its candidates by, from the catalogue.

        A :class:`~...contracts.htn.GoalSignature` carries no content hash of its
        own, and the registry matches a method's ``goal_type_ref`` on the full
        ``(id, version, content_hash)`` triple — so the hash has to come from the
        declaration that owns it rather than be derived here.  A goal type this
        deployment does not declare returns ``None``: there is then nothing to
        retrieve candidates or suggestions for, which is exactly the "empty library"
        case a synthesis round exists to answer.
        """

        for spec in self._catalog.task_types():
            if spec.goal_signature.signature_id == signature.signature_id and int(
                spec.goal_signature.version
            ) == int(signature.version):
                return spec.task_type_ref
        return None

    def _offers(
        self, capabilities: CapabilitySnapshot, *, domain: str | None
    ) -> tuple[OperatorOffer, ...]:
        offers: list[OperatorOffer] = []
        # P2.3k / defect N1: a task type published at a new version (the way P2.3h
        # published the fix methods at @2) is offered at that version only.  The older
        # row stays in the catalogue — a method that already names it still resolves
        # and a stored reply still replays — but a synthesiser shown both would be
        # invited to build on the one whose ports were the defect.
        latest: dict[str, int] = {}
        for spec in self._catalog.task_types():
            key = spec.task_type_ref.id
            latest[key] = max(latest.get(key, 0), int(spec.task_type_ref.version))
        for spec in sorted(
            self._catalog.task_types(),
            key=lambda item: (item.task_type_ref.id, int(item.task_type_ref.version)),
        ):
            if spec.form is not TaskForm.PRIMITIVE:
                continue
            if int(spec.task_type_ref.version) < latest[spec.task_type_ref.id]:
                continue
            if domain is not None and spec.domain is not None and spec.domain != domain:
                continue
            ref = spec.task_type_ref
            offers.append(
                OperatorOffer(
                    task_type_id=ref.id,
                    version=int(ref.version),
                    content_hash=ref.content_hash,
                    form=str(spec.form),
                    statement=spec.goal_signature.statement,
                    input_ports=tuple(port.port_key for port in spec.input_ports),
                    output_ports=tuple(port.port_key for port in spec.output_ports),
                    required_capabilities=tuple(spec.required_capabilities),
                    unavailable_capabilities=capabilities.missing_from(
                        tuple(spec.required_capabilities)
                    ),
                    coverage_criteria=tuple(spec.goal_signature.coverage_criteria),
                    side_effect_kind=str(spec.side_effect_kind),
                    domain=spec.domain,
                )
            )
            if len(offers) >= self._max_operators:
                break
        return tuple(offers)

    @staticmethod
    def _notes(
        registry: MethodRegistry,
        goal_type: VersionedRef | None,
        reports: Mapping[str, ApplicabilityReport],
        *,
        mission_id: str | None,
    ) -> tuple[ApplicabilityNote, ...]:
        if mission_id is None or goal_type is None:
            return ()
        notes: list[ApplicabilityNote] = []
        for candidate in registry.candidates_for(goal_type, mission_id=MissionRef(mission_id)):
            key = f"{candidate.method_ref.method_id}@{int(candidate.method_ref.version)}"
            report = reports.get(key)
            if report is None or report.applicable:
                continue
            notes.append(ApplicabilityNote.of(candidate, report))
        return tuple(notes)

    # ------------------------------------------------------------------ the response
    def accept_response(
        self,
        text: str,
        *,
        policy: AdmissionPolicy,
        author_override: RegistryAuthor | None = None,
    ) -> AdmissionReceipt:
        """``<method_proposal>`` → §7.3's six steps → at most ``TRIAL_ADMITTED``.

        The author is **fixed** at :attr:`RegistryAuthor.MODEL` on this path.  That is
        not a default: ``text`` reached here from a model, so presenting it as anything
        else would hand a model-authored definition the registration rights of the
        registry service, and ``MethodRegistration`` allows a model-authored row only
        at DRAFT precisely to stop that (§6.3, §7.3).  A caller that genuinely has a
        human- or tool-authored definition passes ``author_override`` and says so; the
        P2.3c review asked for exactly this shape, so the synthesis path cannot be
        used to launder authorship.

        The status the payload declares is *kept* and refused by the protocol rather
        than normalised here: a silent rewrite to ``DRAFT`` would leave no record
        that a model-authored submission tried to award itself an admission (§6.3).
        A payload that declares a *different* author than the one presented is refused
        for the same reason — authorship is not self-asserted.

        A malformed block raises :class:`ContractError` and is the caller's bounded
        repair (§18.5 C8) — not a second request, and not a rejected method.
        """

        if not isinstance(policy, AdmissionPolicy):
            raise ContractError("accept_response expects an AdmissionPolicy")
        resolved = (
            SYNTHESIS_AUTHOR if author_override is None else RegistryAuthor(str(author_override))
        )
        try:
            proposal = parse_method_proposal(text)
        except ContractError as error:
            # P2.3g: named, so the caller can ask once more with the codec's words
            # attached instead of concluding the round on a reply nobody could read.
            raise SynthesisReplyUnreadable(error) from error
        receipt = self._registry.admit(proposal, author=resolved, policy=policy)
        if receipt.verdict not in (AdmissionVerdict.TRIAL_ADMITTED, AdmissionVerdict.REJECTED):
            raise ContractError(
                f"the registry answered {receipt.verdict!s} for a synthesis submission; P2 "
                "implements the lifecycle only as far as TRIAL_ADMITTED (§7.3)"
            )
        if receipt.status not in SYNTHESIS_TERMINAL_STATUSES:
            raise ContractError(
                f"a synthesised method reached {receipt.status!s}; succeeding once is not a "
                "promotion and the offline evaluation is P8 (§7.3)"
            )
        return receipt

    @staticmethod
    def proposal_declares_status(text: str) -> bool:
        """Whether the block claims a ``registry_status`` at all (diagnostics only).

        It is *not* a gate: the gate is the admission protocol, which refuses the
        claim and records it.  This exists so a caller can report "the model tried to
        award itself a status" without re-parsing the block by hand.
        """

        proposal = parse_method_proposal(text)
        return proposal.declared_status is not MethodRegistryStatus.DRAFT


def build_request(
    goal: TaskSemanticBindingV1,
    capabilities: CapabilitySnapshot,
    registry: MethodRegistry,
    *,
    catalog: TaskTypeCatalog,
    reports: Mapping[str, ApplicabilityReport] | None = None,
    mission_id: str | None = None,
    domain: str | None = None,
    schema_feedback: Sequence[str] = (),
    review_feedback: Sequence[str] = (),
) -> SynthesisRequest:
    """:meth:`MethodSynthesizer.build_request` for a caller that holds no synthesiser."""

    return MethodSynthesizer(registry, catalog).build_request(
        goal,
        capabilities,
        registry,
        reports=reports,
        mission_id=mission_id,
        domain=domain,
        schema_feedback=schema_feedback,
        review_feedback=review_feedback,
    )


def accept_response(
    text: str,
    *,
    registry: MethodRegistry,
    catalog: TaskTypeCatalog,
    policy: AdmissionPolicy,
    author_override: RegistryAuthor | None = None,
) -> AdmissionReceipt:
    """:meth:`MethodSynthesizer.accept_response` for a caller that holds no synthesiser."""

    return MethodSynthesizer(registry, catalog).accept_response(
        text, policy=policy, author_override=author_override
    )


__all__ = (
    "CORRECTABLE_REJECTIONS",
    "DEFAULT_MAX_OPERATORS",
    "METHOD_SHAPE",
    "NON_CORRECTABLE_REJECTIONS",
    "SYNTHESIS_AUTHOR",
    "SYNTHESIS_TERMINAL_STATUSES",
    "VALUE_CONTAINERS",
    "ApplicabilityNote",
    "MethodSynthesizer",
    "OperatorOffer",
    "SynthesisReplyUnreadable",
    "SynthesisRequest",
    "accept_response",
    "authority_claims",
    "build_request",
    "rejection_is_correctable",
    "rejection_problems",
    "synthesis_rejection_feedback",
    "synthesis_schema_feedback",
)
