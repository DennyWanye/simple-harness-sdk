# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""G audit O1/O2: frozen AC04/10/19/30/42, tests only against the candidate.

O1 schedules two real, already dispatched results in both serial commit orders.
Resolution uses each immutable intent; acceptance additionally checks currentness.
It must not turn an old frozen PASS into a fabricated receipt or current acceptance.
O1 freezes the published DOC4 direct-accept contract; it does not prove DOC5 Critic.

O2 pins actual pre-P33 source (a4aae8c23a2b72b9f2b07c62986fc7dd39f36cdf),
not a handwritten reimplementation. Structural tests deliberately fence the
security-relevant inputs/upgrade branch; ordinary grading behavior is already in
test_p33_document_grading and step04. No Git executable/history is needed at test
runtime: the small original fixture is byte-pinned below, including its license.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import sys
from pathlib import Path
from types import ModuleType

import pytest
from test_p33_source_commits import (
    NEW_TEXT,
    PATH,
    TEXT,
    accept,
    binding,
    change_source,
    produce,
    replay,
    submit,
)
from test_p33_source_commits import e_scenes as source_scenes

from agent_orchestrator.contracts import ClaimStatus, SourceCitation, TaskStatus, ids
from agent_orchestrator.contracts.models import canonical_json
from agent_orchestrator.governance.domains import CODE_PROFILE
from agent_orchestrator.memory import claims
from agent_orchestrator.orchestrator.commit_service import CommitRejected
from agent_orchestrator.verification import adapters, assessments

e_scenes = source_scenes


BASELINE_FILE = Path(__file__).with_name("fixtures") / "a4aae8c_claims.py.txt"
BASELINE_SHA256 = "242a0e4b8ddcde32c1ac78521cb5443a3d73547f71564efccab853cb37abc4f1"


@pytest.mark.parametrize("second_cites_old_version", [False, True])
def test_o1_two_dispatched_attempts_keep_frozen_verdicts_in_both_accept_orders(
    e_scenes, second_cites_old_version
):
    outcomes = []
    for order in (("old", "new"), ("new", "old")):
        scene = e_scenes(paths=(PATH, PATH))
        old = submit(scene, index=0)
        old_version = old.envelope.claims[0].citations[0].version
        change_source(scene)  # real Host source_change request and ApprovalApi.decide
        new_version = scene.store.get_source(scene.mission.id, PATH)["version_hash"]
        assert old_version != new_version
        newer = submit(
            scene,
            index=1,
            citations=(SourceCitation(PATH, old_version, 1, 1, TEXT.strip()),)
            if second_cites_old_version
            else None,
        )
        attempts = {"old": old, "new": newer}
        assert old.attempt.id != newer.attempt.id and old.task.id != newer.task.id
        assert old.envelope.claims[0].content == TEXT.strip()
        assert newer.envelope.claims[0].content == (
            TEXT.strip() if second_cites_old_version else NEW_TEXT.strip()
        )
        assert dict(binding(old).source_versions) == {PATH: old_version}
        assert dict(binding(newer).source_versions) == {PATH: new_version}
        intents = {
            name: canonical_json(scene.store.get_intent_for_subject(e.attempt.id).config)
            for name, e in attempts.items()
        }
        # Both intents/results exist before processing. The second real producer
        # runs AFTER the other result commits, so this exercises order dependence
        # rather than merely comparing two receipts calculated beforehand.
        layers, recorded = {}, {}
        for name in order:
            e = attempts[name]
            layers[name] = produce(e)
            recorded[name] = scene.store.list_verifications(e.envelope.id)
            assert layers[name].status == (
                "FAIL" if name == "new" and second_cites_old_version else "PASS"
            )
            assert [
                r["resolution"]["status"] for r in layers[name].detail["evidence_resolutions"]
            ] == ["stale_source" if name == "new" and second_cites_old_version else "resolved"]
            if name == "new" and second_cites_old_version:
                # A direct accept call cannot bypass an authentic frozen FAIL.
                with pytest.raises(CommitRejected):
                    accept(e)
                assert scene.store.get_task(e.task.id).accepted_result_id is None
                scene.commit.fail_result(e.envelope.id, failures=(layers[name].to_json(),))
            else:
                result_task = accept(e)
                assert (result_task.status is TaskStatus.COMPLETED) == (name == "new")
            for other, candidate in attempts.items():
                assert (
                    canonical_json(scene.store.get_intent_for_subject(candidate.attempt.id).config)
                    == intents[other]
                )
                assert scene.store.list_verifications(candidate.envelope.id) == recorded.get(
                    other, []
                )

        actual = {}
        for name, e in attempts.items():
            task = scene.store.get_task(e.task.id)
            claim = scene.store.get_claim(ids.claim_id(e.envelope.id, 1))
            should_accept = name == "new" and not second_cites_old_version
            assert bool(task.accepted_result_id) is should_accept
            assert claim.status is (
                ClaimStatus.VERIFIED if should_accept else ClaimStatus.UNDER_REVIEW
            )
            failure = scene.store.get_attempt(e.attempt.id).failure
            if not should_accept:
                assert "stale_source" in canonical_json(failure)
            actual[name] = {
                # Acceptance has projected system attribution content/revision.
                # Compare the unchanged dispatch snapshot and recorded receipt,
                # not a new pre-accept binding against the now-formal Claim.
                "frozen_versions": dict(
                    scene.store.get_intent_for_subject(e.attempt.id).config["source_versions"]
                ),
                "rule_status": layers[name].status,
                "resolutions": [
                    {k: v for k, v in row["resolution"].items() if k != "mission_id"}
                    for row in layers[name].detail["evidence_resolutions"]
                ],
                "accepted": bool(task.accepted_result_id),
                "task_status": str(task.status),
                "claim_status": str(claim.status),
                "result_verdict": scene.store.get_result(e.envelope.id).verdict,
            }
        assert len(scene.store.list_knowledge(scene.mission.id)) == (
            0 if second_cites_old_version else 1
        )
        replay(scene)
        outcomes.append(actual)
    assert outcomes[0] == outcomes[1]


def _nodes(source):
    return {
        node.name: node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }


def _baseline():
    raw = BASELINE_FILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA256
    return raw.decode("utf-8")


def test_o2_code_grader_and_dependencies_are_original_a4aae8c_bytes():
    before, current = _baseline(), inspect.getsource(claims)
    historical, present = _nodes(before), _nodes(current)
    for name in (
        "EvidenceRef",
        "_normalise",
        "parse_evidence",
        "_is_untrusted",
        "ran_test_targets",
        "covering_target",
        "ClaimGrade",
        "grade_claim",
    ):
        current_name = "_legacy_grade_claim" if name == "grade_claim" else name
        actual = ast.get_source_segment(current, present[current_name])
        if name == "grade_claim":
            actual = actual.replace("def _legacy_grade_claim(", "def grade_claim(", 1)
        assert actual == ast.get_source_segment(before, historical[name]), name


def test_o2_public_code_dispatch_matches_executable_historical_function(monkeypatch):
    # Execute the checked-in historical module, with current contract/path imports;
    # this is a grading boundary control, not a claim that the entire old SDK ran.
    module = ModuleType("agent_orchestrator.memory._p33_historical_claims")
    module.__package__ = "agent_orchestrator.memory"
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(_baseline(), str(BASELINE_FILE), "exec"), module.__dict__)

    def no_document_branch(*args, **kwargs):
        raise AssertionError("code dispatch entered document grading")

    monkeypatch.setattr(claims, "_grade_document", no_document_branch)
    # Include duplicate pass/fail targets, whole-tree and directory coverage,
    # untrusted prefixes and unresolved references beyond the prior three controls.
    for runs in (
        [],
        [{"target": "", "passed": True}],
        [{"target": "tests", "passed": True}],
        [{"target": "tests/a.py", "passed": True}, {"target": "tests/a.py", "passed": False}],
    ):
        for evidence in (
            ("pytest:tests/a.py",),
            ("file:report.md",),
            ("file:sources/a.md", "unknown"),
            ("tool-run:r", "knowledge:k"),
            (),
        ):
            kwargs = dict(
                verifier_results=[{"layer": "code_test", "detail": {"runs": runs}}],
                artifact_paths=["report.md", "sources/a.md"],
                untrusted_prefixes=["sources/"],
            )
            expected = canonical_json(module.grade_claim("claim", evidence, **kwargs).to_json())
            for domain in (None, CODE_PROFILE):
                assert (
                    canonical_json(
                        claims.grade_claim("claim", evidence, domain=domain, **kwargs).to_json()
                    )
                    == expected
                )


def test_o2_all_verifier_row_readers_exclude_critic_before_accessing_payload():
    source = inspect.getsource(claims)
    functions = _nodes(source)
    readers = {}
    for name, function in functions.items():
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.For) and ast.unparse(node.iter) == "verifier_results":
                readers[name] = node
    assert set(readers) == {"ran_test_targets", "_grade_document"}
    for name, allowed in (("ran_test_targets", "code_test"), ("_grade_document", "rule_check")):
        loop = readers[name]
        guard = loop.body[0]
        assert isinstance(guard, ast.If) and not guard.orelse
        expected = ast.parse(f'if layer.get("layer") != "{allowed}":\n    continue').body[0]
        assert ast.dump(guard) == ast.dump(expected)
    # Every verifier_results use is either those guarded iterations or an explicit
    # forwarding edge. Adding indexing, aliases or another consumer requires review.
    edges = []
    for name, function in functions.items():
        if not isinstance(function, ast.FunctionDef):
            continue
        parents = {
            child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Name)
                and node.id == "verifier_results"
                and isinstance(node.ctx, ast.Load)
            ):
                parent = parents[node]
                if isinstance(parent, ast.For) and parent.iter is node:
                    continue
                call = parents[parent] if isinstance(parent, ast.keyword) else parent
                assert isinstance(call, ast.Call)
                edges.append((name, ast.unparse(call.func)))
    assert sorted(edges) == sorted(
        [
            ("grade_claim", "_grade_document"),
            ("grade_claim", "_legacy_grade_claim"),
            ("_legacy_grade_claim", "ran_test_targets"),
        ]
    )


@pytest.mark.parametrize(
    "name,parameters",
    [
        ("_evaluation", ("binding", "resolutions", "structural")),
        ("_v2_parts", ("binding", "resolutions", "structural")),
        ("_evaluation_v2", ("binding", "resolutions", "structural", "external")),
    ],
)
def test_o2_pure_evaluator_signature_has_no_source_reader_or_raw_source(name, parameters):
    signature = inspect.signature(getattr(assessments, name))
    assert tuple(signature.parameters) == parameters
    assert all(
        p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD and p.default is inspect.Parameter.empty
        for p in signature.parameters.values()
    )
    assert {p.name: p.annotation for p in signature.parameters.values()} == {
        key: value
        for key, value in {
            "binding": "AssessmentBindingV1",
            "resolutions": "Sequence[Mapping[str, Any]]",
            "structural": "LayerResult",
            "external": "Mapping[str, Any]",
        }.items()
        if key in parameters
    }
    assert adapters.COVERAGE_VERDICTS == frozenset({"FAIL", "INCONCLUSIVE", "NEEDS_HUMAN"})


def test_o2_document_verified_has_one_literal_guarded_return():
    function = _nodes(inspect.getsource(claims))["_grade_document"]
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    for node in returns:
        assert isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "ClaimGrade"
        assert ast.unparse(node.value.args[1]) in {
            "ClaimStatus.UNDER_REVIEW",
            "ClaimStatus.SUPPORTED",
            "ClaimStatus.VERIFIED",
        }
    upgraded = [
        node for node in returns if ast.unparse(node.value.args[1]) == "ClaimStatus.VERIFIED"
    ]
    assert upgraded == [function.body[-1]]
    assignments = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and node.id == "matching" and isinstance(node.ctx, ast.Store)
    ]
    assert len(assignments) == 1
    matching = next(
        node
        for node in function.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "matching" for t in node.targets)
    )
    expected = ast.parse(
        "[item for item in resolved if normalise_quote(proposal.content) == "
        'normalise_quote(str(item["ref"]["quote"]))]',
        mode="eval",
    ).body
    assert ast.dump(matching.value) == ast.dump(expected)
    guard = function.body[function.body.index(matching) + 1]
    assert isinstance(guard, ast.If) and ast.unparse(guard.test) == "not matching"
    assert not guard.orelse
    assert ast.unparse(guard.body[-1].value.args[1]) == "ClaimStatus.SUPPORTED"
    assert function.body.index(guard) < function.body.index(upgraded[0])
