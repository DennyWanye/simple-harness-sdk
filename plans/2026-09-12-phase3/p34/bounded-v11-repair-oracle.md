# P34 v11 bounded repair oracle (2026-09-14)

Scope: preserve the original v11 FAIL and every raw evidence file. This note fixes the contract for a subsequent run; it does not reclassify v11 or claim a real pair PASS.

- Same-Task COMPARE synthesis C: `used_knowledge` contains only IDs actually present in the Attempt's `verified_knowledge` catalog. With an empty catalog, submit `[]`; candidate artifact IDs and validated fragment IDs remain input lineage/material, never knowledge aliases. Unknown IDs remain invalid. Final synthesis S still has the original requirement to cite actual verified knowledge derived from C.
- Full-read observer: prove exact bytes only from successful read pages for the same Attempt and path, with complete coverage starting at zero, consistent declared SHA-256, a terminal page, and a recomputed SHA-256 over complete UTF-8 bytes matching the accepted artifact. Gaps, contradictory overlaps, mixed attempts, incomplete streams, wrong SHA, and byte/hash mismatches fail. Identical duplicate pages and matching overlaps are harmless; a single full page remains valid.
- Retain the accepted-artifact oracle and strict FIRST physical-error oracle unchanged. The observer is diagnostic evidence, never a synthetic tool reply or a relaxed acceptance gate. Parent owns all test/API/native execution; no provider run or historical evidence mutation in this repair.

Parent review: changed prompt suffix to compare-v2 so newly frozen Attempts distinguish corrected instructions. Pending request configuration is not rewritten. Initial focused44PASS8.91s/runner9.38s; SDKvenvruffPASS (the live test venv lackedruff). Independent Sol/medium review ACCEPT, no concrete finding. Added actual frozenAttempt prompt-version assertion; final committed recheck and real pair still pending.

Final dirty focused44PASS8.86s/runner9.32s includes actualAttempt compare-v2 assertion. Original actual source materials and strict delivery oracles preserved. No usage or production verifier change.
