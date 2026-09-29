# SPDX-License-Identifier: Apache-2.0
"""Durable questions and authenticated answers; answers never create approvals."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from simple_harness.contracts import canonical_json
from ..contracts.models import ContractError
from ..contracts.planning_decisions import RequestHumanDecision
from ..contracts.semantic_base import content_hash_of
if TYPE_CHECKING:
    from .store import Store

DDL = """
CREATE TABLE planning_human_requests (
 decision_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 subject_key TEXT NOT NULL,
 request_json TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('PENDING','ANSWERED','STALE')),
 answer_json TEXT,
 version INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL,
 answered_at REAL
) STRICT;
CREATE INDEX planning_human_pending ON planning_human_requests(mission_id,state);
"""


class PlanningHumanStore:
    def __init__(self, store: Store):
        self.store = store

    def get(self, decision_id: str) -> dict[str, Any] | None:
        row = self.store.connection.execute(
            "SELECT * FROM planning_human_requests WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        result["answer"] = json.loads(result.pop("answer_json") or "null")
        return result

    def list(self, mission_id: str) -> list[dict[str, Any]]:
        rows = self.store.connection.execute(
            "SELECT decision_id FROM planning_human_requests WHERE mission_id=? ORDER BY created_at,decision_id",
            (mission_id,),
        ).fetchall()
        return [item for row in rows if (item := self.get(row[0])) is not None]

    def binding_current(self, row: dict[str, Any]) -> bool:
        from .htn_store import HtnStore
        htn = HtnStore(self.store)
        plan = htn.active_plan_revision(row["mission_id"])
        requirements = htn.latest_requirements_revision(row["mission_id"])
        current = {"plan_revision": 0 if plan is None else int(plan.revision),
                   "requirements_revision": 0 if requirements is None else int(requirements.revision),
                   "manager_epoch": htn.epoch(row["mission_id"], "mission")}
        return row["request"]["binding"] == current

    def retire_stale(self, mission_id: str) -> None:
        with self.store.transaction():
            for row in self.list(mission_id):
                if row["state"] == "PENDING" and not self.binding_current(row):
                    self.store.connection.execute(
                        "UPDATE planning_human_requests SET state='STALE',version=version+1 WHERE decision_id=? AND state='PENDING'",
                        (row["decision_id"],))
                    from ..orchestrator.hierarchical_dispatch import append_hierarchical_event
                    append_hierarchical_event(self.store, "PlanningHumanStale", mission_id,
                        key=row["decision_id"], payload={"decision_id": row["decision_id"],
                            "request_hash": row["request_hash"], "reason": "binding_superseded"})

    def pending(self, mission_id: str) -> bool:
        return any(r["state"] == "PENDING" and r["request"]["payload"]["blocking"] for r in self.list(mission_id))

    def register(self, *, decision_id: str, mission_id: str, subject_key: str,
                 payload: RequestHumanDecision, request_binding: dict[str, Any], next_ordinal: int,
                 repair_context: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"payload": payload.to_json(), "binding": request_binding, "next_ordinal": next_ordinal}
        if repair_context is not None:
            body["repair_context"] = dict(repair_context)
        digest = content_hash_of(body)
        from .store import StoreConflict
        with self.store.transaction():
            old = self.get(decision_id)
            if old is not None:
                if old["mission_id"] != mission_id or old["subject_key"] != subject_key or old["request_hash"] != digest:
                    raise StoreConflict("human request changed on replay")
                return old
            self.store.connection.execute(
                "INSERT INTO planning_human_requests(decision_id,mission_id,subject_key,request_json,request_hash,state,created_at) VALUES(?,?,?,?,?,'PENDING',?)",
                (decision_id, mission_id, subject_key, canonical_json(body), digest, self.store.now),
            )
            result = self.get(decision_id)
            assert result is not None
            return result

    def answer(self, *, decision_id: str, tenant_id: str, principal: Any,
               answer: str, expected_version: int, nonce: str) -> dict[str, Any]:
        if principal.kind != "human" or not nonce.strip() or not answer.strip() or len(answer) > 12000:
            raise ContractError("human answer requires an authenticated caller, nonce and bounded text")
        from .store import StoreConflict
        with self.store.transaction():
            row = self.get(decision_id)
            mission = None if row is None else self.store.get_mission(row["mission_id"])
            if row is None or mission is None or mission.tenant_id != tenant_id:
                raise StoreConflict("human request is unavailable to this caller")
            receipt = {"decision_id": decision_id, "principal_id": principal.principal_id,
                       "answer": answer, "nonce": nonce, "request_hash": row["request_hash"]}
            receipt["receipt_hash"] = content_hash_of(receipt)
            if row["state"] == "ANSWERED":
                if row["answer"] != receipt:
                    raise StoreConflict("human request already has a different answer")
                return receipt
            if row["state"] != "PENDING" or row["version"] != expected_version:
                raise StoreConflict("human request version changed")
            if str(mission.status) in {"COMPLETED", "FAILED", "CANCELLED"}:
                raise StoreConflict("cannot answer a terminal Mission")
            if not self.binding_current(row):
                raise StoreConflict("human request refers to a superseded plan or requirements")
            options = row["request"]["payload"]["options"]
            if options and answer not in {o["key"] for o in options}:
                raise ContractError("answer must name one of the offered option keys")
            from ..orchestrator.hierarchical_dispatch import append_hierarchical_event
            self.store.connection.execute(
                "UPDATE planning_human_requests SET state='ANSWERED',answer_json=?,version=version+1,answered_at=? WHERE decision_id=?",
                (canonical_json(receipt), self.store.now, decision_id),
            )
            append_hierarchical_event(self.store, "PlanningHumanAnswered", mission.id, key=decision_id,
                payload={**receipt, "next_ordinal": row["request"]["next_ordinal"]})
            self._note_answer_for_workers(mission, row, principal, answer, options)
            return receipt

    def _note_answer_for_workers(self, mission: Any, row: dict[str, Any], principal: Any,
                                 answer: str, options: list[dict[str, Any]]) -> None:
        """The answer is also a mission-level human note, so every later Attempt sees it.

        2026-09-29 真机（收口第 6 项第 4 轮）：用户在回答里给了缺的数据，但回答只进规划器；
        规划器只能让那一步原样重做，重做的执行者看不到回答，照样报缺数据，直到规划次数用完。
        任务级的 ``HumanCommentAdded``（与界面"带说明重做"同一种备注）在每个 Attempt 开工时
        作为"信息、不是授权"交给执行者。
        """

        from ..contracts import Event, ids

        chosen = next((o.get("label") for o in options if o.get("key") == answer), None)
        question = row["request"]["payload"]["question"]
        text = (f"用户回答了规划问题（任务补充资料或决定）。\n问：{question}\n"
                f"答：{answer if chosen is None else f'{chosen}（{answer}）'}")
        key = f"HumanCommentAdded:{mission.id}:planning-answer:{row['decision_id']}"
        self.store.append_event(Event(
            id=ids.event_id(key), type="HumanCommentAdded", trace_id=ids.trace_id(mission.id),
            mission_id=mission.id, task_id=None, attempt_id=None,
            actor_type="user", actor_id=principal.principal_id,
            payload={"target_id": mission.id, "principal_id": principal.principal_id, "text": text,
                     "via": f"planning_answer:{row['decision_id']}"},
            idempotency_key=key, created_at=self.store.now))
