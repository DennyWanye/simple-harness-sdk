# Minimal Consumer Example

Complete working example showing how to integrate Simple Harness SDK into your application.

## What This Demonstrates

- ✅ Provider port implementation (mock LLM)
- ✅ Tool executor port (calculator + echo tools)
- ✅ Authorization port (always allow)
- ✅ Context port (SQLite persistence)
- ✅ Runtime setup and execution
- ✅ Error handling

## Prerequisites

```bash
pip install simple_harness_sdk-0.1.1-py3-none-any.whl
```

## Project Structure

```
minimal-consumer/
├── README.md          # This file
├── demo.py            # Main entry point
├── ports/
│   ├── __init__.py
│   ├── provider.py    # Mock LLM provider
│   ├── tools.py       # Calculator + echo tools
│   └── auth.py        # Always-allow authorization
└── execution.db       # SQLite database (created on first run)
```

## Running the Example

```bash
# From this directory
python demo.py
```

Expected output:
```
[Runtime] Starting run run-001
[Agent] Thinking...
[Agent] Using tool: calculate
[Tool] calculate(expression="2+2") → 4.0
[Agent] Result: The answer is 4
[Runtime] Run completed: COMPLETED
```

## Key Files

### `demo.py`
Main entry point that:
1. Sets up all ports
2. Builds runtime
3. Executes a simple calculation task
4. Prints results

### `ports/provider.py`
Mock LLM provider that simulates:
- Tool-calling workflow
- Realistic latency
- Token usage tracking

### `ports/tools.py`
Two simple tools:
- `calculate`: Evaluate math expressions
- `echo`: Echo back input

### `ports/auth.py`
Always-allow authorization (for demo purposes)

## Next Steps

1. **Replace mock provider:** Implement real LLM client (OpenAI, Anthropic, etc.)
2. **Add real tools:** File I/O, web search, database queries
3. **Add authorization UI:** Show permission dialogs to users
4. **Enable workflows:** Add `workflow_registrations` and `workflow_services`
5. **Add memory:** Implement `MemoryQueryPort` and `MemoryWritePort`

## See Also

- [Integration Guide](../../docs/integration-guide.md) - Complete step-by-step guide
- [Quickstart](../../docs/quickstart.md) - 10-minute quick start
- [API Reference](../../docs/api/) - Detailed API documentation
