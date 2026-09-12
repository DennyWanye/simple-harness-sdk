# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Exact, replayable document-v4 Mission judgment trees from accepted provenance.

The catalog keeps logical paths and every cited historical version. Tree manifests
are part of the actual intent, not reconstructed from today's active source heads.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..artifacts.store import ArtifactStore, read_nofollow
from ..artifacts.workspace import Workspace, WorkspaceManager
from ..contracts import ContractError, Mission, TaskStatus
from ..contracts.models import sha256_hex
from ..governance.domains import DomainProfileV1, requires_mission_source_binding
from ..memory.source_dependencies import merge_source_versions, source_dependencies_for
from .assessments import accepted_assessments_for
from .evidence_resolver import EvidenceResolver, _safe_path

SOURCE_MOUNT = "mission-sources/"


def _fold(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _seal(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "hash": sha256_hex(body)}


def _body(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys | {"hash"}:
        raise ContractError("invalid Mission source binding fields")
    body = {k: value[k] for k in keys}
    if body.get("schema") != 1 or sha256_hex(body) != value["hash"]:
        raise ContractError("invalid Mission source binding hash")
    return body


def prepare_mission_tree(
    store: Any,
    mission: Mission,
    domain: DomainProfileV1,
    cas: ArtifactStore,
    *,
    seed: Mapping[str, str],
    files: Mapping[str, Path | bytes],
) -> dict[str, Any]:
    if not requires_mission_source_binding(domain):
        raise ContractError("Mission source binding requires document v4")
    versions: dict[str, tuple[str, ...]] = {}
    for task in store.list_tasks(mission.id):
        if task.status is not TaskStatus.COMPLETED:
            continue
        binding, rows = accepted_assessments_for(store, task=task)
        lineage, issues = source_dependencies_for(
            store,
            mission_id=mission.id,
            evidence_refs=[ref for row in rows for ref in row.evidence_refs],
            used_knowledge=binding.envelope.used_knowledge,
        )
        if issues:
            raise ContractError("Mission source lineage unavailable")
        versions = merge_source_versions(versions, lineage)
    content = {path: text.encode("utf-8") for path, text in seed.items()}
    content.update(
        {
            path: data if isinstance(data, bytes) else read_nofollow(data)
            for path, data in files.items()
        }
    )
    for path in content:
        if not _safe_path(path) or _fold(path).split("/")[0] == SOURCE_MOUNT.rstrip("/"):
            raise ContractError("Mission source mount collision")
    resolver = EvidenceResolver(store, cas)
    entries = []
    for path, hashes in sorted(versions.items()):
        for version in hashes:
            read = resolver.read_source(
                tenant_id=mission.tenant_id,
                mission_id=mission.id,
                path=path,
                version=version,
                source_roots=domain.source_roots,
            )
            if read.status != "resolved":
                raise ContractError("Mission source bytes unavailable")
            mounted = f"{SOURCE_MOUNT}{version}/{path}"
            # read_source validates registry and CAS; raw CAS preserves CRLF/CR bytes.
            content[mounted] = cas.read(version)
            entries.append(dict(path=path, version=version, mounted_path=mounted))
    folded = sorted(_fold(path) for path in content)
    if len(folded) != len(set(folded)) or any(
        b.startswith(a + "/") for a, b in zip(folded, folded[1:])
    ):
        raise ContractError("Mission source tree path collision")
    manifest = {path: cas.put_bytes(data) for path, data in sorted(content.items())}
    return {
        "mission_source_catalog": _seal(dict(schema=1, entries=entries)),
        "mission_judge_tree": _seal(dict(schema=1, files=manifest)),
        "source_roots": [SOURCE_MOUNT],
        "untrusted_sources": [entry["mounted_path"] for entry in entries],
    }


def ensure_mission_tree(
    store: Any,
    mission: Mission,
    domain: DomainProfileV1,
    workspaces: WorkspaceManager,
    config: Mapping[str, Any],
) -> Workspace:
    """Validate first; restore a wholly missing view, never overwrite an existing view."""
    try:
        return _ensure(store, mission, domain, workspaces, config)
    except ContractError:
        raise
    except (ValueError, OSError, TypeError, KeyError) as error:
        raise ContractError("Mission source tree unavailable") from error


def _ensure(
    store: Any,
    mission: Mission,
    domain: DomainProfileV1,
    workspaces: WorkspaceManager,
    config: Mapping[str, Any],
) -> Workspace:
    if not requires_mission_source_binding(domain):
        raise ContractError("Mission source binding requires document v4")
    view_id = config.get("attempt_id")
    if not isinstance(view_id, str) or not _safe_path(view_id) or "/" in view_id:
        raise ContractError("invalid Mission source view identity")
    catalog = _body(config.get("mission_source_catalog"), {"schema", "entries"})
    tree = _body(config.get("mission_judge_tree"), {"schema", "files"})
    if not isinstance(catalog["entries"], (list, tuple)) or not isinstance(tree["files"], Mapping):
        raise ContractError("invalid Mission source tree")
    if config.get("source_roots") != [SOURCE_MOUNT]:
        raise ContractError("invalid Mission source trust roots")
    mounts = []
    resolver = EvidenceResolver(store, workspaces.artifact_store)
    for entry in catalog["entries"]:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "version", "mounted_path"}:
            raise ContractError("invalid Mission source catalog entry")
        path, version, mounted = (entry[k] for k in ("path", "version", "mounted_path"))
        if mounted != f"{SOURCE_MOUNT}{version}/{path}" or tree["files"].get(mounted) != version:
            raise ContractError("invalid Mission source mount binding")
        read = resolver.read_source(
            tenant_id=mission.tenant_id,
            mission_id=mission.id,
            path=path,
            version=version,
            source_roots=domain.source_roots,
        )
        if read.status != "resolved":
            raise ContractError("Mission source bytes unavailable")
        mounts.append(mounted)
    if len(mounts) != len(set(mounts)) or not set(mounts).issubset(
        config.get("untrusted_sources", ())
    ):
        raise ContractError("invalid Mission source trust markers")
    content = {}
    for path, digest in tree["files"].items():
        if not isinstance(path, str) or not _safe_path(path):
            raise ContractError("invalid Mission source manifest path")
        if _fold(path).startswith(SOURCE_MOUNT) and path not in mounts:
            raise ContractError("unbound Mission source mount")
        content[path] = workspaces.artifact_store.read(digest)
    target = workspaces.root / f"{view_id}-verify"
    if target.is_symlink():
        raise ContractError("invalid Mission source view link")
    if not target.exists():
        return workspaces.integrated_copy(view_id, seed={}, files=content)
    view = workspaces.verification_view(view_id)
    # Exact identity must not use Workspace.list_files: that user-facing listing
    # deliberately omits caches and .git, which remain readable by a direct tool path.
    actual: set[str] = set()
    for directory, dirs, files in os.walk(view.root, followlinks=False):
        for name in [*dirs, *files]:
            child = Path(directory) / name
            if child.is_symlink():
                raise ContractError("Mission source tree changed")
        actual.update((Path(directory) / name).relative_to(view.root).as_posix() for name in files)
    if actual != set(content):
        raise ContractError("Mission source tree changed")
    for path, digest in tree["files"].items():
        if hashlib.sha256(read_nofollow(view.resolve(path))).hexdigest() != digest:
            raise ContractError("Mission source tree changed")
    return view
