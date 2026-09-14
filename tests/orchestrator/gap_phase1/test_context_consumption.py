# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""K04--K06 context-consumption regressions.

These fixtures deliberately use the deterministic retrieval, summary, and Commit
Service storage paths.  They do not infer meaning from a token-overlap score: each
query's intended record is stated by the fixture, and the assertions inspect the
context/source provenance that a downstream consumer actually receives.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from knowledge_helpers import (
    claim,
    drive_to_running,
    envelope,
    passed_layers,
    submit,
    two_branch_service,
)

from agent_orchestrator.context.compression import compress
from agent_orchestrator.context.retrieval import knowledge_view, rank_knowledge
from agent_orchestrator.memory.summaries import build_summaries
from agent_orchestrator.memory.verified_knowledge import KnowledgeRecord


def _record(
    record_id: str,
    mission_id: str,
    source_task: str,
    content: str,
    *,
    key: str | None = None,
    evidence: tuple[str, ...] = ("sources/constraints.md",),
    status: str = "VERIFIED",
    created_at: float = 1.0,
    superseded_by: str | None = None,
) -> KnowledgeRecord:
    return KnowledgeRecord(
        id=record_id,
        mission_id=mission_id,
        claim_id=record_id,
        content=content,
        type="statement",
        status=status,
        version=1,
        key=key,
        stance="affirms",
        proposed_by="agent",
        source_task=source_task,
        source_attempt=f"{source_task}:attempt-1",
        source_result=f"{source_task}:result-1",
        evidence=evidence,
        verifier={"layer": "code_test"},
        dependencies=(),
        created_at=created_at,
        superseded_by=superseded_by,
    )


def test_k04_long_condition_is_available_in_full_context_with_source_provenance(tmp_path):
    service, mission, (task, _) = two_branch_service(tmp_path)
    condition = "关键条件：未经书面同意不得删除归档。"
    record = _record(
        "K-long-condition",
        mission.id,
        task.id,
        "背景说明。" * 80 + condition,
        evidence=("sources/retention-policy.md",),
    )

    ranked = rank_knowledge(
        task,
        (record,),
        tasks_by_id={task.id: task},
        query_text="书面同意 删除归档",
        limit=1,
    )
    view = knowledge_view(record, ranked.items[0])
    summary = compress(
        scope="branch",
        subject_id=task.id,
        tasks=(task,),
        result_summaries={task.id: "历史结论。" * 80 + condition},
        knowledge=(record,),
        claims=(),
        open_conflicts=(),
    )

    assert record.content.index(condition) > 200
    assert condition in view["content"]
    assert view["id"] == record.id
    assert view["source_task"] == task.id
    assert view["evidence"] == ["sources/retention-policy.md"]
    assert condition not in str(summary)
    assert summary["knowledge"] == [
        {"id": record.id, "status": "VERIFIED", "key": None, "stance": "affirms"}
    ]


@pytest.mark.parametrize(
    ("query", "case"),
    (
        ("K-retention-42", "exact-id"),
        ("sources/retention-policy.md", "source-path"),
        ("The system must retain offline recovery capability.", "english-equivalent"),
        ("系统必须保留离线恢复能力。", "chinese-equivalent"),
    ),
    ids=("exact-id", "source-path", "english-equivalent", "chinese-equivalent"),
)
def test_k05_exact_identifiers_paths_and_bilingual_queries_select_the_explicit_record(
    tmp_path, query, case
):
    service, mission, (task, _) = two_branch_service(tmp_path)
    relevant = _record(
        "K-retention-42",
        mission.id,
        task.id,
        "系统必须保留离线恢复能力。",
        evidence=("sources/retention-policy.md",),
    )
    irrelevant = _record(
        "K-aesthetic-01",
        mission.id,
        task.id,
        "仪表盘标题使用蓝色。",
        evidence=("sources/style-guide.md",),
    )
    tasks = {task.id: task}

    result = rank_knowledge(
        task, (relevant, irrelevant), tasks_by_id=tasks, query_text=query, limit=1
    )
    if case == "english-equivalent":
        # A lexical ranker must abstain here, not claim to implement multilingual
        # semantics. The consuming Agent uses the original-knowledge tools;
        # its real-provider semantic selection is a separate acceptance gate.
        assert result.items == ()
        assert result.reason == "no_relevant_match_use_knowledge_catalog"
        assert set(result.dropped["no_relevance"]) == {relevant.id, irrelevant.id}
        return
    assert [item.id for item in result.items] == [relevant.id], case
    assert irrelevant.id not in [item.id for item in result.items]


def test_k06_superseded_knowledge_is_not_projected_as_current_in_downstream_summary(tmp_path):
    service, mission, (task, _) = two_branch_service(tmp_path)
    old = _record(
        "K-withdrawn",
        mission.id,
        task.id,
        "旧的保留条件。",
        key="retention.policy",
        status="SUPERSEDED",
        superseded_by="K-current",
    )
    current = _record(
        "K-current",
        mission.id,
        task.id,
        "当前的保留条件。",
        key="retention.policy",
        created_at=2.0,
    )
    service.store.upsert_knowledge(old)
    service.store.upsert_knowledge(current)

    ranked = rank_knowledge(
        task,
        service.store.list_knowledge(mission.id),
        tasks_by_id={task.id: task},
        query_text="保留条件",
        limit=1,
    )
    assert [item.id for item in ranked.items] == [current.id]
    assert ranked.dropped["superseded"] == [old.id]

    summary = build_summaries(service.store, mission.id)[task.id]
    current_ids = {item["id"] for item in summary["knowledge"]}
    assert old.id not in current_ids
    assert current.id in current_ids


@pytest.mark.parametrize("query", ("K-retention-42", "sources/retention-policy.md"))
def test_k05_exact_reference_respects_scope_currentness_and_lexical_ranking(tmp_path, query):
    _, mission, (task, _) = two_branch_service(tmp_path)
    target = _record(
        "K-retention-42",
        mission.id,
        task.id,
        "恢复条件需要保留。",
        evidence=("sources/retention-policy.md",),
    )
    decoy = _record(
        "K-a-decoy",
        mission.id,
        task.id,
        query,
        evidence=("sources/unrelated.md",),
    )
    # These stronger/recent exact matches must never escape the pre-filter.
    excluded = [
        replace(target, id="K-foreign", mission_id="other-mission", created_at=10),
        replace(target, id="K-stale", created_at=10),
        replace(target, id="K-old", status="SUPERSEDED", superseded_by=target.id),
    ]
    records = (target, decoy, *excluded)
    kwargs = {"tasks_by_id": {task.id: task}, "stale": {"K-stale": [{"code": "stale_source"}]}}
    ranked = rank_knowledge(task, records, query_text=query, **kwargs)
    assert ranked.items[0].id == target.id
    assert ranked.items[0].parts["exact_reference"] == 1.0
    assert {item.id for item in ranked.items} <= {target.id, decoy.id}
    assert ranked.dropped["superseded"] == ["K-old"]
    assert "K-stale" in ranked.dropped["stale"]
    # Ordinary content relevance still works with the same eligible corpus.
    lexical = rank_knowledge(task, records, query_text=target.content, **kwargs)
    assert lexical.items[0].id == target.id
    assert "exact_reference" not in lexical.items[0].parts


@pytest.mark.parametrize(
    "query", ("sources", "retention-policy.md", "SOURCES/retention-policy.md", "K-retention-420")
)
def test_k05_reference_fragments_and_case_variants_are_not_exact_hits(tmp_path, query):
    _, mission, (task, _) = two_branch_service(tmp_path)
    record = _record(
        "K-retention-42",
        mission.id,
        task.id,
        "恢复条件。",
        evidence=("sources/retention-policy.md",),
    )
    ranked = rank_knowledge(task, (record,), tasks_by_id={task.id: task}, query_text=query)
    assert all("exact_reference" not in item.parts for item in ranked.items)


def test_original_knowledge_tools_preserve_tail_and_reject_stale_or_foreign_reads(tmp_path):
    from agent_orchestrator.context.knowledge_tools import read_knowledge_tool

    service, mission, (task, _) = two_branch_service(tmp_path)
    condition = "关键条件：只有校验完整备份后才允许离线恢复。"
    record = _record("K-long", mission.id, task.id, "背景。" * 900 + condition)
    service.store.upsert_knowledge(record)
    listing = read_knowledge_tool(service.store, mission.id, "knowledge_list", {})
    assert condition not in listing["items"][0]["preview"]
    first = read_knowledge_tool(service.store, mission.id, "knowledge_read", {"id": record.id})
    second = read_knowledge_tool(
        service.store,
        mission.id,
        "knowledge_read",
        {
            "id": record.id,
            "offset": first["next_offset"],
            "expected_sha256": first["sha256"],
        },
    )
    assert first["content"] + second["content"] == record.content
    assert condition in second["content"] and second["next_offset"] is None
    assert second["source_task"] == task.id and second["version"] == record.version
    with pytest.raises(ValueError, match="not available"):
        read_knowledge_tool(service.store, "foreign", "knowledge_read", {"id": record.id})
    service.store.upsert_knowledge(replace(record, status="SUPERSEDED", superseded_by="K-new"))
    with pytest.raises(ValueError, match="not current"):
        read_knowledge_tool(service.store, mission.id, "knowledge_read", {"id": record.id})
    with pytest.raises(ValueError, match="catalog changed"):
        read_knowledge_tool(
            service.store,
            mission.id,
            "knowledge_list",
            {
                "offset": 1,
                "expected_sha256": listing["sha256"],
            },
        )


@pytest.mark.parametrize("invalidation", ("superseded", "withdrawn"))
def test_k06_invalidated_basis_masks_accepted_consumer_without_rewriting_history(
    tmp_path, invalidation, monkeypatch
):
    # These are historical accepted semantic claims. Freeze their original
    # grading version while exercising today's downstream projection.
    from agent_orchestrator.governance import domains

    monkeypatch.setattr(
        domains,
        "DOMAINS",
        {
            **domains.DOMAINS,
            domains.CODE_DOMAIN: domains.CODE_PROFILE_V1,
        },
    )
    service, mission, (task_a, task_b) = two_branch_service(tmp_path)
    attempt_a = drive_to_running(service, task_a)
    path_a = "tests/probe/test_impl_a.py"
    source = submit(
        service,
        attempt_a,
        envelope(
            attempt_a,
            claims=[claim("恢复条件已验证。", key="recovery", evidence=[f"pytest:{path_a}"])],
            summary="恢复条件已验证。",
        ),
    )
    service.accept_result(source.envelope.id, verifier_results=passed_layers(path_a))
    (root,) = service.store.list_knowledge(mission.id)
    attempt_b = drive_to_running(service, task_b, agent="consumer", turn="turn-b")
    path_b = "tests/probe/test_impl_b.py"
    consumer = submit(
        service,
        attempt_b,
        envelope(
            attempt_b,
            claims=(),
            artifacts=(path_b,),
            used_knowledge=(root.id,),
            summary="下游沿用先前恢复条件。",
        ),
        artifact_paths=(path_b,),
        turn="turn-b",
    )
    service.accept_result(consumer.envelope.id, verifier_results=passed_layers(path_b))
    valid = _record("K-independent", mission.id, task_b.id, "独立有效的检查结果。")
    service.store.upsert_knowledge(valid)
    original = build_summaries(service.store, mission.id)
    if invalidation == "superseded":
        service.store.upsert_knowledge(replace(root, status="SUPERSEDED", superseded_by=valid.id))
        stale = None
    else:
        # Exercise the public projection input, not a fabricated source-revocation API.
        stale = {root.id: [{"code": "stale_source", "reason": "revoked"}]}
    history = [record.to_json() for record in service.store.list_knowledge(mission.id)]
    fresh = build_summaries(service.store, mission.id, stale=stale)
    branch = fresh[task_b.id]
    row = next(row for row in branch["tasks"] if row["task_id"] == task_b.id)
    assert row["accepted_summary"] != consumer.envelope.summary
    assert root.id in branch["uncertainty"]["stale_knowledge"]
    current = fresh[f"mission:{mission.id}"]
    assert {record["id"] for record in current["knowledge"]} == {valid.id}
    assert root.id not in current["sources"]["knowledge"]
    assert branch["version"] != original[task_b.id]["version"]
    assert (
        service.store.get_result(consumer.envelope.id).envelope.summary == consumer.envelope.summary
    )
    assert [record.to_json() for record in service.store.list_knowledge(mission.id)] == history
    assert build_summaries(service.store, mission.id, stale=stale) == fresh


def test_v3_usage_guidance_preserves_frozen_v2_profile_and_prompt():
    from hashlib import sha256

    from agent_orchestrator.contracts.models import sha256_hex
    from agent_orchestrator.governance.domains import CODE_PROFILE, CODE_PROFILE_V2, DomainProfileV1
    from agent_orchestrator.runtime.role_templates import ROLES, template_for_domain

    frozen = DomainProfileV1.from_json(CODE_PROFILE_V2.to_json())
    assert (
        sha256_hex(frozen.to_json())
        == "ce2a010cf776fe2149d45f252c551f6b59c34003541ee5029b936e4f8d6305a5"
    )
    old = template_for_domain(ROLES["worker"], frozen, {})
    assert (
        sha256(old.instructions.encode()).hexdigest()
        == "2acc1fde38b872e9819d132c0cf93d6b277d0336e95ea0862f9a8b016839c334"
    )
    assert old.prompt_version == "worker-code-observation-v2"
    current = template_for_domain(ROLES["worker"], CODE_PROFILE, {})
    assert CODE_PROFILE.version == "3"
    assert current.prompt_version == "worker-code-observation-v3"
    assert "提及但明确排除" in current.instructions
    assert "你引用过的知识 id 必须写进" not in current.instructions
