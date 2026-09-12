# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""E03/E04 oracle, authored before implementation; main alone runs pytest.

CAS, resolver, citation producer and grading are real. The repository double supplies
historical identities and permits explicit corruption without changing production
write APIs. These are lineage/read-model tests, not proof of Commit/facade authority;
the actual acceptance transaction, approvals and runtime have separate integration
oracles. No model call or fabricated assessment receipt is needed here.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import textwrap
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agent_orchestrator.memory.source_dependencies import (
    merge_source_versions,
    source_current_issues,
    source_dependencies_for,
)

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.context.retrieval import rank_knowledge
from agent_orchestrator.contracts import (
    AttemptStatus,
    ClaimProposal,
    ClaimStatus,
    ResultEnvelope,
    SourceCitation,
    TaskStatus,
    ids,
)
from agent_orchestrator.contracts.models import canonical_json
from agent_orchestrator.governance.domains import CODE_PROFILE, DOC_PROFILE
from agent_orchestrator.memory.claims import grade_claim, system_attribution
from agent_orchestrator.memory.verified_knowledge import KnowledgeIndex, KnowledgeRecord
from agent_orchestrator.verification.assessments import (
    assessment_binding_for,
    citation_integrity,
    validated_assessments,
)
from agent_orchestrator.verification.deterministic_checks import LayerResult, check_used_knowledge
from agent_orchestrator.verification.evidence_resolver import EvidenceResolver

PATH = "sources/a.md"
QUOTE = "资料记录方案可行。"
ISSUE_KEY = "source_provenance_issues"
BASELINE_CHECK_AST = "4c8dcdbc8b0dbae7a9a722d32d2f980a2e86a41c3dfc8a1580ec6afff958dac0"


class Repository:
    """Read API double; all writes below belong solely to the test fixture."""

    def __init__(self):
        self.mission = SimpleNamespace(
            id="m", tenant_id="tenant", goal="核对来源", success_criteria=(QUOTE,)
        )
        self.domain = replace(DOC_PROFILE, version="2", adapters={})
        self.sources, self.records, self.claims = {}, {}, {}
        self.tasks, self.attempts, self.intents, self.results = {}, {}, {}, {}
        self.layers, self.assessments = {}, {}
        self.source_reads = []

    def get_mission(self, mid):
        return self.mission if mid == self.mission.id else None

    def get_mission_domain(self, mid):
        if mid != self.mission.id:
            return None
        return {
            "mission_id": mid,
            "domain_id": self.domain.id,
            "domain_version": self.domain.version,
            "json": self.domain.to_json(),
        }

    def get_source(self, mid, path, version_hash=None):
        self.source_reads.append((mid, path, version_hash))
        if version_hash is not None:
            return self.sources.get((mid, path, version_hash))
        return next(
            (
                row
                for (m, p, _), row in self.sources.items()
                if m == mid and p == path and not row["revoked"] and row["superseded_by"] is None
            ),
            None,
        )

    def list_sources(self, mid, active_only=False):
        return [
            row
            for (m, _, _), row in self.sources.items()
            if m == mid
            and (not active_only or (not row["revoked"] and row["superseded_by"] is None))
        ]

    def get_knowledge(self, kid):
        return self.records.get(kid)

    def list_knowledge(self, mid):
        return [r for r in self.records.values() if r.mission_id == mid]

    def get_claim(self, cid):
        return self.claims.get(cid)

    def list_mission_claims(self, mid):
        return [c for c in self.claims.values() if c.mission_id == mid]

    def get_task(self, tid):
        return self.tasks.get(tid)

    def get_attempt(self, aid):
        return self.attempts.get(aid)

    def get_intent_for_subject(self, aid):
        return self.intents.get(aid)

    def get_result(self, rid):
        return self.results.get(rid)

    def list_verifications(self, rid):
        return self.layers.get(rid, [])

    def list_criterion_assessments(self, mid, *, result_id=None):
        if mid != self.mission.id:
            return []
        return [
            row
            for rid, rows in self.assessments.items()
            if result_id is None or rid == result_id
            for row in rows
        ]

    def history(self):
        return canonical_json(
            {
                "knowledge": {key: row.to_json() for key, row in self.records.items()},
                "claims": {key: vars(row) for key, row in self.claims.items()},
                "layers": self.layers,
                "assessments": self.assessments,
            }
        )


@pytest.fixture
def scene(tmp_path):
    repo = Repository()
    cas = ArtifactStore(tmp_path / "cas")
    resolver = EvidenceResolver(repo, cas)

    def source(text=QUOTE, path=PATH, *, activate=True):
        version = cas.put_bytes((text + "\n").encode("utf-8"))
        if activate:
            for (mid, p, h), row in repo.sources.items():
                if mid == "m" and p == path and h != version:
                    row["superseded_by"] = version
        repo.sources[("m", path, version)] = {
            "mission_id": "m",
            "tenant_id": "tenant",
            "path": path,
            "version_hash": version,
            "kind": "markdown",
            "trust": "untrusted_external",
            "registered_at": 1.0,
            "superseded_by": None,
            "revoked": False,
        }
        return SourceCitation(path, version, 1, 1, text)

    def record(label, citation, *, dependencies=()):
        """Produce genuine frozen proof, then represent its accepted projection."""
        tid, aid, rid = "task-" + label, "attempt-" + label, "result-" + label
        task = SimpleNamespace(
            id=tid,
            mission_id="m",
            kind="work",
            goal="核查材料",
            rationale="保留来源",
            success_criteria=("cite:" + citation.path,),
            verification_policy=("rule_check",),
            outputs=(),
            dependency_ids=(),
            parent_task_ids=(),
            accepted_result_id=rid,
            status=TaskStatus.COMPLETED,
            version=3,
        )
        attempt = SimpleNamespace(
            id=aid, task_id=tid, mission_id="m", status=AttemptStatus.COMPLETED
        )
        proposal = ClaimProposal(citation.quote, 1.0, citations=(citation,))
        env = ResultEnvelope.from_json(
            {
                "id": rid,
                "mission_id": "m",
                "task_id": tid,
                "attempt_id": aid,
                "outcome": "candidate",
                "summary": "已核引用",
                "claims": [proposal.to_json()],
                "used_knowledge": list(dependencies),
            }
        )
        cid = ids.claim_id(rid, 1)
        claim = SimpleNamespace(
            id=cid,
            mission_id="m",
            source_task=tid,
            source_attempt=aid,
            result_id=rid,
            content=proposal.content,
            status=ClaimStatus.UNDER_REVIEW,
            version=2,
        )
        repo.tasks[tid], repo.attempts[aid], repo.claims[cid] = task, attempt, claim
        contract = {
            name: getattr(task, name)
            for name in (
                "kind",
                "goal",
                "rationale",
                "success_criteria",
                "verification_policy",
                "outputs",
            )
        }
        repo.intents[aid] = SimpleNamespace(
            kind="attempt",
            subject_id=aid,
            mission_id="m",
            config={
                "task_contract": {"task_id": tid, **contract},
                "source_roots": ["sources/"],
                "source_versions": {citation.path: citation.version},
            },
        )
        binding = assessment_binding_for(
            repo, task=task, attempt=attempt, envelope=env, artifacts=[]
        )
        layer = citation_integrity(
            binding=binding,
            envelope=env,
            resolver=resolver,
            structural_result=LayerResult("rule_check", "PASS", "existing structure", {}),
        )
        assert layer.status == "PASS"
        rows = validated_assessments(layer, binding=binding)
        grade = grade_claim(
            cid,
            (),
            verifier_results=(layer.to_json(),),
            artifact_paths=(),
            untrusted_prefixes=(),
            domain=repo.domain,
            proposal=proposal,
            assessments=rows,
        )
        assert grade.status is ClaimStatus.VERIFIED
        attribution = system_attribution(grade.basis)
        assert attribution is not None
        claim.status, claim.version = grade.status, 3
        claim.content = attribution["content"]
        claim.confidence_metadata = {"basis": dict(grade.basis), "grade": "verified"}
        item = KnowledgeRecord(
            id=cid,
            mission_id="m",
            claim_id=cid,
            content=attribution["content"],
            type="attribution",
            status="VERIFIED",
            version=1,
            key=attribution["key"],
            stance="affirms",
            proposed_by="worker",
            source_task=tid,
            source_attempt=aid,
            source_result=rid,
            evidence=(),
            verifier=dict(grade.basis),
            dependencies=tuple(dependencies),
            created_at=1.0,
        )
        repo.records[cid] = item
        repo.results[rid] = SimpleNamespace(
            envelope=env, verification_state="DONE", verdict="PASS", artifacts=()
        )
        repo.layers[rid] = [layer.to_json()]
        repo.assessments[rid] = [row.to_json() for row in rows]
        return item

    def derive(*knowledge, refs=()):
        return source_dependencies_for(
            repo,
            mission_id="m",
            evidence_refs=refs,
            used_knowledge=tuple(k.id if isinstance(k, KnowledgeRecord) else k for k in knowledge),
        )

    return SimpleNamespace(
        repo=repo, cas=cas, resolver=resolver, source=source, record=record, derive=derive
    )


def codes(issues):
    return {item["code"] for item in issues}


def test_legacy_knowledge_omits_absent_field_and_keeps_canonical_bytes(scene):
    citation = scene.source()
    item = scene.record("old", citation)
    old = item.to_json()
    assert "source_versions" not in old
    encoded = canonical_json(old)
    loaded = KnowledgeRecord.from_json(copy.deepcopy(old))
    assert loaded.source_versions is None
    assert canonical_json(loaded.to_json()) == encoded
    assert canonical_json(replace(loaded, source_versions=None).to_json()) == encoded
    before = scene.repo.history()
    versions, issues = scene.derive(loaded)
    assert versions == {PATH: (citation.version,)} and issues == []
    assert scene.repo.history() == before  # no read-side migration/backfill


def test_explicit_multiversion_knowledge_roundtrips_sorted_unique_arrays(scene):
    a, b = scene.source(), scene.source("资料记录方案不可行。")
    item = scene.record("multi", b)
    versions = merge_source_versions({PATH: (b.version, a.version, b.version)})
    projected = replace(item, source_versions=versions)
    raw = projected.to_json()
    assert raw["source_versions"] == {PATH: sorted({a.version, b.version})}
    restored = KnowledgeRecord.from_json(raw)
    assert restored.source_versions == versions
    assert canonical_json(restored.to_json()) == canonical_json(raw)
    empty = replace(item, source_versions={})
    assert empty.to_json()["source_versions"] == {}
    assert KnowledgeRecord.from_json(empty.to_json()).source_versions == {}


def test_merge_keeps_same_path_versions_and_is_order_independent():
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    left, right = {PATH: (b, a)}, {PATH: (c, b), "sources/b.md": (a,)}
    before = copy.deepcopy((left, right))
    expected = {PATH: (a, b, c), "sources/b.md": (a,)}
    assert merge_source_versions(left, right) == expected
    assert merge_source_versions(right, left, left) == expected
    assert (left, right) == before
    assert merge_source_versions() == {}


def test_recursive_diamond_is_not_a_cycle_and_retains_multiversion_union(scene):
    a = scene.source()
    root = scene.record("root", a)
    b = scene.source("另一版本记录不同的条件。")
    left = scene.record("left", b, dependencies=(root.id,))
    right = scene.record("right", b, dependencies=(root.id,))
    tip = scene.record("tip", b, dependencies=(left.id, right.id))
    before = scene.repo.history()
    versions, issues = scene.derive(tip)
    assert versions == {PATH: tuple(sorted({a.version, b.version}))}
    assert issues == []  # lineage is known even though the old source is now stale
    assert scene.derive(right, left) == scene.derive(left, right, root)
    assert scene.repo.history() == before
    index = KnowledgeIndex.load(scene.repo, "m")
    stale = index.stale()
    assert set(stale) == {root.id, left.id, right.id, tip.id}
    assert all("stale_source" in codes(reasons) for reasons in stale.values())
    assert index.stale((tip.id,)) == {tip.id: stale[tip.id]}


@pytest.mark.parametrize("field", [None, {}, {PATH: ("f" * 64,)}])
def test_explicit_field_never_hides_real_refs_or_dependencies(scene, field):
    a = scene.source()
    parent = scene.record("parent", a)
    b = scene.source("资料的新版本补充了限定条件。")
    child = scene.record("child", b, dependencies=(parent.id,))
    child = replace(child, source_versions=field)
    scene.repo.records[child.id] = child
    versions, issues = scene.derive(child)
    assert set(versions[PATH]) >= {a.version, b.version}
    if field:
        # A declared version without provenance must remain visible, never replace refs.
        assert "f" * 64 in versions[PATH] or "unknown_source_provenance" in codes(issues)
    assert child.id in KnowledgeIndex.load(scene.repo, "m").stale()


@pytest.mark.parametrize("cycle", ["self", "two_nodes"])
def test_cycle_is_unknown_even_when_each_node_has_a_real_resolved_source(scene, cycle):
    citation = scene.source()
    a, b = scene.record("a", citation), scene.record("b", citation)
    scene.repo.records[a.id] = replace(a, dependencies=(a.id if cycle == "self" else b.id,))
    if cycle == "two_nodes":
        scene.repo.records[b.id] = replace(b, dependencies=(a.id,))
    before = scene.repo.history()
    _, issues = scene.derive(a)
    assert "unknown_source_provenance" in codes(issues)
    assert any(item["reason"] == "cycle" for item in issues)
    assert a.id in KnowledgeIndex.load(scene.repo, "m").stale()
    assert scene.repo.history() == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "foreign",
        "unaccepted",
        "wrong_accepted_result",
        "unknown_adapter",
        "missing_refs",
        "foreign_ref",
    ],
)
def test_unknown_history_is_not_a_clean_empty_or_partial_dependency_set(scene, damage):
    citation = scene.source()
    parent = scene.record("parent", citation)
    child = scene.record("child", citation, dependencies=(parent.id,))
    if damage == "missing":
        scene.repo.records.pop(parent.id)
    elif damage == "foreign":
        scene.repo.records[parent.id] = replace(parent, mission_id="foreign")
    elif damage == "unaccepted":
        scene.repo.results[parent.source_result].verification_state = "RUNNING"
        scene.repo.results[parent.source_result].verdict = None
    elif damage == "wrong_accepted_result":
        scene.repo.tasks[parent.source_task].accepted_result_id = "another-result"
    elif damage == "unknown_adapter":
        scene.repo.records[parent.id] = replace(
            parent, verifier={**parent.verifier, "adapter": "worker@v9"}
        )
    elif damage == "missing_refs":
        scene.repo.records[parent.id] = replace(parent, verifier={})
    else:
        verifier = copy.deepcopy(dict(parent.verifier))
        verifier["evidence_refs"][0]["mission_id"] = "foreign"
        scene.repo.records[parent.id] = replace(parent, verifier=verifier)
    before = scene.repo.history()
    _, issues = scene.derive(child)
    assert "unknown_source_provenance" in codes(issues)
    assert child.id in KnowledgeIndex.load(scene.repo, "m").stale()
    assert scene.repo.history() == before


@pytest.mark.parametrize("retain_dependency", [True, False])
def test_unknown_issue_survives_new_projection_and_one_more_hop(scene, retain_dependency):
    citation = scene.source()
    parent = scene.record("parent", citation, dependencies=("missing-knowledge",))
    versions, issues = scene.derive(parent)
    assert "unknown_source_provenance" in codes(issues)
    child = scene.record("child", citation, dependencies=(parent.id,))
    child = replace(
        child,
        source_versions=versions,
        dependencies=child.dependencies if retain_dependency else (),
        verifier={**child.verifier, ISSUE_KEY: issues},
    )
    scene.repo.records[child.id] = child
    tip = scene.record("tip", citation, dependencies=(child.id,))
    _, propagated = scene.derive(tip)
    assert "unknown_source_provenance" in codes(propagated)
    assert tip.id in KnowledgeIndex.load(scene.repo, "m").stale()


def test_stale_filter_runs_before_ranking_limit_and_does_not_change_check(scene):
    old_citation = scene.source()
    old = scene.record("old", old_citation)
    current = scene.record("current", scene.source("当前版本记录新的适用条件。"))
    index = KnowledgeIndex.load(scene.repo, "m")
    assert index.check((old.id,)) == []
    assert check_used_knowledge((old.id,), index) == []
    before = scene.repo.history()
    stale = index.stale()
    task = scene.repo.tasks[old.source_task]
    ranked = rank_knowledge(
        task,
        (old, current),
        tasks_by_id=scene.repo.tasks,
        limit=1,
        query_text=old.content,
        stale=stale,
    )
    assert [item.id for item in ranked.items] == [current.id]
    assert ranked.dropped["stale"][old.id] == stale[old.id]
    assert index.check((old.id,)) == []
    assert scene.repo.history() == before


def test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged(scene):
    parsed = ast.parse(textwrap.dedent(inspect.getsource(KnowledgeIndex.check)))
    actual = hashlib.sha256(ast.dump(parsed.body[0], include_attributes=False).encode()).hexdigest()
    assert actual == BASELINE_CHECK_AST
    item = scene.record("old", scene.source())
    task = scene.repo.tasks[item.source_task]
    implicit = rank_knowledge(task, (item,), tasks_by_id=scene.repo.tasks)
    explicit = rank_knowledge(task, (item,), tasks_by_id=scene.repo.tasks, stale={})
    assert canonical_json(implicit.to_json()) == canonical_json(explicit.to_json())


def test_index_load_and_check_do_not_read_sources_and_stale_reads_current_state(scene):
    citation = scene.source()
    item = scene.record("old", citation)
    scene.repo.source_reads.clear()
    index = KnowledgeIndex.load(scene.repo, "m")
    assert index.check((item.id,)) == [] and scene.repo.source_reads == []
    assert index.stale() == {}
    scene.repo.sources[("m", PATH, citation.version)]["revoked"] = True
    assert "stale_source" in codes(index.stale()[item.id])
    scene.repo.sources[("m", PATH, citation.version)]["revoked"] = False
    assert index.stale() == {}  # currentness may recover, history was not rewritten


def test_old_code_without_sources_keeps_empty_lineage_and_no_source_io(scene):
    item = scene.record("code", scene.source())
    scene.repo.domain = CODE_PROFILE
    item = replace(item, type="statement", verifier={"grade": "verified", "layer": "code_test"})
    scene.repo.records[item.id] = item
    before = canonical_json(item.to_json())
    scene.repo.source_reads.clear()
    index = KnowledgeIndex.load(scene.repo, "m")
    assert index.stale() == {} and index.check((item.id,)) == []
    assert scene.repo.source_reads == []
    assert canonical_json(item.to_json()) == before and "source_versions" not in item.to_json()


def test_direct_refs_and_inherited_staleness_are_separate(scene):
    old_citation = scene.source()
    old = scene.record("old", old_citation)
    current = scene.source("新版本记录方案可行但限制不同。")
    resolved = scene.resolver.resolve(
        current,
        tenant_id="tenant",
        mission_id="m",
        source_versions={PATH: current.version},
        source_roots=("sources/",),
    )
    assert resolved.status == "resolved"
    versions, issues = scene.derive(old, refs=(resolved.to_json(),))
    assert versions == {PATH: tuple(sorted((old_citation.version, current.version)))}
    assert issues == []
    assert source_current_issues(scene.repo, "m", (current,), scene.cas) == []
    assert codes(source_current_issues(scene.repo, "m", (old_citation,), scene.cas)) == {
        "stale_source"
    }


@pytest.mark.parametrize("change", ["supersede", "revoke"])
def test_current_gate_rechecks_lifecycle_while_resolver_remains_frozen(scene, change):
    citation = scene.source()
    item = scene.record("original", citation)
    frozen = {PATH: citation.version}
    before = scene.resolver.resolve(
        citation,
        tenant_id="tenant",
        mission_id="m",
        source_versions=frozen,
        source_roots=("sources/",),
    ).to_json()
    history = scene.repo.history()
    if change == "supersede":
        scene.source("新版本记录了不同结论。")
    else:
        scene.repo.sources[("m", PATH, citation.version)]["revoked"] = True
    after = scene.resolver.resolve(
        citation,
        tenant_id="tenant",
        mission_id="m",
        source_versions=frozen,
        source_roots=("sources/",),
    ).to_json()
    assert before == after and after["status"] == "resolved"
    issues = source_current_issues(scene.repo, "m", (citation,), scene.cas)
    assert codes(issues) == {"stale_source"}
    assert all(i["path"] == PATH and i["version"] == citation.version for i in issues)
    assert item.id in KnowledgeIndex.load(scene.repo, "m").stale()
    assert scene.repo.history() == history


def test_current_gate_only_reads_actual_citations_and_deduplicates(scene):
    citation = scene.source()
    unused = scene.source("无关来源。", "sources/unused.md")
    scene.repo.sources[("m", unused.path, unused.version)]["revoked"] = True
    scene.repo.source_reads.clear()
    assert source_current_issues(scene.repo, "m", (citation, citation), scene.cas) == []
    assert all(path == PATH for _, path, _ in scene.repo.source_reads)
    scene.repo.sources[("m", PATH, citation.version)]["revoked"] = True
    single = source_current_issues(scene.repo, "m", (citation,), scene.cas)
    assert source_current_issues(scene.repo, "m", (citation, citation), scene.cas) == single
    scene.repo.source_reads.clear()
    assert source_current_issues(scene.repo, "m", (), scene.cas) == []
    assert scene.repo.source_reads == []


@pytest.mark.parametrize("damage", ["bytes", "missing", "symlink", "invalid_utf8"])
def test_unreadable_direct_source_is_error_even_when_also_revoked(scene, damage):
    citation = scene.source()
    path = scene.cas.path_for(citation.version)
    if damage == "bytes":
        path.chmod(0o600)
        path.write_bytes(b"tampered bytes")
    elif damage == "missing":
        path.unlink()
    elif damage == "symlink":
        copy_path = path.with_name("actual")
        copy_path.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy_path)
    else:
        bad_hash = scene.cas.put_bytes(b"\xff\xfe\xff")
        old = scene.repo.sources.pop(("m", PATH, citation.version))
        scene.repo.sources[("m", PATH, bad_hash)] = {**old, "version_hash": bad_hash}
        citation = replace(citation, version=bad_hash)
    scene.repo.sources[("m", PATH, citation.version)]["revoked"] = True
    assert codes(source_current_issues(scene.repo, "m", (citation,), scene.cas)) == {"ERROR"}


@pytest.mark.parametrize("damage", ["missing", "foreign_tenant", "outside_root"])
def test_unregistered_or_unauthorized_direct_source_cannot_be_clean(scene, damage):
    citation = scene.source()
    if damage == "missing":
        scene.repo.sources.clear()
    elif damage == "foreign_tenant":
        scene.repo.sources[("m", PATH, citation.version)]["tenant_id"] = "foreign"
    else:
        old = scene.repo.sources.pop(("m", PATH, citation.version))
        citation = replace(citation, path="reports/self.md")
        scene.repo.sources[("m", citation.path, citation.version)] = {**old, "path": citation.path}
    assert codes(source_current_issues(scene.repo, "m", (citation,), scene.cas)) == {"ERROR"}


def test_store_errors_are_explicit_and_not_empty_clean(scene, monkeypatch):
    citation = scene.source()
    item = scene.record("parent", citation)

    def unavailable(*args, **kwargs):
        raise OSError("source index unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(scene.repo, "get_source", unavailable)
        assert codes(source_current_issues(scene.repo, "m", (citation,), scene.cas)) == {"ERROR"}
        assert "ERROR" in codes(KnowledgeIndex.load(scene.repo, "m").stale()[item.id])
    monkeypatch.setattr(scene.repo, "get_knowledge", unavailable)
    _, issues = scene.derive(item.id)
    assert "ERROR" in codes(issues)


def test_unknown_provenance_remains_visible_in_retrieval_diagnostics(scene):
    item = scene.record("unknown", scene.source())
    scene.repo.records[item.id] = replace(item, verifier={}, source_versions={})
    index = KnowledgeIndex.load(scene.repo, "m")
    stale = index.stale()
    assert "unknown_source_provenance" in codes(stale[item.id])
    task = scene.repo.tasks[item.source_task]
    ranked = rank_knowledge(
        task, tuple(scene.repo.records.values()), tasks_by_id=scene.repo.tasks, stale=stale
    )
    assert ranked.items == () and ranked.dropped["stale"][item.id] == stale[item.id]
