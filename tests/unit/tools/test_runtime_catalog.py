# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from simple_harness.contracts import RunId
from simple_harness.tools import (
    CatalogRunToolExposure,
    ExecutableToolRecord,
    RuntimeCapabilityKind,
    RuntimeToolCatalog,
    RuntimeToolCatalogError,
    SkillResourceRecord,
    ToolExposureMode,
    WorkflowProfileRecord,
)

OBJECT_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
PATH_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Workspace file path to read",
        }
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _records():  # type: ignore[no-untyped-def]
    return (
        ExecutableToolRecord(
            capability_id="builtin:finish",
            namespace="builtin",
            source="core",
            source_revision="core-v1",
            exposure_mode=ToolExposureMode.DIRECT,
            provider_name="finish",
            description="Finish the current task.",
            input_schema=OBJECT_SCHEMA,
        ),
        ExecutableToolRecord(
            capability_id="mcp:filesystem:read_file",
            namespace="mcp:filesystem",
            source="mcp-filesystem",
            source_revision="incarnation-a",
            exposure_mode=ToolExposureMode.DEFERRED,
            provider_name="mcp_filesystem_read_file",
            description="Read a file from the bounded workspace.",
            input_schema=PATH_SCHEMA,
            search_terms=("filesystem", "workspace document"),
        ),
        SkillResourceRecord(
            capability_id="skill:translate-doc",
            namespace="skill",
            source="first-party-skills",
            source_revision="skills-v1",
            exposure_mode=ToolExposureMode.DEFERRED,
            skill_locator="skill://translate-doc@1",
            content_hash="a" * 64,
            description="Translate a document while preserving headings.",
            metadata={"when_to_use": "translate markdown document"},
        ),
        WorkflowProfileRecord(
            capability_id="workflow:deep_research",
            namespace="workflow",
            source="official-workflows",
            source_revision="workflow-v7",
            exposure_mode=ToolExposureMode.DEFERRED,
            profile_key="workflow.deep_research",
            profile_fingerprint="b" * 64,
            description="Run a durable multi-source research workflow.",
            start_input_schema={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
                "additionalProperties": False,
            },
        ),
    )


def test_closed_kinds_freeze_one_immutable_snapshot() -> None:
    catalog = RuntimeToolCatalog(_records(), generation=7)
    snapshot = catalog.snapshot

    assert [item.kind for item in snapshot.records] == [
        RuntimeCapabilityKind.EXECUTABLE_TOOL,
        RuntimeCapabilityKind.EXECUTABLE_TOOL,
        RuntimeCapabilityKind.SKILL_RESOURCE,
        RuntimeCapabilityKind.WORKFLOW_PROFILE,
    ]
    assert snapshot.generation == 7
    assert len(snapshot.fingerprint) == 64
    assert snapshot.to_json()["records"][0]["capability_id"] == "builtin:finish"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        snapshot.generation = 8  # type: ignore[misc]
    with pytest.raises(TypeError):
        _records()[1].input_schema["type"] = "string"  # type: ignore[index]


def test_search_is_stable_bounded_and_descriptor_has_no_schema() -> None:
    catalog = RuntimeToolCatalog(_records(), generation=1)
    state = catalog.start_run(RunId("run-search"))

    first = catalog.search(state, "workspace path", limit=1)
    second = catalog.search(state, "workspace path", limit=1)

    assert first == second
    assert len(first.items) == 1
    assert first.items[0].capability_id == "mcp:filesystem:read_file"
    assert "schema" not in first.items[0].to_json()
    assert first.next_cursor is None
    with pytest.raises(RuntimeToolCatalogError, match="limit") as error:
        catalog.search(state, "workspace", limit=51)
    assert error.value.code == "catalog_search_limit_invalid"


def test_describe_activate_then_provider_projection_is_executable_only() -> None:
    catalog = RuntimeToolCatalog(_records(), generation=1)
    state = catalog.start_run(RunId("run-activate"))

    assert [item.name for item in catalog.provider_specs(state)] == ["finish"]
    description = catalog.describe(state, "mcp:filesystem:read_file")
    assert description.projection["schema_hash"]
    assert description.projection["input_schema"]

    activated, receipt = catalog.activate(state, "mcp:filesystem:read_file", description.nonce)
    assert activated.revision == 1
    assert receipt.to_json()["schema"] == "runtime_tool_activation_receipt/v1"
    assert "arguments" not in receipt.to_json()
    assert [item.name for item in catalog.provider_specs(activated)] == [
        "finish",
        "mcp_filesystem_read_file",
    ]

    skill_description = catalog.describe(activated, "skill:translate-doc")
    with_skill, _ = catalog.activate(activated, "skill:translate-doc", skill_description.nonce)
    workflow_description = catalog.describe(with_skill, "workflow:deep_research")
    with_workflow, _ = catalog.activate(
        with_skill, "workflow:deep_research", workflow_description.nonce
    )
    assert [item.name for item in catalog.provider_specs(with_workflow)] == [
        "finish",
        "mcp_filesystem_read_file",
    ]


def test_activation_is_idempotent_but_old_and_cross_run_nonce_fail_closed() -> None:
    catalog = RuntimeToolCatalog(_records(), generation=1)
    first = catalog.start_run(RunId("run-first"))
    second = catalog.start_run(RunId("run-second"))
    file_description = catalog.describe(first, "mcp:filesystem:read_file")
    skill_description = catalog.describe(first, "skill:translate-doc")

    with_file, receipt = catalog.activate(first, "mcp:filesystem:read_file", file_description.nonce)
    duplicate, duplicate_receipt = catalog.activate(
        with_file, "mcp:filesystem:read_file", file_description.nonce
    )
    assert duplicate == with_file
    assert duplicate_receipt.activation_id == receipt.activation_id

    with pytest.raises(RuntimeToolCatalogError) as stale:
        catalog.activate(with_file, "skill:translate-doc", skill_description.nonce)
    assert stale.value.code == "catalog_describe_nonce_invalid"
    with pytest.raises(RuntimeToolCatalogError) as cross_run:
        catalog.activate(second, "mcp:filesystem:read_file", file_description.nonce)
    assert cross_run.value.code == "catalog_describe_nonce_invalid"


def test_collisions_invalid_schema_and_bounds_are_rejected() -> None:
    direct = _records()[0]
    duplicate_name = ExecutableToolRecord(
        capability_id="builtin:other",
        namespace="builtin",
        source="core",
        source_revision="core-v1",
        exposure_mode=ToolExposureMode.DEFERRED,
        provider_name="finish",
        description="Other Tool.",
        input_schema=OBJECT_SCHEMA,
    )
    with pytest.raises(RuntimeToolCatalogError) as collision:
        RuntimeToolCatalog((direct, duplicate_name), generation=1)
    assert collision.value.code == "catalog_provider_name_collision"

    with pytest.raises(RuntimeToolCatalogError) as namespace_collision:
        RuntimeToolCatalog(
            (
                direct,
                ExecutableToolRecord(
                    capability_id="builtin:other",
                    namespace="builtin",
                    source="another-owner",
                    source_revision="other-v1",
                    exposure_mode=ToolExposureMode.DEFERRED,
                    provider_name="other",
                    description="Other Tool.",
                    input_schema=OBJECT_SCHEMA,
                ),
            ),
            generation=1,
        )
    assert namespace_collision.value.code == "catalog_namespace_collision"

    with pytest.raises(RuntimeToolCatalogError) as invalid_schema:
        ExecutableToolRecord(
            capability_id="builtin:bad",
            namespace="builtin",
            source="core",
            source_revision="core-v1",
            exposure_mode=ToolExposureMode.DEFERRED,
            provider_name="bad",
            description="Bad Tool.",
            input_schema={"type": "object", "additionalProperties": True},
        )
    assert invalid_schema.value.code == "catalog_schema_invalid"

    with pytest.raises(RuntimeToolCatalogError) as oversized:
        SkillResourceRecord(
            capability_id="skill:too-large",
            namespace="skill",
            source="skills",
            source_revision="v1",
            exposure_mode=ToolExposureMode.DEFERRED,
            skill_locator="x" * 2049,
            content_hash="a" * 64,
            description="Oversized locator.",
        )
    assert oversized.value.code == "catalog_field_too_large"


def test_audit_summary_contains_counts_not_schema_or_skill_body() -> None:
    catalog = RuntimeToolCatalog(_records(), generation=3)
    state = catalog.start_run(RunId("run-audit"))

    summary = catalog.audit_summary(state)

    assert summary["direct_count"] == 1
    assert summary["deferred_count"] == 3
    assert summary["activated_count"] == 0
    assert "schema" not in repr(summary)
    assert "instruction" not in repr(summary)


def test_concrete_port_restores_checkpoints_and_reapplies_receipts() -> None:
    catalog = RuntimeToolCatalog(_records(), generation=4)
    port = CatalogRunToolExposure(catalog)
    run_id = RunId("run-port")
    port.restore(run_id, None)
    described = port.describe(run_id, "mcp:filesystem:read_file")
    receipt = port.activate(run_id, "mcp:filesystem:read_file", described.nonce)

    checkpoint = port.checkpoint(run_id)
    assert [item.name for item in port.provider_specs(run_id)] == [
        "finish",
        "mcp_filesystem_read_file",
    ]
    port.observe_tool_result(run_id, "tool_activate", receipt.to_json())

    replayed = CatalogRunToolExposure(catalog)
    replayed.restore(run_id, None)
    replayed.observe_tool_result(run_id, "tool_activate", receipt.to_json())
    assert replayed.checkpoint(run_id) == checkpoint

    restored = CatalogRunToolExposure(catalog)
    restored.restore(run_id, checkpoint)
    restored.observe_tool_result(run_id, "tool_activate", receipt.to_json())
    assert restored.checkpoint(run_id) == checkpoint


def test_prepare_activation_does_not_mutate_before_terminal_effect_observation() -> None:
    catalog = RuntimeToolCatalog(_records(), generation=4)
    port = CatalogRunToolExposure(catalog)
    run_id = RunId("run-prepared")
    port.restore(run_id, None)
    described = port.describe(run_id, "mcp:filesystem:read_file")

    receipt = port.prepare_activation(
        run_id, "mcp:filesystem:read_file", described.nonce
    )
    assert [item.name for item in port.provider_specs(run_id)] == ["finish"]

    port.observe_tool_result(run_id, "tool_activate", receipt.to_json())
    assert [item.name for item in port.provider_specs(run_id)] == [
        "finish",
        "mcp_filesystem_read_file",
    ]

    # A retry in the same process must restore the durable checkpoint rather
    # than preserve an in-memory activation that was not checkpointed.
    port.restore(run_id, None)
    assert [item.name for item in port.provider_specs(run_id)] == ["finish"]

    forged = receipt.to_json()
    forged["capability_hash"] = "0" * 64
    with pytest.raises(RuntimeToolCatalogError) as error:
        port.observe_tool_result(run_id, "tool_activate", forged)
    assert error.value.code == "catalog_activation_receipt_invalid"

    with pytest.raises(RuntimeToolCatalogError) as wrong_tool:
        port.observe_tool_result(run_id, "other_tool", receipt.to_json())
    assert wrong_tool.value.code == "catalog_activation_tool_mismatch"
