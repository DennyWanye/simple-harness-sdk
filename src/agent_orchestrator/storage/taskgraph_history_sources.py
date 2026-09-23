# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Verify exact immutable HTN sources used by a structural document.

This is a read-only integrity helper for the future TaskGraphStore reader. It
does not authorize a caller, verify a TaskGraph certificate, or establish that
the document was committed. A successful check is not a history/readiness API.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import NoReturn

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
from ..contracts.resolution import RequirementsRevision
from ..graph.network_codec import NetworkDocumentV1, decode
from .store import Store


def _invalid(reason: str) -> NoReturn:
    # Keep foreign row identities and raw database content out of diagnostics.
    raise ContractError(f"TASKGRAPH_SOURCE_INTEGRITY: {reason}")


def _exact_rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...],
    expected: list[tuple[object, ...]],
    label: str,
) -> None:
    actual = [tuple(row) for row in connection.execute(query, parameters).fetchall()]
    # Comparison includes row multiplicity; never deduplicate a damaged source.
    if sorted(actual) != sorted(expected):
        _invalid(f"{label} source set is missing, extra, or inconsistent")


def validate_revision_sources(store: Store, document: NetworkDocumentV1) -> None:
    """Check original source rows in one Store read transaction, without writes.

    Exact task binding revisions and frozen drafts are used. Current Task state,
    method retirement, active revision and latest requirements are irrelevant.
    The caller must still validate the revision record, pins, parent chain,
    policy, certificate, event and receipt before exposing historical structure.
    """
    if not isinstance(store, Store) or not isinstance(document, NetworkDocumentV1):
        _invalid("a Store and NetworkDocumentV1 are required")
    network = decode(document.to_json()).snapshot
    mission = document.mission_id
    revision = document.revision
    with store.read_view() as connection:
        if (
            connection.execute(
                "SELECT 1 FROM plan_revisions WHERE mission_id=? AND revision=?",
                (mission, revision),
            ).fetchone()
            is None
        ):
            _invalid("the exact original plan revision is unavailable")

        _exact_rows(
            connection,
            "SELECT occurrence_id,task_id,obligation_id,form,member_json FROM plan_memberships "
            "WHERE mission_id=? AND revision=?",
            (mission, revision),
            [
                (
                    str(item.occurrence_id),
                    str(item.task_id),
                    str(item.obligation_id),
                    str(item.form),
                    canonical_json(item.to_json()),
                )
                for item in network.occurrences
            ],
            "memberships",
        )
        for binding in network.task_bindings:
            _exact_rows(
                connection,
                "SELECT mission_id,obligation_id,form,content_hash,binding_json "
                "FROM task_semantics WHERE task_id=? AND binding_revision=?",
                (str(binding.task_id), int(binding.contract_revision)),
                [
                    (
                        mission,
                        str(binding.obligation_id),
                        str(binding.form),
                        binding.content_hash(),
                        canonical_json(binding.to_json()),
                    )
                ],
                "task binding",
            )

        for draft in network.method_instances:
            _exact_rows(
                connection,
                "SELECT goal_task_id,goal_occurrence_id,obligation_id,draft_json "
                "FROM method_instances WHERE mission_id=? AND instance_id=?",
                (mission, str(draft.instance_id)),
                [
                    (
                        str(draft.goal_id),
                        str(draft.effective_goal_occurrence_id),
                        str(draft.obligation_id),
                        canonical_json(draft.to_json()),
                    )
                ],
                "method draft",
            )
            _exact_rows(
                connection,
                "SELECT slot_key,occurrence_id,goal_occurrence_id,obligation_id,requiredness,"
                "reuse_policy,binding_json FROM method_child_occurrences "
                "WHERE mission_id=? AND instance_id=?",
                (mission, str(draft.instance_id)),
                [
                    (
                        child.slot_key,
                        str(child.occurrence_id),
                        str(
                            child.occurrence_id
                            if child.goal_occurrence_id is None
                            else child.goal_occurrence_id
                        ),
                        str(child.obligation_id),
                        str(child.requiredness),
                        str(child.reuse_policy),
                        canonical_json(child.to_json()),
                    )
                    for child in draft.child_bindings
                ],
                "method children",
            )

        _exact_rows(
            connection,
            "SELECT before_occurrence,after_occurrence,release_condition,constraint_json "
            "FROM order_constraints WHERE mission_id=? AND plan_revision=?",
            (mission, revision),
            [
                (
                    str(item.before),
                    str(item.after),
                    str(item.release_condition),
                    canonical_json(item.to_json()),
                )
                for item in network.order_constraints
            ],
            "ORDER",
        )
        _exact_rows(
            connection,
            "SELECT requirement_id,producer_occurrence,consumer_occurrence,requirement_json "
            "FROM data_requirements WHERE mission_id=? AND plan_revision=?",
            (mission, revision),
            [
                (
                    item.requirement_id,
                    str(item.producer_occurrence),
                    str(item.consumer_occurrence),
                    canonical_json(item.to_json()),
                )
                for item in network.data_requirements
            ],
            "DATA",
        )

        reference = document.requirements_ref
        row = connection.execute(
            "SELECT revision_id,content_hash,revision_json FROM requirements_revisions "
            "WHERE mission_id=? AND revision=?",
            (mission, reference.revision),
        ).fetchone()
        if row is None or row[0] != reference.id or row[1] != reference.content_hash:
            _invalid("the exact requirements source identity is unavailable")
        if hashlib.sha256(row[2].encode("utf-8")).hexdigest() != reference.content_hash:
            _invalid("requirements source bytes do not match their digest")
        try:
            requirements = RequirementsRevision.from_json(json.loads(row[2]))
        except (ContractError, ValueError, TypeError):
            _invalid("requirements source does not decode")
        if (
            requirements.mission_id != mission
            or requirements.revision != reference.revision
            or str(requirements.revision_id) != reference.id
            or canonical_json(requirements.to_json()) != row[2]
        ):
            _invalid("requirements source fields do not match their frozen identity")


__all__ = ("validate_revision_sources",)
