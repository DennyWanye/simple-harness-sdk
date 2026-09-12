# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Whole-criterion projection over durable contracts and complete original bytes.

This module never grades a claim, edits history, or consults a live workspace.
Missing historical execution metadata is an explicit refusal, not a best guess.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from ..artifacts.store import ArtifactStore, ArtifactStoreError, read_verified
from ..contracts import ContractError, ResultEnvelope, Task
from ..contracts.assessments import CriterionAssessmentV1, thaw_json
from ..contracts.fragments import FragmentProposalV1, ScopeProjectionV1, TaskRevisionV1
from ..contracts.models import canonical_json, sha256_hex
from ..memory.source_dependencies import source_dependencies_for, source_versions_current_issues
from ..storage.store import DispatchIntent, Store
from ..verification.assessments import (
    _frozen_contract,
    criterion_id,
    mission_contract_revision,
    task_contract_revision,
)
from ..verification.evidence_resolver import EvidenceResolver, _safe_path

# Known graph bookkeeping cannot change permission or the meaning of a criterion.
# New execution-bearing context keys must be explicitly adjudicated before reuse.
_BOOKKEEPING = frozenset(
    {
        "graph_version",
        "change_id",
        "proposed_by_attempt",
        "supersedes_task",
        "supersede_depth",
        "replaced_by",
    }
)
_EXECUTION_CONTEXT = frozenset({"role", "fragment_validation"})


def _task_contract(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.id,
        **{
            name: getattr(task, name)
            for name in (
                "kind",
                "goal",
                "rationale",
                "success_criteria",
                "verification_policy",
                "outputs",
            )
        },
    }


def _path(path: Any) -> str:
    if not isinstance(path, str) or not _safe_path(path):
        raise ContractError("scope_not_derivable: noncanonical material path")
    return path


def freeze_fragment_execution(
    store: Store,
    artifact_store: ArtifactStore,
    *,
    task: Task,
    intent_config: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    retry_of: str | None,
    validated_input_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """System-only creation hook. Never apply it when replaying an old intent.

    ``validated_input_paths`` comes only from Commit's independently checked
    selection receipt, never from intent_config or model metadata. It maps a real
    artifact ID to its one approved namespace path, without changing its bytes.
    The CAS manifests include whole input files, not selected excerpts. Retried
    workspaces need their original complete snapshot; v1 refuses pytest projection
    from a retry rather than inventing such a snapshot from today's directory.
    """
    mission = store.get_mission(task.mission_id)
    if mission is None:
        raise ContractError("fragment mission unavailable")
    files: dict[str, dict[str, Any]] = {}
    for path, text in (mission.final_report or {}).get("workspace_seed", {}).items():
        _path(path)
        if not isinstance(text, str):
            raise ContractError("fragment seed must be text")
        data = text.encode("utf-8")
        files[path] = {"content_hash": artifact_store.put_bytes(data), "kind": "seed"}
    validated_paths = dict(validated_input_paths or {})
    if set(validated_paths) - {item.get("artifact_id") for item in inputs}:
        raise ContractError("fragment validated input path has no actual input")
    mounted_paths: set[str] = set()
    for item in inputs:
        artifact = store.get_artifact(item.get("artifact_id", ""))
        if (
            artifact is None
            or artifact.mission_id != mission.id
            or artifact.content_hash != item.get("content_hash")
            or validated_paths.get(artifact.id, artifact.path) != item.get("path")
        ):
            raise ContractError("fragment frozen input identity mismatch")
        mounted = _path(item["path"])
        folded = unicodedata.normalize("NFC", mounted).casefold()
        if folded in mounted_paths:
            raise ContractError("fragment duplicate input mount path")
        mounted_paths.add(folded)
        data = read_verified(artifact)
        files[mounted] = {
            "content_hash": artifact_store.put_bytes(data),
            "kind": "artifact",
            "artifact_id": artifact.id,
            "original_path": artifact.path,
        }
    marker = task.context.get("fragment_validation")
    if isinstance(marker, Mapping):
        receipt = store.get_receipt(marker.get("projection_receipt_id", ""))
        if (
            receipt is None
            or receipt.get("validation_task_id") != task.id
            or receipt.get("fragment_id") != marker.get("fragment_id")
            or receipt.get("projection_hash") != sha256_hex(receipt.get("projection"))
        ):
            raise ContractError("fragment validation input receipt mismatch")
        # The new intent must describe the actual complete materialized closure,
        # including unchanged files that the new Worker may never list as outputs.
        read_input_closure(store, artifact_store, receipt["projection"])
        files = thaw_json(receipt["projection"]["input_closure"])
    domain = store.get_mission_domain(mission.id)
    body = {
        "schema_version": 1,
        "task_id": task.id,
        "allowed_tools": list(task.allowed_tools),
        "budget": task.budget.to_json(),
        "dependency_ids": list(task.dependency_ids),
        "context": {key: value for key, value in task.context.items() if key not in _BOOKKEEPING},
        "domain": None if domain is None else domain["json"],
        "policy_binding": store.get_mission_policy(mission.id),
        "source_versions": dict(intent_config.get("source_versions", {})),
        "source_roots": list(intent_config.get("source_roots", ())),
        "inputs": [dict(item) for item in inputs],
        "files": files,
        "retry_of": retry_of,
    }
    return {**body, "constraints_revision": sha256_hex(body)}


def _revision(store: Store, intent: DispatchIntent) -> TaskRevisionV1:
    attempt = store.get_attempt(intent.subject_id)
    task = None if attempt is None else store.get_task(attempt.task_id)
    mission = store.get_mission(intent.mission_id)
    if (
        intent.kind != "attempt"
        or attempt is None
        or task is None
        or mission is None
        or attempt.mission_id != mission.id
        or task.mission_id != mission.id
    ):
        raise ContractError("fragment origin intent binding mismatch")
    contract = _frozen_contract(intent.config)
    if contract["task_id"] != task.id:
        raise ContractError("fragment origin contract identity mismatch")
    execution = intent.config.get("fragment_execution")
    if not isinstance(execution, Mapping):
        raise ContractError("scope_not_derivable: original execution snapshot unavailable")
    body = {key: value for key, value in execution.items() if key != "constraints_revision"}
    if execution.get("constraints_revision") != sha256_hex(body) or body.get("task_id") != task.id:
        raise ContractError("fragment constraints revision mismatch")
    if body.get("schema_version") != 1 or type(body.get("schema_version")) is not int:
        raise ContractError("scope_not_derivable: unknown execution snapshot version")
    if set(body.get("context", {})) - _EXECUTION_CONTEXT:
        raise ContractError("scope_not_derivable: unknown execution context")
    domain = store.get_mission_domain(mission.id)
    if body.get("domain") != (None if domain is None else domain["json"]):
        raise ContractError("fragment frozen domain mismatch")
    if body.get("source_versions") != dict(intent.config.get("source_versions", {})) or body.get(
        "source_roots"
    ) != list(intent.config.get("source_roots", ())):
        raise ContractError("fragment source snapshot mismatch")
    rev = task_contract_revision(contract)
    return TaskRevisionV1(
        mission.id,
        task.id,
        intent.intent_id,
        attempt.task_version,
        contract,
        rev,
        mission_contract_revision(mission),
        body,
        execution["constraints_revision"],
        tuple(
            {
                "id": criterion_id(rev, ordinal, text),
                "ordinal": ordinal,
                "text": text,
                "kind": text.split(":", 1)[0] if ":" in text else "free",
            }
            for ordinal, text in enumerate(contract["success_criteria"], 1)
        ),
    )


def revision_for_result(
    store: Store, artifact_store: ArtifactStore, result_id: str
) -> TaskRevisionV1:
    """Historical origin is allowed to be failed, cancelled, or superseded."""
    del artifact_store  # signature shares the projection reader's deployment handle
    stored = store.get_result(result_id)
    intent = None if stored is None else store.get_intent_for_subject(stored.envelope.attempt_id)
    if stored is None or intent is None:
        raise ContractError("fragment origin result unavailable")
    revision = _revision(store, intent)
    if (
        stored.envelope.task_id != revision.task_id
        or stored.envelope.mission_id != revision.mission_id
    ):
        raise ContractError("fragment origin result binding mismatch")
    return revision


def current_task_revision(store: Store, task: Task) -> TaskRevisionV1:
    attempts = store.list_attempts(task.id)
    intent = None if not attempts else store.get_intent_for_subject(attempts[-1].id)
    if intent is None:
        raise ContractError("fragment consumer has no frozen execution contract")
    revision = _revision(store, intent)
    execution = revision.execution_constraints
    if (
        revision.task_contract_revision != task_contract_revision(_task_contract(task))
        or tuple(execution["allowed_tools"]) != task.allowed_tools
        or thaw_json(execution["budget"]) != task.budget.to_json()
        or tuple(execution["dependency_ids"]) != task.dependency_ids
        or thaw_json(execution["context"])
        != {k: v for k, v in task.context.items() if k not in _BOOKKEEPING}
    ):
        raise ContractError("fragment consumer contract changed")
    return revision


def _current_sources(
    store: Store, cas: ArtifactStore, revision: TaskRevisionV1, envelope: ResultEnvelope
) -> None:
    versions: dict[str, tuple[str, ...]] = {
        path: (version,)
        for path, version in revision.execution_constraints["source_versions"].items()
    }
    inherited, issues = source_dependencies_for(
        store,
        mission_id=revision.mission_id,
        evidence_refs=(),
        # The envelope declares dependencies; persisted KnowledgeRecord.dependencies
        # are traversed recursively by this helper. ClaimProposal has no such field.
        used_knowledge=envelope.used_knowledge,
    )
    for path, hashes in inherited.items():
        versions[path] = tuple(sorted(set(versions.get(path, ())) | set(hashes)))
    issues.extend(source_versions_current_issues(store, revision.mission_id, versions, cas))
    if issues:
        code = "ERROR" if any(item["code"] == "ERROR" for item in issues) else "stale_source"
        raise ContractError(f"fragment {code}: source lineage is unavailable or no longer current")


def read_input_closure(
    store: Store, cas: ArtifactStore, projection: Mapping[str, Any]
) -> dict[str, bytes]:
    """Only CAS-backed full bytes; caller must use the frozen projection's paths."""
    mission_id = projection["origin_revision"]["mission_id"]
    mission = store.get_mission(mission_id)
    if mission is None:
        raise ContractError("fragment mission unavailable")
    result: dict[str, bytes] = {}
    spellings: set[str] = set()
    for path, item in projection["input_closure"].items():
        _path(path)
        folded = unicodedata.normalize("NFC", path).casefold()
        if folded in spellings:
            raise ContractError("fragment ambiguous input path alias")
        spellings.add(folded)
        try:
            if item["kind"] == "source":
                source = EvidenceResolver(store, cas).read_source(
                    tenant_id=mission.tenant_id,
                    mission_id=mission.id,
                    path=path,
                    version=item["content_hash"],
                    source_roots=projection["origin_revision"]["execution_constraints"][
                        "source_roots"
                    ],
                )
                if source.status != "resolved" or source.data is None:
                    raise ContractError("fragment ERROR: source unreadable")
                result[path] = source.data
            else:
                if item.get("artifact_id"):
                    artifact = store.get_artifact(item["artifact_id"])
                    if (
                        artifact is None
                        or artifact.mission_id != mission_id
                        or artifact.content_hash != item["content_hash"]
                    ):
                        raise ContractError("fragment material_unavailable: artifact identity")
                    result[path] = read_verified(artifact)
                else:
                    result[path] = cas.read(item["content_hash"])
        except (ArtifactStoreError, OSError) as error:
            raise ContractError(
                "fragment material_unavailable: original bytes unavailable"
            ) from error
    return result


def fragment_validation_layout(projection: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Relocate output file predicates, never source locators or executable tests.

    Original inputs retain their paths and CAS identities. A file predicate is
    rebound as a whole to its new output; arbitrary pytest code cannot safely be
    rewritten this way or tested against the old input in lieu of the new output.
    """
    criteria = projection["criteria"]
    outputs = list(projection["origin_revision"]["contract"]["outputs"])
    outputs.extend(
        item["text"].removeprefix("file:") for item in criteria if item["kind"] == "file"
    )
    if outputs and any(item["kind"] == "pytest" for item in criteria):
        raise ContractError(
            "scope_not_derivable: pytest output relocation needs an independent contract"
        )
    prefix = "fragment-output/" + projection["fragment_id"].removeprefix("fragment-")
    folded_prefix = unicodedata.normalize("NFC", prefix).casefold()
    for path in projection["input_closure"]:
        folded = unicodedata.normalize("NFC", path).casefold()
        if (
            folded == folded_prefix
            or folded.startswith(folded_prefix + "/")
            or folded_prefix.startswith(folded + "/")
        ):
            raise ContractError("fragment output namespace overlaps original input")
    paths = {_path(path): _path(prefix + "/" + path) for path in sorted(set(outputs))}
    if len({unicodedata.normalize("NFC", path).casefold() for path in paths}) != len(paths):
        raise ContractError("fragment ambiguous output path alias")
    roots = projection["origin_revision"]["execution_constraints"]["source_roots"]
    for path in paths.values():
        folded = unicodedata.normalize("NFC", path).casefold()
        for root in roots:
            protected = unicodedata.normalize("NFC", root).casefold().rstrip("/")
            if folded == protected or folded.startswith(protected + "/"):
                raise ContractError("fragment output namespace overlaps protected source root")
    texts = [
        "file:" + paths[item["text"].removeprefix("file:")]
        if item["kind"] == "file"
        else item["text"]
        for item in criteria
    ]
    return paths, texts


def project_fragment(
    store: Store, artifact_store: ArtifactStore, proposal: FragmentProposalV1
) -> ScopeProjectionV1:
    # Reparse even typed callers: strict contracts are not just a JSON boundary.
    proposal = FragmentProposalV1.from_json(proposal.to_json())
    origin = proposal.origin
    revision = revision_for_result(store, artifact_store, origin["result_id"])
    stored = store.get_result(origin["result_id"])
    assert stored is not None
    if (
        revision.revision_id != origin["task_revision_id"]
        or revision.task_id != origin["task_id"]
        or revision.mission_id != origin["mission_id"]
        or stored.envelope.attempt_id != origin["attempt_id"]
    ):
        raise ContractError("fragment origin revision mismatch")
    selected = set(proposal.criterion_ids)
    if selected - {item["id"] for item in revision.criteria}:
        raise ContractError("fragment criterion does not belong to origin")
    criteria = tuple(item for item in revision.criteria if item["id"] in selected)
    if revision.contract["kind"] != "work" or any(
        item["kind"] not in {"file", "cite", "pytest"} for item in criteria
    ):
        raise ContractError(
            "scope_not_derivable: criterion requires an explicit independent contract"
        )
    if (
        any(item["kind"] == "pytest" for item in criteria)
        and revision.execution_constraints["retry_of"] is not None
    ):
        raise ContractError("scope_not_derivable: retry workspace closure was not frozen")
    for ref in proposal.claim_refs:
        claim = store.get_claim(ref["claim_id"])
        if (
            claim is None
            or claim.result_id != origin["result_id"]
            or claim.mission_id != revision.mission_id
            or claim.source_task != revision.task_id
            or claim.source_attempt != origin["attempt_id"]
            or claim.version != ref["claim_revision"]
        ):
            raise ContractError("fragment claim binding or revision mismatch")
    _current_sources(store, artifact_store, revision, stored.envelope)
    closure = thaw_json(revision.execution_constraints["files"])
    for artifact_id in stored.artifacts:
        artifact = store.get_artifact(artifact_id)
        if artifact is None or (artifact.mission_id, artifact.task_id, artifact.attempt_id) != (
            revision.mission_id,
            revision.task_id,
            origin["attempt_id"],
        ):
            raise ContractError("fragment material_unavailable: result artifact binding")
        closure[_path(artifact.path)] = {
            "kind": "artifact",
            "artifact_id": artifact.id,
            "content_hash": artifact.content_hash,
        }
    for path, version in revision.execution_constraints["source_versions"].items():
        if path in closure and closure[path]["content_hash"] != version:
            raise ContractError("fragment source bytes were overwritten")
        closure[_path(path)] = {"kind": "source", "content_hash": version}
    temporary = {"origin_revision": revision.to_json(), "input_closure": closure}
    full_bytes = read_input_closure(store, artifact_store, temporary)
    for ref in proposal.material_refs:
        if ref["kind"] == "artifact":
            artifact = store.get_artifact(ref["artifact_id"])
            if (
                artifact is None
                or artifact.id not in stored.artifacts
                or artifact.content_hash != ref["content_hash"]
            ):
                raise ContractError("fragment material does not belong to original result")
            data = full_bytes[artifact.path]
            if ref["byte_end_exclusive"] > len(data):
                raise ContractError("fragment material span out of range")
            try:
                data.decode("utf-8")
                data[: ref["byte_start"]].decode("utf-8")
                data[ref["byte_start"] : ref["byte_end_exclusive"]].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ContractError(
                    "fragment material range must preserve UTF-8 boundaries"
                ) from error
        else:
            raw = next(
                (
                    row
                    for row in store.list_criterion_assessments(
                        revision.mission_id, result_id=origin["result_id"]
                    )
                    if row["receipt_id"] == ref["receipt_id"]
                ),
                None,
            )
            if raw is None:
                raise ContractError("fragment citation receipt is not bound to origin")
            assessment = CriterionAssessmentV1.from_json(raw)
            if (
                assessment.output_ref != origin["result_id"]
                or assessment.task_contract_revision != revision.task_contract_revision
            ):
                raise ContractError("fragment citation receipt binding mismatch")
            if ref["citation_index"] >= len(assessment.evidence_refs):
                raise ContractError("fragment citation index out of range")
            evidence = assessment.evidence_refs[ref["citation_index"]]
            if (
                evidence.get("status") != "resolved"
                or evidence.get("target") not in closure
                or closure[evidence["target"]]["content_hash"] != evidence.get("source_version")
            ):
                raise ContractError("fragment citation is not a resolved frozen source")
    for item in criteria:
        target = item["text"].split(":", 1)[1].strip()
        if item["kind"] in {"file", "cite"} and _path(target) not in closure:
            raise ContractError("scope_not_derivable: complete criterion input unavailable")
        if item["kind"] == "cite" and closure[target]["kind"] != "source":
            raise ContractError("scope_not_derivable: citation has no frozen source")
    claim_refs = tuple(
        sorted(proposal.claim_refs, key=lambda item: (item["claim_id"], item["claim_revision"]))
    )
    materials = tuple(sorted(proposal.material_refs, key=lambda item: canonical_json(dict(item))))
    identity = {
        "projection_version": "whole-criterion-v1",
        "origin": dict(origin),
        "criteria": [item["id"] for item in criteria],
        "claim_refs": thaw_json(claim_refs),
        "material_refs": thaw_json(materials),
        "input_closure": closure,
        "constraints_revision": revision.constraints_revision,
    }
    projection = ScopeProjectionV1(
        "fragment-" + sha256_hex(identity),
        revision.to_json(),
        criteria,
        tuple(item for item in revision.criteria if item["id"] not in selected),
        claim_refs,
        materials,
        closure,
    )
    fragment_validation_layout(projection.to_json())
    return projection
