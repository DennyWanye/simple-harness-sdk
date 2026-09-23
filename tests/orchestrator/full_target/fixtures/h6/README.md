# H6 real code cohort

`cohort.json` is the deterministic input for
`agent_orchestrator.evaluation.htn_method_cohort.execute_method_cohort`. It has
exactly 50 entries:

- 20 `trial` Missions covering problems 01–20;
- 5 `heldout` Missions covering problems 21–25;
- 25 `baseline` Missions, one for each matching candidate problem.

All 50 idempotency keys are distinct. Candidate and baseline entries for a
problem have identical source, public checks, hidden checks, goal type and task
wording. The baseline goal differs only by omitting the sentence that asks the
planner to evaluate and use the trial-admitted candidate method. This keeps the
task and oracle fixed while `MethodEvaluationStore` prevents the candidate from
being retrieved in baseline Missions.

Every problem uses `code.fix-failing-test`, a fresh `target.py` stub and a real
isolated pytest oracle. `fixture.files/tests/test_target.py` is a small public
contract available to the worker. `oracle.hidden_files/tests/test_hidden.py` is
frozen evaluation material and is absent from `fixture.files`; it must only be
injected into the grader's separate post-Mission copy. `oracle.test_paths`
selects both frozen test files. Neither a model completion claim nor process exit
alone is a success: the grader must run both paths, preserve the public and
hidden bytes, and record its normal independent oracle receipt.

The problem split is fixed as follows:

| # | Cohort | Problem | Main distinction checked |
|---:|---|---|---|
| 01 | trial | `clamp` | boundary handling and invalid interval |
| 02 | trial | `flatten_once` | exactly one nesting level and input preservation |
| 03 | trial | `dedupe_by` | key-based first occurrence stability |
| 04 | trial | `merge_counts` | zero deletion and non-mutation |
| 05 | trial | `parse_csv_line` | quoted delimiter and doubled quote |
| 06 | trial | `group_runs` | consecutive grouping rather than global counting |
| 07 | trial | `rotate` | negative/oversized steps and empty input |
| 08 | trial | `safe_get` | nested traversal and stored `None` |
| 09 | trial | `partition` | stable two-way partition |
| 10 | trial | `format_bytes` | binary units, formatting and negative rejection |
| 11 | trial | `parse_range` | optional signed endpoints and malformed input |
| 12 | trial | `top_k` | descending selection, duplicates and non-mutation |
| 13 | trial | `invert_mapping` | ordered aggregation by value |
| 14 | trial | `windows` | consecutive window boundaries and invalid size |
| 15 | trial | `normalize_path` | lexical normalization and traversal rejection |
| 16 | trial | `retry_delays` | capped exponential sequence |
| 17 | trial | `coalesce_ranges` | touching inclusive ranges and invalid ranges |
| 18 | trial | `weighted_average` | weighted arithmetic and invalid weights |
| 19 | trial | `deep_keys` | recursive leaf paths and empty mappings |
| 20 | trial | `parse_duration` | compound unit parser and full-input validation |
| 21 | heldout | `bounded_chunks` | greedy text packing under a hard size bound |
| 22 | heldout | `apply_patch_map` | copy-on-write update/delete semantics |
| 23 | heldout | `resolve_dotted` | object attribute traversal with missing default |
| 24 | heldout | `stable_sort_by` | stable ordering with `None` keys last |
| 25 | heldout | `validate_slug` | whole-string grammar and separator boundaries |

The heldout set uses implementation types not present in the trial set: greedy
packing, patch application, object attribute traversal, nullable-key stable
sorting, and lexical grammar validation. Its hidden assertions are never part of
the worker fixture or prompt. Trial problems also carry hidden assertions so the
same independent scoring path applies to every candidate/baseline pair.

## Required grader contract

This fixture requires the H6/H8 code grader extension with these semantics:

1. `immutable_files` remain public files and must match the accepted workspace.
2. `hidden_files` are copied only into a separate grading directory after the
   Mission ends; they are never copied into the worker workspace.
3. `test_paths` may name only frozen `.py` paths present in `immutable_files` or
   `hidden_files`; the grader executes those paths in the separate copy.
4. The separate copy contains the accepted `target.py` plus the frozen tests.

Without that extension, the old grader hard-codes `tests/test_target.py` and
cannot execute hidden checks. In that state this cohort is prepared input, not
passing H6 evidence.

## Runtime connection

Use this file only after a real source Mission in the same runtime database has
produced a terminal, formally `TRIAL_ADMITTED` candidate whose goal signature is
compatible with `code.fix-failing-test`. Derive the exact `MethodRef` from that
stored admission; do not substitute a seed method or hand-written hash.

```bash
.venv/bin/python scripts/acceptance/run_v14_runtime.py h6 \
  --config <frozen-deployment.json> \
  --manifest <frozen-manifest.json> \
  --root <ignored-h6-evidence-root> \
  --runtime-root <ignored-original-runtime-root> \
  --method-ref <db-derived-method-ref.json> \
  --cohort tests/orchestrator/full_target/fixtures/h6/cohort.json
```

The runner freezes all 50 Mission identities and their independent oracle hashes
before scheduling. Terminal Missions with known, settled usage produce immutable
oracle outcomes in the original Store. Candidate failures remain in the frozen
denominator and retain their actual costs; a Mission that never adopted the
candidate is a failure of that candidate, even if another method completed it.
Success requires both current formal acceptance attributed to the exact MethodRef
and a passing independent oracle. The default candidate threshold remains 0.85,
with no critical or unresolved operations and median cost at most twice baseline.
Baselines must be accepted and independently pass for the comparison to be valid.
Unknown usage blocks evaluation and promotion; it is never estimated, cleared, or
retried as a new sample. Add `--promote` only when the resulting
`EVALUATED → ADMITTED` transition is authorized for the acceptance run.
