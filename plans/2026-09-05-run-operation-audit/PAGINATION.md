# Stable audit pagination successor — executable contract

2026-09-05; metadata base6a8b0e4, independent metadata P1 scoped ACCEPT; pagination review pending. Source only, no consumer
installation, frozen wheel/package/schema changes, paid Provider or native execution.

Mutable head rows have no complete as-of history. A durable_seq cutoff alone cannot
freeze Provider/effect/decision/control heads; live rereads cannot certify stable pages.

Public seams: synchronous UoW/port and asynchronous runtime.client:
- open_run_operation_audit(run_id, *, page_size=256) -> RunOperationAuditPageV1
- read_run_operation_audit_page(run_id, *, cursor) -> RunOperationAuditPageV1

Opening walks every declared source once in one SQLite read transaction. A temporary
SDK-owned derived SQLite spool holds only the safe DTOs, sorts deterministically, and
materializes count-bounded pages plus a hash manifest. Atomic publication under the
execution DB's adjacent .audit-snapshots directory makes a content-addressed snapshot
available. This is rebuildable audit projection, never execution state or authority.
It does not copy the source DB or add events to the audited Run. Unpublished failure
removes the temporary spool; no incomplete snapshot/cursor is returned.

Every page binds immutable snapshot hash, exact query Run, original Run version/state,
page index/size, total operations/pages, and page hash. Cursor binds snapshot and next
index; it does not carry authorization. Same entity refs remain unchanged across
pages. Manifest commits all page hashes and source/coverage metadata. Snapshot source
completeness means all declared current sources were captured, not full history audit.
Page assembly uses bounded lists; the safe SQLite spool sorts full Runs on disk.

Later pages read the published safe projection, never today's source heads. Appends,
settlements, cancellations, and DB close/reopen cannot change the original prefix or
continuation. Reopening audit creates a new snapshot if covered facts changed. Unknown
snapshot, corruption, bad cursor or cross-Run use is typed unavailable; no silent live
fallback. Retained snapshot files support restart; removal makes old cursors unavailable.
Automatic retention/expiry is not introduced in this slice.

Decisive acceptance: >256 operations exhausted in fixed-size pages, each exactly once
and stable ordered; actual kernel unknown snapshot then settlement/new facts between
pages preserves old prefix and whole snapshot after DB reopen; new snapshot sees new
state; source hash/join and metadata safety unchanged; malformed/cross-Run/missing/
corrupted page/manifest rejected; failure before publish yields no valid cursor.
Diagnostics are never sources. Full producer enumeration/history gaps remain explicit.


## Implemented fixed seam details

Page DTO keeps operations as immutable RunOperationAuditV1 entries and freezes header
metadata. Page.to_json exposes snapshot_source_complete, not fully_audited; the header
retains partial history and source gaps. Old bounded API remains compatible.

Manifest pins format1, normalizer registered-labels-opaque-refs-v1, source schema,
source-set/header, Run version, dataset namespace, total counts, ordered page hashes,
and per-page random opaque tokens. snapshot_hash hashes that entire manifest; each
page_hash hashes its safe DTO array. The next cursor is snapshotHash.randomPageToken;
there is no client-selected index. Unknown or modified tokens reject. Tokens are
routing refs, not authorization. Different opens can get distinct snapshot hashes
for identical facts because publication tokens differ; a published cursor never drifts.

Dataset namespace hashes resolved execution DB path + device/inode/birth time. It is
stable across normal close/reopen and WAL writes; copying/replacing the DB or moving
it creates another namespace. Old snapshot artifacts are not portable credentials.
Paths use this SDK-computed hash and strict hex snapshot IDs only, never raw Run/cursor
path components. Page reader opens SQLite mode=ro and validates manifest before page.
Normalizer/schema mismatch rejects old cached projections explicitly.

Default per-open caps: 64 MiB of safe operation JSON, 192 MiB temporary/published
SQLite file, 30 seconds monotonic build time. Disk/SQLite errors are typed unavailable;
capacity rejection never publishes a partial snapshot. Source iteration uses cursors,
not full-Run fetchall; disk sort and fetchmany(page_size) produce pages. No await occurs
in the source transaction; it ends before sort/publication. Each source row can still
require its canonical decoder's memory, and manifest size scales with page count within
the byte cap. These are explicit resource bounds, not unlimited throughput claims.

Publication commits and closes the derived SQLite file, fsyncs it, atomic-renames it,
then fsyncs the containing directory. Fail-before-publication removes temp files.
The API only returns a cursor after successful publication and first-page validation.
Already published snapshots survive later source writes; no automatic eviction. Manual
removal means old cursors become unavailable, never silently point at a rebuilt snapshot.

Six new focused cases: real kernel 60 distinct tool effects (>256 audit records), exact
ordered equality with canonical bounded oracle while it fits4096, per37-page exhaustion
and DB reopen; real REQUIRE_USER waiting snapshot stays unchanged after exact ALLOW/
physical effect/terminal, new snapshot sees completed; altered/crossRun/missing cursors,
page/manifest corruption, old normalizer, capacity/timeout and atomic publish failure;
same Run ID in a second independent dataset cannot resume the cursor. Original ledger
hash unchanged by build failures. 80 adjacent/focused tests pass; review still required.
