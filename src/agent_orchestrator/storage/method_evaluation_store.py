# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""H6 evaluation of frozen, actual local Mission runs and registry transitions."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from statistics import median
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts import TERMINAL_MISSION, MissionStatus
from ..contracts.htn import MethodRef, MethodRegistryStatus, RegistryAuthor
from ..contracts.models import ContractError
from ..contracts.resolution import ReviewVerdict
from ..contracts.semantic_base import Provenance, TypedRef, TypedRefKind, content_hash_of
from ..planning.htn.method_lifecycle import (
    EvaluationSetV1,
    MethodEvaluationRecordV1,
    MethodLifecyclePolicyV1,
)
from .htn_store import HtnStore
from .store import Store, StoreConflict


def method_key(reference: MethodRef) -> str:
    return f"{reference.method_id}@{reference.version}#{reference.content_hash}"


class MethodEvaluationStore:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.htn = HtnStore(store)

    def _row(self, reference: MethodRef) -> Any:
        row = self.store.connection.execute(
            "SELECT * FROM method_evaluations WHERE method_id=? AND method_version=?",
            (reference.method_id, reference.version),
        ).fetchone()
        if row is None or row["method_hash"] != reference.content_hash:
            raise StoreConflict("method has no matching frozen evaluation")
        return row

    def freeze(
        self,
        reference: MethodRef,
        evaluation_set: EvaluationSetV1,
        *,
        baseline_mission_ids: tuple[str, ...],
        policy: MethodLifecyclePolicyV1 | None = None,
        oracle_hashes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        policy = policy or MethodLifecyclePolicyV1()
        evaluation_set = EvaluationSetV1.from_json(evaluation_set.to_json())
        if (
            not baseline_mission_ids
            or len(set(baseline_mission_ids)) != len(baseline_mission_ids)
            or set(baseline_mission_ids)
            & set((*evaluation_set.trial_ids, *evaluation_set.heldout_ids))
        ):
            raise ContractError("evaluation requires a separate, unique baseline cohort")
        document: dict[str, Any] = {
            "method_ref": reference.to_json(),
            "evaluation_set": evaluation_set.to_json(),
            "baseline_mission_ids": sorted(baseline_mission_ids),
            "policy": asdict(policy),
        }
        if oracle_hashes is not None:
            all_ids = {*baseline_mission_ids, *evaluation_set.trial_ids, *evaluation_set.heldout_ids}
            if set(oracle_hashes) != all_ids or any(
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in oracle_hashes.values()
            ):
                raise ContractError("external oracles must cover the exact frozen cohort")
            document["oracle_hashes"] = dict(oracle_hashes)
        encoded = canonical_json(document)
        with self.store.transaction():
            registered = self.htn.get_method(reference.method_id, reference.version)
            if registered.registration.method_ref != reference:
                raise StoreConflict("method definition differs from evaluation")
            existing = self.store.connection.execute(
                "SELECT frozen_json FROM method_evaluations WHERE method_id=? AND method_version=?",
                (reference.method_id, reference.version),
            ).fetchone()
            if existing is not None:
                if existing[0] != encoded:
                    raise StoreConflict("evaluation set/policy/baseline is already frozen")
                return document
            if registered.registration.status is not MethodRegistryStatus.TRIAL_ADMITTED:
                raise StoreConflict("only a trial-admitted method may enter evaluation")
            # Host-generated Mission IDs may already exist, but the cohort must
            # be frozen before any planning/worker invocation has been scheduled.
            for run_id in (
                *evaluation_set.trial_ids,
                *evaluation_set.heldout_ids,
                *baseline_mission_ids,
            ):
                mission = self.store.get_mission(run_id)
                if mission is not None and mission.status in TERMINAL_MISSION:
                    raise StoreConflict("evaluation cannot select already-finished Missions")
                started = self.store.connection.execute(
                    "SELECT 1 FROM dispatch_intents WHERE mission_id=? "
                    "UNION ALL SELECT 1 FROM attempts WHERE mission_id=? "
                    "UNION ALL SELECT 1 FROM imported_usage WHERE mission_id=? LIMIT 1",
                    (run_id, run_id, run_id),
                ).fetchone()
                if started is not None:
                    raise StoreConflict("freeze evaluation identities before scheduling their runs")
            self.store.connection.execute(
                "INSERT INTO method_evaluations(method_id,method_version,method_hash,set_hash,"
                "frozen_json,state,created_at,updated_at) VALUES(?,?,?,?,?,'FROZEN',?,?)",
                (
                    reference.method_id,
                    reference.version,
                    reference.content_hash,
                    evaluation_set.content_hash,
                    encoded,
                    self.store.now,
                    self.store.now,
                ),
            )
        return document

    def record_oracle(
        self, reference: MethodRef, mission_id: str, *, oracle_hash: str,
        passed: bool, evidence_sha256: str,
    ) -> dict[str, Any]:
        """Bind a trusted evaluator verdict to the original, settled Mission evidence.

        This system-side port consumes the independent grader's result, never a
        model completion claim. The oracle identities must have been frozen
        before scheduling. A negative verdict is evidence, not a runner error.
        """
        if type(passed) is not bool or not isinstance(evidence_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", evidence_sha256
        ) is None:
            raise ContractError("oracle verdict requires a boolean and exact evidence hash")
        with self.store.transaction():
            row = self._row(reference)
            frozen = json.loads(row["frozen_json"])
            if (mission_id not in frozen.get("oracle_hashes", {})
                    or frozen["oracle_hashes"][mission_id] != oracle_hash):
                raise StoreConflict("oracle does not match this frozen evaluation Mission")
            run = self._run(
                mission_id, None if mission_id in frozen["baseline_mission_ids"] else reference,
                require_exercised=False,
            )
            outcome = {
                "method_ref": reference.to_json(), "mission_id": mission_id,
                "frozen_hash": content_hash_of(frozen), "run_hash": content_hash_of(run),
                "oracle_hash": oracle_hash, "passed": passed, "evidence_sha256": evidence_sha256,
            }
            key = self._oracle_key(reference, mission_id)
            previous = self.store.get_receipt(key)
            if previous is not None:
                if previous != outcome:
                    raise StoreConflict("frozen oracle evidence changed")
                return outcome
            if row["state"] != "FROZEN":
                raise StoreConflict("oracle cannot be added after method evaluation")
            self.store.insert_receipt(
                commit_id=key, kind="method_evaluation_oracle", subject_id=mission_id,
                base_version=None, proposal_hash=content_hash_of(outcome), receipt=outcome,
            )
            return outcome

    @staticmethod
    def _oracle_key(reference: MethodRef, mission_id: str) -> str:
        return "method-oracle:" + content_hash_of([reference.to_json(), mission_id])

    def _apply_oracle(
        self, reference: MethodRef, frozen: dict[str, Any], run: dict[str, Any]
    ) -> dict[str, Any]:
        if "oracle_hashes" not in frozen:
            return run  # Existing evaluations retain their original evidence identity.
        outcome = self.store.get_receipt(self._oracle_key(reference, run["mission_id"]))
        if (outcome is None or outcome.get("method_ref") != reference.to_json()
                or outcome.get("mission_id") != run["mission_id"]
                or outcome.get("frozen_hash") != content_hash_of(frozen)
                or outcome.get("run_hash") != content_hash_of(run)
                or outcome.get("oracle_hash") != frozen["oracle_hashes"].get(run["mission_id"])
                or type(outcome.get("passed")) is not bool):
            raise StoreConflict("frozen evaluation oracle is absent or its original evidence changed")
        return {**run, "accepted": run["accepted"] and outcome["passed"], "oracle": dict(outcome)}

    def _run(self, mission_id: str, reference: MethodRef | None, *,
             require_exercised: bool = True) -> dict[str, Any]:
        mission = self.store.get_mission(mission_id)
        if mission is None or mission.status not in TERMINAL_MISSION:
            raise StoreConflict(f"evaluation Mission {mission_id} is not terminal")
        instances = self.htn.list_method_instances(mission_id)
        if require_exercised and reference is not None and not any(
            item.method_ref == reference for item in instances
        ):
            raise StoreConflict("evaluation Mission did not exercise the frozen method")
        usage = [
            dict(row)
            for row in self.store.connection.execute(
                "SELECT usage_ref,input_tokens,output_tokens,unknown FROM imported_usage "
                "WHERE mission_id=? ORDER BY usage_ref",
                (mission_id,),
            ).fetchall()
        ]
        if not usage or any(row["unknown"] for row in usage):
            raise StoreConflict("evaluation usage is absent or unresolved")
        if (
            self.store.connection.execute(
                "SELECT 1 FROM budget_reservations WHERE mission_id=? AND state!='SETTLED' LIMIT 1",
                (mission_id,),
            ).fetchone()
            is not None
        ):
            raise StoreConflict("evaluation still has unsettled provider work")
        roots = self.store.connection.execute(
            "SELECT json FROM missions WHERE mission_id=?", (mission_id,)
        ).fetchone()
        resolutions = tuple(
            item
            for item in self.htn.list_goal_resolutions(mission_id)
            if str(item.validity) == "CURRENT" and item.verdict is ReviewVerdict.ACCEPT
        )
        active = self.htn.active_plan_revision(mission_id)
        members = (
            () if active is None else self.htn.list_plan_memberships(mission_id, active.revision)
        )
        child_ids = {
            child.occurrence_id
            for instance in self.htn.list_method_instances(mission_id, state="ADOPTED")
            for child in instance.child_bindings
        }
        roots_tasks = {str(item.task_id) for item in members if item.occurrence_id not in child_ids}
        root_resolutions = [item for item in resolutions if str(item.goal_task_id) in roots_tasks]
        accepted = (
            mission.status is MissionStatus.COMPLETED
            and bool(roots_tasks)
            and {str(item.goal_task_id) for item in root_resolutions} == roots_tasks
        )
        if accepted and reference is not None:
            # A rejected candidate followed by a successful replacement is not a
            # success of the candidate. Attribute only current, accepted instances.
            member_tasks = {str(item.task_id) for item in members}
            current_instances = {
                str(binding.adopted_method_instance_id)
                for task_id in member_tasks
                if (binding := self.htn.task_semantics_of(mission_id, task_id)) is not None
                and binding.adopted_method_instance_id is not None
            }
            accepted = any(
                item.method_ref == reference
                and str(item.instance_id) in current_instances
                and any(r.method_instance_id == str(item.instance_id)
                        and str(r.goal_task_id) in member_tasks for r in resolutions)
                for item in self.htn.list_method_instances(mission_id, state="ADOPTED")
            )
        from ..orchestrator.scoped_content_review import uses_completion_protocol

        if accepted and uses_completion_protocol(self.store, mission_id):
            from ..orchestrator.completion_status import read_occurrence_completion

            accepted = all(
                read_occurrence_completion(self.store, mission_id, str(item.occurrence_id)).complete
                for item in members
                if str(item.task_id) in roots_tasks
            )
        for resolution in resolutions:
            stored = self.htn.get_review_record(resolution.review_receipt_id)
            official = self.htn.official_review_record(str(stored.record.package_id))
            if (
                not stored.official
                or official != stored.record
                or official.verdict is not ReviewVerdict.ACCEPT
            ):
                accepted = False
        actions = self.store.list_actions(mission_id)
        unresolved = sum(action["state"] in {"UNKNOWN", "HANDED_OFF"} for action in actions)
        critical = sum(
            action["state"] == "FAILED" or (action["state"] == "SUCCEEDED" and not accepted)
            for action in actions
        )
        return {
            "mission_id": mission_id,
            "tenant_id": mission.tenant_id,
            "mission": json.loads(roots[0]),
            "accepted": accepted,
            "resolutions": [item.to_json() for item in resolutions],
            "instances": [item.to_json() for item in instances],
            "usage": usage,
            "actions": actions,
            "unresolved": unresolved,
            "critical": critical,
            "cost_tokens": sum(
                int(row["input_tokens"]) + int(row["output_tokens"]) for row in usage
            ),
        }

    def _evaluation(
        self, reference: MethodRef, row: Any
    ) -> tuple[MethodEvaluationRecordV1, str, bool]:
        frozen = json.loads(row["frozen_json"])
        evaluation_set = EvaluationSetV1.from_json(frozen["evaluation_set"])
        runs = [
            self._apply_oracle(reference, frozen, self._run(
                run_id, reference, require_exercised="oracle_hashes" not in frozen))
            for run_id in (*evaluation_set.trial_ids, *evaluation_set.heldout_ids)
        ]
        baselines = [self._apply_oracle(reference, frozen, self._run(run_id, None))
                     for run_id in frozen["baseline_mission_ids"]]
        if len({run["tenant_id"] for run in (*runs, *baselines)}) != 1:
            raise StoreConflict("evaluation cannot combine unrelated tenants")
        baseline = float(median(run["cost_tokens"] for run in baselines))
        if baseline <= 0 or not all(run["accepted"] for run in baselines):
            raise StoreConflict("baseline needs accepted, metered runs")
        record = MethodEvaluationRecordV1(
            method_key(reference),
            evaluation_set,
            len(evaluation_set.trial_ids),
            len(evaluation_set.heldout_ids),
            sum(run["accepted"] for run in runs) / len(runs),
            sum(run["critical"] for run in runs),
            sum(run["unresolved"] for run in runs),
            tuple(float(run["cost_tokens"]) for run in runs),
        )
        evidence_hash = content_hash_of({"frozen": frozen, "runs": runs, "baselines": baselines})
        return (
            record,
            evidence_hash,
            record.meets(MethodLifecyclePolicyV1(**frozen["policy"]), baseline),
        )

    def refresh_registry(self, registry: Any, *, mission_id: str | None = None) -> None:
        """Hydrate durable lifecycle changes without changing legacy seed trial scopes."""
        rows = self.store.connection.execute(
            "SELECT frozen_json,state FROM method_evaluations"
        ).fetchall()
        evaluations = {}
        for row in rows:
            frozen = json.loads(row["frozen_json"])
            ref = MethodRef.from_json(frozen["method_ref"])
            evaluations[method_key(ref)] = (frozen, row["state"])
        for stored in self.htn.list_methods():
            registration = stored.registration
            reference = registration.method_ref
            tracked = method_key(reference) in evaluations
            if registration.status is MethodRegistryStatus.TRIAL_ADMITTED and not tracked:
                # Seed admission is scoped afresh for each deployment Mission. Its
                # immutable shared definition row is not a cross-Mission grant.
                if (
                    registry.definition(reference) is not None
                    or registration.trial_scope_mission != mission_id
                ):
                    continue
            registry.restore(stored.contract, registration)
        for frozen, state in evaluations.values():
            if state != "FROZEN":
                continue
            reference = MethodRef.from_json(frozen["method_ref"])
            evaluation_set = EvaluationSetV1.from_json(frozen["evaluation_set"])
            registry.allow_evaluation_trials(
                reference, (*evaluation_set.trial_ids, *evaluation_set.heldout_ids)
            )

    def evaluate(self, reference: MethodRef) -> dict[str, Any]:
        with self.store.transaction():
            row = self._row(reference)
            record, digest, passed = self._evaluation(reference, row)
            encoded = canonical_json(record.to_json())
            if row["evaluation_json"] is not None:
                if row["evaluation_json"] != encoded or row["evidence_hash"] != digest:
                    raise StoreConflict("frozen method evaluation evidence changed")
                return {"state": row["state"], "record": record.to_json(), "evidence_hash": digest}
            registered = self.htn.get_method(reference.method_id, reference.version).registration
            if registered.status is not MethodRegistryStatus.TRIAL_ADMITTED:
                raise StoreConflict("method left trial admission before evaluation")
            state = "EVALUATED" if passed else "REJECTED"
            self.store.connection.execute(
                "UPDATE method_evaluations SET evaluation_json=?,evidence_hash=?,state=?,updated_at=? "
                "WHERE method_id=? AND method_version=?",
                (encoded, digest, state, self.store.now, reference.method_id, reference.version),
            )
            if passed:
                self.htn.set_method_registration(
                    replace(
                        registered,
                        status=MethodRegistryStatus.EVALUATED,
                        author=RegistryAuthor.SYSTEM,
                        admission_receipt_ref=TypedRef(
                            TypedRefKind.SOURCE,
                            "method-evaluation:" + digest,
                            1,
                            digest,
                            produced_by=Provenance.TOOL,
                        ),
                    )
                )
            return {"state": state, "record": record.to_json(), "evidence_hash": digest}

    def promote(self, reference: MethodRef) -> Any:
        with self.store.transaction():
            row = self._row(reference)
            if row["state"] not in {"EVALUATED", "ADMITTED"}:
                raise StoreConflict("method has no passing evaluation")
            record, digest, passed = self._evaluation(reference, row)
            if (
                not passed
                or row["evidence_hash"] != digest
                or row["evaluation_json"] != canonical_json(record.to_json())
            ):
                raise StoreConflict("promotion evidence no longer matches its evaluation")
            registered = self.htn.get_method(reference.method_id, reference.version).registration
            expected = (
                MethodRegistryStatus.EVALUATED
                if row["state"] == "EVALUATED"
                else MethodRegistryStatus.ADMITTED
            )
            if registered.status is not expected:
                raise StoreConflict("method was suspended or changed after evaluation")
            if row["state"] == "ADMITTED":
                return registered
            admitted = replace(
                registered, status=MethodRegistryStatus.ADMITTED, trial_scope_mission=None
            )
            self.htn.set_method_registration(admitted)
            self.store.connection.execute(
                "UPDATE method_evaluations SET state='ADMITTED',updated_at=? WHERE method_id=? AND method_version=?",
                (self.store.now, reference.method_id, reference.version),
            )
            return admitted
