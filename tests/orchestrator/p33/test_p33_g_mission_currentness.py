# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""G/A08: actual acceptance then source approval races final Mission judgment.

Old Task/Claim/assessment history stays byte-identical; only ACTIVE v4 eligibility
changes. Valid independent evidence may replace stale evidence. No fabricated PASS.
"""

import pytest
from test_p33_inconclusive_commits import PATH, QUOTE, accept, produce, submitted
from test_p33_inconclusive_commits import scenes as scenes
from test_p33_source_commits import attach, change_source, historical
from test_p33_source_commits import e_scenes as e_scenes

from agent_orchestrator.contracts import ContractError, MissionStatus
from agent_orchestrator.governance.domains import DOC_PROFILE_V3
from agent_orchestrator.verification.mission_coverage import mission_coverage


def ready(scenes, *, profile=None, criteria=None):
    s = scenes(mission_criteria=criteria or (QUOTE,), task_criteria=(QUOTE,), profile=profile)
    s.profile = s.commit.domain_for(s.mission.id)
    e = submitted(s, content=QUOTE)
    assert produce(e).status == "PASS"
    assert accept(e).accepted_result_id == e.envelope.id
    return attach(s, s.store)


@pytest.mark.parametrize("mode", ["revoke", "supersede"])
def test_g_active_mission_excludes_real_stale_basis_without_regrading_history(scenes, mode):
    s = ready(scenes)
    before = historical(s)
    assert mission_coverage(s.store, s.mission, s.profile)["criteria"][0]["verdict"] == "PASS"
    change_source(s, path=PATH, mode=mode)
    coverage = mission_coverage(s.store, s.mission, s.profile)
    row = coverage["criteria"][0]
    assert row["verdict"] == "FAIL"
    assert row["excluded_claim_ids"] and row["source_provenance_issues"]
    assert not coverage["insufficient"]
    assert historical(s) == before


def test_g_final_commit_refreshes_previously_valid_cached_judgment(scenes):
    s = ready(scenes)
    previous = mission_coverage(s.store, s.mission, s.profile)
    row = previous["criteria"][0]
    judgment = {
        "criterion": QUOTE,
        "criterion_id": row["criterion_id"],
        "met": True,
        "verdict": "PASS",
        "judge": "document_coverage",
    }
    s.commit.record_criteria_judgment(s.mission.id, "old", [judgment], summary="prior valid")
    change_source(s, path=PATH, mode="revoke")
    before = historical(s)
    judged = s.commit.judge_mission(s.mission.id, judgments=[judgment], summary="prior valid")
    assert judged.status is MissionStatus.FAILED
    assert judged.stop_reason == "mission_criteria_unmet"
    assert judged.final_report["success_criteria"][0]["met"] is False
    assert historical(s) == before


def test_g_frozen_v3_retains_historical_coverage(scenes):
    s = ready(scenes, profile=DOC_PROFILE_V3)
    before = mission_coverage(s.store, s.mission, s.profile)
    change_source(s, path=PATH, mode="revoke")
    assert mission_coverage(s.store, s.mission, s.profile) == before


@pytest.mark.parametrize("damage", ["missing", "bytes"])
def test_g_source_cas_damage_is_unavailable_not_uncertainty(scenes, damage):
    s = ready(scenes)
    path = s.cas.path_for(s.version)
    if damage == "missing":
        path.unlink()
    else:
        path.chmod(0o600)
        path.write_bytes(b"corrupt")
    with pytest.raises(ContractError, match="source"):
        mission_coverage(s.store, s.mission, s.profile)


def test_g_final_commit_refresh_is_not_authority_for_forged_unbound_judgment(scenes):
    from agent_orchestrator.orchestrator.commit_service import CommitRejected

    s = ready(scenes)
    with pytest.raises(CommitRejected):
        s.commit.judge_mission(
            s.mission.id, judgments=[{"criterion": QUOTE, "met": False}], summary="forged"
        )


@pytest.mark.parametrize("independent", [False, True])
def test_g_inherited_source_exclusion_and_independent_replacement(e_scenes, independent):
    from test_p33_source_commits import accept as e_accept
    from test_p33_source_commits import produce as e_produce
    from test_p33_source_commits import submit

    first_path, second_path = "sources/upstream.md", "sources/replacement.md"
    text = "替代资料已说明该条件。"
    s = e_scenes(
        paths=(first_path, second_path),
        sources={first_path: "上游限制。\n", second_path: text + "\n"},
        mission_criteria=(text,),
    )
    first = submit(s, path=first_path)
    e_produce(first)
    e_accept(first)
    kid = s.store.list_knowledge(s.mission.id)[0].id
    second = submit(s, index=1, path=second_path, used=() if independent else (kid,))
    e_produce(second)
    e_accept(second)
    before = historical(s)
    change_source(s, path=first_path, mode="revoke")
    row = mission_coverage(s.store, s.mission, s.profile)["criteria"][0]
    assert row["verdict"] == ("PASS" if independent else "FAIL")
    assert bool(row["excluded_claim_ids"]) is (not independent)
    assert historical(s) == before


def test_g_unreferenced_source_revoke_does_not_affect_coverage(e_scenes):
    from test_p33_source_commits import (
        PATH as source_path,
    )
    from test_p33_source_commits import (
        TEXT,
        submit,
    )
    from test_p33_source_commits import (
        accept as e_accept,
    )
    from test_p33_source_commits import (
        produce as e_produce,
    )

    s = e_scenes(
        paths=(source_path,),
        sources={source_path: TEXT, "sources/unused.md": "无关资料。\n"},
        mission_criteria=(TEXT.strip(),),
    )
    e = submit(s)
    e_produce(e)
    e_accept(e)
    before = mission_coverage(s.store, s.mission, s.profile)
    change_source(s, path="sources/unused.md", mode="revoke")
    assert mission_coverage(s.store, s.mission, s.profile) == before


def test_g_terminal_report_is_not_rejudged_after_revoke(scenes):
    s = ready(scenes)
    row = mission_coverage(s.store, s.mission, s.profile)["criteria"][0]
    judgment = {
        "criterion": QUOTE,
        "criterion_id": row["criterion_id"],
        "met": True,
        "judge": "document_coverage",
    }
    final = s.commit.judge_mission(s.mission.id, judgments=[judgment], summary="original")
    assert final.status is MissionStatus.COMPLETED
    before = final.to_json()
    change_source(s, path=PATH, mode="revoke")
    assert (
        s.commit.judge_mission(s.mission.id, judgments=[], summary="do not overwrite").to_json()
        == before
    )


def test_g_cas_damage_between_computation_and_commit_stops_unavailable(scenes):
    s = ready(scenes)
    row = mission_coverage(s.store, s.mission, s.profile)["criteria"][0]
    s.cas.path_for(s.version).unlink()
    final = s.commit.judge_mission(
        s.mission.id,
        judgments=[
            {
                "criterion": QUOTE,
                "criterion_id": row["criterion_id"],
                "met": True,
                "judge": "document_coverage",
            }
        ],
        summary="cached",
    )
    assert final.status is MissionStatus.FAILED
    assert final.stop_reason == "verifier_unavailable"


def test_g_cached_decide_cas_race_stops_failed_unavailable(scenes, monkeypatch):
    import asyncio

    from fixtures_provider import RoleScriptedProvider

    from agent_orchestrator.orchestrator.action_commits import judgment_key
    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.verification import mission_coverage as coverage_module

    s = ready(scenes)
    row = mission_coverage(s.store, s.mission, s.profile)["criteria"][0]
    cached = {
        "criterion": QUOTE,
        "criterion_id": row["criterion_id"],
        "met": True,
        "judge": "document_coverage",
    }
    s.commit.record_criteria_judgment(
        s.mission.id,
        judgment_key(s.store.list_tasks(s.mission.id)),
        [cached],
        summary="prior valid",
    )
    original = coverage_module.mission_coverage

    def damaged(*args, **kwargs):
        s.cas.path_for(s.version).unlink(missing_ok=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(coverage_module, "mission_coverage", damaged)

    async def run():
        provider = RoleScriptedProvider({})
        async with Orchestrator(OrchestratorConfig(evidence_root=s.root), provider) as orch:
            assert await orch._decide(orch.store.get_mission(s.mission.id))
            final = orch.store.get_mission(s.mission.id)
            assert final.status is MissionStatus.FAILED
            assert final.stop_reason == "verifier_unavailable"
            assert provider.calls == 0

    asyncio.run(run())


def test_g_stale_required_basis_precedes_majority_uncertainty(scenes, monkeypatch):
    import test_p33_inconclusive_commits as drivers

    uncertain = ("生产可用性", "长期可靠性")
    s = scenes(
        mission_criteria=(QUOTE, *uncertain), tasks=2, task_criteria_by_index=((QUOTE,), uncertain)
    )
    s.profile = s.commit.domain_for(s.mission.id)
    first = submitted(s, content=QUOTE, mission_candidate_ordinals=(1,))
    produce(first)
    accept(first)
    api = attach(s, s.store).api
    other = "sources/independent.md"
    api.register_source(
        dict(
            mission_id=s.mission.id,
            path=other,
            content=QUOTE + "\n",
            kind="markdown",
            idempotency_key="second",
        )
    )
    with monkeypatch.context() as patch:
        patch.setattr(drivers, "PATH", other)
        second = submitted(s, task_index=1, mission_candidate_ordinals=(2, 3))
        produce(second)
        accept(second)
    current = attach(s, s.store)
    change_source(current, path=PATH, mode="revoke")
    coverage = mission_coverage(s.store, s.mission, s.profile)
    assert [row["verdict"] for row in coverage["criteria"]] == [
        "FAIL",
        "INCONCLUSIVE",
        "INCONCLUSIVE",
    ]
    assert coverage["share"] > coverage["limit"]
    assert not coverage["insufficient"]
    assert s.commit.stop_insufficient_mission(s.mission.id) is None
    final = s.commit.judge_mission(
        s.mission.id,
        judgments=[
            {
                "criterion": row["text"],
                "criterion_id": row["criterion_id"],
                "met": row["verdict"] in {"PASS", "INCONCLUSIVE"},
                "judge": "document_coverage",
            }
            for row in coverage["criteria"]
        ],
        summary="有失效依据且存在局限",
    )
    assert final.status is MissionStatus.FAILED and final.stop_reason == "mission_criteria_unmet"
