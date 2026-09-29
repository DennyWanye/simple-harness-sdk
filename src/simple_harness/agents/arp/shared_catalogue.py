# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""One Skill catalogue authority for every pool of a deployment realm (NEXT-TG-1.0 §11).

A deployment runs several native pools (context sizes, thinking / non-thinking), each
with its own execution library.  Their catalogues must not each approve a Skill of the
same name, so the realm has exactly one writer — the *owner* pool's catalogue — and
every other pool (a *member*) holds a mirror of it:

* every Skill command (install / trial / admit / suspend / resume / retire) runs on the
  owner only; a member refuses direct Skill writes by name (``REF_OUTSIDE_SCOPE``);
* ``sync`` mirrors each owner Skill revision into every member with the owner's exact
  bundle bytes, so the member's revision is the same ``skill_id / revision /
  content_hash``; the member's lifecycle row follows the owner's state through the
  ordinary state machine, and an ADMITTED step copies the owner's recorded official
  acceptance (read from the owner's own rows — no caller supplies it);
* each member still resolves the Skill's dependencies against its own tools: a Skill
  whose required tools this pool lacks keeps an incomplete lock there and cannot be
  admitted or used in that pool (shared catalogue ≠ every pool runs every Skill);
* every use in a member first asks the owner: the same pin must be currently usable
  there.  A suspension on the owner therefore refuses the very next use in every pool,
  whether or not the mirror has caught up; a use already handed off keeps its own
  recorded check (history is never rewritten).

The owner and members are the pools' own ``AgentRuntime`` objects in one process; the
authority is read through the owner's own connection.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import catalogue as cat
from .errors import ArpError
from .pins import Pin
from .ports import TrustedCaller
from .skills import inspect_bundle
from .strict import digest

SKILL_WRITE_VERBS = frozenset({
    "agent_skill_install", "agent_skill_trial", "agent_skill_admit", "agent_skill_suspend",
    "agent_skill_resume", "agent_skill_retire",
})
SKILL_READ_VERBS = frozenset({"agent_skills_list", "agent_skill_details", "agent_capabilities_list", "agent_tool_catalogue"})
#: The order a mirror walks to reach the owner's state from its own.
_PATH = {
    ("QUARANTINED", "TRIAL"): ("TRIAL",),
    ("QUARANTINED", "ADMITTED"): ("TRIAL", "ADMITTED"),
    ("QUARANTINED", "SUSPENDED"): ("TRIAL", "SUSPENDED"),
    ("TRIAL", "ADMITTED"): ("ADMITTED",),
    ("TRIAL", "SUSPENDED"): ("SUSPENDED",),
    ("ADMITTED", "SUSPENDED"): ("SUSPENDED",),
    ("SUSPENDED", "ADMITTED"): ("ADMITTED",),
    ("SUSPENDED", "TRIAL"): ("TRIAL",),
}


def managed_elsewhere() -> ArpError:
    return ArpError("REF_OUTSIDE_SCOPE", "Skills of this pool are managed by the deployment's shared catalogue owner")


class SharedSkillCatalogue:
    """The realm's Skill authority: ``owner`` runtime + ``members`` runtimes (bound late —
    the pools are assembled one by one)."""

    def __init__(self, *, caller: TrustedCaller) -> None:
        self._owner: Any = None
        self._owner_id: str | None = None
        self._members: dict[str, Any] = {}
        self._caller = caller

    # ---- assembly ------------------------------------------------------------------

    def bind_owner(self, pool_id: str, runtime: Any) -> None:
        if self._owner is not None and self._owner_id != pool_id:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "the shared catalogue already has another owner")
        self._owner, self._owner_id = runtime, pool_id

    def bind_member(self, pool_id: str, runtime: Any) -> None:
        if pool_id == self._owner_id:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "the owner pool is not its own member")
        self._members[pool_id] = runtime

    @property
    def owner_id(self) -> str | None:
        return self._owner_id

    def member_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._members))

    def _owner_arp(self) -> Any:
        arp = getattr(self._owner, "arp", None)
        if arp is None or arp.catalogue is None:
            raise ArpError("CATALOGUE_STALE", "the shared catalogue owner is not assembled")
        return arp

    # ---- the authority a member consults before every use -------------------------------

    def skill_view(self, pin: Pin) -> dict[str, Any]:
        """The owner's current view of exactly this Skill pin (never another revision)."""

        arp = self._owner_arp()
        service = arp.catalogue
        revision = cat.read_revision(service.connection, service.namespace_id, "SKILL", pin.id, pin.revision)
        if revision is None or revision.pin != pin:
            return {"present": False, "usable": False, "state": None, "reasons": ["NOT_IN_SHARED_CATALOGUE"]}
        activation = cat.read_activation(service.connection, service.namespace_id, "SKILL", pin.id, pin.revision)
        if activation is None:
            return {"present": True, "usable": False, "state": None, "reasons": ["NO_LIFECYCLE_ROW"]}
        ok, reasons = service.usable(activation, now_ms=service.clock_ms())
        return {"present": True, "usable": ok, "state": activation.state, "reasons": list(reasons)}

    def trial_mission_for(self, pin: Pin) -> str | None:
        """The evaluation Mission the owner's current trial of exactly this pin was
        dispatched to (None: no current trial, expired, or not dispatched)."""
        return self._owner_arp().lifecycle.trial_mission_for(pin)

    def require_usable(self, pin: Pin, *, trial_mission_id: str | None = None) -> None:
        view = self.skill_view(pin)
        if (not view["usable"] and trial_mission_id is not None and view["state"] == "TRIAL"
                and view["reasons"] == ["STATE_TRIAL"] and self.trial_mission_for(pin) == trial_mission_id):
            return  # its own evaluation Mission, re-checked on the owner at every use
        if not view["usable"]:
            raise ArpError(
                "SKILL_NOT_ADMITTED", f"skill {pin.id}@{pin.revision} is not usable in the shared catalogue",
                detail={"reasons": ["SHARED_" + r for r in view["reasons"]], "state": view["state"]},
            )

    # ---- commands -----------------------------------------------------------------------

    async def handle(self, request: Mapping[str, Any], *, caller: TrustedCaller) -> dict[str, Any]:
        """A Skill ``HostRequest`` on the owner; a successful write is mirrored at once."""

        from simple_harness.api.runtime_plane import RuntimePlaneService

        verb = request.get("verb") if isinstance(request, Mapping) else None
        if verb not in SKILL_WRITE_VERBS | SKILL_READ_VERBS:
            raise ArpError("UNSUPPORTED_HOST_VERB", field_path="$.verb")
        body = dict(request)
        if isinstance(body.get("subject_id"), str) and body["subject_id"] == "-":
            body["subject_id"] = self._owner_arp().catalogue.namespace_id
        response = await RuntimePlaneService(self._owner).handle(body, caller=caller)
        if verb in SKILL_WRITE_VERBS and response.get("error") is None:
            response = {**response, "mirror": self.sync()}
        return response

    def _envelope(self, verb: str, *, command_id: str, expected_revision: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"schema_version": 1, "verb": verb, "command_id": command_id, "subject_id": self._owner_arp().catalogue.namespace_id,
                "expected_revision": expected_revision, "cursor": None, "limit": 1, "payload_ref": None, "payload": dict(payload)}

    async def install(self, bundle_ref: Pin, *, data_format: str, caller: TrustedCaller, command_id: str) -> dict[str, Any]:
        """Install one bundle (already in the deployment's artifact store) on the owner."""

        service = self._owner_arp().catalogue
        done = service.connection.execute(
            "SELECT skill_id, skill_revision FROM arp_skill_import_commands WHERE namespace_id=? AND command_id=? AND bundle_hash=?",
            (service.namespace_id, command_id, bundle_ref.content_hash),
        ).fetchone()
        if done is not None:
            # the same install command again: its original entry, never a second import
            revision = cat.read_revision(service.connection, service.namespace_id, "SKILL", str(done[0]), int(done[1]))
            activation = cat.read_activation(service.connection, service.namespace_id, "SKILL", str(done[0]), int(done[1]))
            assert revision is not None and activation is not None
            item = service.summary(revision, activation, access_view="MANAGEMENT", now_ms=service.clock_ms())
            return {"error": None, "items": [item], "replayed": True, "mirror": self.sync()}
        epoch = service.epoch()
        payload = {"bundle_artifact_ref": bundle_ref.to_json(), "scope_ref": service.scope.pin.to_json(),
                   "format": data_format, "expected_catalogue_revision": epoch}
        return await self.handle(self._envelope("agent_skill_install", command_id=command_id, expected_revision=epoch, payload=payload), caller=caller)

    async def lifecycle(self, skill_ref: Pin, action: str, *, caller: TrustedCaller, command_id: str, reason: str) -> dict[str, Any]:
        """SUSPEND / RESUME / RETIRE one Skill revision on the owner (RESUME reuses the
        acceptance that admitted it; nothing else can)."""

        owner = self._owner_arp()
        service = owner.catalogue
        revision = cat.resolve_pin(service.connection, service.namespace_id, skill_ref)
        activation = cat.read_activation(service.connection, service.namespace_id, "SKILL", revision.entry_id, revision.revision)
        if activation is None:
            raise ArpError("CATALOGUE_STALE", "skill has no lifecycle row")
        if action == "SUSPEND":
            payload: dict[str, Any] = {"schema_version": 1, "skill_ref": revision.pin.to_json(), "reason": reason}
            verb = "agent_skill_suspend"
        elif action in ("RESUME", "RETIRE"):
            lock = owner.skills.latest_lock(revision)
            if lock is None:
                raise ArpError("DEPENDENCY_UNRESOLVED", "skill has no dependency lock")
            acceptance = None
            if action == "RESUME":
                if activation.evaluation_ref is None:
                    raise ArpError("SKILL_TRIAL_REQUIRED", "the revision was never admitted")
                acceptance = owner.lifecycle.admission_for(revision, activation.evaluation_ref)
            payload = {"schema_version": 1, "action": action, "skill_ref": revision.pin.to_json(),
                       "expected_activation_revision": activation.row_version,
                       "evaluation_ref": None if acceptance is None else acceptance.to_json(),
                       "dependency_lock_ref": owner.skills.lock_pin(lock).to_json(), "reason": reason}
            verb = "agent_skill_resume" if action == "RESUME" else "agent_skill_retire"
        else:
            raise ArpError("ENUM", field_path="$.action")
        return await self.handle(self._envelope(verb, command_id=command_id, expected_revision=activation.row_version, payload=payload), caller=caller)

    def overview(self) -> dict[str, Any]:
        """Every owner Skill revision with its state and, per pool, whether it is usable there."""

        owner = self._owner_arp()
        oc = owner.catalogue
        now_ms = oc.clock_ms()
        skills = []
        for revision in _skill_revisions(oc):
            activation = cat.read_activation(oc.connection, oc.namespace_id, "SKILL", revision.entry_id, revision.revision)
            view = self.skill_view(revision.pin)
            owner_lock = owner.skills.latest_lock(revision)
            pools = {str(self._owner_id): {"state": view["state"], "usable": bool(view["usable"] and owner_lock and owner_lock["complete"]),
                                           "reasons": view["reasons"]}}
            for pool_id in sorted(self._members):
                member = getattr(self._members[pool_id], "arp", None)
                if member is None or member.catalogue is None:
                    pools[pool_id] = {"state": None, "usable": False, "reasons": ["NOT_ASSEMBLED"]}
                    continue
                mc = member.catalogue
                local = cat.read_revision(mc.connection, mc.namespace_id, "SKILL", revision.entry_id, revision.revision)
                if local is None or local.pin != revision.pin:
                    pools[pool_id] = {"state": None, "usable": False, "reasons": ["NOT_MIRRORED"]}
                    continue
                row = cat.read_activation(mc.connection, mc.namespace_id, "SKILL", local.entry_id, local.revision)
                ok, reasons = (False, ["NO_LIFECYCLE_ROW"]) if row is None else mc.usable(row, now_ms=now_ms)
                lock = member.skills.latest_lock(local)
                if lock is None or not lock["complete"]:
                    ok, reasons = False, [*reasons, "DEPENDENCY_UNRESOLVED"]
                if not view["usable"]:
                    ok, reasons = False, [*reasons, *("SHARED_" + r for r in view["reasons"])]
                pools[pool_id] = {"state": None if row is None else row.state, "usable": ok, "reasons": list(reasons)}
            skills.append({
                "skill_ref": revision.pin.to_json(), "skill_id": revision.entry_id, "version": revision.revision,
                "name": str(revision.body.get("name", revision.entry_id)), "description": str(revision.body.get("description", ""))[:400],
                "state": None if activation is None else activation.state, "pools": pools,
            })
        return {"owner": self._owner_id, "members": list(self.member_ids()), "namespace_id": oc.namespace_id,
                "registry_epoch": oc.epoch(), "skills": skills}

    # ---- mirror ---------------------------------------------------------------------------

    def sync(self) -> dict[str, Any]:
        """Bring every member to the owner's Skill revisions and states; idempotent."""

        owner = self._owner_arp()
        revisions = sorted(
            (r for r in _skill_revisions(owner.catalogue)), key=lambda r: (r.entry_id, r.revision)
        )
        report: dict[str, Any] = {"owner": self._owner_id, "members": {}}
        for pool_id in sorted(self._members):
            member = getattr(self._members[pool_id], "arp", None)
            if member is None or member.catalogue is None:
                report["members"][pool_id] = {"error": "NOT_ASSEMBLED"}
                continue
            rows = []
            for revision in revisions:
                try:
                    rows.append(self._mirror_one(owner, member, revision))
                except ArpError as error:
                    rows.append({"skill_ref": revision.pin.to_json(), "mirrored": False, "error": error.code})
            report["members"][pool_id] = {"skills": rows}
        return report

    def _mirror_one(self, owner: Any, member: Any, revision: cat.RevisionRow) -> dict[str, Any]:
        mc = member.catalogue
        local = cat.read_revision(mc.connection, mc.namespace_id, "SKILL", revision.entry_id, revision.revision)
        if local is None:
            if revision.bundle_root_ref is None:
                raise ArpError("REF_KIND_MISMATCH", "an owner Skill revision has no bundle")
            data = owner.skills.bundle_bytes(revision)
            command = {
                "bundle_artifact_ref": revision.bundle_root_ref.to_json(), "scope_ref": mc.scope.pin.to_json(),
                "format": "NATIVE" if inspect_bundle(data).skill_json is not None else "SKILL_MD",
                "expected_catalogue_revision": mc.epoch(),
            }
            result = member.skills.import_bundle(
                command, data, caller=self._caller, command_id=f"shared-mirror:{revision.entry_id}:{revision.revision}", mirror=True,
            )
            local = result.revision
        if local.pin != revision.pin:
            # the member already holds another definition under this number: never used
            raise ArpError("SOURCE_HASH_CONFLICT", "the member's revision differs from the owner's")
        oc = owner.catalogue
        target = cat.read_activation(oc.connection, oc.namespace_id, "SKILL", revision.entry_id, revision.revision)
        current = cat.read_activation(mc.connection, mc.namespace_id, "SKILL", local.entry_id, local.revision)
        if target is None or current is None:
            raise ArpError("CATALOGUE_STALE", "a lifecycle row is missing")
        if current.state != target.state:
            self._follow(owner, member, revision, current, target)
            current = cat.read_activation(mc.connection, mc.namespace_id, "SKILL", local.entry_id, local.revision)
        return {"skill_ref": revision.pin.to_json(), "mirrored": True, "state": current.state, "owner_state": target.state}

    def _follow(self, owner: Any, member: Any, local: cat.RevisionRow, current: cat.ActivationRow, target: cat.ActivationRow) -> None:
        # ``local`` is the owner's revision; the member's has the identical pin (checked)
        mc = member.catalogue
        steps = ("RETIRED",) if target.state == "RETIRED" else _PATH.get((current.state, target.state))
        if steps is None:
            raise ArpError("STATE_COMBINATION_INVALID", f"no mirror path {current.state} → {target.state}")
        acceptance = None
        if "ADMITTED" in steps:
            if target.evaluation_ref is None:
                raise ArpError("SKILL_EVALUATION_INCOMPLETE", "the owner's admission carries no evaluation")
            acceptance = owner.lifecycle.admission_for(local, target.evaluation_ref)
            if acceptance is None:
                raise ArpError("SKILL_EVALUATION_INCOMPLETE", "the owner's admission has no recorded official acceptance")
        with mc.uow.database.transaction() as txn:
            for state in steps:
                command_id = f"shared-mirror:{local.entry_id}:{local.revision}:{target.row_version}:{state}"
                if state == "ADMITTED":
                    member.lifecycle._record_admission_locked(txn, local, target.evaluation_ref, acceptance, command_id)
                mc.transition_locked(
                    txn, local.pin, state=state, caller=self._caller, command_id=command_id,
                    evaluation_ref=target.evaluation_ref if state in ("TRIAL", "ADMITTED") else None,
                    admission_ref=acceptance if state == "ADMITTED" else None,
                )


def _skill_revisions(service: cat.CatalogueService) -> list[cat.RevisionRow]:
    rows = service.connection.execute(
        "SELECT entry_id, revision FROM arp_catalog_revisions WHERE namespace_id=? AND entry_kind='SKILL' ORDER BY entry_id, revision",
        (service.namespace_id,),
    ).fetchall()
    out = []
    for entry_id, number in rows:
        revision = cat.read_revision(service.connection, service.namespace_id, "SKILL", str(entry_id), int(number))
        if revision is not None:
            out.append(revision)
    return out


def mirror_caller(realm_id: str) -> TrustedCaller:
    """The runtime's own principal for mirror steps (never a model or a payload)."""

    return TrustedCaller(
        principal_ref=Pin("principal", f"runtime:shared-catalogue:{realm_id}", 0, digest({"realm": realm_id})),
        owner_contract_ref=Pin("policy", f"shared-catalogue:{realm_id}", 1, digest({"shared_catalogue": realm_id})),
        command_receipt_ref=Pin("receipt", f"shared-catalogue:{realm_id}:mirror", 0, digest({"mirror": realm_id})),
    )


__all__ = ("SKILL_READ_VERBS", "SKILL_WRITE_VERBS", "SharedSkillCatalogue", "managed_elsewhere", "mirror_caller")

