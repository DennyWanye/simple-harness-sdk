# SPDX-License-Identifier: Apache-2.0
"""Classify only Missions present during migration 26, from original facts.

The SQL UDF is pure. It cannot issue authority, rewrite contracts, or classify
post-upgrade missing rows on demand. Ambiguous/malformed old facts stay unbound.
"""

from __future__ import annotations

import sqlite3

from ..assurance.codec import AssuranceError, canonical, decode, fingerprint
from ..contracts import Event, Mission, ids
from ..contracts.models import ContractError
from ..contracts.planning_decisions import PLANNING_DECISION_V1


def _legacy_proof(
    mission_json: str, spec_hash: str, event_json: str, protocol_json: str | None
) -> str | None:
    try:
        mission = Mission.from_json(decode(mission_json))
        event = Event.from_json(decode(event_json))
        if (
            event.type != "MissionCreated"
            or event.mission_id != mission.id
            or event.actor_type != "user"
            or event.actor_id != mission.tenant_id
            or event.idempotency_key != f"MissionCreated:{mission.id}"
            or event.id != ids.event_id(event.idempotency_key)
            or event.payload.get("spec_hash") != spec_hash
        ):
            return None
        protocol = None if protocol_json is None else decode(protocol_json)
        if protocol is not None:
            from ..runtime.role_templates import hierarchical_planner_pairing_is_valid

            frozen = {
                key: protocol[key]
                for key in ("protocol_version", "package_version", "prompt_version")
            }
            if (
                protocol["protocol_version"] != PLANNING_DECISION_V1
                or fingerprint(frozen) != protocol["binding_hash"]
                or not hierarchical_planner_pairing_is_valid(
                    protocol["prompt_version"], protocol["package_version"]
                )
            ):
                return None
        source_hash = fingerprint(
            {"creation_event": event.to_json(), "planning_protocol": protocol}
        )
        return canonical(
            {
                "schema_version": 1,
                "receipt_role": "MIGRATION_CLASSIFICATION_ONLY",
                "mission_id": mission.id,
                "tenant_id": mission.tenant_id,
                "lane": "LEGACY" if protocol is None else "COMPLETION_V1",
                "creation_event_id": event.id,
                "creation_event_hash": fingerprint(event.to_json()),
                "planning_protocol": protocol,
                "source_hash": source_hash,
            }
        )
    except (AssuranceError, ContractError, KeyError, TypeError, ValueError, AttributeError):
        return None


def install_upgrade_functions(connection: sqlite3.Connection) -> None:
    connection.create_function("assurance_legacy_proof", 4, _legacy_proof, deterministic=True)
    connection.create_function(
        "assurance_json_hash", 1, lambda value: fingerprint(decode(value)), deterministic=True
    )


# All inputs are captured by the migration's original transaction. The temporary
# staging table prevents selecting unrelated historical receipts by kind alone.
# Missing classification after migration can NEVER be inferred as legacy later.
DDL = """
CREATE TEMP TABLE assurance_legacy_classification_stage AS
 SELECT m.mission_id,
 'assurance-classify:'||lower(hex(randomblob(16))) AS receipt_id,
 assurance_legacy_proof(m.json,m.spec_hash,
  json_object('id',e.event_id,'type',e.type,'trace_id',e.trace_id,
   'mission_id',e.mission_id,'task_id',e.task_id,'attempt_id',e.attempt_id,
   'actor_type',e.actor_type,'actor_id',e.actor_id,'payload',json(e.payload_json),
   'idempotency_key',e.idempotency_key,'created_at',e.created_at,
   'schema_version',e.schema_version,'seq',e.seq),
  CASE WHEN p.mission_id IS NULL THEN NULL ELSE json_object(
   'protocol_version',p.protocol_version,'package_version',p.package_version,
   'prompt_version',p.prompt_version,'binding_hash',p.binding_hash) END) AS proof_json
 FROM missions m JOIN events e ON e.mission_id=m.mission_id AND e.type='MissionCreated'
 LEFT JOIN mission_planning_protocols p ON p.mission_id=m.mission_id
 WHERE (SELECT count(*) FROM events other
   WHERE other.mission_id=m.mission_id AND other.type='MissionCreated')=1;
INSERT INTO commit_receipts(commit_id,kind,subject_id,base_version,
 proposal_hash,receipt_json,applied_at)
 SELECT receipt_id,'AssuranceLegacyClassified',mission_id,NULL,
  assurance_json_hash(proof_json),proof_json,CAST(strftime('%s','now') AS REAL)
 FROM assurance_legacy_classification_stage WHERE proof_json IS NOT NULL;
INSERT INTO assurance_creation_contracts(mission_id,lane,origin,source_hash,
 receipt_id,created_at_ms)
 SELECT mission_id,json_extract(proof_json,'$.lane'),'MIGRATION_CLASSIFICATION',
  json_extract(proof_json,'$.source_hash'),receipt_id,CAST(strftime('%s','now') AS INTEGER)*1000
 FROM assurance_legacy_classification_stage WHERE proof_json IS NOT NULL;
DROP TABLE assurance_legacy_classification_stage;
"""
