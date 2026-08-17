# SDK-A6 command receipt

- Command: `uv run pytest -q tests/conformance/test_suite_runner.py tests/conformance/test_cli.py tests/artifact/test_exact_wheel_consumer.py::test_exact_wheel_clean_python311_cli_and_pytest_protocol`
- Result: PASS (`28 passed in 3.57s`)
- Verified surface: SDK-owned verifiers, shared async runner, CLI and clean-wheel pytest protocol; four suites executed 22 consumer cases.
- Negative assertion: conformance execution returns a report and never creates an SDK Run; the scenario records no Session ID or Run ID.
- Raw JUnit index: `.local-test-evidence/2026-08-16/sdk-v0.1.1-slice-a/r3-formal/sdk-a6.xml`
- Raw JUnit SHA-256: `bf0293b70a25307e1fa5bd19b91751ddad61d3fab9e3a70fccd5e73d964534ea`
- Raw JUnit size: 3568 bytes
