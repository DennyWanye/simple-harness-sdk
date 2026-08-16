# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public durable-workflow API.

Imports are resolved lazily so the runtime composition root can import workflow
implementations without creating a ``runtime.kernel``/``workflow.runner`` cycle.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "SDK_DEPENDENCY_LOCK_HASH": (
        ".dependency_lock",
        "SDK_DEPENDENCY_LOCK_HASH",
    ),
    # contracts
    "CapabilityBuildHostServices": (".contracts", "CapabilityBuildHostServices"),
    "ChannelSpec": (".contracts", "ChannelSpec"),
    "DurableTaskHostServices": (".contracts", "DurableTaskHostServices"),
    "JsonType": (".contracts", "JsonType"),
    "PersonalWorkflowHostServices": (".contracts", "PersonalWorkflowHostServices"),
    "PureRouteContext": (".contracts", "PureRouteContext"),
    "ReducerKind": (".contracts", "ReducerKind"),
    "RetryPolicy": (".contracts", "RetryPolicy"),
    "StatePatch": (".contracts", "StatePatch"),
    "WorkflowContext": (".contracts", "WorkflowContext"),
    "WorkflowHostServices": (".contracts", "WorkflowHostServices"),
    "WorkflowRunStatus": (".contracts", "WorkflowRunStatus"),
    # definitions
    "CompiledWorkflow": (".definition", "CompiledWorkflow"),
    "ConditionalEdge": (".definition", "ConditionalEdge"),
    "Edge": (".definition", "Edge"),
    "END_NODE": (".definition", "END_NODE"),
    "NodeDefinition": (".definition", "NodeDefinition"),
    "WorkflowDefinition": (".definition", "WorkflowDefinition"),
    "WorkflowDefinitionRegistration": (
        ".definition",
        "WorkflowDefinitionRegistration",
    ),
    "compile_workflow": (".definition", "compile_workflow"),
    "compile_workflow_registration": (
        ".definition",
        "compile_workflow_registration",
    ),
    "workflow_manifest_hash": (".definition", "workflow_manifest_hash"),
    "validate_dependency_lock_hash": (
        ".dependency_lock",
        "validate_dependency_lock_hash",
    ),
    # durable runner and transaction adapters
    "CheckpointExecutionAdapter": (
        ".execution_ports",
        "CheckpointExecutionAdapter",
    ),
    "WorkflowExecutionPorts": (".execution_ports", "WorkflowExecutionPorts"),
    "WorkflowRegistry": (".runner", "WorkflowRegistry"),
    "WorkflowRunner": (".runner", "WorkflowRunner"),
    # public catalog registration types
    "StartInputSchema": (
        "simple_harness.runtime.orchestration",
        "StartInputSchema",
    ),
    "WorkflowProfileRegistration": (
        "simple_harness.runtime.orchestration",
        "WorkflowProfileRegistration",
    ),
    "ProfileDescriptor": (
        "simple_harness.runtime.profiles",
        "ProfileDescriptor",
    ),
    "profile_descriptor_fingerprint": (
        "simple_harness.runtime.profiles",
        "profile_descriptor_fingerprint",
    ),
}

__all__ = tuple(sorted(_EXPORTS))


def __getattr__(name: str) -> object:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = (
        import_module(module_name, __name__)
        if module_name.startswith(".")
        else import_module(module_name)
    )
    value = getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
