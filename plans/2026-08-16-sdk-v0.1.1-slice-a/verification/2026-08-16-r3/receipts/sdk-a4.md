# SDK-A4 command receipt

- Command: `uv run pytest -q tests/integration/execution/test_delivery_dispatcher.py::test_sink_success_before_settle_crash_retries_same_key_after_reopen tests/integration/runtime/test_kernel_start.py::test_startup_backlog_remains_durable_and_drains_after_runtime_reopen`
- Result: PASS (`2 passed in 0.19s`)
- Verified roots: two isolated `run-delivery` databases covering sink-success/before-settle crash and startup backlog reopen.
- Negative assertion: retry uses the same idempotency key; the idempotent Host sink exposes one visible delivery.
- Raw JUnit index: `.local-test-evidence/2026-08-16/sdk-v0.1.1-slice-a/r3-formal/sdk-a4.xml`
- Raw JUnit SHA-256: `54609203f5ab102eb436d5ee766a8b6752e3241b7a105626841d831207c4b87e`
- Raw JUnit size: 582 bytes
