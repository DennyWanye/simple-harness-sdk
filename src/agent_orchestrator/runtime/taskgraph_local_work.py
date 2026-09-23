# SPDX-License-Identifier: Apache-2.0
"""Exhaustive local work facts; these do not prove external runtime settlement.

Read all original rows before classifying them. In particular, a terminal Attempt
does not hide a submitted intent, an unimported charge, or a held reservation.
The H1 runtime/Operation reader must additionally prove physical completion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.models import sha256_hex
from ..contracts.state_machines import AttemptStatus, TERMINAL_ATTEMPT
from ..storage.store import Store
from .planning_operations import SourceUnavailable


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalWorkFacts:
    mission_id: str
    canonical_document: str

    @property
    def digest(self) -> str:
        return sha256_hex(self.to_json())

    def to_json(self) -> dict[str, Any]:
        return json.loads(self.canonical_document)

    def related_subjects(self, task_ids: frozenset[str]) -> frozenset[str]:
        body = self.to_json()
        subjects = {row["attempt_id"] for row in body["attempts"] if row["task_id"] in task_ids}
        subjects.update(row["reservation_subject"] for row in body["actions"]
                        if row.get("reservation_subject") and (row.get("task_id") in task_ids
                        or row.get("attempt_id") in subjects))
        # A Critic/Manager or MethodSynthesizer can still execute on behalf of
        # the same Task. The latter's original producer stores goal_task_id.
        # Explicit config/Attempt links are the only identity join; no id parsing.
        while True:
            expanded = subjects | {row["subject_id"] for row in body["intents"]
                if row["config"].get("task_id") in task_ids
                or row["config"].get("goal_task_id") in task_ids
                or row["config"].get("attempt_id") in subjects}
            if expanded == subjects:
                break
            subjects = expanded
        return frozenset(subjects)

    def blocking_subjects(self, task_ids: frozenset[str]) -> frozenset[str]:
        body = self.to_json()
        subjects = self.related_subjects(task_ids)
        blocked = {
            row["attempt_id"] for row in body["attempts"]
            if row["attempt_id"] in subjects and AttemptStatus(row["status"]) not in TERMINAL_ATTEMPT
        }
        blocked.update(row["subject_id"] for row in body["intents"]
                       if row["subject_id"] in subjects and row["state"] not in {"SETTLED", "FAILED"})
        blocked.update(row["subject_id"] for row in body["reservations"]
                       if row["subject_id"] in subjects and row["state"] != "SETTLED")
        blocked.update(row["subject_id"] for row in body["provider_grants"]
                       if row["subject_id"] in subjects and row["state"] in {"RESERVED", "HANDED_OFF", "UNKNOWN"})
        blocked.update(row["subject_id"] for row in body["usage"]
                       if row["subject_id"] in subjects and row["unknown"])
        blocked.update(row["subject_id"] for row in body["tool_calls"]
                       if row["subject_id"] in subjects and row["outcome"] not in {"succeeded", "failed", "rejected"})
        return frozenset(blocked)


def read_local_work(store: Store, mission_id: str) -> LocalWorkFacts:
    """A consistent original-Store read, with no terminal-only or lease filter."""
    with store.read_view() as db:
        if store.get_mission(mission_id) is None:
            raise SourceUnavailable("mission_not_found")
        body: dict[str, Any] = {"mission_id": mission_id}
        # Action accounts use their own original reservation subjects. They are
        # never DispatchIntents, but an APPLIED Operation with an unreleased
        # account must still prevent declaring the affected work quiescent.
        body["actions"] = store.list_actions(mission_id)
        # Fixed SQL/table inventory, never a caller-selected table or predicate.
        for key, table, order in (
            ("attempts", "attempts", "attempt_id"),
            ("intents", "dispatch_intents", "intent_id"),
            ("reservations", "budget_reservations", "subject_id"),
            ("provider_grants", "provider_token_grants", "invocation_id,handoff_ordinal"),
            ("usage", "imported_usage", "usage_ref"),
            ("tool_calls", "tool_calls", "call_key"),
        ):
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE mission_id=? ORDER BY {order}", (mission_id,))]
            body[key] = rows
        for row in body["attempts"]:
            try:
                AttemptStatus(row["status"])
                attempt = store.get_attempt(row["attempt_id"])
                if (attempt is None or attempt.mission_id != mission_id
                        or attempt.task_id != row["task_id"] or str(attempt.status) != row["status"]
                        or attempt.version != row["version"]):
                    raise ValueError("Attempt columns differ from original body")
            except (ValueError, TypeError) as error:
                raise SourceUnavailable("taskgraph_attempt_source_invalid") from error
        for row in body["intents"]:
            if row["state"] not in {"PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED"}:
                raise SourceUnavailable("taskgraph_intent_state_unknown")
            config = json.loads(row.pop("config_json"))
            if not isinstance(config, dict):
                raise SourceUnavailable("taskgraph_intent_config_invalid")
            row["config"] = config
        for row in body["reservations"]:
            if row["state"] not in {"RESERVED", "SETTLED"}:
                raise SourceUnavailable("taskgraph_reservation_state_unknown")
        for row in body["provider_grants"]:
            if row["state"] not in {"RESERVED", "HANDED_OFF", "UNKNOWN", "SETTLED", "RELEASED", "OVERRUN"}:
                raise SourceUnavailable("taskgraph_provider_grant_state_unknown")
        return LocalWorkFacts(mission_id=mission_id, canonical_document=canonical_json(body))
