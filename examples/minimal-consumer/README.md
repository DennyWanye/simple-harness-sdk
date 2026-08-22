<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Minimal Consumer Example

Complete working example showing how to integrate Simple Harness SDK into your application.

## What This Demonstrates

- ✅ Provider port implementation (mock LLM)
- ✅ Tool executor port (calculator + echo tools)
- ✅ Authorization port (always allow)
- ✅ Context port (SQLite persistence)
- ✅ Runtime setup and execution
- ✅ Terminal-state assertion (exit code 0 only when the run reaches `COMPLETED`)
- ✅ Re-runnable: fresh run IDs and a temporary database on every invocation

## Prerequisites

The SDK is not published to PyPI. Build the wheel from the SDK repository and
install it (see [docs/quickstart.md](../../docs/quickstart.md) for the full
walkthrough):

```bash
git clone <sdk-repo-url> && cd simple-harness-sdk
uv build
pip install dist/simple_harness_sdk-0.3.0-py3-none-any.whl
```

## Project Structure

```
minimal-consumer/
├── README.md          # This file
├── demo.py            # Main entry point
├── verify_from_zero.sh# From-zero verification (clean clone → install → run)
└── ports/
    ├── __init__.py
    ├── provider.py    # Mock LLM provider
    ├── tools.py       # Calculator + echo tools
    └── auth.py        # Always-allow authorization
```

The SQLite execution database is created in a fresh temporary directory on
every run, so repeated runs never collide with persisted state.

## Running the Example

```bash
# From this directory
python demo.py
```

Expected output (run IDs and temp paths vary per run):
```
[Runtime] Starting run run-2af0a5bc
[Agent] Thinking... (using calculate tool)
[Tool] calculate(expression='2+2') → 4
[Agent] Formulating final answer...
[Runtime] Run terminal state: completed
✅ Task completed successfully
```

Exit code contract: `0` when the run reaches `COMPLETED`, `1` otherwise.

## Key Files

### `demo.py`
Main entry point that:
1. Sets up all ports
2. Builds runtime
3. Executes a simple calculation task
4. Reads the real terminal state via `client.query(run_id)` and asserts it

Note: `runtime.wait_idle(run_id)` returns `None` — it only waits until the run
is no longer live. Always read the terminal state back via `client.query()`.

### `ports/provider.py`
Mock LLM provider that simulates:
- Tool-calling workflow
- Realistic latency
- Token usage tracking

### `ports/tools.py`
Two simple tools:
- `calculate`: Evaluate math expressions
- `echo`: Echo back input

The consumer builder registers the configured closed tool schemas; undeclared schemas retain the
fail-closed no-argument default.

### `ports/auth.py`
Always-allow authorization (for demo purposes)

## Next Steps

1. **Replace mock provider:** Implement real LLM client (OpenAI, Anthropic, etc.)
2. **Add real tools:** File I/O, web search, database queries
3. **Add authorization UI:** Show permission dialogs to users
4. **Enable workflows:** Add `workflow_registrations` and `workflow_services`
5. **Add memory:** Pass Memory SDK 0.4 `MemoryManager` as `memory=` and use typed conversation entry

## See Also

- [Integration Guide](../../docs/integration-guide.md) - Complete step-by-step guide
- [Quickstart](../../docs/quickstart.md) - 10-minute quick start
- [API Reference](../../docs/api/) - Detailed API documentation
