# P34 v15 strict real pair — source gate accepted

Last updated: 2026-09-14 CST. P3.4-A04 SOURCE PASS. Current approved source scope46/46; original48 criteria contain46SOURCE PASS and2user-deferred packaging/installer criteria. This is not installer/release acceptance or completion of a larger orchestration-design backlog.

## Fixed source and contract

SDK ae8d37b3c74576bf558adfe3718805ccec888343 / Host0e1a36db (Host production18ff5d24), immutable source-snapshot-v41. The post-run audit rehashed all336SDK attestation inputs with zero mismatches. The committed source check and paid runner both recorded a clean working tree. Source-native UI retains its separately tested v39/earlier source boundaries.

One predeclared pair, both arms run once: FIRST_VERIFIED followed by COMPARE_THEN_SYNTHESIZE. Model deepseek-flash, official Beta endpoint, tool_schema_mode=deepseek-strict-v1; independent profile deepseek-strict-context-256k-v1. Configuration context256-8m-strict32k-v9 uses32768 initial and maximum output tokens,262144 context,8M/24 Mission budget, and unchanged Task goals/materials/deadlines/budgets/criteria. The original v7 byte-level contract is preserved by regression; v9 is separately versioned, not a claim of the same historical contract hash. No reroll, oracle weakening, argument repair or removal of failed physical calls.

v15 same-arm contract SHA256 c808f13c3e866360dd0d799d9f13be9612a274b29cd4978d8d437bc95983af61. Tokenizer SHA25681f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99. Strict estimator fingerprint deepseek-v41:0103315ed3b4213c6ce14f04875bd1c24d057927f9f27bb83a7fbd6e88a8abc3; runtime context405f4af3b1514ef6e7cfb69cff9e2587296f8bb372cac686ad71cb63b3e299fe.

## Results

| Arm | Strict gate / Mission | Calls / admission grants | Total tokens | Cached input subset | Wall seconds | Provider errors / unknown usage / final reserve / rehandoffs |
|---|---|---:|---:|---:|---:|---|
| FIRST | PASS / COMPLETED, verification_passed |76|645304|466816|423.613|0 / 0 / 0 / 0|
| COMPARE | PASS / COMPLETED, verification_passed |112|1273253|935424|605.314|0 / 0 / 0 / 0|

Pair188calls1918557tokens, runner1029.73seconds, pytest1PASS1029.20seconds. Both physical invocation ledgers contain only succeeded records. FIRST retained one react_max_turns_exceeded AgentTurn and recovered; it is not a hidden/deleted provider error. Expected failing immutable baseline audits remain FAIL in both arms. Both independent databases use deterministic Mission mission-5e0e032026d1bacc. Process groups61220(pair) and61139(committed regression) have zero residual members.

The unchanged oracle verified actual C code/analysis tests, Critic, distinct final S tests and its real reads of C artifacts/verified knowledge; immutable material/tests/config were unchanged in every materialized Attempt workspace. COMPARE has two distinct recorder.py candidates on the same C Task, a durable synthesize decision, a new synthesizer Attempt rather than best-only fallback, actual reads of both candidates, real Manager dependency repair, and valid analysis fragment reuse from failed baseline audits. No extra audit failures occurred.

| Evidence link | Accepted identity |
|---|---|
| FIRST verified C | result-55ec588180a1905e |
| FIRST failed baseline | result-05abf6e0ee22a217 |
| COMPARE verified C | result-7306285905216c07 |
| COMPARE synthesis decision | selection-command:8a655fe9733222e12e59e807225c7561adf8601919657b93845cf3d502bf8573 |
| Reused fragment | result-ae4298a1135c3fb6 |
| COMPARE failed baselines | result-bf3b4517e1ff0a3e; result-c169aeaf106ddf5c |

## Software and interpretation boundaries

Committed158PASS6.76seconds (runner7.26seconds), g-p34-strict-committed-v4: providers, exact shared counter/HTTP wire, canonical durable roundtrip, legacy fingerprint/payload invariance, runtime profile/cold pool isolation, real admission accounting, existing output recovery and protocol handling. Ruff changed files and mypy four production files passed. Full commands are in the local runner receipt. Earlier canonical-roundtrip RED and incorrect endpoint/test-fixture failures are retained with the correction history in strict-provider-mode.md.

This closes the current source real-comparison acceptance criterion. It proves a successful real mechanism trace for one fixed task/pair, not statistical multi-agent superiority. COMPARE used627949 more tokens in this pair; dollars remain UNPRICED and exact provider tokenization remains NOT_PROVEN. Cache is a subset of input and is not additional usage. v13/v14 strict failures remain failures; v14 independently showed length and non-length arguments_json failures, without retroactively proving v13's exact cause.

Strict is an explicit SDK provider/profile configuration whose tool schemas are active under that mode. Existing Host default/legacy endpoints and frozen continuations are not automatically redirected. No new strict native UI run was performed; v15 declares native_ui=NOT_RUN. Prior native acceptance remains version-scoped. Packaging P3.1-A08 and P3.6-A07 remains DEFERRED; no wheel rebuild, installer, release, push or production key migration.

## Local evidence index

Raw root relative to Host: .local-test-evidence/2026-09-13/p33-g/source-snapshot-v41/sdk/.local-test-evidence/2026-09-14/p34-real-search-value-dc8ffb949aeb4540b743bb6c0e37e5f9/. Raw evidence remains Git-ignored, no credentials stored, no NAS deletion. SDK runner receipts are relative to sibling SDK .local-test-evidence/2026-09-12/p33-g/. This document contains conclusions and hashes only.

| Local file | SHA256 |
|---|---|
| comparison.json | 820a1acdfd2c83bd0b7ded3dc354237f1d1342f4ceb7462f91355b4a8324e956 |
| FIRST_VERIFIED/verdict.json | d278ac81f68a24eab61d70b19f428490bda0793d5e0d19ba191ae1b5a43ee2c6 |
| COMPARE_THEN_SYNTHESIZE/verdict.json | 74dbdd6a80bc6c76ff60a8d934d0c676abe376cba1999e7002143dcb5ff81330 |
| SDK/.local-test-evidence/2026-09-12/p33-g/g-p34-real-value-pair-v15.json | ecd9d6566bb6dce0c2092371eeebd9a4bc8e19e21229da33a435977fdda1df99 |
| SDK/.local-test-evidence/2026-09-12/p33-g/g-p34-strict-committed-v4.json | cf1936fd9a01f82cf79bd98e2f5d104e9341eb2b740fbcef0a503ea59e552df3 |
| Host/.local-test-evidence/2026-09-13/p33-g/source-snapshot-v41/sdk-attestation.json | 4a354b56332fe1cd1596b7649042377f79b57ad5ac3f5e3afe577330ad6c2741 |
| Host/.local-test-evidence/2026-09-13/p33-g/source-snapshot-v41/runtime-identity.json | 922b0a45cdaa91715b6af943bd9e083926d81dbd3d1c674e66bbf9f627be5d40 |
| final-audit.json | eec87174a4df9a36e3f840a85204b6de2a1d3d5060a4654bc14705c395229f20 |
| process-cleanup.json | 5b596929818f62cbe0078352f1531182b1bf4616bfc400a19b7ac1d2bfd366ff |
| SDK/.local-test-evidence/2026-09-12/p33-g/g-p34-real-value-pair-v15.log | dbb76a776215c8337d1429998bf682de0f64a5fff18a541f8b45f321a3bf4a37 |
| SDK/.local-test-evidence/2026-09-12/p33-g/g-p34-strict-committed-v4.log | eb91ce282788d2679f22c1b5145079da34d4f0939937f63c2cc7d6d7163f0f1d |

## Current-request timing

User request arrived 2026-09-14T00:13:47.462000+00:00; documentation checkpoint 2026-09-14T01:21:18.485220+00:00. Wall span 4051.023seconds, including source work, review, API/model waits and checks; this is not isolated active engineering time and excludes subsequent commit/response. Historical whole-Phase3 and per-module active totals were not fully metered, so no fabricated cumulative hours are reported. v14 runner1097.48seconds, final v15 runner1029.73seconds, and committed affected regression7.26seconds are measured subsets, not additional wall time. Raw session-timing.json SHA256 8162c2a31d766a67ed2e874f9f2b79ac4f14763150c769a93de26350c4816c00.
