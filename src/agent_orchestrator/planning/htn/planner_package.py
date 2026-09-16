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

from ...contracts.htn import TaskForm
from ...contracts.semantic_base import content_hash_of
from ...graph.task_network import TaskNetworkSnapshot

#: Bumped when the shape of the hierarchical package changes.  Separate from the
#: legacy ``PACKAGE_VERSION`` so a change to one never moves the other's request hash.
#: v3 (P2.3c part 2c) adds the ``facts`` section and makes ``applicability`` read the
#: fields an :class:`~.applicability.ApplicabilityReport` actually has.
HIERARCHICAL_PACKAGE_VERSION = "planner-package-hierarchical-v3"

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
    registry: Any, signatures: Sequence[str], *, limit: int = MAX_METHODS_PER_SIGNATURE
) -> tuple[dict[str, Any], ...]:
    """The methods this deployment holds for the open goals' signatures.

    Every entry carries the ``method_ref`` triple verbatim, because that triple is
    what a ``refine`` operation has to quote and §18.5 refuses a model-invented
    version or hash.  ``registry_status`` is shown and stated as read-only: a
    ``TRIAL_ADMITTED`` method is offered *and* labelled, so the Planner can prefer a
    promoted one without the package having to hide the other.
    """

    entries: list[dict[str, Any]] = []
    for signature in sorted({str(item) for item in signatures}):
        found = _methods_for(registry, signature)
        for contract in list(found)[: max(0, limit)]:
            reference = contract.method_ref()
            entries.append(
                {
                    "goal_signature_id": signature,
                    "method_ref": reference.to_json(),
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


def recorded_facts(
    observations: Sequence[Any], *, limit: int = MAX_FACTS
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
        to_json = getattr(record, "to_json", None)
        digest = content_hash_of(to_json()) if callable(to_json) else ""
        entries.append(
            {
                "proposition_key": key,
                "polarity": bool(getattr(record, "polarity", False)),
                "coverage": str(getattr(record, "coverage", "")),
                "observer_id": getattr(record, "observer_id", None),
                "observed_at_ms": int(getattr(record, "observed_at_ms", 0)),
                "read_set_entry": {
                    "kind": "fact",
                    "id": str(getattr(record, "observation_id", "")),
                    "semantic_revision": 1,
                    "content_hash": digest,
                },
            }
        )
    return tuple(entries[: max(0, limit)])


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
) -> dict[str, Any]:
    """The whole package, as a plain mapping the context builder can seal.

    Returned as data rather than as a rendered string so the caller keeps ownership of
    rendering and of the context hash — the legacy ``_seal`` already does both, and a
    second renderer here would be a second answer to "what did the model see".
    """

    goals = open_goals(network)
    signatures = [item["goal_signature_id"] for item in goals]
    return {
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
        "method_library": [dict(item) for item in method_library(registry, signatures)],
        "applicability": [dict(item) for item in applicability_reports(reports)],
        # Every observation this Mission has recorded, each with the read-set entry
        # that cites it verbatim.  See :func:`recorded_facts`: a Planner that is shown
        # no observation id can only invent one, and an invented id is
        # ``READ_SET_UNRESOLVED``.
        "facts": [dict(item) for item in recorded_facts(observations)],
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
            "appear in this package; if facts is empty, write no kind=fact entry at all"
        ),
        "output_contract": "<plan_revision_proposal>{json}</plan_revision_proposal>",
        "package_version": HIERARCHICAL_PACKAGE_VERSION,
    }


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
    "HIERARCHICAL_PACKAGE_VERSION",
    "MAX_APPLICABILITY_REPORTS",
    "MAX_FACTS",
    "MAX_METHODS_PER_SIGNATURE",
    "MethodApplicability",
    "applicability_reports",
    "hierarchical_planner_package",
    "method_library",
    "open_goals",
    "pending_primitives",
    "recorded_facts",
)
