# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Shared public registration construction for official workflow profiles."""

from __future__ import annotations

import hashlib

from simple_harness.contracts import JsonValue, canonical_json
from simple_harness.runtime.orchestration import (
    StartInputSchema,
    WorkflowProfileRegistration,
)
from simple_harness.runtime.profiles import (
    ProfileDescriptor,
    profile_descriptor_fingerprint,
)
from simple_harness.workflow.definition import (
    WorkflowDefinition,
    WorkflowDefinitionRegistration,
    compile_workflow,
    workflow_manifest_hash,
)
from simple_harness.workflow.dependency_lock import SDK_DEPENDENCY_LOCK_HASH


def build_registration(
    *,
    profile_key: str,
    description: str,
    use_when: str,
    avoid_when: str,
    schema_ref: str,
    schema: dict[str, JsonValue],
    generation: int,
    definition: WorkflowDefinition,
    transaction_owner: object,
) -> WorkflowDefinitionRegistration:
    descriptor = ProfileDescriptor(
        key=profile_key,
        description=description,
        use_when=use_when,
        avoid_when=avoid_when,
        input_schema_ref=schema_ref,
        generation=generation,
        fingerprint=profile_descriptor_fingerprint(
            profile_key,
            description,
            use_when,
            avoid_when,
            schema_ref,
            generation,
        ),
    )
    profile = WorkflowProfileRegistration(
        descriptor=descriptor,
        workflow_name=definition.name,
        workflow_version=definition.version,
        start_input_schema=StartInputSchema(
            schema_ref=schema_ref,
            canonical_schema=schema,
            schema_hash=hashlib.sha256(canonical_json(schema).encode()).hexdigest(),
        ),
    )
    compiled = compile_workflow(definition, dependency_lock_hash=SDK_DEPENDENCY_LOCK_HASH)
    return WorkflowDefinitionRegistration(
        profile=profile,
        definition=definition,
        dependency_lock_hash=compiled.manifest.dependency_lock_hash,
        expected_manifest_hash=workflow_manifest_hash(compiled.manifest),
        expected_implementation_fingerprint=(compiled.manifest.implementation_bundle_hash),
        transaction_owner=transaction_owner,
    )


__all__ = ("build_registration",)
