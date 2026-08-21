# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from simple_harness.workflow import (
    SDK_DEPENDENCY_LOCK_HASH,
    WorkflowRegistry,
    compile_workflow,
    compile_workflow_registration,
    workflow_manifest_hash,
)
from simple_harness.workflow.errors import WorkflowDefinitionError
from simple_harness.workflows.durable_task import (
    build_durable_task_definition,
    build_durable_task_registration,
)


def test_embedded_dependency_identity_matches_repository_lock() -> None:
    repository = Path(__file__).resolve().parents[3]
    assert hashlib.sha256((repository / "uv.lock").read_bytes()).hexdigest() == (
        SDK_DEPENDENCY_LOCK_HASH
    )


def test_dependency_identity_seams_fail_closed(tmp_path: Path) -> None:
    definition = build_durable_task_definition()
    compiled = compile_workflow(definition, dependency_lock_hash=SDK_DEPENDENCY_LOCK_HASH)
    assert compiled.manifest.dependency_lock_hash == SDK_DEPENDENCY_LOCK_HASH

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        compile_workflow(definition, dependency_lock_hash="A" * 64)
    with pytest.raises(ValueError, match="mutually exclusive"):
        compile_workflow(
            definition,
            dependency_lock_hash=SDK_DEPENDENCY_LOCK_HASH,
            dependency_lock_path=Path(__file__),
        )
    with pytest.raises(WorkflowDefinitionError, match="does not exist"):
        compile_workflow(definition, dependency_lock_path=tmp_path / "missing.lock")

    owner = object()
    registration = build_durable_task_registration(generation=1, transaction_owner=owner)
    other_lock = tmp_path / "other.lock"
    other_lock.write_text("different dependency graph", encoding="utf-8")
    with pytest.raises(ValueError, match="dependency lock hash mismatch"):
        compile_workflow_registration(
            registration,
            transaction_owner=owner,
            dependency_lock_path=other_lock,
        )

    custom_compiled = compile_workflow(definition, dependency_lock_path=other_lock)
    custom_registration = replace(
        registration,
        dependency_lock_hash=custom_compiled.manifest.dependency_lock_hash,
        expected_manifest_hash=workflow_manifest_hash(custom_compiled.manifest),
        expected_implementation_fingerprint=(custom_compiled.manifest.implementation_bundle_hash),
    )
    custom_registry = WorkflowRegistry(transaction_owner=owner)
    custom_registry.register_definition(custom_registration)


def test_isolated_installed_package_builds_all_official_registrations(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[3]
    uv = shutil.which("uv")
    assert uv is not None
    distribution = tmp_path / "dist"
    built = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(distribution)],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(distribution.glob("*.whl"))
    script = """
from simple_harness.workflow import (
    CapabilityBuildHostServices,
    DurableTaskHostServices,
    PersonalWorkflowHostServices,
    WorkflowHostServices,
    WorkflowRegistry,
)
from simple_harness.workflows import build_official_workflow_registrations

class Ports:
    async def propose(self, state): return state
    async def execute_tools(self, calls, **kwargs): return {}
    async def execute(self, **kwargs): return {}
    async def search(self, **kwargs): return kwargs
    async def authorize_source(self, **kwargs): return kwargs
    async def build(self, **kwargs): return kwargs
    async def store(self, **kwargs): return kwargs
    async def activate(self, **kwargs): return kwargs
    async def authorize_build(self, **kwargs): return kwargs

owner = object()
ports = Ports()
services = WorkflowHostServices(
    durable_task=DurableTaskHostServices(proposal=ports, workspace=ports),
    personal_v1=PersonalWorkflowHostServices(runtime=ports),
    capability_build=CapabilityBuildHostServices(
        proposal=ports, workspace=ports, search=ports, source_policy=ports,
        isolated_build=ports, package_store=ports, activate=ports,
        authorization=ports,
    ),
)
registrations = build_official_workflow_registrations(
    generation=1, transaction_owner=owner, host_services=services
)
registry = WorkflowRegistry(transaction_owner=owner)
for registration in registrations:
    registry.register_definition(registration)
print(','.join(sorted(item.profile.descriptor.key for item in registrations)))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(wheel)
    clean_working_directory = tmp_path / "clean"
    clean_working_directory.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=clean_working_directory,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "workflow.capability_build,workflow.durable_task,workflow.personal_v1"
    )
    assert not (clean_working_directory / "uv.lock").exists()
