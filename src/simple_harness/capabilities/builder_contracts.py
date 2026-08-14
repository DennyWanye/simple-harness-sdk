# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Capability builder data contracts and immutable state snapshots.

This module defines the frozen data structures used throughout the capability
build protocol: lineage tracking, search evidence, launch configuration, draft
validation evidence, and completion signaling.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import InitVar, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
from types import MappingProxyType

from simple_harness.contracts import JsonValue, canonical_json, fingerprint_json

from .builder_errors import CapabilityBuildError

# Protocol constants
CAPABILITY_BUILD_PROTOCOL_VERSION = 1
MAX_REPAIR_DRAFTS = 3
MIN_SUFFICIENT_EXECUTABLE_SCORE = 80.0
CAPABILITY_STAGING_DIRECTORY = "capability-staging"
GENERATED_WORKER_PATH = "worker.py"
HAPPY_PATH_TEST = "tests/happy.json"
INVALID_INPUT_TEST = "tests/invalid.json"
PACK_MANIFEST_NAME = "pack.toml"

# Type aliases
BuildOperationKind = Literal["install", "repair"]

# Validation patterns
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

# Internal admission token
_CANDIDATE_ADMISSION_ISSUER = object()


def _required_text(value: object, name: str) -> str:
    """Validate that a value is a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        raise CapabilityBuildError(
            "invalid_builder_payload",
            f"{name} is required",
        )
    return value.strip()


def _digest(value: object, name: str) -> str:
    """Validate that a value is a lowercase SHA-256 digest."""
    text = _required_text(value, name)
    if not _DIGEST.fullmatch(text):
        raise CapabilityBuildError(
            "invalid_builder_payload",
            f"{name} must be a lowercase SHA-256 digest",
        )
    return text


def _json_mapping(value: Mapping[str, Any], name: str) -> Mapping[str, JsonValue]:
    """Validate and freeze a JSON mapping."""
    try:
        import json
        payload = json.loads(canonical_json(dict(value)))
    except (TypeError, ValueError) as exc:
        raise CapabilityBuildError(
            "invalid_builder_payload",
            f"{name} must be a closed JSON object: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise CapabilityBuildError(
            "invalid_builder_payload",
            f"{name} must be a JSON object",
        )
    return MappingProxyType(payload)


@dataclass(frozen=True, slots=True)
class CapabilityBuildSearchEvidence:
    """Search attestation required for capability build admission.

    Proves that a capability_search was executed and that no sufficient
    executable capability exists in the current catalog.
    """

    receipt_ref: str
    catalog_stamp: Mapping[str, JsonValue]
    snapshot_ref: str
    query_hash: str
    hit_count: int
    best_executable_score: float | None
    evidence_kind: Literal["search", "repair_receipt"] = "search"

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_ref", _digest(self.receipt_ref, "receipt_ref"))

        # Validate catalog stamp structure
        stamp = dict(self.catalog_stamp)
        required_keys = {"catalog_generation", "registry_revision", "binding_generation", "fingerprint"}
        if not required_keys.issubset(stamp.keys()):
            raise CapabilityBuildError(
                "invalid_builder_payload",
                f"catalog_stamp must contain {required_keys}",
            )
        object.__setattr__(self, "catalog_stamp", MappingProxyType(stamp))

        object.__setattr__(
            self,
            "snapshot_ref",
            _required_text(self.snapshot_ref, "snapshot_ref"),
        )
        object.__setattr__(self, "query_hash", _digest(self.query_hash, "query_hash"))

        if self.hit_count < 0:
            raise CapabilityBuildError(
                "invalid_builder_payload",
                "hit_count must be non-negative",
            )

        import math
        if self.best_executable_score is not None and not math.isfinite(
            float(self.best_executable_score)
        ):
            raise CapabilityBuildError(
                "invalid_builder_payload",
                "best_executable_score must be finite",
            )

        if self.evidence_kind not in {"search", "repair_receipt"}:
            raise CapabilityBuildError(
                "invalid_builder_payload",
                f"unsupported admission evidence: {self.evidence_kind}",
            )

    @property
    def stamp_fingerprint(self) -> str:
        return str(self.catalog_stamp["fingerprint"])

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "receipt_ref": self.receipt_ref,
            "catalog_stamp": dict(self.catalog_stamp),
            "snapshot_ref": self.snapshot_ref,
            "query_hash": self.query_hash,
            "hit_count": self.hit_count,
            "best_executable_score": self.best_executable_score,
            "evidence_kind": self.evidence_kind,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> CapabilityBuildSearchEvidence:
        stamp = value.get("catalog_stamp")
        if not isinstance(stamp, Mapping):
            raise CapabilityBuildError(
                "invalid_builder_payload",
                "catalog_stamp must be an object",
            )
        score = value.get("best_executable_score")
        return cls(
            receipt_ref=str(value.get("receipt_ref") or ""),
            catalog_stamp=dict(stamp),
            snapshot_ref=str(value.get("snapshot_ref") or ""),
            query_hash=str(value.get("query_hash") or ""),
            hit_count=int(value.get("hit_count") or 0),
            best_executable_score=(None if score is None else float(score)),
            evidence_kind=str(
                value.get("evidence_kind") or "search"
            ),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CapabilityBuildLineage:
    """Immutable parent goal and search ancestry for a capability build.

    Tracks the original request, search receipt, and repair ancestry if applicable.
    The lineage_id is deterministically derived from all immutable fields.
    """

    root_run_id: str
    parent_run_id: str
    parent_goal_ref: str
    original_objective: str
    original_args: Mapping[str, JsonValue]
    search_receipt_ref: str
    catalog_stamp_fingerprint: str
    operation_kind: BuildOperationKind = "install"
    parent_version: str | None = None
    parent_manifest_hash: str | None = None
    failure_receipt_ref: str | None = None
    lineage_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "root_run_id",
            "parent_run_id",
            "parent_goal_ref",
            "original_objective",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name),
            )

        object.__setattr__(
            self,
            "original_args",
            _json_mapping(self.original_args, "original_args"),
        )
        object.__setattr__(
            self,
            "search_receipt_ref",
            _digest(self.search_receipt_ref, "search_receipt_ref"),
        )
        object.__setattr__(
            self,
            "catalog_stamp_fingerprint",
            _digest(
                self.catalog_stamp_fingerprint,
                "catalog_stamp_fingerprint",
            ),
        )

        if self.operation_kind not in {"install", "repair"}:
            raise CapabilityBuildError(
                "invalid_builder_payload",
                f"unsupported operation kind: {self.operation_kind}",
            )

        repair_values = (
            self.parent_version,
            self.parent_manifest_hash,
            self.failure_receipt_ref,
        )
        if self.operation_kind == "repair":
            if not all(repair_values):
                raise CapabilityBuildError(
                    "repair_lineage_required",
                    "repair requires parent version/hash and failure receipt",
                )
            object.__setattr__(
                self,
                "parent_manifest_hash",
                _digest(self.parent_manifest_hash, "parent_manifest_hash"),
            )
        elif any(value is not None for value in repair_values):
            raise CapabilityBuildError(
                "invalid_builder_payload",
                "install lineage cannot carry repair ancestry",
            )

        # Compute deterministic lineage_id
        identity: Mapping[str, JsonValue] = {
            "root_run_id": self.root_run_id,
            "parent_run_id": self.parent_run_id,
            "parent_goal_ref": self.parent_goal_ref,
            "original_objective": self.original_objective,
            "original_args": dict(self.original_args),
            "search_receipt_ref": self.search_receipt_ref,
            "catalog_stamp_fingerprint": self.catalog_stamp_fingerprint,
            "operation_kind": self.operation_kind,
            "parent_version": self.parent_version,
            "parent_manifest_hash": self.parent_manifest_hash,
            "failure_receipt_ref": self.failure_receipt_ref,
        }
        expected = fingerprint_json(identity)
        if self.lineage_id and self.lineage_id != expected:
            raise CapabilityBuildError(
                "builder_lineage_mismatch",
                "builder lineage does not match its immutable parent facts",
            )
        object.__setattr__(self, "lineage_id", expected)

    @property
    def original_args_fingerprint(self) -> str:
        return fingerprint_json(dict(self.original_args))

    @property
    def original_objective_fingerprint(self) -> str:
        return fingerprint_json({"objective": self.original_objective})

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "root_run_id": self.root_run_id,
            "parent_run_id": self.parent_run_id,
            "parent_goal_ref": self.parent_goal_ref,
            "original_objective": self.original_objective,
            "original_args": dict(self.original_args),
            "original_args_fingerprint": self.original_args_fingerprint,
            "original_objective_fingerprint": (
                self.original_objective_fingerprint
            ),
            "search_receipt_ref": self.search_receipt_ref,
            "catalog_stamp_fingerprint": self.catalog_stamp_fingerprint,
            "operation_kind": self.operation_kind,
            "parent_version": self.parent_version,
            "parent_manifest_hash": self.parent_manifest_hash,
            "failure_receipt_ref": self.failure_receipt_ref,
            "lineage_id": self.lineage_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CapabilityBuildLineage:
        args = value.get("original_args")
        if not isinstance(args, Mapping):
            raise CapabilityBuildError(
                "invalid_builder_payload",
                "original_args must be an object",
            )
        lineage = cls(
            root_run_id=str(value.get("root_run_id") or ""),
            parent_run_id=str(value.get("parent_run_id") or ""),
            parent_goal_ref=str(value.get("parent_goal_ref") or ""),
            original_objective=str(value.get("original_objective") or ""),
            original_args=dict(args),
            search_receipt_ref=str(value.get("search_receipt_ref") or ""),
            catalog_stamp_fingerprint=str(
                value.get("catalog_stamp_fingerprint") or ""
            ),
            operation_kind=str(
                value.get("operation_kind") or "install"
            ),  # type: ignore[arg-type]
            parent_version=(
                str(value["parent_version"])
                if value.get("parent_version") is not None
                else None
            ),
            parent_manifest_hash=(
                str(value["parent_manifest_hash"])
                if value.get("parent_manifest_hash") is not None
                else None
            ),
            failure_receipt_ref=(
                str(value["failure_receipt_ref"])
                if value.get("failure_receipt_ref") is not None
                else None
            ),
            lineage_id=str(value.get("lineage_id") or ""),
        )

        # Validate fingerprints if provided
        expected_args = value.get("original_args_fingerprint")
        if (
            expected_args is not None
            and str(expected_args) != lineage.original_args_fingerprint
        ):
            raise CapabilityBuildError(
                "builder_lineage_mismatch",
                "original args changed after builder admission",
            )
        expected_objective = value.get("original_objective_fingerprint")
        if (
            expected_objective is not None
            and str(expected_objective)
            != lineage.original_objective_fingerprint
        ):
            raise CapabilityBuildError(
                "builder_lineage_mismatch",
                "parent objective changed after builder admission",
            )
        return lineage


@dataclass(frozen=True, slots=True)
class CapabilityBuildCandidateAdmissionV1:
    """Host-issued execution identity frozen before a governed builder starts.

    This admission is required when the builder will produce Skill or Workflow
    entries (governed output). It captures the permit hash and expected entry kinds.
    """

    permit: Any  # GrowthCandidateBuildPermitV1 - not ported yet
    builder_launch_id: str
    child_run_id: str
    child_start_hash: str
    expected_entry_kinds: tuple[Literal["skill", "workflow"], ...]
    _host_token: InitVar[object] = None

    def __post_init__(self, _host_token: object) -> None:
        if _host_token is not _CANDIDATE_ADMISSION_ISSUER:
            raise CapabilityBuildError(
                "candidate_build_admission_host_only",
                "candidate build admission must be issued by the host",
            )
        for name in ("builder_launch_id", "child_run_id"):
            _required_text(getattr(self, name), name)
        _digest(self.child_start_hash, "child_start_hash")

        kinds = tuple(sorted(set(self.expected_entry_kinds)))
        if not kinds or any(item not in {"skill", "workflow"} for item in kinds):
            raise CapabilityBuildError(
                "candidate_entry_kind_invalid",
                "candidate admission requires skill and/or workflow output",
            )
        object.__setattr__(self, "expected_entry_kinds", kinds)

    @classmethod
    def issue(
        cls,
        *,
        permit: Any,
        builder_launch_id: str,
        child_run_id: str,
        child_start_hash: str,
        expected_entry_kinds: Sequence[Literal["skill", "workflow"]],
    ) -> CapabilityBuildCandidateAdmissionV1:
        """Issue a new candidate admission (host-only factory)."""
        # Note: permit validation not ported yet
        return cls(
            permit=permit,
            builder_launch_id=builder_launch_id,
            child_run_id=child_run_id,
            child_start_hash=child_start_hash,
            expected_entry_kinds=tuple(expected_entry_kinds),
            _host_token=_CANDIDATE_ADMISSION_ISSUER,
        )


@dataclass(frozen=True, slots=True)
class CapabilityBuildLaunch:
    """Frozen admission result given to the builder child workflow.

    Contains lineage, search evidence, staging paths, and policy. This is the
    complete contract between host and builder.
    """

    lineage: CapabilityBuildLineage
    search_evidence: CapabilityBuildSearchEvidence
    task_workspace: str
    managed_staging_base: str
    staging_root: str
    install_scope: Literal["run", "project", "user"] = "run"
    publish_policy: Literal["general_install", "candidate_only"] = "general_install"
    candidate_admission: CapabilityBuildCandidateAdmissionV1 | None = None
    max_repair_drafts: int = MAX_REPAIR_DRAFTS
    schema_version: int = CAPABILITY_BUILD_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_BUILD_PROTOCOL_VERSION:
            raise CapabilityBuildError(
                "unsupported_builder_protocol",
                f"unsupported builder protocol: {self.schema_version}",
            )
        if self.max_repair_drafts != MAX_REPAIR_DRAFTS:
            raise CapabilityBuildError(
                "invalid_repair_budget",
                f"builder repair budget must be {MAX_REPAIR_DRAFTS}",
            )

        # Validate lineage matches search evidence
        if (
            self.lineage.search_receipt_ref
            != self.search_evidence.receipt_ref
            or self.lineage.catalog_stamp_fingerprint
            != self.search_evidence.stamp_fingerprint
        ):
            raise CapabilityBuildError(
                "builder_search_lineage_mismatch",
                "search evidence changed after builder admission",
            )

        # Validate staging paths
        workspace = Path(self.task_workspace).expanduser().resolve(strict=False)
        managed_base = Path(self.managed_staging_base).expanduser().resolve(
            strict=False
        )
        staging = Path(self.staging_root).expanduser().resolve(strict=False)
        expected = (managed_base / self.lineage.lineage_id).resolve(
            strict=False
        )
        if staging != expected:
            raise CapabilityBuildError(
                "builder_staging_mismatch",
                "staging root is not the host-owned capability staging path",
            )

        if self.install_scope not in {"run", "project", "user"}:
            raise CapabilityBuildError(
                "invalid_install_scope",
                f"unsupported generated capability scope: {self.install_scope}",
            )

        # Validate publish policy matches admission
        if self.publish_policy == "candidate_only":
            if not isinstance(
                self.candidate_admission, CapabilityBuildCandidateAdmissionV1
            ):
                raise CapabilityBuildError(
                    "candidate_build_admission_required",
                    "candidate-only launch requires host-issued admission",
                )
        elif self.candidate_admission is not None:
            raise CapabilityBuildError(
                "candidate_build_admission_unexpected",
                "general install cannot carry candidate admission",
            )

        object.__setattr__(self, "task_workspace", str(workspace))
        object.__setattr__(self, "managed_staging_base", str(managed_base))
        object.__setattr__(self, "staging_root", str(staging))

    @property
    def initial_draft(self) -> str:
        return str(self.draft_path(0))

    def draft_path(self, draft_index: int) -> Path:
        """Return the path for a given draft index (0..max_repair_drafts)."""
        if not isinstance(draft_index, int) or not 0 <= draft_index <= (
            self.max_repair_drafts
        ):
            raise CapabilityBuildError(
                "repair_budget_exhausted",
                f"draft index must be between 0 and {self.max_repair_drafts}",
            )
        return Path(self.staging_root) / f"draft-{draft_index}"

    def source_revision(self, draft_index: int) -> str:
        """Return the source revision string for a given draft."""
        self.draft_path(draft_index)  # Validate index
        return (
            f"generated-{self.lineage.lineage_id[:20]}-draft-{draft_index}"
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "lineage": self.lineage.to_dict(),
            "search_evidence": self.search_evidence.to_dict(),
            "task_workspace": self.task_workspace,
            "managed_staging_base": self.managed_staging_base,
            "staging_root": self.staging_root,
            "initial_draft": self.initial_draft,
            "install_scope": self.install_scope,
            "publish_policy": self.publish_policy,
            "candidate_admission": (
                None
                if self.candidate_admission is None
                else {
                    "builder_launch_id": (
                        self.candidate_admission.builder_launch_id
                    ),
                    "child_run_id": self.candidate_admission.child_run_id,
                    "child_start_hash": (
                        self.candidate_admission.child_start_hash
                    ),
                    "expected_entry_kinds": list(
                        self.candidate_admission.expected_entry_kinds
                    ),
                }
            ),
            "max_repair_drafts": self.max_repair_drafts,
            "required_artifacts": [
                PACK_MANIFEST_NAME,
                GENERATED_WORKER_PATH,
                "schemas/<tool-id>.schema.json",
                HAPPY_PATH_TEST,
                INVALID_INPUT_TEST,
            ],
            "worker_protocol": "deskpet-json-tool-v1",
            "generated_execution_profile": "brokered-effect-v1",
            "output_contract": (
                "candidate-draft-receipt-v1"
                if self.publish_policy == "candidate_only"
                else "capability-manager-install-request-v1"
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CapabilityBuildLaunch:
        lineage = value.get("lineage")
        search = value.get("search_evidence")
        if not isinstance(lineage, Mapping) or not isinstance(search, Mapping):
            raise CapabilityBuildError(
                "invalid_builder_payload",
                "builder lineage and search evidence are required",
            )

        policy = str(value.get("publish_policy") or "general_install")
        if policy == "candidate_only":
            raise CapabilityBuildError(
                "candidate_launch_requires_host_admission",
                "candidate-only launch cannot be reconstructed from model JSON",
            )

        launch = cls(
            schema_version=int(value.get("schema_version") or 0),
            lineage=CapabilityBuildLineage.from_dict(lineage),
            search_evidence=CapabilityBuildSearchEvidence.from_dict(search),
            task_workspace=str(value.get("task_workspace") or ""),
            managed_staging_base=str(
                value.get("managed_staging_base") or ""
            ),
            staging_root=str(value.get("staging_root") or ""),
            install_scope=str(
                value.get("install_scope") or "run"
            ),  # type: ignore[arg-type]
            publish_policy=policy,  # type: ignore[arg-type]
            max_repair_drafts=int(value.get("max_repair_drafts") or -1),
        )

        # Validate initial_draft if provided
        if value.get("initial_draft") not in {None, launch.initial_draft}:
            raise CapabilityBuildError(
                "builder_staging_mismatch",
                "initial draft path changed after builder admission",
            )
        return launch


@dataclass(frozen=True, slots=True)
class CapabilityManagerInstallRequest:
    """Install request sent to the capability manager after validation.

    Contains source location, scope, and repair ancestry if applicable.
    """

    operation_kind: BuildOperationKind
    source_type: str
    source_uri: str
    source_revision: str
    scope: Literal["run", "project", "user"]
    scope_key: str
    expected_pack_id: str
    generated: bool
    parent_version: str | None = None
    parent_manifest_hash: str | None = None
    failure_receipt_ref: str | None = None

    def __post_init__(self) -> None:
        if self.operation_kind not in {"install", "repair"}:
            raise CapabilityBuildError(
                "invalid_install_request",
                f"unsupported operation kind: {self.operation_kind}",
            )
        if self.source_type != "local":
            raise CapabilityBuildError(
                "invalid_install_request",
                "generated packs must enter the manager through a local source",
            )
        if self.scope not in {"run", "project", "user"}:
            raise CapabilityBuildError(
                "invalid_install_request",
                f"unsupported install scope: {self.scope}",
            )
        _required_text(self.scope_key, "scope_key")
        _required_text(self.expected_pack_id, "expected_pack_id")
        if self.generated is not True:
            raise CapabilityBuildError(
                "invalid_install_request",
                "generated capability requests must set generated=true",
            )

        repair = (
            self.parent_version,
            self.parent_manifest_hash,
            self.failure_receipt_ref,
        )
        if self.operation_kind == "repair":
            if not all(repair):
                raise CapabilityBuildError(
                    "repair_lineage_required",
                    "repair install request lacks immutable parent lineage",
                )
            _digest(self.parent_manifest_hash, "parent_manifest_hash")
        elif any(value is not None for value in repair):
            raise CapabilityBuildError(
                "invalid_install_request",
                "install request cannot carry repair ancestry",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "operation_kind": self.operation_kind,
            "source": {
                "source_type": self.source_type,
                "uri": self.source_uri,
                "revision": self.source_revision,
            },
            "scope": self.scope,
            "scope_key": self.scope_key,
            "expected_pack_id": self.expected_pack_id,
            "generated": self.generated,
            "parent_version": self.parent_version,
            "parent_manifest_hash": self.parent_manifest_hash,
            "failure_receipt_ref": self.failure_receipt_ref,
        }

    def to_tool_args(self) -> dict[str, JsonValue]:
        """Convert to tool call arguments format."""
        args: dict[str, JsonValue] = {
            "source_type": self.source_type,
            "uri": self.source_uri,
            "revision": self.source_revision,
            "scope": self.scope,
            "expected_pack_id": self.expected_pack_id,
            "generated": True,
        }
        if self.operation_kind == "repair":
            args.update(
                {
                    "parent_version": self.parent_version,
                    "parent_manifest_hash": self.parent_manifest_hash,
                    "failure_receipt_ref": self.failure_receipt_ref,
                }
            )
        return args


@dataclass(frozen=True, slots=True)
class CapabilityBuildEvidence:
    """Validated draft evidence with all cryptographic hashes.

    This is the immutable proof that a draft passed all host validation checks
    and is ready for installation or candidate receipt storage.
    """

    lineage_id: str
    draft_index: int
    material_fingerprint: str
    validated_draft_hash: str
    manifest_hash: str
    archive_hash: str
    file_set_hash: str
    effect_topology_hash: str
    archive_bytes: bytes
    checks: tuple[str, ...]
    install_request: CapabilityManagerInstallRequest
    search_receipt_ref: str
    catalog_stamp_fingerprint: str
    original_objective_fingerprint: str
    original_args_fingerprint: str

    def __post_init__(self) -> None:
        _digest(self.lineage_id, "lineage_id")
        _digest(self.material_fingerprint, "material_fingerprint")
        _digest(self.validated_draft_hash, "validated_draft_hash")
        _digest(self.manifest_hash, "manifest_hash")
        _digest(self.archive_hash, "archive_hash")
        _digest(self.file_set_hash, "file_set_hash")
        _digest(self.effect_topology_hash, "effect_topology_hash")

        if (
            not isinstance(self.archive_bytes, bytes)
            or hashlib.sha256(self.archive_bytes).hexdigest() != self.archive_hash
        ):
            raise CapabilityBuildError(
                "invalid_builder_evidence",
                "candidate archive bytes do not match the frozen archive hash",
            )

        _digest(self.search_receipt_ref, "search_receipt_ref")
        _digest(
            self.catalog_stamp_fingerprint,
            "catalog_stamp_fingerprint",
        )
        _digest(
            self.original_objective_fingerprint,
            "original_objective_fingerprint",
        )
        _digest(self.original_args_fingerprint, "original_args_fingerprint")

        if not 0 <= self.draft_index <= MAX_REPAIR_DRAFTS:
            raise CapabilityBuildError(
                "invalid_builder_evidence",
                "draft index exceeds the bounded repair protocol",
            )

        if not self.checks or len(self.checks) != len(set(self.checks)):
            raise CapabilityBuildError(
                "invalid_builder_evidence",
                "validation checks must be non-empty and unique",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "lineage_id": self.lineage_id,
            "draft_index": self.draft_index,
            "repair_round": self.draft_index,
            "material_fingerprint": self.material_fingerprint,
            "validated_draft_hash": self.validated_draft_hash,
            "manifest_hash": self.manifest_hash,
            "archive_hash": self.archive_hash,
            "file_set_hash": self.file_set_hash,
            "effect_topology_hash": self.effect_topology_hash,
            "checks": list(self.checks),
            "install_request": self.install_request.to_dict(),
            "search_receipt_ref": self.search_receipt_ref,
            "catalog_stamp_fingerprint": self.catalog_stamp_fingerprint,
            "original_objective_fingerprint": (
                self.original_objective_fingerprint
            ),
            "original_args_fingerprint": self.original_args_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class CapabilityBuildCompletion:
    """Child terminal value signaling which draft to validate and finalize.

    The child builder workflow returns this JSON value when it completes successfully.
    """

    lineage_id: str
    draft_index: int
    draft_path: str
    schema_version: int = CAPABILITY_BUILD_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_BUILD_PROTOCOL_VERSION:
            raise CapabilityBuildError(
                "unsupported_builder_protocol",
                f"unsupported builder completion protocol: {self.schema_version}",
            )
        _digest(self.lineage_id, "lineage_id")
        if not 0 <= self.draft_index <= MAX_REPAIR_DRAFTS:
            raise CapabilityBuildError(
                "repair_budget_exhausted",
                f"draft index must be between 0 and {MAX_REPAIR_DRAFTS}",
            )
        object.__setattr__(
            self,
            "draft_path",
            str(Path(_required_text(self.draft_path, "draft_path")).expanduser()),
        )

    @classmethod
    def from_value(cls, value: object) -> CapabilityBuildCompletion:
        """Parse completion from child terminal value (JSON string or object)."""
        import json

        payload: object = value
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise CapabilityBuildError(
                    "builder_completion_invalid",
                    "builder terminal value must be one JSON object",
                ) from exc

        if not isinstance(payload, Mapping):
            raise CapabilityBuildError(
                "builder_completion_invalid",
                "builder terminal value must be an object",
            )

        expected = {
            "schema_version",
            "lineage_id",
            "draft_index",
            "draft_path",
        }
        if set(payload) != expected:
            raise CapabilityBuildError(
                "builder_completion_invalid",
                f"builder terminal keys must be exactly {sorted(expected)}",
            )

        return cls(
            schema_version=int(payload.get("schema_version") or 0),
            lineage_id=str(payload.get("lineage_id") or ""),
            draft_index=int(payload.get("draft_index") or 0),
            draft_path=str(payload.get("draft_path") or ""),
        )

    def validate_for(self, launch: CapabilityBuildLaunch) -> None:
        """Validate that this completion belongs to the given launch."""
        if self.lineage_id != launch.lineage.lineage_id:
            raise CapabilityBuildError(
                "builder_lineage_mismatch",
                "builder completion belongs to another parent lineage",
            )
        expected = launch.draft_path(self.draft_index).resolve(strict=False)
        actual = Path(self.draft_path).resolve(strict=False)
        if actual != expected:
            raise CapabilityBuildError(
                "builder_staging_mismatch",
                "builder completion points outside its host-selected draft",
            )


__all__ = [
    "CAPABILITY_BUILD_PROTOCOL_VERSION",
    "CAPABILITY_STAGING_DIRECTORY",
    "GENERATED_WORKER_PATH",
    "HAPPY_PATH_TEST",
    "INVALID_INPUT_TEST",
    "MAX_REPAIR_DRAFTS",
    "MIN_SUFFICIENT_EXECUTABLE_SCORE",
    "PACK_MANIFEST_NAME",
    "BuildOperationKind",
    "CapabilityBuildCandidateAdmissionV1",
    "CapabilityBuildCompletion",
    "CapabilityBuildEvidence",
    "CapabilityBuildLaunch",
    "CapabilityBuildLineage",
    "CapabilityBuildSearchEvidence",
    "CapabilityManagerInstallRequest",
]
