# Exact short source revision — H075 successor

2026-09-06; authorized necessary public-contract repair. Base1470cd8 is documentation
on frozen H074 source92292699, wheel6caf9def. H074 remains unchanged. Inventory of
local refs/tags/worktrees found no075; version assignment follows reviewed source.
Host checkpointcb544203 preserves actual short-r1 failure plus long/clock/negative
evidence. No Host mapping of None to1; no Memory DTO/fixture/oracle/pin edits.

## Wire and authority

ContextFragmentV2.source_revision is source identity, not its schema_version2.
RecallSelectedItemV4 already requires short revisionNone and chunk_ref==source_ref.
H075 ContextFragmentV2 permits onlyNone for SHORT_HORIZON; every other discriminant
keeps strict positiveint (bool invalid). Constructor and from_json agree. Key set,
fragment schema_version2 and canonical domain remain unchanged:
H(C({domain: "simple-harness/context-fragment/v2", payload: fragment.to_json()})).
Short serializes JSON null. Existing valid nonshort byte/hash vectors stay unchanged;
V1 is unchanged. Never restamp old short+1, receipt, grant, checkpoint or source.

Changing only discriminant must fail the revision pairing. The DTO cannot prove
that arbitrary source_ref belongs to a recalled item: it contains no source-kind
lookup authority. Host must retain the exact actual public selected item mapping,
page/result binding and final actual occurrence verification. Changing BOTH fields
and re-signing an invented Host carrier is not made trustworthy by DTO validation;
source mapping tests belong to the actual Host authority path, not a fake SDK grant.

## Whole-database compatibility

Fresh execution schema9 descriptor is a mandatory binary boundary. H074 does
connection-local foreign_keys PRAGMA, then rejects the new descriptor before
persistent PRAGMA, audit initialization, checkpoint/grant reads or runtime writes.
H075 likewise refuses old7/8 at builder open; explicit migration is required even
for generic consumers, avoiding mid-read partial compatibility.

New synchronous root/runtime public API:
`migrate_execution_to_v9(path, *, backup_path, timeout=5.0)` returns
`ExecutionShortContextUpgradeReceiptV1 | None`.
Receipt fields: backup_path, backup_sha256, source_root_hash, prior_descriptor_hash,
new_descriptor_hash, from_version(7|8), to_version9, schema_version1. Hash domain
`simple-harness/execution-short-context-upgrade/v1`, same canonical envelope algorithm.
Exact accepted source histories: [7], [8], [7,8], [9], [7,9], [8,9], [7,8,9]. Unknown
mixes/checksums/catalog/integrity refuse on read-only connection. Fresh9 returnsNone;
upgraded9 must have the exact independently retained backup and replayable receipt.

7->9 adds existing8 tables plus9 receipt table, records only real markers[7,9], and
does NOT invent a7->8 receipt. 8->9 preserves all old rows/receipts. Old public
migrate_execution_v7_to_v8 and every descriptor helper retain frozen8 semantics.
Host successor composition should call migrate_execution_to_v9 directly for
existing file before manager/runtime open, fixed same-directory `.pre-schema-9.backup`;
missing paths remain builder-owned. No private SDK SQL in Host. Original data stays
unchanged until separately coordinated candidate migration; no native in this leaf.

Before backup or DDL, validate every provider_context_use_attempts.intent_json and
all versions (including terminal history) of react.termination.v1 checkpoint's
context_use_attempt. Parse actual DTO/hash; absent/None optional carrier remains
valid generic/pre-reservation state. Known malformed carrier or old short+1 refuses
the WHOLE upgrade. Do not search arbitrary user JSON/text. Repeat admission under
BEGIN IMMEDIATE. WAL-aware backup root must equal the locked source; finite catalog
is NOT bounded RSS or P99. timeout5s means SQLite lock wait, not total hard deadline.
Atomic descriptor/receipt transaction; precommit failure retains backup for exact
retry, aftercommit lost ACK replays same receipt. Mismatched/incomplete backup is
never overwritten. Parent directory fsync; original migration histories preserved.

## Decisive checks (source written, NOT_RUN)

Wire null constructor/from_json/hash/intent roundtrip; short1/0/bool/text refuse;
longNone/discriminant swap refuse. Actual H074 binary accepts retained old sample
but rejects all schema9 databases before business reads. Real old public consumers
produce nonempty completed Run/provider/checkpoint WAL stores for7->9,8->9,7->8->9;
row/backup/receipt exact, reopen/no-op/retry, known bad carrier no backup/source change.
Then actual Host short11groups -> selectedNone -> page/fragment -> real Memory grant
-> real physical guard, with independent full Host source refusal and normal allow.
No long/clock reruns merely to accumulate totals. Sourcefixed -> Dirac -> resource
bounded targeted tests -> once H075 version/snapshots + double offline wheel and
small target-installed consumer; no new full venv/model/native.
