# Real pair v12: strict FAIL with completed deliveries

Last updated: 2026-09-14 CST. Immutable source-snapshot-v34: Host df72e9b6, SDK 6fb5c50. Profile context256-8m-out32k-v7; original contract 643dc6c26c02918246d33a4b0b77d47ff4c7a2fde3ba8963ccc86766175a10e5. No live LC2/action/observer edits entered this runtime.

| Arm | Mission delivery | Frozen strict test | Calls | Known tokens | Actual seconds |
|---|---|---|---:|---:|---:|
| FIRST_VERIFIED | COMPLETED / verification_passed | PASS, with SDK failure caveat below | 60 | 522350 | 347.602484 |
| COMPARE_THEN_SYNTHESIZE | COMPLETED / verification_passed | FAIL: retained adapter protocol error | 107 | 1099980 | 650.599131 |

Total 167 calls and 1622330 known tokens; pytest 1 FAIL / 998.48 seconds, runner 998.90 seconds. No superiority conclusion or automatic promotion. Both branches delivered, but delivery does not close the original strict pair or prove C synthesized a new accepted answer.

## Actual C failure and retained fallback

COMPARE Task 2 B accepted result-2331b5eee310ef9b from a synthesizer with an empty knowledge catalog. Task 3 C had two verified Worker candidates. Its synthesis Attempt mission-af59d93088005e0b:task-3:attempt-3 failed at provider-turn:4 (agent-2a3a725f6c72afdd29fe616c28fb521a). Durable turn error: provider_protocol_error, _ProtocolErrorWithUsage, source_kind tool_parse. This physical call retained 20336 input + 8192 output = 28528 tokens, including 6065 reasoning and 14592 cached input; no rehandoff. The SDK invocation settled failed.

The final C selection used accept_complete and accepted the earlier Worker result-5675f007e00e411d. Its synthesis Attempt produced no stored Result. Final independent S later delivered result-24adabd06bfbefcb using two actual knowledge IDs. This is working fallback, not successful C synthesis. The previous v11 unknown-knowledge defect did not recur in the successful B synthesis; it does not establish a successful C repair.

8192 output consumption alone does not prove truncation. Original protocol exception retained usage but not finish_reason or malformed field; neither is reconstructed or invented. A diagnostic-only successor can preserve an allowlisted finish_reason for future failures. No widening protocol acceptance or automatic resampling is justified by this run.

## FIRST observation boundary

The adapter observer saw no exception, so the unchanged frozen strict test says PASS. SDK AgentProviderWire nevertheless rejected one parsed empty response: 4929 input + 8192 output = 13121 tokens, all 8192 output reasoning, zero rehandoff. It later recovered and delivered. Thus observer error_type=None is not evidence of zero SDK failures. The parent initially conflated the two error layers and corrected the user update. New response_empty/finish_reason/reasoning diagnostics retain transparent adapter behavior; they never rewrite this result. Both layers must be reported separately.

## Evidence and next gate

Raw local-only root, relative to Host: `.local-test-evidence/2026-09-13/p33-g/source-snapshot-v34/sdk/.local-test-evidence/2026-09-13/p34-real-search-value-8262a1f954f94512875757de94145b6a/`.

- comparison.json SHA-256: d8aef3d6abe42a008b5aa78c11fb5c1ff00acc271dc1e0a81970d0da76d526fe
- FIRST_VERIFIED/verdict.json SHA-256: 16d65af1bf185d7c6cd6d3871e1e4ec0df121c3d917e983e51a69155dceb02f1
- COMPARE_THEN_SYNTHESIZE/verdict.json SHA-256: 4f1b8fd7a738725e01ec4c72bd00468360cdb5c74675b3fde28cd428a19dfd4b
- SDK central runner: `.local-test-evidence/2026-09-12/p33-g/g-p34-real-value-pair-v12.json` and `.log`.

Original v11/v12 evidence stays unchanged. No immediate paid reroll. Complete deterministic diagnostics, LC2/action fixes, source-native checks and cumulative audit first. P34 strict comparison and whole Phase3 remain OPEN; packaging/installer/release are deferred by user.
