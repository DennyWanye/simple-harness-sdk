# AIPhone SDK Integration Handoff

> Historical 0.1.1 design record. AIPhone has not integrated or tested Harness SDK 0.3.0 in the
> current program. For the interface-ready Agent Memory v1 contract, use
> [`../api/contracts.md`](../api/contracts.md); the query/write examples below are retired and
> must not be used for a new integration.

**Date:** 2026-08-17  
**SDK Version:** v0.1.1  
**Target:** AIPhone Mobile Application

---

## Overview

This document provides everything needed to integrate Simple Harness SDK v0.1.1 into the AIPhone mobile application runtime.

**What AIPhone Gets:**
- Fault-tolerant workflow execution engine
- Three official workflow profiles (durable_task, personal_v1, capability_build)
- Conformance testing framework for implementation validation
- Protocol-first design for cross-platform deployment
- Memory Port interfaces for future Memory SDK integration

**What AIPhone Must Provide:**
- Mobile-compatible persistence layer (SQLite)
- LLM provider adapter (API calls from mobile runtime)
- Tool implementations for mobile context
- Platform-specific admission control
- Optional: Memory integration (long-term and working memory)

---

## Installation

### 1. Download from Private GitHub Release

**Release URL:** https://github.com/DennyWanye/simple-harness-sdk/releases/tag/v0.1.1

Download artifacts:
```bash
# Wheel (pure Python, works on any platform)
curl -LO https://github.com/DennyWanye/simple-harness-sdk/releases/download/v0.1.1/simple_harness_sdk-0.1.1-py3-none-any.whl

# Checksum file
curl -LO https://github.com/DennyWanye/simple-harness-sdk/releases/download/v0.1.1/SHA256SUMS

# Build metadata
curl -LO https://github.com/DennyWanye/simple-harness-sdk/releases/download/v0.1.1/BUILD_INFO.txt
```

**Authentication:** Requires GitHub access token with read access to private repository.

### 2. Verify Integrity

```bash
# Linux/macOS
sha256sum --ignore-missing -c SHA256SUMS

# Verify specific file
sha256sum simple_harness_sdk-0.1.1-py3-none-any.whl
# Compare with SHA256SUMS content
```

### 3. Install Wheel

```bash
# Basic installation
pip install simple_harness_sdk-0.1.1-py3-none-any.whl

# With testing extras (for conformance validation)
pip install "simple_harness_sdk-0.1.1-py3-none-any.whl[testing]"

# Verify installation
python -c "import simple_harness; print(simple_harness.__version__)"
# Expected output: 0.1.1
```

---

## Platform Compatibility

### Verified Platforms

SDK v0.1.1 tested on:
- **Linux x64** - Ubuntu 24.04, Python 3.11
- **macOS ARM64** - macOS 14, Python 3.11  
- **Windows x64** - Windows Server 2022, Python 3.11

### Mobile Platform Considerations

**iOS:**
- Pure Python wheel works on iOS with Python 3.11+ runtime
- SQLite available via system libraries
- No native dependencies beyond standard library

**Android:**
- Compatible with Kivy, BeeWare, or other Python-on-Android frameworks
- Requires Python 3.11+ runtime (e.g., via Chaquopy, python-for-android)
- aiosqlite works with Android SQLite

**Key Constraints:**
- Python 3.11+ required (no 3.10 support)
- SQLite required for context persistence
- Async-first API (all runtime calls are `async def`)

---

## Public API Reference

### Core Imports

```python
from simple_harness import (
    # Runtime construction
    build_runtime,
    Runtime,
    RuntimeProfile,
    RuntimeDriver,
    RuntimePorts,
    RuntimeServices,
    RuntimeUnitOfWork,
    ROOT_PROFILE_KEY,
    
    # JSON contracts
    JsonValue,
    canonical_json,
    freeze_json,
    fingerprint_json,
    
    # Identity types
    ExecutionSessionId,
    RunId,
    RequestId,
    
    # Messages
    Message,
    MessageRole,
    
    # Context and admission
    ContextPort,
    SqliteContextPort,
    AdmissionPort,
    AdmissionVerdict,
    AllowAllAdmission,
)
```

### Minimal Runtime Setup

```python
import asyncio
from pathlib import Path
from simple_harness import (
    build_runtime,
    RuntimeProfile,
    RuntimePorts,
    RuntimeDriver,
    RuntimeUnitOfWork,
    SqliteContextPort,
    AllowAllAdmission,
    ROOT_PROFILE_KEY,
)

# 1. Implement RuntimePorts
class MobileRuntimePorts:
    def __init__(self, db_path: Path):
        self.context = SqliteContextPort(db_path)
        self.admission = AllowAllAdmission()
    
    async def get_provider(self, run_id, provider_key):
        """Return LLM provider adapter."""
        # TODO: Implement mobile provider adapter
        raise NotImplementedError()
    
    async def execute_tool_batch(self, run_id, tools):
        """Execute tool calls."""
        # TODO: Implement mobile tool execution
        raise NotImplementedError()
    
    async def deliver_terminal(self, run_id, message):
        """Deliver final result to user."""
        # TODO: Implement mobile UI delivery
        raise NotImplementedError()

# 2. Define workflow profile
mobile_profile = RuntimeProfile(
    name="aiphone_personal",
    driver_key="react",
    max_iterations=40,
    admission_policy="allow_all",
)

# 3. Build runtime
async def create_mobile_runtime():
    ports = MobileRuntimePorts(
        db_path=Path("/data/aiphone/workflow.db")
    )
    
    runtime = await build_runtime(
        ports=ports,
        profiles={ROOT_PROFILE_KEY: mobile_profile},
        drivers={},  # Use built-in ReAct driver
    )
    
    return runtime

# 4. Use runtime
async def main():
    runtime = await create_mobile_runtime()
    
    # Start a new workflow run
    run_id = await runtime.start_run(
        profile_key=ROOT_PROFILE_KEY,
        initial_message="Search for nearby restaurants"
    )
    
    # Runtime handles execution asynchronously
    # Results delivered via ports.deliver_terminal()

asyncio.run(main())
```

---

## Memory Integration (Optional)

SDK v0.1.1 introduces Memory Port interfaces for future Memory SDK integration.

### Memory Port Interfaces

**MemoryQueryPort** - Read-only access to long-term memory:

```python
from simple_harness.runtime import MemoryQueryPort

class AIPhoneMemoryQuery:
    """Read from mobile local memory database."""
    
    def __init__(self, mobile_db):
        self.db = mobile_db
    
    async def recall_readonly(self, query, limit, scope):
        """Query user's conversation history and saved knowledge.
        
        Args:
            query: Natural language query (e.g., "what did user say about restaurants?")
            limit: Maximum number of results
            scope: Memory scope (e.g., "user:123", "session:abc")
        
        Returns:
            List of memory entries as JSON-safe dicts
        """
        # Vector search in mobile SQLite
        results = await self.db.vector_search(
            table="conversation_memory",
            query_text=query,
            limit=limit,
            user_id=scope.split(":")[1],
        )
        
        return [
            {
                "content": r["text"],
                "timestamp": r["created_at"],
                "relevance": r["similarity_score"],
            }
            for r in results
        ]
```

**MemoryWritePort** - Session-scoped working memory:

```python
from simple_harness.runtime import MemoryWritePort

class AIPhoneMemoryWrite:
    """Write to mobile session memory (todos, notes)."""
    
    def __init__(self, mobile_db):
        self.db = mobile_db
    
    async def replace_session_todos(self, session_id, items):
        """Replace working memory list for a session.
        
        Args:
            session_id: Execution session ID
            items: New todo list (JSON-safe dicts)
        """
        async with self.db.transaction() as tx:
            # Atomic replace
            await tx.execute(
                "DELETE FROM session_todos WHERE session_id = ?",
                (session_id,)
            )
            
            for idx, item in enumerate(items):
                await tx.execute(
                    "INSERT INTO session_todos (session_id, idx, content) VALUES (?, ?, ?)",
                    (session_id, idx, item.get("content", ""))
                )
```

### Integrating Memory Ports

```python
# In your runtime setup
ports = RuntimePorts(
    provider=mobile_provider,
    tool_executor=mobile_tools,
    authorization=mobile_auth,
    context=mobile_context,
    
    # Optional: Add memory
    memory_query=AIPhoneMemoryQuery(mobile_db),
    memory_write=AIPhoneMemoryWrite(mobile_db),
)
```

**Benefits:**
- Agent can recall past conversations
- Agent can maintain working notes across turns
- Future-proof for standalone Memory SDK

**Mobile Constraints:**
- Keep memory DB size reasonable (< 100MB for smooth operation)
- Index conversation history for fast vector search
- Consider background sync to cloud backup

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    AIPhone Mobile App                       │
├─────────────────────────────────────────────────────────────┤
│  UI Layer                                                   │
│  • Chat interface                                           │
│  • Permission dialogs                                       │
│  • Notification display                                     │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│              AIPhone Adapter Layer (Your Code)              │
├─────────────────────────────────────────────────────────────┤
│  RuntimePorts Implementation                                │
│  • MobileProvider (LLM API client)                         │
│  • MobileToolExecutor (contacts, calendar, location)      │
│  • MobileAuthorization (user permissions)                 │
│  • MobileMemoryQuery (conversation history)               │
│  • MobileMemoryWrite (session notes)                      │
│                                                             │
│  Mobile-Specific Services                                  │
│  • SQLite persistence (conversations, memory, todos)      │
│  • Background task scheduler                               │
│  • Network adapter (offline handling)                      │
└────────────────────┬────────────────────────────────────────┘
                     │ Ports Interface
                     ↓
┌─────────────────────────────────────────────────────────────┐
│           Simple Harness SDK (v0.1.1 Wheel)                 │
├─────────────────────────────────────────────────────────────┤
│  Runtime Engine                                             │
│  • RunKernel (state management)                            │
│  • ReActDriver (Agent loop)                                │
│  • WorkflowDriver (multi-step orchestration)              │
│  • Recovery & reconciliation                               │
│                                                             │
│  Official Workflows                                         │
│  • workflow.durable_task                                   │
│  • workflow.personal_v1 (user-defined routines)           │
│  • workflow.capability_build (optional)                    │
│                                                             │
│  Persistence Layer                                          │
│  • SQLite execution state                                  │
│  • Checkpoint storage                                       │
│  • Effect ledger (idempotency)                             │
└─────────────────────────────────────────────────────────────┘
```

**Data Flow:**

1. **User Input** → AIPhone UI → Adapter → SDK Runtime
2. **LLM Call** → SDK → MobileProvider → Cloud API → Response
3. **Tool Execution** → SDK → MobileToolExecutor → Mobile OS
4. **Memory Recall** → SDK → MobileMemoryQuery → Local SQLite
5. **Result** → SDK → Adapter.deliver_terminal() → UI Update

---

## Implementation Checklist

### Phase 1: Basic Runtime (Week 1-2)

- [ ] Install SDK wheel in mobile build environment
- [ ] Implement `RuntimePorts` interface:
  - [ ] `ContextPort` - Use `SqliteContextPort` with mobile DB path
  - [ ] `AdmissionPort` - Start with `AllowAllAdmission`
  - [ ] `get_provider()` - Adapter for mobile LLM API client
  - [ ] `execute_tool_batch()` - Stub that logs tool calls
  - [ ] `deliver_terminal()` - Bridge to mobile UI notification
- [ ] Define one `RuntimeProfile` (e.g., "aiphone_personal")
- [ ] Call `build_runtime()` successfully
- [ ] Run conformance tests (see below)

### Phase 2: Tool Integration (Week 3-4)

- [ ] Implement mobile tool registry
- [ ] Register mobile-specific tools:
  - [ ] `contacts_search` - Query mobile contact list
  - [ ] `calendar_create` - Add calendar event
  - [ ] `location_get` - Current GPS location
  - [ ] `notification_send` - System notification
- [ ] Wire `execute_tool_batch()` to tool registry
- [ ] Test tool execution via SDK runtime

### Phase 3: Production Hardening (Week 5-6)

- [ ] Implement real admission policy (user budget, permissions)
- [ ] Add error handling and recovery
- [ ] Persistence strategy (SQLite on device storage)
- [ ] Logging and observability
- [ ] Battery/network awareness
- [ ] Run full conformance suite

### Phase 4: Memory Integration (Optional, Week 7-8)

- [ ] Design mobile memory schema (conversations, entities, notes)
- [ ] Implement `MemoryQueryPort`:
  - [ ] Vector search setup (e.g., sqlite-vec extension)
  - [ ] Query conversation history by semantic similarity
  - [ ] Scope memory by user/session
- [ ] Implement `MemoryWritePort`:
  - [ ] Session todos storage
  - [ ] Atomic replace logic
- [ ] Wire Memory Ports to RuntimePorts
- [ ] Test memory recall in multi-turn conversations
- [ ] Optimize for mobile (index size, query speed)

**Memory Constraints for Mobile:**
- Keep total memory DB < 100MB for app store guidelines
- Limit vector search to top 50 results max
- Background sync to cloud (don't block main thread)
- Periodic cleanup of old conversations (>6 months)

---

## Conformance Testing

SDK includes conformance tests to validate your implementation.

### CLI Interface

```bash
# Run all conformance suites
python -m simple_harness.testing \
  --host aiphone.sdk_adapter:build_host \
  --suite provider,tool,runtime,workflow \
  --json conformance-report.json

# Run specific suite
python -m simple_harness.testing \
  --host aiphone.sdk_adapter:build_host \
  --suite runtime

# Check version
python -m simple_harness.testing --version
```

### Pytest Plugin

```python
# conftest.py
from aiphone.sdk_adapter import build_host

def pytest_configure(config):
    config.option.simple_harness_host = "aiphone.sdk_adapter:build_host"

# Run with pytest
# pytest tests/ --simple-harness-host aiphone.sdk_adapter:build_host
```

### Host Factory Pattern

Implement a host factory that returns your configured runtime:

```python
# aiphone/sdk_adapter.py

from simple_harness import Runtime, RuntimeProfile, RuntimePorts

async def build_host() -> dict:
    """Conformance test host factory.
    
    Returns:
        {
            "runtime": Runtime instance,
            "profiles": dict[str, RuntimeProfile],
            "ports": RuntimePorts instance,
        }
    """
    ports = MobileRuntimePorts(db_path=":memory:")
    profiles = {ROOT_PROFILE_KEY: mobile_profile}
    runtime = await build_runtime(ports=ports, profiles=profiles)
    
    return {
        "runtime": runtime,
        "profiles": profiles,
        "ports": ports,
    }
```

---

## Workflow Profiles

SDK ships with three official profiles. AIPhone can use these directly or define custom profiles.

### 1. durable_task (Base Profile)

General-purpose task execution with fault tolerance.

```python
from simple_harness.workflows.durable_task import DURABLE_TASK_PROFILE

runtime = await build_runtime(
    ports=ports,
    profiles={ROOT_PROFILE_KEY: DURABLE_TASK_PROFILE},
)
```

**Characteristics:**
- ReAct agent loop
- Unlimited iterations (bounded by admission)
- Supports tool execution, continuation, terminal delivery

### 2. personal_v1 (Personal Workflow)

Profile for personal productivity with budget constraints.

```python
from simple_harness.workflows.personal_v1 import PERSONAL_V1_PROFILE

runtime = await build_runtime(
    ports=ports,
    profiles={ROOT_PROFILE_KEY: PERSONAL_V1_PROFILE},
)
```

**Characteristics:**
- Default proposal budget: 40 turns
- Default fix budget: 3 repair attempts
- Optimized for single-user mobile context

### 3. capability_build (Builder Profile)

Profile for capability generation workflows.

```python
from simple_harness.workflows.capability_build import CAPABILITY_BUILD_PROFILE

runtime = await build_runtime(
    ports=ports,
    profiles={ROOT_PROFILE_KEY: CAPABILITY_BUILD_PROFILE},
)
```

**Characteristics:**
- Constrained budgets for proposal/fix
- Builder contracts for capability lifecycle
- Use case: generating new workflow capabilities

---

## Known Limitations

### v0.1.0 Constraints

1. **SQLite Required**
   - Context persistence requires SQLite
   - Mobile must provide writable SQLite database
   - In-memory SQLite (`:memory:`) works for testing

2. **Python 3.11+ Only**
   - No support for Python 3.10 or earlier
   - Mobile runtime must provide Python 3.11+ environment

3. **Async API**
   - All runtime methods are `async def`
   - Requires asyncio event loop
   - No synchronous wrappers provided

4. **No Network Fallback**
   - SDK does not make network calls directly
   - Mobile must implement provider API calls
   - SDK handles orchestration only

### Mobile-Specific Considerations

1. **Battery Impact**
   - Long-running workflows drain battery
   - Consider admission policies that limit iterations
   - Pause/resume support via context persistence

2. **Network Reliability**
   - LLM provider calls may fail on poor network
   - Implement retry logic in provider adapter
   - SDK resumes from last checkpoint on recovery

3. **Storage Constraints**
   - SQLite context DB grows with workflow history
   - Implement cleanup policy for old runs
   - Context API supports pruning completed runs

---

## Support and Resources

### Documentation

- **API Reference:** See source docstrings in `src/simple_harness/`
- **Architecture:** `plans/2026-08-13-simple-harness-sdk/` in SDK repo
- **Release Notes:** `docs/release/v0.1.0.md` in SDK repo

### Contact

For integration questions or issues:
- Review source code in SDK repository
- Check conformance test failures for validation errors
- Contact: DennyWanye (project maintainer)

### Private Repository Access

SDK repository is private: `git@github.com:DennyWanye/simple-harness-sdk.git`

Authorized consumers receive:
- GitHub repository read access
- Private Release artifact access
- Integration support

**Do not redistribute SDK artifacts or documentation publicly.**

---

## Example: Complete Mobile Integration

```python
# aiphone/sdk_runtime.py

import asyncio
from pathlib import Path
from typing import Any

from simple_harness import (
    build_runtime,
    RuntimeProfile,
    RuntimePorts,
    RuntimeUnitOfWork,
    ContextPort,
    SqliteContextPort,
    AdmissionPort,
    AdmissionVerdict,
    AllowAllAdmission,
    RunId,
    Message,
    ROOT_PROFILE_KEY,
)

class AIPhoneRuntimePorts:
    """Mobile runtime ports implementation."""
    
    def __init__(self, db_path: Path, llm_client, tool_registry, ui_callback):
        self.context = SqliteContextPort(db_path)
        self.admission = AllowAllAdmission()
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.ui_callback = ui_callback
    
    async def get_provider(self, run_id: RunId, provider_key: str):
        """Return mobile LLM provider adapter."""
        return MobileLLMProvider(self.llm_client, provider_key)
    
    async def execute_tool_batch(self, run_id: RunId, tools: list[dict]) -> list[dict]:
        """Execute mobile tools."""
        results = []
        for tool_call in tools:
            result = await self.tool_registry.execute(
                tool_name=tool_call["name"],
                arguments=tool_call["arguments"],
            )
            results.append(result)
        return results
    
    async def deliver_terminal(self, run_id: RunId, message: Message) -> None:
        """Deliver result to mobile UI."""
        await self.ui_callback.show_result(message.content)


class MobileLLMProvider:
    """Mobile LLM provider adapter."""
    
    def __init__(self, client, provider_key: str):
        self.client = client
        self.provider_key = provider_key
    
    async def call(self, messages: list[Message], tools: list[dict]) -> dict:
        """Call mobile LLM API."""
        response = await self.client.chat_completion(
            model=self.provider_key,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            tools=tools,
        )
        return response


async def create_aiphone_runtime(
    db_path: Path,
    llm_client,
    tool_registry,
    ui_callback,
):
    """Create AIPhone SDK runtime."""
    
    ports = AIPhoneRuntimePorts(
        db_path=db_path,
        llm_client=llm_client,
        tool_registry=tool_registry,
        ui_callback=ui_callback,
    )
    
    profile = RuntimeProfile(
        name="aiphone_assistant",
        driver_key="react",
        max_iterations=30,
        admission_policy="allow_all",
    )
    
    runtime = await build_runtime(
        ports=ports,
        profiles={ROOT_PROFILE_KEY: profile},
        drivers={},  # Use built-in ReAct driver
    )
    
    return runtime


# Usage from mobile app
async def handle_user_request(user_input: str):
    runtime = await create_aiphone_runtime(
        db_path=Path("/data/aiphone/sdk_context.db"),
        llm_client=mobile_llm_client,
        tool_registry=mobile_tools,
        ui_callback=mobile_ui,
    )
    
    run_id = await runtime.start_run(
        profile_key=ROOT_PROFILE_KEY,
        initial_message=user_input,
    )
    
    # Runtime executes asynchronously
    # Result delivered via ports.deliver_terminal()
```

---

## Next Steps

1. **Download SDK wheel** from GitHub Release v0.1.0
2. **Verify integrity** with SHA256SUMS
3. **Install in mobile build environment**
4. **Implement minimal RuntimePorts** (Phase 1 checklist)
5. **Run conformance tests** to validate implementation
6. **Integrate tool execution** (Phase 2 checklist)
7. **Production hardening** (Phase 3 checklist)

For questions or clarifications, contact the SDK maintainer with:
- AIPhone architecture overview
- Specific integration blockers
- Conformance test failures (include JSON report)

**Welcome to Simple Harness SDK v0.1.0!**
