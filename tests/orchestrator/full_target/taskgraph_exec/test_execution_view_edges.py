# SPDX-License-Identifier: Apache-2.0
"""NEXT-TG-1.0 §8.1: a rework is a new Attempt with a causal edge, never a merged node.

Recorded rows of one repaired step (the shape the desktop run
``mission-a22c9fc39b8abed8`` left behind): Attempt 1 fails its rule check, a repair
request is recorded, the planner's REPAIR decision authorizes a retry, Attempt 2
passes and its accepted candidate becomes a publish operation.  The projection must
keep both Attempts and both checks, and join them only by recorded identities.
The rows are written into a real Store schema; the reader under test is unchanged.
"""
from __future__ import annotations

import json

from agent_orchestrator.orchestrator.taskgraph_execution_view import ExecutionProjection, turn_items
from agent_orchestrator.contracts import Budget, Mission, MissionStatus
from agent_orchestrator.storage.store import Store

M, T, OCC = "m-rework", "task-b", "occ-b"
A1, A2 = f"{T}:attempt-1", f"{T}:attempt-2"


def _world(tmp_path):
    store = Store.open(tmp_path / "orch.db")
    store.insert_mission(Mission(
        id=M, goal="g", success_criteria=("ok",), stop_conditions=(), allowed_tools=(),
        risk_level="sandbox", budget=Budget(max_tokens=1000, max_attempts=2), tenant_id="t",
        status=MissionStatus.CREATED, created_at=1.0, version=1, idempotency_key=M,
    ), spec_hash="f" * 64)
    db = store.connection
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (T, M, 1, "COMPLETED", 1, "{}", 1.0))
    seq = iter(range(1, 100))

    def intent(intent_id, kind, subject, config, agent, at):
        db.execute("INSERT INTO dispatch_intents(intent_id,kind,subject_id,mission_id,state,version,creation_key,"
                   "input_id,input_hash,config_json,agent_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (intent_id, kind, subject, M, "SETTLED", 1, intent_id, "in", "h", json.dumps(config), agent, at, at))

    def event(type_, payload, at):
        n = next(seq)
        db.execute("INSERT INTO events(event_id,idempotency_key,type,trace_id,mission_id,actor_type,actor_id,"
                   "payload_json,created_at,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (f"e{n}", f"k{n}", type_, "tr", M, "system", "s", json.dumps(payload), at, 1))

    context = {"fragment_execution": {"context": {"occurrence_id": OCC, "plan_revision": 1}}, "role": "worker"}
    for ordinal, attempt, retry, at in ((1, A1, None, 10.0), (2, A2, A1, 20.0)):
        db.execute("INSERT INTO attempts(attempt_id,task_id,mission_id,ordinal,status,version,agent_id,json,updated_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   (attempt, T, M, ordinal, "RETRY_WAIT" if ordinal == 1 else "COMPLETED", 1, f"agent-{ordinal}",
                    json.dumps({"created_at": at, "retry_of": retry, "role": "worker"}), at))
        config = dict(context, **({"planning_retry_decision_id": "pd-repair"} if retry else {}))
        intent(f"intent-attempt-{attempt}", "attempt", attempt, config, f"agent-{ordinal}", at)
        verdict = "FAIL" if ordinal == 1 else "PASS"
        result = f"result-{ordinal}"
        db.execute("INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (result, attempt, T, M, "turn", "h", "DONE", verdict,
                    json.dumps({"envelope": {"summary": f"try {ordinal}", "outcome": "candidate"}}), at + 1, at + 1))
        db.execute("INSERT INTO verifications VALUES(?,?,?,?,?,?,?)",
                   (f"{result}:rule_check", result, attempt, "rule_check", verdict,
                    json.dumps({"summary": "artifact_not_in_result: NOTES.md"}), at + 1))
        db.execute("INSERT INTO verifications VALUES(?,?,?,?,?,?,?)",
                   (f"{result}:human_review", result, attempt, "human_review", "NOT_REQUIRED", "{}", at + 1))
    for request, ordinal, decision, kind, at in (("req-plan", 1, "pd-refine", "REFINE", 1.0),
                                                 ("req-repair", 2, "pd-repair", "REPAIR", 15.0)):
        intent(request, "plan", f"{M}:planner:{ordinal}", {"planning_package": {}}, f"agent-p{ordinal}", at)
        db.execute("INSERT INTO planning_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (request, M, "planning-decision-v1", 7, "p", 1, 1, "d", "s", "v", "pv", "ph", request, at))
        db.execute("INSERT INTO planning_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (decision, request, 1, "r", None, json.dumps({"rationale": f"{kind} because"}), "c", kind,
                    "COMMITTED", "[]", "{}", at))
    db.execute("INSERT INTO plan_revisions VALUES(?,?,?,?,?,?,?,?,?)", (M, 1, "ACTIVE", None, "a" * 64, "d", "{}", 2.0, 2.0))
    db.execute("INSERT INTO plan_commit_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               ("plan:req-plan", M, "d", 0, 1, "c" * 64, "c" * 64, "{}", "{}",
                json.dumps({"source": {"intent_id": "req-plan", "rationale": "selected"}}), 2.0))
    event("PlanningRepairRequested", {"request_id": "rr-1", "request": {
        "request_id": "rr-1", "trigger_refs": [A1, T],
        "context": {"event_type": "VerificationFailed", "detail": {"failures": [
            {"summary": "action_candidate_rejected (artifact_not_in_result)"}]}}}}, 12.0)
    event("PlanningRepairAddressed", {"decision_id": "pd-repair", "repair_request_ids": ["rr-1"]}, 16.0)
    db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?)",
               ("act:v1", "act", 1, M, "SUCCEEDED", json.dumps({
                   "attempt_id": A2, "connector": "file_publish", "operation": "publish",
                   "params": {"artifact_path": "NOTES.md", "storage_uri": "/private/internal/path"}}), 30.0, 30.0))
    db.commit()
    return store


def test_a_repaired_step_keeps_both_attempts_and_links_them_causally(tmp_path):
    store = _world(tmp_path)
    projection = ExecutionProjection(store.connection, M).build()
    nodes, edges = projection.nodes, {(e["kind"], e["source"], e["target"]) for e in projection.edges}
    a1, a2 = nodes[f"attempt:{A1}"], nodes[f"attempt:{A2}"]
    assert (a1["status"], a2["status"]) == ("RETRY_WAIT", "COMPLETED")  # the failure is not overwritten
    assert nodes["check:result-1"]["verdict"] == "FAIL" and nodes["check:result-2"]["verdict"] == "PASS"
    assert [layer["layer"] for layer in nodes["check:result-1"]["layers"]] == ["rule_check"]  # NOT_REQUIRED hidden
    assert nodes["check:result-1"]["summary"]["text"].startswith("artifact_not_in_result")
    assert {
        ("attempt_of", f"attempt:{A1}", OCC), ("attempt_of", f"attempt:{A2}", OCC),
        ("review_of", "check:result-1", f"attempt:{A1}"), ("review_of", "check:result-2", f"attempt:{A2}"),
        ("rework_of", f"attempt:{A2}", f"attempt:{A1}"),
        ("repair_requested", f"attempt:{A1}", "repair_request:rr-1"),
        ("decision_for", "planning:req-repair", "repair_request:rr-1"),
        ("retry_authorized", "planning:req-repair", f"attempt:{A2}"),
        ("committed_as", "planning:req-plan", "plan_revision:1"),
        ("operation_of", "operation:act:v1", f"attempt:{A2}"),
    } <= edges
    # the display loop is causal edges between instances, never a step pointing at itself
    assert not any(e["source"] == e["target"] for e in projection.edges)
    assert nodes["operation:act:v1"]["target"] == "NOTES.md"
    assert "/private/internal/path" not in json.dumps(nodes, ensure_ascii=False)
    assert nodes["planning:req-repair"]["summary"]["text"] == "REPAIR because"


class _Record:
    def __init__(self, seq, kind, message):
        self.seq, self.kind, self.message_json = seq, kind, message


def test_turn_items_whitelist_drops_instructions_thinking_and_credentials():
    records = [
        _Record(1, "instructions", {"content": "[role:worker] secret system prompt"}),
        _Record(2, "user_input", {"content": "context package"}),
        _Record(3, "assistant", {"content": "<thinking>private chain</thinking>我先写文件 sk-" + "b" * 30,
                                 "metadata": {"reasoning": "hidden"}}),
        _Record(4, "tool_result", {"name": "workspace_write_file", "content": json.dumps(
            {"outcome": "succeeded", "value": {"path": "a.md", "bytes": 12, "content": "file body"}})}),
        _Record(5, "tool_result", {"name": "workspace_write_file", "content": json.dumps(
            {"outcome": "succeeded", "value": {"path": "a.md", "bytes": 12}})}),
        _Record(6, "assistant", {"content": '<result_envelope>{"outcome":"candidate","summary":"done"}</result_envelope>'}),
    ]
    items, hidden, through = turn_items(records, review=False)
    text = json.dumps(items, ensure_ascii=False)
    assert through == 6 and hidden == 0
    for leaked in ("[role:worker]", "context package", "private chain", "hidden", "file body", "b" * 30):
        assert leaked not in text
    assert items[0]["t"] == "say" and "<redacted:api_key_sk>" in items[0]["text"]
    assert items[1] == {"t": "tool", "tool": "workspace_write_file", "ok": True, "path": "a.md", "bytes": 12, "count": 2}
    assert items[2] == {"t": "submit", "outcome": "candidate", "text": "done"}
