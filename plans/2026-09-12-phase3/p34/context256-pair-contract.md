# New long-context paired experiment contract

plan-status: finalized (scope refinement of approved long-context Phase3 work; 2026-09-14)

This is a new experiment, not a repair of historical 2M runs or their oracle. Both FIRST and COMPARE use the same immutable recorder materials, original required behavior and assertion logic. Preserve all historical profile exports, failures and accounting. No packaging/release.

New profile: context256-8m-out32k-v7. Mission8,000,000tokens/24attempts; A1,400,000/4, F uses the same Task cap, B1,600,000/3, C1,600,000/4, S1,200,000/2. Total planned A+F+B+C+S7,200,000, leaving800,000 Mission capacity for system work. Input262144 with pinned official DeepSeek tokenizer; output8192 start/32768 bounded ceiling. Default Task budgets in this test remain explicit and the production budget mechanism must reject spending beyond those caps. Never free UNKNOWN usage or revive failed A. Both arms have the exact same budget/context and both must actually deliver verified C+S. COMPARE additionally demonstrates real Manager repair, verified failed-fragment reuse and candidate synthesis. No reroll or scripted work results.

Before real calls: pure controls assert original-v2 and all existing profile exports unchanged; new public Mission budget and A/B/C/S charter match declared values; static preflight includes F and system headroom; profile input/output/fingerprint are configured as declared, and original 2M profile still uses its original output budget. Test/assertion code must read the chosen contract rather than compare new runs to old global constants. Unknown profile fails before credentials. Parent alone runs tests and real requests under the shared lock.

One pair (FIRST then COMPARE even if FIRST fails) constitutes the run. A failure leads to diagnosis and retained FAIL, not retries to select a favorable sample. Record wall time, calls, known/unknown/cache-separated tokens and all failed attempts. Synthetic admission/evaluation approval controls remain labelled fixture. No superiority claim from one pair.

VERDICT: NOT_RUN. New code and independent review pending; current-source native256/512 short Missions already passed separately, not proof of this comparison.

Implementation/review checkpoint: parent review corrected the output reserve to32768 and explicitly binds Mission/runtime to deepseek-context-256k-v1 so the actual selected-profile floor matches Host; runtime metadata/hash and correct budget-source path added to new export only. New actual startup + public approved-policy creation proves294912 first/critic floor. Initial19PASS/1new-test schema-shapeFAIL0.33s, corrected20PASS0.30s, route integration25PASS0.53s; final pure21PASS0.37s/runner0.75s. Original hash controls retained. Test-local changes only; production unchanged since dab3d44. Paid pair still NOT_RUN at checkpoint.
