# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Same-statement epoch barriers for the audited original source tables.

SQLite triggers execute within each original writer's Store transaction. They
do not evaluate truth, grant access, or schedule work. Global invalidation needs
the original Commit writer's explicit receipt context, never a generated grant.
"""

from ..assurance.event_kinds import SOURCE_EVENT_SQL
from .assurance_source_inventory import (
    CROSS_MISSION_KEYS,
    GLOBAL_TABLES,
    MISSION_TABLES,
    SOURCE_COLUMNS,
)

_SCOPE = "assurance:mission"


def _mission_barrier(table: str, expression: str) -> str:
    return f"""
 SELECT CASE WHEN EXISTS(
  SELECT 1 FROM assurance_mission_bindings b WHERE b.mission_id IN ({expression})
   AND NOT EXISTS(SELECT 1 FROM validity_epochs e
     WHERE e.mission_id=b.mission_id AND e.scope_id='{_SCOPE}'))
 THEN RAISE(ABORT,'ASSURANCE_MISSION_EPOCH_UNINITIALIZED') END;
 UPDATE validity_epochs SET epoch=epoch+1,bumped_by='assurance-source:{table}',
  updated_at=CAST(strftime('%s','now') AS REAL)
 WHERE scope_id='{_SCOPE}' AND mission_id IN ({expression})
 AND mission_id IN (SELECT mission_id FROM assurance_mission_bindings);
 INSERT INTO events(event_id,idempotency_key,type,trace_id,mission_id,task_id,attempt_id,
  actor_type,actor_id,payload_json,created_at,schema_version)
 SELECT 'assurance-mission-epoch:'||e.mission_id||':'||e.epoch,
  'assurance-mission-epoch:'||e.mission_id||':'||e.epoch,'AssuranceEvidenceChanged',
  'assurance-mission-epoch:'||e.mission_id||':'||e.epoch,e.mission_id,NULL,NULL,
  'system','assurance-source-v1',
  json_object('scope','MISSION','epoch',e.epoch,'source_table','{table}'),
  CAST(strftime('%s','now') AS REAL),1
 FROM validity_epochs e JOIN assurance_mission_bindings b ON b.mission_id=e.mission_id
 WHERE e.scope_id='{_SCOPE}' AND e.mission_id IN ({expression});
"""


def _global_barrier() -> str:
    return """
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM assurance_environment_state WHERE singleton=1)
 THEN RAISE(ABORT,'ASSURANCE_ENVIRONMENT_UNINITIALIZED') END;
 SELECT CASE WHEN assurance_change_receipt() IS NULL
 THEN RAISE(ABORT,'SOURCE_CHANGE_RECEIPT_REQUIRED') END;
 UPDATE assurance_environment_state SET epoch=epoch+1,row_version=row_version+1,
  change_receipt_id=assurance_change_receipt() WHERE singleton=1;
 INSERT INTO events(event_id,idempotency_key,type,trace_id,mission_id,task_id,attempt_id,
  actor_type,actor_id,payload_json,created_at,schema_version)
 SELECT 'assurance-environment-epoch:'||b.mission_id||':'||e.epoch,
  'assurance-environment-epoch:'||b.mission_id||':'||e.epoch,'AssuranceEvidenceChanged',
  'assurance-environment-epoch:'||b.mission_id||':'||e.epoch,b.mission_id,NULL,NULL,
  'system','assurance-source-v1',
  json_object('scope','GLOBAL','epoch',e.epoch,'source_receipt_id',e.change_receipt_id),
  CAST(strftime('%s','now') AS REAL),1
 FROM assurance_mission_bindings b CROSS JOIN assurance_environment_state e
 WHERE e.singleton=1;
"""


def _build() -> str:
    statements = [
        f"""
CREATE TRIGGER assurance_import_event_no_update BEFORE UPDATE ON events
WHEN OLD.type IN ({SOURCE_EVENT_SQL}) OR NEW.type IN ({SOURCE_EVENT_SQL})
BEGIN SELECT RAISE(ABORT,'immutable Assurance source event'); END;
CREATE TRIGGER assurance_import_event_no_delete BEFORE DELETE ON events
WHEN OLD.type IN ({SOURCE_EVENT_SQL})
BEGIN SELECT RAISE(ABORT,'retain Assurance source event'); END;
CREATE TRIGGER assurance_import_event_no_replace BEFORE INSERT ON events
WHEN EXISTS(SELECT 1 FROM events prior
 WHERE (prior.type IN ({SOURCE_EVENT_SQL}) OR NEW.type IN ({SOURCE_EVENT_SQL}))
 AND (prior.seq=NEW.seq OR prior.event_id=NEW.event_id
      OR prior.idempotency_key=NEW.idempotency_key))
BEGIN SELECT RAISE(ABORT,'duplicate Assurance source event; replay original receipt'); END;
"""
    ]
    for table in (
        *MISSION_TABLES,
        *GLOBAL_TABLES,
        "input_manifests",
        "verifications",
        "validity_epochs",
        "events",
    ):
        columns = SOURCE_COLUMNS[table]
        # Ignore only generic write timestamps: they are not evidence time,
        # query watermarks, validity bounds, or lifecycle metadata.
        changed = " OR ".join(
            f"NEW.{column} IS NOT OLD.{column}" for column in columns if column != "updated_at"
        )
        for operation in ("INSERT", "UPDATE", "DELETE"):
            images = (
                ("NEW", "OLD")
                if operation == "UPDATE"
                else ("OLD" if operation == "DELETE" else "NEW",)
            )
            condition = f"({changed})" if operation == "UPDATE" else "1"
            if table == "events":
                # Invalidation/clock/diagnostic wakeups never invalidate their
                # own generation. Only the fixed imported source bridges do.
                condition += (
                    " AND ("
                    + " OR ".join(f"{image}.type IN ({SOURCE_EVENT_SQL})" for image in images)
                    + ")"
                )
            if table == "artifacts" and operation == "UPDATE":
                unchanged = " AND ".join(
                    f"NEW.{column} IS OLD.{column}" for column in columns if column != "json"
                )
                condition += (
                    " AND NOT (assurance_offline_relocation()=1 AND "
                    + unchanged
                    + " AND assurance_same_artifact_content(OLD.json,NEW.json)=1)"
                )
            if table == "validity_epochs":
                condition += " AND " + " AND ".join(
                    f"{image}.scope_id<>'{_SCOPE}'" for image in images
                )
            if table in GLOBAL_TABLES:
                condition += " AND EXISTS(SELECT 1 FROM assurance_mission_bindings)"
                body = _global_barrier()
            else:
                if table == "input_manifests":
                    hashes = ",".join(f"{image}.manifest_hash" for image in images)
                    origins = " UNION ".join(
                        f"SELECT {image}.origin_mission_id" for image in images
                    )
                    expression = origins + (
                        " UNION SELECT mission_id FROM input_manifest_bindings"
                        f" WHERE manifest_hash IN ({hashes})"
                    )
                elif table == "verifications":
                    expression = (
                        "SELECT mission_id FROM results WHERE result_id IN ("
                        + ",".join(f"{image}.result_id" for image in images)
                        + ")"
                    )
                else:
                    expression = ",".join(f"{image}.mission_id" for image in images)
                body = _mission_barrier(table, expression)
            statements.append(
                f"CREATE TRIGGER assurance_source_{table}_{operation.lower()} "
                f"AFTER {operation} ON {table} WHEN {condition} BEGIN {body} END;"
            )
        # Prevent a replacement relocating an existing globally unique record
        # between Missions while recursive_triggers is OFF. Same-Mission
        # REPLACE is covered by INSERT; normal UPSERT remains usable.
        if table in MISSION_TABLES or table == "events":
            # All unique constraints, not just the primary key, are enumerated
            # below; these are original-schema identities, not model input.
            for ordinal, keys in enumerate(CROSS_MISSION_KEYS.get(table, ())):
                identity = " AND ".join(f"prior.{key}=NEW.{key}" for key in keys)
                statements.append(f"""
CREATE TRIGGER assurance_source_{table}_relocation_{ordinal} BEFORE INSERT ON {table}
WHEN EXISTS(SELECT 1 FROM {table} prior WHERE {identity}
 AND prior.mission_id<>NEW.mission_id AND (
 EXISTS(SELECT 1 FROM assurance_mission_bindings b WHERE b.mission_id=prior.mission_id)
 OR EXISTS(SELECT 1 FROM assurance_mission_bindings b WHERE b.mission_id=NEW.mission_id)))
BEGIN SELECT RAISE(ABORT,'ASSURANCE_SOURCE_RELOCATION_FORBIDDEN'); END;
""")
    return "\n".join(statements)


DDL = _build()
