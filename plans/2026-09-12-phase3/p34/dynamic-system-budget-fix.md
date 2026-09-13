# P34/P35 dynamic system budget correction

Last updated: 2026-09-13 17:48 CST. Status: implementation verified locally; independent review and committed real/source-native gates pending.

Scope: original P34-A02/A05/A08 and P35-A04. Continue approved Phase3 function/source-native work; no packaging, installer, release or push. P36 functional AC remain in scope for inventory and later acceptance.

## Decisive acceptance recorded before implementation

Dynamic Task commitments must count a materialized synthesis or Conflict Task once. Initial Planner admission keeps the full fixed system reserve. Unmaterialized synthesis and remaining conflict reserve are protected. Cancelled/removed work retains settled and in-flight commitment; unused allocation returns only once. Missing/conflicting reserve state cannot create additional spendable budget. A400+B480+C400+S240+F400=1920K must fit a2M pool; A480+B480+C400+S240+F480=2080K must not. Actual public commit and cold replay must remain idempotent. Frozen previous experiment exports/materials/criteria stay unchanged.

## Failure and minimal change

Clean af1c76f v9 real deepseek-flash pair failed both arms with no_progress: FIRST244.780s/293284tokens/36calls; COMPARE319.653s/454042tokens/59calls, runner564.99s. Both F proposals were rejected with committed1600K plus incorrectly duplicated S240K leaving160K for F480K. The parent experiment also overcommitted2080K even after correcting this duplicate; this configuration oversight is distinct from the production defect. No quality/value PASS is claimed and original FAIL files remain unchanged. Evidence: SDK .local-test-evidence/2026-09-13/p34-real-search-value-c4d358554b3246a4bd4d953810111171/comparison.json.

Dynamic validation now adds only unmaterialized S plus conflict_reserve_remaining; existing Task commitments already include materialized allocations. Original conflict allocation conservation is checked when a remaining balance is present; missing balance conservatively retains the full original reserve. Initial planning and cancellation settled/inflight calculation are unchanged. No new flag or public schema.

New experiment audit400-docs480-s240-v6 preserves per-Mission2M/24 and allB/C/S budgets, materials, criteria and policy. A/F each400K. A pure preflight rejects future2080K experiments before credentials or Provider configuration/calls. Historical v5 export/hash remains accessible for old-evidence review. A320K was observed insufficient in v8; A400K supplies80K Worker/Critic headroom without exceeding static pool.

## Verification and rework

- New dynamic controls old source: initial6FAIL/5PASS0.49s included wrong exception class and incomplete cancellation fixture. Corrected fixtures:5 decisiveFAIL/6PASS0.41s, runner0.70s, g-dynamic-system-budget-red-v2.
- Patch: new11 controls plus graph/conflict/synthesis/selection37PASS11.71s, runner12.16s, g-dynamic-system-budget-green-v1. Includes public S creation,1920K dynamic commit and cold receipt replay;2080K atomic rejection; initial/unmaterialized reserve; conflict exact limit; malformed/missing balances; settled+inflight cancellation.
- Added actual public conflict accept/create -> exact40K dynamic admission,40K+1 rejection and cold receipt replay. Initial parent missing imports corrected. Child profile assertion omitted nullable Budget export fields and normalization accidentally changed equal C400K text; both corrected without changing the frozen input contract.
- ruffPASS; mypy117 source filesPASS. Adjacent integration214PASS/3 opt-in realSKIP71.88s, runner72.28s (g-dynamic-budget-integration-v4). Sol/high independent review requested public conflict+cold coverage and caught the same C-normalization assertion; both fixed and covered by this run. Clean commit and real/native verification pending.

Raw evidence remains Git-ignored. No full Phase3 completion or statistical model advantage claimed.

VERDICT: IN_PROGRESS — current budget fix needs final review and committed real/native gates.
