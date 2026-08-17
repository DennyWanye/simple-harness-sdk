"""Minimal Consumer Example - Main Entry Point

Demonstrates basic Simple Harness SDK integration.
"""

import asyncio
import time
from pathlib import Path

from simple_harness.runtime import build_runtime, RuntimePorts, RunStart
from simple_harness.execution.sqlite import Database
from simple_harness.execution.uow import RunState
from simple_harness.contracts import ExecutionSessionId, RunId, RequestId

from ports.provider import MockLLMProvider
from ports.tools import CalculatorToolExecutor
from ports.auth import AlwaysAllowAuthorization
from ports.context import SqliteContextPort


async def main():
    """Run a simple agent task."""

    print("=== Minimal Consumer Example ===\n")

    # Setup database
    db_path = Path(__file__).parent / "execution.db"
    print(f"Using database: {db_path}")
    db = Database.open(str(db_path))

    # Build runtime
    print("Building runtime...")
    ports = RuntimePorts(
        provider=MockLLMProvider(),
        tool_executor=CalculatorToolExecutor(),
        authorization=AlwaysAllowAuthorization(),
        context=SqliteContextPort(db),
    )

    runtime = await build_runtime(ports).__aenter__()

    try:
        # Create run
        run_id = RunId("run-001")
        run_start = RunStart(
            execution_session_id=ExecutionSessionId("session-001"),
            run_id=run_id,
            request_id=RequestId("req-001"),
            turn_id="turn-001",
            input={
                "messages": [
                    {"role": "user", "content": "What is 2 + 2?"}
                ],
                "capability_snapshot": {
                    "tools": ["calculate", "echo"],
                },
            },
            created_at_unix_ms=int(time.time() * 1000),
        )

        print(f"\n[Runtime] Starting run {run_id}")
        print(f"[User] What is 2 + 2?\n")

        # Start run
        await runtime.start(run_start)

        # Wait for completion
        print("[Agent] Processing...")
        state = await runtime.wait_idle(run_id)

        print(f"\n[Runtime] Run completed: {state}\n")

        # Show results
        if state == RunState.COMPLETED:
            # In real app, fetch from database
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
        db.close()
        print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
