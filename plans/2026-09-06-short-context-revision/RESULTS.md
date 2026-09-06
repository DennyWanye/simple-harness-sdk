# H075 source evidence — 2026-09-06

Product0735fb5; later commits add independently challenged test controls. Dirac
b13f2c2 scoped read accepted proceeding to focused tests; final review pending.

- source-r1: wire8PASS; subsequent migration control FAIL because test-only forensic
  sqlite connection was not closed before journal-mode reopen. Preserved. No product
  locking relaxation. PID46001 exit1, no children, peak138800KiB.
- source-r2:7PASS3.57s, PID46052 exit0, peak224816KiB, no children. Real H073/H074 public
  producers, WAL-only nonempty state, explicit7->9/8->9/7->8->9, all old table rows
  (excluding descriptor and new9 receipt table) preserved; old real long2item grant
  parsed/preserved; exact H074 long wire/hash unchanged. Known fake short+1 and
  malformed checkpoint/attempt carrier fail before backup. Retained backup crash
  retry and repeated postcommit call return identical receipt. Fresh9 no-op.
- Host f1cdeb9f short-h075-r1:2PASS9.58s, PGID46115 exit0, peak211568KiB, no children.
  Real11groups -> actual shortNone -> public page/fragment -> actual Memory grant ->
  physical pre-invoke guard -> local HTTP MockTransport second send; reopen no
  duplicate send. Separate grant-first independent Host source forget still denies
  second send. This uses declared Harness source overlay, frozen H074/M614/S0313
  tinyenv unchanged. No model/native/main-data migration or full program acceptance.

Known boundary: DTO enforces discriminant/revision pair, not an independent lookup
of source_ref ownership. Host actual selected mapping and full occurrence/source
checks remain required. Whole-catalog migration/root scan has no P99 or hard total
RSS/time promise; external wrapper limits tests to2GiB/180s. Disk admission/stop
thresholds1GiB/256MiB and shared OS lock remain unchanged.

Original Host H074 short-r1, long/clock/negative evidence preserved atcb544203.
No fake source revision, schema/receipt rewrite, candidate overwrite or main edits.

Raw roots: SDK `.local-test-evidence/2026-09-06/short-context/`; Host
`../simple_harness-typed-recall-context-use-full/.local-test-evidence/2026-09-06/typed-use-primary/`.
Version/snapshot/build/installed checks remain NOT_RUN until reviewed source freezes.
