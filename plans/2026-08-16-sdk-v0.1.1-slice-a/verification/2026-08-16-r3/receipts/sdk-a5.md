# SDK-A5 command receipt

- Command: `uv run pytest -q tests/artifact/test_exact_wheel_consumer.py::test_exact_wheel_clean_python311_cli_and_pytest_protocol tests/integration/runtime/test_kernel_start.py::test_policy_pin_rejects_drift_after_pre_checkpoint_restart`
- Result: PASS (`2 passed in 3.57s`)
- Verified roots: `run-max_turns` on the fresh lane and `run-policy-drift` on the temporal-fault lane.
- Negative assertion: hard-budget exhaustion and pre-checkpoint policy drift both fail closed before unbounded or drifted execution.
- Raw JUnit index: `.local-test-evidence/2026-08-16/sdk-v0.1.1-slice-a/r3-formal/sdk-a5.xml`
- Raw JUnit SHA-256: `674dbecd1b2df7bde6875df7bb578ab687141d35a64b18c77db42d0ed28c1693`
- Raw JUnit size: 549 bytes
