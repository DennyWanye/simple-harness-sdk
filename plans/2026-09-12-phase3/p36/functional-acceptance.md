# P36 functional sidecar acceptance matrix

Updated 2026-09-13 CST. Scope is the approved Phase 3 functionality only; packaging and release are excluded. This sidecar adds no production guard, API, policy, or implementation change. It was prepared at SDK `753b61a`; the parent remains the sole runner.

| P36 target | Existing selector(s) | Verdict | P36 action |
| --- | --- | --- | --- |
| A04 test-only isolation | `tests/orchestrator/step08/test_evaluation.py::test_review_p0_1_every_case_and_every_run_may_only_bring_the_test_service`; `tests/orchestrator/step09/test_policy_gates.py::test_s9_03_a_passing_candidate_runs_only_in_evaluation_libraries` | Covered: validation and run-time service checks refuse a real connector before a Mission exists; candidate and baseline each bind an evaluation library, while production has no evaluation Mission policy. | No duplicate. |
| A05 insufficient statistics cannot promote | `tests/orchestrator/step09/test_policy_gates.py::test_thin_samples_or_a_broken_harness_are_insufficient_never_failed`; `tests/orchestrator/step09/test_policy_promotion_closure.py::test_the_policy_promotion_demo_closes_the_loop` | Covered: thin or broken evaluation reaches `INSUFFICIENT`; the public policy workflow records insufficient history instead of registering a promotable candidate. | No duplicate. |
| A06 old Mission frozen; new Mission uses new policy; rollback | `tests/orchestrator/step09/test_policy_binding.py::test_s9_04_an_in_flight_mission_keeps_its_version_when_the_deployment_changes`; `tests/orchestrator/step09/test_policy_binding.py::test_s9_05_a_rollback_leaves_the_events_and_costs_of_the_rolled_back_version`; `tests/orchestrator/step09/test_policy_promotion_closure.py::test_the_policy_promotion_demo_closes_the_loop` | Covered: old binding survives promotion, later Mission uses ACTIVE policy, rollback restores the prior active version without altering historical events, costs, or replay. | No duplicate. |
| A08 secret-safe support evidence | `tests/orchestrator/step06/test_observability.py::test_s6_09_a_credential_never_reaches_the_evidence_and_no_value_is_echoed`; `tests/orchestrator/step06/test_step06_review_fixes.py::test_p1_5_credential_bearing_artifacts_are_withheld_and_json_is_redacted` | Existing coverage scans baseline/event output and withholds credential-bearing workspace artifacts. It does not inject a canary into the public `test_report` argument, although `write_evidence()` persists it as `test-report.json`. | Add `tests/orchestrator/p36/test_functional_acceptance.py::test_p36_a08_support_evidence_redacts_test_report_payload`: real fixture Mission -> public `write_evidence()` -> receipt redaction plus full generated-directory scan. |

## Actual uncovered boundary

`agent_orchestrator.governance.learning.learn()` accepts history paths without a caller or `Principal`, and the public policy history CLI likewise has no history-authorisation parameter. Therefore an “unauthorised history” acceptance control cannot state an authorised/unauthorised outcome through an existing public entry. Adding such a test would either encode behaviour that is absent or require a new guard/API, both outside this sidecar's write scope. The secret-safe support-evidence path is covered by the one new control above.

## Parent runner matrix

Run only if the parent includes this sidecar in its bounded suite:

```text
tests/orchestrator/p36/test_functional_acceptance.py::test_p36_a08_support_evidence_redacts_test_report_payload
```

Parent run `g-p36-support-report-v1` first stopped at collection with `ModuleNotFoundError: helpers_step06`. P36 now follows the P34/P35 local-conftest pattern: `tests/orchestrator/p36/conftest.py` exposes the existing `step06` fixture-builder directory; no fixture code was copied. Static review confirms the test calls the existing fixture Mission -> public `write_evidence()` path, supplies `test_report` as its persisted mapping schema expects, and asserts the documented `redactions` receipt shape. The expected next result is fixture behavior, not a production/provider failure; a remaining failure would indicate either the evidence-writer redaction contract or the established fixture execution path. No timing claim is made here; tool metrics are authoritative. Files changed: `tests/orchestrator/p36/conftest.py`; `tests/orchestrator/p36/test_functional_acceptance.py`; `plans/2026-09-12-phase3/p36/functional-acceptance.md`.

Parent verification update2026-09-14: g-p36-support-report-v2 passed the new public test-report canary control plus five context controls (6PASS0.50s/runner0.96s); original collection failure retained. SDK adjacent g-long-context-sdk-integration-v1 also included the P36 control. This does not close all P36 or the unauthorised-history boundary.
