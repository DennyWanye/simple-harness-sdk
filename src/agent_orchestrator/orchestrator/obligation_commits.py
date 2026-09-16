# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.3: a replacement Task inherits its predecessor's duty (§6.1, §8.4, §15.2).

The plan says the same thing three times, because it is the one place where a
planner could buy itself an unlimited retry budget for free: splitting a task,
switching a method, handing the work to another agent and superseding a running
Task are all changes of *shape*.  The failure count, the consumed budget and the
recursion fuel belong to the ``obligation_id``, so a successor that quietly
received a new id would start again from zero simply because the work was renamed.

This mixin is the single wiring point for that rule.  It is deliberately narrow:

* **Legacy work returns immediately.**  A Task with no ``task_semantics`` row is a
  Mission from before the full-target layer.  One lookup answers that, and nothing
  after it runs — no new table is written and no existing event payload is touched
  (§18.5 rule 1).
* **It never names a new table itself.**  Every read and write goes through
  :class:`HtnStore` / :class:`ObligationStore`, so ``storage`` stays the single
  writing authority for the migration-16 rows (§18.5).
* **It writes inside the caller's transaction.**  ``Store.transaction()`` is
  re-entrant for the task that opened it, so the successor's binding, the duty
  relation and the shape-change row land with the graph change or not at all.
* **Nothing here moves a counter, and nothing here rewrites a neighbour.**  The
  only ledger call is ``note_shape_change``, which appends one row for one duty.
  There is deliberately no ``persist(ledger)``: writing the whole ledger back
  would restamp every *other* duty's ``updated_at`` and re-insert the shape
  history of Missions this replacement never touched.

The successor's binding carries the *meaning* across and deliberately drops the
*dispatch state*: a replacement has not been dispatched, has adopted no method
instance and has resolved no inputs yet, so those fields start at zero (§6.4).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from simple_harness.contracts import canonical_json

from ..contracts import ContractError
from ..contracts.htn import (
    ContractRevision,
    DispatchGeneration,
    InputBindingRevision,
    ObligationId,
    ObligationRelation,
    TaskRef,
    TaskSemanticBindingV1,
)
from ..contracts.obligations import ObligationAccountView, ShapeChange
from ..contracts.semantic_base import content_hash_of
from ..storage.htn_store import HtnStore
from ..storage.obligation_store import ObligationStore
from ..storage.store import StoreError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..contracts import Event
    from ..storage.store import Store

#: The re-planning reasons this hook accepts and the ledger history each records.
#: They are exactly the four §8.4 names ("fix, replacement Task, role change, agent
#: change"); every one of them is a shape change, so none of them touches a counter.
INHERITANCE_REASONS: Mapping[str, ShapeChange] = {
    "REPLACE": ShapeChange.SUCCESSOR_TASK,
    "RENAME": ShapeChange.TASK_RENAMED,
    "ROLE_CHANGE": ShapeChange.AGENT_REASSIGNED,
    "METHOD_CHANGE": ShapeChange.METHOD_SWITCHED,
}

#: "somebody with a name asked for this duty's work, and here is what they showed."
#: P2.3c part 2d, decision 3.  Before it, ``demand_admitted`` was only ever flipped
#: by a test fixture, so the readiness gate TG §6 and §24.1 decision 9 require was
#: permanently closed on the production path.
DEMAND_ADMITTED = "ObligationDemandAdmitted"

#: The symmetric record: that interest ended.  §24.1 decision 9 -- a branch that
#: retires removes **its own** adoption relation and nobody else's.
DEMAND_WITHDRAWN = "ObligationDemandWithdrawn"

#: The three shapes a requester may take.  ``method_slot`` is the ordinary one (TG
#: §9.2's ``DemandRef``); ``authorization`` is an independently authorised duty
#: standing on its own authority (§6.1); ``mission_root`` is the Mission's own root
#: duty, which is asked for by the requirements the Mission was created from and
#: therefore has no adopting slot above it.
REQUESTER_KINDS = frozenset({"method_slot", "authorization", "mission_root"})


class ObligationCommitsMixin:
    """Carry one LogicalObligation across a re-planning step."""

    if TYPE_CHECKING:  # pragma: no cover - provided by CommitService
        _store: Store

        def _emit(
            self,
            event_type: str,
            mission_id: str,
            *,
            key: str,
            task_id: str | None = None,
            attempt_id: str | None = None,
            payload: Mapping[str, Any] | None = None,
        ) -> Event: ...

    # ------------------------------------------------------- P2.3c 2d: demand (§6.1)
    def admit_obligation_demand(
        self,
        mission_id: str,
        obligation_id: ObligationId | str,
        *,
        principal: str,
        requester: Mapping[str, Any],
        evidence: Mapping[str, Any],
        parent_obligation_id: ObligationId | str | None = None,
        relation: str | None = None,
        ledger: Any = None,
        plan_revision: int | None = None,
    ) -> ObligationAccountView:
        """Record that a named consumer wants this duty's work, on stated evidence.

        This is the **single production entry point** for admitting a demand, and
        ``tests/orchestrator/full_target/test_plan_commits.py`` holds a static guard
        that nothing else in ``src/`` calls the ledger primitive directly.  The point
        is not ceremony: ``demand_admitted`` is what makes an occurrence dispatchable
        (TG §6, §24.1 decision 9), so "who asked, on what authority" has to be a
        written record rather than a boolean somebody set.

        ``requester`` says *who* (see :data:`REQUESTER_KINDS`); ``evidence`` says
        *what they showed* -- the parent duty and the plan revision that adopted the
        slot, the authorisation reference, or the Mission's requirement refs.  Both
        are written into the event, so the admission can be audited without replaying
        the planner.

        ``ledger`` lets the plan-commit path admit inside its own transaction on the
        ledger it is about to persist; without one the store is used directly.
        """

        kind = str(requester.get("kind", ""))
        if kind not in REQUESTER_KINDS:
            raise ContractError(
                f"{kind!r} is not a requester kind; a demand is asked for by one of "
                f"{sorted(REQUESTER_KINDS)} (§6.1, TG §9.2)"
            )
        if not str(principal).strip():
            raise ContractError(
                "admitting a demand needs the principal that authorised the command; "
                "a model may propose the shape of the work, never the fact that it was "
                "asked for (§18.5)"
            )
        if not evidence:
            raise ContractError(
                f"the demand for {obligation_id!s} names no evidence; §6.1 requires the "
                "authorisation to be recorded, not asserted"
            )
        target = ObligationId(str(obligation_id))
        view = self._flip_demand(mission_id, target, ledger=ledger, admit=True)
        self._emit_demand_event(
            DEMAND_ADMITTED,
            mission_id,
            target,
            principal=principal,
            requester=requester,
            evidence=evidence,
            parent_obligation_id=parent_obligation_id,
            relation=relation,
            plan_revision=plan_revision,
            view=view,
        )
        return view

    def withdraw_obligation_demand(
        self,
        mission_id: str,
        obligation_id: ObligationId | str,
        *,
        principal: str,
        requester: Mapping[str, Any],
        evidence: Mapping[str, Any],
        parent_obligation_id: ObligationId | str | None = None,
        relation: str | None = None,
        ledger: Any = None,
        plan_revision: int | None = None,
    ) -> ObligationAccountView:
        """End one consumer's interest in this duty, and say whose it was.

        §24.1 decision 9: a retiring branch removes **its own** adoption relation.
        The requester travels for exactly that reason -- a withdrawal that did not
        name whose interest it ended could not be told apart from cancelling the duty
        for everybody.
        """

        kind = str(requester.get("kind", ""))
        if kind not in REQUESTER_KINDS:
            raise ContractError(
                f"{kind!r} is not a requester kind; a demand is withdrawn by the consumer "
                f"that held it, one of {sorted(REQUESTER_KINDS)} (§24.1 decision 9)"
            )
        # Third-round review P2-4: the same standard as admitting.  Ending a
        # consumer's interest makes an occurrence undispatchable, which is as much a
        # decision about the work as starting it — an unsigned, unevidenced withdrawal
        # is a boolean somebody set, which is what this entry point exists to replace.
        if not str(principal).strip():
            raise ContractError(
                "withdrawing a demand needs the principal that authorised the command; a "
                "withdrawal nobody signed cannot be told apart from a lost demand (§18.5)"
            )
        if not evidence:
            raise ContractError(
                f"the withdrawal of the demand for {obligation_id!s} names no evidence; §6.1 "
                "requires the authorisation to be recorded, not asserted"
            )
        target = ObligationId(str(obligation_id))
        view = self._flip_demand(mission_id, target, ledger=ledger, admit=False)
        self._emit_demand_event(
            DEMAND_WITHDRAWN,
            mission_id,
            target,
            principal=principal,
            requester=requester,
            evidence=evidence,
            parent_obligation_id=parent_obligation_id,
            relation=relation,
            plan_revision=plan_revision,
            view=view,
        )
        return view

    def _flip_demand(
        self, mission_id: str, target: ObligationId, *, ledger: Any, admit: bool
    ) -> ObligationAccountView:
        """Set the bit, on the caller's in-flight ledger or through the store."""

        if ledger is not None:
            return ledger.admit_demand(target) if admit else ledger.withdraw_demand(target)
        duties = ObligationStore(self._store)
        if admit:
            return duties.admit_demand(mission_id, target)
        return duties.withdraw_demand(mission_id, target)

    def _emit_demand_event(
        self,
        event_type: str,
        mission_id: str,
        target: ObligationId,
        *,
        principal: str,
        requester: Mapping[str, Any],
        evidence: Mapping[str, Any],
        parent_obligation_id: ObligationId | str | None,
        relation: str | None,
        plan_revision: int | None,
        view: ObligationAccountView,
    ) -> None:
        """One audit entry per *act*, not one per (duty, revision, direction).

        Third-round review P2-4.  Keying on ``(mission, duty, event_type, revision)``
        alone made a second admission inside one revision — ``admit → withdraw →
        admit``, which is exactly what a retired-and-re-adopted slot does — collide
        with the first and be swallowed by the Commit Service's idempotency key.  The
        ledger bit really flipped back, so the account and the audit trail disagreed
        by one act.  The ordinal is how many complete flips this duty has already been
        through, read back from the events themselves, so a genuine replay (the same
        act, re-offered) still lands on the same key and a new act never does.  The
        bootstrap path (``plan_revision=None``) is the one that shows it most: the root
        duty's admission carries no revision to separate it at all.
        """

        self._emit(
            event_type,
            mission_id,
            key=f"{mission_id}:{target!s}:{event_type}:{plan_revision}:"
            f"{self._demand_ordinal(mission_id, target, event_type)}",
            payload={
                "obligation_id": str(target),
                "parent_obligation_id": (
                    None if parent_obligation_id is None else str(parent_obligation_id)
                ),
                "relation": None if relation is None else str(relation),
                "requester": dict(sorted(requester.items())),
                "evidence": dict(sorted(evidence.items())),
                "principal": str(principal),
                "plan_revision": plan_revision,
                "has_admitted_demand": bool(view.has_admitted_demand),
            },
        )

    def _demand_ordinal(self, mission_id: str, target: ObligationId, event_type: str) -> int:
        """Which flip of this duty's demand this act is (review P2-4).

        Counted from the events of the **opposite** direction, which is what makes the
        answer both correct and replay-stable.  A demand alternates: admitted,
        withdrawn, admitted again.  So the *n*-th admission is the one with *n*
        withdrawals recorded before it, and the *n*-th withdrawal is the one that ends
        the *n*-th admission.  Counting this direction's own events instead would make
        a re-offered act look like a new one (the act's own event is already there) and
        no state check can tell those apart — which is how the first attempt at this
        repair still let ``admit → withdraw → admit`` write only one admission.
        """

        def recorded(kind: str) -> int:
            return sum(
                1
                for event in self._store.list_events(mission_id)
                if event.type == kind
                and str((event.payload or {}).get("obligation_id", "")) == str(target)
            )

        if event_type == DEMAND_ADMITTED:
            return recorded(DEMAND_WITHDRAWN)
        return max(recorded(DEMAND_ADMITTED) - 1, 0)

    def _inherit_obligation_on_replacement(
        self,
        conn: sqlite3.Connection,
        mission_id: str,
        old_task_id: str,
        new_task_id: str,
        *,
        reason: str,
        graph_version: int | None = None,
    ) -> ObligationId | None:
        """Hand ``old_task_id``'s duty to ``new_task_id``; return it, or ``None``.

        ``None`` means "this is legacy work": the predecessor carries no semantic
        binding, so there is no duty to inherit and nothing at all was written.
        ``new_task_id == old_task_id`` is the in-place case (a role or method
        change): the history is recorded, no successor binding is created.
        """

        if not self._store.has_table("task_semantics"):
            return None  # a library from before migration 16
        semantics = HtnStore(self._store)
        binding = semantics.task_semantics_of(mission_id, old_task_id)
        if binding is None:
            return None  # legacy Task: no meaning beside it, so no duty to carry
        change = INHERITANCE_REASONS.get(reason)
        if change is None:
            raise ContractError(
                f"{reason!r} is not an inheritance reason; a duty is carried across "
                f"one of {sorted(INHERITANCE_REASONS)} (§8.4)"
            )
        if not conn.in_transaction:
            raise StoreError("obligation inheritance must run inside the commit transaction")
        inherited = binding.obligation_id
        obligations = ObligationStore(self._store)
        if not obligations.exists(mission_id, inherited):
            raise StoreError(
                f"task {old_task_id} is bound to obligation {inherited!s}, which mission "
                f"{mission_id} does not carry; a successor may not invent an allowance (§15.2)"
            )
        successor, carried = self._successor_obligation(semantics, mission_id, new_task_id, binding)
        relation: str | None = None
        if successor != inherited:
            # §6.1: a genuinely new responsibility is created explicitly and keeps its
            # lineage; the accumulated counters stay with the duty that earned them.
            obligations.add_relation(
                mission_id,
                parent=inherited,
                child=successor,
                kind=ObligationRelation.REFINES_PARENT,
                detail={
                    "reason": reason,
                    "superseded_task": old_task_id,
                    "successor_task": new_task_id,
                },
            )
            relation = str(ObligationRelation.REFINES_PARENT)
        detail = canonical_json(
            {
                "reason": reason,
                "old_task_id": old_task_id,
                "new_task_id": new_task_id,
                "graph_version": graph_version,
            }
        )
        # One row, for this duty only.  The returned view's ``shape_changes`` is the
        # ordinal just written, which is what makes two identical re-plans of the same
        # Task two distinct events instead of one silently dropped duplicate.
        view = obligations.note_shape_change(mission_id, inherited, change, detail=detail)
        self._emit(
            "ObligationInherited",
            mission_id,
            key=f"{mission_id}:{old_task_id}:{new_task_id}:{reason}:{view.shape_changes}",
            task_id=new_task_id,
            payload={
                "obligation_id": str(inherited),
                "successor_obligation_id": str(successor),
                "superseded_task": old_task_id,
                "successor_task": new_task_id,
                "reason": reason,
                "shape_change": str(change),
                "shape_change_ordinal": view.shape_changes,
                "graph_version": graph_version,
                "relation": relation,
                "binding_inherited": carried,
                "failure_count": view.failure_count,
                "consumed_cost_micros": view.consumed_cost_micros,
                "consumed_attempts": view.consumed_attempts,
                "spent_tokens": obligations.spent_tokens(mission_id, inherited),
                "fuel_limit": view.fuel_limit,
                "fuel_used": view.fuel_used,
                "fuel_remaining": view.remaining_fuel,
            },
        )
        return inherited

    @staticmethod
    def _successor_obligation(
        semantics: HtnStore,
        mission_id: str,
        new_task_id: str,
        binding: TaskSemanticBindingV1,
    ) -> tuple[ObligationId, bool]:
        """The successor's duty, copying the binding when the command brought none."""

        if new_task_id == str(binding.task_id):
            return binding.obligation_id, False  # in-place: the same Task keeps its binding
        own = semantics.task_semantics_of(mission_id, new_task_id)
        if own is not None:
            return own.obligation_id, False  # the replacement command brought its own meaning
        semantics.put_task_semantics(mission_id, _successor_binding(binding, new_task_id))
        return binding.obligation_id, True


def _successor_binding(binding: TaskSemanticBindingV1, new_task_id: str) -> TaskSemanticBindingV1:
    """The predecessor's meaning on a new Task, with its dispatch state cleared (§6.4).

    What is carried: the duty, the goal signature, the typed parameters, the ports,
    the requirements and the operator.  What is *not*: the adopted method instance,
    the occurrence it sat in, the resolved-input revision and the dispatch
    generation — a replacement has done none of those things yet, and inheriting
    them would let a stale dispatch right or a stale input manifest follow the work
    onto a Task that never earned it.
    """

    return replace(
        binding,
        task_id=TaskRef(new_task_id),
        contract_revision=ContractRevision(0),
        contract_hash=content_hash_of(
            {
                "inherited_from_task_id": str(binding.task_id),
                "inherited_from_contract_hash": binding.contract_hash,
                "inherited_from_contract_revision": int(binding.contract_revision),
                "task_id": new_task_id,
                "contract_revision": 0,
            }
        ),
        occurrence_binding=None,
        adopted_method_instance_id=None,
        input_binding_revision=InputBindingRevision(0),
        dispatch_generation=DispatchGeneration(0),
    )


__all__ = (
    "DEMAND_ADMITTED",
    "DEMAND_WITHDRAWN",
    "INHERITANCE_REASONS",
    "REQUESTER_KINDS",
    "ObligationCommitsMixin",
)
