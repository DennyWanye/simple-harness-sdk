# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Skill lifecycle commands (ARP-EXEC-1.1.1 §9.6–9.7, SKILL-CATALOGUE §3–§4).

``begin_trial`` turns a QUARANTINED (or SUSPENDED) Skill with a complete dependency
lock into a TRIAL under an isolated evaluation scope: no production credentials, no
network, only the policy's allowed tools, a bounded validity. The binding is immutable
and stored per command id, so a re-sent command returns the same binding.

``admit`` needs the official EvaluationAcceptance of that binding. The SDK does not own
an evaluator: the acceptance reader is a port (``SkillAcceptancePort``). Without the
Assurance successor every admit is refused by name (``SKILL_EVALUATION_INCOMPLETE`` with
``successor = ASSURANCE_SUCCESSOR_PENDING``); nothing here signs its own PASS.

SUSPEND refuses new calls at once; RESUME needs the same evaluation still valid for the
same lock, otherwise a new trial is required; RETIRED is terminal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import catalogue as cat
from .codec import check
from .errors import ArpError
from .pins import Pin
from .ports import PendingAssuranceAcceptance, TrustedCaller
from .strict import digest, plain

TRIAL_TTL_MS = 24 * 3600 * 1000
ACTION_TARGET = {"TRIAL": "TRIAL", "ADMIT": "ADMITTED", "SUSPEND": "SUSPENDED", "RESUME": "ADMITTED", "RETIRE": "RETIRED"}


def _json_column(value: Any) -> str:
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(slots=True)
class SkillLifecycleService:
    catalogue: cat.CatalogueService
    skills: Any  # SkillImporter (dependency locks)
    acceptance: Any | None
    clock_ms: Callable[[], int]
    # NEXT-TG-1.0 §11: a member pool of a shared catalogue takes no lifecycle command.
    managed_by: Any | None = None

    def _owner_only(self) -> None:
        if self.managed_by is not None:
            from .shared_catalogue import managed_elsewhere

            raise managed_elsewhere()

    def __post_init__(self) -> None:
        if self.acceptance is None:
            self.acceptance = PendingAssuranceAcceptance()

    # ---- lookups ----------------------------------------------------------------------------

    def _skill(self, pin: Pin) -> tuple[cat.RevisionRow, cat.ActivationRow]:
        revision = cat.resolve_pin(self.catalogue.connection, self.catalogue.namespace_id, pin)
        if revision.entry_kind != "SKILL":
            raise ArpError("CATALOGUE_KIND_MISMATCH", "the command names a non-Skill entry")
        activation = cat.read_activation(self.catalogue.connection, self.catalogue.namespace_id, "SKILL", revision.entry_id, revision.revision)
        if activation is None:
            raise ArpError("CATALOGUE_STALE", "skill has no lifecycle row")
        return revision, activation

    def _current_lock(self, revision: cat.RevisionRow) -> tuple[Mapping[str, Any], Pin]:
        lock = self.skills.latest_lock(revision)
        if lock is None or not lock["complete"]:
            raise ArpError("DEPENDENCY_UNRESOLVED", f"skill {revision.entry_id}@{revision.revision} has no complete dependency lock")
        return lock, self.skills.lock_pin(lock)

    def binding_for(self, revision: cat.RevisionRow) -> Mapping[str, Any] | None:
        raw = self.catalogue.connection.execute(
            "SELECT body_json FROM arp_skill_evaluations WHERE namespace_id=? AND skill_id=? AND skill_revision=? ORDER BY rowid DESC LIMIT 1",
            (self.catalogue.namespace_id, revision.entry_id, revision.revision),
        ).fetchone()
        return None if raw is None else json.loads(str(raw[0]))

    def binding_by_evaluation(self, evaluation: Pin) -> Mapping[str, Any] | None:
        raw = self.catalogue.connection.execute(
            "SELECT body_json FROM arp_skill_evaluations WHERE namespace_id=? AND evaluation_id=? AND revision=? AND content_hash=?",
            (self.catalogue.namespace_id, evaluation.id, evaluation.revision, evaluation.content_hash),
        ).fetchone()
        return None if raw is None else json.loads(str(raw[0]))

    def admission_for(self, revision: cat.RevisionRow, evaluation: Pin) -> Pin | None:
        raw = self.catalogue.connection.execute(
            "SELECT acceptance_ref_json FROM arp_skill_admissions WHERE namespace_id=? AND skill_id=? AND skill_revision=? AND evaluation_id=?",
            (self.catalogue.namespace_id, revision.entry_id, revision.revision, evaluation.id),
        ).fetchone()
        return None if raw is None else Pin.from_json(json.loads(str(raw[0])))

    # ---- begin_trial (§9.6) -------------------------------------------------------------------

    def begin_trial(self, command: Mapping[str, Any], *, caller: TrustedCaller, command_id: str, run_id: str | None = None) -> dict[str, Any]:
        self._owner_only()
        if not isinstance(caller, TrustedCaller):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "skill commands need an authenticated caller")
        value = check("SkillTrialCommand", plain(command))
        revision, activation = self._skill(Pin.from_json(value["skill_ref"]))
        previous = self.catalogue.connection.execute(
            "SELECT body_json FROM arp_skill_evaluations WHERE namespace_id=? AND command_id=?", (self.catalogue.namespace_id, command_id)
        ).fetchone()
        if previous is not None:
            binding = json.loads(str(previous[0]))
            same = (
                binding["skill_ref"] == value["skill_ref"]
                and binding["lock_ref"] == value["dependency_lock_ref"]
                and binding["policy_ref"] == value["evaluation_policy_ref"]
            )
            if not same:
                raise ArpError("SOURCE_HASH_CONFLICT", "trial command id re-sent with another body")
            return binding
        if activation.row_version != int(value["expected_activation_revision"]):
            raise ArpError("EXPECTED_REVISION_MISMATCH", "the activation moved since the command was prepared")
        if activation.state not in ("QUARANTINED", "SUSPENDED"):
            raise ArpError("STATE_COMBINATION_INVALID", f"a trial starts from QUARANTINED or SUSPENDED, not {activation.state}")
        lock, lock_pin = self._current_lock(revision)
        if lock_pin != Pin.from_json(value["dependency_lock_ref"]):
            raise ArpError("SOURCE_HASH_CONFLICT", "dependency_lock_ref is not the skill's current complete lock")
        policy = Pin.from_json(value["evaluation_policy_ref"])
        if policy != Pin.from_json(revision.body["verification_policy_ref"]):
            raise ArpError("POLICY_CONFLICT", "evaluation policy is not the skill's approved verification policy")
        allowed, unavailable = self._allowed_tools(revision)
        scope_body = {
            "kind": "skill-trial-scope", "skill_ref": revision.pin.to_json(), "workspace": f"trial/{revision.entry_id}/{revision.revision}/{command_id}",
            "network": "NONE", "credential_policy": "NO_PRODUCTION_CREDENTIALS", "allowed_tool_refs": [p.to_json() for p in allowed],
        }
        scope = Pin("scope", f"skill-trial:{revision.entry_id}@{revision.revision}:{command_id}", 0, digest(scope_body))
        authority = Pin(
            "authority", f"skill-trial:{caller.principal_ref.id}:{command_id}", 0,
            digest({"caller": caller.to_json(), "policy": policy.to_json(), "scope": scope.to_json(), "allowed_tool_refs": [p.to_json() for p in allowed], "unavailable_tool_refs": unavailable}),
        )
        now = self.clock_ms()
        evaluation_id = f"skill-eval:{revision.entry_id}@{revision.revision}:{lock['lock_hash'][:16]}:{command_id}"
        partial = {
            "schema_version": 1,
            "skill_ref": revision.pin.to_json(),
            "lock_ref": lock_pin.to_json(),
            "scope_ref": scope.to_json(),
            "source_authority_ref": authority.to_json(),
            "allowed_tool_refs": [p.to_json() for p in allowed],
            "credential_policy": "NO_PRODUCTION_CREDENTIALS",
            "invocation_refs": [],
            "budget_owner_ref": Pin("task", f"skill-trial:{command_id}", 0, digest({"eval": command_id, "skill": revision.pin.to_json()})).to_json(),
            "policy_ref": policy.to_json(),
            "expires_at_ms": now + TRIAL_TTL_MS,
        }
        evaluation = Pin("evaluation", evaluation_id, 1, digest(partial))
        binding = check("SkillEvaluationBinding", {**partial, "evaluation_ref": evaluation.to_json()})
        with self.catalogue.uow.database.transaction() as txn:  # binding + TRIAL step: one transaction
            txn.execute(
                "INSERT INTO arp_skill_evaluations(namespace_id,evaluation_id,revision,content_hash,command_id,skill_id,skill_revision,lock_hash,body_json,expires_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.catalogue.namespace_id, evaluation.id, evaluation.revision, evaluation.content_hash, command_id, revision.entry_id, revision.revision, lock["lock_hash"], _json_column(binding), int(binding["expires_at_ms"])),
            )
            self.catalogue.transition_locked(txn, revision.pin, state="TRIAL", caller=caller, command_id=f"{command_id}:TRIAL", evaluation_ref=evaluation, run_id=run_id)
        return binding

    def _allowed_tools(self, revision: cat.RevisionRow) -> tuple[list[Pin], list[dict[str, Any]]]:
        """The skill's required tools that are admitted and usable right now; the rest are
        named as unavailable in the authority receipt, never silently granted."""

        connection = self.catalogue.connection
        now = self.clock_ms()
        allowed: list[Pin] = []
        unavailable: list[dict[str, Any]] = []
        for raw in revision.body["required_tool_refs"]:
            pin = Pin.from_json(raw)
            try:
                tool = cat.resolve_pin(connection, self.catalogue.namespace_id, pin)
            except ArpError as error:
                unavailable.append({"tool_ref": pin.to_json(), "reasons": [error.code]})
                continue
            activation = cat.read_activation(connection, self.catalogue.namespace_id, "TOOL", tool.entry_id, tool.revision)
            ok, reasons = (False, ["NO_LIFECYCLE_ROW"]) if activation is None else self.catalogue.usable(activation, now_ms=now)
            if ok:
                allowed.append(pin)
            else:
                unavailable.append({"tool_ref": pin.to_json(), "reasons": reasons})
        return allowed, unavailable

    # ---- evaluation dispatch link (SKILL-CATALOGUE §3, BW09) ----------------------------------

    def dispatch_for(self, evaluation: Pin) -> Mapping[str, Any] | None:
        """The recorded Assurance Mission that evaluates ``evaluation``; None when the Host
        never dispatched it (then no acceptance can be official for it)."""

        raw = self.catalogue.connection.execute(
            "SELECT body_json FROM arp_skill_evaluation_dispatches WHERE namespace_id=? AND evaluation_id=? AND revision=? AND content_hash=?",
            (self.catalogue.namespace_id, evaluation.id, evaluation.revision, evaluation.content_hash),
        ).fetchone()
        return None if raw is None else json.loads(str(raw[0]))

    def record_evaluation_dispatch(
        self, evaluation: Pin, *, mission_id: str, task_id: str, caller: TrustedCaller, command_id: str
    ) -> Mapping[str, Any]:
        """Record, once, which original Assurance Mission and which evaluated task the Host
        dispatched for this evaluation binding. The link is what later lets an acceptance
        certificate be tied to the evaluation (together with the Mission's idempotency key
        and the issue-after-dispatch rule in ``assurance_acceptance``); it never marks the
        evaluation as passed."""
        self._owner_only()

        if not isinstance(caller, TrustedCaller):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "skill commands need an authenticated caller")
        evaluation.require_kind("evaluation")
        if type(mission_id) is not str or not mission_id.strip():
            raise ArpError("MISSING_FIELD", "mission_id is required", field_path="$.mission_id")
        if type(task_id) is not str or not task_id.strip():
            raise ArpError("MISSING_FIELD", "task_id is required", field_path="$.task_id")
        binding = self.binding_by_evaluation(evaluation)
        if binding is None:
            raise ArpError("SKILL_TRIAL_REQUIRED", "no trial binding for this evaluation")
        if int(binding["expires_at_ms"]) < self.clock_ms():
            raise ArpError("SKILL_TRIAL_REQUIRED", "the trial binding expired")
        existing = self.dispatch_for(evaluation)
        if existing is not None:
            if existing["mission_id"] != mission_id or existing["task_id"] != task_id:
                raise ArpError("SOURCE_HASH_CONFLICT", "this evaluation was dispatched to another mission")
            return existing
        body = {
            "schema_version": 1,
            "evaluation_ref": evaluation.to_json(),
            "skill_ref": binding["skill_ref"],
            "mission_id": mission_id,
            "task_id": task_id,
            "command_id": command_id,
            "caller": caller.to_json(),
            "recorded_at_ms": self.clock_ms(),
        }
        connection = self.catalogue.connection
        taken = connection.execute(
            "SELECT evaluation_id, command_id FROM arp_skill_evaluation_dispatches WHERE mission_id=? OR (namespace_id=? AND command_id=?)",
            (mission_id, self.catalogue.namespace_id, command_id),
        ).fetchone()
        if taken is not None:
            if str(taken[1]) == command_id:
                raise ArpError("SOURCE_HASH_CONFLICT", "dispatch command id re-sent for another evaluation")
            raise ArpError("SOURCE_HASH_CONFLICT", "this mission already evaluates another skill evaluation")
        with self.catalogue.uow.database.transaction() as txn:
            txn.execute(
                "INSERT INTO arp_skill_evaluation_dispatches(namespace_id,evaluation_id,revision,content_hash,mission_id,task_id,command_id,body_json) VALUES (?,?,?,?,?,?,?,?)",
                (self.catalogue.namespace_id, evaluation.id, evaluation.revision, evaluation.content_hash, mission_id, task_id, command_id, _json_column(body)),
            )
        return body

    # ---- admit / suspend / lifecycle (§9.7) --------------------------------------------------

    def _verified_acceptance(self, revision: cat.RevisionRow, activation: cat.ActivationRow, acceptance_ref: Pin, *, policy: Pin | None, scope: Pin | None, lock_ref: Pin | None) -> Mapping[str, Any]:
        acceptance_ref.require_kind("acceptance")
        binding = self.binding_for(revision)
        if binding is None or activation.evaluation_ref is None or Pin.from_json(binding["evaluation_ref"]) != activation.evaluation_ref:
            raise ArpError("SKILL_TRIAL_REQUIRED", "no trial binding for this skill revision")
        if policy is not None and Pin.from_json(binding["policy_ref"]) != policy:
            raise ArpError("POLICY_CONFLICT", "evaluation policy differs from the trial binding")
        if scope is not None and Pin.from_json(binding["scope_ref"]) != scope:
            raise ArpError("REF_OUTSIDE_SCOPE", "evaluation scope differs from the trial binding")
        if int(binding["expires_at_ms"]) < self.clock_ms():
            raise ArpError("SKILL_TRIAL_REQUIRED", "the trial binding expired")
        _, current_lock = self._current_lock(revision)
        if current_lock != Pin.from_json(binding["lock_ref"]) or (lock_ref is not None and lock_ref != current_lock):
            raise ArpError("SKILL_TRIAL_REQUIRED", "the dependency lock changed since the trial")
        view = self.acceptance.verify(binding, acceptance_ref)  # type: ignore[union-attr]
        if not isinstance(view, Mapping) or view.get("accepted") is not True or view.get("evaluation_ref") != binding["evaluation_ref"]:
            raise ArpError("SKILL_EVALUATION_INCOMPLETE", "the acceptance does not accept this evaluation")
        return binding

    def _admit_locked(self, txn: Any, revision: cat.RevisionRow, evaluation: Pin, acceptance_ref: Pin, *, caller: TrustedCaller, command_id: str, run_id: str | None) -> cat.ActivationRow:
        """Admission record and the ADMITTED step in the same transaction (SKILL-CATALOGUE §4)."""

        self._record_admission_locked(txn, revision, evaluation, acceptance_ref, command_id)
        return self.catalogue.transition_locked(txn, revision.pin, state="ADMITTED", caller=caller, command_id=command_id, admission_ref=acceptance_ref, run_id=run_id)

    def _record_admission_locked(self, txn: Any, revision: cat.RevisionRow, evaluation: Pin, acceptance_ref: Pin, command_id: str) -> None:
        if True:
            existing = txn.execute(
                "SELECT acceptance_ref_json FROM arp_skill_admissions WHERE namespace_id=? AND skill_id=? AND skill_revision=? AND evaluation_id=?",
                (self.catalogue.namespace_id, revision.entry_id, revision.revision, evaluation.id),
            ).fetchone()
            if existing is None:
                txn.execute(
                    "INSERT INTO arp_skill_admissions(namespace_id,skill_id,skill_revision,evaluation_id,acceptance_ref_json,command_id) VALUES (?,?,?,?,?,?)",
                    (self.catalogue.namespace_id, revision.entry_id, revision.revision, evaluation.id, _json_column(acceptance_ref.to_json()), command_id),
                )
            elif Pin.from_json(json.loads(str(existing[0]))) != acceptance_ref:
                raise ArpError("SOURCE_HASH_CONFLICT", "this evaluation was admitted under another acceptance")

    def admit(self, command: Mapping[str, Any], *, caller: TrustedCaller, command_id: str, run_id: str | None = None) -> cat.ActivationRow:
        self._owner_only()
        if not isinstance(caller, TrustedCaller):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "skill commands need an authenticated caller")
        value = check("SkillAdmitCommand", plain(command))
        revision, activation = self._skill(Pin.from_json(value["skill_ref"]))
        acceptance_ref = Pin.from_json(value["evaluation_acceptance_ref"])
        if activation.state == "ADMITTED":
            if activation.evaluation_ref is not None and self.admission_for(revision, activation.evaluation_ref) == acceptance_ref:
                return activation  # command re-sent: one admission, one receipt
            raise ArpError("STATE_COMBINATION_INVALID", "skill already admitted under another acceptance")
        if activation.state != "TRIAL":
            raise ArpError("SKILL_TRIAL_REQUIRED", f"admission needs a TRIAL, current state {activation.state}")
        self._verified_acceptance(revision, activation, acceptance_ref, policy=Pin.from_json(value["evaluation_policy_ref"]), scope=Pin.from_json(value["scope_ref"]), lock_ref=None)
        assert activation.evaluation_ref is not None
        with self.catalogue.uow.database.transaction() as txn:
            return self._admit_locked(txn, revision, activation.evaluation_ref, acceptance_ref, caller=caller, command_id=command_id, run_id=run_id)

    def _command_replayed(self, activation: cat.ActivationRow, *, state: str, caller: TrustedCaller, command_id: str) -> bool:
        """Owner-side ledger for suspend / resume / retire: the activation's latest step names
        its command.  The same command re-sent replays; the id reused for another step conflicts."""

        if self.catalogue.applied_by(activation, state=state, caller=caller, command_id=command_id):
            return True
        if activation.authority_ref.id == f"catalogue:{caller.principal_ref.id}:catalogue:{command_id}":
            raise ArpError("SOURCE_HASH_CONFLICT", "command id already names another lifecycle step")
        return False

    def suspend(self, command: Mapping[str, Any], *, caller: TrustedCaller, command_id: str, run_id: str | None = None) -> cat.ActivationRow:
        self._owner_only()
        if not isinstance(caller, TrustedCaller):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "skill commands need an authenticated caller")
        value = check("SkillSuspendCommand", plain(command))
        revision, activation = self._skill(Pin.from_json(value["skill_ref"]))
        if self._command_replayed(activation, state="SUSPENDED", caller=caller, command_id=command_id):
            return activation
        if activation.state == "SUSPENDED":
            return activation
        if activation.state not in ("TRIAL", "ADMITTED"):
            raise ArpError("STATE_COMBINATION_INVALID", f"{activation.state} cannot be suspended")
        return self.catalogue.transition(revision.pin, state="SUSPENDED", caller=caller, command_id=command_id, run_id=run_id)

    def transition(self, command: Mapping[str, Any], *, caller: TrustedCaller, command_id: str, run_id: str | None = None) -> cat.ActivationRow:
        self._owner_only()
        if not isinstance(caller, TrustedCaller):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "skill commands need an authenticated caller")
        value = check("SkillLifecycleCommand", plain(command))
        revision, activation = self._skill(Pin.from_json(value["skill_ref"]))
        action = str(value["action"])
        target = ACTION_TARGET[action]
        acceptance_ref = None if value["evaluation_ref"] is None else Pin.from_json(value["evaluation_ref"])
        lock_ref = Pin.from_json(value["dependency_lock_ref"])
        if action != "TRIAL" and self._command_replayed(activation, state=target, caller=caller, command_id=command_id):
            return activation
        if activation.state == target:
            if target != "ADMITTED" or (activation.evaluation_ref is not None and self.admission_for(revision, activation.evaluation_ref) == acceptance_ref):
                return activation  # re-sent command: the original outcome
            raise ArpError("STATE_COMBINATION_INVALID", "skill already admitted under another acceptance")
        if activation.row_version != int(value["expected_activation_revision"]):
            raise ArpError("EXPECTED_REVISION_MISMATCH", "the activation moved since the command was prepared")
        if action == "TRIAL":
            raise ArpError("MISSING_FIELD", "a trial starts with SkillTrialCommand (evaluation policy + isolated scope)", field_path="$.evaluation_policy_ref")
        if action == "RETIRE":
            return self.catalogue.transition(revision.pin, state="RETIRED", caller=caller, command_id=command_id, run_id=run_id)
        if action == "SUSPEND":
            if activation.state not in ("TRIAL", "ADMITTED"):
                raise ArpError("STATE_COMBINATION_INVALID", f"{activation.state} cannot be suspended")
            return self.catalogue.transition(revision.pin, state="SUSPENDED", caller=caller, command_id=command_id, run_id=run_id)
        if acceptance_ref is None:
            raise ArpError("SKILL_EVALUATION_INCOMPLETE", f"{action} needs the official evaluation acceptance", field_path="$.evaluation_ref")
        if action == "ADMIT":
            if activation.state != "TRIAL":
                raise ArpError("SKILL_TRIAL_REQUIRED", f"admission needs a TRIAL, current state {activation.state}")
            self._verified_acceptance(revision, activation, acceptance_ref, policy=None, scope=None, lock_ref=lock_ref)
            assert activation.evaluation_ref is not None
            with self.catalogue.uow.database.transaction() as txn:
                return self._admit_locked(txn, revision, activation.evaluation_ref, acceptance_ref, caller=caller, command_id=command_id, run_id=run_id)
        # RESUME: only the evaluation that admitted this revision, still valid for the same lock.
        if activation.state != "SUSPENDED":
            raise ArpError("STATE_COMBINATION_INVALID", f"RESUME applies to SUSPENDED, not {activation.state}")
        if activation.evaluation_ref is None or self.admission_for(revision, activation.evaluation_ref) != acceptance_ref:
            raise ArpError("SKILL_TRIAL_REQUIRED", "the suspended revision was never admitted under this acceptance")
        self._verified_acceptance(revision, activation, acceptance_ref, policy=None, scope=None, lock_ref=lock_ref)
        return self.catalogue.transition(revision.pin, state="ADMITTED", caller=caller, command_id=command_id, admission_ref=acceptance_ref, run_id=run_id)


__all__ = ("ACTION_TARGET", "TRIAL_TTL_MS", "SkillLifecycleService")
