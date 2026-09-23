# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Unified Capability / Provider / Deployment / Tool / Skill catalogue (§8, HOST-DTOS §5).

One namespace (``realm/owner/project``) has exactly one managed catalogue writer; its
durable state lives in ``arp_catalog_revisions`` (immutable definition bodies),
``arp_catalog_activation`` (the lifecycle row per revision, QUARANTINED → TRIAL →
ADMITTED ⇄ SUSPENDED → RETIRED, enforced by triggers), ``arp_registry_epoch`` (advances
exactly once per semantic change), ``arp_deployment_health`` (immutable probe
snapshots, their own revision line) and ``arp_catalogue_mounts``.

``CatalogueService`` is that writer's typed entry (register / transition / probe /
page); ``CapabilityResolver`` selects one deployment by hard filter then approved
priority; ``ToolExposureService`` freezes the tools a request may see into a
``ToolSnapshot``. None of them takes a caller from a payload: the ``TrustedCaller``
comes from the authenticated dispatcher or from the runtime itself.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import store
from .codec import check
from .errors import ArpError
from .pins import Pin
from .ports import TrustedCaller
from .store import _json_column, _load, require_transaction
from .strict import canonical, digest, plain

ENTRY_KINDS = ("CAPABILITY", "PROVIDER", "DEPLOYMENT", "TOOL", "SKILL", "SCHEMA", "WORKFLOW")
ENTRY_SCHEMA = {"CAPABILITY": "Capability", "PROVIDER": "ProviderDefinition", "DEPLOYMENT": "DeploymentDefinition", "TOOL": "Tool", "SKILL": "Skill"}
PIN_KIND = {"CAPABILITY": "capability", "PROVIDER": "provider", "DEPLOYMENT": "deployment", "TOOL": "tool", "SKILL": "skill", "SCHEMA": "schema", "WORKFLOW": "workflow"}
STATES = ("QUARANTINED", "TRIAL", "ADMITTED", "SUSPENDED", "RETIRED")
TRANSITIONS = {
    "QUARANTINED": ("TRIAL", "RETIRED"),
    "TRIAL": ("ADMITTED", "SUSPENDED", "RETIRED"),
    "ADMITTED": ("SUSPENDED", "RETIRED"),
    "SUSPENDED": ("TRIAL", "ADMITTED", "RETIRED"),
    "RETIRED": (),
}
PAGE_ITEMS_MAX = 64
PAGE_BYTES_MAX = 65536
INVOCATION_PROVIDER_KINDS = {
    "MODEL": ("MODEL",),
    "TOOL": ("TOOL",),
    "SKILL": ("TOOL", "SCRIPT_RUNNER"),
    "WORKFLOW": ("WORKFLOW",),
    "CONTROLLER": ("MODEL", "TOOL", "SCRIPT_RUNNER", "WORKFLOW", "EMBEDDING"),
}


# ---- rows ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RevisionRow:
    namespace_id: str
    entry_kind: str
    entry_id: str
    revision: int
    content_hash: str
    body: Mapping[str, Any]
    source_receipt_ref: Pin
    bundle_root_ref: Pin | None

    @property
    def pin(self) -> Pin:
        return Pin(PIN_KIND[self.entry_kind], self.entry_id, self.revision, self.content_hash)


@dataclass(frozen=True, slots=True)
class ActivationRow:
    namespace_id: str
    entry_kind: str
    entry_id: str
    revision: int
    content_hash: str
    state: str
    row_version: int
    authority_ref: Pin
    evaluation_ref: Pin | None
    scope_ref: Pin

    @property
    def pin(self) -> Pin:
        return Pin(PIN_KIND[self.entry_kind], self.entry_id, self.revision, self.content_hash)

    @property
    def witness(self) -> Pin:
        """The receipt-kind witness of this lifecycle row (manifest ``catalogue_witness_refs``)."""

        return Pin("receipt", f"activation:{self.namespace_id}:{self.entry_kind}:{self.entry_id}:{self.revision}", self.row_version, digest(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "namespace_id": self.namespace_id, "entry_kind": self.entry_kind, "entry_id": self.entry_id, "revision": self.revision,
            "content_hash": self.content_hash, "state": self.state, "row_version": self.row_version,
            "authority_ref": self.authority_ref.to_json(), "evaluation_ref": None if self.evaluation_ref is None else self.evaluation_ref.to_json(),
            "scope_ref": self.scope_ref.to_json(),
        }


@dataclass(frozen=True, slots=True)
class HealthRow:
    namespace_id: str
    deployment_id: str
    definition_revision: int
    health_revision: int
    snapshot_hash: str
    body: Mapping[str, Any]
    probe_receipt_ref: Pin


@dataclass(frozen=True, slots=True)
class MountRow:
    namespace_id: str
    catalogue_owner_root_id: str
    realm_scope_ref: Pin
    mount_receipt_ref: Pin


_REV_COLUMNS = "namespace_id,entry_kind,entry_id,revision,content_hash,body_json,source_receipt_ref_json,bundle_root_ref_json"
_ACT_COLUMNS = "namespace_id,entry_kind,entry_id,revision,content_hash,state,row_version,authority_ref_json,evaluation_ref_json,scope_ref_json"
_HEALTH_COLUMNS = "namespace_id,deployment_id,definition_revision,health_revision,snapshot_hash,body_json,probe_receipt_ref_json"


def _rev(raw: Any) -> RevisionRow:
    return RevisionRow(
        str(raw[0]), str(raw[1]), str(raw[2]), int(raw[3]), str(raw[4]), _load(raw[5]), Pin.from_json(_load(raw[6])),
        None if raw[7] is None else Pin.from_json(_load(raw[7])),
    )


def _act(raw: Any) -> ActivationRow:
    return ActivationRow(
        str(raw[0]), str(raw[1]), str(raw[2]), int(raw[3]), str(raw[4]), str(raw[5]), int(raw[6]), Pin.from_json(_load(raw[7])),
        None if raw[8] is None else Pin.from_json(_load(raw[8])), Pin.from_json(_load(raw[9])),
    )


def _health(raw: Any) -> HealthRow:
    return HealthRow(str(raw[0]), str(raw[1]), int(raw[2]), int(raw[3]), str(raw[4]), _load(raw[5]), Pin.from_json(_load(raw[6])))


def _pin_column(pin: Pin | None) -> str | None:
    return None if pin is None else _json_column(pin.to_json())


# ---- store: revisions --------------------------------------------------------------


def validate_entry(entry_kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
    if entry_kind not in ENTRY_KINDS:
        raise ArpError("ENUM", field_path="entry_kind")
    schema = ENTRY_SCHEMA.get(entry_kind)
    if schema is not None:
        return check(schema, plain(body))
    value = plain(body)
    if not isinstance(value, dict) or not value:
        raise ArpError("STATE_COMBINATION_INVALID", f"{entry_kind} body must be a non-empty object")
    if entry_kind == "WORKFLOW":
        raise ArpError("WORKFLOW_UNAVAILABLE", "no workflow executor is deployed on this plane")
    return value


def put_revision_locked(
    connection: sqlite3.Connection,
    *,
    namespace_id: str,
    entry_kind: str,
    entry_id: str,
    revision: int,
    body: Mapping[str, Any],
    source_receipt_ref: Pin,
    bundle_root_ref: Pin | None = None,
) -> RevisionRow:
    """Immutable definition body; same identity + same hash replays, another body conflicts."""

    require_transaction(connection)
    value = validate_entry(entry_kind, body)
    content_hash = digest(value)
    existing = read_revision(connection, namespace_id, entry_kind, entry_id, revision)
    if existing is not None:
        if existing.content_hash != content_hash:
            raise ArpError("SOURCE_HASH_CONFLICT", f"{entry_kind} {entry_id}@{revision} already has another body")
        return existing
    connection.execute(
        f"INSERT INTO arp_catalog_revisions({_REV_COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
        (namespace_id, entry_kind, entry_id, int(revision), content_hash, _json_column(value), _pin_column(source_receipt_ref), _pin_column(bundle_root_ref)),
    )
    return RevisionRow(namespace_id, entry_kind, entry_id, int(revision), content_hash, value, source_receipt_ref, bundle_root_ref)


def read_revision(connection: sqlite3.Connection, namespace_id: str, entry_kind: str, entry_id: str, revision: int) -> RevisionRow | None:
    raw = connection.execute(
        f"SELECT {_REV_COLUMNS} FROM arp_catalog_revisions WHERE namespace_id=? AND entry_kind=? AND entry_id=? AND revision=?",
        (namespace_id, entry_kind, entry_id, int(revision)),
    ).fetchone()
    return None if raw is None else _rev(raw)


def resolve_pin(connection: sqlite3.Connection, namespace_id: str, pin: Pin) -> RevisionRow:
    """Exact resolution of a catalogue pin: kind, id, revision and hash must all match."""

    kinds = [k for k, v in PIN_KIND.items() if v == pin.kind]
    if not kinds:
        raise ArpError("REF_KIND_MISMATCH", field_path="kind")
    row = read_revision(connection, namespace_id, kinds[0], pin.id, pin.revision)
    if row is None:
        raise ArpError("REF_OUTSIDE_SCOPE", f"{pin.kind} {pin.id}@{pin.revision} is not in namespace {namespace_id}")
    if row.content_hash != pin.content_hash:
        raise ArpError("REF_IDENTITY_MISMATCH", f"{pin.kind} {pin.id}@{pin.revision} hash differs")
    return row


def latest_revision(connection: sqlite3.Connection, namespace_id: str, entry_kind: str, entry_id: str) -> RevisionRow | None:
    raw = connection.execute(
        f"SELECT {_REV_COLUMNS} FROM arp_catalog_revisions WHERE namespace_id=? AND entry_kind=? AND entry_id=? ORDER BY revision DESC LIMIT 1",
        (namespace_id, entry_kind, entry_id),
    ).fetchone()
    return None if raw is None else _rev(raw)


def list_latest(connection: sqlite3.Connection, namespace_id: str, entry_kind: str) -> tuple[RevisionRow, ...]:
    rows = connection.execute(
        f"SELECT {_REV_COLUMNS} FROM arp_catalog_revisions r WHERE namespace_id=? AND entry_kind=?"
        " AND revision=(SELECT MAX(revision) FROM arp_catalog_revisions x WHERE x.namespace_id=r.namespace_id AND x.entry_kind=r.entry_kind AND x.entry_id=r.entry_id)"
        " ORDER BY entry_id",
        (namespace_id, entry_kind),
    ).fetchall()
    return tuple(_rev(r) for r in rows)


# ---- store: activation -------------------------------------------------------------


def read_activation(connection: sqlite3.Connection, namespace_id: str, entry_kind: str, entry_id: str, revision: int) -> ActivationRow | None:
    raw = connection.execute(
        f"SELECT {_ACT_COLUMNS} FROM arp_catalog_activation WHERE namespace_id=? AND entry_kind=? AND entry_id=? AND revision=?",
        (namespace_id, entry_kind, entry_id, int(revision)),
    ).fetchone()
    return None if raw is None else _act(raw)


def put_activation_locked(connection: sqlite3.Connection, *, revision: RevisionRow, authority_ref: Pin, scope_ref: Pin) -> ActivationRow:
    """Every revision starts QUARANTINED (trigger-enforced); an existing row replays."""

    require_transaction(connection)
    authority_ref.require_kind("authority")
    scope_ref.require_kind("scope")
    existing = read_activation(connection, revision.namespace_id, revision.entry_kind, revision.entry_id, revision.revision)
    if existing is not None:
        return existing
    connection.execute(
        f"INSERT INTO arp_catalog_activation({_ACT_COLUMNS}) VALUES (?,?,?,?,?,'QUARANTINED',1,?,NULL,?)",
        (revision.namespace_id, revision.entry_kind, revision.entry_id, revision.revision, revision.content_hash, _pin_column(authority_ref), _pin_column(scope_ref)),
    )
    return ActivationRow(revision.namespace_id, revision.entry_kind, revision.entry_id, revision.revision, revision.content_hash, "QUARANTINED", 1, authority_ref, None, scope_ref)


def transition_activation_locked(
    connection: sqlite3.Connection,
    current: ActivationRow,
    *,
    state: str,
    authority_ref: Pin,
    evaluation_ref: Pin | None = None,
) -> ActivationRow:
    """One lifecycle step under CAS; illegal steps are named before SQLite aborts them."""

    require_transaction(connection)
    if state not in STATES:
        raise ArpError("ENUM", field_path="state")
    authority_ref.require_kind("authority")
    if evaluation_ref is not None:
        evaluation_ref.require_kind("evaluation")
    if current.state == "RETIRED":
        raise ArpError("STATE_COMBINATION_INVALID", "RETIRED is terminal")
    if state == current.state:
        return current
    if state not in TRANSITIONS[current.state]:
        raise ArpError("STATE_COMBINATION_INVALID", f"{current.state} → {state} is not a catalogue transition")
    if state == "ADMITTED" and current.entry_kind == "SKILL" and evaluation_ref is None:
        raise ArpError("SKILL_EVALUATION_INCOMPLETE", "a Skill is admitted only with an official evaluation")
    keep_eval = evaluation_ref if evaluation_ref is not None else current.evaluation_ref
    updated = connection.execute(
        "UPDATE arp_catalog_activation SET state=?, row_version=row_version+1, authority_ref_json=?, evaluation_ref_json=?"
        " WHERE namespace_id=? AND entry_kind=? AND entry_id=? AND revision=? AND row_version=?",
        (state, _pin_column(authority_ref), _pin_column(keep_eval), current.namespace_id, current.entry_kind, current.entry_id, current.revision, current.row_version),
    ).rowcount
    if updated != 1:
        raise ArpError("CATALOGUE_STALE", "activation row advanced elsewhere")
    row = read_activation(connection, current.namespace_id, current.entry_kind, current.entry_id, current.revision)
    assert row is not None
    return row


# ---- store: epoch / mounts / health -----------------------------------------------


def registry_epoch(connection: sqlite3.Connection, namespace_id: str) -> int:
    raw = connection.execute("SELECT epoch FROM arp_registry_epoch WHERE namespace_id=?", (namespace_id,)).fetchone()
    return 0 if raw is None else int(raw[0])


def advance_epoch_locked(connection: sqlite3.Connection, *, namespace_id: str, source_receipt_ref: Pin) -> int:
    """Exactly +1 (trigger-enforced); the receipt of the change that caused it is kept."""

    require_transaction(connection)
    source_receipt_ref.require_kind("receipt")
    current = connection.execute("SELECT epoch FROM arp_registry_epoch WHERE namespace_id=?", (namespace_id,)).fetchone()
    if current is None:
        connection.execute("INSERT INTO arp_registry_epoch(namespace_id,epoch,source_receipt_ref_json) VALUES (?,1,?)", (namespace_id, _pin_column(source_receipt_ref)))
        return 1
    epoch = int(current[0]) + 1
    connection.execute("UPDATE arp_registry_epoch SET epoch=?, source_receipt_ref_json=? WHERE namespace_id=? AND epoch=?", (epoch, _pin_column(source_receipt_ref), namespace_id, epoch - 1))
    return epoch


def read_mount(connection: sqlite3.Connection, namespace_id: str) -> MountRow | None:
    raw = connection.execute(
        "SELECT namespace_id,catalogue_owner_root_id,realm_scope_ref_json,mount_receipt_ref_json FROM arp_catalogue_mounts WHERE namespace_id=?", (namespace_id,)
    ).fetchone()
    return None if raw is None else MountRow(str(raw[0]), str(raw[1]), Pin.from_json(_load(raw[2])), Pin.from_json(_load(raw[3])))


def mount_locked(connection: sqlite3.Connection, *, namespace_id: str, catalogue_owner_root_id: str, realm_scope_ref: Pin, mount_receipt_ref: Pin) -> MountRow:
    require_transaction(connection)
    existing = read_mount(connection, namespace_id)
    if existing is not None:
        if existing.catalogue_owner_root_id != catalogue_owner_root_id or existing.realm_scope_ref != realm_scope_ref:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "namespace is mounted from another catalogue owner")
        return existing
    connection.execute(
        "INSERT INTO arp_catalogue_mounts(namespace_id,catalogue_owner_root_id,realm_scope_ref_json,mount_receipt_ref_json) VALUES (?,?,?,?)",
        (namespace_id, catalogue_owner_root_id, _pin_column(realm_scope_ref), _pin_column(mount_receipt_ref)),
    )
    return MountRow(namespace_id, catalogue_owner_root_id, realm_scope_ref, mount_receipt_ref)


def latest_health(connection: sqlite3.Connection, namespace_id: str, deployment_id: str, definition_revision: int) -> HealthRow | None:
    raw = connection.execute(
        f"SELECT {_HEALTH_COLUMNS} FROM arp_deployment_health WHERE namespace_id=? AND deployment_id=? AND definition_revision=? ORDER BY health_revision DESC LIMIT 1",
        (namespace_id, deployment_id, int(definition_revision)),
    ).fetchone()
    return None if raw is None else _health(raw)


def put_health_locked(connection: sqlite3.Connection, *, namespace_id: str, deployment_id: str, definition_revision: int, snapshot: Mapping[str, Any], probe_receipt_ref: Pin) -> HealthRow:
    require_transaction(connection)
    value = check("DeploymentHealthSnapshot", plain(snapshot))
    snapshot_hash = digest(value)
    connection.execute(
        f"INSERT INTO arp_deployment_health({_HEALTH_COLUMNS}) VALUES (?,?,?,?,?,?,?)",
        (namespace_id, deployment_id, int(definition_revision), int(value["health_revision"]), snapshot_hash, _json_column(value), _pin_column(probe_receipt_ref)),
    )
    return HealthRow(namespace_id, deployment_id, int(definition_revision), int(value["health_revision"]), snapshot_hash, value, probe_receipt_ref)


# ---- service ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogueScope:
    realm_id: str
    owner_id: str
    project_id: str | None
    catalogue_owner_root_id: str

    @property
    def namespace_id(self) -> str:
        return f"{self.realm_id}/{self.owner_id}/{self.project_id or '-'}"

    def to_json(self) -> dict[str, Any]:
        return check("CatalogueScope", {
            "realm_id": self.realm_id, "owner_id": self.owner_id, "project_id": self.project_id,
            "namespace_id": self.namespace_id, "catalogue_owner_root_id": self.catalogue_owner_root_id,
        })

    @property
    def pin(self) -> Pin:
        return Pin("scope", self.namespace_id, 0, digest(self.to_json()))


@dataclass(slots=True)
class CatalogueService:
    """The namespace's managed writer + read views (HOST-DTOS §5)."""

    uow: Any
    scope: CatalogueScope
    clock_ms: Callable[[], int]
    clock: Callable[[], float]

    @property
    def namespace_id(self) -> str:
        return self.scope.namespace_id

    @property
    def connection(self) -> sqlite3.Connection:
        return self.uow.database.connection

    # -- mount / epoch --

    def mount(self, *, mount_receipt_ref: Pin) -> MountRow:
        with self.uow.database.transaction() as connection:
            return mount_locked(connection, namespace_id=self.namespace_id, catalogue_owner_root_id=self.scope.catalogue_owner_root_id, realm_scope_ref=self.scope.pin, mount_receipt_ref=mount_receipt_ref)

    def epoch(self) -> int:
        return registry_epoch(self.connection, self.namespace_id)

    def _changed_locked(self, connection: sqlite3.Connection, *, entry: Pin | None, source_receipt_ref: Pin, run_id: str | None) -> int:
        epoch = advance_epoch_locked(connection, namespace_id=self.namespace_id, source_receipt_ref=source_receipt_ref)
        if run_id is not None:
            # The catalogue is namespace-level; its change event is bound to the original
            # Run of the command that caused it (a Host command Run or the calling Agent).
            store.append_runtime_event_locked(
                connection, run_id=run_id, event_type="RuntimeCatalogueChanged",
                body={"namespace_id": self.namespace_id, "entry_ref": None if entry is None else entry.to_json(), "semantic_epoch": epoch, "source_receipt_ref": source_receipt_ref.to_json()},
                source_receipt_ref=source_receipt_ref, dedupe_key=f"{self.namespace_id}:{epoch}", now=self.clock(),
            )
        return epoch

    # -- register / transition --

    def register(
        self,
        entry_kind: str,
        body: Mapping[str, Any],
        *,
        entry_id: str,
        caller: TrustedCaller,
        command_id: str,
        revision: int | None = None,
        bundle_root_ref: Pin | None = None,
        run_id: str | None = None,
    ) -> tuple[RevisionRow, ActivationRow]:
        """A new definition revision + its QUARANTINED lifecycle row + one epoch step (same txn)."""

        if not isinstance(caller, TrustedCaller):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "catalogue commands need an authenticated caller")
        value = validate_entry(entry_kind, body)
        with self.uow.database.transaction() as connection:
            latest = latest_revision(connection, self.namespace_id, entry_kind, entry_id)
            if revision is None and latest is not None and latest.content_hash == digest(value):
                # Same body re-registered (bootstrap replay, command re-send): the latest
                # revision *is* this definition; no new revision, no epoch step.
                activation = put_activation_locked(connection, revision=latest, authority_ref=self._authority(caller), scope_ref=self.scope.pin)
                return latest, activation
            target = int(revision) if revision is not None else (1 if latest is None else latest.revision + 1)
            receipt = Pin("receipt", f"catalogue:{command_id}", 0, digest({"command": command_id, "kind": entry_kind, "entry": entry_id, "revision": target, "caller": caller.to_json()}))
            existing = read_revision(connection, self.namespace_id, entry_kind, entry_id, target)
            row = put_revision_locked(connection, namespace_id=self.namespace_id, entry_kind=entry_kind, entry_id=entry_id, revision=target, body=body, source_receipt_ref=receipt, bundle_root_ref=bundle_root_ref)
            activation = put_activation_locked(connection, revision=row, authority_ref=self._authority(caller), scope_ref=self.scope.pin)
            if existing is None:
                self._changed_locked(connection, entry=row.pin, source_receipt_ref=receipt, run_id=run_id)
            return row, activation

    def transition(
        self,
        pin: Pin,
        *,
        state: str,
        caller: TrustedCaller,
        command_id: str,
        evaluation_ref: Pin | None = None,
        run_id: str | None = None,
    ) -> ActivationRow:
        if not isinstance(caller, TrustedCaller):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "catalogue commands need an authenticated caller")
        with self.uow.database.transaction() as connection:
            revision = resolve_pin(connection, self.namespace_id, pin)
            current = read_activation(connection, self.namespace_id, revision.entry_kind, revision.entry_id, revision.revision)
            if current is None:
                raise ArpError("CATALOGUE_STALE", "definition has no lifecycle row")
            if current.state == state:
                return current  # command re-sent: the same outcome, no second epoch
            if revision.entry_kind == "SKILL" and state in ("TRIAL", "ADMITTED"):
                lock = connection.execute(
                    "SELECT complete FROM arp_dependency_locks WHERE skill_id=? AND skill_revision=? ORDER BY rowid DESC LIMIT 1", (revision.entry_id, revision.revision)
                ).fetchone()
                if lock is None or int(lock[0]) != 1:
                    raise ArpError("DEPENDENCY_UNRESOLVED", f"skill {revision.entry_id}@{revision.revision} has no complete dependency lock (§9.5)")
            receipt = Pin("receipt", f"catalogue:{command_id}", 0, digest({"command": command_id, "pin": pin.to_json(), "state": state, "caller": caller.to_json()}))
            row = transition_activation_locked(connection, current, state=state, authority_ref=self._authority(caller, receipt), evaluation_ref=evaluation_ref)
            self._changed_locked(connection, entry=row.pin, source_receipt_ref=receipt, run_id=run_id)
            return row

    @staticmethod
    def _authority(caller: TrustedCaller, receipt: Pin | None = None) -> Pin:
        source = receipt or caller.command_receipt_ref
        return Pin("authority", f"catalogue:{caller.principal_ref.id}:{source.id}", source.revision, digest({"caller": caller.to_json(), "receipt": source.to_json()}))

    # -- health --

    def probe(
        self,
        deployment_pin: Pin,
        *,
        health: str,
        registered: bool,
        configured: bool,
        compatible: bool,
        ttl_ms: int,
        caller: TrustedCaller,
        command_id: str,
        run_id: str | None = None,
    ) -> HealthRow:
        """Import one probe result. Only a category change (READY↔not, registered /
        configured / compatible flips) advances the semantic epoch; a healthy→healthy
        refresh just extends the health line (§8, N15)."""

        with self.uow.database.transaction() as connection:
            revision = resolve_pin(connection, self.namespace_id, deployment_pin)
            previous = latest_health(connection, self.namespace_id, revision.entry_id, revision.revision)
            now = self.clock_ms()
            receipt = Pin("receipt", f"probe:{command_id}", 0, digest({"command": command_id, "deployment": deployment_pin.to_json(), "observed_at_ms": now, "health": health}))
            snapshot = {
                "schema_version": 1, "health": health, "registered": bool(registered), "configured": bool(configured), "compatible": bool(compatible),
                "observed_at_ms": now, "expires_at_ms": now + int(ttl_ms), "health_revision": 1 if previous is None else previous.health_revision + 1,
                "health_receipt_ref": receipt.to_json(), "deployment_ref": deployment_pin.to_json(),
            }
            row = put_health_locked(connection, namespace_id=self.namespace_id, deployment_id=revision.entry_id, definition_revision=revision.revision, snapshot=snapshot, probe_receipt_ref=receipt)
            category = (health == "READY", bool(registered), bool(configured), bool(compatible))
            before = None if previous is None else (previous.body["health"] == "READY", previous.body["registered"], previous.body["configured"], previous.body["compatible"])
            if before != category:
                self._changed_locked(connection, entry=revision.pin, source_receipt_ref=receipt, run_id=run_id)
            return row

    # -- usability --

    def usable(self, activation: ActivationRow, *, now_ms: int) -> tuple[bool, list[str]]:
        """Current usability of one revision with named reasons (never a guess)."""

        reasons: list[str] = []
        if activation.state != "ADMITTED":
            reasons.append(f"STATE_{activation.state}")
        connection = self.connection
        if activation.entry_kind == "DEPLOYMENT":
            health = latest_health(connection, self.namespace_id, activation.entry_id, activation.revision)
            if health is None:
                reasons.append("HEALTH_UNKNOWN")
            elif health.body["health"] != "READY" or not (health.body["registered"] and health.body["configured"] and health.body["compatible"]):
                reasons.append(f"HEALTH_{health.body['health']}")
            elif int(health.body["expires_at_ms"]) < now_ms:
                reasons.append("HEALTH_EXPIRED")
        if activation.entry_kind in ("TOOL", "SKILL"):
            revision = read_revision(connection, self.namespace_id, activation.entry_kind, activation.entry_id, activation.revision)
            if revision is not None:
                body = revision.body
                if activation.entry_kind == "TOOL":
                    ok, why = self._provider_usable(Pin.from_json(body["provider_ref"]), now_ms=now_ms)
                    if not ok:
                        reasons.extend(why)
                # Everything the definition depends on must itself be admitted: its
                # capabilities and its schemas (a suspended capability revokes the tool).
                dependencies = [Pin.from_json(c) for c in body.get("required_capabilities", [])]
                if activation.entry_kind == "SKILL":
                    dependencies.append(Pin.from_json(body["capability_ref"]))
                dependencies += [Pin.from_json(body["input_schema_ref"]), Pin.from_json(body["output_schema_ref"])]
                for dependency in dependencies:
                    ok, why = self._dependency_admitted(dependency)
                    if not ok:
                        reasons.append(why)
        return (not reasons), reasons

    def _dependency_admitted(self, pin: Pin) -> tuple[bool, str]:
        try:
            revision = resolve_pin(self.connection, self.namespace_id, pin)
        except ArpError as error:
            return False, f"{pin.kind.upper()}_{error.code}"
        activation = read_activation(self.connection, self.namespace_id, revision.entry_kind, revision.entry_id, revision.revision)
        if activation is None or activation.state != "ADMITTED":
            return False, f"{pin.kind.upper()}_NOT_ADMITTED:{pin.id}"
        return True, ""

    def _provider_usable(self, provider_pin: Pin, *, now_ms: int) -> tuple[bool, list[str]]:
        connection = self.connection
        try:
            provider = resolve_pin(connection, self.namespace_id, provider_pin)
        except ArpError as error:
            return False, [f"PROVIDER_{error.code}"]
        activation = read_activation(connection, self.namespace_id, "PROVIDER", provider.entry_id, provider.revision)
        if activation is None or activation.state != "ADMITTED":
            return False, ["PROVIDER_NOT_ADMITTED"]
        deployments = self.usable_deployments(provider.pin, now_ms=now_ms)
        if not deployments:
            return False, ["NO_USABLE_DEPLOYMENT"]
        return True, []

    def usable_deployments(self, provider_pin: Pin, *, now_ms: int, platform: str | None = None, capability: Pin | None = None) -> tuple[tuple[RevisionRow, ActivationRow, HealthRow], ...]:
        """ADMITTED deployments of one exact provider whose current health is READY and fresh."""

        connection = self.connection
        found = []
        for revision in list_latest(connection, self.namespace_id, "DEPLOYMENT"):
            if Pin.from_json(revision.body["provider_ref"]) != provider_pin:
                continue
            activation = read_activation(connection, self.namespace_id, "DEPLOYMENT", revision.entry_id, revision.revision)
            if activation is None:
                continue
            ok, _ = self.usable(activation, now_ms=now_ms)
            if not ok:
                continue
            platforms = list(revision.body["platforms"])
            if platform is not None and platforms and platform not in platforms:
                continue
            if capability is not None and capability.to_json() not in [dict(c) for c in revision.body["supported_capabilities"]]:
                continue
            health = latest_health(connection, self.namespace_id, revision.entry_id, revision.revision)
            assert health is not None
            found.append((revision, activation, health))
        return tuple(found)

    # -- read view --

    def summary(self, revision: RevisionRow, activation: ActivationRow, *, access_view: str, now_ms: int) -> dict[str, Any]:
        usable, reasons = self.usable(activation, now_ms=now_ms)
        body = revision.body
        kind = revision.entry_kind
        name = str(body.get("name") or body.get("tool_id") or body.get("provider_id") or body.get("capability_id") or revision.entry_id)[:128]
        description = str(body.get("description") or f"{kind.lower()} {revision.entry_id}")[:2048] or revision.entry_id
        files = list(body.get("files", [])) if kind == "SKILL" else []
        item = {
            "kind": kind,
            "definition_ref": revision.pin.to_json(),
            "name": name,
            "description": description,
            "state": activation.state,
            "state_revision": activation.row_version,
            "current_usable": usable,
            "reason_codes": reasons[:16],
            "registry_epoch": self.epoch(),
            "file_count": len(files),
            "total_payload_bytes": sum(int(f["size_bytes"]) for f in files),
            "detail_ref": revision.pin.to_json(),
            "access_view": access_view,
        }
        return check("CatalogueSummary", item)

    def page(self, command: Mapping[str, Any], *, access_view: str) -> dict[str, Any]:
        """``CatalogueReadCommand`` → ``CataloguePage`` (bounded summaries, epoch-bound cursor)."""

        value = check("CatalogueReadCommand", plain(command))
        if value["namespace_id"] != self.namespace_id:
            raise ArpError("REF_OUTSIDE_SCOPE", "the submitted namespace must equal the bound namespace")
        if access_view not in ("MODEL", "MANAGEMENT"):
            raise ArpError("ENUM", field_path="access_view")
        epoch = self.epoch()
        offset = 0
        if value["cursor"] is not None:
            try:
                cursor_epoch, offset_text = str(value["cursor"]).split(":", 1)
                offset = int(offset_text)
            except ValueError as error:
                raise ArpError("CURSOR_UNKNOWN") from error
            if int(cursor_epoch) != epoch:
                raise ArpError("CATALOGUE_STALE", "the catalogue changed since this cursor was issued")
        now_ms = self.clock_ms()
        connection = self.connection
        entries = list_latest(connection, self.namespace_id, value["kind"])
        limit = min(int(value["limit"]), PAGE_ITEMS_MAX)
        items: list[dict[str, Any]] = []
        index = offset
        while index < len(entries) and len(items) < limit:
            revision = entries[index]
            activation = read_activation(connection, self.namespace_id, revision.entry_kind, revision.entry_id, revision.revision)
            index += 1
            if activation is None:
                continue
            candidate = self.summary(revision, activation, access_view=access_view, now_ms=now_ms)
            page = self._page(value["namespace_id"], epoch, [*items, candidate], has_more=True, next_cursor=f"{epoch}:{index}", access_view=access_view)
            if page["body_bytes"] > PAGE_BYTES_MAX:
                if not items:
                    raise ArpError("ITEM_TOO_LARGE", "one summary does not fit the page byte cap")
                index -= 1
                break
            items.append(candidate)
        has_more = index < len(entries)
        return self._page(value["namespace_id"], epoch, items, has_more=has_more, next_cursor=f"{epoch}:{index}" if has_more else None, access_view=access_view)

    @staticmethod
    def _page(namespace_id: str, epoch: int, items: list[dict[str, Any]], *, has_more: bool, next_cursor: str | None, access_view: str) -> dict[str, Any]:
        page = {"schema_version": 1, "namespace_id": namespace_id, "registry_epoch": epoch, "items": items, "has_more": has_more, "next_cursor": next_cursor, "access_view": access_view, "body_bytes": 0}
        for _ in range(4):
            size = len(canonical(page))
            if size == page["body_bytes"]:
                break
            page["body_bytes"] = size
        return check("CataloguePage", page)


# ---- capability resolution (§8) ----------------------------------------------------


@dataclass(slots=True)
class CapabilityResolver:
    catalogue: CatalogueService

    def resolve(
        self,
        session: store.SessionRow,
        *,
        owner_contract_ref: Pin,
        requirement_key: str,
        capability_ref: Pin,
        input_manifest_ref: Pin,
        invocation_mode: str,
        authority_refs: Sequence[Pin],
        verification_policy_ref: Pin | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        """Hard filter (admitted capability/provider/deployment, kind, semantics, platform,
        health) → approved priority (desc) → stable provider order; the first qualified
        deployment is bound and persisted. No candidate is ``CAPABILITY_UNAVAILABLE``."""

        catalogue = self.catalogue
        connection = catalogue.connection
        now_ms = catalogue.clock_ms()
        if invocation_mode not in INVOCATION_PROVIDER_KINDS:
            raise ArpError("ENUM", field_path="invocation_mode")
        capability = resolve_pin(connection, catalogue.namespace_id, capability_ref)
        activation = read_activation(connection, catalogue.namespace_id, "CAPABILITY", capability.entry_id, capability.revision)
        if activation is None or activation.state != "ADMITTED":
            raise ArpError("CAPABILITY_UNAVAILABLE", f"capability {capability.entry_id} is not admitted")
        required = set(capability.body["required_semantics"])
        qualified: list[tuple[tuple[int, str, int, str], RevisionRow, RevisionRow]] = []
        # Candidates: providers the capability names + admitted providers that declare
        # this exact capability pin (definitions are hash-pinned, so the provider →
        # capability direction is the one that can always be expressed).
        candidates: dict[tuple[str, int], RevisionRow] = {}
        for provider_json in capability.body["provider_refs"]:
            try:
                provider = resolve_pin(connection, catalogue.namespace_id, Pin.from_json(provider_json))
            except ArpError:
                continue
            candidates[(provider.entry_id, provider.revision)] = provider
        for provider in list_latest(connection, catalogue.namespace_id, "PROVIDER"):
            if capability.pin.to_json() in [dict(c) for c in provider.body["capability_refs"]]:
                candidates.setdefault((provider.entry_id, provider.revision), provider)
        for provider in candidates.values():
            provider_activation = read_activation(connection, catalogue.namespace_id, "PROVIDER", provider.entry_id, provider.revision)
            if provider_activation is None or provider_activation.state != "ADMITTED":
                continue
            if provider.body["kind"] not in INVOCATION_PROVIDER_KINDS[invocation_mode]:
                continue
            if not required <= set(provider.body["supported_semantics"]):
                continue
            # Hard filter: the provider's declared input/output schemas must be the
            # capability's exact schemas (§8: 输入schema 是硬门).
            if provider.body["input_schema_ref"] != capability.body["input_schema_ref"] or provider.body["output_schema_ref"] != capability.body["output_schema_ref"]:
                continue
            deployments = catalogue.usable_deployments(provider.pin, now_ms=now_ms, platform=platform, capability=capability.pin)
            for deployment, _, _ in deployments:
                order = (-int(provider.body["selection_priority"]), provider.entry_id, provider.revision, provider.content_hash)
                qualified.append((order, provider, deployment))
        if not qualified:
            raise ArpError("CAPABILITY_UNAVAILABLE", f"no admitted, healthy deployment serves {capability.entry_id}")
        qualified.sort(key=lambda q: (q[0], q[2].entry_id, q[2].revision))
        _, provider, deployment = qualified[0]
        binding = {
            "schema_version": 1,
            "binding_id": "",
            "session_id": session.session_id,
            "owner_contract_ref": owner_contract_ref.to_json(),
            "requirement_key": requirement_key,
            "capability_ref": capability.pin.to_json(),
            "provider_ref": provider.pin.to_json(),
            "deployment_ref": deployment.pin.to_json(),
            "input_manifest_ref": input_manifest_ref.to_json(),
            "authority_refs": [a.to_json() for a in authority_refs],
            "registry_epoch": catalogue.epoch(),
            "invocation_mode": invocation_mode,
            "verification_policy_ref": (verification_policy_ref or Pin.from_json(capability.body["verification_policy_ref"])).to_json(),
        }
        binding_hash = digest({k: v for k, v in binding.items() if k != "binding_id"})
        binding["binding_id"] = f"binding-{binding_hash[:32]}"
        value = check("CapabilityBinding", binding)
        owner_key = digest(owner_contract_ref.to_json())
        with catalogue.uow.database.transaction() as txn:
            existing = txn.execute(
                "SELECT body_json FROM arp_capability_bindings WHERE session_id=? AND owner_contract_key=? AND requirement_key=? AND binding_hash=?",
                (session.session_id, owner_key, requirement_key, binding_hash),
            ).fetchone()
            if existing is None:
                txn.execute(
                    "INSERT INTO arp_capability_bindings(binding_id,session_id,owner_contract_key,requirement_key,binding_hash,body_json,source_receipt_ref_json) VALUES (?,?,?,?,?,?,?)",
                    (value["binding_id"], session.session_id, owner_key, requirement_key, binding_hash, _json_column(value), _pin_column(deployment.source_receipt_ref)),
                )
        return value


# ---- tool exposure (§8 ToolExposure) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExposedTool:
    model_name: str
    tool: RevisionRow
    activation: ActivationRow
    provider: RevisionRow
    schema_ref: Pin


@dataclass(slots=True)
class ToolExposureService:
    catalogue: CatalogueService

    def exposed(self, requested: Iterable[str], *, now_ms: int) -> tuple[tuple[ExposedTool, ...], dict[str, list[str]]]:
        """Which of the requested tool ids are usable right now, and why the others are not."""

        catalogue = self.catalogue
        connection = catalogue.connection
        exposed: list[ExposedTool] = []
        refused: dict[str, list[str]] = {}
        for name in sorted(set(requested)):
            revision = latest_revision(connection, catalogue.namespace_id, "TOOL", name)
            if revision is None:
                refused[name] = ["NOT_IN_CATALOGUE"]
                continue
            activation = read_activation(connection, catalogue.namespace_id, "TOOL", revision.entry_id, revision.revision)
            if activation is None:
                refused[name] = ["NO_LIFECYCLE_ROW"]
                continue
            ok, reasons = catalogue.usable(activation, now_ms=now_ms)
            if not ok:
                refused[name] = reasons
                continue
            provider = resolve_pin(connection, catalogue.namespace_id, Pin.from_json(revision.body["provider_ref"]))
            exposed.append(ExposedTool(name, revision, activation, provider, Pin.from_json(revision.body["input_schema_ref"])))
        # A model name is the tool id unless two exposed tools would collide; then it
        # carries the identity hash. Same name + another ref is never overwritten.
        names: dict[str, int] = {}
        for item in exposed:
            names[item.model_name] = names.get(item.model_name, 0) + 1
        final = tuple(
            item if names[item.model_name] == 1 else ExposedTool(f"{item.model_name}-{item.tool.content_hash[:8]}", item.tool, item.activation, item.provider, item.schema_ref)
            for item in exposed
        )
        return final, refused

    def prepare(self, session: store.SessionRow, requested: Iterable[str], *, authority_refs: Sequence[Pin]) -> tuple[dict[str, Any], tuple[ExposedTool, ...], dict[str, list[str]]]:
        """Freeze the exposure of one request into a persisted ``ToolSnapshot``."""

        catalogue = self.catalogue
        now_ms = catalogue.clock_ms()
        exposed, refused = self.exposed(requested, now_ms=now_ms)
        tools = [
            {
                "model_name": item.model_name,
                "tool_ref": item.tool.pin.to_json(),
                "schema_ref": item.schema_ref.to_json(),
                "provider_ref": item.provider.pin.to_json(),
                "effective_authority_refs": [a.to_json() for a in authority_refs],
            }
            for item in exposed
        ]
        catalogue_hash = digest([[t["tool_ref"], t["schema_ref"], t["provider_ref"], item.activation.witness.to_json()] for t, item in zip(tools, exposed)])
        epoch = catalogue.epoch()
        snapshot = {
            "schema_version": 1,
            "snapshot_id": f"tools-{digest({'session': session.session_id, 'generation': session.generation, 'epoch': epoch, 'catalogue': catalogue_hash})[:32]}",
            "session_id": session.session_id,
            "generation": session.generation,
            "registry_epoch": epoch,
            "tools": tools,
            "catalogue_hash": catalogue_hash,
        }
        value = check("ToolSnapshot", snapshot)
        snapshot_hash = digest(value)
        with catalogue.uow.database.transaction() as txn:
            existing = txn.execute("SELECT snapshot_hash FROM arp_tool_exposures WHERE snapshot_id=?", (value["snapshot_id"],)).fetchone()
            if existing is None:
                txn.execute(
                    "INSERT INTO arp_tool_exposures(snapshot_id,session_id,generation,catalog_epoch,snapshot_hash,body_json,source_receipt_ref_json) VALUES (?,?,?,?,?,?,?)",
                    (value["snapshot_id"], session.session_id, session.generation, epoch, snapshot_hash, _json_column(value), _pin_column(Pin("receipt", f"exposure:{value['snapshot_id']}", 0, snapshot_hash))),
                )
            elif str(existing[0]) != snapshot_hash:
                raise ArpError("SOURCE_HASH_CONFLICT", "tool snapshot id reused with another body")
        return value, exposed, refused

    @staticmethod
    def snapshot_pin(snapshot: Mapping[str, Any]) -> Pin:
        return Pin("tool_snapshot", str(snapshot["snapshot_id"]), int(snapshot["generation"]), digest(snapshot))


def read_tool_snapshot(connection: sqlite3.Connection, snapshot_id: str) -> Mapping[str, Any] | None:
    raw = connection.execute("SELECT body_json FROM arp_tool_exposures WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    return None if raw is None else _load(raw[0])


def latest_tool_snapshot(connection: sqlite3.Connection, session_id: str) -> Mapping[str, Any] | None:
    raw = connection.execute(
        "SELECT body_json FROM arp_tool_exposures WHERE session_id=? ORDER BY generation DESC, catalog_epoch DESC, rowid DESC LIMIT 1", (session_id,)
    ).fetchone()
    return None if raw is None else _load(raw[0])


__all__ = (
    "ENTRY_KINDS", "STATES", "TRANSITIONS", "ActivationRow", "CapabilityResolver", "CatalogueScope", "CatalogueService", "ExposedTool",
    "HealthRow", "MountRow", "RevisionRow", "ToolExposureService", "advance_epoch_locked", "latest_health", "latest_revision", "latest_tool_snapshot",
    "list_latest", "mount_locked", "put_activation_locked", "put_health_locked", "put_revision_locked", "read_activation", "read_mount",
    "read_revision", "read_tool_snapshot", "registry_epoch", "resolve_pin", "transition_activation_locked", "validate_entry",
)
