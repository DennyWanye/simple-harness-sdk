# SPDX-License-Identifier: Apache-2.0
"""A group (reviews / acceptance / completion readers): plan cases A01–A18.

Pure cases (A01, A02, A04, A05, A09) drive the production codec, formula,
check gate and review decision directly. Runtime cases use the assured fixture
runtime (real Store/Commit/Scope/HtnStore, an actual AgentRuntime with a
scripted reviewer, the original critic entry / collector / official importer /
acceptance writer) and the original completion readers on the MIXED world.
Seam-backed cases (A04 executor, A10 evidence tools, A12 format repair) run the
item 4/5 seams as child processes and assert on their reports. No real model,
no Host.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_orchestrator.assurance.checks import (
    CheckResult,
    CriterionPolicy,
    Formula,
    Grade,
    ReviewReply,
    decide_review,
    evaluate_check_gate,
)
from agent_orchestrator.assurance.codec import AssuranceError, canonical, decode, fingerprint
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.assurance.reviews import AssuranceReviewBinding, ReviewRecordBinding
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.resolution import (
    AllExpr,
    AnyExpr,
    CriterionExpr,
    RequirementClass,
    criteria_only_under_any,
    hard_constraints_not_independent,
    parse_success_expression,
    unknown_expression_criteria,
)
from agent_orchestrator.orchestrator.assurance_purpose_reviews import (
    composition_subject,
    purpose_review_key,
)
from agent_orchestrator.orchestrator.assurance_review_import import review_subject_stopped
from agent_orchestrator.orchestrator.completion_status import read_occurrence_completion
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.storage.assurance_store import AssuranceStore
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.store import StoreConflict

SDK_ROOT = Path(__file__).resolve().parents[4]
SEAMS = SDK_ROOT / "scripts/assurance_seams"
sys.path.insert(0, str(SEAMS))
HASH = "a" * 64
ACCEPT_REPLY = {"schema_version": 2, "verdict": "ACCEPT", "assessments": [
    {"criterion_id": "criterion-report", "verdict": "PASS", "evidence_ids": [], "reason": "fixture", "limitations": []}],
    "findings": []}


def _refused(call, *codes):
    with pytest.raises(AssuranceError) as raised:
        call()
    assert raised.value.code in codes, (raised.value.code, codes)
    return raised.value.code


def _seam(name, *, json_summary=True):
    completed = subprocess.run([sys.executable, str(SEAMS / name)], capture_output=True, text=True, timeout=900,
                               cwd=str(SDK_ROOT))
    assert completed.returncode == 0, (name, completed.stderr[-4000:])
    last = completed.stdout.strip().splitlines()[-1]
    if json_summary:
        summary = json.loads(last)
        assert summary["status"] == "PASS", summary
        return json.loads(Path(summary["evidence"]).read_text())
    assert last.startswith("PASS ")
    return json.loads(Path(last[5:].strip()).read_text())


def spec_ref(name):
    return AssuranceRef("check_spec", Pin(name, 1, HASH))


def receipt_ref(name):
    return AssuranceRef("local_check_receipt", Pin(name, 0, HASH))


def result(name, grade=Grade.PASS, *, state="SUCCEEDED", valid=True, receipt=None):
    return CheckResult(spec_ref(name), receipt or receipt_ref("rcpt-" + name), state, grade, valid)


def reply(**grades):
    return ReviewReply.from_json({"schema_version": 2, "verdict": "ACCEPT", "findings": [], "assessments": [
        {"criterion_id": name, "verdict": str(grade.value if hasattr(grade, "value") else grade), "evidence_ids": [],
         "reason": "r", "limitations": []} for name, grade in grades.items()]})


def count(store, sql, *params):
    return store.connection.execute(sql, params).fetchone()[0]


def _readers(rt):
    dispatch = HierarchicalDispatch(rt.store, rt.commit)
    network = dispatch.network(rt.mission.id)
    return dispatch, network, network.root_occurrence_ids[0]


async def _accepted(rt):
    """Official TASK_CONTENT review + the router's layer + the production acceptance writer."""
    verdict, record = await rt.run_critic()
    assert verdict.passed
    rt.record_critic_layer(record)
    rt.settle_fixture_worker()
    completed = rt.accept_now()
    assert completed.accepted_result_id == rt.stored.envelope.id
    return record


# --------------------------------------------------------------------------- A01
def test_strict_boundary():
    good = canonical(ACCEPT_REPLY)
    assert ReviewReply.from_json(decode(good)).verdict == "ACCEPT"
    # Codec boundary: each malformed body is refused before any interpretation.
    _refused(lambda: decode('{"a": 1, "a": 2}'), "JSON_DUPLICATE_KEY")
    _refused(lambda: decode('{"a": NaN}'), "JSON_NUMBER_INVALID", "JSON_INVALID")
    _refused(lambda: decode('{"a": 1e400}'), "JSON_NUMBER_INVALID", "JSON_INTEGER_OVERFLOW", "JSON_INVALID")
    _refused(lambda: decode('{"a": 9007199254740992}'), "JSON_INTEGER_OVERFLOW")
    _refused(lambda: decode('{"a": "\\udc00"}'), "JSON_UNICODE", "JSON_INVALID")
    _refused(lambda: decode("[" * 200 + "]" * 200), "JSON_DEPTH_LIMIT")
    _refused(lambda: decode("x" * (256 * 1024 + 1)), "JSON_BYTES_LIMIT")
    _refused(lambda: canonical({"a": float("nan")}), "JSON_NUMBER_INVALID", "JSON_INVALID")
    _refused(lambda: canonical({1: "non-text key"}), "JSON_OBJECT_KEY", "JSON_INVALID")
    # Review reply: unknown verdict, bool version, wrong version, duplicate criterion,
    # foreign finding, unknown field, empty assessments.
    _refused(lambda: ReviewReply.from_json({**ACCEPT_REPLY, "verdict": "OFFICIAL"}), "ENUM_INVALID")
    _refused(lambda: ReviewReply.from_json({**ACCEPT_REPLY, "schema_version": True}), "INTEGER_INVALID")
    _refused(lambda: ReviewReply.from_json({**ACCEPT_REPLY, "schema_version": 1}), "REVIEW_SCHEMA_VERSION")
    _refused(lambda: ReviewReply.from_json({**ACCEPT_REPLY, "assessments": ACCEPT_REPLY["assessments"] * 2}),
             "DUPLICATE_CRITERION")
    _refused(lambda: ReviewReply.from_json({**ACCEPT_REPLY, "findings": [
        {"criterion_id": "other", "severity": "BLOCKER", "reason": "x"}]}), "FINDING_SCOPE")
    _refused(lambda: ReviewReply.from_json({**ACCEPT_REPLY, "extra": 1}), "OBJECT_FIELDS_UNKNOWN")
    _refused(lambda: ReviewReply.from_json({k: v for k, v in ACCEPT_REPLY.items() if k != "findings"}),
             "OBJECT_FIELDS_MISSING")
    _refused(lambda: ReviewReply.from_json({**ACCEPT_REPLY, "assessments": []}), "ARRAY_INVALID")
    _refused(lambda: ReviewReply.from_json({**ACCEPT_REPLY, "assessments": [
        {**ACCEPT_REPLY["assessments"][0], "verdict": "MAYBE"}]}), "ENUM_INVALID")
    # The official record binding refuses a raw output whose bytes do not hash to the pinned raw ref.
    body = {"schema_version": 2, "mission_id": "m", "review_key": "k",
            "package_ref": {"id": "p", "revision": 1, "content_hash": HASH},
            "record_ref": {"id": "r", "revision": 1, "content_hash": HASH}, "reviewer_agent_id": "reviewer",
            "reviewer_turn_ref": {"kind": "agent_turn_receipt", "pin": {"id": "t", "revision": 0, "content_hash": HASH}},
            "raw_output_ref": {"kind": "artifact", "pin": {"id": "raw", "revision": 1, "content_hash": HASH}},
            "raw_output_hash": "b" * 64, "codec_version": "assurance-review-reply-v2", "consumed_check_refs": [],
            "exposed_evidence_refs": [], "evidence_manifest_hash": HASH,
            "binding_read_set": [{"channel": "OBJECT", "key": "o", "fingerprint": HASH, "coverage": "COMPLETE"}],
            "disclosure_refs": [], "invocation_ordinal": 1}
    _refused(lambda: ReviewRecordBinding.from_json(body), "REVIEW_RAW_HASH_MISMATCH")
    ok = ReviewRecordBinding.from_json({**body, "raw_output_hash": HASH})
    assert ok.content_hash == fingerprint(ok.to_json())
    _refused(lambda: ReviewRecordBinding.from_json({**body, "raw_output_hash": HASH, "invocation_ordinal": 3}),
             "INTEGER_INVALID")
    _refused(lambda: ReviewRecordBinding.from_json({**body, "raw_output_hash": HASH, "schema_version": 3}),
             "REVIEW_RECORD_BINDING_VERSION")
    _refused(lambda: ReviewRecordBinding.from_json({**body, "raw_output_hash": HASH, "binding_read_set": [
        body["binding_read_set"][0], body["binding_read_set"][0]]}), "DUPLICATE_READ_KEY")


# --------------------------------------------------------------------------- A02
def test_formula_truth_tables():
    names = frozenset({"a", "b", "m"})
    all_of = Formula.from_json({"all": [{"criterion": "a"}, {"criterion": "b"}]}, names)
    any_of = Formula.from_json({"any": [{"criterion": "a"}, {"criterion": "b"}]}, names)
    grades = [Grade.PASS, Grade.FAIL, Grade.UNKNOWN]
    for ga in grades:
        for gb in grades:
            g = {"a": ga, "b": gb, "m": Grade.PASS}
            expect_all = (Grade.FAIL if Grade.FAIL in (ga, gb)
                          else (Grade.PASS if ga is gb is Grade.PASS else Grade.UNKNOWN))
            expect_any = (Grade.PASS if Grade.PASS in (ga, gb)
                          else (Grade.FAIL if ga is gb is Grade.FAIL else Grade.UNKNOWN))
            assert all_of.evaluate(g)[0] is expect_all, (ga, gb)
            assert any_of.evaluate(g)[0] is expect_any, (ga, gb)
    assert all_of.evaluate({"a": Grade.PASS, "b": Grade.PASS})[1] == {"a", "b"}
    assert any_of.evaluate({"a": Grade.FAIL, "b": Grade.PASS})[1] == {"b"}
    # Unknown criterion / empty formula / illegal node never become a formula.
    _refused(lambda: Formula.from_json({"criterion": "zz"}, names), "FORMULA_UNKNOWN_CRITERION")
    _refused(lambda: Formula.from_json({"all": []}, names), "ARRAY_INVALID", "FORMULA_INVALID")
    _refused(lambda: Formula.from_json({"not": {"criterion": "a"}}, names), "FORMULA_INVALID")
    _refused(lambda: Formula.from_json({"all": [{"criterion": "a"}], "any": []}, names), "FORMULA_INVALID")
    _refused(lambda: all_of.evaluate({"a": Grade.PASS}), "FORMULA_UNKNOWN_CRITERION")
    deep = {"criterion": "a"}
    for _ in range(20):
        deep = {"all": [deep]}
    _refused(lambda: Formula.from_json(deep, names), "FORMULA_LIMIT")
    # Mandatory criteria are ANDed on top and cannot be bypassed by an ANY branch.
    policies = {n: CriterionPolicy(n, "SEMANTIC", ()) for n in names}
    decision = decide_review(reply(a=Grade.FAIL, b=Grade.PASS, m=Grade.FAIL), any_of, ("m",), policies, {})
    assert not decision.acceptable and decision.effective_grades["m"] is Grade.FAIL
    decision = decide_review(reply(a=Grade.FAIL, b=Grade.PASS, m=Grade.UNKNOWN), any_of, ("m",), policies, {})
    assert not decision.acceptable  # unevaluated stays UNKNOWN, never PASS
    decision = decide_review(reply(a=Grade.FAIL, b=Grade.PASS, m=Grade.PASS), any_of, ("m",), policies, {})
    assert decision.acceptable and decision.success_witness == {"b", "m"}
    _refused(lambda: decide_review(reply(a=Grade.PASS, b=Grade.PASS, m=Grade.PASS), any_of, ("zz",), policies, {}),
             "MANDATORY_CRITERIA_INVALID")
    _refused(lambda: decide_review(reply(a=Grade.PASS, b=Grade.PASS), any_of, (), policies, {}),
             "POLICY_CATALOGUE_MISMATCH")
    # Approved requirements: an illegal success expression is refused at the contract layer.
    with pytest.raises(ContractError):
        AllExpr(())
    with pytest.raises(ContractError):
        parse_success_expression({"op": "all", "children": []})
    with pytest.raises(ContractError):
        parse_success_expression({"op": "xor", "children": [{"op": "criterion", "criterion_id": "a"}]})
    expression = parse_success_expression({"op": "any", "children": [
        {"op": "criterion", "criterion_id": "a"}, {"op": "criterion", "criterion_id": "b"}]})
    assert isinstance(expression, AnyExpr) and criteria_only_under_any(expression) == {"a", "b"}
    assert unknown_expression_criteria(expression, ()) == ("a", "b")


# --------------------------------------------------------------------------- A03
def test_requirement_authority(tmp_path):
    from _assured_fixture import build_world

    world, task, stored, artifact, scope_ref = build_world(tmp_path)
    store, mission_id = world.store, world.mission.id
    htn = HtnStore(store)
    original = htn.get_requirements_revision(mission_id, 1)
    row_sql = ("SELECT revision_json,content_hash,authority_subject FROM requirements_revisions "
               "WHERE mission_id=? AND revision=1")
    original_row = tuple(store.connection.execute(row_sql, (mission_id,)).fetchone())
    # A planner-side rewrite of the same revision (dropping a criterion) is refused; bytes stay.
    reduced = dataclasses.replace(original, criteria=original.criteria[:1],
                                  success_expression=CriterionExpr(original.criteria[0].criterion_id),
                                  authority_subject="planner")
    with pytest.raises(StoreConflict):
        htn.insert_requirements_revision(reduced)
    assert tuple(store.connection.execute(row_sql, (mission_id,)).fetchone()) == original_row
    # A re-confirmation lands as a new revision with its own digest; the old bytes remain.
    confirmed = dataclasses.replace(original, revision=2, revision_id="completion-requirements-2")
    digest = htn.insert_requirements_revision(confirmed)
    assert [r.revision for r in htn.list_requirements_revisions(mission_id)] == [1, 2]
    assert htn.latest_requirements_revision(mission_id).revision == 2
    assert htn.get_requirements_revision(mission_id, 1).to_json() == original.to_json()
    assert digest == confirmed.content_hash() != original.content_hash()
    # Hard constraints can never be moved under an ANY, and an expression may only
    # name catalogued criteria.
    hard = [c for c in original.criteria if c.requirement_class is RequirementClass.HARD_CONSTRAINT]
    if hard:
        bad = AnyExpr(tuple(CriterionExpr(c.criterion_id) for c in original.criteria))
        assert hard_constraints_not_independent(bad, original.criteria)
    assert hard_constraints_not_independent(original.success_expression, original.criteria) == ()
    assert unknown_expression_criteria(original.success_expression, original.criteria) == ()
    assert unknown_expression_criteria(AllExpr((CriterionExpr("ghost"),)), original.criteria) == ("ghost",)


# --------------------------------------------------------------------------- A04
def test_check_receipt_scope():
    policy = CriterionPolicy("c", "CHECKED", ((spec_ref("fmt"), spec_ref("rule")),))
    good = {spec_ref("fmt"): result("fmt"), spec_ref("rule"): result("rule")}
    gate = evaluate_check_gate(policy, good)
    assert gate.grade is Grade.PASS and gate.reason == "CHECK_GROUP_SATISFIED"
    assert set(gate.consumed) == {receipt_ref("rcpt-fmt"), receipt_ref("rcpt-rule")}
    # The same real execution's receipt offered for another spec: identity mismatch.
    swapped = {spec_ref("fmt"): result("fmt"), spec_ref("rule"): result("fmt")}
    _refused(lambda: evaluate_check_gate(policy, swapped), "CHECK_RESULT_IDENTITY")
    # A receipt from another checkspec / target / manifest never satisfies: absent -> incomplete.
    partial = {spec_ref("fmt"): result("fmt"), spec_ref("other"): result("other")}
    gate = evaluate_check_gate(policy, partial)
    assert gate.grade is Grade.UNKNOWN and gate.reason == "CHECK_EVIDENCE_INCOMPLETE"
    # Real failure / ERROR / NOT_RUN / invalid source are never PASS.
    assert result("fmt", Grade.FAIL).effective is Grade.FAIL
    for state in ("ERROR", "CANCELLED", "NOT_RUN", "RUNNING"):
        assert result("fmt", state=state).effective is Grade.UNKNOWN
    assert result("fmt", valid=False).effective is Grade.UNKNOWN
    failed = {spec_ref("fmt"): result("fmt", Grade.FAIL), spec_ref("rule"): result("rule")}
    gate = evaluate_check_gate(policy, failed)
    assert gate.grade is Grade.FAIL and gate.reason == "CHECK_GROUPS_FAILED"
    _refused(lambda: CheckResult(receipt_ref("x"), receipt_ref("x"), "SUCCEEDED", Grade.PASS, True), "CHECK_REF_INVALID")
    _refused(lambda: CheckResult(spec_ref("x"), receipt_ref("x"), "DONE", Grade.PASS, True), "ENUM_INVALID")
    _refused(lambda: CheckResult(spec_ref("x"), receipt_ref("x"), "SUCCEEDED", "PASS", True), "CHECK_RESULT_TYPE")
    _refused(lambda: CriterionPolicy("c", "CHECKED", ((receipt_ref("x"),),)), "CHECK_SPEC_REF_REQUIRED")
    _refused(lambda: CriterionPolicy("c", "SEMANTIC", ((spec_ref("x"),),)), "SEMANTIC_HAS_CHECKS")
    _refused(lambda: CriterionPolicy("c", "CHECKED", ()), "CHECKED_GROUPS_REQUIRED")
    _refused(lambda: CriterionPolicy("c", "CHECKED", ((spec_ref("x"),), (spec_ref("x"),))), "DUPLICATE_CHECK_GROUP")
    # The real executor seam: code_test through the sandbox, receipts imported, never re-run.
    report = _seam("executor-check-seam.py")
    assert report["passing"]["binding_verdict"] == "PASS" and report["replay_idempotent"] is True
    assert report["failing"]["binding_verdict"] == "FAIL"
    assert report["workspace_mutated"]["binding_verdict"] != "PASS"
    assert report["timeout"]["binding_verdict"] == "UNKNOWN" and report["timeout"]["state"] == "CANCELLED"
    assert report["no_executor_receipt"]["binding_verdict"] != "PASS"
    assert report["exit0_without_nodeids"]["binding_verdict"] == "UNKNOWN"
    assert report["events"]["AssuranceExecutionImported"] == report["bindings_total"]


# --------------------------------------------------------------------------- A05
def test_any_branch_not_mandatory():
    names = frozenset({"a", "b", "privacy"})
    formula = Formula.from_json({"any": [{"criterion": "a"}, {"criterion": "b"}]}, names)
    policies = {"a": CriterionPolicy("a", "CHECKED", ((spec_ref("a-check"),),)),
                "b": CriterionPolicy("b", "CHECKED", ((spec_ref("b-check"),),)),
                "privacy": CriterionPolicy("privacy", "SEMANTIC", ())}
    b_ok = {spec_ref("b-check"): result("b-check")}
    # A fails or is untested; B really succeeded: B carries, A does not block.
    decision = decide_review(reply(a=Grade.FAIL, b=Grade.PASS, privacy=Grade.PASS), formula, ("privacy",), policies, b_ok)
    assert decision.acceptable and decision.success_witness == {"b", "privacy"}
    assert decision.consumed_receipts == (receipt_ref("rcpt-b-check"),)
    decision = decide_review(reply(a=Grade.PASS, b=Grade.PASS, privacy=Grade.PASS), formula, ("privacy",), policies, b_ok)
    assert decision.acceptable and decision.success_witness == {"b", "privacy"}
    assert decision.effective_grades["a"] is Grade.UNKNOWN  # model PASS without its real check is not PASS
    # Privacy failing blocks whatever the branches say.
    both = {**b_ok, spec_ref("a-check"): result("a-check")}
    decision = decide_review(reply(a=Grade.PASS, b=Grade.PASS, privacy=Grade.FAIL), formula, ("privacy",), policies, both)
    assert not decision.acceptable and decision.success_witness == frozenset()
    # Both branches complete: frozen formula order picks the first PASS, never model ranking.
    decision = decide_review(reply(a=Grade.PASS, b=Grade.PASS, privacy=Grade.PASS), formula, ("privacy",), policies, both)
    assert decision.success_witness == {"a", "privacy"} and decision.consumed_receipts == (receipt_ref("rcpt-a-check"),)
    # A BLOCKER finding on the chosen branch forces its grade to FAIL.
    blocked = ReviewReply.from_json({"schema_version": 2, "verdict": "ACCEPT", "assessments": [
        {"criterion_id": n, "verdict": "PASS", "evidence_ids": [], "reason": "r", "limitations": []}
        for n in ("a", "b", "privacy")],
        "findings": [{"criterion_id": "b", "severity": "BLOCKER", "reason": "leak"}]})
    decision = decide_review(blocked, formula, ("privacy",), policies, b_ok)
    assert not decision.acceptable and decision.effective_grades["b"] is Grade.FAIL
    # A non-ACCEPT verdict is never acceptable even when every grade passes.
    rework = ReviewReply.from_json({**ACCEPT_REPLY, "verdict": "REWORK", "assessments": [
        {"criterion_id": n, "verdict": "PASS", "evidence_ids": [], "reason": "r", "limitations": []}
        for n in ("a", "b", "privacy")]})
    assert not decide_review(rework, formula, ("privacy",), policies, both).acceptable


# --------------------------------------------------------------------------- A06
def test_review_dispatch_atomic(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            tables = ("review_packages", "assurance_review_bindings", "assurance_review_invocations",
                      "assurance_blob_pins", "dispatch_intents", "budget_reservations")

            def counts():
                return {t: count(store, f"SELECT COUNT(*) FROM {t} WHERE mission_id=?", mission_id) for t in tables}

            before = counts()
            # The preparation UoW dies at the CAS pin: package/binding/invocation/intent/
            # reservation all roll back together.
            with patch.object(AssuranceStore, "acquire_pin", side_effect=OSError("cut: pin")):
                with pytest.raises(Exception):
                    await rt.run_critic()
            assert counts() == before and not store.connection.in_transaction
            assert rt.provider.calls == 0  # no dispatch happened
            # Then one real dispatch: one logical slot, one invocation, one model call.
            verdict, record = await rt.run_critic()
            assert verdict.passed and record is not None
            after = counts()
            assert after["assurance_review_invocations"] == before["assurance_review_invocations"] + 1
            assert after["review_packages"] == before["review_packages"] + 1
            assert rt.provider.calls == 1
            # Re-sending the same logical review does not create a second package/invocation or call.
            verdict_again, record_again = await rt.run_critic()
            assert record_again.record_id == record.record_id and counts() == after and rt.provider.calls == 1

    asyncio.run(body())


# --------------------------------------------------------------------------- A07
def test_reviewer_independence(tmp_path):
    from _assured_fixture import AssuredRuntime

    def producers(store, mission_id):
        row = store.connection.execute(
            "SELECT binding_json FROM assurance_review_bindings WHERE mission_id=? LIMIT 1", (mission_id,)).fetchone()
        return [] if row is None else decode(row["binding_json"])["producer_agent_ids"]

    async def independent():
        async with AssuredRuntime(tmp_path / "independent", [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            verdict, record = await rt.run_critic()
            assert verdict.passed
            bound = producers(store, mission_id)
            assert bound  # the author is named
            binding = decode(count(store, "SELECT binding_json FROM assurance_review_record_bindings WHERE record_id=?",
                                   str(record.record_id)))
            assert binding["reviewer_agent_id"] not in bound
            reviewer = count(store, "SELECT agent_id FROM dispatch_intents i JOIN assurance_review_invocations v "
                             "ON v.dispatch_intent_id=i.intent_id WHERE v.mission_id=? AND v.ordinal=1", mission_id)
            assert reviewer == binding["reviewer_agent_id"]

    async def forged():
        from agent_orchestrator.orchestrator import assurance_review_import as importer

        async with AssuredRuntime(tmp_path / "forged", [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            original = importer.read_review_invocation_locked

            class ClaimsAuthor:
                """The binding exactly as stored, except that the frozen producer set
                (as the official importer reads it) names the reviewer agent."""

                def __init__(self, binding, reviewer):
                    self._binding, self._reviewer = binding, reviewer

                def __getattr__(self, name):
                    return getattr(self._binding, name)

                def to_json(self):
                    body = self._binding.to_json()
                    body["producer_agent_ids"] = sorted(set(body["producer_agent_ids"]) | {self._reviewer})
                    return body

            def forged_read(commit, reader, intent_id):
                invocation, binding = original(commit, reader, intent_id)
                intent = store.get_intent(intent_id)
                if intent is not None and intent.agent_id is not None:
                    return invocation, ClaimsAuthor(binding, intent.agent_id)
                return invocation, binding

            with patch.object(importer, "read_review_invocation_locked", forged_read):
                with pytest.raises(Exception) as raised:
                    await rt.run_critic()
            assert "REVIEW_INDEPENDENCE_REQUIRED" in str(raised.value), str(raised.value)
            packages = [row[0] for row in store.connection.execute(
                "SELECT package_id FROM review_packages WHERE mission_id=?", (mission_id,))]
            assert packages and all(HtnStore(store).official_review_record(p) is None for p in packages)
            assert count(store, "SELECT COUNT(*) FROM review_records WHERE mission_id=?", mission_id) == 0
            assert count(store, "SELECT COUNT(*) FROM acceptances WHERE mission_id=?", mission_id) == 0
            with store.read_view():
                assert rt.runner.task_record(mission_id, rt.stored.envelope.attempt_id) is None

    asyncio.run(independent())
    asyncio.run(forged())


# --------------------------------------------------------------------------- A08
def test_review_source_binding(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            verdict, record = await rt.run_critic()
            binding = decode(count(store, "SELECT binding_json FROM assurance_review_record_bindings WHERE record_id=?",
                                   str(record.record_id)))
            assert binding["mission_id"] == mission_id and binding["invocation_ordinal"] == 1
            # Every source is pinned: mission, turn receipt, raw output hash, package, ordinal, codec.
            base = ReviewRecordBinding.from_json(binding).content_hash
            for change in ({"mission_id": "other"},
                           {"reviewer_turn_ref": {"kind": "agent_turn_receipt",
                                                  "pin": {"id": "t2", "revision": 0, "content_hash": "c" * 64}}},
                           {"invocation_ordinal": 2}, {"codec_version": "assurance-review-reply-v1"},
                           {"package_ref": {**binding["package_ref"], "content_hash": "d" * 64}}):
                assert ReviewRecordBinding.from_json({**binding, **change}).content_hash != base
            _refused(lambda: ReviewRecordBinding.from_json({**binding, "raw_output_hash": "b" * 64}),
                     "REVIEW_RAW_HASH_MISMATCH")
            # The same source replayed: one official record per package, one invocation, one call.
            packages = [row[0] for row in store.connection.execute(
                "SELECT package_id FROM review_packages WHERE mission_id=?", (mission_id,))]
            assert len(packages) == 1
            assert HtnStore(store).official_review_record(packages[0]).record_id == record.record_id
            assert count(store, "SELECT COUNT(*) FROM assurance_review_invocations WHERE mission_id=?", mission_id) == 1
            assert count(store, "SELECT COUNT(*) FROM commit_receipts WHERE kind='AssuranceReviewTurnImported'") == 1
            assert rt.provider.calls == 1
            again = await rt.run_critic()
            assert again[1].record_id == record.record_id and rt.provider.calls == 1
            assert count(store, "SELECT COUNT(*) FROM review_records WHERE mission_id=? AND official=1", mission_id) == 1

    asyncio.run(body())


# --------------------------------------------------------------------------- A09
def test_review_new_round_identity():
    key1 = purpose_review_key("MISSION_FINAL", "m", "package-1")
    assert key1 == purpose_review_key("MISSION_FINAL", "m", "package-1")  # same round, same key
    assert key1 != purpose_review_key("MISSION_FINAL", "m", "package-2")  # a real new round is a new package
    assert key1 != purpose_review_key("TASK_CONTENT", "m", "package-1")
    assert key1 != purpose_review_key("MISSION_FINAL", "m2", "package-1")
    assert key1.startswith("assurance-mission-final:")
    # A notification/event id is not a round: the key never takes one.
    with pytest.raises(TypeError):
        purpose_review_key("MISSION_FINAL", "m", "package-1", "event-7")  # type: ignore[call-arg]
    _refused(lambda: AssuranceReviewBinding.from_json({"schema_version": 2}), "OBJECT_FIELDS_MISSING")


# --------------------------------------------------------------------------- A10
def test_extra_evidence_exposure():
    report = _seam("evidence-tools-seam.py", json_summary=False)
    complete = report["task_content_complete_read"]
    assert complete["record_id"] and complete["appended_label"] and complete["replay_added_batch"] is False
    assert len(complete["batches"]) >= 2 and complete["verdict"].endswith("ACCEPT")
    partial = report["task_content_partial_read"]
    assert partial["rejection"] == "UNEXPOSED_EVIDENCE" and partial["batches"] == [0]
    assert report["mission_final_no_attempt"]["record_id"]


# --------------------------------------------------------------------------- A11
def test_scope_pinned_not_latest(tmp_path):
    from _assured_fixture import AssuredRuntime

    from agent_orchestrator.orchestrator.operation_completion import OperationCompletionError

    async def moved_requirements():
        async with AssuredRuntime(tmp_path / "moved", [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            verdict, record = await rt.run_critic()
            row = store.connection.execute(
                "SELECT * FROM assurance_review_bindings WHERE mission_id=? LIMIT 1", (mission_id,)).fetchone()
            bound = AssuranceReviewBinding(row["binding_json"])
            assert row["requirements_revision"] == 1  # frozen v1
            assert not review_subject_stopped(store, bound)
            # A later requirements revision does not move the frozen review: the v1
            # record stays bound to v1, nothing re-reviews under v2, and the
            # acceptance writer refuses to use a v1 scope against moved requirements.
            original = HtnStore(store).get_requirements_revision(mission_id, 1)
            HtnStore(store).insert_requirements_revision(dataclasses.replace(original, revision=2, revision_id="req-2"))
            assert HtnStore(store).official_review_record(row["package_id"]).record_id == record.record_id
            assert count(store, "SELECT requirements_revision FROM assurance_review_bindings WHERE review_key=?",
                         row["review_key"]) == 1
            assert count(store, "SELECT requirements_revision FROM review_packages WHERE package_id=?",
                         row["package_id"]) == 1
            assert count(store, "SELECT COUNT(*) FROM assurance_review_bindings WHERE mission_id=?", mission_id) == 1
            rt.record_critic_layer(record)
            rt.settle_fixture_worker()
            with pytest.raises(OperationCompletionError) as raised:
                rt.accept_now()
            assert raised.value.code == "OP_EFFECT_SCOPE_STALE"
            assert count(store, "SELECT COUNT(*) FROM acceptances WHERE mission_id=?", mission_id) == 0
            assert rt.provider.calls == 1

    async def terminal_subject():
        async with AssuredRuntime(tmp_path / "terminal", [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            record = await _accepted(rt)
            row = store.connection.execute(
                "SELECT * FROM assurance_review_bindings WHERE mission_id=? LIMIT 1", (mission_id,)).fetchone()
            bound = AssuranceReviewBinding(row["binding_json"])
            assert count(store, "SELECT requirements_revision FROM acceptances WHERE mission_id=?", mission_id) == 1
            assert HtnStore(store).official_review_record(row["package_id"]).record_id == record.record_id
            # Once the reviewed Attempt is terminal the subject is stopped: a late
            # answer cannot re-review it.
            assert review_subject_stopped(store, bound)

    asyncio.run(moved_requirements())
    asyncio.run(terminal_subject())


# --------------------------------------------------------------------------- A12
def test_check_budget_and_format_bounds():
    report = _seam("critic-format-repair-seam.py")
    assert report["status"] == "PASS"
    counts = report["counts"]
    assert counts["official_records"] == 1 and counts["provider_calls"] == 2  # exactly one funded repair


# --------------------------------------------------------------------------- A13
def test_preparation_does_not_complete(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            await _accepted(rt)
            dispatch, network, occurrence = _readers(rt)
            status = read_occurrence_completion(store, mission_id, str(occurrence))
            assert status.preparation_ready and status.content_ready
            assert not status.effects_ready and not status.complete
            assert not dispatch.terminal(mission_id) and not dispatch.root_review_ready(mission_id)
            assert store.get_mission(mission_id).status.value == "ACTIVE"
            assert count(store, "SELECT COUNT(*) FROM acceptances WHERE mission_id=?", mission_id) == 1
            assert count(store, "SELECT COUNT(*) FROM goal_resolutions WHERE mission_id=?", mission_id) == 0

    asyncio.run(body())


# --------------------------------------------------------------------------- A14
def test_preparation_data_not_order(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            await _accepted(rt)
            dispatch, network, occurrence = _readers(rt)
            index = dispatch.accepted_outputs(mission_id, network)
            # DATA: the accepted artifact is readable by purpose from the preparation.
            assert occurrence in index.completed_producers
            assert [item.artifact_id for item in index.outputs] == [rt.artifact.id]
            # ORDER: the occurrence is not complete, so nothing downstream is released.
            status = read_occurrence_completion(store, mission_id, str(occurrence))
            assert status.preparation_acceptance_ids and not status.complete
            assert not dispatch.terminal(mission_id)
            with pytest.raises(Exception):
                read_occurrence_completion(store, mission_id, "occurrence-without-scope")

    asyncio.run(body())


# --------------------------------------------------------------------------- A15
def test_composition_not_all_children(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            record = await _accepted(rt)
            dispatch, network, occurrence = _readers(rt)
            # Every child (here: the single leaf) accepted, yet the root is not ready:
            # readiness is not all(children.completed).
            assert occurrence in dispatch.accepted_outputs(mission_id, network).completed_producers
            assert not dispatch.root_review_ready(mission_id) and not dispatch.terminal(mission_id)
            package = HtnStore(store).get_review_package(str(record.package_id))
            # A COMPOSITION subject needs the compound's own frozen scope and adopted
            # method instance; a leaf package cannot stand in for it.
            _refused(lambda: composition_subject(store, mission_id=mission_id, package=package,
                                                 occurrence_id=str(occurrence), accepted={}), "SOURCE_UNAVAILABLE")

    asyncio.run(body())


# --------------------------------------------------------------------------- A16
def test_pending_effect_no_worker_loop(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            await _accepted(rt)
            dispatch, network, occurrence = _readers(rt)
            attempts = count(store, "SELECT COUNT(*) FROM attempts WHERE mission_id=?", mission_id)
            results = count(store, "SELECT COUNT(*) FROM results WHERE mission_id=?", mission_id)
            for _ in range(5):  # repeated durable ticks: the wait stays visible, nothing regenerates
                for _ in range(3):
                    await rt.pump.tick()
                status = read_occurrence_completion(store, mission_id, str(occurrence))
                assert status.preparation_ready and not status.complete
                assert not dispatch.terminal(mission_id)
            assert count(store, "SELECT COUNT(*) FROM attempts WHERE mission_id=?", mission_id) == attempts
            assert count(store, "SELECT COUNT(*) FROM results WHERE mission_id=?", mission_id) == results
            mission = store.get_mission(mission_id)
            assert mission.status.value == "ACTIVE" and mission.stop_reason is None
            assert rt.provider.calls == 1 and not rt.pump.rejections

    asyncio.run(body())


# --------------------------------------------------------------------------- A17
def test_root_requirement_effect_catalogue(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            dispatch, network, occurrence = _readers(rt)
            requirements = HtnStore(store).get_requirements_revision(mission_id, 1)
            assert [c.criterion_id for c in requirements.criteria] == ["criterion-report", "criterion-delivered"]
            # The frozen scope catalogues one owed effect; no intent/action exists yet.
            status = read_occurrence_completion(store, mission_id, str(occurrence))
            assert status.scope.content_criterion_ids == ("criterion-report",)
            assert len(status.scope.required_effect_keys) == 1
            assert not status.effects_ready and not status.complete
            assert not dispatch.root_review_ready(mission_id)
            # Content accepted does not settle the effect either.
            await _accepted(rt)
            status = read_occurrence_completion(store, mission_id, str(occurrence))
            assert status.content_ready and not status.effects_ready and not status.complete
            assert not dispatch.root_review_ready(mission_id)

    asyncio.run(body())


# --------------------------------------------------------------------------- A18
def test_direct_final_commit_guard(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
            store, mission_id = rt.store, rt.mission.id
            verdict, record = await rt.run_critic()
            rt.record_critic_layer(record)
            rt.settle_fixture_worker()
            # Without the deployment's validity evaluator (a "ready" cache is not one)
            # the writer refuses; nothing is written.
            rt.commit._assurance_validity = None
            with pytest.raises(Exception) as raised:
                rt.accept_now()
            assert "USE_CERTIFICATE_REQUIRED" in str(raised.value)
            assert count(store, "SELECT COUNT(*) FROM acceptances WHERE mission_id=?", mission_id) == 0
            assert count(store, "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=?", mission_id) == 0
            rt.commit._assurance_validity = rt.validity
            # A prepared candidate for another consumer identity is refused inside the UoW.
            real_prepare = rt.validity.prepare_accept_use_for_result

            def other_consumer(mission, result_id):
                candidate = real_prepare(mission, result_id)
                swapped = dataclasses.replace(candidate, identity=dataclasses.replace(candidate.identity,
                                                                                     consumer_id="other"))
                rt.validity._remember(swapped)
                return swapped

            with patch.object(rt.validity, "prepare_accept_use_for_result", other_consumer):
                with pytest.raises(Exception) as raised:
                    rt.accept_now()
            assert "USE_CERTIFICATE_IDENTITY" in str(raised.value)
            assert count(store, "SELECT COUNT(*) FROM acceptances WHERE mission_id=?", mission_id) == 0
            assert count(store, "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=?", mission_id) == 0
            rt.validity.forget(mission_id, str(record.record_id))
            # No MISSION_FINAL certificate: no direct final commit, the Mission stays ACTIVE.
            dispatch, network, occurrence = _readers(rt)
            assert not dispatch.root_review_ready(mission_id) and not dispatch.terminal(mission_id)
            assert store.get_mission(mission_id).status.value == "ACTIVE"
            # The real path still works afterwards, exactly once.
            completed = rt.accept_now()
            assert completed.accepted_result_id == rt.stored.envelope.id
            assert count(store, "SELECT COUNT(*) FROM acceptances WHERE mission_id=?", mission_id) == 1
            assert count(store, "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=? AND purpose='ACCEPT'",
                         mission_id) == 1

    asyncio.run(body())
