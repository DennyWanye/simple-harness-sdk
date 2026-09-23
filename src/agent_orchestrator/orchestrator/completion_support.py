# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Exact current completion supports shared by composition producers and commits."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts.evidence_state import Validity
from ..contracts.htn import TaskForm
from ..contracts.resolution import ReviewPurpose, ReviewVerdict
from ..contracts.semantic_base import Provenance, TypedRef, TypedRefKind, content_hash_of
from ..storage.htn_store import HtnStore
from ..storage.operation_completion_store import OperationCompletionStore
from ..storage.store import Store, StoreError
from .completion_status import _official_accept_review, read_occurrence_completion


@dataclass(frozen=True, slots=True)
class CompletionSupport:
    ref: TypedRef
    record: Any
    package: Any


def read_completion_support(store: Store, mission_id: str, support_id: str) -> CompletionSupport:
    htn = HtnStore(store)
    source: Any
    try:
        source = htn.get_acceptance(support_id)
        kind = TypedRefKind.ACCEPTANCE
        review_id = source.review_record_id
    except StoreError:
        source = htn.get_goal_resolution(support_id)
        kind = TypedRefKind.RESOLUTION
        review_id = source.review_receipt_id
    if source.mission_id != mission_id or source.validity is not Validity.CURRENT:
        raise StoreError("completion support is not current in this Mission")
    stored = htn.get_review_record(str(review_id))
    package = htn.get_review_package(str(stored.record.package_id))
    official = htn.official_review_record(str(package.package_id))
    if (
        not stored.official
        or official != stored.record
        or official.verdict is not ReviewVerdict.ACCEPT
    ):
        raise StoreError("completion support has no official accepted review")
    return CompletionSupport(
        TypedRef(
            kind, support_id, 1, content_hash_of(source.to_json()), produced_by=Provenance.TOOL
        ),
        stored.record,
        package,
    )


def current_child_supports(
    store: Store, mission_id: str, children: Sequence[Any]
) -> dict[str, tuple[str, ...]]:
    """Return only fully completed occurrences in their current immutable Scopes."""
    from ..contracts.htn import ObligationId
    from ..storage.obligation_store import ObligationStore

    htn = HtnStore(store)
    completion = OperationCompletionStore(store)
    duties = ObligationStore(store)
    found: dict[str, tuple[str, ...]] = {}
    for child in children:
        duty = ObligationId(str(child.obligation_id))
        if not duties.exists(mission_id, duty):
            continue
        if str(duties.account(mission_id, duty).lifecycle) in {"CANCELLED", "SUPERSEDED"}:
            continue
        pinned_resolution = getattr(child, "resolution_ref", None)
        status = read_occurrence_completion(store, mission_id, str(child.occurrence_id))
        if not status.complete:
            continue
        scope = status.scope
        semantics = htn.task_semantics_of(mission_id, scope.task_ref.id)
        if semantics is None:
            continue
        if pinned_resolution is not None:
            try:
                pinned = htn.get_goal_resolution(pinned_resolution.id)
            except StoreError:
                continue
            if (pinned.mission_id != mission_id or pinned.goal_task_id != scope.task_ref.id
                    or pinned.obligation_id != scope.obligation_id
                    or pinned.contract_revision != scope.task_ref.revision
                    or pinned.requirements_version != scope.requirements_ref.revision
                    or content_hash_of(pinned.to_json()) != pinned_resolution.content_hash
                    or pinned.validity is not Validity.CURRENT or pinned.verdict is not ReviewVerdict.ACCEPT):
                continue
        if semantics.form is TaskForm.PRIMITIVE:
            accepted: list[str] = []
            for row in completion.retained_content_contributions(scope):
                contribution = row["document"]
                purpose = (
                    ReviewPurpose.OPERATION_OUTCOME
                    if contribution.effect_keys
                    else ReviewPurpose.TASK_CONTENT
                )
                if contribution.spec_hash == scope.spec_hash and _official_accept_review(
                    htn,
                    acceptance_id=contribution.acceptance_id,
                    mission_id=mission_id,
                    task_id=scope.task_ref.id,
                    requirements_revision=scope.requirements_ref.revision,
                    requirements_hash=scope.requirements_ref.content_hash,
                    contract_revision=scope.task_ref.revision,
                    purpose=purpose,
                ):
                    accepted.append(contribution.acceptance_id)
            if accepted:
                found[str(child.occurrence_id)] = tuple(sorted(accepted))
        else:
            resolutions = tuple(
                item
                for item in htn.list_goal_resolutions(mission_id)
                if item.goal_task_id == scope.task_ref.id
                and item.obligation_id == scope.obligation_id
                and item.requirements_version == scope.requirements_ref.revision
                and item.contract_revision == scope.task_ref.revision
                and item.validity is Validity.CURRENT
                and item.verdict is ReviewVerdict.ACCEPT
                and (pinned_resolution is None or str(item.resolution_id) == pinned_resolution.id)
            )
            if len(resolutions) == 1:
                found[str(child.occurrence_id)] = (str(resolutions[0].resolution_id),)
    return found
