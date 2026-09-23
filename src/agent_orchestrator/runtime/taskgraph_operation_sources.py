# SPDX-License-Identifier: Apache-2.0
"""Read the original immutable Operation-to-producer links without ID inference."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict

from ..contracts.models import sha256_hex
from ..storage.store import Store
from .planning_operations import OperationSnapshot, SourceUnavailable


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationProducer:
    operation_id: str
    task_id: str
    occurrence_id: str
    contract_revision: int
    plan_revision: int
    link_hash: str


def read_operation_producers(store: Store, snapshot: OperationSnapshot) -> tuple[OperationProducer, ...]:
    with store.read_view() as db:
        rows = db.execute("SELECT * FROM planning_operation_action_links WHERE mission_id=? ORDER BY operation_id",
                          (snapshot.mission_id,)).fetchall()
        if {row['operation_id'] for row in rows} != {link.operation_id for link in snapshot.links}:
            raise SourceUnavailable("taskgraph_operation_links_changed")
        original = {link.operation_id: asdict(link) for link in snapshot.links}
        producers = []
        for row in rows:
            body = json.loads(row['link_json'])
            if (not isinstance(body, dict) or any(body.get(key) != value for key, value in dict(row).items()
                    if key != 'link_json')
                    or any(body.get(key) != value for key, value in original[row['operation_id']].items())):
                raise SourceUnavailable("taskgraph_operation_link_identity_mismatch")
            content = dict(body)
            digest = content.pop('link_hash', None)
            if digest != sha256_hex(content):
                raise SourceUnavailable("taskgraph_operation_link_hash_mismatch")
            if (not body.get('producer_task_id') or not body.get('producer_htn_occurrence_id')
                    or not body.get('provenance_receipt_id')
                    or type(body.get('producer_contract_revision')) is not int
                    or type(body.get('producer_plan_revision')) is not int):
                raise SourceUnavailable("taskgraph_operation_producer_missing")
            task = store.get_task(body['producer_task_id'])
            if task is None or task.mission_id != snapshot.mission_id:
                raise SourceUnavailable("taskgraph_operation_producer_mismatch")
            producers.append(OperationProducer(operation_id=row['operation_id'], task_id=task.id,
                occurrence_id=body['producer_htn_occurrence_id'],
                contract_revision=body['producer_contract_revision'], plan_revision=body['producer_plan_revision'],
                link_hash=digest))
        return tuple(producers)
