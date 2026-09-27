# SPDX-License-Identifier: Apache-2.0
"""Execution-process projection for the TaskGraph read boundary (NEXT-TG-1.0 §8).

The strict TaskGraph read answers *what the plan is*; this module answers *what
actually happened while executing it*: every Attempt, the checks and reviews of
its result, repair requests, planning turns and the revisions they committed, and
effectful operations — with causal edges between those execution instances.

Rules (plan §8.1–§8.5):

- Read only, inside the caller's one ``Store.read_view``; never calls a model,
  reserves budget, retries or appends an event.
- Every join uses a recorded identity (intent config, attempt row, result row,
  commit receipt, repair event payload) — never time, role text or id prefixes.
- Process edges have their own vocabulary and are never ORDER/DATA edges; a rework
  is a new Attempt node with a ``rework_of`` edge, the failed one stays visible.
- Turn transcripts live in the runtime pools. They are read only on request
  (:func:`turn_items`) by exact agent id, through a whitelist: the model's visible
  words, tool names and shaped tool facts, submitted summaries, verdict reasons.
  Instructions, user input packages, hidden thinking and raw tool payloads never
  leave; every text passes the credential redactor.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ..observability.secrets import redact_text

SUMMARY_LIMIT = 240
SAY_LIMIT = 600
REASON_LIMIT = 400
MAX_REASONS = 8
MAX_ITEMS = 120

#: node kinds and edge kinds of the execution layer (a discriminated union by ``kind``)
NODE_KINDS = ("attempt", "check", "review", "planning", "repair_request", "plan_revision", "operation")
EDGE_KINDS = ("attempt_of", "rework_of", "review_of", "reviews", "repair_requested", "decision_for",
              "retry_authorized", "committed_as", "supersedes", "operation_of")

_ROLE_REVIEW = {"root_reviewer": "MISSION_FINAL", "operation_proposal_reviewer": "ACTION_PROPOSAL",
                "operation_outcome_reviewer": "OPERATION_OUTCOME", "composition_reviewer": "COMPOSITION",
                "method_plan_reviewer": "METHOD_PLAN"}
_SKIPPED_LAYERS = {"NOT_REQUIRED"}


def _cut(value: Any, limit: int) -> str:
    text, _found = redact_text(str(value or "").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return None


def _ms(value: Any) -> int | None:
    return None if value is None else int(round(float(value) * 1000))


def _summary(text: Any, source_kind: str, source_ref: str) -> dict[str, str] | None:
    cut = _cut(text, SUMMARY_LIMIT)
    return {"text": cut, "source_kind": source_kind, "source_ref": source_ref} if cut else None


def _turn_ref(intent: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if intent is None:
        return None
    config = intent["config"]
    return {"intent_id": intent["intent_id"], "agent_id": intent["agent_id"],
            "state": intent["state"], "profile_id": str(config.get("runtime_profile_id") or "") or None,
            "model": str(config.get("model") or "") or None}


class ExecutionProjection:
    """Nodes and edges of one Mission's execution process from one read cut."""

    def __init__(self, db: sqlite3.Connection, mission_id: str) -> None:
        self.db, self.mission_id = db, mission_id
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.intents: dict[str, dict[str, Any]] = {}
        self.by_subject: dict[str, dict[str, Any]] = {}
        self.by_agent: dict[str, dict[str, Any]] = {}
        self.occurrence_of_task: dict[str, str] = {}

    # ------------------------------------------------------------------ sources
    def _rows(self, sql: str, *args: Any) -> list[sqlite3.Row | tuple[Any, ...]]:
        return self.db.execute(sql, (self.mission_id, *args)).fetchall()

    def _load_intents(self) -> None:
        for row in self._rows("SELECT intent_id, kind, subject_id, state, agent_id, config_json, created_at,"
                              " updated_at FROM dispatch_intents WHERE mission_id=? ORDER BY created_at, intent_id"):
            config = _json(row[5])
            intent = {"intent_id": str(row[0]), "kind": str(row[1]), "subject_id": str(row[2]),
                      "state": str(row[3]), "agent_id": None if row[4] is None else str(row[4]),
                      "config": config if isinstance(config, dict) else {}, "created_at": row[6], "updated_at": row[7]}
            self.intents[intent["intent_id"]] = intent
            self.by_subject[intent["subject_id"]] = intent
            if intent["agent_id"]:
                self.by_agent[intent["agent_id"]] = intent
        for occurrence, task in self._rows(
                "SELECT occurrence_id, task_id FROM plan_memberships WHERE mission_id=? ORDER BY revision"):
            self.occurrence_of_task[str(task)] = str(occurrence)

    def _node(self, node_id: str, kind: str, at_ms: int | None, **fields: Any) -> dict[str, Any]:
        node = {"node_id": node_id, "kind": kind, "at_ms": at_ms, **fields}
        self.nodes[node_id] = node
        return node

    def _edge(self, kind: str, source: str, target: str, *, target_layer: str = "execution") -> None:
        edge = {"kind": kind, "source": source, "target": target, "target_layer": target_layer}
        if edge not in self.edges:
            self.edges.append(edge)

    # ------------------------------------------------------------------ build
    def build(self) -> "ExecutionProjection":
        self._load_intents()
        self._plan_revisions()
        self._attempts()
        self._checks()
        self._planning()
        self._repairs()
        self._reviews()
        self._operations()
        # an edge whose execution endpoint is not a node of this cut is not an edge
        self.edges = [e for e in self.edges if e["source"] in self.nodes
                      and (e["target_layer"] == "structure" or e["target"] in self.nodes)]
        self.edges.sort(key=lambda e: (e["kind"], e["source"], e["target"]))
        return self

    def _plan_revisions(self) -> None:
        receipts = {int(row[0]): _json(row[1]) for row in self._rows(
            "SELECT new_plan_revision, receipt_json FROM plan_commit_receipts WHERE mission_id=?")}
        for revision, state, base, created_at in self._rows(
                "SELECT revision, state, base_revision, created_at FROM plan_revisions WHERE mission_id=?"
                " ORDER BY revision"):
            node_id = f"plan_revision:{int(revision)}"
            receipt = receipts.get(int(revision))
            source = receipt.get("source") if isinstance(receipt, dict) else None
            source = source if isinstance(source, dict) else {}
            self._node(node_id, "plan_revision", _ms(created_at), plan_revision=int(revision), state=str(state),
                       base_revision=None if base is None else int(base),
                       summary=_summary(source.get("rationale"), "plan_commit_receipt", node_id))
            if base is not None:
                self._edge("supersedes", node_id, f"plan_revision:{int(base)}")
            planner = source.get("intent_id")
            if isinstance(planner, str) and planner in self.intents:
                self._edge("committed_as", f"planning:{planner}", node_id)

    def _attempts(self) -> None:
        for attempt_id, task_id, ordinal, status, raw in self._rows(
                "SELECT attempt_id, task_id, ordinal, status, json FROM attempts WHERE mission_id=?"
                " ORDER BY task_id, ordinal"):
            body = _json(raw) if isinstance(_json(raw), dict) else {}
            intent = self.by_subject.get(str(attempt_id))
            config = intent["config"] if intent else {}
            context = (config.get("fragment_execution") or {}).get("context") if isinstance(
                config.get("fragment_execution"), dict) else None
            context = context if isinstance(context, dict) else {}
            occurrence = context.get("occurrence_id") or self.occurrence_of_task.get(str(task_id))
            node_id = f"attempt:{attempt_id}"
            self._node(node_id, "attempt", _ms(body.get("created_at")), attempt_id=str(attempt_id),
                       task_id=str(task_id), occurrence_id=None if occurrence is None else str(occurrence),
                       plan_revision=context.get("plan_revision") if type(context.get("plan_revision")) is int else None,
                       ordinal=int(ordinal), status=str(status), role=str(body.get("role") or "worker"),
                       turn=_turn_ref(intent), summary=None, result_id=None)
            if occurrence is not None:
                self._edge("attempt_of", node_id, str(occurrence), target_layer="structure")
            retry_of = body.get("retry_of")
            if isinstance(retry_of, str) and retry_of:
                self._edge("rework_of", node_id, f"attempt:{retry_of}")
            decision = config.get("planning_retry_decision_id")
            if isinstance(decision, str):
                request = self._request_of_decision(decision)
                if request is not None:
                    self._edge("retry_authorized", f"planning:{request}", node_id)

    def _request_of_decision(self, decision_id: str) -> str | None:
        row = self.db.execute("SELECT d.request_id FROM planning_decisions d JOIN planning_requests r"
                              " ON r.request_id=d.request_id WHERE d.decision_id=? AND r.mission_id=?",
                              (decision_id, self.mission_id)).fetchone()
        return None if row is None else str(row[0])

    def _checks(self) -> None:
        layers: dict[str, list[dict[str, Any]]] = {}
        for result_id, layer, status, detail in self._rows(
                "SELECT v.result_id, v.layer, v.status, v.detail_json FROM verifications v JOIN results r"
                " ON r.result_id=v.result_id WHERE r.mission_id=? ORDER BY v.result_id, v.layer"):
            if str(status) in _SKIPPED_LAYERS:
                continue
            parsed = _json(detail)
            parsed = parsed if isinstance(parsed, dict) else {}
            item: dict[str, Any] = {"layer": str(layer), "status": str(status)}
            if str(status) != "PASS" and parsed.get("summary"):
                item["summary"] = _cut(parsed.get("summary"), REASON_LIMIT)
            if layer == "critic_review" and isinstance(parsed.get("critic_intent_id"), str):
                item["critic_intent_id"] = parsed["critic_intent_id"]
            layers.setdefault(str(result_id), []).append(item)
        for result_id, attempt_id, state, verdict, raw, received_at in self._rows(
                "SELECT result_id, attempt_id, verification_state, verdict, json, received_at FROM results"
                " WHERE mission_id=? ORDER BY received_at, result_id"):
            attempt_node = f"attempt:{attempt_id}"
            envelope = (_json(raw) or {}).get("envelope") if isinstance(_json(raw), dict) else None
            envelope = envelope if isinstance(envelope, dict) else {}
            if attempt_node in self.nodes:
                self.nodes[attempt_node]["summary"] = _summary(envelope.get("summary"), "result_envelope", str(result_id))
                self.nodes[attempt_node]["result_id"] = str(result_id)
                self.nodes[attempt_node]["outcome"] = _cut(envelope.get("outcome"), 40) or None
            rows = layers.get(str(result_id), [])
            critic = next((r.get("critic_intent_id") for r in rows if r.get("critic_intent_id")), None)
            failed = next((r for r in rows if r["status"] == "FAIL"), None)
            node_id = f"check:{result_id}"
            self._node(node_id, "check", _ms(received_at), result_id=str(result_id), attempt_id=str(attempt_id),
                       verdict=None if verdict is None else str(verdict), state=str(state), layers=rows,
                       turn=_turn_ref(self.intents.get(critic)) if critic else None,
                       summary=_summary(failed.get("summary"), "verification", f"{result_id}:{failed['layer']}")
                       if failed else None)
            self._edge("review_of", node_id, attempt_node)

    def _planning(self) -> None:
        decisions: dict[str, list[dict[str, Any]]] = {}
        for request_id, decision_id, decision_type, status, codes, canonical in self._rows(
                "SELECT d.request_id, d.decision_id, d.decision_type, d.status, d.rejection_codes_json,"
                " d.canonical_json FROM planning_decisions d JOIN planning_requests r ON r.request_id=d.request_id"
                " WHERE r.mission_id=? ORDER BY d.request_id, d.attempt_ordinal"):
            parsed_codes = _json(codes)
            body = _json(canonical)
            decisions.setdefault(str(request_id), []).append({
                "decision_id": str(decision_id), "decision_type": None if decision_type is None else str(decision_type),
                "status": str(status),
                "rejection_codes": [str(c) for c in parsed_codes][:16] if isinstance(parsed_codes, list) else [],
                "rationale": _cut(body.get("rationale"), REASON_LIMIT) if isinstance(body, dict) else ""})
        requests = {str(row[0]): int(row[1]) for row in self._rows(
            "SELECT request_id, base_plan_revision FROM planning_requests WHERE mission_id=?")}
        for intent in self.intents.values():
            config = intent["config"]
            role = str(config.get("role") or "")
            if intent["kind"] != "plan" or role in _ROLE_REVIEW:
                continue
            is_request = intent["intent_id"] in requests
            if not is_request and role != "method_synthesizer":
                continue
            rows = decisions.get(intent["intent_id"], [])
            last = rows[-1] if rows else None
            node_id = f"planning:{intent['intent_id']}"
            self._node(node_id, "planning", _ms(intent["created_at"]),
                       role="method_synthesizer" if role == "method_synthesizer" else "planner",
                       request_id=intent["intent_id"] if is_request else None,
                       base_plan_revision=requests.get(intent["intent_id"]),
                       decisions=[{k: v for k, v in row.items() if k != "rationale"} for row in rows],
                       turn=_turn_ref(intent),
                       summary=_summary(last["rationale"], "planning_decision", last["decision_id"])
                       if last and last["rationale"] else None)
            goal = config.get("goal_task_id")
            if isinstance(goal, str) and goal in self.occurrence_of_task:
                self._edge("decision_for", node_id, self.occurrence_of_task[goal], target_layer="structure")

    def _repairs(self) -> None:
        for seq, created_at, payload in self._rows(
                "SELECT seq, created_at, payload_json FROM events WHERE mission_id=?"
                " AND type='PlanningRepairRequested' ORDER BY seq"):
            body = _json(payload) if isinstance(_json(payload), dict) else {}
            request = body.get("request") if isinstance(body.get("request"), dict) else {}
            request_id = str(body.get("request_id") or request.get("request_id") or f"event-{seq}")
            context = request.get("context") if isinstance(request.get("context"), dict) else {}
            detail = context.get("detail") if isinstance(context.get("detail"), dict) else {}
            reasons = [f.get("summary") for f in detail.get("failures") or () if isinstance(f, dict)]
            reason = next((r for r in reasons if r), None) or detail.get("reason") or context.get("event_type")
            triggers = [str(x) for x in request.get("trigger_refs") or () if isinstance(x, str)]
            node_id = f"repair_request:{request_id}"
            self._node(node_id, "repair_request", _ms(created_at), request_id=request_id,
                       trigger_refs=triggers[:8], source_event_type=_cut(context.get("event_type"), 60) or None,
                       summary=_summary(reason, "repair_request", request_id))
            for ref in triggers:
                if f"attempt:{ref}" in self.nodes:
                    self._edge("repair_requested", f"attempt:{ref}", node_id)
        for payload, in self._rows("SELECT payload_json FROM events WHERE mission_id=?"
                                   " AND type='PlanningRepairAddressed' ORDER BY seq"):
            body = _json(payload) if isinstance(_json(payload), dict) else {}
            request = self._request_of_decision(str(body.get("decision_id") or ""))
            for repair in body.get("repair_request_ids") or ():
                if request is not None:
                    self._edge("decision_for", f"planning:{request}", f"repair_request:{repair}")

    def _reviews(self) -> None:
        records = {}
        for record_id, purpose, agent, verdict, raw, created_at in self._rows(
                "SELECT record_id, purpose, reviewer_agent_id, verdict, record_json, created_at FROM review_records"
                " WHERE mission_id=? AND official=1 ORDER BY created_at"):
            records[str(agent)] = (str(record_id), str(purpose), str(verdict), _json(raw), created_at)
        attempts = {n["attempt_id"] for n in self.nodes.values() if n["kind"] == "attempt"}
        for intent in self.intents.values():
            config = intent["config"]
            role = str(config.get("role") or "")
            purpose = _ROLE_REVIEW.get(role)
            # a critic intent that reviews no Attempt is the Mission-level judge
            judge = intent["kind"] == "critic" and str(config.get("attempt_id") or "") not in attempts
            if purpose is None and not judge:
                continue
            record = records.get(intent["agent_id"] or "")
            node_id = f"review:{intent['intent_id']}"
            reason = None
            if record is not None and isinstance(record[3], dict):
                reason = record[3].get("summary") or record[3].get("reason")
            self._node(node_id, "review", _ms(intent["created_at"]), purpose=purpose or "MISSION_JUDGE",
                       record_id=None if record is None else record[0],
                       verdict=None if record is None else record[2], turn=_turn_ref(intent),
                       summary=_summary(reason, "review_record", record[0]) if record and reason else None)
            task = config.get("task_id")
            if isinstance(task, str) and task in self.occurrence_of_task:
                self._edge("reviews", node_id, self.occurrence_of_task[task], target_layer="structure")

    def _operations(self) -> None:
        for action_key, state, raw, created_at in self._rows(
                "SELECT action_key, state, json, created_at FROM actions WHERE mission_id=? ORDER BY created_at"):
            body = _json(raw) if isinstance(_json(raw), dict) else {}
            params = body.get("params") if isinstance(body.get("params"), dict) else {}
            target = params.get("artifact_path") if isinstance(params.get("artifact_path"), str) else None
            node_id = f"operation:{action_key}"
            label = f"{body.get('connector') or ''}.{body.get('operation') or ''}".strip(".")
            self._node(node_id, "operation", _ms(created_at), action_key=str(action_key), state=str(state),
                       connector=_cut(body.get("connector"), 60) or None, operation=_cut(body.get("operation"), 60) or None,
                       target=_cut(target, 200) if target else None,
                       summary=_summary(f"{label} → {target}" if target else label, "action", str(action_key)))
            attempt = body.get("attempt_id")
            if isinstance(attempt, str) and f"attempt:{attempt}" in self.nodes:
                self._edge("operation_of", node_id, f"attempt:{attempt}")

    def step_labels(self, occurrences: Iterable[str]) -> list[dict[str, Any]]:
        """What each method step is answerable for: its method ``local_id`` and the
        ``evidence_requirement`` text of every criterion link the adopted method's
        contract ties to it — the same duty the Worker is told it owes (2026-09-26 真机:
        every child's task goal is the whole Mission goal)."""
        wanted = set(occurrences)
        contracts: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
        orders: dict[tuple[str, int], list[str]] = {}
        labels = []
        for occurrence, slot, method_id, version in self._rows(
                "SELECT c.occurrence_id, c.slot_key, i.method_id, i.method_version FROM method_child_occurrences c"
                " JOIN method_instances i ON i.mission_id=c.mission_id AND i.instance_id=c.instance_id"
                " WHERE c.mission_id=? ORDER BY c.occurrence_id"):
            if str(occurrence) not in wanted:
                continue
            key = (str(method_id), int(version))
            if key not in contracts:
                row = self.db.execute("SELECT contract_json FROM method_contracts WHERE method_id=? AND method_version=?",
                                      key).fetchone()
                body = _json(row[0]) if row else None
                composition = body.get("composition") if isinstance(body, dict) else None
                links = composition.get("criterion_links") if isinstance(composition, dict) else None
                steps = body.get("steps") if isinstance(body, dict) else None
                contracts[key] = [link for link in links or () if isinstance(link, Mapping)]
                orders[key] = [str(s.get("local_id")) for s in steps or () if isinstance(s, Mapping)]
            duties = [_cut(link.get("evidence_requirement"), 600) for link in contracts[key]
                      if link.get("child_step") == slot and link.get("evidence_requirement")]
            order = orders[key]
            labels.append({"occurrence_id": str(occurrence), "step_key": _cut(slot, 80),
                           "step_index": order.index(str(slot)) if str(slot) in order else None,
                           "duties": duties[:12]})
        return labels

    def in_flight_turns(self) -> dict[str, int]:
        """Model turns not yet imported, per runtime pool (the runtime source watermark)."""
        pending: dict[str, int] = {}
        for intent in self.intents.values():
            if intent["state"] not in {"SETTLED", "FAILED"}:
                profile = str(intent["config"].get("runtime_profile_id") or "default")
                pending[profile] = pending.get(profile, 0) + 1
        return pending


# ---------------------------------------------------------------------- turn detail
_ENVELOPE = re.compile(r"<(result_envelope|planning_decision|method_proposal|critic_verdict)>(.*?)</\1>", re.S)
_THINKING = re.compile(r"<thinking>.*?</thinking>", re.S)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)
#: ``feedback`` is the journal's catch-all kind (e.g. the SDK's mandatory-context
#: control message carries its instruction text); it never leaves (review 2026-09-28).
_TURN_KINDS = ("assistant", "tool_result")


def turn_items(records: Iterable[Any], *, review: bool) -> tuple[list[dict[str, Any]], int, int]:
    """Whitelisted, redacted items of one Agent's journal; ``(items, hidden, through_seq)``.

    Only ``assistant`` and ``tool_result`` rows are read: ``instructions``,
    ``user_input`` and the catch-all ``feedback`` rows are skipped by kind. Of
    ``metadata`` (where a provider keeps reasoning) only the journal's own
    preview marker is looked at — a large tool result is journaled in full and
    again as a context preview, and the preview is not a second call.
    ``<thinking>`` blocks are dropped."""
    items: list[dict[str, Any]] = []
    through = 0
    for record in records:
        through = max(through, int(record.seq))
        if record.kind not in _TURN_KINDS:
            continue
        message = record.message_json
        if not isinstance(message, Mapping):
            continue
        if record.kind == "tool_result":
            metadata = message.get("metadata")
            if isinstance(metadata, Mapping) and "journal_full_record_seq" in metadata:
                continue
            _push(items, _tool(message))
        else:
            for piece in _assistant(message.get("content"), review=review):
                _push(items, piece)
    hidden = max(0, len(items) - MAX_ITEMS)
    return items[-MAX_ITEMS:], hidden, through


def _push(items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    last = items[-1] if items else None
    if last is not None and item["t"] == "tool" and last["t"] == "tool" and all(
            last.get(k) == item.get(k) for k in ("tool", "ok", "path", "error")):
        last["count"] = int(last.get("count", 1)) + 1
        return
    items.append(item)


def _reasons(body: Mapping[str, Any]) -> list[dict[str, str]]:
    reasons = []
    for key in ("assessments", "findings", "mission_criteria"):
        for row in body.get(key) or ():
            if not isinstance(row, Mapping):
                continue
            text = row.get("reason") or row.get("detail") or row.get("description")
            if text:
                mark = row.get("verdict") or row.get("severity") or (
                    None if row.get("met") is None else ("met" if row.get("met") else "not_met"))
                reasons.append({"verdict": _cut(mark, 20), "text": _cut(text, REASON_LIMIT)})
    return reasons[:MAX_REASONS]


def _assistant(content: Any, *, review: bool) -> list[dict[str, Any]]:
    text = _THINKING.sub("", str(content or ""))
    out: list[dict[str, Any]] = []
    for match in _ENVELOPE.finditer(text):
        tag, body = match.group(1), _json(match.group(2).strip())
        if not isinstance(body, Mapping):
            continue
        if tag == "result_envelope":
            out.append({"t": "submit", "outcome": _cut(body.get("outcome"), 40),
                        "text": _cut(body.get("summary"), SAY_LIMIT)})
        elif tag == "planning_decision":
            out.append({"t": "decision", "decision_type": _cut(body.get("decision_type"), 40),
                        "text": _cut(body.get("rationale"), SAY_LIMIT)})
        elif tag == "method_proposal":
            method = body.get("method") if isinstance(body.get("method"), Mapping) else {}
            out.append({"t": "method", "steps": [_cut(s.get("local_id"), 80) for s in method.get("steps") or ()
                                                  if isinstance(s, Mapping)][:40],
                        "text": _cut(body.get("rationale"), SAY_LIMIT)})
        else:
            out.append({"t": "verdict", "verdict": _cut(body.get("verdict"), 40), "reasons": _reasons(body)})
    rest = _ENVELOPE.sub("", text).strip()
    if review and rest:
        fenced = _FENCE.search(rest)
        verdict = _json(fenced.group(1) if fenced else rest) if (fenced or rest.startswith("{")) else None
        if isinstance(verdict, Mapping) and "verdict" in verdict:
            said = (rest[: fenced.start()] if fenced else "").strip()
            head = [{"t": "say", "text": _cut(said, SAY_LIMIT)}] if said else []
            return out + head + [{"t": "verdict", "verdict": _cut(verdict.get("verdict"), 40),
                                  "reasons": _reasons(verdict)}]
    if rest:
        out.insert(0, {"t": "say", "text": _cut(rest, SAY_LIMIT)})
    return out


def _tool(message: Mapping[str, Any]) -> dict[str, Any]:
    name = _cut(message.get("name"), 60)
    reply = _json(message.get("content"))
    if not isinstance(reply, Mapping):
        return {"t": "tool", "tool": name, "ok": False, "error": "回复无法解析"}
    ok = reply.get("outcome") == "succeeded"
    value = reply.get("value") if isinstance(reply.get("value"), Mapping) else {}
    item: dict[str, Any] = {"t": "tool", "tool": name, "ok": ok}
    if not ok:
        item["error"] = _cut(reply.get("public_message") or reply.get("error_code") or reply.get("outcome"), 200)
    if isinstance(value.get("path"), str):
        item["path"] = _cut(value["path"], 200)
    if isinstance(value.get("bytes"), int):
        item["bytes"] = value["bytes"]
    if isinstance(value.get("total_chars"), int):
        item["chars"] = value["total_chars"]
    if isinstance(value.get("files"), list):
        files = [str(f) for f in value["files"]]
        item["files"] = [_cut(f, 120) for f in files[:12]]
        item["file_count"] = len(files)
    if name == "run_tests":
        item["passed"] = value.get("passed") is True
        item["returncode"] = value.get("returncode") if isinstance(value.get("returncode"), int) else None
        lines = [line for line in str(value.get("stdout") or "").splitlines() if line.strip()]
        if lines:
            item["tail"] = _cut(lines[-1], 200)
    if isinstance(value.get("total"), int):
        item["found"] = value["total"]
    return item


JournalReader = Callable[[Mapping[str, Any]], Iterable[Any]]


def read_turn_journal(orchestrator: Any, intent: Mapping[str, Any]) -> tuple[Any, ...]:
    """The session Journal of the Agent an intent created, from the pool it is bound to.

    An intent bound to a profile this process does not run raises (the caller shows
    SOURCE_UNAVAILABLE); it is never looked up in another pool."""
    from ..runtime.model_router import DEFAULT_PROFILE
    profile = str(intent["config"].get("runtime_profile_id") or DEFAULT_PROFILE)
    return orchestrator.assembled.pool(profile).runtime.uow.read_agent_journal(str(intent["agent_id"]))

__all__ = ("EDGE_KINDS", "ExecutionProjection", "JournalReader", "NODE_KINDS", "read_turn_journal", "turn_items")
