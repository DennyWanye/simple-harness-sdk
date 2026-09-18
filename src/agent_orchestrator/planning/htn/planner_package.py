# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2: the context package a **hierarchical** Planner is actually given.

P2.3b's journal recorded this as blocker (c): the prompt selection did not look at
``is_hierarchical`` and ``build_planner_package`` kept producing the legacy DAG
package.  A Planner told to answer with ``<plan_revision_proposal>`` while being
handed a package about ``budget_for_tasks`` and ``workspace_files`` has nothing to
propose *with* — it does not know which methods are registered, which goals are open,
which plan revision it is answering against, or why the methods it can see were
refused — so every round came back ``proposal_unreadable`` under a real model.

This module builds the other package.  It is a *new* function beside the legacy one
rather than a branch inside it (§18.5 rule 1): ``build_planner_package`` keeps its
exact bytes, so a legacy Mission's request hash does not move.

What the package carries, and why each part is load-bearing:

``plan``
    the current revision and the open compound goals, each with its obligation, its
    goal signature and its bound parameters.  The Planner's ``refine`` operation names
    a ``goal_id`` and an ``obligation_id``; without this section it would be guessing
    both.
``method_library``
    every method the registry holds for those goal signatures, with the exact
    ``method_ref`` triple (id / version / content_hash) the operation must quote.  The
    triple is the whole point: §18.5 forbids the model to invent a version or a hash,
    and a model that is not *shown* them can only invent them.
``applicability``
    why an applicable-looking method was refused, split by axis (preconditions,
    parameters, capabilities, authority).  This is the input the MethodSynthesizer
    needs to be worth calling at all — "no method fits" is not actionable, "no method
    fits because none of them declares the ``code.run-tests`` capability this
    deployment has" is.
``operators``
    the capabilities this deployment really registered, and the ones a method could
    ask for and not get.  A method proposal that requires a capability nobody runs is
    refused at admission, so telling the Planner in advance is the difference between
    one wasted round and none.

Nothing here decides anything.  Every value is read from the store, the registry and
the deployment, and the Planner's answer still goes through ``parse_plan_proposal`` →
``assess_method`` → ``compile_refinement_bundle`` → ``commit_plan_revision`` before a
single row moves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...contracts.htn import OccurrenceId, ReadItemKind, TaskForm
from ...contracts.models import ContractError
from ...contracts.planning_decisions import (
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
)
from ...contracts.semantic_base import content_hash_of
from ...graph.task_network import TaskNetworkSnapshot

#: Bumped when the shape of the hierarchical package changes.  Separate from the
#: legacy ``PACKAGE_VERSION`` so a change to one never moves the other's request hash.
#: v3 (P2.3c part 2c) adds the ``facts`` section and makes ``applicability`` read the
#: fields an :class:`~.applicability.ApplicabilityReport` actually has.
#: v4 (P2.3j) adds ``rejected_refinements`` — the occurrences whose adopted method the
#: root review rejected, with the instance to retire and the findings — computes
#: ``method_library`` / ``applicability`` for those occurrences too, and flags each
#: library entry ``rejected_by_root_review``.
HIERARCHICAL_PACKAGE_VERSION = "planner-package-hierarchical-v4"

#: H1-D (V2 §38, addendum §7.1): the in-package string label of the package built
#: when the caller explicitly selects ``planning_protocol="planning-decision-v1"``.
#: A separate constant, because the integer pairing version and this string are the
#: two values a legacy request hash is computed over — and the legacy path must keep
#: producing ``HIERARCHICAL_PACKAGE_VERSION`` byte for byte.
HIERARCHICAL_DECISION_PACKAGE_VERSION = "planner-package-hierarchical-v5"

#: The output block the decision protocol asks for; the legacy block is unchanged.
DECISION_OUTPUT_CONTRACT = "<planning_decision>{json}</planning_decision>"

#: V2 §48 / §18: how many reference quadruples one package exposes.  A bound, for the
#: same reason every other cap here exists — the package is a prompt — and a
#: *deterministic* one: the surviving refs are the sorted prefix, so two builds of the
#: same request carry the same ones and the request binding can be recomputed.
MAX_VISIBLE_REFS = 128

#: §16 decision limits, spelled with the contract constants so the package and the
#: codec can never disagree about a number.
DECISION_LIMITS: Mapping[str, int] = {
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

#: How many method definitions one package lists per goal signature.  A bound, because
#: the package is a prompt: a registry with two hundred methods for one signature would
#: push the open goals out of the model's attention long before it ran out of context.
MAX_METHODS_PER_SIGNATURE = 12

#: How many refused-applicability reports one package carries.  Same reason.
MAX_APPLICABILITY_REPORTS = 12

#: How many recorded observations one package quotes.  Same reason again, and one
#: more: the section exists so the Planner can *cite* a fact, not so it can browse
#: the Mission's whole evidence history.
MAX_FACTS = 24


def open_goals(network: TaskNetworkSnapshot) -> tuple[dict[str, Any], ...]:
    """The compound occurrences that still have no adopted method.

    These are exactly the goals a ``refine`` operation may name.  A compound that is
    already refined is *not* listed: proposing a second adopted method for one
    occurrence is refused by the network contract ("alternatives are OR, not AND"),
    and offering it as a choice would invite a round that cannot be committed.
    """

    goals: list[dict[str, Any]] = []
    for spec in network.occurrences:
        if spec.form is not TaskForm.COMPOUND:
            continue
        if network.adopted_instance_for(spec.occurrence_id) is not None:
            continue
        binding = network.binding_for_occurrence(spec.occurrence_id)
        goals.append(
            {
                "occurrence_id": str(spec.occurrence_id),
                "goal_id": str(spec.task_id),
                "obligation_id": str(spec.obligation_id),
                "requiredness": str(spec.requiredness),
                "goal_signature_id": str(binding.goal_signature.signature_id),
                "statement": binding.goal_signature.statement,
                "typed_parameters": dict(binding.typed_parameters),
                "requirement_refs": list(binding.requirement_refs),
                "contract_revision": int(binding.contract_revision),
                "capability_requirements": sorted(
                    str(item) for item in binding.capability_requirements
                ),
            }
        )
    return tuple(sorted(goals, key=lambda item: item["occurrence_id"]))


def pending_primitives(network: TaskNetworkSnapshot) -> tuple[dict[str, Any], ...]:
    """The primitive occurrences on the board, so the Planner does not re-plan them.

    A Planner that cannot see the work it already committed proposes it again; the
    commit then refuses the round for a structural reason and the model is told
    nothing useful about what it did wrong.
    """

    return tuple(
        {
            "occurrence_id": str(spec.occurrence_id),
            "task_id": str(spec.task_id),
            "obligation_id": str(spec.obligation_id),
            "requiredness": str(spec.requiredness),
        }
        for spec in sorted(network.occurrences, key=lambda item: str(item.occurrence_id))
        if spec.form is TaskForm.PRIMITIVE
    )


def method_library(
    registry: Any,
    signatures: Sequence[str],
    *,
    limit: int = MAX_METHODS_PER_SIGNATURE,
    rejected_refs: Sequence[Any] = (),
    read_only_rejected_refs: Sequence[Any] = (),
) -> tuple[dict[str, Any], ...]:
    """The methods this deployment holds for the open goals' signatures.

    Every entry carries the ``method_ref`` triple verbatim, because that triple is
    what a ``refine`` operation has to quote and §18.5 refuses a model-invented
    version or hash.  ``registry_status`` is shown and stated as read-only: a
    ``TRIAL_ADMITTED`` method is offered *and* labelled, so the Planner can prefer a
    promoted one without the package having to hide the other.

    P2.3j: ``rejected_refs`` are the methods whose adopted instance the root review
    rejected on this plan (see :func:`rejected_refinements`).  They are still listed
    — hiding them would leave the Planner unable to say why the obvious method is not
    an option — and flagged ``rejected_by_root_review`` so the reason is in the same
    row as the triple.

    P2.3q / N12: ``read_only_rejected_refs`` are the methods a read-only leaf cancel
    retired (``read_only_leaf_needs_write``).  They used to share the root-review
    flag, so C1-r1's Planner wrote "rejected_by_root_review blocks reuse" on a
    Mission that never had a root review.  Two fields, two reasons.
    """

    rejected = {_ref_key(item) for item in rejected_refs}
    read_only_rejected = {_ref_key(item) for item in read_only_rejected_refs}
    entries: list[dict[str, Any]] = []
    for signature in sorted({str(item) for item in signatures}):
        found = _methods_for(registry, signature)
        for contract in list(found)[: max(0, limit)]:
            reference = contract.method_ref()
            entries.append(
                {
                    "goal_signature_id": signature,
                    "method_ref": reference.to_json(),
                    "rejected_by_root_review": _ref_key(reference) in rejected,
                    "rejected_by_read_only_leaf": _ref_key(reference) in read_only_rejected,
                    # P2.3c part 2b: the *same* triple again, spelled the way a
                    # ``refine`` operation has to spell it.  ``MethodRef.to_json``
                    # writes ``method_id`` and the proposal codec reads ``id``, so a
                    # package that showed only the first shape asked the model to
                    # re-key a hash by hand — which is exactly the invention §18.5
                    # forbids, and exactly what the first real-model round did wrong.
                    "refine_method_ref": {
                        "id": str(contract.method_id),
                        "version": int(contract.method_version),
                        "content_hash": str(reference.content_hash),
                    },
                    "method_id": str(contract.method_id),
                    "parameter_schema_ref": _ref_id(contract.parameter_schema_ref),
                    # ``MethodStep`` names them ``local_id`` and ``task_type_ref``;
                    # reading ``step_key`` / ``goal_type_ref`` produced one empty
                    # entry per step, so the package said "this method has four
                    # anonymous steps" and the model had nothing to reason about.
                    "steps": [
                        {
                            "step": str(getattr(item, "local_id", "")),
                            "form": str(getattr(item, "form", "")),
                            "task_type_ref": _ref_id(getattr(item, "task_type_ref", None)),
                            "required_capabilities": sorted(
                                str(one) for one in getattr(item, "required_capabilities", ())
                            ),
                        }
                        for item in getattr(contract, "steps", ())
                    ],
                    "required_capabilities": sorted(
                        str(item) for item in getattr(contract, "required_capabilities", ())
                    ),
                    "registry_status": str(_status(registry, contract)),
                }
            )
    return tuple(entries)


#: How many reviewer findings one ``rejected_refinements`` entry quotes, and how long
#: each may be.  The findings are the reviewer's words and the package is a prompt.
MAX_REJECTION_FINDINGS = 8
MAX_FINDING_CHARS = 1200


def rejected_refinements(
    network: TaskNetworkSnapshot, rejected: Sequence[Any]
) -> tuple[dict[str, Any], ...]:
    """The occurrences whose adopted method the root review rejected (P2.3j).

    The repair round exists to answer these, and before this section it could not
    even name them: ``open_goals`` lists only *unrefined* compounds, and a rejected
    root is refined — by the instance being rejected.  Each entry carries what a
    replacement proposal has to quote (``goal_id`` / ``obligation_id`` for the
    ``refine``, ``rejected_method_instance_id`` for the ``retire_method``), the
    goal's parameters and requirements as ``open_goals`` would show them, and the
    reviewer's findings verbatim, bounded.  Nothing here is a judgment of this
    package: the rejection is the review's, the instance is the plan's.
    """

    entries: list[dict[str, Any]] = []
    for item in rejected:
        occurrence = OccurrenceId(str(item.occurrence_id))
        try:
            spec = network.occurrence(occurrence)
            binding = network.binding_for_occurrence(occurrence)
        except KeyError:
            continue
        rendered = item.to_json()
        findings = [
            {
                **{key: value for key, value in dict(finding).items() if key != "detail"},
                "detail": str(finding.get("detail", ""))[:MAX_FINDING_CHARS],
            }
            for finding in list(rendered.get("findings", ()))[:MAX_REJECTION_FINDINGS]
        ]
        entries.append(
            {
                **rendered,
                "findings": findings,
                "requiredness": str(spec.requiredness),
                "statement": binding.goal_signature.statement,
                "typed_parameters": dict(binding.typed_parameters),
                "requirement_refs": list(binding.requirement_refs),
                "contract_revision": int(binding.contract_revision),
            }
        )
    return tuple(sorted(entries, key=lambda entry: str(entry["occurrence_id"])))


def _ref_key(reference: Any) -> tuple[str, int, str]:
    to_json = getattr(reference, "to_json", None)
    data = to_json() if callable(to_json) else dict(reference or {})
    return (
        str(data.get("method_id", data.get("id", ""))),
        int(data.get("version", 0) or 0),
        str(data.get("content_hash", "")),
    )


@dataclass(frozen=True, slots=True)
class MethodApplicability:
    """One ``assess_method`` verdict, with the goal and method it is about.

    :class:`~.applicability.ApplicabilityReport` carries the verdict and nothing that
    says *whose* verdict it is — ``assess_method`` is called with the goal and the
    method as arguments — so the two identities travel beside it rather than being
    guessed from the report.
    """

    goal_occurrence_id: str
    goal_signature_id: str
    method_ref: Any
    report: Any


def applicability_reports(
    reports: Sequence[MethodApplicability], *, limit: int = MAX_APPLICABILITY_REPORTS
) -> tuple[dict[str, Any], ...]:
    """Why the methods that *looked* applicable were not.

    Four axes kept apart, because they are four different repairs: a missing
    precondition is waited for or observed, a parameter mismatch is re-bound, a
    missing capability needs a different method, and a missing authority needs a
    person.  Merging them into "not applicable" is what made the round unactionable.

    P2.3c part 2c (review F16): this used to read ``unmet_preconditions`` /
    ``parameter_problems`` / ``missing_capabilities`` / ``missing_authority`` off the
    report with ``getattr`` defaults.  ``ApplicabilityReport`` has none of those
    names — its fields are ``needs_evidence``, ``conflicts``, ``type_errors``,
    ``unmet_capabilities`` and ``authorization`` — so every axis came back empty and
    the section the module docstring calls load-bearing was four empty lists under a
    verdict string.  Nothing caught it because no test ever put a real report through
    here.  The names are the report's own now.
    """

    out: list[dict[str, Any]] = []
    for entry in list(reports)[: max(0, limit)]:
        report = entry.report
        gate = getattr(report, "authorization", None)
        out.append(
            {
                "goal_occurrence_id": str(entry.goal_occurrence_id),
                "goal_signature_id": str(entry.goal_signature_id),
                "method_ref": _ref_json(entry.method_ref),
                "verdict": str(getattr(report, "status", "")),
                "precondition_truth": str(getattr(report, "truth", "")),
                # The preconditions nobody has looked at yet.  These are the
                # propositions an evidence round would observe, and the reason the
                # ``facts`` section below exists.
                "unknown_preconditions": [
                    str(item) for item in getattr(report, "needs_evidence", ())
                ],
                "conflicting_preconditions": [
                    str(item) for item in getattr(report, "conflicts", ())
                ],
                "parameter_problems": [str(item) for item in getattr(report, "type_errors", ())],
                "missing_capabilities": [
                    str(item) for item in getattr(report, "unmet_capabilities", ())
                ],
                # Authority is a *gate decision*, not a list: it is refused with a
                # reason or it is not refused at all.
                "missing_authority": (
                    []
                    if gate is None or bool(getattr(gate, "allowed", False))
                    else [str(getattr(gate, "reason", "authorisation refused"))]
                ),
            }
        )
    return tuple(out)


#: Everything one ``facts`` entry may say.  Review P2-12: the section exists to hand
#: the Planner *references* — what was observed, by whom, when, and the read-set entry
#: that cites it — and never a conclusion drawn from them.  A judgement ("this holds",
#: "this method is applicable") computed here would be this package deciding the very
#: question the refinement round decides, with no record that it did.  The guard is
#: structural because the failure is: nobody notices a field being added.
FACT_ENTRY_FIELDS: frozenset[str] = frozenset(
    {
        "proposition_key",
        "polarity",
        "coverage",
        "observer_id",
        "observed_at_ms",
        "read_set_entry",
    }
)


def refuse_fact_inference(entries: Sequence[Mapping[str, Any]]) -> None:
    """Refuse a ``facts`` section that carries anything but references (P2-12)."""

    for entry in entries:
        extra = sorted(set(entry) - FACT_ENTRY_FIELDS)
        if extra:
            raise ContractError(
                f"the planner package's facts section may only reference what was observed; "
                f"{extra} would hand the model an inference this package is not entitled to "
                f"make (allowed: {sorted(FACT_ENTRY_FIELDS)})"
            )


def recorded_facts(
    observations: Sequence[Any],
    *,
    limit: int = MAX_FACTS,
    read_item: Any = None,
) -> tuple[dict[str, Any], ...]:
    """The observations this Mission has recorded, in the shape a ``read_set`` wants.

    P2.3c part 2c.  The real-model smoke got as far as a readable
    ``<plan_revision_proposal>`` and was then refused with ``READ_SET_UNRESOLVED``:
    the model had written ``kind=fact`` entries in its read-set, and the package had
    never shown it a single observation id, so the ids it wrote were invented and the
    library could not re-check them.  Telling the model harder not to invent them is
    the wrong repair — a model that is not *shown* an identifier can only make one up,
    which is the same reasoning that put ``refine_method_ref`` in ``method_library``.

    So each entry carries a ready-made ``read_set_entry``: the observation's id, the
    semantic revision and the content hash the checker recomputes
    (``_read_set.ReadSetChecker.observation_state``).  Copy it, do not derive it.

    Review P2-13: **who computes that entry** is the checker, when a caller hands one
    over.  ``ReadSetChecker.read_item``'s own docstring says the proposing side and
    the checking side must agree on what a semantic revision is and that writing the
    formula twice is how they stop agreeing — and this function was the second place
    it was written.  ``read_item`` is therefore a callable ``(ReadItemKind, id) ->
    ReadItem``; the literal below is the offline form, used only when no checker is
    available (a package rendered without a store behind it).

    Two deliberate limits:

    * **Only the newest observation per proposition.**  An earlier record for the
      same proposition is *superseded*, and the read-set checker reports exactly that
      — so offering it would be handing the Planner an entry guaranteed to refuse the
      commit.
    * **No predicate statement.**  ``ObservationRecord`` keys a fact by
      ``proposition_key``, a digest of the signature and the grounded arguments, and
      the record does not carry the signature itself.  Rather than reconstruct a name
      the store does not hold, the fact is quoted by its key and the *names* stay in
      ``applicability``, where the unknown preconditions are listed.
    """

    newest: dict[str, Any] = {}
    for record in observations:
        key = str(getattr(record, "proposition_key", ""))
        if not key:
            continue
        newest[key] = record
    entries: list[dict[str, Any]] = []
    for key in sorted(newest):
        record = newest[key]
        identity = str(getattr(record, "observation_id", ""))
        if read_item is not None:
            quoted = read_item(ReadItemKind.FACT, identity).to_json()
        else:
            to_json = getattr(record, "to_json", None)
            quoted = {
                "kind": "fact",
                "id": identity,
                "semantic_revision": 1,
                "content_hash": content_hash_of(to_json()) if callable(to_json) else "",
            }
        entries.append(
            {
                "proposition_key": key,
                "polarity": bool(getattr(record, "polarity", False)),
                "coverage": str(getattr(record, "coverage", "")),
                "observer_id": getattr(record, "observer_id", None),
                "observed_at_ms": int(getattr(record, "observed_at_ms", 0)),
                "read_set_entry": quoted,
            }
        )
    chosen = tuple(entries[: max(0, limit)])
    refuse_fact_inference(chosen)
    return chosen


# ------------------------------------------------------------------ decision protocol
# H1-D (V2 §18/§19/§38/§39, ruling addendum §1/§7).  Everything below is a *pure*
# reader of the package that already exists, or a canonical-JSON hash of one of its
# sections.  Nothing here decides anything, writes anything or changes what the
# legacy package carries: the collector is what H1-F's admission layer re-runs to
# answer "is this ref something this request showed the model", and the three hash
# helpers are what H1-F's request binding is computed over.


def _ref_hash(kind: str, id: str, semantic_revision: int) -> str:
    """The hash a ref gets when the package carries no authoritative one (§5.1).

    The table says "no existing hash → sha256 of the object's canonical JSON".  The
    object a *reference* is derived from has exactly three stable parts, so hashing
    them — rather than the whole package entry they happened to sit in — keeps the
    ref recomputable from the ref alone, which is what a request binding needs.
    """

    return content_hash_of(
        {"kind": kind, "id": id, "semantic_revision": int(semantic_revision)}
    )


def _one_ref(
    kind: str, id: object, semantic_revision: object, content_hash: object = None
) -> dict[str, Any] | None:
    """One reference quadruple, or ``None`` when the source is not usable.

    A reference the package cannot state correctly is *skipped*, never invented: the
    alternative — synthesising an id, or overwriting an authoritative hash with a
    guess — is exactly the "model invented a version" failure §18.5 exists to refuse.
    Two cases are deliberately kept apart:

    * **no hash at all** (§5.1: "no existing hash → sha256 of the object's canonical
      JSON").  Some kinds — an obligation, a task whose entry carries no
      ``contract_hash`` — genuinely have no authoritative digest in the package, so
      one is *derived* from the ref's own three stable parts.
    * **a hash that is present but malformed** is not repaired, because a method ref
      whose digest disagrees with ``method_contracts`` would be refused at commit
      time; the ref is dropped instead of shown wrong.

    ``PlanningRefV1`` is the validator, so the shape returned here is exactly the
    shape H1-F's admission layer re-checks the model's answer against.
    """

    if id is None or str(id) == "":
        return None
    if isinstance(semantic_revision, bool) or semantic_revision is None:
        return None
    try:
        revision = int(semantic_revision)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if revision < 1:
        revision = 1
    digest = content_hash
    if digest is None or digest == "":
        digest = _ref_hash(str(kind), str(id), revision)
    try:
        PlanningRefV1(
            kind=PlanningRefKind(str(kind)),
            id=str(id),
            semantic_revision=revision,
            content_hash=digest,
        )
    except (ContractError, ValueError):
        return None
    return {
        "kind": str(kind),
        "id": str(id),
        "semantic_revision": revision,
        "content_hash": str(digest),
    }


def _method_ref(value: Any) -> dict[str, Any] | None:
    """A ``{id, version, content_hash}`` method triple, however the package spells it."""

    to_json = getattr(value, "to_json", None)
    data = to_json() if callable(to_json) else value
    if not isinstance(data, Mapping):
        return None
    method_id = data.get("id", data.get("method_id"))
    version = data.get("version", data.get("semantic_revision"))
    if method_id is None or version is None:
        return None
    return _one_ref(PlanningRefKind.METHOD.value, method_id, version, data.get("content_hash"))


def _task_ref(entry: Mapping[str, Any], id_key: str, revision_key: str) -> list[dict[str, Any]]:
    """The task and the obligation a plan entry names, with their revisions (§5.1)."""

    out: list[dict[str, Any]] = []
    task_id = entry.get(id_key)
    revision = entry.get(revision_key, 1)
    if task_id:
        ref = _one_ref(
            PlanningRefKind.TASK.value, task_id, revision, entry.get("contract_hash")
        )
        if ref is not None:
            out.append(ref)
    obligation_id = entry.get("obligation_id")
    if obligation_id:
        ref = _one_ref(PlanningRefKind.OBLIGATION.value, obligation_id, 1, None)
        if ref is not None:
            out.append(ref)
    return out


def _read_set_ref(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``facts[].read_set_entry`` as an ``observation`` (V2 §17: no ``fact`` kind)."""

    quoted = entry.get("read_set_entry")
    if not isinstance(quoted, Mapping):
        return None
    # §17: a planning fact *is* an observation.  The package's own entry says
    # kind=fact (the read-set wire kind); the planning ref kind is observation.
    return _one_ref(
        PlanningRefKind.OBSERVATION.value,
        quoted.get("id"),
        quoted.get("semantic_revision", 1),
        quoted.get("content_hash"),
    )


def _method_instance_ref(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """The method *instance* a rejected refinement named, when the wire kind exists.

    The addendum §1 makes ``method_instance`` the sixteenth ``PlanningRefKind``,
    taken from ``method_instances`` (id ``instance_id``, revision ``plan_revision``,
    hash ``parameters_digest``).  That enum member belongs to the H1-A envelope
    slice, not to this module's whitelist, so on a build that does not yet carry it
    this returns ``None`` and the caller falls back to the method ref the entry also
    names — the reference stays *correct*, just coarser, and no version or hash is
    invented.  As soon as the member exists the same pure function starts emitting
    the finer ref with no change here.
    """

    instance_id = entry.get("rejected_method_instance_id")
    if instance_id is None:
        return None
    kind = getattr(PlanningRefKind, "METHOD_INSTANCE", None)
    if kind is None:
        return None
    return _one_ref(
        getattr(kind, "value", kind),
        instance_id,
        entry.get("plan_revision", 1),
        entry.get("parameters_digest"),
    )


def _accepted_ref(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    goal = entry.get("acceptance_ref", entry.get("resolution_ref"))
    if not isinstance(goal, Mapping):
        return None
    return _one_ref(
        PlanningRefKind.ACCEPTANCE.value,
        goal.get("id"),
        goal.get("semantic_revision", goal.get("revision", 1)),
        goal.get("content_hash"),
    )


def _collect_refs(package: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every reference the package's *own* sections already show, in §17 shape."""

    collected: list[dict[str, Any]] = []

    plan = package.get("plan")
    plan = plan if isinstance(plan, Mapping) else {}
    for entry in plan.get("open_compound_goals", ()):  # type: ignore[union-attr]
        if isinstance(entry, Mapping):
            collected.extend(_task_ref(entry, "goal_id", "contract_revision"))
    for entry in plan.get("committed_primitives", ()):  # type: ignore[union-attr]
        if isinstance(entry, Mapping):
            collected.extend(_task_ref(entry, "task_id", "contract_revision"))

    for entry in package.get("method_library", ()):
        if not isinstance(entry, Mapping):
            continue
        ref = _method_ref(entry.get("refine_method_ref", entry.get("method_ref")))
        if ref is not None:
            collected.append(ref)
    for entry in package.get("applicability", ()):
        if isinstance(entry, Mapping):
            ref = _method_ref(entry.get("method_ref"))
            if ref is not None:
                collected.append(ref)
    for entry in package.get("rejected_refinements", ()):
        if not isinstance(entry, Mapping):
            continue
        instance = _method_instance_ref(entry)
        if instance is not None:
            collected.append(instance)
        ref = _method_ref(entry.get("rejected_method_ref", entry.get("method_ref")))
        if ref is not None:
            collected.append(ref)
    for entry in package.get("facts", ()):
        if isinstance(entry, Mapping):
            ref = _read_set_ref(entry)
            if ref is not None:
                collected.append(ref)
    for entry in package.get("accepted_results", ()):
        if isinstance(entry, Mapping):
            ref = _accepted_ref(entry)
            if ref is not None:
                collected.append(ref)
    return collected


def _ref_sort_key(ref: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(ref["kind"]),
        str(ref["id"]),
        int(ref["semantic_revision"]),
        str(ref["content_hash"]),
    )


def _sorted_unique_refs(package: Mapping[str, Any]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, int, str]] = set()
    unique: list[dict[str, Any]] = []
    for ref in _collect_refs(package):
        key = _ref_sort_key(ref)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    unique.sort(key=_ref_sort_key)
    return unique


def visible_refs_from_hierarchical_package(
    package: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """The §18 reference quadruples a package exposes to the model.

    A pure function of what the package *already* carries — method library,
    applicability, open goals, committed primitives, recorded facts, rejected
    refinements and (from H2) accepted results.  Deduped on the §17 quadruple and
    sorted by ``(kind, id, semantic_revision, content_hash)``, then capped at
    :data:`MAX_VISIBLE_REFS`; the count dropped by the cap is reported separately by
    :func:`visible_refs_omitted` so the request binding can pin both.
    """

    return tuple(_sorted_unique_refs(package)[:MAX_VISIBLE_REFS])


def visible_refs_omitted(package: Mapping[str, Any]) -> int:
    """How many references the :data:`MAX_VISIBLE_REFS` cap dropped (§38 truncation)."""

    return max(0, len(_sorted_unique_refs(package)) - MAX_VISIBLE_REFS)


def planning_subjects(network: TaskNetworkSnapshot) -> tuple[dict[str, Any], ...]:
    """The §19 subject bindings: one per occurrence, unique and recomputable.

    ``subject_key`` is derived from the occurrence and its semantic binding, not
    handed out as a counter, so the same request computed twice yields the same
    keys and the request binding (§34 ``subject_bindings_hash``) can be recomputed
    by whoever checks the reply.  The key is scoped to this one PlannerRequest; the
    model only ever copies it back.
    """

    subjects: list[dict[str, Any]] = []
    for spec in network.occurrences:
        binding = network.binding_for_occurrence(spec.occurrence_id)
        material = {
            "occurrence_id": str(spec.occurrence_id),
            "task_id": str(spec.task_id),
            "obligation_id": str(spec.obligation_id),
            "contract_revision": int(binding.contract_revision),
        }
        subjects.append(
            {
                "subject_key": "subject-" + content_hash_of(material)[:32],
                **material,
            }
        )
    return tuple(sorted(subjects, key=lambda item: item["subject_key"]))


def _as_json_refs(values: object) -> list[Any]:
    rows: list[Any] = []
    for item in values if isinstance(values, Sequence) else ():  # type: ignore[arg-type]
        to_json = getattr(item, "to_json", None)
        rows.append(to_json() if callable(to_json) else item)
    return rows


def visible_refs_digest(refs: object) -> str:
    """The canonical-JSON SHA-256 of a visible-ref list (§34 request binding)."""

    return content_hash_of(_as_json_refs(refs))


def subject_bindings_hash(subjects: object) -> str:
    """The canonical-JSON SHA-256 of a subject-binding list (§34 request binding)."""

    return content_hash_of(_as_json_refs(subjects))


def package_hash(package: Mapping[str, Any]) -> str:
    """The canonical-JSON SHA-256 of a whole package (§34 ``package_hash``).

    Canonical JSON sorts object keys, so two packages that differ only in the order
    their keys were inserted hash the same; array order *does* count, exactly as §15
    says.  A model never computes or submits this — the caller does, before sending.
    """

    return content_hash_of(package)


def _enabled_decision_types() -> list[str]:
    """The H1-executable decision kinds from ``contracts.planning_decisions`` (§12)."""

    return sorted(
        name for name, enablement in H1_DECISION_ENABLEMENT.items() if enablement.executable
    )


def _feedback_json(value: Any) -> dict[str, Any] | None:
    """The ``PlanningFeedbackV1`` a re-ask carries, or ``None`` for a first round.

    A mapping is accepted and *validated* through the contract rather than trusted:
    a malformed feedback object is a programming error on the calling side and must
    not reach the model as a half-formed field.
    """

    if value is None:
        return None
    if isinstance(value, PlanningFeedbackV1):
        feedback = value
    else:
        feedback = PlanningFeedbackV1.from_json(value, "previous_feedback")
    return feedback.to_json()


def _decision_fields(
    package: Mapping[str, Any],
    network: TaskNetworkSnapshot,
    previous_feedback: Any,
) -> dict[str, Any]:
    """The five decision-protocol fields, added on top of the legacy package (§38)."""

    refs = visible_refs_from_hierarchical_package(package)
    return {
        "planning_protocol": {
            "protocol": PLANNING_DECISION_V1,
            "enabled_decision_types": _enabled_decision_types(),
        },
        "planning_subjects": [dict(item) for item in planning_subjects(network)],
        "visible_refs": [dict(item) for item in refs],
        "visible_refs_omitted": visible_refs_omitted(package),
        "previous_feedback": _feedback_json(previous_feedback),
        "decision_limits": dict(DECISION_LIMITS),
    }


def hierarchical_planner_package(
    mission: Any,
    network: TaskNetworkSnapshot,
    *,
    registry: Any,
    capabilities: Sequence[str] = (),
    unavailable_capabilities: Sequence[str] = (),
    reports: Sequence[MethodApplicability] = (),
    observations: Sequence[Any] = (),
    attempt_ordinal: int = 1,
    rejected: Sequence[Mapping[str, Any]] = (),
    read_item: Any = None,
    rejected_refinements_of: Sequence[Any] = (),
    rejected_method_refs_of: Mapping[str, Sequence[Any]] | None = None,
    read_only_rejected_method_refs_of: Mapping[str, Sequence[Any]] | None = None,
    planning_protocol: str | None = None,
    previous_feedback: Any = None,
) -> dict[str, Any]:
    """The whole package, as a plain mapping the context builder can seal.

    Returned as data rather than as a rendered string so the caller keeps ownership of
    rendering and of the context hash — the legacy ``_seal`` already does both, and a
    second renderer here would be a second answer to "what did the model see".

    P2.3j: ``rejected_refinements_of`` are the
    :class:`~..orchestrator.hierarchical_dispatch.RejectedRefinement` records for
    this plan.  Their signatures join the open goals' for ``method_library``, so the
    repair round is shown the library for the goal it is about (H-L3-C1-r1 was shown
    an empty one), and their methods are flagged in it.  ``rejected_method_refs_of``
    (verification P1-1) is the history — every method the review rejected at each
    occurrence, on any revision — so a method rejected two revisions ago is still
    flagged, not offered afresh.

    H1-D: ``planning_protocol`` is the explicit opt-in for the decision shape
    (§38).  ``None`` — the default, and the only value a legacy Mission's caller
    passes — leaves the returned mapping *byte for byte* what it always was.
    Passing ``PLANNING_DECISION_V1`` adds the five fields §38 names, switches the
    output contract to ``<planning_decision>`` and the in-package label to
    ``planner-package-hierarchical-v5``; ``previous_feedback`` is the caller's
    ``PlanningFeedbackV1`` for a re-ask, or ``None`` for a first round.  An unknown
    protocol name is a programming error and raises rather than silently falling
    back to the legacy shape.
    """

    goals = open_goals(network)
    struck: list[Any] = [
        item.method_ref
        for item in rejected_refinements_of
        if str(getattr(item, "reason", "")) != "read_only_leaf_needs_write"
    ]
    for references in (rejected_method_refs_of or {}).values():
        struck.extend(references)
    read_only_struck: list[Any] = [
        item.method_ref
        for item in rejected_refinements_of
        if str(getattr(item, "reason", "")) == "read_only_leaf_needs_write"
    ]
    for references in (read_only_rejected_method_refs_of or {}).values():
        read_only_struck.extend(references)
    if planning_protocol not in (None, PLANNING_DECISION_V1):
        raise ContractError(
            f"planning_protocol must be {PLANNING_DECISION_V1!r} or None, "
            f"not {planning_protocol!r}"
        )
    replaced = rejected_refinements(network, rejected_refinements_of)
    signatures = [item["goal_signature_id"] for item in goals] + [
        item["goal_signature_id"] for item in replaced
    ]
    package: dict[str, Any] = {
        "role": "planner",
        "mode": "hierarchical",
        "mission": {
            "mission_id": mission.id,
            "goal": mission.goal,
            "success_criteria": list(mission.success_criteria),
            "allowed_tools": list(mission.allowed_tools),
            "budget": mission.budget.to_json(),
            "risk_level": mission.risk_level,
        },
        "planning_attempt": int(attempt_ordinal),
        "plan": {
            "plan_revision": int(network.plan_revision),
            "root_occurrences": [str(item) for item in network.root_occurrence_ids],
            "open_compound_goals": [dict(item) for item in goals],
            "committed_primitives": [dict(item) for item in pending_primitives(network)],
            "required_obligations": sorted(str(item) for item in network.required_obligations),
        },
        "method_library": [
            dict(item)
            for item in method_library(
                registry,
                signatures,
                rejected_refs=struck,
                read_only_rejected_refs=read_only_struck,
            )
        ],
        "applicability": [dict(item) for item in applicability_reports(reports)],
        # P2.3j: the goals whose adopted method the root review rejected.  A repair
        # proposal names ``rejected_method_instance_id`` in a ``retire_method`` and
        # ``goal_id`` / ``obligation_id`` in the ``refine`` that replaces it.
        "rejected_refinements": [dict(item) for item in replaced],
        # Every observation this Mission has recorded, each with the read-set entry
        # that cites it verbatim.  See :func:`recorded_facts`: a Planner that is shown
        # no observation id can only invent one, and an invented id is
        # ``READ_SET_UNRESOLVED``.
        "facts": [dict(item) for item in recorded_facts(observations, read_item=read_item)],
        "operators": {
            "available_capabilities": sorted(str(item) for item in capabilities),
            "unavailable_capabilities": sorted(str(item) for item in unavailable_capabilities),
        },
        "planning_rejected": [dict(item) for item in rejected],
        "constraint": (
            "propose semantic operations on the current plan; quote expected_plan_revision "
            "exactly as given, copy a method_library entry's refine_method_ref into the "
            "operation's method_ref field verbatim (id / version / content_hash unchanged), "
            "and list in read_set every object you actually read (a stale read refuses the "
            "commit). A read_set entry must be copied from this package: a method entry from "
            "method_library.refine_method_ref (as kind=method) and a fact entry from "
            "facts[].read_set_entry unchanged. Do not write a read_set entry whose id does not "
            "appear in this package; if facts is empty, write no kind=fact entry at all. "
            "A rejected_refinements entry is repaired by ONE proposal carrying a retire_method "
            "of its rejected_method_instance_id together with a refine of the same goal_id / "
            "obligation_id using a method_library entry whose rejected_by_root_review is false "
            "and whose rejected_by_read_only_leaf is false and whose applicability verdict is "
            "APPLICABLE (or which applicability does not list as a refusal); a newly admitted "
            "synthesised method is such an entry. rejected_by_root_review marks a MISSION_FINAL "
            "rejection; rejected_by_read_only_leaf marks a read-only leaf cancel "
            "(read_only_leaf_needs_write) — they are different reasons and both block reuse. "
            "If every unrejected library entry is listed as a refusal, answer no_applicable_method"
        ),
        "output_contract": (
            DECISION_OUTPUT_CONTRACT
            if planning_protocol == PLANNING_DECISION_V1
            else "<plan_revision_proposal>{json}</plan_revision_proposal>"
        ),
        "package_version": (
            HIERARCHICAL_DECISION_PACKAGE_VERSION
            if planning_protocol == PLANNING_DECISION_V1
            else HIERARCHICAL_PACKAGE_VERSION
        ),
    }
    if planning_protocol == PLANNING_DECISION_V1:
        # The five §38 fields go *on top of* the legacy ones — the plan, method
        # library, applicability, facts, operators and rejected refinements all stay.
        package.update(_decision_fields(package, network, previous_feedback))
    return package


# ------------------------------------------------------------------ registry probing
# The registry is a Protocol in ``hierarchical_dispatch`` and a concrete class in
# ``planning/htn/registry``; deployments bring their own.  These readers ask for the
# richer interface and fall back to the narrower one rather than requiring every
# deployment to grow a method this package happens to want.


def _methods_for(registry: Any, signature: str) -> Sequence[Any]:
    """Every definition the registry holds whose goal type is ``signature``.

    ``MethodRegistry`` exposes ``method_refs()`` + ``definition(ref)``; a deployment
    that brings a richer index (``methods_for_signature``) is used directly.  Neither
    is required, because a deployment with no registry at all is a deployment whose
    Planner is simply shown no methods — which is a package that says "there is
    nothing to choose from", not a crash mid-prompt.
    """

    reader = getattr(registry, "methods_for_signature", None)
    if callable(reader):
        try:
            return tuple(reader(signature))
        except (KeyError, TypeError, ValueError):
            return ()
    refs = getattr(registry, "method_refs", None)
    definition = getattr(registry, "definition", None)
    if not callable(refs) or not callable(definition):
        return ()
    found: list[Any] = []
    try:
        for reference in refs():
            contract = definition(reference)
            if contract is None:
                continue
            if str(getattr(contract.goal_type_ref, "id", "")) == signature:
                found.append(contract)
    except (KeyError, TypeError, ValueError):
        return ()
    return tuple(found)


def _status(registry: Any, contract: Any) -> str:
    reader = getattr(registry, "registration", None)
    if not callable(reader):
        return "UNKNOWN"
    try:
        registration = reader(contract.method_ref())
    except (KeyError, TypeError, ValueError):
        return "UNKNOWN"
    return (
        "UNKNOWN"
        if registration is None
        else str(getattr(registration, "registry_status", "UNKNOWN"))
    )


def _ref_id(value: Any) -> str | None:
    return None if value is None else str(getattr(value, "id", value))


def _ref_json(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    to_json = getattr(value, "to_json", None)
    return to_json() if callable(to_json) else {"id": str(value)}


__all__ = (
    "DECISION_LIMITS",
    "DECISION_OUTPUT_CONTRACT",
    "HIERARCHICAL_DECISION_PACKAGE_VERSION",
    "HIERARCHICAL_PACKAGE_VERSION",
    "MAX_APPLICABILITY_REPORTS",
    "MAX_FACTS",
    "MAX_METHODS_PER_SIGNATURE",
    "MAX_REJECTION_FINDINGS",
    "MAX_VISIBLE_REFS",
    "MethodApplicability",
    "applicability_reports",
    "hierarchical_planner_package",
    "method_library",
    "open_goals",
    "package_hash",
    "pending_primitives",
    "planning_subjects",
    "rejected_refinements",
    "subject_bindings_hash",
    "visible_refs_digest",
    "visible_refs_from_hierarchical_package",
    "visible_refs_omitted",
    "FACT_ENTRY_FIELDS",
    "recorded_facts",
    "refuse_fact_inference",
)
