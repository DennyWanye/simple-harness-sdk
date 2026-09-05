# C5 command domain and explicit audit schema 1

2026-09-05; original C5/C8 authority, independently challenged with Dirac. This is
an SDK-owned observational schema in the existing execution database and authority
transaction. It is **not execution schema8**, not an implicit unversioned table and
not a write fence enforced by old0.7.2.

Write open first verifies exact execution7, then initializes/validates the separate
audit descriptor and complete DDL atomically. Read-only audit opens only validate.
Unknown/future/checksum-conflicting or partially initialized audit schema is a typed
unavailable/incompatible outcome, never repaired by CREATE IF NOT EXISTS. Existing
commands receive no fabricated acceptance/history: an absent initial version-one accepted
event is a legacy introduction gap. Old072 may continue to update execution7 while
ignoring the new tables; missed command versions must remain detectable after new
code writes later versions. No execution descriptor/wire or frozen artifact changes.

One append-only command event table binds the real existing command_id,
intent_hash/namespace/projection key/accept_seq identity hash, original source row
hash, command version, claim epoch/attempt count, actual recorded timestamp, closed
operation/outcome and safe error hash/code. It has a command FK, **no nonexistent
Run FK**. Each successful command CAS and its event commit together; failed CAS or
event failure cannot leave business success. Record admission, claim, transition,
retry, rejection, lease renewal, UoW applied, cancel-before-create self applied and
the original transaction's batch cancellation of prior commands. No raw command
payload, owner, nonce or credentials are duplicated into audit rows.

Coverage requires actual accepted version1 plus every command version through the
captured head and its exact source hash. Counts alone, claim_epoch alone or a new
writer's latest marker are insufficient. Legacy gaps are retained permanently.

Public command-domain open/page methods use stable immutable snapshots and the
existing bounded safe spool mechanism. Command identity is an opaque ref and its
target Run intent is distinct from actual Run materialization. A command-only
query does not create or fabricate a Run. Snapshot incarnation is the canonical
immutable command+namespace binding; source cut includes exact command event hash.
Run snapshots also include associated original command events and pin their cut.
No cursor fallback, old-row restamping or execution during reads.

Required oracles: real pre-Run retry/reject/reopen and full page traversal; cancellation
before Run creation with all affected commands; applied/ack-loss replay; event failure
rolls back original head; fresh/old execution7/reopen and future/partial audit-schema
rejection; old072 write between new events leaves a version gap even after new current
head; immutable snapshot prefix across later command/Run creation and DB restart.
No paid Provider/native or full suite; no successor wheel until complete source review.
