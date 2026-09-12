# ruff: noqa: E402
"""P35 A07: production Worker Context, local Provider fixture, actual OS boundary.

UpperBoundTokenizer is intentional, not a substitute claim for model BPE accuracy.
No embedding/network provider is installed. Retrieval observations wrap the real
SessionRetriever; they neither supply selections nor change returned material.

Rework v20: the first fixture waited outside Orch liveness, so its 1s Attempt
lease expired before request four. Keep the real liveness path running until the
blocked handoff, and fail immediately if the SDK turn ends before that boundary.

Rework cold-v2: invocation fingerprint covers logical request_json; Context binds
the wire copy after durable tool-call restoration. Verify both identities and
their exact effect-ledger provenance instead of asserting unlike hashes equal.

Rework cold-v3: retain the zero-retrieval oracle; report actual cold entry counts
and bounded call sites even on failure. No query or recalled content is logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sqlite3
import sys
import traceback
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2]
for path in (TESTS.parent / "src", TESTS / "orchestrator" / "step07", TESTS / "agents"):
    sys.path.insert(0, str(path))

from graph_helpers7 import node, spec
from test_provider_budget_guard import Counter

from agent_orchestrator.contracts import Budget
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig, PriceTable
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.testing.fixtures import RoleScriptedProvider, role_of
from simple_harness.agents.context import ContextPolicy, UpperBoundTokenizer
from simple_harness.agents.context.tokenizer import count_message, count_tools
from simple_harness.agents.memory.retrieval import SessionRetriever
from simple_harness.contracts import RunId, canonical_json, thaw_json
from simple_harness.execution.provider_invocations import (
    provider_request_fingerprint,
    provider_request_json,
)

READS = 12
STAGE_SECONDS = 10
CHILD_SECONDS = 20
TEARDOWN_SECONDS = 3
GOAL = "P35-A07: preserve original constraints and current input through cold recovery."
POLICY = ContextPolicy(
    max_input_tokens=32768,
    output_reserve=1000,
    safety_margin=256,
    max_tool_result_tokens=6000,
    tool_result_preview_chars=1024,
)
SEEDS = {f"context-{n:02d}.txt": f"original-block-{n:02d} " + "x" * 8100 for n in range(READS)}


def write_marker(path, body):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(body, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def rows(path, query, args=()):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, args)]


def snapshot(root, agent_id):
    database = root / "execution.db"
    return {
        "journal": rows(
            database,
            "SELECT * FROM base_agent_session_journal_v1 WHERE agent_id=? ORDER BY seq",
            (agent_id,),
        ),
        "selections": rows(
            database,
            "SELECT * FROM base_agent_context_selections_v1 WHERE agent_id=? "
            "ORDER BY revision,selection_id",
            (agent_id,),
        ),
        "summaries": rows(
            database,
            "SELECT * FROM base_agent_session_summaries_v1 WHERE agent_id=? ORDER BY summary_id",
            (agent_id,),
        ),
        "requests": rows(
            database,
            "SELECT invocation_id,request_id,request_fingerprint,request_json,"
            "handoff_attempt,rehandoff_count FROM provider_invocations "
            "WHERE run_id=? ORDER BY invocation_id",
            (agent_id,),
        ),
        "effects": rows(
            database,
            "SELECT raw_call_id,turn_ordinal,call_ordinal,tool_name,arguments_json "
            "FROM execution_effects WHERE run_id=? AND raw_call_id IS NOT NULL "
            "ORDER BY turn_ordinal,call_ordinal",
            (agent_id,),
        ),
        "admission": rows(
            root / "orchestrator.db",
            "SELECT invocation_id,handoff_ordinal,request_hash,wire_hash "
            "FROM provider_token_grants WHERE agent_id=? ORDER BY invocation_id,handoff_ordinal",
            (agent_id,),
        ),
    }


def assert_request_identities(snapshot_value, invocation_id, selection, wire_request):
    """Allow only protocol restoration backed by exact durable tool-call facts.

    This compares the actual warm Provider payload, not a freshly prepared request
    (which would touch Context). Cold checks use that same captured wire payload.
    """
    (record,) = [r for r in snapshot_value["requests"] if r["invocation_id"] == invocation_id]
    def digest(value):
        return hashlib.sha256(canonical_json(value).encode()).hexdigest()
    logical = json.loads(record["request_json"])
    assert digest(logical) == record["request_fingerprint"]
    assert record["request_id"] == selection["provider_request_id"]
    assert digest(wire_request) == selection["request_hash"]
    (grant,) = [r for r in snapshot_value["admission"] if r["invocation_id"] == invocation_id]
    assert grant["request_hash"] == record["request_fingerprint"]
    assert grant["wire_hash"] == selection["request_hash"]
    assert grant["handoff_ordinal"] == 1
    groups = {}
    for effect in snapshot_value["effects"]:
        key = (effect["turn_ordinal"], effect["raw_call_id"])
        assert key not in groups
        groups[key] = effect
    expected = json.loads(record["request_json"])
    restored = 0
    for index, message in enumerate(expected["messages"]):
        if message["role"] != "assistant":
            continue
        followers = []
        for follower in expected["messages"][index + 1 :]:
            if follower["role"] != "tool":
                break
            followers.append(follower)
        if not followers:
            continue
        ordinal = message["metadata"].pop("provider_turn_ordinal")
        assert type(ordinal) is int
        calls = []
        for follower in followers:
            fact = groups[(ordinal, follower["call_id"])]
            assert fact["tool_name"] == follower["name"]
            calls.append(
                {
                    "id": fact["raw_call_id"],
                    "name": fact["tool_name"],
                    "arguments": json.loads(fact["arguments_json"]),
                }
            )
        message["metadata"]["provider_tool_calls"] = calls
        restored += len(calls)
    assert restored > 0, "rotated request must retain complete tool protocol groups"
    assert expected == wire_request, "wire changed beyond durable tool-call restoration"
    return {
        "request_id": record["request_id"],
        "ledger_hash": record["request_fingerprint"],
        "wire_hash": selection["request_hash"],
    }


def observe_retrieval(*, trace=False):
    counts = {"prewarm": 0, "search": 0}
    prewarm, search = SessionRetriever.prewarm, SessionRetriever.search_sync

    def observed(operation, retriever):
        counts[operation] += 1
        if trace:
            print(
                "context.cold.retrieval "
                + json.dumps(
                    {
                        "operation": operation,
                        "counts": dict(counts),
                        "embedding_configured": retriever.embedding_fingerprint is not None,
                        "call_sites": [
                            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                            for frame in traceback.extract_stack(limit=9)[:-1]
                        ],
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )

    async def wrapped_prewarm(self, *args, **kwargs):
        observed("prewarm", self)
        return await prewarm(self, *args, **kwargs)

    def wrapped_search(self, *args, **kwargs):
        observed("search", self)
        return search(self, *args, **kwargs)

    SessionRetriever.prewarm = wrapped_prewarm
    SessionRetriever.search_sync = wrapped_search
    return counts


class ContextProvider(RoleScriptedProvider):
    def __init__(self, *, cold=False):
        super().__init__(
            {
                "worker": [
                    ("workspace_read_file", {"path": path, "max_chars": 8192}) for path in SEEDS
                ]
            }
        )
        self.cold = cold
        self.entered = asyncio.Event()

    async def invoke(self, request, *, cancel):
        if self.cold:
            self.requests.append(request)
            raise AssertionError("UNKNOWN cold recovery must not invoke Provider")
        assert role_of(request) == "worker"
        if len(self.requests) == READS:
            self.requests.append(request)
            self.by_role["worker"] += 1
            self.entered.set()
            await asyncio.Event().wait()  # no response, usage, exception or graceful exit
            raise AssertionError("unreachable")
        return await super().invoke(request, cancel=cancel)


def orchestrator(root, provider):
    profile = RuntimeProfile(
        "default",
        provider,
        "agent-model",
        context_policy=POLICY,
        tokenizer=UpperBoundTokenizer(),
        price_table=PriceTable("fixture", 1_000_000, 2_000_000),
        default_max_output_tokens=1000,
        max_output_tokens_ceiling=1000,
    )
    return Orchestrator(
        OrchestratorConfig(
            evidence_root=root,
            max_concurrency=1,
            lease_seconds=1,
            sdk_lease_ttl_seconds=0.5,
            attempt_reserve_tokens=40_000,
            max_model_calls_per_turn=READS + 2,
            max_tool_calls_per_turn=READS + 2,
        ),
        profiles={"default": profile},
        provider_token_estimator=Counter(100),
        poll_interval=0.005,
    )


async def warm(root, marker):
    retrieval = observe_retrieval()
    provider = ContextProvider()
    orch = orchestrator(root, provider)
    await orch.__aenter__()  # parent SIGKILL is the only normal termination
    mission = await orch.submit_mission(
        spec(
            goal=GOAL,
            workspace_seed=SEEDS,
            conflict_reserve_tokens=0,
            success_criteria=("file:a.md",),
            budget=Budget(max_tokens=1_000_000, max_cost_micros=10_000_000, max_attempts=12),
        )
    )
    planning = orch.commit.begin_planning(mission.id)
    tasks, _ = orch.commit.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json(
            {
                "tasks": [
                    node(
                        "A",
                        goal=GOAL,
                        budget={
                            "max_tokens": 200_000,
                            "max_cost_micros": 2_000_000,
                            "max_attempts": 3,
                        },
                        verification_policy=["format_check", "rule_check"],
                    )
                ]
            }
        ),
        base_version=planning.version,
        source={"planner": "P35 A07 fixed graph fixture"},
    )
    assert await orch._next_attempt(orch.store.get_mission(mission.id), tasks[0], [])
    (attempt,) = orch.store.list_attempts(tasks[0].id)
    intent = orch.store.get_intent_for_subject(attempt.id)
    assert intent is not None and await orch._dispatch(intent)
    intent = orch.store.get_intent(intent.intent_id)
    bridge = orch.bridge_for(intent)
    while not provider.entered.is_set():
        result = await bridge.result(
            agent_id=intent.agent_id,
            turn_id=intent.expected_turn_id,
        )
        assert result is None, (
            "SDK turn ended before rotated handoff: "
            f"state={result.state}, error={result.error}, provider_calls={provider.calls}"
        )
        # This is the same production heartbeat path _collect uses for a pending
        # turn. Waiting on the Provider Event alone starves the Orch Attempt lease
        # even though the independently owned SDK Run continues heartbeating.
        await orch._observe_liveness(intent)
        await asyncio.sleep(0.01)
    live_attempt = orch.store.get_attempt(attempt.id)
    assert live_attempt.lease_owner == orch._owner
    assert live_attempt.lease_expires_at > orch.store.now
    uow = bridge.runtime.uow
    selection = uow.read_agent_context_selection_by_request(provider.requests[-1].request_id.value)
    assert selection is not None and selection.dropped_ranges, "no actual Context rotation"
    assert not selection.required_over_budget
    assert selection.request_hash == provider_request_fingerprint(provider.requests[-1])
    counter = UpperBoundTokenizer()
    for request in provider.requests:
        assert (
            sum(count_message(counter, message) for message in request.messages)
            + count_tools(counter, request.tools)
            <= POLICY.input_budget() + POLICY.render_slack_tokens
        )
    # A fixed budget must fit the original required package and tools with room for
    # a complete tool group. Never shrink required content to obtain rotation.
    first = provider.requests[0]
    assert (
        sum(count_message(counter, message) for message in first.messages)
        + count_tools(counter, first.tools)
        < POLICY.input_budget() - 6000
    )
    records = uow.list_provider_invocations(RunId(intent.agent_id))
    assert len(records) == READS + 1 and provider.calls == READS + 1
    pending = [r for r in records if str(r.state) == "handed_off"]
    assert len(pending) == 1
    blocked = pending[0]
    assert blocked.request_id.value == selection.provider_request_id
    assert blocked.response_json is None and thaw_json(blocked.usage_json).get("usage") is None
    assert all(str(r.state) == "succeeded" for r in records if r is not blocked)
    assert all(r.handoff_attempt == 1 and r.rehandoff_count == 0 for r in records)
    before = snapshot(root, intent.agent_id)
    wire_request = provider_request_json(provider.requests[-1])
    identity = assert_request_identities(
        before, blocked.invocation_id, asdict(selection), wire_request
    )
    assert identity["ledger_hash"] == blocked.request_fingerprint
    assert bridge.runtime._assembled.wire.fallback_total == 0
    journal = before["journal"]
    instructions = [r for r in journal if r["kind"] == "instructions"]
    inputs = [r for r in journal if r["kind"] == "user_input"]
    assert len(instructions) == len(inputs) == 1
    assert GOAL in inputs[0]["message_json"]
    assert json.loads(instructions[0]["message_json"])["role"] == "system"
    assert json.loads(inputs[0]["message_json"])["content"] == intent.config["message"]["content"]
    assert retrieval["prewarm"] > 0 and retrieval["search"] > 0
    for row in instructions + inputs:
        original = json.loads(row["message_json"])["content"]
        assert any(message.content == original for message in provider.requests[-1].messages)
        assert row["seq"] in selection.selected_seqs
    tools = [
        json.loads(r["message_json"])["content"] for r in journal if r["kind"] == "tool_result"
    ]
    assert len(tools) == READS
    for payload in SEEDS.values():
        assert any(payload in content for content in tools), "journal lost original tool bytes"
    dropped = {seq for lo, hi in selection.dropped_ranges for seq in range(lo, hi + 1)}
    assert dropped.intersection(r["seq"] for r in journal if r["kind"] == "tool_result")
    assert before["summaries"]
    assert orch.store.find_result_for_attempt(attempt.id) is None
    write_marker(
        marker,
        {
            "pid": os.getpid(),
            "mission_id": mission.id,
            "task_id": tasks[0].id,
            "attempt_id": attempt.id,
            "intent_id": intent.intent_id,
            "agent_id": intent.agent_id,
            "turn_id": intent.expected_turn_id,
            "invocation_id": blocked.invocation_id,
            "selection": asdict(selection),
            "owner": orch._owner,
            "lease_expires_at": orch.store.get_attempt(attempt.id).lease_expires_at,
            "provider_calls": provider.calls,
            "retrieval": retrieval,
            "snapshot": before,
            "policy": POLICY.to_json(),
            "request_identity": identity,
            "wire_request": wire_request,
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    )


async def cold(root, original, result_path):
    marker = json.loads(original.read_text())
    retrieval = observe_retrieval(trace=True)
    provider = ContextProvider(cold=True)
    orch = orchestrator(root, provider)
    try:

        async def recover():
            await orch.__aenter__()
            assert orch._owner != marker["owner"]
            intent = orch.store.get_intent(marker["intent_id"])
            assert (
                intent.mission_id,
                intent.subject_id,
                intent.agent_id,
                intent.expected_turn_id,
            ) == (
                marker["mission_id"],
                marker["attempt_id"],
                marker["agent_id"],
                marker["turn_id"],
            )
            for recovery_pass in range(2):
                await orch.recover()
                for _ in range(3):
                    await orch._cycle()
                    await asyncio.sleep(0.01)
                assert provider.calls == 0 and retrieval == {"prewarm": 0, "search": 0}, (
                    f"cold recovery pass={recovery_pass + 1}: "
                    f"provider_calls={provider.calls}, retrieval={retrieval}"
                )
                assert snapshot(root, marker["agent_id"]) == marker["snapshot"]
                assert (
                    assert_request_identities(
                        snapshot(root, marker["agent_id"]),
                        marker["invocation_id"],
                        marker["selection"],
                        marker["wire_request"],
                    )
                    == marker["request_identity"]
                )
                assert len(orch.store.list_attempts(marker["task_id"])) == 1
                assert orch.store.find_result_for_attempt(marker["attempt_id"]) is None
            grant = orch.store.connection.execute(
                "SELECT state,actual_tokens,actual_cost_micros FROM provider_token_grants "
                "WHERE invocation_id=?",
                (marker["invocation_id"],),
            ).fetchone()
            assert tuple(grant) == ("UNKNOWN", None, None)
            with orch.store.transaction():
                reservation = orch.commit.ledger.reservation(marker["attempt_id"])
            assert reservation["state"] == "RESERVED" and reservation["settled_tokens"] is None

        await asyncio.wait_for(recover(), STAGE_SECONDS)
    finally:
        try:
            await asyncio.wait_for(orch.__aexit__(None, None, None), TEARDOWN_SECONDS)
        finally:
            print(
                "context.cold.final_counts "
                + json.dumps(
                    {
                        "provider_calls": provider.calls,
                        "retrieval": retrieval,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
    assert provider.calls == 0 and retrieval == {"prewarm": 0, "search": 0}, (
        f"cold after close: provider_calls={provider.calls}, retrieval={retrieval}"
    )
    assert snapshot(root, marker["agent_id"]) == marker["snapshot"]
    write_marker(
        result_path, {"pid": os.getpid(), "provider_calls": provider.calls, "retrieval": retrieval}
    )


async def main():
    mode, root, marker, output = sys.argv[1:]
    if mode == "warm":
        await asyncio.wait_for(warm(Path(root), Path(marker)), STAGE_SECONDS)
        await asyncio.Event().wait()
    else:
        assert mode == "cold"
        await cold(Path(root), Path(marker), Path(output))


if __name__ == "__main__":
    # Independent OS wall timeout also covers synchronous SQLite/Context work.
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.alarm(CHILD_SECONDS)
    asyncio.run(main())
