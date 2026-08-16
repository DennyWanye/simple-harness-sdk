# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations


class _Ports:
    async def propose(self, state):  # type: ignore[no-untyped-def]
        return state

    async def execute_tools(self, calls, **kwargs):  # type: ignore[no-untyped-def]
        del calls, kwargs
        return {}

    async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return {}

    async def search(self, **kwargs):  # type: ignore[no-untyped-def]
        return kwargs

    async def authorize_source(self, **kwargs):  # type: ignore[no-untyped-def]
        return kwargs

    async def build(self, **kwargs):  # type: ignore[no-untyped-def]
        return kwargs

    async def store(self, **kwargs):  # type: ignore[no-untyped-def]
        return kwargs

    async def activate(self, **kwargs):  # type: ignore[no-untyped-def]
        return kwargs

    async def authorize_build(self, **kwargs):  # type: ignore[no-untyped-def]
        return kwargs


def test_optional_service_group_only_controls_its_own_profile() -> None:
    from simple_harness.workflow import (
        DurableTaskHostServices,
        PersonalWorkflowHostServices,
        WorkflowHostServices,
    )
    from simple_harness.workflows import build_official_workflow_registrations

    owner = object()
    ports = _Ports()
    services = WorkflowHostServices(
        durable_task=DurableTaskHostServices(
            proposal=ports, workspace=ports
        ),
        personal_v1=PersonalWorkflowHostServices(runtime=ports),
    )
    registrations = build_official_workflow_registrations(
        generation=1,
        transaction_owner=owner,
        host_services=services,
    )
    assert {item.profile.descriptor.key for item in registrations} == {
        "workflow.durable_task",
        "workflow.personal_v1",
    }


def test_all_ready_official_profiles_are_default_on() -> None:
    from simple_harness.workflow import (
        CapabilityBuildHostServices,
        DurableTaskHostServices,
        PersonalWorkflowHostServices,
        WorkflowHostServices,
        WorkflowRegistry,
    )
    from simple_harness.workflows import build_official_workflow_registrations

    owner = object()
    ports = _Ports()
    services = WorkflowHostServices(
        durable_task=DurableTaskHostServices(
            proposal=ports, workspace=ports
        ),
        personal_v1=PersonalWorkflowHostServices(runtime=ports),
        capability_build=CapabilityBuildHostServices(
            proposal=ports,
            workspace=ports,
            search=ports,
            source_policy=ports,
            isolated_build=ports,
            package_store=ports,
            activate=ports,
            authorization=ports,
        ),
    )
    registrations = build_official_workflow_registrations(
        generation=1,
        transaction_owner=owner,
        host_services=services,
    )
    assert {item.profile.descriptor.key for item in registrations} == {
        "workflow.durable_task",
        "workflow.personal_v1",
        "workflow.capability_build",
    }
    registry = WorkflowRegistry(transaction_owner=owner)
    for registration in registrations:
        registry.register_definition(registration)
    assert {
        item.profile.descriptor.key for item in registry.profile_registrations()
    } == {
        "workflow.capability_build",
        "workflow.durable_task",
        "workflow.personal_v1",
    }
