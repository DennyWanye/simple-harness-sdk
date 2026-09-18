# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2: the bridge from a plan **occurrence** to a dispatchable ``Task`` row.

P2.3b left the hierarchical mode unable to dispatch anything, and the reason was
narrow: ``commit_plan_revision`` writes memberships, method instances and semantic
bindings, but no legacy ``Task`` row.  ``Store.list_tasks`` therefore came back
empty, the legacy allocator saw no work and ``_decide`` returned at ``if not
tasks``.  This module is the missing half.

Three decisions are load-bearing, and each one is a decision *not* to be clever:

* **The Task contract is reused, not extended.**  An occurrence materialises an
  ordinary ``kind="work"`` Task whose meaning already lives in ``task_semantics``
  (§18.5: the semantic binding sits *beside* the Task, it does not replace it).
  Nothing about the hierarchy is encoded in the Task row, which is why the row can
  be rebuilt from the plan at any time.

* **``dependencies`` stay empty.**  It is tempting to project ORDER constraints onto
  ``Task.dependency_ids`` so the legacy allocator's READY arithmetic "just works".
  That would be wrong twice: the legacy rule is *conjunctive* (every dependency
  COMPLETED), so an OR method's alternatives would each block the other forever,
  and an ORDER edge released by ``order_released`` under a non-default
  ``ReleaseCondition`` is not the same fact as "the predecessor is COMPLETED".
  Ordering in this mode is answered by ``evaluate_readiness`` over the typed
  network, and encoding it in ``dependency_ids`` would put a second, disagreeing
  answer in the database (§18.5 hard constraint 4, §24.1 decision 6).

* **A compound gets a row too, and it is never dispatched.**  The row exists so a
  compound is *visible* — listings, the Manager's read, the operator UI — and it is
  refused at three independent gates: ``form=compound`` in
  ``legacy_ready_is_not_eligibility`` (the allocator's ``evaluate_frontier_v2`` and
  ``HierarchicalDispatch.intercept_worker_dispatch` both ask that one function), and
  ``admit_for_dispatch`` which only builds an ``EligiblePrimitiveTask`` for a
  primitive.  Its status is BLOCKED, never READY, so even a reader that mistakes the
  display index for a permission finds the answer "no".

The budget share is the other thing this module owns.  ``BudgetLedger.open_account``
refuses a child whose limits do not fit its parent, so the share has to be computed
rather than guessed: what is left of the Mission's task pool (its budget minus the
system reserve, minus what the existing rows already hold) is divided over the
occurrences this round funds *plus one share held back for every compound nobody has
refined yet*, and every other dimension the Mission bounds is inherited.  See
:func:`share_tokens` for why the divisor counts the unrefined compounds, and
:meth:`Materialisation.conservation` for the equation the suite checks each round.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import Budget, Mission, Task, TaskStatus
from ..contracts.htn import (
    OccurrenceSpec,
    Requiredness,
    SideEffectKind,
    TaskForm,
    TaskSemanticBindingV1,
)
from ..contracts.models import default_change_policy
from ..contracts.resolution import RequirementsRevision
from ..planning.manager import system_reserve_tokens

#: ``Task.context`` keys this bridge writes.  Namespaced so nothing in the legacy
#: path can collide with them, and readable so an operator can tell a materialised
#: occurrence from a Planner-drawn Task without joining another table.
CONTEXT_OCCURRENCE = "occurrence_id"
CONTEXT_PLAN_REVISION = "plan_revision"
CONTEXT_FORM = "form"
CONTEXT_REQUIREDNESS = "requiredness"
CONTEXT_MATERIALISED_BY = "materialised_by"

#: What ``context[CONTEXT_MATERIALISED_BY]`` says.  A version string rather than a
#: boolean: when the shape of a materialised row changes, a reader has to be able to
#: tell which rule produced the row it is looking at.
MATERIALISER = "occurrence-tasks-v1"

#: The smallest token share a materialised *primitive* occurrence may be opened with.
#: Zero is not "unbounded", it is "cannot pay for anything", and an account that can
#: never reserve is a Task that can never run — so a pool that cannot give every new
#: primitive this much is a refusal (``BUDGET_INSUFFICIENT``), never a row rounded
#: down to an account nobody can draw on.
MIN_TOKEN_SHARE = 1

#: What a compound's account is opened with.  A compound is refined and never
#: dispatched, so "may spend nothing" is the true statement about it rather than a
#: rounding artefact — and it is what keeps the conservation sum from counting the
#: same tokens twice, once for the compound and once for the children that will
#: actually do its work.  (When P3 charges a COMPOSITION review to the parent
#: compound's account — main plan §13, the purpose→account table — that review is
#: funded by raising this deliberately, not by leaving dead weight here now.)
COMPOUND_TOKENS = 0


@dataclass(frozen=True, slots=True)
class OccurrenceTask:
    """One materialised occurrence: the row to insert plus the account to open."""

    task: Task
    occurrence_id: str
    form: TaskForm
    obligation_id: str
    ordinal: int

    @property
    def dispatchable(self) -> bool:
        """Whether a Worker may ever be given this row (never for a compound)."""

        return self.form is TaskForm.PRIMITIVE

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task.id,
            "occurrence_id": self.occurrence_id,
            "obligation_id": self.obligation_id,
            "form": str(self.form),
            "status": str(self.task.status),
            "max_tokens": self.task.budget.max_tokens,
            "success_criteria": list(self.task.success_criteria),
            "verification_policy": list(self.task.verification_policy),
        }


@dataclass(frozen=True, slots=True)
class Materialisation:
    """What one plan revision put on the board, and the budget equation it used."""

    tasks: tuple[OccurrenceTask, ...] = ()
    reused: tuple[str, ...] = ()
    pool_tokens: int | None = None
    share_tokens: int | None = None
    #: What the Mission's Task rows already hold, *before* this round — read from the
    #: store, not from this network, because an occurrence retired by an earlier
    #: revision still owns the tokens its account was opened with.
    committed_tokens: int = 0
    funded_now: int = 0
    reserved_subtrees: int = 0
    existing: Mapping[str, int] = field(default_factory=dict)

    @property
    def created(self) -> tuple[str, ...]:
        return tuple(item.task.id for item in self.tasks)

    @property
    def granted_tokens(self) -> int:
        """What *this* round handed out."""

        return sum(item.task.budget.max_tokens or 0 for item in self.tasks)

    def conservation(self) -> dict[str, Any]:
        """§21.5 budget conservation, as the equation and not as a claim.

        The equation is the legacy one, applied to the occurrence rows instead of to
        a Planner's DAG (``graph/task_graph.py``: *sum of task max_tokens plus the
        system reserve never exceeds the Mission*)::

            committed_before + granted_now <= pool

        where ``pool`` is the Mission's ceiling minus the system reserve.  Writing it
        against what the *store* already holds rather than against this network is
        the load-bearing part: a plan grows by refining compounds, so round two's
        occurrences are funded out of what round one left, and an equation stated
        over one network alone would re-spend the same pool every revision.

        ``pool_tokens is None`` means the Mission declared no token ceiling, in which
        case there is nothing to conserve and the equation is reported as holding
        vacuously rather than silently skipped.
        """

        granted = self.granted_tokens
        return {
            "pool_tokens": self.pool_tokens,
            "committed_tokens": self.committed_tokens,
            "granted_tokens": granted,
            "share_tokens": self.share_tokens,
            "funded_now": self.funded_now,
            "reserved_subtrees": self.reserved_subtrees,
            "materialised": len(self.tasks),
            "reused": list(self.reused),
            # What the reused rows already hold, per row.  Reported rather than only
            # summed, because "the pool is full" and "one occurrence is holding all of
            # it" need different repairs and the sum cannot tell them apart.
            "held_by_reused": {key: int(value) for key, value in sorted(self.existing.items())},
            # Review F15: ``held_by_reused`` covers only the rows *this* network reuses,
            # while ``committed_tokens`` counts every row the Mission has — including
            # the ones a previous revision retired, which still hold their accounts.
            # Reporting only the first made the event look like a decomposition of the
            # second that did not add up.  This is the rest of it, so the account
            # closes: ``held_by_reused`` + ``held_elsewhere`` == ``committed_tokens``.
            "held_elsewhere": int(self.committed_tokens)
            - sum(int(value) for value in self.existing.values()),
            "holds": (
                self.pool_tokens is None or self.committed_tokens + granted <= self.pool_tokens
            ),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "materialised_by": MATERIALISER,
            "tasks": [item.to_json() for item in self.tasks],
            "reused": list(self.reused),
            "budget": self.conservation(),
        }


def task_pool_tokens(mission: Mission) -> int | None:
    """The tokens a materialised plan may spend: the Mission's, minus the reserve.

    The same quantity the DAG Planner is handed as ``budget_for_tasks`` (D4-20), read
    through the same function, so the two modes cannot disagree about how big the
    system reserve is.
    """

    ceiling = mission.budget.max_tokens
    if ceiling is None:
        return None
    return max(0, int(ceiling) - system_reserve_tokens(mission))


def share_tokens(available: int | None, funded_now: int, reserved_subtrees: int = 0) -> int | None:
    """One new primitive occurrence's share of what the pool still has.

    Two things make this different from "pool divided by the primitives of this
    network", which is the obvious rule and the wrong one:

    * **The divisor counts the compounds nobody has refined yet.**  A plan grows by
      refining a compound into children, and those children have to be payable when
      they arrive.  If every token were handed to the primitives of revision one,
      revision two's children would be funded out of nothing and the refinement would
      be refused for lack of budget — the plan would be shaped by the order the
      Planner happened to expand it in.  So each unrefined compound reserves one
      share for the subtree it still owes.
    * **The dividend is what is *left*.**  ``available`` is the pool minus what the
      Mission's existing rows already hold, so the equation in
      :meth:`Materialisation.conservation` stays true across revisions instead of
      being re-satisfied from scratch each time.

    Floor division and not "distribute the remainder": the remainder stays on the
    Mission account, where a Task that needs more than its share can still draw it
    through the normal reserve path (``BudgetLedger.reserve`` charges the whole
    chain).  Handing the remainder to whichever occurrence happens to sort first
    would make the plan's budget depend on occurrence ids.
    """

    if available is None:
        return None
    slots = max(0, funded_now) + max(0, reserved_subtrees)
    if slots <= 0:
        return max(0, available)
    return max(0, available) // slots


def occurrence_criteria(
    binding: TaskSemanticBindingV1, requirements: RequirementsRevision | None
) -> tuple[str, ...]:
    """The Task row's ``success_criteria``: the duty's criteria and their checks.

    Two sources, and the second is the one that makes the row *checkable*: the
    occurrence's ``requirement_refs`` name which criteria it owes, and the
    Mission's ``RequirementsRevision`` says which checks each of those criteria
    requires (``RequiredEvidencePolicy.required_check_ids``).  The legacy verifier
    layers read strings with known prefixes (``pytest:``, ``file:``), so a criterion
    whose required checks are written in that vocabulary is verified by the real
    router rather than by an unread criterion id.

    Criterion ids with no declared check still go on the row.  They are inert to the
    deterministic layers and they are the record of *what this occurrence owes* —
    dropping them would make the Task row disagree with the Acceptance that will
    later quote them.
    """

    criteria: list[str] = []
    declared: Mapping[str, tuple[str, ...]] = {}
    if requirements is not None:
        declared = {
            item.criterion_id: tuple(item.required_evidence_policy.required_check_ids)
            for item in requirements.criteria
        }
    for reference in binding.requirement_refs:
        if reference not in criteria:
            criteria.append(reference)
        for check in declared.get(reference, ()):
            if check not in criteria:
                criteria.append(check)
    if not criteria:
        # ``Task.success_criteria`` may not be empty (§6.3: "完成条件不清").  An
        # occurrence with no requirement at all has nothing a review could quote, so
        # the goal signature's coverage criteria are the honest fallback — they are
        # what the *type* says this goal owes.
        criteria = [str(item) for item in binding.goal_signature.coverage_criteria]
    return tuple(criteria)


#: P2.3k / defect N4: the side effects under which a leaf changes nothing the
#: ``code_test`` layer could measure.  The same set the registry calls read-only.
READ_ONLY_SIDE_EFFECTS = frozenset({SideEffectKind.NONE, SideEffectKind.EXTERNAL_READ})


def read_only_leaf(binding: TaskSemanticBindingV1) -> bool:
    """Whether this occurrence's type declares that it writes nothing (P2.3k / N4).

    Three declarations have to agree, and silence on the first is *not* read-only: a
    binding whose ``side_effect_kind`` is ``None`` said nothing, and a leaf that said
    nothing keeps the system default.  A write capability (``repo.write``, anything
    ending in ``.write``) or a declared ``resource_writes`` overrides a read-only side
    effect — the type's own words are the authority, and a type that asks to write is
    not read-only whatever else it says.
    """

    if binding.side_effect_kind is None:
        return False
    if SideEffectKind(binding.side_effect_kind) not in READ_ONLY_SIDE_EFFECTS:
        return False
    if any(str(item).endswith(".write") for item in binding.capability_requirements):
        return False
    return not binding.resource_writes


def occurrence_policy(
    criteria: Sequence[str],
    deployed: frozenset[str],
    declared: Sequence[str] = (),
    *,
    read_only: bool = False,
) -> tuple[str, ...]:
    """The verification layers one materialised occurrence runs.

    ``declared`` wins when a deployment states one (narrowed to what it runs);
    otherwise it is the system default narrowed the same way, plus ``code_test``
    whenever a criterion actually names a ``pytest:`` target — a Task carrying a test
    criterion that no layer runs is a criterion nobody checks.

    P2.3k / defect N4: a ``read_only`` leaf is not given the *default* ``code_test``.
    Grok C3's ``facts`` and ``reproduce`` leaves each lost their first Attempt to
    ``code_test`` on a baseline that is red by construction, and the model then patched
    product code inside a read-only leaf to get through.  The layer can only measure
    the patch step's work, so it stays on the leaves that write.  A ``pytest:``
    criterion of the leaf's own still adds it (a criterion nobody checks is the worse
    outcome), and a policy the deployment *declared* is narrowed, never rewritten.
    """

    stated = tuple(layer for layer in declared if layer in deployed)
    chosen = stated or default_change_policy(deployed)
    if read_only and not stated:
        chosen = tuple(layer for layer in chosen if layer != "code_test")
    if any(str(item).startswith("pytest:") for item in criteria) and "code_test" in deployed:
        if "code_test" not in chosen:
            chosen = (*chosen, "code_test")
    return chosen


def read_only_existing_paths(
    seed: Mapping[str, Any] | Iterable[str],
    upstream: Iterable[Any] = (),
    extra: Mapping[str, Any] | Iterable[str] = (),
) -> tuple[str, ...]:
    """Paths a read-only leaf started from: seed ∪ overlay/upstream ∪ extras.

    Same set :func:`read_only_rewrites` uses as ``initial``.  A retry tree that
    copied the previous Attempt still lets the leaf rewrite files it created
    (REPORT.md, port outputs) — those paths are not in this snapshot.
    """

    paths: set[str] = set()
    if isinstance(seed, Mapping):
        paths.update(str(path) for path in seed)
    else:
        paths.update(str(path) for path in seed)
    for item in upstream:
        path = getattr(item, "path", item)
        paths.add(str(path))
    if isinstance(extra, Mapping):
        paths.update(str(path) for path in extra)
    else:
        paths.update(str(path) for path in extra)
    return tuple(sorted(path for path in paths if path))


#: P2.3m.  How many times one occurrence may be refused
#: ``read_only_leaf_rewrote_workspace`` before the named feedback goes to planning
#: instead of another Attempt.  Same bound as ``MAX_SYNTHESIS_ASKS`` /
#: ``MAX_ROOT_REVIEW_ASKS`` / ``evidence_saturation_rounds``: one retry, then the
#: honest next layer.  Not a config item.
MAX_READ_ONLY_REWRITE_REJECTIONS = 2
#: How many planning repair rounds one Mission may open for this reason.  Same
#: shape as ``max_root_review_repairs``'s default, as a constant so nothing is
#: added to the policy snapshot.
MAX_READ_ONLY_REWRITE_REPAIRS = 1
#: P2.3v.  How many consecutive *identical* verification failures one occurrence
#: may retry before the named feedback goes to planning.  One more than the
#: read-only rewrite bound: the first two retries are still the leaf's, the
#: third is the honest next layer.  Not a config item.
MAX_IDENTICAL_VERIFICATION_FAILURES = 3
#: How many planning repair rounds one Mission may open for this reason.
MAX_IDENTICAL_VERIFICATION_REPAIRS = 1

_TIMING = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b")
_WORKSPACE_PATH = re.compile(r"(?:/[\w.-]+)*/workspaces/[\w.:-]+/")
_PYTEST_EXC = re.compile(r"^E\s+(\w+(?:Error|Exception|Warning)): ", re.M)
_PYTEST_NODE = re.compile(r"^(?:ERROR|FAILED) (\S+)", re.M)


def read_only_rewrites(
    binding: TaskSemanticBindingV1,
    artifacts: Sequence[Any],
    initial: Mapping[str, str],
    *,
    guarded: Iterable[str] = (),
    accepted: Mapping[str, str] = (),
) -> list[str]:
    """The files a read-only leaf's Attempt changed that it was not allowed to change.

    P2.3k verification P1-2: the type declaration is now *enforced* at result
    collection, not only used to drop a verification layer.  Grok C3's ``facts`` and
    ``reproduce`` leaves — ``external_read`` by declaration — each rewrote
    ``stats/window.py``.  A read-only leaf may create files (its port outputs, its
    report) — that is how it delivers what it observed — but may not change a file
    it started from: ``initial`` is the seed plus the resolved upstream inputs, and a
    path present there whose bytes differ is a write into the world the leaf was asked
    to observe.  ``guarded`` paths are already refused as ``protected_path_rewritten``
    and are not reported twice.

    P2.3m: ``accepted`` is path → content hash of CURRENT accepted artifacts on this
    Mission.  Grok H-L3-C1-r0's verify leaf rewrote ``metrics/collector.py`` and
    ``metrics/reporter.py`` to the hashes the apply-patch leaf had already had
    accepted — it was re-applying the patch onto a workspace that still started from
    the unpatched seed.  Bytes that already belong to an accepted artifact at that
    path are not a new write.
    """

    if not read_only_leaf(binding):
        return []
    shielded = set(guarded)
    allowed = dict(accepted)
    return sorted(
        artifact.path
        for artifact in artifacts
        if artifact.path in initial
        and artifact.path not in shielded
        and artifact.content_hash != initial[artifact.path]
        and allowed.get(artifact.path) != artifact.content_hash
    )


def verification_failure_fingerprint(failures: Sequence[Any]) -> str:
    """Identity of one verification failure for consecutive-retry bounding (P2.3v).

    Same layer + same problems list, else a stable ``code_test`` node (exception
    class / pytest node id, paths and ``0.06s`` stripped).  Grok M3-r0's 21
    ``code_test`` FAILs were the same SyntaxError; two different exceptions
    that both say ``1 error in Xs`` are not the same failure (P1-2).
    """

    parts: list[str] = []
    for item in failures:
        if not isinstance(item, Mapping):
            continue
        layer = str(item.get("layer") or "")
        detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
        problems = detail.get("problems") if isinstance(detail, Mapping) else None
        if isinstance(problems, list) and problems:
            body = json.dumps(problems, ensure_ascii=False, sort_keys=True)
        else:
            body = _stable_code_test_body(
                detail if isinstance(detail, Mapping) else {},
                str(item.get("summary") or ""),
            )
        body = _TIMING.sub("in Xs", body)
        body = _WORKSPACE_PATH.sub("<ws>/", body)
        parts.append(f"{layer}\0{body}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _stable_code_test_body(detail: Mapping[str, Any], summary: str) -> str:
    nodes = detail.get("failure_nodes")
    if isinstance(nodes, list) and nodes:
        return json.dumps(nodes, ensure_ascii=False)
    extracted: list[dict[str, Any]] = []
    for run in detail.get("runs") or ():
        if not isinstance(run, Mapping):
            continue
        text = str(run.get("stdout") or run.get("error") or "")
        text = _WORKSPACE_PATH.sub("<ws>/", text)
        text = _TIMING.sub("in Xs", text)
        extracted.append(
            {
                "exc": _PYTEST_EXC.findall(text),
                "node": _PYTEST_NODE.findall(text),
            }
        )
    if any(item["exc"] or item["node"] for item in extracted):
        return json.dumps(extracted, ensure_ascii=False)
    return summary


def occurrence_task(
    mission: Mission,
    spec: OccurrenceSpec,
    binding: TaskSemanticBindingV1,
    *,
    plan_revision: int,
    budget: Budget,
    ordinal: int,
    deployed: frozenset[str],
    requirements: RequirementsRevision | None = None,
    now: float = 0.0,
    declared_policy: Sequence[str] = (),
    criterion_linked: bool = False,
) -> OccurrenceTask:
    """Build the Task row for one occurrence.  Pure: nothing is written here.

    ``criterion_linked`` (P2.3k verification P1-2): the plan's ``criterion_links``
    point at this occurrence, so its accepted output is what a root criterion is
    judged on.  Such a leaf keeps the default ``code_test`` layer even when its type
    is read-only — ``code.verify-tests`` is ``external_read``, and it is exactly the
    leaf whose report says the tests pass; the deterministic layer stays on it.

    A primitive starts READY — "may compete", which in this mode is not a permission
    because the v2 frontier takes only ``EligiblePrimitiveTask`` records.  A compound
    starts BLOCKED, because a compound is refined and never dispatched, and BLOCKED
    is the closest true statement the legacy vocabulary has.
    """

    primitive = binding.form is TaskForm.PRIMITIVE
    criteria = occurrence_criteria(binding, requirements)
    goal = binding.goal_signature.statement or f"satisfy {binding.goal_signature.signature_id}"
    return OccurrenceTask(
        task=Task(
            id=str(spec.task_id),
            mission_id=mission.id,
            parent_task_ids=(),
            # Deliberately empty — see the module docstring.  Ordering in this mode is
            # the ORDER gate's answer over the typed network, not a second copy of it
            # in a conjunctive legacy field.
            dependency_ids=(),
            goal=goal,
            rationale=(
                f"occurrence {spec.occurrence_id!s} of plan revision {int(plan_revision)}, "
                f"serving duty {binding.obligation_id!s} through goal "
                f"{binding.goal_signature.signature_id}"
            ),
            success_criteria=criteria,
            verification_policy=occurrence_policy(
                criteria,
                deployed,
                declared_policy,
                read_only=read_only_leaf(binding) and not criterion_linked,
            ),
            allowed_tools=mission.allowed_tools,
            budget=budget,
            priority=_priority(spec),
            status=TaskStatus.READY if primitive else TaskStatus.BLOCKED,
            version=1,
            root_goal=mission.goal,
            created_at=now,
            ready_at=now if primitive else None,
            kind="work",
            context={
                CONTEXT_OCCURRENCE: str(spec.occurrence_id),
                CONTEXT_PLAN_REVISION: int(plan_revision),
                CONTEXT_FORM: str(binding.form),
                CONTEXT_REQUIREDNESS: str(spec.requiredness),
                CONTEXT_MATERIALISED_BY: MATERIALISER,
            },
        ),
        occurrence_id=str(spec.occurrence_id),
        form=binding.form,
        obligation_id=str(binding.obligation_id),
        ordinal=int(ordinal),
    )


def _priority(spec: OccurrenceSpec) -> float:
    """A required occurrence outranks an optional one; nothing finer is claimed.

    The §29.3 score does the real ordering.  This is only the tie-break input the
    Task contract asks for, and inventing a finer scale here would be a scheduling
    policy hidden in a materialiser.
    """

    return 1.0 if spec.requiredness is Requiredness.REQUIRED else 0.5


__all__ = (
    "CONTEXT_FORM",
    "CONTEXT_MATERIALISED_BY",
    "CONTEXT_OCCURRENCE",
    "CONTEXT_PLAN_REVISION",
    "CONTEXT_REQUIREDNESS",
    "COMPOUND_TOKENS",
    "MATERIALISER",
    "MIN_TOKEN_SHARE",
    "Materialisation",
    "OccurrenceTask",
    "MAX_READ_ONLY_REWRITE_REJECTIONS",
    "MAX_READ_ONLY_REWRITE_REPAIRS",
    "MAX_IDENTICAL_VERIFICATION_FAILURES",
    "MAX_IDENTICAL_VERIFICATION_REPAIRS",
    "verification_failure_fingerprint",
    "occurrence_criteria",
    "occurrence_policy",
    "occurrence_task",
    "read_only_existing_paths",
    "read_only_leaf",
    "read_only_rewrites",
    "share_tokens",
    "task_pool_tokens",
)
