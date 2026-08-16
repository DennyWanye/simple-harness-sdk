# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from simple_harness.contracts import canonical_json
from simple_harness.runtime.orchestration import (
    StartInputSchema,
    WorkflowProfileRegistration,
)
from simple_harness.runtime.profiles import (
    ProfileDescriptor,
    profile_descriptor_fingerprint,
)


class _Proposal:
    async def propose(self, state):  # type: ignore[no-untyped-def]
        return state


class _Workspace:
    async def execute_tools(self, calls, **kwargs):  # type: ignore[no-untyped-def]
        del calls, kwargs
        return {}


async def _done(state, _context):
    from simple_harness.workflow import StatePatch

    return StatePatch({"values": {**state["values"], "done": True}})


def _definition():
    from simple_harness.workflow import (
        END_NODE,
        ChannelSpec,
        Edge,
        JsonType,
        NodeDefinition,
        ReducerKind,
        WorkflowDefinition,
    )

    return WorkflowDefinition(
        name="public_contract",
        version="v1",
        state_schema_version=1,
        entry_node="done",
        nodes=(NodeDefinition("done", _done),),
        channels={
            "values": ChannelSpec(
                JsonType.OBJECT,
                ReducerKind.SINGLE_WRITER,
                frozenset({"done"}),
            )
        },
        edges=(Edge("done", END_NODE),),
        recursion_limit=4,
        max_supersteps=2,
    )


def _profile():
    schema = {
        "type": "object",
        "properties": {"request": {"type": "string", "maxLength": 128}},
        "required": ["request"],
        "additionalProperties": False,
    }
    schema_ref = "sdk://workflow/public-contract/v1/start"
    descriptor = ProfileDescriptor(
        key="workflow.public_contract",
        description="Public contract fixture.",
        use_when="Testing public workflow composition.",
        avoid_when="Outside tests.",
        input_schema_ref=schema_ref,
        generation=1,
        fingerprint=profile_descriptor_fingerprint(
            "workflow.public_contract",
            "Public contract fixture.",
            "Testing public workflow composition.",
            "Outside tests.",
            schema_ref,
            1,
        ),
    )
    return WorkflowProfileRegistration(
        descriptor=descriptor,
        workflow_name="public_contract",
        workflow_version="v1",
        start_input_schema=StartInputSchema(
            schema_ref=schema_ref,
            canonical_schema=schema,
            schema_hash=hashlib.sha256(canonical_json(schema).encode()).hexdigest(),
        ),
    )


def test_public_workflow_namespace_is_lazy_and_complete() -> None:
    import simple_harness.workflow as workflow

    assert "WorkflowRunner" in dir(workflow)
    assert workflow.WorkflowDefinition.__name__ == "WorkflowDefinition"
    assert workflow.WorkflowHostServices.__name__ == "WorkflowHostServices"
    assert callable(workflow.workflow_interrupt)
    assert hasattr(workflow.WorkflowRunner, "resolve_and_resume")
    assert workflow.WorkflowDefinitionRegistration.__name__ == (
        "WorkflowDefinitionRegistration"
    )


def test_definition_registration_checks_owner_and_all_fingerprints() -> None:
    from simple_harness.workflow import (
        WorkflowDefinitionRegistration,
        compile_workflow,
        compile_workflow_registration,
        workflow_manifest_hash,
    )

    owner = object()
    compiled = compile_workflow(_definition())
    registration = WorkflowDefinitionRegistration(
        profile=_profile(),
        definition=_definition(),
        dependency_lock_hash=compiled.manifest.dependency_lock_hash,
        expected_manifest_hash=workflow_manifest_hash(compiled.manifest),
        expected_implementation_fingerprint=(
            compiled.manifest.implementation_bundle_hash
        ),
        transaction_owner=owner,
    )
    assert compile_workflow_registration(
        registration, transaction_owner=owner
    ).manifest == compiled.manifest
    with pytest.raises(ValueError, match="transaction owner"):
        compile_workflow_registration(registration, transaction_owner=object())


def test_host_services_are_frozen_and_unknown_groups_fail_closed() -> None:
    from simple_harness.workflow import (
        DurableTaskHostServices,
        WorkflowHostServices,
    )

    with pytest.raises(TypeError, match="proposal must implement"):
        DurableTaskHostServices(proposal=object(), workspace=_Workspace())  # type: ignore[arg-type]
    group = DurableTaskHostServices(proposal=_Proposal(), workspace=_Workspace())
    services = WorkflowHostServices(durable_task=group)
    assert services.ports_for("workflow.durable_task")["proposal"] is group.proposal
    with pytest.raises(FrozenInstanceError):
        services.durable_task = None  # type: ignore[misc]
    with pytest.raises(KeyError, match="unknown workflow profile"):
        services.ports_for("workflow.not_registered")


def test_registry_profile_duplicate_is_idempotent_only_for_same_fingerprint() -> None:
    from dataclasses import replace

    from simple_harness.workflow import (
        WorkflowDefinitionRegistration,
        WorkflowRegistry,
        compile_workflow,
        workflow_manifest_hash,
    )

    owner = object()
    compiled = compile_workflow(_definition())
    registration = WorkflowDefinitionRegistration(
        profile=_profile(),
        definition=_definition(),
        dependency_lock_hash=compiled.manifest.dependency_lock_hash,
        expected_manifest_hash=workflow_manifest_hash(compiled.manifest),
        expected_implementation_fingerprint=compiled.manifest.implementation_bundle_hash,
        transaction_owner=owner,
    )
    registry = WorkflowRegistry(transaction_owner=owner)
    first = registry.register_definition(registration)
    assert registry.register_definition(registration) is first
    with pytest.raises(ValueError, match="profile key already registered"):
        registry.register_definition(
            replace(registration, expected_manifest_hash="0" * 64)
        )
