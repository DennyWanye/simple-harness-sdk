# Long context implementation and verification checkpoint

Updated: 2026-09-14 CST. IN_PROGRESS, no packaging/release/push.

New official DeepSeek source Missions default to 256K input and offer 512K. Output remains 8192 default / 32768 ceiling, independently. New Mission token budgets default to 4M/8M respectively; these are spending caps, not prepaid input or automatic token use. An explicit Mission capacity is persisted in request identity and all role routes. Cold retries keep their original capacity and budget despite changed creation defaults. Invalid selections fail before dispatch.

Per-profile admission guards preserve the old bounded default pool's exact price/estimator identity while sharing the same durable global physical-slot grants. Legacy 32K Missions and actual dispatch configurations survive upgrade. Truly pre-context/pre-admission libraries remain legacy-only: automatic long-capacity upgrade for this older case is still OPEN, because mixing unguarded legacy calls with guarded pools would invalidate the global slot bound. The UI explains this boundary. Wheel/unrelated-endpoint behavior is preserved.

Independent Sol/high review found default-guard fingerprint drift, uncertain retry/default drift, stale unsent choices and a send(false) dead-end. All four were corrected with targeted controls. send(false) restores the preceding retry state: it clears a first unsent request without discarding an earlier UNKNOWN request. Latest bounded review accepted the UI correction; prior per-pool SDK review found no additional concrete issue for current Host configuration.

| Evidence / scope | Actual result | Test / runner seconds |
|---|---|---|
| SDK g-long-context-sdk-integration-v1: selected routing, floors, global/per-pool load, Critic/cold, policy binding, P36 privacy | 26 PASS | 10.01 / 10.38 |
| Host g-long-context-old-intent-v1: actual old32K dispatch migration plus source wiring | 6 PASS | 1.49 / 1.99 |
| Host g-long-context-host-allroles-v1: 256/512 all-role completion and cold zero-call reopen, creation identity | 4 PASS | 2.97 / 3.46 |
| Host g-long-context-host-adjacent-v1: lifecycle, Mission/document create, restart, policy and isolation | 65 PASS | 105.00 /105.48 |
| Host long-context-ui-unit-v3 | 78 PASS; tsc also PASS | 0.803 / 1.31 |
| Real deepseek-flash256K synthetic SDK retrieval | PASS, input258149/output588/cache257920; 1 call, total258737 | 9.296 / 9.60 |
| Real deepseek-flash512K synthetic SDK retrieval | PASS, input520283/output803/cache0; 1 call, total521086 | 22.855 / 23.18 |

Both real probes verify first/middle/last records and their cross-position sum against an independently written oracle. Counted input equals provider-reported input and stays within the selected capacity. These are one-class synthetic SDK transport/retrieval controls, not full Mission, original material, multi-turn or native UI acceptance. Required long-history/over-cap/cold and current native UI gates remain open at this checkpoint.

## Failure and rework accounting

The first256K run handed off once but the new observer incorrectly called ProviderUsage.to_json(), leading to UNKNOWN instead of capturing a returned response; actual usage for that run is unavailable, never zero. Original g-long-context-capacity-real256-v1 remains FAIL (243.266 /243.64 seconds). A bounded short diagnostic received HTTP200 (8289input+150output=8439tokens) but hit the same observer defect; the parent stopped the now-idle wait, retaining FAIL (98.142/98.50 seconds). This was parent test-code rework, not evidence of a model capacity limit. Dataclasses.asdict and a dry response with non-null usage now cover the observer path; corrected dry-v2 PASS4.685/4.97 seconds preceded the successful real tests. Three known-usage calls total788262tokens plus one earlier call with unknown usage; do not hide failures or infer subscription savings.

Other retained setup rework: initial Host source test omitted mandatory actual source attestation (2FAIL/13PASS); fixture now captures real imported SDK source hashes, not a mocked identity. P36 support-report first collection lacked helpers_step06 search path; established conftest pattern fixed it, followed by combined6PASS0.50/0.96seconds. No production credentials are stored in evidence.

## Raw evidence indexes (Host, ignored)

All under .local-test-evidence/2026-09-13/p33-g/; preserved even though the clock crossed September14 during this continuation.

| Relative file | SHA-256 |
|---|---|
| long-context-capacity-real256-v1/summary.json | 21a02346ef7e1b3b1f4aee957afaf42e3b16dca538993c7a67a28e6e59479514 |
| long-context-small-diagnostic-v1/summary.json | 7827888723dd94203fddd921efcb330c3de1c8fc4695346f9afde466549dd83e |
| long-context-capacity-real256-v2/summary.json | a39e0a89bc336570a6543613f0b1eea1720a386ca12d83954bc57b413480634e |
| long-context-capacity-real512-v1/summary.json | 514c7a7e4fab9e4710d35bd597f4a2f488d70175058704d2ec285ae629c7ab10 |

Commands use the shared SDK .local-test-evidence/2026-09-12/p33-g/run-tests.py wrapper (one lock) and Host source-test-live-v21/bin/python. Capacity probe is the ignored Host long-context-capacity-probe.py, arguments 262144/524288 and unique run label. SDK/Host test selectors are listed above and in the corresponding receipt args. Latest cumulative suite remains historical f25a4de1826PASS/9paidSKIP; not current-source proof.

## Prior unrecorded checkpoint now reconciled

SDK753b61a /Host383d0835 native budget-v29: actual UI creation, artifact open and new-process cold read PASS; mission83ece7bbe60981fb completedverification_passed,24calls/3600tokens/0reserve/0rehandoff. Lifecycle150.418+80.823seconds. case-summary.json SHA2565c00345f2c4eef7abcb162be53f1c3cc568ced85c0b37291f423769df67b8b9b. It predates this context change.

Same SDK real pair v10 remains FAIL: FIRST no_progress256.875448seconds/307073tokens/33calls; COMPARE budget_exhausted448.946754seconds/535771tokens/60calls; total842844tokens/93calls, runner706.52seconds. No comparison value claim. P34/P35-A04 and overall Phase3 remain OPEN. P36 archived replay/attribution and new privacy control are partial evidence, not all-P36 completion.

## 2026-09-14 native and long-history verification

Committed production SDK dab3d442f76ea1d967a83b0cfa1e3cd46c937524 /Host e183e400. Clean committed g-long-context-committed-v1:25PASS13.31s/runner13.80s, including actual SIGKILL UNKNOWN frozen-request recovery, multi-profile load and existing journal/tool protocol controls. These old smaller-cap controls remain distinct from the new pinned-capacity test.

New tests/agents/test_long_context_capacity.py parameterized256/512 passed2/6.16s (runner6.40): actual BPE-sized history rotates only after exceeding capacity; original journal exact; instructions/current input and complete tool groups retained; cold same-input replay has zero new calls; required over-cap content rejects before dispatch. Controlled provider, not multi-turn real-model quality or a new512K UNKNOWN-kill experiment.

Actual native source-ui-long-context-v30: default256K and selected512K each submitted via UI to real deepseek-flash, formally delivered and actual NOTES.md opened with three exact expected lines. New independent app/backend cold startup reopened both original files and retained each actual capacity. 256K mission32bbd63a777d55e7:4calls11566tokens, actual Mission9.871s;512K mission009f4bad3ec6c987:4calls12454tokens,11.156s. Native lifecycle224.909s and cold70.252s include human-style interaction/startup, not model duration. All8calls succeeded;0reserve/0rehandoff. Both artifacts54bytes, hashes316a097081edb266db0cda194c59ecd704e8c065f2af74509d86375dac3e9b70/add0c535b3d2f45977bf1903b56b46ec424ba15bcbd5083e09949327d5ed2027. Before/after entire Mission/Task/intent/artifact payloads,73events,12verificationrows,8grants,20journalrows/8selections and all provider records exactly equal except capture timestamp. No extra calls on cold read. Both owned groups exited normally; no detached carrier found; caffeinate remains.

Host ignored evidence source-ui-long-context-v30/case-summary.json SHA2567c6f0abe582fd3b948d71e577bcd62422285b6c47370161995688ca27162eb73; before-cold.json b22a16872e2d199154dbe2668fed5ea4c6860e6c88cf97b95e34166f16d53e30; after-cold.json6c709242f6d43883f04919f780226253a1436f371980d5a8b9e8b08cbc1aac8f. These two short file-writing tasks cover deterministic capacity UI and cold artifact reading, not two semantic classes or near-cap original-material Mission acceptance. Rule/format verification passed; code_test/Critic not required by these short Tasks, not claimed as executed.

LC1/3/4/5/7/8 have current software/real/native evidence as applicable. LC6 controlled selected-capacity + existing actual UNKNOWN-kill coverage is present; real multi-turn long-content semantic smoke remains to run. LC2 truepre-context automatic coexistence remains OPEN. P34 real-pair and overallPhase3 still OPEN; no packaging, release or push.
