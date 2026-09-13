# Real pair v11: strict FAIL, delivery separately observed

Last updated2026-09-14 CST. Immutable SDKdc2f156, experimentcontext256-8m-out32k-v7. Original comparison.json retained under `.local-test-evidence/2026-09-13/p34-real-search-value-ad741ef4677248f08d3a8a96cb4ffd55/`, SHA256 `0e0d3b66fd2652080b3221dda6d790a3fd13771a7dd35818ef7ccc9efe0161ac`. Runner g-p34-real-value-pair-v11:1FAIL1073.22s/runner1073.58s.

| Arm | Mission delivery | Strict result | Calls | Reported tokens | Wall seconds |
|---|---|---|---:|---:|---:|
| FIRST_VERIFIED | COMPLETED/verification_passed | FAIL: retained physical Provider protocol error |73|704488|334.649|
| COMPARE_THEN_SYNTHESIZE | COMPLETED/verification_passed | FAIL: C accepted prior candidate, not new synth |120|1215487|738.299|

Total193calls/1919975known tokens; COMPARE+510999tokens. One pair proves no quality superiority. FIRST error carried21679input+832output22511tokens (188reasoning includedoutput,21120cache). The ledger retained and settled it; later recovery delivered. Error type alone does not establish output truncation; malformed field was not retained, so cause is not claimed. Frozen strict no-physical-error oracle remains unchanged.

COMPARE synth read two distinct candidates but submitted three unknown used_knowledge IDs and was correctly rejected. Accepted C is a previous candidate. Offline rootcause found actual verified_knowledge=[]/retrieval0 while generic Synthesizer demanded nonempty knowledge, and candidate artifact/verified fragment IDs were present. Scope-limited repair must distinguish input lineage from actual knowledge and allow[] only when catalogempty; final independent S requirement and unknown-ID rejection remain.

The old observer reported no full-byte C read of accepted B DOCS.md, but11289Unicodecodepoints exceed the8192-byte tool page cap. Same-Attempt sameSHA pages0→8104→end concatenate to the acceptedB fullbytehash; two C candidates also have complete pagechains. Requiring one page was an observer false-negative, not proof of missing reading. New observer must validate contiguous sameAttempt/path/SHA/size byte-complete coverage and reject gaps/contradictions. This correction alone cannot turn failed synth or original pair into PASS.

All initial workspacefiles retained42/42FIRST70/70COMPARE; intended baselinefailure and verified F plus final C/S delivery separately established. No new paid pair before deterministic repair/review. See bounded-v11-repair-oracle.md for successor scope. OriginalFAIL and raw observations preserved.

VERDICT: FAIL — original v11 strict pair; new bounded repair and real revalidation pending.
