"""Doc9 submission references preserve exact frozen IDs, never repair bad hashes.

Ordinal references are a versioned input spelling only. They do not grant evidence,
relax criteria, mutate the original Provider output, or change persisted Claim shape.
"""

import asyncio
import copy
import json
import sqlite3
from contextlib import closing

import pytest
from doc5_helpers import graph_service, node
from fixtures_provider import envelope_step
from test_g_doc5_accept_critic import _prepared, _provider

from agent_orchestrator.contracts import ContractError
from agent_orchestrator.governance import domains
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.verification.assessments import (
    criterion_id,
    mission_contract_revision,
    mission_criterion_catalog,
    task_contract_revision,
)
from agent_orchestrator.verification.document_refs import expand_document_claim_refs


@pytest.fixture
def frozen(tmp_path):
    service, mission, tasks = graph_service(
        tmp_path, domain=domains.DOC_DOMAIN,
        nodes=[node("A", outputs=["report.md"], success_criteria=["file:report.md"])],
    )
    task = next(iter(tasks.values())) if isinstance(tasks, dict) else tasks[0]
    contract = {**task.to_json(), "task_id": task.id}
    config = {"task_contract": contract,
              "mission_contract_revision": mission_contract_revision(mission),
              "mission_criteria": list(mission_criterion_catalog(mission))}
    yield mission, config, criterion_id(task_contract_revision(contract), 1, "file:report.md")
    service.store.close()


def test_exact_frozen_refs_leave_input_and_evidence_unchanged(frozen):
    mission, config, task_id = frozen
    raw = {"task_id": config["task_contract"]["task_id"], "claims": [
        {"content": "not evidence", "criterion_refs": [1], "mission_criterion_refs": [1],
         "citations": [{"quote": "unchanged"}]}]}
    before = copy.deepcopy(raw)
    result = expand_document_claim_refs(raw, intent_config=config, mission=mission)
    assert raw == before
    assert result["claims"] == [{"content": "not evidence",
        "criterion_ids": [task_id],
        "mission_criterion_ids": [config["mission_criteria"][0]["criterion_id"]],
        "citations": [{"quote": "unchanged"}]}]
    assert task_id != config["mission_criteria"][0]["criterion_id"]


@pytest.mark.parametrize("refs", [[0], [-1], [True], [1.0], ["1"], [99], [1, 1], None, "1"])
@pytest.mark.parametrize("field", ["criterion_refs", "mission_criterion_refs"])
def test_invalid_refs_fail_without_guessing(frozen, refs, field):
    mission, config, _ = frozen
    with pytest.raises(ContractError):
        expand_document_claim_refs({"task_id": config["task_contract"]["task_id"],
            "claims": [{field: refs}]}, intent_config=config, mission=mission)


@pytest.mark.parametrize("field", ["criterion", "mission_criterion"])
def test_full_ids_and_refs_cannot_ambiguously_coexist(frozen, field):
    mission, config, _ = frozen
    with pytest.raises(ContractError):
        expand_document_claim_refs({"task_id": config["task_contract"]["task_id"],
            "claims": [{field + "_refs": [1], field + "_ids": []}]},
            intent_config=config, mission=mission)


@pytest.mark.parametrize("damage", ["revision", "catalog", "task"])
def test_different_frozen_identity_is_rejected(frozen, damage):
    mission, config, _ = frozen
    changed = copy.deepcopy(config)
    if damage == "revision":
        changed["mission_contract_revision"] = "a" * 64
    elif damage == "catalog":
        changed["mission_criteria"][0]["criterion_id"] = "criterion-" + "a" * 64
    else:
        changed["task_contract"]["task_id"] = "another-task"
    with pytest.raises(ContractError):
        expand_document_claim_refs({"task_id": config["task_contract"]["task_id"],
            "claims": [{"criterion_refs": [1]}]}, intent_config=changed, mission=mission)


def test_actual_worker_refs_persist_canonical_ids_and_cold_parse_is_identical(
    tmp_path, monkeypatch,
):
    async def run():
        provider = _provider()
        original_outputs = []
        def aliases(body):
            body["claims"][0]["criterion_refs"] = [1]
            body["claims"][0]["mission_criterion_refs"] = [1]
            return body
        emit = envelope_step(summary="报告已写入", artifacts=["report.md"],
                             claims=["报告已写入 report.md。"], override=aliases)
        def observed(request):
            result = emit(request)
            original_outputs.append(result)
            return result
        provider.scripts["worker"][1] = observed
        config = OrchestratorConfig(evidence_root=tmp_path)
        async with Orchestrator(config, provider, owner="refs") as orch:
            mission, _, stored, _, _ = await _prepared(orch, monkeypatch)
            [claim] = stored.envelope.claims
            assert claim.criterion_ids and claim.mission_criterion_ids
            assert "criterion_refs" not in claim.to_json()
            raw_text = original_outputs[0]
            assert '"criterion_refs"' in raw_text
            attempt = orch.store.get_attempt(stored.envelope.attempt_id)
            parsed, _ = orch._parse_envelope(raw_text, attempt, turn_id=attempt.turn_id)
            assert parsed == stored.envelope
            canonical = stored.envelope.to_json()
        async with Orchestrator(config, _provider(), owner="refs") as cold:
            reparsed, _ = cold._parse_envelope(raw_text, attempt, turn_id=attempt.turn_id)
            assert reparsed.to_json() == canonical
            assert cold.commit.domain_for(mission.id).version == "9"
            with closing(sqlite3.connect(config.execution_db)) as connection:
                original_messages = connection.execute(
                    "SELECT message_json FROM base_agent_session_journal_v1 WHERE agent_id=?",
                    (attempt.agent_id,),
                ).fetchall()
            assert any(json.loads(row[0]).get("content") == raw_text for row in original_messages)
    asyncio.run(run())


@pytest.mark.parametrize("profile", [domains.DOC_PROFILE_V8, domains.CODE_PROFILE])
def test_legacy_profiles_reject_refs_in_real_parser(tmp_path, monkeypatch, profile):
    async def run():
        async with Orchestrator(OrchestratorConfig(evidence_root=tmp_path), _provider()) as orch:
            _, _, stored, _, _ = await _prepared(orch, monkeypatch, profile=profile)
            attempt = orch.store.get_attempt(stored.envelope.attempt_id)
            raw = stored.envelope.to_json()
            raw["claims"][0].pop("criterion_ids", None)
            raw["claims"][0]["criterion_refs"] = [1]
            with pytest.raises(ContractError):
                orch._parse_envelope("<result_envelope>" + json.dumps(raw)
                                     + "</result_envelope>", attempt, turn_id=attempt.turn_id)
    asyncio.run(run())


def test_bad_full_hash_is_not_repaired_and_alias_gives_no_extra_evidence(tmp_path, monkeypatch):
    async def run():
        async with Orchestrator(OrchestratorConfig(evidence_root=tmp_path), _provider()) as orch:
            _, _, stored, _, _ = await _prepared(orch, monkeypatch)
            attempt = orch.store.get_attempt(stored.envelope.attempt_id)
            raw = stored.envelope.to_json()
            raw["claims"][0]["mission_criterion_ids"] = ["criterion-" + "a" * 61]
            with pytest.raises(ContractError, match="complete criterion IDs"):
                orch._parse_envelope("<result_envelope>" + json.dumps(raw)
                                     + "</result_envelope>", attempt, turn_id=attempt.turn_id)
            raw["claims"][0].pop("mission_criterion_ids")
            raw["claims"][0]["mission_criterion_refs"] = [1]
            parsed, _ = orch._parse_envelope("<result_envelope>" + json.dumps(raw)
                                             + "</result_envelope>", attempt,
                                             turn_id=attempt.turn_id)
            expected = copy.deepcopy(raw)
            expected["claims"][0].pop("mission_criterion_refs")
            mission = orch.store.get_mission(attempt.mission_id)
            expected["claims"][0]["mission_criterion_ids"] = [
                mission_criterion_catalog(mission)[0]["criterion_id"]]
            same, _ = orch._parse_envelope("<result_envelope>" + json.dumps(expected)
                                           + "</result_envelope>", attempt,
                                           turn_id=attempt.turn_id)
            assert parsed == same  # every grade/verification input remains byte-equivalent
            assert parsed.claims[0].citations == stored.envelope.claims[0].citations
            assert parsed.claims[0].evidence == stored.envelope.claims[0].evidence
    asyncio.run(run())
