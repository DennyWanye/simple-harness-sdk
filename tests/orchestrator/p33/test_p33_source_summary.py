"""E03: source invalidation must not reappear as VERIFIED in derived summaries."""

from dataclasses import replace

from graph_helpers7 import graph_service
from test_p33_doc_consumption import record

from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.memory.summaries import build_summaries


def test_stale_summary_projection_excludes_current_knowledge_without_history_rewrite(tmp_path):
    service, mission, tasks = graph_service(tmp_path, domain=DOC_DOMAIN)
    old = replace(record("old", mission.id, "旧资料。"), source_task=tasks["A"].id)
    valid = replace(record("valid", mission.id, "有效资料。"), source_task=tasks["A"].id)
    service.store.upsert_knowledge(old)
    service.store.upsert_knowledge(valid)
    before = [r.to_json() for r in service.store.list_knowledge(mission.id)]
    historical = build_summaries(service.store, mission.id)
    assert {row["id"] for row in historical[f"mission:{mission.id}"]["knowledge"]} == {
        "old",
        "valid",
    }
    fresh = build_summaries(service.store, mission.id, stale={"old": [{"reason": "stale_source"}]})
    for key in (tasks["A"].id, f"mission:{mission.id}"):
        assert {r["id"] for r in fresh[key]["knowledge"]} == {"valid"}
        assert "old" not in fresh[key]["sources"]["knowledge"]
        assert "old" in fresh[key]["uncertainty"]["stale_knowledge"]
        assert fresh[key]["version"] != historical[key]["version"]
    assert [r.to_json() for r in service.store.list_knowledge(mission.id)] == before
    assert build_summaries(service.store, mission.id) == historical


def test_stale_used_dependency_masks_only_its_consumer_summary(tmp_path, monkeypatch):
    from types import SimpleNamespace

    service, mission, tasks = graph_service(tmp_path, domain=DOC_DOMAIN)
    old = replace(record("old", mission.id, "旧资料。"), source_task=tasks["A"].id)
    service.store.upsert_knowledge(old)
    # Isolate summary projection, without claiming this fabricated acceptance is an E2E.
    originals = service.store.list_tasks(mission.id)
    monkeypatch.setattr(
        service.store,
        "list_tasks",
        lambda _: [replace(t, accepted_result_id=t.id + ":result") for t in originals],
    )
    monkeypatch.setattr(
        service.store,
        "get_result",
        lambda rid: SimpleNamespace(
            envelope=SimpleNamespace(
                summary="unrelated history"
                if rid.startswith(tasks["C"].id + ":")
                else "obsolete world statement",
                used_knowledge=(old.id,) if rid.startswith(tasks["B"].id + ":") else (),
            )
        ),
    )
    fresh = build_summaries(service.store, mission.id, stale={"old": [{"code": "stale_source"}]})
    rows = {r["task_id"]: r for r in fresh[f"mission:{mission.id}"]["tasks"]}
    assert "obsolete world statement" not in rows[tasks["B"].id]["accepted_summary"]
    assert rows[tasks["C"].id]["accepted_summary"] == "unrelated history"
