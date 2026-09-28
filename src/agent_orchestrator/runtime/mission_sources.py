# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The orchestrator's ``MissionSourcePort`` for ARP MISSION-mode pools (NEXT-TG-1.0 §10).

Every Agent the orchestrator creates comes from one durable dispatch intent.  This
reader turns the intent the caller's receipt names into the role-typed source set the
ARP creation records, reading only the orchestrator's own records:

* ``worker``  – the Attempt (task, task version, role) and, on a strict TaskGraph
  Mission, its frozen InputManifest identity (occurrence, contract hash, input binding
  revision, dispatch generation, manifest hash) through the validating reader; a
  Mission that is not TaskGraph-bound names that reason instead of a manifest;
* ``reviewer`` – the ReviewPackage (purpose, hash, requirements hash), the role the
  purpose demands (a package of another purpose is refused), criteria and the
  reviewed Attempt / Operation identities;
* ``critic``  – the reviewed Attempt (legacy critic without a package);
* ``planner`` / ``method_synthesizer`` / ``service`` – the frozen planning inputs of the
  intent; they have no Worker Attempt and say so, never borrowing one.

``require_current`` re-derives the same set and refuses by name when it moved, when the
intent is stopped, the Mission is terminal, a Worker Attempt is terminal, or one of the
original current-use gates (TaskGraph handoff, Assurance review handoff) refuses.  It
does not duplicate the provider guard's lease / reservation checks, which still run at
handoff.  Nothing here writes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.mission_sources import SCHEMA
from simple_harness.agents.arp.ports import TrustedCaller

from ..contracts.state_machines import TERMINAL_ATTEMPT, TERMINAL_MISSION

LIVE_INTENT_STATES = frozenset({"CLAIMED", "AGENT_CREATED", "SUBMITTED"})
#: Planning inputs frozen into a service intent when it is created.
_FROZEN_CONFIG = ("role", "runtime_profile_id", "context_version", "prompt_version", "base_version",
                  "ordinal", "plan_revision", "planning_retry_decision_id", "goal_task_id", "synthesis_round",
                  "task_id", "attempt_id", "operation_intent_id", "outcome_binding_id", "assurance_protocol",
                  "task_version", "policy_version_id", "allowed_tools", "read_only_leaf", "model")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _receipt(intent: Any) -> dict[str, str]:
    # Mirrors ``native_plane.intent_caller``: what the caller's receipt pinned.
    return {
        "intent_id": str(intent.intent_id),
        "mission_id": str(intent.mission_id),
        "kind": str(intent.kind),
        "creation_key": str(intent.creation_key),
        "input_hash": str(intent.input_hash),
    }


class MissionSourceReader:
    """``bind`` / ``require_current`` over one orchestrator (resolved lazily: the Host
    assembles pools before the orchestrator exists)."""

    def __init__(self, orchestrator: Callable[[], Any]) -> None:
        self._orchestrator = orchestrator

    def _parts(self) -> tuple[Any, Any]:
        orchestrator = self._orchestrator()
        if orchestrator is None:
            raise ArpError("SOURCE_UNAVAILABLE", "the Mission authority is not assembled yet")
        return orchestrator.store, orchestrator.commit

    # ---- port ------------------------------------------------------------------------

    def bind(self, *, caller: TrustedCaller, role: str) -> Mapping[str, Any]:
        receipt_id = caller.command_receipt_ref.id
        if not receipt_id.startswith("dispatch-intent:"):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "a Mission Agent is created only from a dispatch intent")
        store, commit = self._parts()
        with store.read_view():
            intent = store.get_intent(receipt_id[len("dispatch-intent:"):])
            if intent is None:
                raise ArpError("SOURCE_UNAVAILABLE", "the caller's dispatch intent is not recorded")
            from simple_harness.agents.arp.strict import digest

            if digest(_receipt(intent)) != caller.command_receipt_ref.content_hash:
                # another Mission / kind / creation key / input than the intent recorded
                raise ArpError("REF_IDENTITY_MISMATCH", "the caller's receipt differs from the recorded dispatch intent")
            return self._derive(store, commit, intent)

    def require_current(self, sources: Mapping[str, Any]) -> None:
        store, commit = self._parts()
        with store.read_view():
            intent = store.get_intent(str(sources.get("intent_id", "")))
            if intent is None:
                raise ArpError("SOURCE_UNAVAILABLE", "the bound dispatch intent is no longer recorded")
            if intent.state not in LIVE_INTENT_STATES:
                raise ArpError("REQUEST_SOURCE_STALE", "the bound dispatch intent is stopped",
                               detail={"state": str(intent.state)})
            mission = store.get_mission(intent.mission_id)
            if mission is None or mission.status in TERMINAL_MISSION:
                raise ArpError("REQUEST_SOURCE_STALE", "the bound Mission is terminal")
            current = self._derive(store, commit, intent)
            if _hash(current) != _hash(dict(sources)):
                changed = sorted(k for k in set(current) | set(sources) if current.get(k) != sources.get(k))
                raise ArpError("REQUEST_SOURCE_STALE", "the Mission sources moved since the Agent was created",
                               detail={"changed": changed[:16]})
        from ..storage.store import StoreError
        from ..assurance.codec import AssuranceError
        from ..orchestrator.assurance_review_transport import require_review_handoff

        try:
            with store.read_view():
                commit.require_taskgraph_handoff(intent)
        except StoreError as error:
            raise ArpError("GENERATION_STALE", "the TaskGraph authority no longer admits this intent",
                           detail={"reason": str(error)[:200]}) from error
        try:
            require_review_handoff(commit, intent)
        except AssuranceError as error:
            raise ArpError("AUTHORIZATION_REQUIRED", "the Assurance review authority no longer admits this intent",
                           detail={"reason": str(getattr(error, "code", error))[:200]}) from error

    # ---- derivation ------------------------------------------------------------------

    def _derive(self, store: Any, commit: Any, intent: Any) -> dict[str, Any]:
        config = intent.config
        mission = store.get_mission(intent.mission_id)
        if mission is None:
            raise ArpError("SOURCE_UNAVAILABLE", "the intent's Mission is not recorded")
        fragment = config.get("fragment_execution")
        context = fragment.get("context") if isinstance(fragment, Mapping) else None
        base: dict[str, Any] = {
            "schema": SCHEMA,
            **_receipt(intent),
            "subject_id": str(intent.subject_id),
            "input_id": str(intent.input_id),
            "tenant_id": str(getattr(mission, "tenant_id", "") or ""),
            "occurrence": None if not isinstance(context, Mapping) else {
                "occurrence_id": context.get("occurrence_id"), "plan_revision": context.get("plan_revision"),
            },
            "frozen_config": {k: config[k] for k in _FROZEN_CONFIG if k in config},
        }
        if config.get("review_package_id"):
            return {**base, **self._reviewer(store, intent)}
        if intent.kind == "attempt":
            return {**base, **self._worker(store, commit, intent)}
        if intent.kind == "critic":
            return {**base, "source_kind": "critic", "reviewed_attempt": self._attempt_identity(store, intent, config.get("attempt_id")),
                    "input_manifest": {"not_applicable": "critic_reviews_an_attempt_result"}}
        if config.get("role") == "method_synthesizer":
            return {**base, "source_kind": "method_synthesizer",
                    "input_manifest": {"not_applicable": "method_synthesizer_has_no_worker_attempt"}}
        if intent.kind == "plan":
            return {**base, "source_kind": "planner",
                    "input_manifest": {"not_applicable": "planner_has_no_worker_attempt"}}
        return {**base, "source_kind": "service",
                "input_manifest": {"not_applicable": f"{intent.kind}_service_turn"}}

    @staticmethod
    def _attempt_identity(store: Any, intent: Any, attempt_id: Any) -> dict[str, Any]:
        attempt = store.get_attempt(str(attempt_id)) if isinstance(attempt_id, str) and attempt_id else None
        if attempt is None or attempt.mission_id != intent.mission_id:
            raise ArpError("REF_IDENTITY_MISMATCH", "the intent's Attempt is not an Attempt of its Mission")
        return {"attempt_id": attempt.id, "task_id": attempt.task_id, "task_version": attempt.task_version,
                "role": str(attempt.role), "input_hash": attempt.input_hash}

    def _worker(self, store: Any, commit: Any, intent: Any) -> dict[str, Any]:
        configured = intent.config.get("attempt_id")
        if configured is not None and configured != intent.subject_id:
            raise ArpError("REF_IDENTITY_MISMATCH", "the intent names another Attempt than its subject")
        attempt = self._attempt_identity(store, intent, intent.subject_id)
        if attempt["input_hash"] != intent.input_hash:
            raise ArpError("REF_IDENTITY_MISMATCH", "the Attempt's frozen input differs from the intent")
        live = store.get_attempt(intent.subject_id)
        if live.status in TERMINAL_ATTEMPT:
            raise ArpError("REQUEST_SOURCE_STALE", "the Worker Attempt is terminal", detail={"status": str(live.status)})
        from ..orchestrator.taskgraph_dispatch import taskgraph_enabled

        if taskgraph_enabled(store, intent.mission_id):
            from ..contracts import ContractError
            from ..storage.store import StoreError

            try:
                # the installed, validating history reader (the same one recovery/review use)
                frozen = commit.taskgraph_attempt_context(intent.mission_id, intent.subject_id).inputs
            except (ContractError, StoreError, ValueError) as error:
                raise ArpError("REQUEST_SOURCE_STALE", "the Attempt's frozen InputManifest is unavailable or moved",
                               detail={"reason": str(error)[:200]}) from error
            identity = dict(frozen.binding.identity_json())
            if identity.get("intent_id") != intent.intent_id:
                raise ArpError("REF_IDENTITY_MISMATCH", "the frozen InputManifest belongs to another intent")
            manifest: dict[str, Any] = {"manifest_hash": frozen.binding.manifest_hash, "binding": identity}
        else:
            manifest = {"not_applicable": "mission_not_taskgraph_bound",
                        "inputs_hash": _hash(intent.config.get("inputs")),
                        "completion_inputs_hash": _hash(intent.config.get("completion_inputs"))}
        return {"source_kind": "worker", "attempt": attempt, "input_manifest": manifest}

    def _reviewer(self, store: Any, intent: Any) -> dict[str, Any]:
        from ..orchestrator.assurance_review_transport import ROLES
        from ..storage.htn_store import HtnStore
        from ..storage.store import StoreError

        package_id = str(intent.config["review_package_id"])
        try:
            package = HtnStore(store).get_review_package(package_id)
        except StoreError as error:
            raise ArpError("SOURCE_UNAVAILABLE", "the review package is not recorded") from error
        purpose = str(getattr(package.purpose, "value", package.purpose))
        role = intent.config.get("role")
        expected = ROLES.get(purpose)
        # a legacy critic intent carries no role; its package must still be a TASK_CONTENT one
        actual = role if role is not None else ("critic" if intent.kind == "critic" else None)
        if expected is None or actual != expected:
            raise ArpError("REF_IDENTITY_MISMATCH", "the review package serves another purpose than this reviewer",
                           detail={"purpose": purpose, "role": role})
        body = package.to_json()
        if str(body.get("binding", {}).get("mission_id", intent.mission_id)) != intent.mission_id:
            raise ArpError("REF_IDENTITY_MISMATCH", "the review package belongs to another Mission")
        return {
            "source_kind": "reviewer",
            "review": {
                "package_id": package_id, "purpose": purpose, "package_hash": _hash(body),
                "requirements_content_hash": package.requirements_content_hash,
                "criteria": list(intent.config.get("review_criteria") or ()),
            },
            "input_manifest": {"not_applicable": "reviewer_reads_its_review_package"},
        }


__all__ = ("LIVE_INTENT_STATES", "MissionSourceReader")
