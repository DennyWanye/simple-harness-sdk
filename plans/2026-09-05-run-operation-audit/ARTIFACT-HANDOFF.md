# Harness 0.7.3 exact local wheel handoff

2026-09-06. Runtime source leaves independently scoped accepted through fb39d91.
Declared producer/reader inventory is implemented; retained limits are in COVERAGE.md
and the public coverage metadata. No all_operations_recorded/fully audited claim.
This handoff does not close Host/Memory/Service producers, consumer findings or native.

## Fixed inputs / reproducible artifact

- Branch: feat/run-operation-audit; build source 0282fa982995b24bc893fdf6bed69d2caacd6587.
- Runtime source reviewed through fb39d91100e0f1972bbf7de8a4701e4b8c19a21d; later build
  commit only version/changelog/standalone consumer. Local heads/tags/remotes showed
  highest source072, next unused073. No network publication/push/tag/main modification.
- Two independent git-archive extracts, each exactly one
  `SOURCE_DATE_EPOCH=0 uv build --offline --out-dir <buildN>`; both exit0.
- Wheel: simple_harness_sdk-0.7.3-py3-none-any.whl
  SHA256 **1a9ed5c95e6cddd4e0ccd85124320a6001008740a53213712fc89f3467cb4cd7**.
- Sdist: simple_harness_sdk-0.7.3.tar.gz
  SHA256 **981a3ae5894fa335c703af5843e3ec5de46f64191b7aa53d102de3daf3ac4636**.
- Both builds have identical wheel/sdist hashes. All164 wheel package files equal
  fixed source bytes. Sdist is a build/source comparison artifact, not a release claim.

Base path: `.local-test-evidence/2026-09-06/run-operation-audit-073/`.
Install wheel at `build1/simple_harness_sdk-0.7.3-py3-none-any.whl` (build2 identical).
`candidate-manifest.json` retains source/ref, double hashes, archive hash, tools,
exact consumer source/log, original counterexamples and pending Host gate. Raw logs,
DBs, artifacts and manifest remain ignored; this text is the reviewable Git handoff.

## Independent installed public consumer

Fresh isolated installed-venv; offline exact wheel install; external cwd, no PYTHONPATH,
no SDK test imports/private SQL/paid Provider/native. Command from consumer-area:

`env -u PYTHONPATH PYTHONNOUSERSITE=1 ../installed-venv/bin/python run_operation_audit_consumer_exact.py ./data3 0.7.3`

Exit0,90 public operations, one Run: physical success1/failure1/deny0, Provider2.
All pages and saved cursor after DB reopen agree; audit dispatch delta0; raw canary
absent; typed terminal evidence equal across snapshot/pages. This Run reports recorded
coverage with no gaps, not global legacy/every-domain coverage. Log installed-consumer-fixed.log
SHA25628cfa4de2e15e859def182edc4cb4cd0db6e5f4a794a7aa02c90a98358418638.

Consumer fixed source f90cab416a5c204a20f48390583d51a7747b2ec0, exact script bytes checked.
First installed-consumer.log failed before dispatch because fixture enum omitted its
required string type. Original preserved; runtime/wheel did not change. The external
consumer-only correction is separately versioned; build-source sdist retains the old
fixture. Do not represent that bundled old fixture as a passing acceptance command.
No second candidate version or wheel rebuild occurred for this fixture correction.

## Mandatory Host composition gate / original red evidence

Main owns actual Host runtime/consumer installation and exact terminal comparison.
Use package-root RunTerminalAuditEvidenceV1.from_json(page metadata).matches with the
verified raw SDK event ID/payload hash/state from existing Host identity normalization.
Keep Run/owner/fence checks and check every page. Never compare Host envelope hash or
whole-row source_hash to event_payload_hash. See HOST-TERMINAL-CONSUMER.md for positive,
wrong hash/event/state, reopen and zero-redelivery oracle. New sdk_memory_outbox field
is included in actual canonical terminal payload; do not strip it for old expected hashes.
New RULE_VERSION preserves old jobs/unavailable history; do not rewrite old evidence.

Original committed-turn two P1s share sibling memory-port-ba1d6ec-review/probe.log,
SHA a83f4b68145a0710f104a47785b3c53bd34f8cf84d3e205e9385bd38f080b37f;
unchanged fixed-a6b0a7e/probe-fixed.log SHA
cab497e5a35cfa3cf75d1eba485cf73646ff52e1a0cf6f60c10703ed5566db33.
Full absolute paths in local manifest; stage/terminal independent old reds likewise
preserved. Test sets overlap and are not added;90 operations are not90 tests.

Frozen072 wheel SHA53bded3fea87168e5d2ad9e49fea5f99e1c1edb1d6077b2a52dd62716692f9ed
and main2b8428465cbd41032ba024a0b7199183161f5ecd rechecked unchanged.

Status: SDK installed public consumer PASS; artifact independent review requested;
Host installed exact terminal gate pending main. No promotion/native completion claim.
