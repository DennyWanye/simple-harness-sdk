# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""COMPARE's side receipts, on the existing Commit/lease/verification engine.

READY is not Result PASS. Only accept_selected_result can publish this mode;
all candidate selection writes share the original Store transaction.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..artifacts.store import read_verified
from ..contracts import TERMINAL_MISSION, TERMINAL_TASK, AttemptStatus, Task, TaskStatus
from ..contracts.models import canonical_json, jsonable, sha256_hex
from ..governance.permissions import decision_receipt_hash
from ..governance.tail_budget import TailAllocation, TailBudgetLedger, TailReserve
from ..memory.source_dependencies import merge_source_versions, source_versions_current_issues
from ..planning.candidate_selection import (
    COMPARE,
    SELECTION_VERSION,
    candidate_path,
    selection_revision,
    validate_selection_policy,
)
from ..scheduling.allocator import OPEN_ATTEMPT_STATES


class SelectionCommitsMixin:
    if TYPE_CHECKING:
        from ..governance.budgets import BudgetLedger
        from ..storage.store import Store

        _store: Store
        _ledger: BudgetLedger

        def _require_task(self, task_id: str) -> Task: ...
        def _require_mission(self, mission_id: str) -> Any: ...
        def _require_attempt(self, attempt_id: str) -> Any: ...
        def _require_result(self, result_id: str) -> Any: ...
        def _emit(self, *args: Any, **kwargs: Any) -> Any: ...
        def _acceptance_materials(self, *args: Any, **kwargs: Any) -> Any: ...
        def _accept_result(self, *args: Any, **kwargs: Any) -> Task: ...
        def fail_result(self, *args: Any, **kwargs: Any) -> Task: ...
        def _source_cas(self) -> Any: ...
        def _close_attempt(self, *args: Any, **kwargs: Any) -> Any: ...
        def domain_for(self, mission_id: str) -> Any: ...
        def stop_task(self, *args: Any, **kwargs: Any) -> Any: ...

    @staticmethod
    def _selection_task_account(task_id: str) -> str:
        from .commit_service import task_account

        return task_account(task_id)

    def _selection_error(self, message: str):
        from .commit_service import CommitRejected

        return CommitRejected(message)

    def approved_search_policy(self, version_id: str) -> dict[str, Any]:
        """Read the same authentic promotion proof used for a new Mission bind."""
        with self._store.read_view():
            return self._approved_search_policy(version_id)

    def _approved_search_policy(self, version_id: str) -> dict[str, Any]:
        version = self._store.get_policy_version(version_id)
        if (
            not version
            or version["status"] != "ACTIVE"
            or version["params"].get("schema_version") != 2
        ):
            raise self._selection_error("search policy is not an active approved successor")
        policy = validate_selection_policy(version["params"]["search_selection"])
        proposals = [
            p
            for p in self._store.list_policy_proposals()
            if p["version_id"] == version_id and p["state"] == "PROMOTED"
        ]
        for proposal in proposals:
            approval = proposal.get("approval") or {}
            evaluation = proposal.get("last_evaluation") or {}
            if evaluation not in self._store.list_policy_evaluations(proposal["proposal_id"]):
                continue
            for decision in self._store.list_policy_decisions(proposal["proposal_id"]):
                expected = decision_receipt_hash(
                    request_id=proposal["proposal_id"],
                    binding=decision["binding"],
                    principal_id=decision["principal_id"],
                    decision=decision["decision"],
                    nonce=decision["nonce"],
                )
                if (
                    decision["decision"] == "approve"
                    and expected == decision["receipt_hash"] == approval.get("receipt_hash")
                    and dict(decision["binding"])
                    == {
                        "proposal_id": proposal["proposal_id"],
                        "version_id": version_id,
                        "evaluation_id": evaluation.get("evaluation_id"),
                        "baseline_version_id": evaluation.get("baseline_version_id"),
                    }
                    and evaluation.get("evaluation_id") is not None
                    and evaluation.get("verdict") == "PASSED"
                    and any(
                        a.get("approval_receipt") == expected
                        and a["version_id"] == version_id
                        and a.get("proposal_id") == proposal["proposal_id"]
                        and a.get("action") == "promote"
                        for a in self._store.list_policy_activations()
                    )
                ):
                    return {
                        "schema_version": 1,
                        "version_id": version_id,
                        "policy_hash": sha256_hex(policy),
                        "approval_receipt_id": expected,
                        "proposal_id": proposal["proposal_id"],
                        "evaluation_id": evaluation["evaluation_id"],
                        "evidence_kind": evaluation.get("evidence_kind"),
                        "policy": policy,
                    }
        raise self._selection_error("search policy has no matching evaluation/approval/promotion")

    def bind_search_policy(self, mission_id: str, version_id: str) -> dict[str, Any]:
        with self._store.transaction():
            self._require_mission(mission_id)
            binding = {**self._approved_search_policy(version_id), "mission_id": mission_id}
            known = self.search_binding(mission_id)
            if known is not None:
                if known != binding:
                    raise self._selection_error("Mission search policy is immutable")
                return known
            self._store.connection.execute(
                "INSERT INTO search_bindings VALUES(?,?)", (mission_id, canonical_json(binding))
            )
            self._emit("SearchPolicyBound", mission_id, key=mission_id, payload=binding)
            return binding

    def search_binding(self, mission_id: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT json FROM search_bindings WHERE mission_id=?",
            (mission_id,),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def selection_policy_for(self, task_id: str) -> dict[str, Any] | None:
        task = self._require_task(task_id)
        if task.kind != "work" or "fragment_validation" in task.context:
            return None
        binding = self.search_binding(task.mission_id)
        if binding is None or binding["policy"]["mode"] != COMPARE:
            return None
        policy = validate_selection_policy(binding["policy"])
        if sha256_hex(policy) != binding["policy_hash"]:
            raise self._selection_error("frozen search binding is inconsistent")
        return policy

    def selection_round(self, task_id: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT json FROM selection_rounds WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def selection_candidates(self, task_id: str) -> list[dict[str, Any]]:
        round_ = self.selection_round(task_id)
        if round_ is None:
            return []
        return [
            json.loads(row[0])
            for row in self._store.connection.execute(
                "SELECT json FROM selection_candidates WHERE round_id=? ORDER BY result_id",
                (round_["round_id"],),
            )
        ]

    def _selection_round_id(self, round_id: str) -> dict[str, Any]:
        row = self._store.connection.execute(
            "SELECT json FROM selection_rounds WHERE round_id=?",
            (round_id,),
        ).fetchone()
        if row is None:
            raise self._selection_error("unknown selection round")
        return json.loads(row[0])

    def _save_selection_round(self, round_: dict[str, Any]) -> None:
        previous = round_["version"]
        round_["version"] += 1
        changed = self._store.connection.execute(
            "UPDATE selection_rounds SET version=?,state=?,json=? WHERE round_id=? AND version=?",
            (
                round_["version"],
                round_["state"],
                canonical_json(round_),
                round_["round_id"],
                previous,
            ),
        ).rowcount
        if changed != 1:
            raise self._selection_error("selection round version conflict")
        self._emit(
            "SelectionRoundUpdated",
            round_["mission_id"],
            key=f"{round_['round_id']}:{round_['version']}",
            task_id=round_["task_id"],
            payload=round_,
        )

    def _selection_replay(self, mission_id: str, command_id: str, body: Mapping[str, Any]):
        if not isinstance(command_id, str) or not command_id.strip():
            raise self._selection_error("selection command needs an idempotency key")
        cid = "selection-command:" + sha256_hex([mission_id, command_id])
        old = self._store.get_receipt(cid)
        if old is not None and old.get("command_hash") != sha256_hex(dict(body)):
            raise self._selection_error("selection idempotency body conflict")
        return cid, old

    def _selection_receipt(
        self,
        cid: str,
        kind: str,
        round_: Mapping[str, Any],
        body: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        receipt = {**dict(result), "receipt_id": cid, "command_hash": sha256_hex(dict(body))}
        self._store.insert_receipt(
            commit_id=cid,
            kind=kind,
            subject_id=round_["task_id"],
            base_version=round_["version"],
            proposal_hash=receipt["command_hash"],
            receipt=receipt,
        )
        return receipt

    def _selection_live(self, round_: Mapping[str, Any], expected: int | None = None) -> Task:
        task = self._require_task(round_["task_id"])
        mission = self._require_mission(task.mission_id)
        if (
            task.status in TERMINAL_TASK
            or mission.status in TERMINAL_MISSION
            or selection_revision(self._store, task) != round_["task_revision_id"]
            or self.selection_policy_for(task.id) != round_["policy"]
        ):
            raise self._selection_error("selection task/revision/mission no longer live")
        if expected is not None and round_["version"] != expected:
            raise self._selection_error("selection round version conflict")
        return task

    def _selection_owner(self, attempt_id: str, owner: str) -> None:
        attempt = self._require_attempt(attempt_id)
        if (
            not isinstance(owner, str)
            or not owner.strip()
            or attempt.lease_owner != owner
            or attempt.lease_expires_at is None
            or attempt.lease_expires_at <= self._store.now
        ):
            raise self._selection_error(
                "selection requires a nonempty current owner and live lease"
            )

    def begin_selection_round(self, task_id: str, *, command_id: str) -> dict[str, Any]:
        with self._store.transaction():
            task = self._require_task(task_id)
            old = self.selection_round(task_id)
            if old is not None:
                return old
            policy = self.selection_policy_for(task_id)
            if policy is None or task.status in TERMINAL_TASK:
                raise self._selection_error("no approved live COMPARE task")
            if self._store.list_attempts(task_id):
                raise self._selection_error("selection must begin before exploration")
            revision = selection_revision(self._store, task)
            round_ = {
                "round_id": "selection:" + sha256_hex([task_id, revision]),
                "task_id": task_id,
                "mission_id": task.mission_id,
                "task_revision_id": revision,
                "policy": policy,
                "state": "COLLECTING",
                "version": 1,
                "deadline_at": self._store.now + policy["deadline_seconds"],
                "attempt_ids": [],
                "synthesis_attempt_id": None,
                "decision_id": None,
            }
            if policy["synthesis_limit"]:
                TailBudgetLedger(self._ledger).reserve_selection_tail(
                    round_["round_id"],
                    self._selection_task_account(task_id),
                    TailReserve(**policy["synthesis_reserve"], attempts=1),
                    mission_id=task.mission_id,
                    task_revision=revision,
                )
            self._store.connection.execute(
                "INSERT INTO selection_rounds VALUES(?,?,?,?,?,?)",
                (
                    round_["round_id"],
                    task.mission_id,
                    task_id,
                    1,
                    "COLLECTING",
                    canonical_json(round_),
                ),
            )
            cid, _ = self._selection_replay(task.mission_id, command_id, {"task_id": task_id})
            self._selection_receipt(cid, "selection_round", round_, {"task_id": task_id}, round_)
            self._emit(
                "SelectionRoundUpdated",
                task.mission_id,
                key=f"{round_['round_id']}:1",
                task_id=task_id,
                payload=round_,
            )
            return round_

    def _candidate_eligibility(
        self,
        result_id: str,
        round_: Mapping[str, Any],
        owner: str,
        *,
        connectors=None,
        deployment=None,
    ) -> dict[str, Any] | None:
        task = self._selection_live(round_)
        stored = self._require_result(result_id)
        attempt = self._require_attempt(stored.envelope.attempt_id)
        self._selection_owner(attempt.id, owner)
        if (
            stored.envelope.task_id != task.id
            or stored.envelope.mission_id != task.mission_id
            or stored.verification_state != "RUNNING"
            or stored.verdict is not None
            or attempt.status is not AttemptStatus.VERIFYING
        ):
            raise self._selection_error("candidate has no live recorded verification")
        rows = self._store.list_verifications(result_id)
        passes = [dict(row) for row in rows if row["status"] == "PASS"]
        materials = self._acceptance_materials(
            stored,
            task,
            attempt,
            self._require_mission(task.mission_id),
            verifier_results=passes,
            owner=owner,
            connectors=connectors,
            deployment=deployment,
        )
        if isinstance(materials, Task):
            return None
        lineage = materials.get("source_dependencies") or {}
        for versions, issues in lineage.values():
            current = [
                *issues,
                *source_versions_current_issues(
                    self._store, task.mission_id, versions, self._source_cas()
                ),
            ]
            if current:
                self.fail_result(
                    result_id,
                    failures=[
                        {
                            "layer": "rule_check",
                            "status": "ERROR"
                            if any(issue.get("code") == "ERROR" for issue in current)
                            else "FAIL",
                            "summary": "selection source lineage no longer current",
                            "detail": {"reason": "stale_source", "source_current_issues": current},
                        }
                    ],
                    owner=owner,
                )
                return None
        by_layer = {row["layer"]: row for row in rows}
        for layer in {"format_check", "rule_check", *task.verification_policy}:
            row = by_layer.get(layer)
            # The shared doc guard already verifies the exact review/result/receipt.
            human_exception = (
                self.domain_for(task.mission_id).id == "doc-research-v1"
                and row is not None
                and row["status"] == "NEEDS_HUMAN"
                and by_layer.get("human_review", {}).get("status") == "PASS"
            )
            if row is None or (row["status"] != "PASS" and not human_exception):
                raise self._selection_error(f"candidate lacks actual required layer {layer}")
        if any(row["status"] in {"FAIL", "ERROR"} for row in rows):
            raise self._selection_error("candidate has a hard failed layer")
        artifacts = []
        for artifact_id in stored.artifacts:
            artifact = self._store.get_artifact(artifact_id)
            if artifact is None or artifact.attempt_id != attempt.id:
                raise self._selection_error("candidate artifact binding missing")
            read_verified(artifact)
            artifacts.append(
                {"id": artifact.id, "hash": artifact.content_hash, "path": artifact.path}
            )
        intent = self._store.get_intent_for_subject(attempt.id)
        if intent is None:
            raise self._selection_error("candidate intent missing")
        critic_receipts = [
            "critic-verdict:" + item.intent_id
            for item in self._store.list_intents("SETTLED")
            if item.kind == "critic"
            and item.subject_id.startswith(attempt.id + ":critic:")
            and self._store.get_receipt("critic-verdict:" + item.intent_id)
        ]
        human_receipts = [
            decision["receipt_hash"]
            for row in rows
            if row["layer"] == "human_review"
            for decision in self._store.list_decisions(row["detail"].get("request_id", ""))
            if decision.get("decision") == "grant"
        ]
        return {
            "result_id": result_id,
            "attempt_id": attempt.id,
            "round_id": round_["round_id"],
            "task_id": task.id,
            "mission_id": task.mission_id,
            "task_revision_id": round_["task_revision_id"],
            "verification_rows_hash": sha256_hex(rows),
            "artifact_refs": artifacts,
            "required_layer_set": sorted({"format_check", "rule_check", *task.verification_policy}),
            "eligibility_version": SELECTION_VERSION,
            "result_hash": sha256_hex(stored.envelope.to_json()),
            "source_binding_hash": sha256_hex(
                {
                    "source_versions": intent.config.get("source_versions", {}),
                    "source_roots": intent.config.get("source_roots", []),
                    "lineage": jsonable(lineage),
                }
            ),
            "critic_receipt_ids": sorted(critic_receipts),
            "human_receipt_ids": sorted(human_receipts),
            "source_versions": jsonable(merge_source_versions(*(v for v, _ in lineage.values()))),
            "source_provenance_issues": [
                issue for _, issues in lineage.values() for issue in issues
            ],
        }

    def _selection_source_lineage(self, attempt_id: str) -> tuple[dict, list[dict]]:
        """Actual C material lineage; never trust model-declared used_knowledge."""
        attempt = self._require_attempt(attempt_id)
        round_ = self.selection_round(attempt.task_id)
        if round_ is None or round_["synthesis_attempt_id"] != attempt_id:
            return {}, []
        parts, issues = [], []
        try:
            self._selection_live(round_)
            decision = self._store.get_receipt(round_["decision_id"])
            if decision is None or decision.get("action") != "synthesize":
                raise self._selection_error("selection material decision unavailable")
            for selected in decision["selected_inputs"]:
                receipt = self._store.get_receipt(selected["receipt_id"])
                state = self._store.connection.execute(
                    "SELECT state FROM selection_candidates WHERE result_id=? AND round_id=?",
                    (selected["result_id"], round_["round_id"]),
                ).fetchone()
                original_result = self._require_result(selected["result_id"])
                if (
                    receipt is None
                    or state is None
                    or state[0] != "READY"
                    or original_result.verification_state != "RUNNING"
                    or original_result.verdict is not None
                    or receipt["round_id"] != round_["round_id"]
                    or receipt["task_revision_id"] != round_["task_revision_id"]
                    or any(selected.get(key) != value for key, value in receipt.items())
                    or "source_versions" not in receipt
                    or receipt["verification_rows_hash"]
                    != sha256_hex(self._store.list_verifications(receipt["result_id"]))
                ):
                    raise self._selection_error("selection material receipt changed")
                for ref in receipt["artifact_refs"]:
                    artifact = self._store.get_artifact(ref["id"])
                    if artifact is None or artifact.content_hash != ref["hash"]:
                        raise self._selection_error("selection material artifact changed")
                    read_verified(artifact)
                parts.append(receipt["source_versions"])
                issues.extend(receipt["source_provenance_issues"])
            versions = merge_source_versions(*parts)
            issues.extend(
                source_versions_current_issues(
                    self._store, attempt.mission_id, versions, self._source_cas()
                )
            )
            return versions, issues
        except (ValueError, RuntimeError, KeyError) as error:
            return {}, [
                {
                    "code": "ERROR",
                    "reason": "selection_source_lineage_unavailable",
                    "detail": str(error),
                }
            ]

    def record_candidate_ready(
        self,
        result_id: str,
        *,
        owner: str,
        round_id: str,
        expected_round_version: int,
        command_id: str,
        connectors=None,
        deployment=None,
    ) -> dict[str, Any] | None:
        with self._store.transaction():
            round_ = self._selection_round_id(round_id)
            body = {"result_id": result_id, "round_id": round_id, "owner": owner}
            cid, replay = self._selection_replay(round_["mission_id"], command_id, body)
            if replay is not None:
                return replay
            self._selection_live(round_, expected_round_version)
            if self._store.now >= round_["deadline_at"]:
                raise self._selection_error("selection deadline elapsed")
            result = self._require_result(result_id)
            allowed = (
                round_["attempt_ids"]
                if round_["state"] == "COLLECTING"
                else [round_["synthesis_attempt_id"]]
                if round_["state"] == "SYNTHESIZING"
                else []
            )
            if result.envelope.attempt_id not in allowed:
                raise self._selection_error("result does not belong to this round phase")
            ready = self._candidate_eligibility(
                result_id, round_, owner, connectors=connectors, deployment=deployment
            )
            if ready is None:
                return None
            receipt = self._selection_receipt(
                cid, "candidate_ready", round_, body, {**ready, "created_at": self._store.now}
            )
            record = {**receipt, "state": "READY"}
            self._store.connection.execute(
                "INSERT INTO selection_candidates VALUES(?,?,?,?)",
                (result_id, round_id, "READY", canonical_json(record)),
            )
            self._save_selection_round(round_)
            self._emit(
                "CandidateReady",
                round_["mission_id"],
                key=result_id,
                task_id=round_["task_id"],
                attempt_id=ready["attempt_id"],
                payload=record,
            )
            return receipt

    def selection_waiting_ids(self, *, mission_id: str | None = None) -> frozenset[str]:
        rows = self._store.connection.execute(
            "SELECT c.json,r.json FROM selection_candidates c JOIN selection_rounds r "
            "ON c.round_id=r.round_id WHERE c.state='READY' "
            "AND r.state IN ('COLLECTING','DECIDED','SYNTHESIZING')"
        )
        return frozenset(
            json.loads(c)["attempt_id"]
            for c, r in rows
            if mission_id is None or json.loads(r)["mission_id"] == mission_id
        )

    def candidate_is_waiting(self, result_id: str) -> bool:
        row = self._store.connection.execute(
            "SELECT json FROM selection_candidates WHERE result_id=? AND state='READY'",
            (result_id,),
        ).fetchone()
        return row is not None and json.loads(row[0])["attempt_id"] in self.selection_waiting_ids()

    def _ready_current(
        self,
        candidate: Mapping[str, Any],
        round_: Mapping[str, Any],
        owner: str,
        *,
        connectors=None,
        deployment=None,
    ) -> bool:
        current = self._candidate_eligibility(
            candidate["result_id"], round_, owner, connectors=connectors, deployment=deployment
        )
        if current is None:
            return False
        original = self._store.get_receipt(candidate["receipt_id"])
        if original is None or any(current[k] != original.get(k) for k in current):
            raise self._selection_error("candidate ready receipt no longer matches verification")
        return True

    def decide_selection(
        self, task_id: str, *, owner: str, command_id: str, connectors=None, deployment=None
    ) -> dict[str, Any] | None:
        with self._store.transaction():
            round_ = self.selection_round(task_id)
            if round_ is None:
                return None
            self._selection_live(round_)
            if round_["state"] not in {"COLLECTING", "SYNTHESIZING"}:
                known = (
                    self._store.get_receipt(round_["decision_id"])
                    if round_["decision_id"]
                    else None
                )
                return None if known is None else dict(known)
            if not isinstance(owner, str) or not owner.strip():
                raise self._selection_error("selection decision requires a nonempty owner")
            ready = self.selection_candidates(task_id)
            valid = []
            for candidate in ready:
                if candidate["state"] != "READY":
                    continue
                self._selection_owner(candidate["attempt_id"], owner)
                try:
                    eligible = self._ready_current(
                        candidate, round_, owner, connectors=connectors, deployment=deployment
                    )
                    reason = "candidate verification or source no longer current"
                except (ValueError, RuntimeError) as error:
                    # The owner/round fence above is not a qualification failure.
                    # Receipt, source or artifact changes are durable invalidations.
                    eligible = False
                    reason = str(error)
                if eligible:
                    valid.append(candidate)
                else:
                    self._invalidate_selection_candidate(candidate, reason=reason, owner=owner)
            expired = self._store.now >= round_["deadline_at"]
            live = [
                a
                for a in self._store.list_attempts(task_id)
                if a.status in OPEN_ATTEMPT_STATES and a.id not in self.selection_waiting_ids()
            ]
            if round_["state"] == "SYNTHESIZING":
                c = next(
                    (
                        item
                        for item in valid
                        if item["attempt_id"] == round_["synthesis_attempt_id"]
                    ),
                    None,
                )
                if c and not expired:
                    known = self._store.get_receipt(round_["decision_id"])
                    return None if known is None else dict(known)
                if live and not expired:
                    return None
            elif not expired and (
                live or len(round_["attempt_ids"]) < round_["policy"]["max_candidates"]
            ):
                return None
            complete = [item for item in valid if item["attempt_id"] in round_["attempt_ids"]]
            # Deterministic, approved v1: preserve all eligible inputs in stable ID
            # order and independently synthesize once. Never invent a Judge run.
            action = (
                "synthesize"
                if len(complete) >= 2
                and not expired
                and round_["policy"]["synthesis_limit"]
                and round_["synthesis_attempt_id"] is None
                else "accept_complete"
                if complete
                else "stop"
            )
            chosen = complete if action == "synthesize" else complete[:1]
            chosen_ids = {item["result_id"] for item in chosen}
            eligible_ids = {item["result_id"] for item in valid}
            considered = []
            for item in self.selection_candidates(task_id):
                eligible = item["result_id"] in eligible_ids
                reason = (
                    ("selected_for_synthesis" if action == "synthesize" else "selected_complete")
                    if item["result_id"] in chosen_ids
                    else (
                        "eligible_not_selected_by_stable_order"
                        if eligible
                        else item.get("reason", "not_eligible")
                    )
                )
                considered.append({**item, "eligible": eligible, "reason": reason})
            body = {
                "round_id": round_["round_id"],
                "version": round_["version"],
                "action": action,
                "selected_results": [item["result_id"] for item in chosen],
            }
            cid, replay = self._selection_replay(round_["mission_id"], command_id, body)
            if replay:
                return replay
            decision = self._selection_receipt(
                cid,
                "selection_decision",
                round_,
                body,
                {
                    **body,
                    "expected_round_version": round_["version"],
                    "task_revision_id": round_["task_revision_id"],
                    "policy_hash": sha256_hex(round_["policy"]),
                    "task_id": task_id,
                    "judge_intent_id": None,
                    "reason": "deadline" if expired else "bounded_candidates_complete",
                    "rule": SELECTION_VERSION,
                    "considered": considered,
                    "selected_inputs": chosen,
                    "budget_snapshot": self._ledger.account(
                        self._selection_task_account(task_id)
                    ).to_json(),
                },
            )
            round_.update(state="DECIDED", decision_id=cid)
            self._emit(
                "SelectionDecisionRecorded",
                round_["mission_id"],
                key=cid,
                task_id=task_id,
                payload=decision,
            )
            self._save_selection_round(round_)
            return decision

    def _invalidate_selection_candidate(
        self, candidate: Mapping[str, Any], *, reason: str, owner: str
    ) -> None:
        attempt = self._require_attempt(candidate["attempt_id"])
        if attempt.status is AttemptStatus.VERIFYING:
            self.fail_result(
                candidate["result_id"],
                failures=[
                    {
                        "layer": "rule_check",
                        "status": "FAIL",
                        "summary": reason,
                        "detail": {"reason": "selection_candidate_invalidated"},
                    }
                ],
                owner=owner,
            )
        record = {**dict(candidate), "state": "INVALIDATED", "reason": reason}
        self._store.connection.execute(
            "UPDATE selection_candidates SET state='INVALIDATED',json=? WHERE result_id=?",
            (canonical_json(record), candidate["result_id"]),
        )
        self._emit(
            "CandidateInvalidated",
            candidate["mission_id"],
            key=candidate["result_id"],
            task_id=candidate["task_id"],
            attempt_id=candidate["attempt_id"],
            payload=record,
        )

    def selection_input_artifacts(self, decision_id: str) -> list[Any]:
        decision = self._store.get_receipt(decision_id)
        if decision is None or decision.get("action") != "synthesize":
            raise self._selection_error("no synthesis decision")
        inputs = []
        for candidate in decision["selected_inputs"]:
            for ref in candidate["artifact_refs"]:
                artifact = self._store.get_artifact(ref["id"])
                if artifact is None or artifact.content_hash != ref["hash"]:
                    raise self._selection_error("selected input disappeared")
                read_verified(artifact)
                inputs.append(
                    replace(artifact, path=candidate_path(candidate["result_id"], ref["path"]))
                )
        return inputs

    def accept_selected_result(
        self,
        result_id: str,
        *,
        round_id: str,
        decision_id: str,
        expected_round_version: int,
        owner: str,
        command_id: str,
        connectors=None,
        deployment=None,
    ) -> dict[str, Any]:
        with self._store.transaction():
            if not isinstance(owner, str) or not owner.strip():
                raise self._selection_error("selection acceptance requires owner")
            round_ = self._selection_round_id(round_id)
            body = {"result_id": result_id, "round_id": round_id, "decision_id": decision_id}
            cid, replay = self._selection_replay(round_["mission_id"], command_id, body)
            if replay:
                return replay
            task = self._selection_live(round_, expected_round_version)
            decision = self._store.get_receipt(decision_id)
            if (
                decision is None
                or round_["decision_id"] != decision_id
                or decision.get("round_id") != round_id
            ):
                raise self._selection_error("selection decision binding mismatch")
            candidate = next(
                (c for c in self.selection_candidates(task.id) if c["result_id"] == result_id), None
            )
            if candidate is None:
                raise self._selection_error("result has no selection readiness receipt")
            if decision["action"] == "synthesize":
                if (
                    candidate["attempt_id"] != round_["synthesis_attempt_id"]
                    or self._store.now >= round_["deadline_at"]
                ):
                    raise self._selection_error("result is not the timely bound synthesis")
            elif (
                decision["action"] != "accept_complete"
                or result_id not in decision["selected_results"]
            ):
                raise self._selection_error("result is not the selected complete candidate")
            if not self._ready_current(
                candidate, round_, owner, connectors=connectors, deployment=deployment
            ):
                return {"result_id": result_id, "accepted": False}
            completed = self._accept_result(
                result_id,
                verifier_results=self._store.list_verifications(result_id),
                owner=owner,
                connectors=connectors,
                deployment=deployment,
            )
            if completed.status is not TaskStatus.COMPLETED:
                return {"result_id": result_id, "accepted": False}
            round_["state"] = "COMMITTED"
            self._save_selection_round(round_)
            if round_["policy"]["synthesis_limit"]:
                TailBudgetLedger(self._ledger).release_tail(
                    round_id, task_revision=round_["task_revision_id"], reason="selection_completed"
                )
            return self._selection_receipt(
                cid,
                "selection_acceptance",
                round_,
                body,
                {"accepted": True, "result_id": result_id, "task_id": task.id},
            )

    def _admit_selection_attempt(
        self, task: Task, *, decision_id: str | None, owner: str | None, reservation: Any
    ) -> dict[str, Any] | None:
        policy = self.selection_policy_for(task.id)
        if policy is None:
            if decision_id is not None:
                raise self._selection_error("selection decision on a non-COMPARE task")
            return None
        round_ = self.selection_round(task.id)
        if round_ is None:
            raise self._selection_error("COMPARE exploration requires its durable round/tail first")
        self._selection_live(round_)
        if self._store.now >= round_["deadline_at"]:
            raise self._selection_error("selection deadline elapsed")
        if decision_id is None:
            if (
                round_["state"] != "COLLECTING"
                or len(round_["attempt_ids"]) >= policy["max_candidates"]
            ):
                raise self._selection_error("bounded selection candidates already consumed")
        else:
            decision = self._store.get_receipt(decision_id)
            if (
                round_["state"] != "DECIDED"
                or round_["decision_id"] != decision_id
                or round_["synthesis_attempt_id"] is not None
                or decision is None
                or decision.get("action") != "synthesize"
            ):
                raise self._selection_error("no unused synthesis decision")
            if not isinstance(owner, str) or not owner.strip():
                raise self._selection_error("synthesis requires a nonempty owner")
            for candidate in decision["selected_inputs"]:
                if not self._ready_current(candidate, round_, owner):
                    raise self._selection_error("synthesis input no longer eligible")
            for field in ("tokens", "cost_micros", "tool_calls"):
                if getattr(reservation, field) > policy["synthesis_reserve"][field]:
                    raise self._selection_error("synthesis exceeds the approved tail amount")
        return round_

    def _register_selection_attempt(
        self, round_: dict[str, Any], attempt_id: str, *, synthesis: bool
    ) -> None:
        if synthesis:
            round_.update(synthesis_attempt_id=attempt_id, state="SYNTHESIZING")
        else:
            round_["attempt_ids"].append(attempt_id)
        self._save_selection_round(round_)

    def selection_deadline(self, attempt_id: str) -> float | None:
        attempt = self._require_attempt(attempt_id)
        round_ = self.selection_round(attempt.task_id)
        if round_ and attempt.id in [*round_["attempt_ids"], round_["synthesis_attempt_id"]]:
            return float(round_["deadline_at"])
        return None

    def _selection_service_reserve(
        self,
        attempt_id: str | None,
        subject_id: str,
        account_id: str,
        reservation: Any,
        *,
        kind: str,
        mission_id: str,
        task_id: str | None,
    ) -> bool:
        round_ = self._selection_service_identity(
            kind=kind,
            mission_id=mission_id,
            task_id=task_id,
            attempt_id=attempt_id,
            subject_id=subject_id,
            account_id=account_id,
        )
        if round_ is None:
            return False
        self._selection_live(round_)
        if self._store.now >= round_["deadline_at"]:
            raise self._selection_error("no service admission after selection deadline")
        TailBudgetLedger(self._ledger).transfer_tail(
            round_["round_id"],
            subject_id,
            [
                TailAllocation(
                    subject_id,
                    account_id,
                    "critic",
                    reservation.tokens,
                    reservation.cost_micros,
                    reservation.tool_calls,
                )
            ],
            task_revision=round_["task_revision_id"],
        )
        return True

    def _selection_service_identity(
        self,
        *,
        kind: str,
        mission_id: str,
        task_id: str | None,
        attempt_id: str | None,
        subject_id: str,
        account_id: str,
    ):
        attempts = [
            self._store.get_attempt(aid)
            for aid in (attempt_id, subject_id.rsplit(":critic:", 1)[0])
            if aid
        ]
        task_ids = {a.task_id for a in attempts if a is not None}
        if task_id is not None:
            task_ids.add(task_id)
        for tid in task_ids:
            round_ = self.selection_round(tid)
            if round_ is None or round_["synthesis_attempt_id"] is None:
                continue
            c_id = round_["synthesis_attempt_id"]
            if attempt_id != c_id and not subject_id.startswith(c_id + ":"):
                continue
            prefix = c_id + ":critic:"
            ordinal = subject_id.removeprefix(prefix)
            if (
                kind != "critic"
                or mission_id != round_["mission_id"]
                or task_id != tid
                or attempt_id != c_id
                or account_id != self._selection_task_account(tid)
                or not subject_id.startswith(prefix)
                or not ordinal.isascii()
                or not ordinal.isdecimal()
                or not ordinal
                or ordinal.startswith("0")
            ):
                raise self._selection_error("selection Critic service identity differs")
            return round_
        return None

    def expire_selection_dispatch(self, attempt_id: str, *, owner: str) -> bool:
        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            deadline = self.selection_deadline(attempt_id)
            intent = self._store.get_intent_for_subject(attempt_id)
            if deadline is None or self._store.now < deadline or intent is None:
                return False
            if intent.state not in {"PENDING", "CLAIMED", "AGENT_CREATED"}:
                return False
            if (
                intent.lease_owner not in (None, owner)
                and intent.lease_expires_at is not None
                and intent.lease_expires_at > self._store.now
            ):
                return False
            self._close_attempt(attempt, AttemptStatus.CANCELLED, reason="selection_deadline")
            return True

    def stop_selection(self, task_id: str, *, reason: str) -> None:
        from ..contracts import MissionStopReason

        with self._store.transaction():
            round_ = self.selection_round(task_id)
            if round_ is None or round_["state"] in {"COMMITTED", "EXHAUSTED", "INVALIDATED"}:
                return
            if round_["policy"]["synthesis_limit"]:
                TailBudgetLedger(self._ledger).release_tail(
                    round_["round_id"],
                    task_revision=round_["task_revision_id"],
                    reason=reason,
                )
            round_.update(state="EXHAUSTED", stop_reason=reason)
            self._save_selection_round(round_)
            self.stop_task(
                task_id,
                stop_reason=MissionStopReason.NO_PROGRESS,
                detail={"reason": reason, "selection_round_id": round_["round_id"]},
            )

    def release_terminal_selection_holds(
        self, mission_id: str, *, task_id: str | None = None
    ) -> None:
        """Release unused selection resources on cancellation, never real UNKNOWN calls."""
        mission = self._require_mission(mission_id)
        for task in self._store.list_tasks(mission_id):
            if task_id is not None and task.id != task_id:
                continue
            if mission.status not in TERMINAL_MISSION and task.status not in {
                TaskStatus.CANCELLED,
                TaskStatus.FAILED,
            }:
                continue
            round_ = self.selection_round(task.id)
            if round_ is None or round_["state"] in {"COMMITTED", "EXHAUSTED", "INVALIDATED"}:
                continue
            if round_["policy"]["synthesis_limit"]:
                TailBudgetLedger(self._ledger).release_tail(
                    round_["round_id"],
                    task_revision=round_["task_revision_id"],
                    reason="selection_subject_stopped",
                )
            round_.update(state="INVALIDATED", stop_reason="selection_subject_stopped")
            self._save_selection_round(round_)
