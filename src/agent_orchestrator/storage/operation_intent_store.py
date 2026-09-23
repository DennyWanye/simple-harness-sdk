# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Immutable T0 bindings, stored on the original orchestration connection."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from simple_harness.contracts import canonical_json

from .store import Store, StoreConflict


class OperationIntentStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, intent_id: str) -> dict[str, Any] | None:
        row = self.store.connection.execute(
            "SELECT * FROM operation_intent_bindings WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["binding"] = json.loads(result["binding_json"])
        return result

    def for_mission(self, mission_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.store.connection.execute(
            "SELECT intent_id FROM operation_intent_bindings WHERE mission_id=? ORDER BY intent_id",
            (mission_id,),
        ).fetchall()
        return tuple(value for row in rows if (value := self.get(str(row[0]))) is not None)

    def insert(self, values: Mapping[str, Any]) -> None:
        if self.store._reading or self.store._depth < 1:
            raise StoreConflict("operation intent requires the caller's write transaction")
        # The column list comes from the registered migration, never from a command.
        columns = tuple(
            str(row[1])
            for row in self.store.connection.execute("PRAGMA table_info(operation_intent_bindings)")
        )
        if set(values) != set(columns):
            raise StoreConflict("operation intent columns differ from the durable contract")
        binding = json.loads(str(values["binding_json"]))
        if canonical_json(binding) != values["binding_json"]:
            raise StoreConflict("operation intent binding must be canonical JSON")
        receipt = self.store.get_receipt(str(values["submission_receipt_id"]))
        if (
            receipt is None
            or any(
                receipt.get(key) != values[key]
                for key in (
                    "intent_id",
                    "mission_id",
                    "tenant_id",
                    "principal_id",
                    "submission_hash",
                )
            )
            or receipt.get("kind") != "operation_intent_submitted"
        ):
            raise StoreConflict("operation intent has no exact submission receipt")
        existing = self.get(str(values["intent_id"]))
        if existing is not None:
            if any(existing[key] != values[key] for key in columns):
                raise StoreConflict("immutable operation intent conflict")
            return
        self.store.connection.execute(
            "INSERT INTO operation_intent_bindings("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(values[key] for key in columns),
        )


__all__ = ("OperationIntentStore",)
