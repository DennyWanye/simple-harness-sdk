# Same-Task Critic allowance and terminal admission handling

Last updated: 2026-09-13. Slice IN_PROGRESS; P34 value and final cumulative gates remain open.

The real docs480-s240-v3 FIRST run left 122683 tokens in its final synthesis hold, but Critic1 could not grow its original40960 reservation by6653. The generic account fallback saw zero because this money was already reserved. Its nonretryable admission error was treated as a malformed verdict, causing Critic2 and an unnecessary Worker redo (65399 additional tokens). No malformed completed Critic output was observed in that failure.

The fix moves only the requested deficit from the same Task's original protected hold. Frozen route/price, Task revision, actual Attempt, original allocation receipt and live reservation identity must match. Other live candidates retain their frozen first-Critic minima. Token and cost preflight precede all writes; aggregate reserved budget does not increase. Growth receipts are idempotent, known unused allowance returns once, and UNKNOWN usage cannot be borrowed, refunded or reissued.

An SDK nonretryable provider-admission failure now remains typed through Critic verification. Its result rejection and terminal Task handling commit together. It cannot consume a schema retry or create a replacement Worker. Genuine completed schema errors retain their existing retry behavior; a transport rejection without proven usage retains its UNKNOWN reservation.

## Evidence and rework

All raw outputs are ignored under `.local-test-evidence/2026-09-12/p33-g/`; commands use the parent-only `run-tests.py` runner and the Host source-test-live-v21 Python3.12 runtime with current editable SDK.

| Check | Outcome | pytest / runner seconds |
|---|---|---|
| g-system-critic-hold-red-v1 | 8FAIL/6PASS; includes two initial fixture issues | 8.44 / 8.80 |
| g-system-critic-hold-red-v2 | Corrected ContextPolicy/UNKNOWN fixture; unchanged production7FAIL/7PASS | 8.33 / 8.69 |
| g-system-critic-hold-green-v1 | 13PASS/1FAIL: repeated-read fixture hit real loop guard before budget | 3.04 / 3.29 |
| g-system-critic-hold-green-v2 | 13PASS/1FAIL: fixture expected9 handoffs, actual cumulative-output bound allows8 | 3.07 / 3.40 |
| g-system-critic-hold-green-v3 | 61PASS/2FAIL: parent variable-rename integration defect; corrected before delivery | 6.17 / 6.66 |
| g-system-critic-hold-green-v4 | 63PASS including14 new cases, schema, protected/price/recovery and budget profiles | 6.72 / 7.22 |
| g-critic-admission-integration-v1 | 1074PASS; four optional tokenizer skips and one real-API skip | 60.52 / 61.09 |
| g-critic-tokenizer-coverage-v1 | Pinned tokenizer supplied;11PASS and no skips | 0.39 / 0.86 |
| Static checks | ruff and mypy117 production files PASS | no separate timing claim |

The positive deterministic SDK case keeps S240K and Mission2M, grows Critic40960 to49192 by8232, settles30000 Critic tokens, completes all verification layers with one Worker/one Critic intent and leaves zero Task reserve. Both budget/input-cap negative cases stop without extra ordinal/Worker, and the same Orchestrator can complete an unrelated Mission. These are real in-process SDK requests against a deterministic Provider, not paid-model or native UI evidence.

Independent review: Popper Sol/high found no concrete P0/P1/P2. Pauli Astra/high authored draft production patch and tests; parent corrected fixture and integration errors above. Both cold-library denial gaps PASS (g-critic-admission-cold-v1,1.58s/runner1.88s): new runtime reopens closed SDK/Orchestrator databases after actual FAILED intent or persisted ERROR, preserves all request identities and200000 Critic usage, stops without new handoff/Worker/ordinal, and settles Task200300 with zero reserve. This is library reopen, not process SIGKILL or native acceptance. No savings claim from unmatched tasks.

## Remaining decisive gates

1. Cold-library reopen after durable FAILED Critic / ERROR layer: completed, pending clean committed recheck.
2. Independent production-diff review completed; clean committed validation pending.
3. One fixed real FIRST/COMPARE pair using audit320-docs480-s240-v4; same materials/oracles and per-Mission2M total, A320K/B480K/C400K/S240K. The prior v3 failure remains immutable.
4. Current cumulative and affected source-native UI verification. No packaging/P36/push.

Retro: the real provider check exposed a production budget defect that deterministic happy paths missed. Several new fixture mistakes and one parent overbroad rename added avoidable rework; exact failure evidence, typing and affected regression caught them before any new paid run.

## Child execution measurements

| Child / actual runtime | Wall seconds including waiting | Uncached input | Cached input | Output | Accepted work / rework |
|---|---:|---:|---:|---:|---|
| Schrodinger Terra/medium | 64.61 | 61283 | 291072 | 2398 | Budget-profile review, no findings/rework |
| Pauli Astra/high | 1844.73 | 321981 | 5501312 | 29213 | Two-defect diagnosis, production draft and16 tests; parent corrected fixtures, typing and formatting; cold2 pass |
| Popper Sol/high | 84.43 | 91438 | 413952 | 3677 | Independent production review, no findings/rework |

Parent integration/check window approximately15:46-16:01, includes test time and overlap with child work; no exclusive coordination CPU/token timing available. The parent also introduced and fixed an overbroad rename caught by tests/mypy. Cached input is recorded separately; these unmatched tasks do not establish monetary savings or a model ranking. Agents closed after outputs. Numeric raw source: Host ignored agent-efficiency-0500/usage-1600.json.
