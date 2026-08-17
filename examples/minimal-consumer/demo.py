"""Minimal Consumer Example - Main Entry Point

Demonstrates basic Simple Harness SDK integration.
"""

import asyncio
import time
from pathlib import Path

from simple_harness.runtime import (
    build_consumer_runtime,
    ConsumerRuntimePorts,
    RunStart,
    RunClient,
)
from simple_harness.execution.uow import RunState
from simple_harness.contracts import ExecutionSessionId, RunId, RequestId

from ports.provider import MockLLMProvider
from ports.tools import CalculatorToolExecutor
from ports.auth import AlwaysAllowAuthorization


async def main():
    """Run a simple agent task."""

    print("=== Minimal Consumer Example ===\n")

    # Setup database
    db_path = Path(__file__).parent / "execution.db"
    print(f"Using database: {db_path}")

    # Build consumer runtime ports
    print("Building runtime...")
    ports = ConsumerRuntimePorts(
        provider=MockLLMProvider(),
        tool_executor=CalculatorToolExecutor(),
        authorization=AlwaysAllowAuthorization(),
        database_path=str(db_path),
        tool_names=("calculate", "echo"),
        max_turns=10,
        max_tool_calls=20,
    )

    runtime = await build_consumer_runtime(ports)

    try:
        # Enter runtime context
        await runtime.__aenter__()

        # Create run client
        client = RunClient(runtime)

        # Create run
        run_id = RunId("run-001")
        run_start = RunStart(
            execution_session_id=ExecutionSessionId("session-001"),
            run_id=run_id,
            request_id=RequestId("req-001"),
            turn_id="turn-001",
            tool_catalog_generation=1,
            input={
                "messages": [
                    {"role": "user", "content": "What is 2 + 2?"}
                ],
                "capability_snapshot": {
                    "tools": ["calculate", "echo"],
                },
            },
        )

        print(f"\n[Runtime] Starting run {run_id}")
        print(f"[User] What is 2 + 2?\n")

        # Start run
        await client.start(run_start)

        # Wait for completion
        print("[Agent] Processing...")
        state = await runtime.wait_idle(run_id)

        print(f"\n[Runtime] Run completed: {state}\n")

        # Show results
        if state == RunState.COMPLETED:
            print("✅ Task completed successfully")
            print("\nIn a real app, you would:")
            print("  1. Fetch final state from database")
            print("  2. Extract terminal_payload")
            print("  3. Show result to user")
        elif state == RunState.FAILED:
            print("❌ Task failed")
        elif state == RunState.CANCELLED:
            print("⚠️  Task cancelled")
        else:
            print(f"⏸️  Task in state: {state}")

    finally:
        # Cleanup
        print("\nCleaning up...")
        await runtime.__aexit__(None, None, None)
        print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
