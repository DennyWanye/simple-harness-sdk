# SPDX-License-Identifier: Apache-2.0
"""Managed-root identity and current read grants; never execution resurrection.

The protected state file is deliberately outside the backup's file inventory.
It points to an original Commit receipt. Neither a backup nor a database receipt
alone opens this gate. Current external ACL checks remain mandatory on each read.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..artifacts.store import open_nofollow
from ..governance.permissions import Principal
from .codec import AssuranceError, array, decode, digest, fields, fingerprint, integer, text
from .evidence import ReadItem
from .refs import AssuranceRef

if TYPE_CHECKING:
    from ..storage.store import Store

STATE_FILE = "assurance-root-state.json"
ROOT_KINDS = ("AssuranceRootInstalled", "AssuranceRestoredReadAuthorized")


@dataclass(frozen=True, slots=True)
class RootIdentity:
    root_incarnation_id: str
    restore_manifest_hash: str | None


@dataclass(frozen=True, slots=True)
class CurrentReadPermission:
    """Result of a deployed CURRENT authority, not something decoded from a request."""

    access: ReadItem
    policy: ReadItem
    not_after_ms: int

    def __post_init__(self) -> None:
        if self.access.channel != "ACCESS" or self.policy.channel != "POLICY":
            raise AssuranceError("ACCESS_POLICY_WITNESS_REQUIRED")
        integer(self.not_after_ms)


def _read(path: Path, *, protected: bool = False) -> bytes:
    try:
        with open_nofollow(path) as stream:
            if protected and stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & 0o077:
                raise AssuranceError("ROOT_STATE_PERMISSIONS")
            data = stream.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise AssuranceError("ROOT_MANIFEST_LIMIT")
        return data
    except (OSError, ValueError) as error:
        if isinstance(error, AssuranceError):
            raise
        raise AssuranceError("ROOT_QUARANTINED") from error


class AssuranceRootGate:
    def __init__(self, store: Store, root: Path) -> None:
        self.store = store
        self.root = Path(root).absolute()
        self._restore_integrity_checked = False
        if (
            self.root.is_symlink()
            or store.path.resolve() != (self.root / "orchestrator.db").resolve()
        ):
            raise AssuranceError("ROOT_STORE_MISMATCH")

    @staticmethod
    def required(store: Store, root: Path) -> bool:
        # Legacy roots remain on their old path until the authenticated Assurance
        # installer is invoked. A managed restore or existing Assurance root may
        # never opt out merely by omitting its deployment collaborator.
        return (
            any(
                (root / name).exists() or (root / name).is_symlink()
                for name in (
                    STATE_FILE,
                    "restore-quarantine.json",
                    "restore-manifest.json",
                )
            )
            or store.connection.execute(
                "SELECT 1 FROM assurance_mission_bindings UNION ALL SELECT 1 FROM commit_receipts "
                "WHERE kind IN ('AssuranceRootInstalled','AssuranceRestoredReadAuthorized') LIMIT 1"
            ).fetchone()
            is not None
        )

    def restored_identity(self) -> RootIdentity:
        marker = fields(
            decode(_read(self.root / "restore-quarantine.json")),
            {
                "schema_version",
                "root_incarnation_id",
                "restore_manifest_hash",
                "state",
            },
        )
        if integer(marker["schema_version"]) != 1 or marker["state"] != "QUARANTINED":
            raise AssuranceError("ROOT_QUARANTINED")
        raw = _read(self.root / "restore-manifest.json")
        manifest_hash = hashlib.sha256(raw).hexdigest()
        if digest(marker["restore_manifest_hash"]) != manifest_hash:
            raise AssuranceError("ROOT_MANIFEST_MISMATCH")
        from .codec import _pairs

        try:
            manifest = json.loads(raw, object_pairs_hook=_pairs)
        except (ValueError, UnicodeError) as error:
            raise AssuranceError("ROOT_MANIFEST_INVALID") from error
        root_id = text(marker["root_incarnation_id"])
        if (
            not isinstance(manifest, dict)
            or manifest.get("root_incarnation_id") != root_id
            or manifest.get("assurance_state") != "QUARANTINED"
            or manifest.get("runtime_started") is not False
            or manifest.get("protocol") != "orchestrator-offline-backup-v1"
        ):
            raise AssuranceError("ROOT_MANIFEST_MISMATCH")
        inventory = manifest.get("database_inventory")
        if (
            not isinstance(inventory, list)
            or not inventory
            or len(inventory) > 256
            or any(not isinstance(name, str) for name in inventory)
            or not isinstance(manifest.get("formal_fingerprints"), dict)
            or set(manifest["formal_fingerprints"]) != set(inventory)
            or "orchestrator.db" not in inventory
            or len(inventory) != len(set(inventory))
        ):
            raise AssuranceError("RESTORE_DATABASE_INVENTORY_MISSING")
        for name in inventory:
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or not name.endswith(".db")
                or (self.root / name).is_symlink()
                or not (self.root / name).is_file()
            ):
                raise AssuranceError("RESTORE_DATABASE_INCOMPLETE")
        contexts = manifest.get("context_sidecars")
        if not isinstance(contexts, dict) or len(contexts) > len(inventory):
            raise AssuranceError("RESTORE_CONTEXT_INVENTORY_MISSING")
        for name, expected in contexts.items():
            if (
                not name.endswith(".db.context.json")
                or name.removesuffix(".context.json") not in inventory
                or hashlib.sha256(_read(self.root / name)).hexdigest() != digest(expected)
            ):
                raise AssuranceError("RESTORE_CONTEXT_INCOMPLETE")
        return RootIdentity(root_id, manifest_hash)

    def check_restored_integrity(self) -> None:
        """Cold startup / grant preparation only; never scan databases on every read."""
        self._restore_integrity_checked = False
        self.restored_identity()
        manifest = json.loads(_read(self.root / "restore-manifest.json"))
        try:
            for name in manifest["database_inventory"]:
                connection = sqlite3.connect((self.root / name).as_uri() + "?mode=ro", uri=True)
                try:
                    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                        raise AssuranceError("RESTORE_DATABASE_INTEGRITY")
                finally:
                    connection.close()
        except sqlite3.Error as error:
            raise AssuranceError("RESTORE_DATABASE_INTEGRITY") from error
        self._restore_integrity_checked = True

    def _head_locked(self, root_id: str):
        # Current root authorization head, NOT an exact-object/latest resolver.
        return self.store.connection.execute(
            "SELECT rowid,* FROM commit_receipts WHERE kind IN (?,?) "
            "AND json_extract(receipt_json,'$.root_incarnation_id')=? "
            "AND json_extract(receipt_json,'$.root_path')=? ORDER BY rowid DESC LIMIT 1",
            (*ROOT_KINDS, root_id, str(self.root)),
        ).fetchone()

    def _state_locked(self) -> tuple[dict, dict]:
        state = fields(
            decode(_read(self.root / STATE_FILE, protected=True)),
            {
                "schema_version",
                "root_incarnation_id",
                "receipt_ref",
            },
        )
        if integer(state["schema_version"]) != 1:
            raise AssuranceError("ROOT_STATE_INVALID")
        ref = AssuranceRef.from_json(state["receipt_ref"], kinds={"commit_receipt"})
        row = self._head_locked(text(state["root_incarnation_id"]))
        if row is None or row["commit_id"] != ref.pin.id or ref.pin.revision != 0:
            raise AssuranceError("ROOT_QUARANTINED")
        common = {
            "schema_version",
            "mode",
            "root_path",
            "root_incarnation_id",
            "restore_manifest_hash",
            "tenant_id",
            "principal_id",
            "command_id",
            "issued_at_ms",
        }
        extra = (
            {"request_hash", "requested_targets", "targets", "ttl_ms", "not_after_ms"}
            if row["kind"] == ROOT_KINDS[1]
            else {"installation_origin"}
        )
        body = fields(decode(row["receipt_json"]), common | extra)
        if (
            fingerprint(body) != ref.pin.content_hash
            or row["proposal_hash"] != fingerprint(body)
            or row["subject_id"] != state["root_incarnation_id"]
            or row["base_version"] != 0
            or integer(body["schema_version"]) != 1
            or body.get("root_incarnation_id") != state["root_incarnation_id"]
            or body.get("root_path") != str(self.root)
        ):
            raise AssuranceError("ROOT_STATE_MISMATCH")
        for key in ("tenant_id", "principal_id", "command_id"):
            text(body[key])
        issued = integer(body["issued_at_ms"])
        if row["kind"] == "AssuranceRestoredReadAuthorized":
            ttl = integer(body["ttl_ms"], minimum=1, maximum=86_400_000)
            if (
                body["mode"] != "READ_ONLY_REAUTHORIZED"
                or not issued < integer(body["not_after_ms"]) <= issued + ttl
            ):
                raise AssuranceError("ROOT_STATE_MISMATCH")
            requested = array(body["requested_targets"], minimum=1)
            targets = array(body["targets"], minimum=1)
            if len(targets) != len(requested):
                raise AssuranceError("ROOT_STATE_MISMATCH")
            for target, request in zip(targets, requested, strict=True):
                fields(request, {"mission_id", "ref", "purpose"})
                fields(target, {"mission_id", "ref", "purpose", "access", "policy"})
                if any(target[key] != request[key] for key in request):
                    raise AssuranceError("ROOT_STATE_MISMATCH")
                text(target["mission_id"])
                AssuranceRef.from_json(target["ref"])
                if (
                    target["purpose"] not in {"DISCLOSE", "CONTEXT"}
                    or ReadItem.from_json(target["access"]).channel != "ACCESS"
                    or ReadItem.from_json(target["policy"]).channel != "POLICY"
                ):
                    raise AssuranceError("ROOT_STATE_MISMATCH")
            request_body = {
                key: body[key]
                for key in (
                    "root_path",
                    "root_incarnation_id",
                    "restore_manifest_hash",
                    "tenant_id",
                    "principal_id",
                    "command_id",
                    "requested_targets",
                    "ttl_ms",
                )
            }
            if fingerprint(request_body) != digest(body["request_hash"]):
                raise AssuranceError("ROOT_STATE_MISMATCH")
            identity = self.restored_identity()
            if (
                body.get("restore_manifest_hash") != identity.restore_manifest_hash
                or body["root_incarnation_id"] != identity.root_incarnation_id
            ):
                raise AssuranceError("ROOT_STATE_MISMATCH")
        else:
            if (
                body["mode"] != "NATIVE_INSTALLATION"
                or body["restore_manifest_hash"] is not None
                or body["installation_origin"] not in {"EMPTY", "HISTORICAL"}
                or any(
                    (self.root / name).exists() or (self.root / name).is_symlink()
                    for name in ("restore-manifest.json", "restore-quarantine.json")
                )
            ):
                raise AssuranceError("ROOT_QUARANTINED")
        return dict(row), body

    def require_execution(self) -> RootIdentity:
        with self.store.read_view():
            row, body = self._state_locked()
            if row["kind"] != "AssuranceRootInstalled":
                raise AssuranceError("RESTORED_EXECUTION_REQUIRES_RECOVERY")
            return RootIdentity(body["root_incarnation_id"], None)

    def require_read(
        self,
        *,
        principal: Principal,
        tenant_id: str,
        mission_id: str,
        ref: AssuranceRef,
        purpose: str,
        current: CurrentReadPermission,
        now_ms: int,
    ) -> ReadItem:
        if (
            not isinstance(principal, Principal)
            or purpose not in {"DISCLOSE", "CONTEXT"}
            or not isinstance(current, CurrentReadPermission)
        ):
            raise AssuranceError("CURRENT_READ_AUTHORITY_REQUIRED")
        integer(now_ms)
        if now_ms >= current.not_after_ms:
            raise AssuranceError("READ_AUTHORITY_EXPIRED")
        if not self._restore_integrity_checked and (self.root / "restore-manifest.json").exists():
            self.check_restored_integrity()
        with self.store.read_view():
            row, body = self._state_locked()
            mission = self.store.get_mission(mission_id)
            if mission is None or mission.tenant_id != tenant_id:
                raise AssuranceError("ROOT_READ_NOT_AUTHORIZED")
            if row["kind"] == "AssuranceRestoredReadAuthorized":
                target = {
                    "mission_id": mission_id,
                    "ref": ref.to_json(),
                    "purpose": purpose,
                    "access": current.access.to_json(),
                    "policy": current.policy.to_json(),
                }
                if (
                    body.get("tenant_id") != tenant_id
                    or body.get("principal_id") != principal.principal_id
                    or now_ms < body["issued_at_ms"]
                    or now_ms >= body["not_after_ms"]
                    or target not in body["targets"]
                ):
                    raise AssuranceError("ROOT_READ_NOT_AUTHORIZED")
                clock = self.store.connection.execute(
                    "SELECT clock_state,wall_high_ms FROM assurance_environment_state "
                    "WHERE singleton=1"
                ).fetchone()
                if clock is None or clock[0] != "STABLE" or now_ms < clock[1]:
                    raise AssuranceError("TIME_DISCONTINUITY")
            return ReadItem(
                "ACCESS",
                current.access.key,
                fingerprint(
                    {
                        "current_access": current.access.to_json(),
                        "current_policy": current.policy.to_json(),
                        "root_receipt_hash": fingerprint(body),
                        "root_incarnation_id": body["root_incarnation_id"],
                        "principal": principal.principal_id,
                        "tenant": tenant_id,
                        "mission": mission_id,
                        "ref": ref.to_json(),
                        "purpose": purpose,
                    }
                ),
            )

    def diagnostic(self) -> dict:
        # No titles, summaries, source paths, object ids or evidence contents.
        try:
            with self.store.read_view():
                row, body = self._state_locked()
                if row["kind"] == ROOT_KINDS[1]:
                    if not self._restore_integrity_checked:
                        raise AssuranceError("RESTORE_INTEGRITY_NOT_CHECKED")
                    now = integer(int(self.store.now * 1000))
                    clock = self.store.connection.execute(
                        "SELECT clock_state,wall_high_ms FROM assurance_environment_state "
                        "WHERE singleton=1"
                    ).fetchone()
                    if (
                        not body["issued_at_ms"] <= now < body["not_after_ms"]
                        or clock is None
                        or clock[0] != "STABLE"
                        or now < clock[1]
                    ):
                        raise AssuranceError("ROOT_READ_GRANT_EXPIRED")
            state = "READ_ONLY_REAUTHORIZED" if row["kind"] == ROOT_KINDS[1] else "NATIVE"
        except AssuranceError:
            state = "QUARANTINED"
        return {
            "state": state,
            "execution_allowed": state == "NATIVE",
            "current_authentication_required": state == "QUARANTINED",
        }
