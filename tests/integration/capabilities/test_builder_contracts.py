# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for capability builder data contracts.

Validates:
- CapabilityBuildSearchEvidence construction and validation
- CapabilityBuildLineage construction, fingerprinting, and serialization
- CapabilityBuildLaunch construction, staging path validation
- CapabilityBuildEvidence construction and hash validation
- CapabilityBuildCompletion parsing and validation
- CapabilityManagerInstallRequest construction
- Round-trip serialization for all contracts
"""

from __future__ import annotations

import hashlib
import json

import pytest

from simple_harness.capabilities import (
    CapabilityBuildCompletion,
    CapabilityBuildError,
    CapabilityBuildEvidence,
    CapabilityBuildLaunch,
    CapabilityBuildLineage,
    CapabilityBuildSearchEvidence,
    CapabilityManagerInstallRequest,
)


def test_search_evidence_construction():
    """Test CapabilityBuildSearchEvidence validation."""
    evidence = CapabilityBuildSearchEvidence(
        receipt_ref="a" * 64,
        catalog_stamp={
            "catalog_generation": 1,
            "registry_revision": 10,
            "binding_generation": 2,
            "fingerprint": "b" * 64,
        },
        snapshot_ref="snapshot-123",
        query_hash="c" * 64,
        hit_count=5,
        best_executable_score=75.5,
    )

    assert evidence.receipt_ref == "a" * 64
    assert evidence.stamp_fingerprint == "b" * 64
    assert evidence.hit_count == 5
    assert evidence.best_executable_score == 75.5


def test_search_evidence_invalid_receipt_ref():
    """Test search evidence rejects invalid receipt ref."""
    with pytest.raises(CapabilityBuildError) as exc:
        CapabilityBuildSearchEvidence(
            receipt_ref="not-a-hash",
            catalog_stamp={
                "catalog_generation": 1,
                "registry_revision": 1,
                "binding_generation": 1,
                "fingerprint": "b" * 64,
            },
            snapshot_ref="snap",
            query_hash="c" * 64,
            hit_count=0,
            best_executable_score=None,
        )
    assert exc.value.code == "invalid_builder_payload"


def test_search_evidence_roundtrip():
    """Test search evidence serialization roundtrip."""
    original = CapabilityBuildSearchEvidence(
        receipt_ref="a" * 64,
        catalog_stamp={
            "catalog_generation": 1,
            "registry_revision": 1,
            "binding_generation": 1,
            "fingerprint": "b" * 64,
        },
        snapshot_ref="snap-123",
        query_hash="c" * 64,
        hit_count=3,
        best_executable_score=None,
    )

    serialized = original.to_dict()
    restored = CapabilityBuildSearchEvidence.from_dict(serialized)

    assert restored.receipt_ref == original.receipt_ref
    assert restored.stamp_fingerprint == original.stamp_fingerprint
    assert restored.query_hash == original.query_hash
    assert restored.hit_count == original.hit_count
    assert restored.best_executable_score == original.best_executable_score


def test_lineage_construction():
    """Test CapabilityBuildLineage validation and lineage_id generation."""
    lineage = CapabilityBuildLineage(
        root_run_id="root-123",
        parent_run_id="parent-456",
        parent_goal_ref="goal-789",
        original_objective="Build a calculator tool",
        original_args={"precision": 10, "mode": "strict"},
        search_receipt_ref="a" * 64,
        catalog_stamp_fingerprint="b" * 64,
    )

    assert lineage.root_run_id == "root-123"
    assert lineage.operation_kind == "install"
    assert len(lineage.lineage_id) == 64  # SHA-256 hex
    assert lineage.parent_version is None
    assert lineage.parent_manifest_hash is None


def test_lineage_repair_ancestry():
    """Test lineage with repair ancestry."""
    lineage = CapabilityBuildLineage(
        root_run_id="root-123",
        parent_run_id="parent-456",
        parent_goal_ref="goal-789",
        original_objective="Repair calculator",
        original_args={"tool": "calc"},
        search_receipt_ref="a" * 64,
        catalog_stamp_fingerprint="b" * 64,
        operation_kind="repair",
        parent_version="1.0.0",
        parent_manifest_hash="c" * 64,
        failure_receipt_ref="d" * 64,
    )

    assert lineage.operation_kind == "repair"
    assert lineage.parent_version == "1.0.0"
    assert lineage.parent_manifest_hash == "c" * 64
    assert lineage.failure_receipt_ref == "d" * 64


def test_lineage_repair_missing_fields():
    """Test lineage repair requires all repair fields."""
    with pytest.raises(CapabilityBuildError) as exc:
        CapabilityBuildLineage(
            root_run_id="root-123",
            parent_run_id="parent-456",
            parent_goal_ref="goal-789",
            original_objective="Repair",
            original_args={},
            search_receipt_ref="a" * 64,
            catalog_stamp_fingerprint="b" * 64,
            operation_kind="repair",
            parent_version="1.0.0",
            # Missing parent_manifest_hash and failure_receipt_ref
        )
    assert exc.value.code == "repair_lineage_required"


def test_lineage_roundtrip():
    """Test lineage serialization roundtrip."""
    original = CapabilityBuildLineage(
        root_run_id="root-123",
        parent_run_id="parent-456",
        parent_goal_ref="goal-789",
        original_objective="Build tool",
        original_args={"key": "value"},
        search_receipt_ref="a" * 64,
        catalog_stamp_fingerprint="b" * 64,
    )

    serialized = original.to_dict()
    restored = CapabilityBuildLineage.from_dict(serialized)

    assert restored.lineage_id == original.lineage_id
    assert restored.root_run_id == original.root_run_id
    assert restored.original_objective == original.original_objective
    assert dict(restored.original_args) == dict(original.original_args)


def test_lineage_fingerprint_mismatch():
    """Test lineage rejects mismatched lineage_id."""
    with pytest.raises(CapabilityBuildError) as exc:
        CapabilityBuildLineage(
            root_run_id="root-123",
            parent_run_id="parent-456",
            parent_goal_ref="goal-789",
            original_objective="Build",
            original_args={},
            search_receipt_ref="a" * 64,
            catalog_stamp_fingerprint="b" * 64,
            lineage_id="wrong" + ("0" * 59),  # Invalid lineage_id
        )
    assert exc.value.code == "builder_lineage_mismatch"


def test_launch_construction():
    """Test CapabilityBuildLaunch validation."""
    evidence = CapabilityBuildSearchEvidence(
        receipt_ref="a" * 64,
        catalog_stamp={
            "catalog_generation": 1,
            "registry_revision": 1,
            "binding_generation": 1,
            "fingerprint": "b" * 64,
        },
        snapshot_ref="snap",
        query_hash="c" * 64,
        hit_count=0,
        best_executable_score=None,
    )

    lineage = CapabilityBuildLineage(
        root_run_id="root-123",
        parent_run_id="parent-456",
        parent_goal_ref="goal-789",
        original_objective="Build",
        original_args={},
        search_receipt_ref="a" * 64,
        catalog_stamp_fingerprint="b" * 64,
    )

    launch = CapabilityBuildLaunch(
        lineage=lineage,
        search_evidence=evidence,
        task_workspace="/tmp/workspace",
        managed_staging_base="/tmp/staging",
        staging_root=f"/tmp/staging/{lineage.lineage_id}",
    )

    assert launch.install_scope == "run"
    assert launch.publish_policy == "general_install"
    assert launch.max_repair_drafts == 3
    assert launch.initial_draft.endswith("/draft-0")


def test_launch_draft_path():
    """Test launch draft path generation."""
    evidence = CapabilityBuildSearchEvidence(
        receipt_ref="a" * 64,
        catalog_stamp={
            "catalog_generation": 1,
            "registry_revision": 1,
            "binding_generation": 1,
            "fingerprint": "b" * 64,
        },
        snapshot_ref="snap",
        query_hash="c" * 64,
        hit_count=0,
        best_executable_score=None,
    )

    lineage = CapabilityBuildLineage(
        root_run_id="root-123",
        parent_run_id="parent-456",
        parent_goal_ref="goal-789",
        original_objective="Build",
        original_args={},
        search_receipt_ref="a" * 64,
        catalog_stamp_fingerprint="b" * 64,
    )

    launch = CapabilityBuildLaunch(
        lineage=lineage,
        search_evidence=evidence,
        task_workspace="/tmp/workspace",
        managed_staging_base="/tmp/staging",
        staging_root=f"/tmp/staging/{lineage.lineage_id}",
    )

    assert str(launch.draft_path(0)).endswith("/draft-0")
    assert str(launch.draft_path(1)).endswith("/draft-1")
    assert str(launch.draft_path(3)).endswith("/draft-3")

    with pytest.raises(CapabilityBuildError) as exc:
        launch.draft_path(4)  # Exceeds max_repair_drafts
    assert exc.value.code == "repair_budget_exhausted"


def test_launch_search_lineage_mismatch():
    """Test launch rejects mismatched search evidence."""
    evidence = CapabilityBuildSearchEvidence(
        receipt_ref="1" * 64,  # Different receipt (valid hash format)
        catalog_stamp={
            "catalog_generation": 1,
            "registry_revision": 1,
            "binding_generation": 1,
            "fingerprint": "b" * 64,
        },
        snapshot_ref="snap",
        query_hash="c" * 64,
        hit_count=0,
        best_executable_score=None,
    )

    lineage = CapabilityBuildLineage(
        root_run_id="root-123",
        parent_run_id="parent-456",
        parent_goal_ref="goal-789",
        original_objective="Build",
        original_args={},
        search_receipt_ref="a" * 64,
        catalog_stamp_fingerprint="b" * 64,
    )

    with pytest.raises(CapabilityBuildError) as exc:
        CapabilityBuildLaunch(
            lineage=lineage,
            search_evidence=evidence,
            task_workspace="/tmp/workspace",
            managed_staging_base="/tmp/staging",
            staging_root=f"/tmp/staging/{lineage.lineage_id}",
        )
    assert exc.value.code == "builder_search_lineage_mismatch"


def test_launch_roundtrip():
    """Test launch serialization roundtrip."""
    evidence = CapabilityBuildSearchEvidence(
        receipt_ref="a" * 64,
        catalog_stamp={
            "catalog_generation": 1,
            "registry_revision": 1,
            "binding_generation": 1,
            "fingerprint": "b" * 64,
        },
        snapshot_ref="snap",
        query_hash="c" * 64,
        hit_count=0,
        best_executable_score=None,
    )

    lineage = CapabilityBuildLineage(
        root_run_id="root-123",
        parent_run_id="parent-456",
        parent_goal_ref="goal-789",
        original_objective="Build",
        original_args={},
        search_receipt_ref="a" * 64,
        catalog_stamp_fingerprint="b" * 64,
    )

    original = CapabilityBuildLaunch(
        lineage=lineage,
        search_evidence=evidence,
        task_workspace="/tmp/workspace",
        managed_staging_base="/tmp/staging",
        staging_root=f"/tmp/staging/{lineage.lineage_id}",
    )

    serialized = original.to_dict()
    restored = CapabilityBuildLaunch.from_dict(serialized)

    assert restored.lineage.lineage_id == original.lineage.lineage_id
    assert restored.staging_root == original.staging_root
    assert restored.install_scope == original.install_scope


def test_install_request_construction():
    """Test CapabilityManagerInstallRequest validation."""
    request = CapabilityManagerInstallRequest(
        operation_kind="install",
        source_type="local",
        source_uri="/tmp/staging/draft-0",
        source_revision="generated-abc123-draft-0",
        scope="run",
        scope_key="run-123",
        expected_pack_id="com.example.calculator",
        generated=True,
    )

    assert request.operation_kind == "install"
    assert request.generated is True
    assert request.parent_version is None


def test_install_request_repair():
    """Test install request with repair ancestry."""
    request = CapabilityManagerInstallRequest(
        operation_kind="repair",
        source_type="local",
        source_uri="/tmp/staging/draft-1",
        source_revision="generated-abc123-draft-1",
        scope="run",
        scope_key="run-123",
        expected_pack_id="com.example.calculator",
        generated=True,
        parent_version="1.0.0",
        parent_manifest_hash="a" * 64,
        failure_receipt_ref="b" * 64,
    )

    assert request.operation_kind == "repair"
    assert request.parent_version == "1.0.0"
    assert request.parent_manifest_hash == "a" * 64


def test_install_request_to_tool_args():
    """Test install request conversion to tool args."""
    request = CapabilityManagerInstallRequest(
        operation_kind="install",
        source_type="local",
        source_uri="/tmp/draft",
        source_revision="rev-1",
        scope="project",
        scope_key="/workspace",
        expected_pack_id="test.pack",
        generated=True,
    )

    args = request.to_tool_args()
    assert args["source_type"] == "local"
    assert args["uri"] == "/tmp/draft"
    assert args["generated"] is True
    assert "parent_version" not in args  # install, not repair


def test_completion_construction():
    """Test CapabilityBuildCompletion validation."""
    completion = CapabilityBuildCompletion(
        lineage_id="a" * 64,
        draft_index=1,
        draft_path="/tmp/staging/abc123/draft-1",
    )

    assert completion.lineage_id == "a" * 64
    assert completion.draft_index == 1
    assert completion.schema_version == 1


def test_completion_from_json_string():
    """Test completion parsing from JSON string."""
    value = json.dumps(
        {
            "schema_version": 1,
            "lineage_id": "a" * 64,
            "draft_index": 2,
            "draft_path": "/tmp/draft-2",
        }
    )

    completion = CapabilityBuildCompletion.from_value(value)
    assert completion.draft_index == 2
    assert completion.lineage_id == "a" * 64


def test_completion_from_object():
    """Test completion parsing from dict object."""
    value = {
        "schema_version": 1,
        "lineage_id": "b" * 64,
        "draft_index": 0,
        "draft_path": "/tmp/draft-0",
    }

    completion = CapabilityBuildCompletion.from_value(value)
    assert completion.draft_index == 0
    assert completion.lineage_id == "b" * 64


def test_completion_invalid_json():
    """Test completion rejects invalid JSON."""
    with pytest.raises(CapabilityBuildError) as exc:
        CapabilityBuildCompletion.from_value("not valid json")
    assert exc.value.code == "builder_completion_invalid"


def test_completion_missing_fields():
    """Test completion rejects incomplete objects."""
    with pytest.raises(CapabilityBuildError) as exc:
        CapabilityBuildCompletion.from_value(
            {
                "schema_version": 1,
                "lineage_id": "a" * 64,
                # Missing draft_index and draft_path
            }
        )
    assert exc.value.code == "builder_completion_invalid"


def test_evidence_construction():
    """Test CapabilityBuildEvidence validation."""
    archive_bytes = b"mock archive content"
    archive_hash = hashlib.sha256(archive_bytes).hexdigest()

    install_request = CapabilityManagerInstallRequest(
        operation_kind="install",
        source_type="local",
        source_uri="/tmp/draft",
        source_revision="rev-1",
        scope="run",
        scope_key="run-123",
        expected_pack_id="test.pack",
        generated=True,
    )

    evidence = CapabilityBuildEvidence(
        lineage_id="a" * 64,
        draft_index=0,
        material_fingerprint="b" * 64,
        validated_draft_hash="c" * 64,
        manifest_hash="d" * 64,
        archive_hash=archive_hash,
        file_set_hash="e" * 64,
        effect_topology_hash="f" * 64,
        archive_bytes=archive_bytes,
        checks=("manifest_and_integrity", "healthcheck", "happy_path"),
        install_request=install_request,
        search_receipt_ref="0" * 64,
        catalog_stamp_fingerprint="1" * 64,
        original_objective_fingerprint="2" * 64,
        original_args_fingerprint="3" * 64,
    )

    assert evidence.draft_index == 0
    assert len(evidence.checks) == 3
    assert evidence.archive_hash == archive_hash


def test_evidence_archive_hash_mismatch():
    """Test evidence rejects mismatched archive hash."""
    archive_bytes = b"mock content"
    wrong_hash = "0" * 64  # Valid format but not the actual hash

    install_request = CapabilityManagerInstallRequest(
        operation_kind="install",
        source_type="local",
        source_uri="/tmp/draft",
        source_revision="rev-1",
        scope="run",
        scope_key="run-123",
        expected_pack_id="test.pack",
        generated=True,
    )

    with pytest.raises(CapabilityBuildError) as exc:
        CapabilityBuildEvidence(
            lineage_id="a" * 64,
            draft_index=0,
            material_fingerprint="b" * 64,
            validated_draft_hash="c" * 64,
            manifest_hash="d" * 64,
            archive_hash=wrong_hash,
            file_set_hash="e" * 64,
            effect_topology_hash="f" * 64,
            archive_bytes=archive_bytes,
            checks=("check1",),
            install_request=install_request,
            search_receipt_ref="1" * 64,
            catalog_stamp_fingerprint="2" * 64,
            original_objective_fingerprint="3" * 64,
            original_args_fingerprint="4" * 64,
        )
    assert exc.value.code == "invalid_builder_evidence"
