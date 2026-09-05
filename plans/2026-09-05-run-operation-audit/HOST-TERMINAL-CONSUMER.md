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
are hashed exactly as stored, without adding fields or restamping. Normalizer v10
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
