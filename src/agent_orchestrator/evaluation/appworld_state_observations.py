# SPDX-License-Identifier: Apache-2.0
"""Deployment-owned AppWorld state predicates, independent of hidden scoring.

The service loads these declarations before creating a world. Requests can select
only a frozen policy hash; neither agent code nor receipt text supplies a query.
Only aggregate counts leave the service, never database rows or credentials.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .appworld_api_observations import canonical, digest

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True, slots=True)
class AppWorldStatePolicy:
    # Store canonical bytes so a caller cannot mutate a validated declaration.
    document: bytes

    @classmethod
    def from_json(cls, value: Any) -> AppWorldStatePolicy:
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "task_id", "instruction_sha256", "predicates"}:
            raise ValueError("invalid AppWorld state policy fields")
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["task_id"], str) or not value["task_id"]
                or not isinstance(value["instruction_sha256"], str)
                or not _HASH.fullmatch(value["instruction_sha256"])):
            raise ValueError("invalid AppWorld state policy identity")
        predicates = value["predicates"]
        if not isinstance(predicates, list) or not 1 <= len(predicates) <= 32:
            raise ValueError("state policy requires one to 32 explicit predicates")
        ids: set[str] = set()
        for predicate in predicates:
            if not isinstance(predicate, dict) or set(predicate) != {"id", "app", "model", "where", "count"}:
                raise ValueError("invalid state predicate")
            for key in ("id", "app", "model"):
                if not isinstance(predicate[key], str) or not _NAME.fullmatch(predicate[key]):
                    raise ValueError("invalid state predicate identifier")
            if predicate["id"] in ids:
                raise ValueError("duplicate state predicate")
            ids.add(predicate["id"])
            # Supervisor task status is an agent assertion, not business state.
            if predicate["app"] in {"supervisor", "admin"}:
                raise ValueError("task status cannot establish external effects")
            where = predicate["where"]
            if not isinstance(where, dict) or not 1 <= len(where) <= 24:
                raise ValueError("state queries require explicit bounded field filters")
            for key, item in where.items():
                if (not isinstance(key, str) or not _NAME.fullmatch(key) or "__" in key
                        or type(item) not in (str, int, float, bool, type(None))):
                    raise ValueError("state query supports literal field equality only")
            if type(predicate["count"]) is not int or not 0 <= predicate["count"] <= 100000:
                raise ValueError("state predicate requires an explicit exact count")
        raw = canonical(value)
        if len(raw) > 65536:
            raise ValueError("state policy exceeds size limit")
        return cls(raw)

    def to_json(self) -> dict[str, Any]:
        import json
        result: dict[str, Any] = json.loads(self.document)
        return result

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.document).hexdigest()

    def observe(self, active: Any) -> dict[str, Any]:
        policy = self.to_json()
        if (active.task_id != policy["task_id"]
                or hashlib.sha256(active.task.instruction.encode()).hexdigest() != policy["instruction_sha256"]):
            raise ValueError("state policy differs from the actual task instruction")
        if active.models is None:
            raise ValueError("state observation requires the service's actual model collection")
        observations = []
        for predicate in policy["predicates"]:
            if predicate["app"] not in active.task.allowed_apps:
                raise ValueError("state predicate app is outside the task")
            model = active.models[predicate["app"]][predicate["model"]]
            # AppWorld pins SQLModel/Pydantic v1; its public field reader also
            # excludes computed properties and relations from database filters.
            stored_fields = model.field_names(keep_computed=False, keep_relations=False)
            if not set(predicate["where"]) <= set(stored_fields):
                raise ValueError("state predicate contains unknown database fields")
            count = model.count(**predicate["where"])
            if type(count) is not int or count < 0:
                raise ValueError("invalid database count")
            observations.append({"id": predicate["id"], "count": count,
                                 "satisfied": count == predicate["count"]})
        return {"policy_hash": self.content_hash, "predicates": observations,
                "satisfied": all(item["satisfied"] for item in observations)}

    def validate_result(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict) or set(result) != {"policy_hash", "predicates", "satisfied"}:
            raise ValueError("invalid state observation")
        if result["policy_hash"] != self.content_hash or type(result["satisfied"]) is not bool:
            raise ValueError("state observation policy differs")
        rows = result["predicates"]
        predicates = self.to_json()["predicates"]
        if not isinstance(rows, list) or len(rows) != len(predicates):
            raise ValueError("state observation is incomplete")
        for row, expected in zip(rows, predicates, strict=True):
            if (not isinstance(row, dict) or set(row) != {"id", "count", "satisfied"}
                    or row["id"] != expected["id"] or type(row["count"]) is not int or row["count"] < 0
                    or type(row["satisfied"]) is not bool
                    or row["satisfied"] != (row["count"] == expected["count"])):
                raise ValueError("state observation predicate differs")
        if result["satisfied"] != all(row["satisfied"] for row in rows):
            raise ValueError("state observation conclusion differs")
        import json
        validated: dict[str, Any] = json.loads(canonical(result))
        return validated


def load_state_policies(path: str | None) -> Mapping[str, AppWorldStatePolicy]:
    if path is None:
        return {}
    import json
    from pathlib import Path
    value = json.loads(Path(path).read_bytes())
    if not isinstance(value, list) or not value:
        raise ValueError("state policy file must contain explicit policies")
    policies = [AppWorldStatePolicy.from_json(item) for item in value]
    registered = {item.content_hash: item for item in policies}
    if len(registered) != len(policies):
        raise ValueError("duplicate state policy registration")
    return registered


def task_dataset_hash(data_root: Any, task_id: str) -> str:
    """Hash public task inputs and base databases; never open hidden scoring files."""
    from pathlib import Path
    root = Path(data_root).resolve(strict=True)
    task = (root / "tasks" / task_id).resolve(strict=True)
    if task.parent != root / "tasks":
        raise ValueError("task dataset path is outside the actual dataset")
    files = [root / "version.txt", task / "specs.json"]
    for directory in (root / "base_dbs", task / "dbs", root / "api_docs" / "standard"):
        members = sorted(directory.iterdir())
        if not members:
            raise ValueError("actual task dataset source is missing")
        files.extend(path for path in members if path.is_file())
    entries = []
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
            raise ValueError("actual task dataset source is unavailable")
        with path.open("rb") as source:
            entries.append([path.relative_to(root).as_posix(), hashlib.file_digest(source, "sha256").hexdigest()])
    return digest(entries)


@dataclass(frozen=True, slots=True)
class AppWorldStateReceipt:
    receipt_id: str
    run_id: str
    task_id: str
    episode_id: str
    world_version: int
    service_identity: str
    policy_hash: str
    result_hash: str

    def matches(self, result: Mapping[str, Any]) -> bool:
        return self.result_hash == digest(result) and self.policy_hash == result.get("policy_hash")
