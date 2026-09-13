# Independent test evidence before Critic review

Last updated: 2026-09-14 CST.

Native v37 exposed a production ordering bug: Worker ran pytest, but the verifier called Critic before code_test, so test_output was always None. Real Critic repeatedly refused the missing execution evidence. The parent cancelled that Mission; its 43 calls and 172374 tokens remain recorded as a failure.

When policy requires code_test and local execution is enabled, the router now runs it on the verification copy after format/rule gates and before Critic. Only that actual stdout reaches Critic. A failing deterministic test skips the model call. Layer names and final presentation order remain stable; a completed test keeps its actual evidence even if Critic later rejects content. Human-review resume reuses recorded layers without rerunning tests or models. Disabled execution, ablations and policies without code_test retain their boundaries.

Focused checks: 56 PASS / 1 obsolete-behavior assertion FAIL in 18.51s. The existing repair test was updated to expect no first Critic after failed pytest, zero usage settlement of the original 6000-token hold, and only the successful attempt's Critic call. Its original artifact, retry and budget checks remain; follow-up 1 PASS in 2.06s. Ruff and mypy pass. The new reuse check uses an actual child-process test followed by in-memory LayerResult reuse; it is not native cold-start evidence.

Raw evidence: ignored SDK `.local-test-evidence/2026-09-12/p33-g/g-verifier-order-v38{,-fixed,-accounting}.{log,json}`. Current cumulative checks and the same native goal still require verification. No packaging, release or push.
