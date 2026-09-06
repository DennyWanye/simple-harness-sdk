# H077 source results — 2026-09-06

Fixed product: `37da5ee` (base frozen H076 `bc2d42c`). 21 unique new tests passed across batches; **not 24**. Original userdata is **not recovered**. No model/native/old lease10/analysis14 runs.

| Batch | Actual result | Owned PG / exit / remaining | Evidence |
|---|---|---|---|
| r1 | collection error; package-relative fixture import fixed | 71878 / 2 / [] | `.local-test-evidence/2026-09-06/decision-terminal-recovery/r1/command.log` SHA256 `382706e92abafc83a764f40a314a1edc2d804d54992f12537199344ce2f4731d` |
| r2 | 19 PASS / 2 FAIL in 0.87s | 71911 / 1 / [] | `.local-test-evidence/2026-09-06/decision-terminal-recovery/r2/command.log` SHA256 `ba56739187ab0c4d25fbceba6d062619ff1806b5f1f4ca258e72187fa50e1df7` |
| r3 | 3 PASS / 18 deselected in 0.45s; two failures fixed, filter also selected one already-green effect control | 72064 / 0 / [] | `.local-test-evidence/2026-09-06/decision-terminal-recovery/r3/command.log` SHA256 `7e541c6ba6b2402cedfea379a7a25008d7a6fa62a567cdd035ac4e97af02ff2a` |
| native-copy-resource | PASS: real r6 consistent COPY public eligibility, recovery, exact terminal and reopen | 72080 / 0 / [] | `.local-test-evidence/2026-09-06/decision-terminal-recovery/native-copy-resource/command.log` SHA256 `f3c29019d948bf8b5e2ec8d6bcbd6aeb056bca3f16d6667dde6ca9d9429cd8b2` |

Two r2 failures: SDK fixed authorization terminal codes were absent from the audit whitelist; the active-owner negative fixture needed UPSERT over the real expired lease. Fixed in37da5ee. No remaining product failure observed in this bounded matrix.

## Authority and original-data boundary

Actual r6 SDK database was copied with SQLite `backup` from `mode=ro` inside a read transaction; WAL commits were included. Candidate operations then touched only the new ignored copy. Public H075 terminal was absent before recovery. The original SDK database and WAL SHA256 stayed unchanged during this copy gate; this is a point-in-time assertion, not a ban on later authorized native writes.

Public eligibility accepted the exact original failed Run and expired decision. Explicit recovery retained original Run/decision/start/effect rows and all33 original events; added only recovery+failed events and their two SDK audit witnesses. Recovery event time is new, original resolved time remains separately bound. Public terminal metadata matches its audit proof; reopen returns the exact first proof. Source/hash-only evidence contains no conversation/credential payload.

Native copy manifest: `.local-test-evidence/2026-09-06/decision-terminal-recovery/native-copy/result.json` SHA256 `daf3d73a529ed899118a72812d3cdfd70d582bdc2a595acb83e8ce31b8dcf496`.

## Remaining boundaries

- Recovery supports only the proved first-authorization expired root React shape; unknown/multicycle/workflow/child shapes refuse. No generic SQL repair or automatic database-open repair.
- Future root React tool-authorization terminal transitions cover both command branches; generic admission/child terminal domains are not redefined.
- Original r6 userdata has not been recovered. Host public wiring, installed077 consumption and subsequent native restart remain pending at this source checkpoint.
- Public metadata reader is additive and retains unique event/kind-state checking; it does not export raw terminal payload.
- Source tests used the existing tiny Python/generic dependencies and explicit candidate source. This is not an independently installed077 environment.

## Reproduction

Run the committed `tests/integration/runtime/test_decision_terminal_recovery.py` under the default Host `scripts/run_resource_bounded.py` OS lock (2GiB/180s, disk guards). Set `H077_LEGACY_075_TARGET` to the preserved `.local-test-evidence/2026-09-06/short-context/artifact-075/installed-target`; the child asserts exact075 version and module origin before generating the genuine old defect. The exact executed command and source-loading script are retained at the evidence root. No full-suite claim.
