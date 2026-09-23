# SPDX-License-Identifier: Apache-2.0
"""Original execution resources and exact material mounts for TaskGraph Workers."""
from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from simple_harness.agents import AgentConfig

from ..artifacts.input_bindings import TargetRules
from ..artifacts.store import ArtifactStoreError, read_nofollow, read_verified
from ..artifacts.versioning import ArtifactConflict, UpstreamInput
from ..contracts.models import sha256_hex
from ..scheduling.allocator import OPEN_ATTEMPT_STATES
from ..storage.store import Store, StoreConflict, StoreError
from ..verification.evidence_resolver import _safe_path


def require_mounts(inputs: Sequence[UpstreamInput], rules: TargetRules, *,
                   supplementary: Mapping[str, bytes] | None = None) -> None:
    """Reject collisions before a dict/update or a filesystem write loses them.

    DATA and Selection mounts were independently selected by their original
    authorities. Additional frozen source/fragment bytes may share an exact
    path only when they are byte-identical; they cannot replace selected DATA.
    """
    mounted: dict[str, tuple[str, str]] = {}
    def add(path: str, digest: str, *, additional: bool) -> None:
        if not _safe_path(path):
            raise ArtifactConflict("TASKGRAPH_MATERIAL_PATH_INVALID")
        key = unicodedata.normalize("NFC", path)
        if rules.case_insensitive:
            key = key.casefold()
        old = mounted.get(key)
        if old is not None:
            if not additional or old != (path, digest):
                raise ArtifactConflict("TASKGRAPH_MATERIAL_PATH_CONFLICT")
            return
        if any(key.startswith(other + "/") or other.startswith(key + "/") for other in mounted):
            raise ArtifactConflict("TASKGRAPH_MATERIAL_FILE_DIRECTORY_CONFLICT")
        mounted[key] = (path, digest)
    for item in inputs:
        add(item.path, item.content_hash, additional=False)
    for path, data in (supplementary or {}).items():
        add(path, hashlib.sha256(data).hexdigest(), additional=True)


def verify_materialized(root: Path, inputs: Sequence[UpstreamInput]) -> None:
    """Check actual mounts at binding/recovery without following parent symlinks."""
    if root.is_symlink() or not root.is_dir():
        raise ArtifactConflict("TASKGRAPH_WORKSPACE_ROOT_INVALID")
    for item in inputs:
        if not _safe_path(item.path):
            raise ArtifactConflict("TASKGRAPH_MATERIAL_PATH_INVALID")
        path = root
        for part in item.path.split("/"):
            path = path / part
            if path.is_symlink():
                raise ArtifactConflict("TASKGRAPH_MATERIAL_SYMLINK")
        try:
            data = read_nofollow(path)
        except ArtifactStoreError as error:
            raise ArtifactConflict("TASKGRAPH_MATERIAL_FILE_UNAVAILABLE") from error
        if hashlib.sha256(data).hexdigest() != item.content_hash:
            raise ArtifactConflict("TASKGRAPH_MATERIAL_HASH_CHANGED")


class TaskGraphExecutionGuard:
    """Fixed adapter over installed tools, original CAS and deployment limits.

    Current permission intersection is checked by TaskGraphExecutionImports;
    real reservation and per-call Provider/Tool/Action admission remain in the
    original Commit/runtime path. No planning grant is treated as execution.
    """
    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator, self.store = orchestrator, orchestrator.store

    def __call__(self, store: Store, view: Any, admission: Any, manifest: Any,
                 config: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]], input_hash: str) -> None:
        orch = self.orchestrator
        if store is not self.store or not store.connection.in_transaction:
            raise StoreError("TASKGRAPH_EXECUTION_RECHECK_TRANSACTION_REQUIRED")
        task = store.get_task(str(admission.task_id))
        if task is None or task.mission_id != str(view.network.mission_id):
            raise StoreError("TASKGRAPH_EXECUTION_TASK_OWNER_MISMATCH")
        dispatcher = orch._dispatch_for(task.mission_id)
        if dispatcher is None:
            raise StoreError("TASKGRAPH_MISSION_DISPATCHER_UNAVAILABLE")
        rules = dispatcher.target_rules_for(task.id)
        if not isinstance(rules, TargetRules):
            raise StoreError("TARGET_RULES_UNAVAILABLE")
        if orch.commit._source_artifact_store is None:
            raise StoreError("TASKGRAPH_ORIGINAL_ARTIFACT_STORE_REQUIRED")
        frozen = tuple(UpstreamInput.from_json(item) for item in inputs)
        require_mounts(frozen, rules)
        for item in frozen:
            artifact = store.get_artifact(item.artifact_id)
            if (artifact is None or artifact.mission_id != task.mission_id
                    or artifact.task_id != item.task_id or artifact.content_hash != item.content_hash):
                raise StoreError("TASKGRAPH_MATERIAL_ARTIFACT_IDENTITY_CHANGED")
            read_verified(artifact)
        if sha256_hex(config.get("message")) != input_hash:
            raise StoreConflict("TASKGRAPH_EXECUTION_MESSAGE_CHANGED")
        profile_id = config.get("runtime_profile_id")
        pool = orch.assembled.pools.get(profile_id)
        if pool is None:
            raise StoreError("TASKGRAPH_RUNTIME_PROFILE_UNAVAILABLE")
        pool.bridge.check_tools(AgentConfig.from_json(dict(config["agent_config"])))
        mission = store.get_mission(task.mission_id)
        if mission is None or orch._active_source_binding(task.mission_id) != {
                key: config[key] for key in ("source_versions", "source_roots") if key in config}:
            raise StoreConflict("TASKGRAPH_EXECUTION_SOURCE_BINDING_CHANGED")
        # Count every original open Attempt, with the same explicit Selection
        # waiting exclusion as original create_attempt. Caller-supplied optional
        # limits cannot bypass the actual installed deployment cap.
        waiting = orch.commit.selection_waiting_ids()
        opened = store.count_attempts_by_status(*(str(state) for state in OPEN_ATTEMPT_STATES))
        opened -= sum(orch.commit._require_attempt(identity).status in OPEN_ATTEMPT_STATES for identity in waiting)
        if opened >= orch._config.max_running_attempts:
            raise StoreConflict("TASKGRAPH_EXECUTION_CAPACITY_REACHED")
