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
from ..contracts.obligations import ShapeChange
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


__all__ = ("INHERITANCE_REASONS", "ObligationCommitsMixin")
