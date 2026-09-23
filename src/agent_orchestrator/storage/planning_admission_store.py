# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Persistence helpers for H1-H admission seams.

This module is intentionally a thin Store adapter.  It owns no execution state;
in particular, operation/action links contain only immutable identity facts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any

from .store import Store, StoreConflict


class PlanningAdmissionStore:
    def __init__(self, store: Store) -> None:
        self._store = store

    def _tx(self):
        # Callers such as ActionCommitsMixin already own the write transaction.  A
        # direct API call still gets the same atomicity without opening a nested
        # SQLite transaction.
        return (
            nullcontext(self._store.connection) if self._store._depth else self._store.transaction()
        )

    # ---------------------------------------------------------- planning authority
    @staticmethod
    def _grant_row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        payload = json.loads(value.pop("grant_json"))
        value["allowed_decisions"] = tuple(json.loads(value.pop("allowed_decisions_json")))
        value["planner_principal_id"] = value["grantee_id"]
        value["grant_hash"] = value["content_hash"]
        value.update({key: payload[key] for key in ("command_hash", "reason") if key in payload})
        value["created_at"] = payload.get("created_at", 0.0)
        return value

    def get_grant(self, grant_id: str, revision: int | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM planning_lane_grants WHERE grant_id = ?"
        args: list[Any] = [str(grant_id)]
        if revision is not None:
            query += " AND revision = ?"
            args.append(int(revision))
        query += " ORDER BY revision DESC LIMIT 1"
        return self._grant_row(self._store.connection.execute(query, tuple(args)).fetchone())

    def get_grant_by_command(self, command_id: str) -> dict[str, Any] | None:
        return self._grant_row(
            self._store.connection.execute(
                "SELECT * FROM planning_lane_grants WHERE issuer_command_id = ?", (str(command_id),)
            ).fetchone()
        )

    def get_request_binding(self, request_id: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT * FROM planning_request_authority_bindings WHERE request_id = ?",
            (str(request_id),),
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        payload = json.loads(value.pop("binding_json"))
        # The typed columns are the authoritative identity.  ``binding_json`` is
        # audit detail and may add metadata, but must never shadow a column read.
        value.update({key: item for key, item in payload.items() if key not in value})
        return value

    # ----------------------------------------------------------- operation reads
    # These read adapters keep operation identity SQL in the storage boundary.
    # Callers may use them inside their own Store transaction; no nested write or
    # transaction is opened for a read.
    def get_operation_identity(self, operation_id: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT mission_id,request_hash,envelope_hash FROM operation_identities "
            "WHERE operation_id=?",
            (str(operation_id),),
        ).fetchone()
        return None if row is None else dict(row)

    def get_operation_binding(self, operation_occurrence_id: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT operation_id,request_hash,mission_id,operation_occurrence_id,"
            "principal_id,scope_id,obligation_id,envelope_hash FROM operation_bindings "
            "WHERE operation_occurrence_id=?",
            (str(operation_occurrence_id),),
        ).fetchone()
        return None if row is None else dict(row)

    def list_operation_bindings(self, mission_id: str) -> list[dict[str, Any]]:
        rows = self._store.connection.execute(
            "SELECT mission_id,operation_id,operation_occurrence_id,request_hash,"
            "envelope_hash,principal_id,scope_id,obligation_id FROM operation_bindings "
            "WHERE mission_id=? ORDER BY operation_id,operation_occurrence_id",
            (str(mission_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_operation_actions(self, mission_id: str) -> list[dict[str, Any]]:
        rows = self._store.connection.execute(
            "SELECT action_key,json FROM actions WHERE mission_id=? ORDER BY created_at,action_key",
            (str(mission_id),),
        ).fetchall()
        return [json.loads(row[1]) for row in rows]

    def put_grant(
        self,
        grant: Mapping[str, Any],
        *,
        request_id: str | None = None,
        binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        required = (
            "grant_id",
            "mission_id",
            "tenant_id",
            "scope_id",
            "revision",
            "active",
            "allowed_decisions",
            "policy_hash",
            "not_before_ms",
            "expires_at_ms",
            "issuer_id",
            "issuer_command_id",
            "issuer_receipt_hash",
            "grant_hash",
        )
        missing = [key for key in required if key not in grant]
        if missing:
            raise StoreConflict(f"planning grant missing fields: {', '.join(missing)}")
        planner = str(grant.get("planner_principal_id", grant.get("grantee_id", "")))
        command_hash = str(grant.get("command_hash", ""))
        payload = {
            "command_hash": command_hash,
            "reason": str(grant.get("reason", "")),
            "created_at": float(grant.get("created_at", self._store.now)),
        }
        values = (
            str(grant["grant_id"]),
            int(grant["revision"]),
            str(grant["mission_id"]),
            str(grant["tenant_id"]),
            str(grant["scope_id"]),
            planner,
            str(grant["issuer_id"]),
            str(grant["issuer_command_id"]),
            str(grant["issuer_receipt_hash"]),
            int(bool(grant["active"])),
            json.dumps(list(grant["allowed_decisions"]), separators=(",", ":")),
            str(grant["policy_hash"]),
            int(grant["not_before_ms"]),
            int(grant["expires_at_ms"]),
            str(grant["grant_hash"]),
            __import__("simple_harness.contracts", fromlist=["canonical_json"]).canonical_json(
                payload
            ),
        )
        with self._tx() as connection:
            if request_id is not None:
                request = connection.execute(
                    "SELECT p.mission_id,m.tenant_id FROM planning_requests p "
                    "JOIN missions m ON m.mission_id=p.mission_id WHERE p.request_id=?",
                    (str(request_id),),
                ).fetchone()
                if (
                    request is None
                    or request["mission_id"] != str(grant["mission_id"])
                    or request["tenant_id"] != str(grant["tenant_id"])
                ):
                    raise StoreConflict("planning request is not available to this caller")
            existing = connection.execute(
                "SELECT * FROM planning_lane_grants WHERE issuer_command_id = ?",
                (str(grant["issuer_command_id"]),),
            ).fetchone()
            if existing is not None:
                old = self._grant_row(existing)
                if old is not None and old.get("command_hash") == command_hash:
                    # Repeat issuer isolation inside the write transaction: a
                    # competing issue can insert after the API's initial lookup.
                    if any(
                        str(old[key]) != str(grant[key])
                        for key in ("tenant_id", "issuer_id", "mission_id")
                    ):
                        raise StoreConflict("planning grant is not available to this caller")
                    return old
                raise StoreConflict("planning grant command id was reused with different input")
            try:
                connection.execute(
                    "INSERT INTO planning_lane_grants("
                    "grant_id,revision,mission_id,tenant_id,scope_id,grantee_id,issuer_id,"
                    "issuer_command_id,issuer_receipt_hash,active,allowed_decisions_json,"
                    "policy_hash,not_before_ms,expires_at_ms,content_hash,grant_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            except Exception as error:
                raise StoreConflict(f"planning grant conflict: {error}") from error
            if request_id is not None:
                if binding is None:
                    raise StoreConflict("request authority binding is required")
                self._insert_binding(connection, request_id, binding, planner)
            from .taskgraph_source_events import record_source_change
            record_source_change(self._store, str(grant["mission_id"]), kind="planning_grant",
                source_id=str(grant["grant_id"]), revision=int(grant["revision"]),
                content_hash=str(grant["grant_hash"]))
        return self._grant_row(
            self._store.connection.execute(
                "SELECT * FROM planning_lane_grants WHERE grant_id = ? AND revision = ?",
                (str(grant["grant_id"]), int(grant["revision"])),
            ).fetchone()
        ) or dict(grant)

    @staticmethod
    def _insert_binding(
        connection: Any, request_id: str, binding: Mapping[str, Any], planner: str
    ) -> None:
        from simple_harness.contracts import canonical_json

        payload = {"request_id": request_id, **dict(binding)}
        import hashlib

        bind_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        connection.execute(
            "INSERT INTO planning_request_authority_bindings("
            "request_id,mission_id,tenant_id,scope_id,planner_principal_id,grant_id,"
            "grant_revision,grant_hash,binding_hash,binding_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                binding["mission_id"],
                binding["tenant_id"],
                binding["scope_id"],
                planner,
                binding["grant_id"],
                int(binding["grant_revision"]),
                binding["grant_hash"],
                bind_hash,
                canonical_json(payload),
            ),
        )

    def bind_request(
        self,
        *,
        request_id: str,
        mission_id: str,
        tenant_id: str,
        scope_id: str,
        planner_principal_id: str,
        grant_id: str,
        grant_revision: int,
        grant_hash: str,
        policy_hash: str,
    ) -> dict[str, Any]:
        request = self._store.connection.execute(
            "SELECT mission_id FROM planning_requests WHERE request_id = ?", (str(request_id),)
        ).fetchone()
        if request is None:
            raise StoreConflict("planning request does not exist")
        grant = self.get_grant(grant_id, grant_revision)
        if grant is None or any(
            (
                grant["mission_id"] != mission_id,
                grant["tenant_id"] != tenant_id,
                grant["scope_id"] != scope_id,
                grant["planner_principal_id"] != planner_principal_id,
                grant["grant_hash"] != grant_hash,
                grant["policy_hash"] != policy_hash,
                str(request["mission_id"]) != str(mission_id),
            )
        ):
            raise StoreConflict("request and planning grant identity do not match")
        old = self.get_request_binding(request_id)
        desired = {
            "mission_id": mission_id,
            "tenant_id": tenant_id,
            "scope_id": scope_id,
            "planner_principal_id": planner_principal_id,
            "grant_id": grant_id,
            "grant_revision": int(grant_revision),
            "grant_hash": grant_hash,
            "policy_hash": policy_hash,
        }
        if old is not None:
            if all(old.get(key) == value for key, value in desired.items()):
                return old
            raise StoreConflict("planning request is already bound to another grant")
        with self._tx() as connection:
            self._insert_binding(
                connection,
                request_id,
                {**desired, "created_at": self._store.now},
                planner_principal_id,
            )
        return self.get_request_binding(request_id) or {}

    def put_operation_action_link(self, link: Mapping[str, Any]) -> dict[str, Any]:
        required = (
            "operation_id",
            "request_hash",
            "operation_occurrence_id",
            "mission_id",
            "envelope_hash",
            "principal_id",
            "scope_id",
            "obligation_id",
            "producer_task_id",
            "producer_htn_occurrence_id",
            "producer_contract_revision",
            "producer_plan_revision",
            "action_key",
            "action_id",
            "action_version",
            "params_hash",
            "idempotency_key",
            "provenance_receipt_id",
            "link_hash",
            "link_json",
        )
        missing = [name for name in required if name not in link]
        if missing:
            raise StoreConflict(f"operation action link lacks {missing}")
        values = tuple(link[name] for name in required)
        with self._tx() as connection:
            existing = connection.execute(
                "SELECT * FROM planning_operation_action_links WHERE operation_id = ?",
                (str(link["operation_id"]),),
            ).fetchone()
            if existing is not None:
                # Compare the persisted columns, including the serialized audit
                # body. That body deliberately does not contain itself.
                stored = {name: existing[name] for name in required}
                if stored != {name: link[name] for name in required}:
                    raise StoreConflict("operation_action_link_conflict")
                return stored
            for column, value in (
                ("operation_occurrence_id", link["operation_occurrence_id"]),
                ("action_key", link["action_key"]),
            ):
                row = connection.execute(
                    f"SELECT operation_id FROM planning_operation_action_links WHERE {column} = ?",
                    (str(value),),
                ).fetchone()
                if row is not None and str(row[0]) != str(link["operation_id"]):
                    raise StoreConflict("operation_occurrence_alias_conflict")
            connection.execute(
                "INSERT INTO planning_operation_action_links("
                + ",".join(required)
                + ") VALUES ("
                + ",".join("?" for _ in required)
                + ")",
                values,
            )
        return dict(link)

    def get_operation_action_link(self, operation_id: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT link_json FROM planning_operation_action_links WHERE operation_id = ?",
            (str(operation_id),),
        ).fetchone()
        return None if row is None else dict(json.loads(row[0]))

    def get_operation_action_link_for_action(self, action_key: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT link_json FROM planning_operation_action_links WHERE action_key = ?",
            (str(action_key),),
        ).fetchone()
        return None if row is None else dict(json.loads(row[0]))

    def list_operation_action_links(self, mission_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._store.connection.execute(
            "SELECT link_json FROM planning_operation_action_links "
            "WHERE mission_id = ? ORDER BY operation_id",
            (str(mission_id),),
        ).fetchall()
        return tuple(dict(json.loads(row[0])) for row in rows)

    def put_admission_check(self, check: Mapping[str, Any]) -> dict[str, Any]:
        fields = (
            "check_id",
            "request_id",
            "decision_id",
            "phase",
            "snapshot_hash",
            "decision_hash",
            "authority_hash",
            "operations_hash",
            "delta_hash",
            "check_schema",
            "detail_json",
            "checked_at_ms",
        )
        values = tuple(check.get(name) for name in fields)
        with self._tx() as connection:
            existing = connection.execute(
                "SELECT " + ",".join(fields) + " FROM planning_admission_checks WHERE check_id = ?",
                (str(check.get("check_id")),),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise StoreConflict("planning admission check conflict")
                return dict(check)
            connection.execute(
                "INSERT INTO planning_admission_checks("
                + ",".join(fields)
                + ") VALUES ("
                + ",".join("?" for _ in fields)
                + ") ON CONFLICT(check_id) DO NOTHING",
                values,
            )
        return dict(check)

    def list_admission_checks(self, request_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._store.connection.execute(
            "SELECT check_id,request_id,decision_id,phase,snapshot_hash,decision_hash,"
            "authority_hash,operations_hash,delta_hash,check_schema,detail_json,checked_at_ms "
            "FROM planning_admission_checks WHERE request_id = ? ORDER BY checked_at_ms, check_id",
            (str(request_id),),
        ).fetchall()
        fields = (
            "check_id",
            "request_id",
            "decision_id",
            "phase",
            "snapshot_hash",
            "decision_hash",
            "authority_hash",
            "operations_hash",
            "delta_hash",
            "check_schema",
            "detail_json",
            "checked_at_ms",
        )
        return tuple(dict(zip(fields, row, strict=True)) for row in rows)


__all__ = ("PlanningAdmissionStore",)
