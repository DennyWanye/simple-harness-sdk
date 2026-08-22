# Workflow API Reference

## Overview

Simple Harness SDK includes a native Workflow Engine for durable, multi-step task orchestration. This document covers:

1. **Official Workflows**: Three SDK-provided workflows ready to use
2. **Host-owned Workflows**: How to define your own custom workflows
3. **Workflow Registration**: How to register workflows with the Runtime
4. **Host Services**: Port interfaces workflows depend on

---

## Official Workflows

The SDK provides three official workflows that consumers can use immediately by implementing the required Host Ports.

### 1. `workflow.durable_task`

**Purpose**: Durable multi-step software development tasks with planning, tool execution, HITL, and testing.

**Use cases:**
- Multi-file code changes requiring review
- Tasks requiring test/audit/repair cycles
- Complex operations needing checkpoint/recovery

**Profile key**: `"workflow.durable_task"`

**Required Host Services**: `DurableTaskHostServices`

**Input schema:**
```python
{
    "request": str,              # User's task description
    "run_id": str,              # Run identity
    "session_metadata": dict,   # Session context
    "capability_refs": list,    # Available tools
    "approval_required": bool,  # Whether to require human approval
}
```

**Output schema:**
```python
{
    "status": str,              # "completed" | "failed" | "partial"
    "values": {
        "result": str,          # Final result description
        "artifacts": list,      # Generated artifacts
    }
}
```

**Example:**
```python
from simple_harness.workflows.durable_task import create_initial_state

state = create_initial_state(
    request="Refactor the auth module to use OAuth 2.0",
    run_id="run-durable-123",
    session_metadata={},
    capability_refs=["read_file", "write_file", "run_shell"],
    approval_required=True,
)

# Runtime will execute the workflow through the registered profile
```

---

### 2. `workflow.personal_v1`

**Purpose**: User-defined personal workflows (saved routines, custom automations).

**Use cases:**
- Recurring personal tasks (daily standup report, weekly summary)
- User-customized multi-step routines
- Personalized workflows owned by end-users

**Profile key**: `"workflow.personal_v1"`

**Required Host Services**: `PersonalWorkflowHostServices`

**Input schema:**
```python
{
    "run_id": str,
    "personal_workflow_selection": {
        "candidate_id": str,        # Which workflow to run
        "owner_identity": str,      # Workflow owner
        "workflow_id": str,
        "frozen_graph": dict,       # Workflow definition
    },
    "inputs": dict,                 # User inputs for this run
}
```

**Output schema:**
```python
{
    "status": str,
    "values": {
        "result": dict,             # Workflow output
    }
}
```

**Example:**
```python
from simple_harness.workflows.personal_v1 import (
    create_initial_state,
    PersonalWorkflowSelectionV1,
)

selection = PersonalWorkflowSelectionV1(
    candidate_id="weekly-summary",
    owner_identity="user-123",
    workflow_id="weekly-summary-v1",
    frozen_graph={...},  # Your workflow graph
)

state = create_initial_state(
    run_id="run-personal-456",
    personal_workflow_selection=selection.to_child_payload(),
    inputs={"week_number": 42},
)
```

**Important constraints:**
- Personal workflows can only use `idempotent_read` or `deterministic_reusable` tools
- No arbitrary side effects (for safe recovery)
- Entire workflow executes in one Native checkpoint

---

### 3. `workflow.capability_build`

**Purpose**: Build and install new capabilities (tools/skills) when current catalog is insufficient.

**Use cases:**
- Dynamic capability extension
- Install missing tools on-demand
- Automated capability discovery and setup

**Profile key**: `"workflow.capability_build"`

**Required Host Services**: `CapabilityBuildHostServices`

**Input schema:**
```python
{
    "run_id": str,
    "request": str,                  # What capability is needed
    "search_miss_receipt": str,      # Proof that capability search failed
}
```

**Output schema:**
```python
{
    "status": str,
    "values": {
        "active": bool,              # Whether capability is now active
        "package_ref": str,          # Installed package reference
    }
}
```

**Example:**
```python
from simple_harness.workflows.capability_build import create_initial_state

state = create_initial_state(
    run_id="run-capability-789",
    request="Install PostgreSQL database tool",
    search_miss_receipt="search-miss-20260817",
)
```

**Note**: This workflow is **optional**. If `CapabilityBuildHostServices` is not provided, the Runtime will not offer this profile.

---

## Registering Official Workflows

### `build_official_workflow_registrations(*, generation, transaction_owner, host_services) -> tuple[WorkflowDefinitionRegistration, ...]`

Returns workflow registrations for all official workflows that have their Host Services provided.

**Parameters:**
- `generation` (int): Profile generation number (use `1` for current release)
- `transaction_owner` (object): Transaction owner from your UnitOfWork
- `host_services` (WorkflowHostServices): Host service implementations

**Returns:** Tuple of WorkflowDefinitionRegistration objects (only includes workflows with provided services)

```python
from simple_harness.workflows import build_official_workflow_registrations
from simple_harness.workflow.contracts import (
    WorkflowHostServices,
    DurableTaskHostServices,
    PersonalWorkflowHostServices,
)

# Build host services
host_services = WorkflowHostServices(
    durable_task=DurableTaskHostServices(
        propose=my_planner,
        workspace=my_workspace,
        artifact=my_artifacts,
    ),
    personal_v1=PersonalWorkflowHostServices(
        catalog=my_personal_catalog,
    ),
    # capability_build is optional - omit if not needed
)

# Get registrations (only returns workflows with services provided)
official = build_official_workflow_registrations(
    generation=1,
    transaction_owner=uow.transaction_owner,
    host_services=host_services,
)

# Register with Runtime
# Workflow registrations are composed through the current production/runtime builders.
```

**Conditional registration:**

Only workflows with provided Host Services are included in the result. If `host_services.capability_build` is `None`, the `workflow.capability_build` registration will not be returned.

```python
# Only provide durable_task services
host_services = WorkflowHostServices(
    durable_task=my_durable_services,
    # personal_v1 and capability_build omitted
)

# Result will only contain workflow.durable_task registration
official = build_official_workflow_registrations(
    generation=1,
    transaction_owner=owner,
    host_services=host_services,
)
```

---

## Host-owned Workflows

Consumers can define their own workflows and register them alongside official ones.

### Step 1: Define Your Workflow

```python
from simple_harness.workflow import (
    WorkflowDefinition,
    NodeDefinition,
    Edge,
    START_NODE,
    END_NODE,
    StatePatch,
)

async def research_node(state, context):
    """Custom research logic."""
    query = state["query"]
    
    # Call your host services
    results = await context.ports.my_search.search(query)
    
    return StatePatch({"results": results})

async def analyze_node(state, context):
    """Analyze research results."""
    results = state["results"]
    
    analysis = await context.ports.my_llm.analyze(results)
    
    return StatePatch({"analysis": analysis})

my_workflow = WorkflowDefinition(
    name="my_research",
    version="v1",
    nodes=[
        NodeDefinition(
            name="research",
            handler=research_node,
            on_success=["analyze"],
        ),
        NodeDefinition(
            name="analyze",
            handler=analyze_node,
            on_success=[END_NODE],
        ),
    ],
    edges=[
        Edge(START_NODE, "research"),
    ],
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
        },
    },
)
```

### Step 2: Create a Profile Registration

```python
from simple_harness.workflow import (
    WorkflowProfileRegistration,
    ProfileDescriptor,
    profile_descriptor_fingerprint,
    workflow_manifest_hash,
)

registration = WorkflowProfileRegistration(
    profile=ProfileDescriptor(
        key="workflow.my_research",
        description="Custom research workflow with analysis",
        generation=1,
    ),
    definition=my_workflow,
    expected_manifest_hash=workflow_manifest_hash(my_workflow),
)
```

### Step 3: Register with Runtime

```python
from simple_harness.workflows import build_official_workflow_registrations

# Combine official + your workflows
all_workflows = [
    *build_official_workflow_registrations(),
    registration,  # Your custom workflow
]

ports = RuntimePorts(
    ...
    workflow_registrations=all_workflows,
)
```

### Step 4: Agent Can Now Use It

The Agent will see your workflow in the catalog:

```python
# Agent can call via workflow_spawn:
{
    "name": "workflow_spawn",
    "arguments": {
        "profile_key": "workflow.my_research",
        "inputs": {"query": "Latest AI research 2026"}
    }
}
```

---

## Host Services (Ports)

Workflows call back into your application through Host Services ports.

### DurableTaskHostServices

Required for `workflow.durable_task`:

```python
class DurableTaskHostServices(Protocol):
    async def propose(self, state: dict) -> dict:
        """Generate a task plan."""
        ...
    
    async def execute_tools(self, calls: list, **kwargs) -> dict:
        """Execute tool calls."""
        ...
    
    async def run_tests(self, state: dict) -> dict:
        """Run tests on changes."""
        ...
    
    async def audit(self, audit: dict, state: dict) -> dict:
        """Audit task completion."""
        ...
    
    async def check_completion_evidence(self, state: dict, outcome: dict) -> bool:
        """Verify task is complete."""
        ...
```

### PersonalWorkflowHostServices

Required for `workflow.personal_v1`:

```python
class PersonalWorkflowHostServices(Protocol):
    async def execute(self, **values) -> dict:
        """Execute a personal workflow node."""
        ...
```

### CapabilityBuildHostServices

Required for `workflow.capability_build`:

```python
class CapabilityBuildHostServices(Protocol):
    async def search(self, **values) -> dict:
        """Search for capabilities."""
        ...
    
    async def authorize_source(self, **values) -> dict:
        """Check if source is allowed."""
        ...
    
    async def build(self, **values) -> dict:
        """Build capability package."""
        ...
    
    async def store(self, **values) -> dict:
        """Store built package."""
        ...
    
    async def activate(self, **values) -> dict:
        """Activate installed capability."""
        ...
    
    async def authorize_build(self, **values) -> dict:
        """Authorize build operation."""
        ...
```

**See:** `docs/api/ports.md` for detailed method signatures.

---

## Workflow Execution Flow

1. **Agent decides** to use a workflow (via `workflow_spawn` tool)
2. **Runtime validates** the profile exists and is authorized
3. **Runtime creates** a child Run with frozen workflow state
4. **Workflow Driver** executes the graph node-by-node
5. **Checkpoints** are saved after each node
6. **Crash recovery**: Can resume from last checkpoint
7. **Terminal state**: Workflow returns result to parent Run

---

## Checkpoint and Recovery

Workflows automatically checkpoint after each node:

```python
# If process crashes here:
async def my_node(state, context):
    await expensive_operation()  # ← Crash happens
    return StatePatch({...})

# On restart, the workflow resumes from the last checkpoint
# expensive_operation() is NOT replayed
```

**Recovery guarantees:**
- Completed nodes are never re-executed
- In-progress nodes may be marked as `unknown` (requires reconciliation)
- State is restored from the last committed checkpoint

---

## Testing Your Workflow

The SDK provides conformance tests for workflows:

```python
# Test your host-owned workflow
from simple_harness.testing import ConformanceSuite

# This will verify:
# - Workflow can be registered
# - Workflow can execute to completion
# - Checkpoint/recovery works
# - Host Services are called correctly
```

**See:** `docs/conformance.md` for `workflow.host_owned` test case.

---

## Example: Complete Custom Workflow

```python
from simple_harness.workflow import (
    WorkflowDefinition,
    NodeDefinition,
    Edge,
    START_NODE,
    END_NODE,
    StatePatch,
    WorkflowProfileRegistration,
    ProfileDescriptor,
    workflow_manifest_hash,
)

# Define nodes
async def fetch_data(state, context):
    data = await context.ports.http_client.get(state["url"])
    return StatePatch({"data": data})

async def process_data(state, context):
    result = await context.ports.processor.process(state["data"])
    return StatePatch({"result": result})

# Build workflow
workflow_def = WorkflowDefinition(
    name="data_pipeline",
    version="v1",
    nodes=[
        NodeDefinition("fetch", fetch_data, on_success=["process"]),
        NodeDefinition("process", process_data, on_success=[END_NODE]),
    ],
    edges=[Edge(START_NODE, "fetch")],
    input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"result": {"type": "object"}}},
)

# Register
registration = WorkflowProfileRegistration(
    profile=ProfileDescriptor(
        key="workflow.data_pipeline",
        description="ETL data pipeline",
        generation=1,
    ),
    definition=workflow_def,
    expected_manifest_hash=workflow_manifest_hash(workflow_def),
)

# Use in RuntimePorts
ports = RuntimePorts(
    ...
    workflow_registrations=[
        *build_official_workflow_registrations(),
        registration,
    ],
)
```

---

## See Also

- [Runtime API](runtime.md) - How to build and manage Runtime
- [Ports API](ports.md) - Host Services interface details
- [Conformance](../conformance.md) - Workflow test cases
- [Integration Guide](../integration-guide.md) - Step-by-step workflow integration
