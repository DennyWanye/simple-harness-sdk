# Manager wire contract and failed COMPARE fragment recovery

Last updated: 2026-09-13 16:45 CST. JOURNAL_VERDICT: IN_PROGRESS.

## Decisive actual failure

Clean SDK 6c17d4d, fixed audit320-docs480-s240-v4, same original materials/oracle and per-Mission2M/24. Real deepseek-flash v7 ran one FIRST and one COMPARE, both FAILED/no_progress. FIRST327.513s/441139tokens/52calls produced an independent F, but a later Manager used nested operation wrappers rather than the required flat op field, leaving C/S blocked. An earlier fragment proposal also had an unknown field. COMPARE337.840s/422739tokens/49calls completed B, but stopped when both original A candidates failed: the runtime never allowed Manager fragment recovery from an empty bounded selection. Total101calls/863878tokens; all observed actual model deepseek-flash and known usage, no observed physical Provider error. This is not successful delivery or evidence of a quality/cost benefit.

Raw SDK .local-test-evidence/2026-09-13/p34-real-search-value-3a38e3f86874442999f0774077887b80/pair-summary.json SHA256638d9e480774c987e3eabace70f820cfb44ea7dc94a2852cb62556c03d8ec929. Pair pytest665.59s/runner666.06s. Both Mission IDs mission-74ddfd18fa43ca85 belong to separate arm databases; failures remain immutable.

## Implemented behavior

- New default manager-v4 gives exact executable flat operation examples and excludes unknown fragment fields. Historical manager-v1/v2/v3 and all document templates keep their exact hashes. Strict parsing remains in place.
- A durable bounded empty candidate decision may request one independent F validation before its original deadline, funded from existing Mission budget. It cannot reopen A, reset its budget, erase either FAIL, or retry Manager per scheduler tick. Invalid proposals, unavailable funds, disabled management, expired deadlines and failed F stop within the original bounds.
- The selection round, empty stop decision, actual originating Attempt and live deadline are checked within the same write transaction that creates F. Successful command replay preserves the original bound identity. A separately verified F and a normal downstream Manager patch can retarget C to F+B and cancel A while retaining its failures.
- Completed COMPARE predecessors remain in allocator dependency facts. C candidate2 and its new synthesis consume the same independently accepted F under their original live round, frozen policy and exact prior input bindings; they do not impersonate a failed retry.

## Software evidence and rework

All raw outputs stay in ignored SDK .local-test-evidence/2026-09-12/p33-g/. Parent is the sole outer pytest runner using Host source-test-live-v21 Python3.12 and editable SDK.

| Run | Outcome | pytest / runner seconds |
|---|---|---:|
| g-manager-wire-v1 | 5PASS; historical hashes, strict parser examples and actual SDK Manager request | 0.41 / 0.91 |
| g-selection-fragment-red-v1 | Original selection source2FAIL, no Manager requests | 0.69 / 1.20 |
| g-selection-fragment-green-v1 | 1FAIL/1PASS; fixture used wrong frozen downstream key | 20.63 / 21.14 |
| g-selection-fragment-green-v2 | 1FAIL/1PASS; actual completed dependency disappeared from allocator | 1.23 / 1.67 |
| g-selection-fragment-green-v3 | 1FAIL/1PASS; actual C candidate2 rejected as a non-failed retry | 1.43 / 1.88 |
| g-selection-fragment-green-v4 | 9PASS/1FAIL; budget fixture Mission40K could not admit its original300K graph | 23.05 / 23.54 |
| g-selection-fragment-green-v5 | Corrected existing Manager reserve500K;66PASS including adjacent selection/fragment/replay | 29.16 / 29.68 |
| g-selection-fragment-cold-v1 | 7PASS, includes actual library close/reopen after F commit and downstream graph commit | 5.57 / 6.04 |
| g-selection-fragment-atomic-v1 | 8PASS, adds deadline crossing between collector and commit | 5.95 / 6.46 |

Cold tests retain original round ID/deadline/Attempt IDs/decision/policy, complete C after independent F, preserve both A FAILs, and produce no second Manager handoff, extra events or charges on a further run. This is library reopen, not OS SIGKILL or native UI evidence. Ruff and mypy117 pass. Wider integration g-manager-selection-integration-v1:1292PASS/4FAIL/5SKIP in142.78s/runner143.23s. Four failures were old current-default manager-v3 assertions; updated to manager-v4 while historical hash checks remain. Four optional tokenizer skips covered by the pinned-tokenizer followup; one paid API remains opt-in. Legacy wrong-round receipt test first failed as expected0.10s/runner0.42s, then restricting legacy replay to unbound commands passed. Final affected g-manager-selection-boundaries-v2:52PASS/no skips10.12s/runner10.44s; ruff/mypy117PASS. Clean committed recheck remains pending.

## Native Critic slice already observed

Host dbd7fd07 / SDK6c17d4d snapshot-v27: source-native UI created mission-fdbc5683ef374677, showed COMPLETED/verification_passed with format/rule/CriticPASS and two VERIFIED source citations. Actual opened compare.md108B SHA256dbd66082d0d81b180abc8f7fb38991551e78cfcef7795356018ff10151630fcc matched after a new app/backend process reopened the same data. Both readings retained1050tokens/0reserve/7calls/0rehandoff,46Mission events/17journal/7context selections. Controlled Provider: not a paid model quality claim. Lifecycle291.693s/cold176.755s; process groups exited and managed ports were free. Host .local-test-evidence/2026-09-13/p33-g/source-ui-critic-doc-v27b/case-summary.json SHA25615a3363707b078977e57d2886ead887eef5c91552e9bf4fa218c29b71eea297b. Latest Manager/selection source is not included in this historical snapshot.

## Child effectiveness

| Child / verified model | Wall seconds including waits | Uncached input | Cached input | Output | Result / rework |
|---|---:|---:|---:|---:|---|
| Archimedes Terra/high | 734.19 | 147195 | 2703360 | 27077 | manager-v4 draft and5 tests; syntax/prose assertion and parent import sorting rework |
| Carson Sol/high | 926.31 | 139438 | 2319872 | 14111 | Independent review found expiry, unfunded Manager handling and prompt gaps; followup atomic race and legacy receipt binding. All findings have parent regression/fixes tracked above |

Numeric source: Host ignored agent-efficiency-0500/usage-1644.json. Work overlaps; wall durations include waiting and are not summed as elapsed development time. Parent integration/review approximately16:15 onward, no exclusive coordination token/CPU metric. These unmatched tasks do not prove model rankings or monetary savings. Both children closed after their outputs; main session model unchanged.

## Next gates

Finish legacy binding regression and committed affected checks, then exactly one new v8 FIRST/COMPARE with the same audit320-docs480-s240-v4 budget/material/oracle. Preserve all previous groups; do not reroll without a new fix. Current actual value gate remains OPEN. Only after decisive value passes run cumulative source checks and latest source-native acceptance. P35-A04 and overall Phase3 remain OPEN; no packaging/P36/push.
