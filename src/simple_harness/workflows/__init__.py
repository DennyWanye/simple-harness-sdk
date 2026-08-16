# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public composition helpers for SDK-owned official workflows."""

from __future__ import annotations

from simple_harness.workflow.contracts import WorkflowHostServices
from simple_harness.workflow.definition import WorkflowDefinitionRegistration


def build_official_workflow_registrations(
    *,
    generation: int,
    transaction_owner: object,
    host_services: WorkflowHostServices,
) -> tuple[WorkflowDefinitionRegistration, ...]:
    """Build every ready official profile; missing groups remain isolated."""

    if not isinstance(host_services, WorkflowHostServices):
        raise TypeError("host_services must be a WorkflowHostServices")
    registrations: list[WorkflowDefinitionRegistration] = []
    if host_services.durable_task is not None:
        from .durable_task.factory import build_durable_task_registration

        registrations.append(
            build_durable_task_registration(
                generation=generation, transaction_owner=transaction_owner
            )
        )
    if host_services.personal_v1 is not None:
        from .personal_v1.factory import build_personal_v1_registration

        registrations.append(
            build_personal_v1_registration(
                generation=generation, transaction_owner=transaction_owner
            )
        )
    if host_services.capability_build is not None:
        from .capability_build.factory import build_capability_build_registration

        registrations.append(
            build_capability_build_registration(
                generation=generation, transaction_owner=transaction_owner
            )
        )
    return tuple(registrations)


__all__ = ("build_official_workflow_registrations",)
