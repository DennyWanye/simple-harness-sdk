# Exact Host terminal compare for the successor installed candidate

2026-09-06. Required before Host candidate acceptance; not covered by old072 PASS.

Public runtime facade: `await runtime.client.read_run_operation_audit(RunId(id))`
returns `RunOperationAuditSnapshotV1.terminal_evidence`. Stable pages carry the same
value at `page.to_json()["metadata"]["terminal_evidence"]`; deserialize with package-root
`RunTerminalAuditEvidenceV1.from_json(...)`. Then call:

```python
assert evidence.matches(
    event_id=actual_host_sdk_event_id,
    payload_hash=actual_host_sdk_terminal_payload_hash,
    state=actual_sdk_terminal_state,
)
```

All three Host values must come from its verified SDK terminal observation, not Host
ExecutionEvidence/observation envelope namespace or caller input. Evidence has closed
state/event_kind, opaque terminal_event reference, exact stored UTF-8 payload SHA256,
separately named entire `event_record_hash`, and actual recorded created_at. It never
exports raw event ID, payload, user text, credential, or arbitrary error. Do not compare
`event_record_hash` or operation `source_hash` as if it were `event_payload_hash`.
The Run ID comes from the bound snapshot/page and must still equal the Host SDK Run.

This field covers ordinary/root run.completed/failed/cancelled. Child terminal receipts
remain their existing separate canonical audit domain. Missing root terminal source
returns None and explicit terminal_event_unavailable gap, not fabricated success;
duplicate or wrong-kind root event is unavailable. Old canonical terminal payloads
are hashed exactly as stored, without adding fields or restamping. Normalizer v11
invalidates older source spools rather than claiming they already have this field.

New root/continuation terminal commit paths derive `sdk_memory_outbox` v1 with actual
committed_turn_hash or null in the canonical terminal payload. The exact payload hash
includes it. Host must not drop this SDK-owned key to preserve an old expected hash.

Decisive installed Host oracle: actual Host runtime completion -> read verified terminal
observation + installed SDK public audit metadata -> same Run/event/state/payload match;
wrong payload hash, same Run/state with another event, and Host envelope hash each fail.
Close/reopen both DBs and page cursor, compare the same evidence, assert no new Provider,
tool or Memory dispatch. A real committed-turn positive should include a non-null hash;
ordinary/no-outbox and failed/cancelled paths may carry null without claiming a Memory
write. Preserve original Host compare; missing/mismatched proof prevents audit acceptance.

SDK source oracle: test_run_audit_terminal_evidence.py uses real SQLite runtime and
actual terminal payload as independent expected bytes; wrong hash/event/state and duplicate
terminal negatives, no raw ID metadata, page equality and DB reopen. Deterministic Context/
driver are test adapters, not paid Provider/native evidence. First setup attempt lacked a
conversation output (retained log); actual red public-terminal-red-actual.log is the
missing public field. public-terminal-adjacent.log29 PASS2.01s; final typed-root1 PASS0.20s
is overlapping. Installed Host composition remains main-owned and pending.

The page evidence also carries actual event_sequence. Each continuation validates
that exact Run/sequence PK row and event_record_hash, even if a later Memory claim
is now the max run-event cut. A changed/deleted terminal cannot hide behind a valid
later cut. public-terminal-cut-red.log demonstrates that stale cached evidence before
this check; public-terminal-cut-green.log36 PASS2.32s includes actual later public
MemoryOutbox claim, unchanged max cut and altered older terminal negative. No Host
or Provider was used to substitute this SDK source oracle.

## Closed terminal proof on continuation — 2026-09-06

fd86 public terminal projection was independently BLOCKED on stale saved evidence.
Original independent payload/duplicate probes retained under sibling
simple_harness-primary-api/.local-test-evidence/2026-09-06/terminal-fd86bc1-review/.
c313 fixed payload binding but duplicate-terminal probe still failed; do not mark
those candidates accepted. The final continuation path additionally checks actual
Run state (PK) and closed root terminal uniqueness (LIMIT2) under the same source TX,
and compares every typed field with the saved evidence. A page captured without
terminal evidence stays that original prefix; it is not upgraded with a later terminal.

Explicit unreleased audit2 DDL includes partial sdk_audit_terminal_events_idx on
run_events(run_id,kind,durable_seq) WHERE kind is one of the three closed root terminal
kinds. This is a nonunique read index, not a new ledger or business insertion restriction.
DDL/checksum validation and exact audit1→2 apply; earlier WIP2 checksum rejects.
EXPLAIN proves SEARCH by run_id/kind, bounded LIMIT2, not a per-page whole-Run scan.

public-terminal-closed-page-green.log25 PASS2.01s (direct schema/pages/terminal),
public-terminal-state-index.log1 PASS0.24s overlaps and adds actual Run-state negative
and indexed plan. old072-terminal-index.log runs exact installed072 real public runtime
on current schema2, commits an actual root terminal and returns an indexed current
projection while retaining five legacy gaps. No Provider network call. Frozen wheel
unchanged; all source failures retained. Independent fixed review still gates build.


## Non-null committed-turn Host oracle (2026-09-06)

The existing Host `tests.operation_audit.test_terminal_audit.setup` calls the real
foreground `build` without `memory`, so its RuntimePorts.agent_memory is None.
Do not fabricate a terminal payload to cover the non-null case. The existing
`tests.execution.test_primary_foreground_runtime.test_primary_real_runtime_with_agent_memory_and_validated_identity`
is the production-adapter fixture precedent: create public
`MemoryManager.build_development(tmp_path / "agent-memory.db")`, pass it to
`build(..., memory=memory)`, and close it after runtime/stack. Development embeddings
and deterministic Provider are fixture evidence, not paid/native production evidence.
The helper already wires ForegroundConversationEntrypoint with validated identity,
ProductConversationContextProvider and ContextStagingRepository. Real main wires
its actual memory backend through ProductionRuntimeConfig.memory; this is not a
missing production MemoryPort claim.

For the first ordinary root completion (no continuation/legacy input replacement),
observe only public SDK APIs against the same SDK execution database/UoW:

```python
from simple_harness import RunId
from simple_harness.runtime.start_snapshot import StartSnapshot
from simple_harness.execution.memory_outbox import MemoryOutboxRepository

start = StartSnapshot.from_json(uow.read_start_snapshot(sdk_run_id))
assert start.conversation is not None
assert start.conversation.memory_text is not None
record = MemoryOutboxRepository(database).read(
    f"agent-memory-turn/v1/{start.turn_id}"
)
assert record is not None
assert record.run_id == sdk_run_id
assert record.turn_id == start.turn_id
actual_turn = record.committed_turn()  # validates canonical payload and hash
assert record.payload_hash == actual_turn.payload_hash
snapshot = uow.read_run_operation_audit(RunId(sdk_run_id))
created = [op for op in snapshot.operations
           if op.kind == "memory_port"
           and op.operation_name == "memory.outbox.created"
           and op.request_hash == actual_turn.payload_hash]
assert len(created) == 1
assert snapshot.terminal_evidence is not None
```

The intent prefix is the public CommittedTurnSpec identity convention; its turn ID
comes from the actual durable start, not a caller-made turn. Do not generalize that
root ID derivation to continuations or replaced legacy input: use their actual
accepted turn identity. Run this before legitimate outbox cleanup. No read above
claims work or invokes Memory/Provider. A pending outbox is already a committed-turn
positive; it does not mean the Memory mutation was physically applied.

The SDK terminal transaction atomically inserts this exact committed turn and
writes its payload_hash into sdk_memory_outbox.committed_turn_hash. Non-null requires
agent_memory + accepted conversation + completed typed assistant output with
non-null memory_text + non-null USER memory_text + actual consumed Context staging.
ReActDriver supplies typed output for a text final; a generic test Driver may not.
Failed/cancelled or opted-out text may legitimately have no committed turn.

The public terminal DTO intentionally exposes the exact whole terminal payload hash,
not the nested sdk_memory_outbox object. The public head plus created receipt above
is an independent committed-turn observation under the SDK atomic producer contract;
it is not a public raw terminal JSON getter. Host must still compare every page with
its verified raw SDK event ID/state/full payload hash using matches(). Never substitute
CommittedTurn.payload_hash or a Host envelope hash. Preserve the non-null positive,
wrong raw terminal hash/event/state negatives, DB reopen and zero extra Provider/tool
calls. This note is source-verified wiring guidance; the new installed Host combination
remains owned by main and is not reported passed here.

For a larger Run, enumerate stable public pages to find the created receipt; absence
in a truncated bounded snapshot is not an absence proof. The example targets one
short ordinary first-turn fixture.
