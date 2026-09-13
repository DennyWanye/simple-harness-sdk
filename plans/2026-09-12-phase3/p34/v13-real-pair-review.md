# P34 v13 fixed-source real comparison

Last updated: 2026-09-14 CST. Overall strict pair FAIL/OPEN; COMPARE arm PASS. No packaging/release/push. Both Missions completed, but delivery is not the strict oracle.

Frozen source: SDK7f926b217aaddd71ec8ea3a215456046bf735174 / Host18ff5d247452856bdd10f82b1f4bca07f0274d56, snapshot-v39. Model deepseek-flash at api.deepseek.com. Same contract643dc6c26c02918246d33a4b0b77d47ff4c7a2fde3ba8963ccc86766175a10e5; context256-8m-out32k-v7, default output8192/ceiling32768, unchanged materials/criteria. No arm reroll.

| Arm | Strict result | Mission | Physical calls | Total tokens | Cached input subset | Seconds |
|---|---|---|---:|---:|---:|---:|
| FIRST_VERIFIED | FAIL: one physical tool-parse error | COMPLETED/verification_passed |56|544122|368000|322.582|
| COMPARE_THEN_SYNTHESIZE | PASS | COMPLETED/verification_passed |101|998962|676992|556.739|

Both use independent databases with deterministic mission-af59d93088005e0b. Runner880.20s; pytest1FAIL879.63s. Total157calls1543084tokens; no rehandoffs, final reserved0 in both. Comparison costs454840 more tokens in this single pair; no quality superiority or monetary price claim. Runtime admission exercised56/101 grants. These are application API tokens, not Codex subscription usage.

COMPARE strict checks include actual verified consumer C and final S, approval/selection binding, Manager repair, failed-fragment reuse and same-Task candidate synthesis. Consumer result result-63773c01d6753f4d, reused fragment result-ab48e1c81d30cdfe; failed baseline results result-2128965cbd0303dc and result-5285d1995cc08baf remain. No other audit failures. Raw criteria and verifications remain in its verdict.

FIRST error request agent-cd35fec3b3591f6b23239ed0bacbd162:provider-turn:8, task-3 attempt-1. Persisted AgentTurn error is provider_protocol_error/tool_parse, finish_reason=tool_calls, input18111/output1347/total19458/cache17920/reasoning503. It is not confirmed output truncation; the length-only recovery must not trigger. The old adapter did not retain the precise parse branch; invalid JSON versus missing fields cannot now be distinguished. Original failed usage is fully accounted. Mission later delivery does not erase that physical failure, and the oracle stops at that failure rather than certifying all remaining FIRST checks.

Raw evidence under Host ignored .local-test-evidence/2026-09-13/p33-g/source-snapshot-v39/sdk/.local-test-evidence/2026-09-13/p34-real-search-value-c5e30aaff39048b69a938daca65548c8/:

| File | SHA256 |
|---|---|
| comparison.json |4c23f337f55c919a237ac2f4ab3a98071248c040623ca4c4224b6503022766da|
| FIRST_VERIFIED/verdict.json |77d2156fec235d66748c6dc864644bf5fcfed3f6bc1414757742ce91b9db1748|
| COMPARE_THEN_SYNTHESIZE/verdict.json |9741ca859d1d3426ed9f43f25a14a8490dbadad68f018c31b00abd82dac6292b|

Runner group55567 has no remaining processes (process-cleanup.json). All raw failures including v12 retained. This paired test is not native UI evidence; native-v39 has independent UI acceptance.

Next: safe parse-reason diagnostics are implemented/tested separately in tool-parse-diagnostics-v40.md. They do not repair this historical failure. Do not spend another full pair merely seeking a lucky PASS. Capture a precise error category on a justified successor run, prove a targeted correction, then execute a fixed full pair without dropping errors.

A read-only follow-up checked [DeepSeek strict tool-call documentation](https://api-docs.deepseek.com/guides/tool_calls/): its Beta endpoint and all-required object fields require a separate compatibility design. Existing workspace_read_file has optional offset/max_chars/expected_sha256 and run_tests has optional path. Merely switching strict=true would not preserve these contracts. No endpoint/schema switch or paid Beta probe was made; support for this exact model/profile is not established.
