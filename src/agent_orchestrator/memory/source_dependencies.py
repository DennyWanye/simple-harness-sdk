# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Read-only source lineage and a separate, current-source acceptance fence.

Historical attribution is not regraded when a source changes. Dependency expansion
keeps every version, while currentness is queried separately. No source text is
read except through EvidenceResolver.read_source at the direct-citation fence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..artifacts.store import ArtifactStore
from ..contracts import ContractError, SourceCitation
from ..contracts.assessments import content_hash
from ..contracts.models import canonical_json
from ..governance.domains import CODE_PROFILE, DOC_DOMAIN, DomainProfileV1
from ..verification.evidence_resolver import EvidenceResolver, _safe_path

if TYPE_CHECKING:
    from ..storage.store import Store

SOURCE_PROVENANCE_ISSUES = "source_provenance_issues"
_ISSUE_CODES = frozenset({"stale_source", "unknown_source_provenance", "ERROR"})


def merge_source_versions(
    *mappings: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    """Canonical union; never replace an older hash at the same path."""
    merged: dict[str, set[str]] = {}
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            raise ContractError("source_versions must be an object")
        for path, versions in mapping.items():
            if not isinstance(path, str) or not _safe_path(path):
                raise ContractError("source_versions has an invalid path")
            if not isinstance(versions, (list, tuple)) or not versions:
                raise ContractError("source_versions values must be nonempty version arrays")
            merged.setdefault(path, set()).update(
                content_hash(version, "source version") for version in versions
            )
    return {path: tuple(sorted(versions)) for path, versions in sorted(merged.items())}


def _issue(code: str, reason: str, **identity: str) -> dict[str, Any]:
    return {"code": code, "reason": reason, **identity}


def _ordered(issues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique = {canonical_json(dict(issue)): dict(issue) for issue in issues}
    return [unique[key] for key in sorted(unique)]


def _domain(store: Store, mission_id: str) -> DomainProfileV1:
    frozen = store.get_mission_domain(mission_id)
    if frozen is None:
        return CODE_PROFILE  # original code Missions predate domain snapshots
    domain = DomainProfileV1.from_json(frozen["json"])
    if (domain.id, domain.version) != (frozen["domain_id"], frozen["domain_version"]):
        raise ContractError("source domain snapshot identity mismatch")
    return domain


def _refs_versions(
    refs: Sequence[Mapping[str, Any]], *, mission_id: str, tenant_id: str
) -> dict[str, tuple[str, ...]]:
    values: list[dict[str, tuple[str, ...]]] = []
    for ref in refs:
        if not isinstance(ref, Mapping):
            raise ContractError("source evidence is not a resolution")
        citation = SourceCitation.from_json(ref.get("ref"))
        if (
            ref.get("schema") != 1
            or ref.get("status") != "resolved"
            or ref.get("kind") != "source"
            or ref.get("mission_id") != mission_id
            or ref.get("tenant_id") != tenant_id
            or ref.get("target") != citation.path
            or ref.get("source_version") != citation.version
            or ref.get("source_trust") != "untrusted_external"
        ):
            raise ContractError("source evidence identity or resolution mismatch")
        values.append({citation.path: (citation.version,)})
    return merge_source_versions(*values)


def source_dependencies_for(
    store: Store,
    *,
    mission_id: str,
    evidence_refs: Sequence[Mapping[str, Any]],
    used_knowledge: Sequence[str],
) -> tuple[dict[str, tuple[str, ...]], list[dict[str, Any]]]:
    """Union actual validated refs and existing Knowledge, without backfilling it.

    The caller supplies system-validated refs for a new projection. Historical refs
    must also agree with the original accepted assessment receipts. Active traversal
    detects cycles; completed-node memoization permits shared diamond dependencies.
    """
    # Local import avoids assessments -> deterministic_checks -> KnowledgeIndex cycle.
    from ..verification.assessments import accepted_assessments_for

    try:
        mission = store.get_mission(mission_id)
        if mission is None:
            return {}, [_issue("ERROR", "mission_unavailable")]
        domain = _domain(store, mission_id)
    except Exception:
        return {}, [_issue("ERROR", "source_provenance_unavailable")]
    mappings: list[Mapping[str, Sequence[str]]] = []
    issues: list[dict[str, Any]] = []
    try:
        mappings.append(
            _refs_versions(evidence_refs, mission_id=mission_id, tenant_id=mission.tenant_id)
        )
    except ContractError:
        issues.append(_issue("unknown_source_provenance", "invalid_evidence_refs"))

    memo: dict[str, tuple[dict[str, tuple[str, ...]], list[dict[str, Any]]]] = {}
    active: set[str] = set()
    accepted: dict[str, Any] = {}

    def visit(kid: str) -> tuple[dict[str, tuple[str, ...]], list[dict[str, Any]]]:
        if kid in active:
            return {}, [_issue("unknown_source_provenance", "cycle", knowledge_id=kid)]
        if kid in memo:
            return memo[kid]
        active.add(kid)
        parts: list[Mapping[str, Sequence[str]]] = []
        problems: list[dict[str, Any]] = []
        try:
            record = store.get_knowledge(kid)
            if record is None or record.mission_id != mission_id:
                return {}, [
                    _issue(
                        "unknown_source_provenance",
                        "missing_or_foreign_knowledge",
                        knowledge_id=kid,
                    )
                ]
            # Code projections without document provenance retain their original semantics.
            if domain.id != DOC_DOMAIN and record.verifier.get("system_domain") != DOC_DOMAIN:
                return {}, []
            if record.source_versions is not None:
                parts.append(merge_source_versions(record.source_versions))
            inherited = record.verifier.get(SOURCE_PROVENANCE_ISSUES, ())
            if not isinstance(inherited, (list, tuple)):
                raise ContractError("invalid inherited source issues")
            for problem in inherited:
                if (
                    not isinstance(problem, Mapping)
                    or problem.get("code") not in _ISSUE_CODES
                    or not isinstance(problem.get("reason"), str)
                    or not problem["reason"]
                ):
                    raise ContractError("invalid inherited source issue")
                problems.append(dict(problem))
            dependencies = set(record.dependencies)
            # Validate original provenance; current claim content/revision may have changed.
            try:
                task = store.get_task(record.source_task)
                if (
                    task is None
                    or task.mission_id != mission_id
                    or task.accepted_result_id != record.source_result
                    or record.verifier.get("system_domain") != DOC_DOMAIN
                    or record.verifier.get("adapter")
                    not in {"citation_integrity@v1", "citation_integrity@v2"}
                    or record.verifier.get("grade") != "verified"
                ):
                    raise ContractError("unknown accepted source provenance")
                if task.id not in accepted:
                    accepted[task.id] = accepted_assessments_for(store, task=task)
                binding, rows = accepted[task.id]
                relevant = [row for row in rows if row.claim_id == record.claim_id]
                if (
                    binding.attempt_id != record.source_attempt
                    or binding.result_id != record.source_result
                    or not relevant
                    or any(row.verdict != "PASS" for row in relevant)
                    or sorted(record.verifier.get("assessment_receipts", ()))
                    != sorted(row.receipt_id for row in relevant)
                ):
                    raise ContractError("source assessment receipts mismatch")
                refs = record.verifier.get("evidence_refs")
                if not isinstance(refs, (list, tuple)) or not refs:
                    raise ContractError("missing actual source evidence")
                encoded = sorted(canonical_json(ref) for ref in refs)
                if any(
                    sorted(canonical_json(ref) for ref in row.to_json()["evidence_refs"]) != encoded
                    for row in relevant
                ):
                    raise ContractError("source evidence differs from accepted receipts")
                parts.append(
                    _refs_versions(refs, mission_id=mission_id, tenant_id=mission.tenant_id)
                )
                # Old projection dependencies may omit the envelope's original used IDs.
                dependencies.update(binding.envelope.used_knowledge)
            except ContractError as error:
                code = (
                    "ERROR"
                    if str(error).startswith("accepted artifact invalid")
                    else "unknown_source_provenance"
                )
                problems.append(_issue(code, "invalid_accepted_provenance", knowledge_id=kid))
            for dependency in sorted(dependencies):
                versions, inherited_issues = visit(dependency)
                parts.append(versions)
                problems.extend(inherited_issues)
        except ContractError:
            problems.append(
                _issue("unknown_source_provenance", "malformed_provenance", knowledge_id=kid)
            )
        except Exception:
            problems.append(_issue("ERROR", "source_provenance_unavailable", knowledge_id=kid))
        finally:
            active.remove(kid)
        result = merge_source_versions(*parts), _ordered(problems)
        memo[kid] = result
        return result

    for kid in sorted(set(used_knowledge)):
        versions, inherited_issues = visit(kid)
        mappings.append(versions)
        issues.extend(inherited_issues)
    return merge_source_versions(*mappings), _ordered(issues)


def _current_version_issues(
    store: Store, *, mission_id: str, tenant_id: str, path: str, version: str
) -> list[dict[str, Any]]:
    identity = {"path": path, "version": version}
    row = store.get_source(mission_id, path, version)
    if row is None or any(
        row.get(key) != value
        for key, value in (
            ("mission_id", mission_id),
            ("tenant_id", tenant_id),
            ("path", path),
            ("version_hash", version),
        )
    ):
        return [_issue("ERROR", "source_unavailable", **identity)]
    if row.get("revoked"):
        return [_issue("stale_source", "revoked", **identity)]
    if row.get("superseded_by") is not None:
        return [_issue("stale_source", "superseded", **identity)]
    current = store.get_source(mission_id, path)
    if current is None or current.get("version_hash") != version:
        return [_issue("stale_source", "not_current", **identity)]
    if current.get("tenant_id") != tenant_id or current.get("mission_id") != mission_id:
        return [_issue("ERROR", "source_unavailable", **identity)]
    return []


def source_current_issues(
    store: Store,
    mission_id: str,
    citations: Sequence[SourceCitation],
    artifact_store: ArtifactStore,
) -> list[dict[str, Any]]:
    """Direct citation fence, called inside the accept transaction after receipt checks."""
    if not citations:
        return []
    try:
        mission = store.get_mission(mission_id)
        domain = _domain(store, mission_id)
        if mission is None or domain.id != DOC_DOMAIN:
            return [_issue("ERROR", "source_authority_unavailable")]
    except Exception:
        return [_issue("ERROR", "source_authority_unavailable")]
    issues = []
    resolver = EvidenceResolver(store, artifact_store)
    for path, version in sorted({(c.path, c.version) for c in citations}):
        try:
            read = resolver.read_source(
                tenant_id=mission.tenant_id,
                mission_id=mission_id,
                path=path,
                version=version,
                source_roots=domain.source_roots,
            )
            if read.status != "resolved":
                issues.append(
                    _issue("ERROR", read.reason or "unreadable", path=path, version=version)
                )
                continue
            issues.extend(
                _current_version_issues(
                    store,
                    mission_id=mission_id,
                    tenant_id=mission.tenant_id,
                    path=path,
                    version=version,
                )
            )
        except Exception:
            issues.append(_issue("ERROR", "source_unavailable", path=path, version=version))
    return _ordered(issues)


def stale_knowledge_for(
    store: Store, *, mission_id: str, ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Current registry state for lineage; it never mutates the loaded read model."""
    stale: dict[str, list[dict[str, Any]]] = {}
    for kid in sorted(set(ids)):
        versions, issues = source_dependencies_for(
            store, mission_id=mission_id, evidence_refs=(), used_knowledge=(kid,)
        )
        try:
            mission = store.get_mission(mission_id)
            if mission is None:
                raise ContractError("mission unavailable")
            for path, hashes in versions.items():
                for version in hashes:
                    issues.extend(
                        _current_version_issues(
                            store,
                            mission_id=mission_id,
                            tenant_id=mission.tenant_id,
                            path=path,
                            version=version,
                        )
                    )
        except Exception:
            issues.append(_issue("ERROR", "source_unavailable", knowledge_id=kid))
        if issues:
            stale[kid] = _ordered(issues)
    return stale
