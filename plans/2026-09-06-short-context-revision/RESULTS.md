# H075 source evidence — 2026-09-06

Product0735fb5; later commits add independently challenged test controls. Dirac
b13f2c2 accepted focused testing and87e4b64/Host f1cdeb9f received final scoped source ACCEPT.

- source-r1: wire8PASS; subsequent migration control FAIL because test-only forensic
  sqlite connection was not closed before journal-mode reopen. Preserved. No product
  locking relaxation. PID46001 exit1, no children, peak138800KiB.
- source-r2:7PASS3.57s, PID46052 exit0, peak224816KiB, no children. Real H073/H074 public
  producers, WAL-only nonempty state, explicit7->9/8->9/7->8->9, all old table rows
  (excluding descriptor and new9 receipt table) preserved; old real long2item grant
  parsed/preserved; exact H074 long wire/hash unchanged. Known fake short+1 in
  checkpoint/attempt carriers fail before backup. Retained backup crash
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


Necessary protocol/legacy7->8 adjacent batch:8PASS1.17s, PGID46587 exit0,
peak155280KiB, no children. This specifically preserves the old migration API
boundary after fresh schema9, not another full recall run. Existing green batches
will not be rerun. Local ref inventory highest074; successor075 was unused.

Current freeze changes only version and additive root/runtime API snapshot. The
prior074 snapshot is retained exactly. Dirac source ACCEPT does not claim whole
Host typed/no-recall crash boundaries or installed artifacts/native.


## Fixed 0.7.5 artifact and installed short consumer

Source **abbb0fd707f2ceadb271471da2ef906c27748420**. Two offline builds from separate
extracts of the same clean Git archive are byte-identical. Wheel:

`/Users/denny/projects/simple-harness-sdk-short-context-revision/.local-test-evidence/2026-09-06/short-context/artifact-075/build1/simple_harness_sdk-0.7.5-py3-none-any.whl`

SHA256 **7969a2e5028f2c5d0b348973a5b330bca797f10c1bdfa2532ae033d352d2ee66**.
Only the new170 package files were compared to source; old wheels were not rescanned.
Freeze API4PASS; sparse-checkout missing CI file caused version-check failure,
then only that failed check reran1PASS after restoring the tracked sparse path.
Build setup failures (system Python3.9 tarfile filter and wrong interpreter path)
produced no wheel; retained ignored. Actual double build PGID46784 exit0/nochildren.

Host source **ac4dab5c** includes official migrate_execution_to_v9 before handles,
actual source revision transport, coherent clock and original final guards; no
main/user data changed. H075 new --target installed from Host's own vendor path,
no change to venv074614 or M614/S0313. Consumer runs Python -I/no PYTHONPATH with
explicit Host source and installed H075 target (not SDK source overlay).
175 new wheel members except RECORD match target;119 loaded Harness origins belong
to target. **3PASS11.40s**: installed short allow/reopen and independent group-source
forget refusal (same two source oracles), plus correction of the previous Host
factory missing-ledger refusal. PGID46888 exit0,401552KiB peak, no residualchildren.
This does not sum with source passes as independent goals. No long/clock rerun.

Repeat only changed or failed checks, through main default shared resource wrapper:
`uv build --offline --wheel --out-dir <buildN>` with SOURCE_DATE_EPOCH=0;
`uv pip install --offline --no-deps --target <new-target> --python <existing074614python> <Hostvendor075wheel>`;
installed consumer script: artifact-075/installed_short.py, invoked with same Python
`-I -B`, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 and no PYTHONPATH. Candidate/installed
manifests and raw reports remain ignored. Independent artifact review pending;
source review ACCEPT does not substitute that read. No tag/push/release/native.

Public migration source7/8/9 semantics and retained backup are in CONTRACT.md.
Host exact-consumption pin/path check is necessary; no repeated old full-member
comparison is requested. Remaining entire Host typed-use sink/pending/crash coverage,
M615/M616 composition and main promotion are independent of this short delta.

- `source-r1/command.log` SHA256 `186a36c96bcdb751cb7a4f236b4250bafbf80bfe21410e44b096bf20048e826b`
- `source-r2/command.log` SHA256 `d688214c7a6f029c6e2350a7e34cb9f64196905c5a276eb1858e92da9318b3c0`
- `adjacent-r1/command.log` SHA256 `1852d276d3a1f2ba6dcb6e44a5cb5af281b22ce28c327ca8b2266b5c9ba8da07`
- `build-r3/resource.json` SHA256 `9207bfae7716ecbb25ca11dc1c690903668d81d1964c4c4d029760247eae2140`
- `artifact-075/candidate-manifest.json` SHA256 `8fe91bf2c02fa04d11d03a8b3add51dfa92f59ac8980c4ca896ed4dfb66754a2`
- `artifact-075/installed-manifest.json` SHA256 `44b9e466627977c955960e56b2ad86afabfb44f3cf12ceb6f986b6825cd00327`
