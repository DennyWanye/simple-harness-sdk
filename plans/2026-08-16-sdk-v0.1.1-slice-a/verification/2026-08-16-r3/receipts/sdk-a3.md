# SDK-A3 command receipt

- Command: `uv run pytest -q tests/integration/runtime/test_react_sqlite_runtime.py::test_require_user_is_durable_and_double_bound_before_tool_handoff tests/integration/runtime/test_react_sqlite_runtime.py::test_open_authorization_survives_runtime_restart_and_invokes_at_most_once`
- Result: PASS (`2 passed in 0.05s`)
- Verified roots: `run-auth`, `run-fault`; physical Tool remained zero before dual receipt handoff and at most once after restart.
- Negative assertion: wrong/absent authorization binding never reaches the physical Tool boundary.
- Raw JUnit index: `.local-test-evidence/2026-08-16/sdk-v0.1.1-slice-a/r3-formal/sdk-a3.xml`
- Raw JUnit SHA-256: `52c60fcbc80b70e0dce1505fc73c4ec8b0d623841ef32fed2aff93357fda13c8`
- Raw JUnit size: 592 bytes
