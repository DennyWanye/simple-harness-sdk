# SPDX-License-Identifier: Apache-2.0
"""Bind the candidate document to the exact semantic rows the original Commit writes."""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from simple_harness.contracts import canonical_json

from ..contracts.semantic_base import TypedRef
from ..contracts.htn import TaskSemanticBindingV1
from ..contracts.models import sha256_hex
from ..graph.network_codec import NetworkDocumentV1, encode
from ..graph.convergence import compute_convergence_impact
from ..storage.htn_store import HtnStore
from ..storage.store import Store, StoreError
from .taskgraph_bindings import current_bindings as read_current_bindings

if TYPE_CHECKING:
    from .plan_commits import CommitPlanCommand


def document_for_commit(store: Store, command: CommitPlanCommand,
                        requirements_ref: TypedRef, *,
                        before: NetworkDocumentV1 | None = None) -> NetworkDocumentV1:
    """Run before preview binding, and again before any original Commit write.

    H1's network can decorate a compound binding with its newly adopted method.
    Original Commit persists adoption separately, leaving that binding's immutable
    bytes untouched. TaskGraph records those bytes and the explicit adopted set.
    Only this one known decoration may differ. A surviving input-replaced task's
    generation change is predicted using the original revocation function before
    preview hashes are frozen; the actual Commit must write those exact bytes.
    """
    with store.read_view():
        semantics = HtnStore(store)
        tasks = store.list_tasks(command.mission_id)
        bindings = read_current_bindings(semantics, command.mission_id)
        return document_from_bindings(command, requirements_ref, before=before, current_bindings=bindings,
                                      existing_task_ids=frozenset(task.id for task in tasks))


def document_from_bindings(command: CommitPlanCommand, requirements_ref: TypedRef, *,
                           before: NetworkDocumentV1 | None,
                           current_bindings: tuple[TaskSemanticBindingV1, ...],
                           existing_task_ids: frozenset[str]) -> NetworkDocumentV1:
    """Pure candidate construction from the complete frozen original binding set."""
    current = {str(item.task_id): item for item in current_bindings}
    if len(current) != len(current_bindings):
        raise StoreError("TASKGRAPH_CANDIDATE_CURRENT_BINDINGS_DUPLICATE")
    planned = {(str(item.task_id), int(item.contract_revision)): item for item in command.task_bindings}
    if len(planned) != len(command.task_bindings):
        raise StoreError("TASKGRAPH_CANDIDATE_BINDING_DUPLICATE")
    rewrites = {str(item.binding.task_id): item for item in command.delta.binding_rewrites}
    if len(rewrites) != len(command.delta.binding_rewrites):
        raise StoreError("TASKGRAPH_CANDIDATE_REWRITE_DUPLICATE")
    for task_id, declared_rewrite in rewrites.items():
        original = current.get(task_id)
        if original is None or sha256_hex(original.to_json()) != declared_rewrite.expected_hash:
            raise StoreError("TASKGRAPH_CANDIDATE_REWRITE_SOURCE_CHANGED")
    bindings = []
    for candidate in command.network.task_bindings:
        key = (str(candidate.task_id), int(candidate.contract_revision))
        actual = current.get(key[0])
        rewrite = rewrites.get(key[0])
        if rewrite is not None:
            actual = rewrite.binding
        if actual is not None:
            if int(actual.contract_revision) != key[1]:
                raise StoreError("TASKGRAPH_CANDIDATE_BINDING_SOURCE_UNAVAILABLE")
        else:
            actual = planned.get(key)
            if actual is None or key[0] in existing_task_ids:
                raise StoreError("TASKGRAPH_CANDIDATE_BINDING_SOURCE_UNAVAILABLE")
        offered, persisted = candidate.to_json(), actual.to_json()
        if canonical_json(offered) != canonical_json(persisted):
            offered_adoption = offered.pop("adopted_method_instance_id", None)
            persisted.pop("adopted_method_instance_id", None)
            adoptions = {
                str(draft.instance_id) for draft in command.network.method_instances
                if str(draft.goal_id) == str(candidate.task_id)
                and draft.instance_id in command.network.adopted_instance_ids
            }
            if canonical_json(offered) != canonical_json(persisted) or offered_adoption not in adoptions:
                raise StoreError("TASKGRAPH_CANDIDATE_BINDING_BYTES_MISMATCH")
        bindings.append(actual)
    frozen_network = replace(command.network, task_bindings=tuple(bindings))
    candidate_document = encode(frozen_network, requirements_ref)
    if int(command.delta.base_plan_revision) == 0:
        if before is not None:
            raise StoreError("TASKGRAPH_SEED_BASE_MISMATCH")
        return candidate_document
    if before is None or before.revision != int(command.delta.base_plan_revision):
        raise StoreError("TASKGRAPH_CANDIDATE_BASE_REQUIRED")
    impact = compute_convergence_impact(before, candidate_document)
    surviving = {str(item.task_id) for item in bindings}
    if not set(rewrites).issubset(surviving):
        raise StoreError("TASKGRAPH_CANDIDATE_REWRITE_MEMBER_MISSING")
    replacements = {}
    from .plan_commits import _superseded
    for target in impact.targets:
        current_binding = current.get(target.task_id)
        if current_binding is None or int(current_binding.dispatch_generation) != target.expected_generation:
            raise StoreError("TASKGRAPH_CANDIDATE_CONTROL_STALE")
        if target.task_id in surviving:
            # H4 supplies the exact control rewrite the original Commit writes.
            # Do not apply a second increment or discard its input/owner update.
            rewrite = rewrites.get(target.task_id)
            replacements[target.task_id] = rewrite.binding if rewrite is not None else _superseded(current_binding)
    final = encode(replace(frozen_network, task_bindings=tuple(
        replacements.get(str(item.task_id), item) for item in bindings)), requirements_ref)
    final_impact = compute_convergence_impact(before, final)
    if final_impact.targets != impact.targets or final_impact.preserved_occurrences != impact.preserved_occurrences:
        raise StoreError("TASKGRAPH_CANDIDATE_REVOCATION_IMPACT_CHANGED")
    return final
