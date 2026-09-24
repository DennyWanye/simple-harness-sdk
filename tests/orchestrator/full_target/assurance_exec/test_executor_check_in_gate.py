# SPDX-License-Identifier: Apache-2.0
"""A criterion requiring code_test can pass on the Assurance lane.

Host native run arp.13 (2026-09-24): the reviewer accepted a document task and every
check ran and passed, yet both reviews ended INCONCLUSIVE — the executor check
(code_test) was skipped wherever a check result is *used* ("no importer yet"), so the
gate lacked it and a missing result counts as UNKNOWN.  Now an executor receipt is used
from its recorded result like a local one (never re-run).

User decision (same day): a document task's code_test that has nothing to attest (no
pytest target, nothing collected) is not counted — it is already graded PASS
"nothing to attest" (executor_checks.py), which leaves an all-PASS group unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

from agent_orchestrator.assurance.checks import CheckResult, CriterionPolicy, Grade, evaluate_check_gate
from agent_orchestrator.assurance.refs import AssuranceRef, Pin

FORMAT, RULE, CODE = (AssuranceRef("check_spec", Pin(f"event-{n}", 0, n[0] * 64)) for n in ("aformat", "brule", "ccode"))
POLICY = CriterionPolicy("c-notes-written", "CHECKED", ((FORMAT, RULE, CODE),))


def _result(spec, grade, kind="local_check_receipt"):  # type: ignore[no-untyped-def]
    return CheckResult(spec, AssuranceRef(kind, Pin("r-" + spec.pin.id, 0, "d" * 64)), "SUCCEEDED", grade, True)


def test_all_checks_including_a_vacuous_code_test_pass() -> None:
    results = {FORMAT: _result(FORMAT, Grade.PASS), RULE: _result(RULE, Grade.PASS),
               CODE: _result(CODE, Grade.PASS, "execution_receipt")}
    assert evaluate_check_gate(POLICY, results).grade is Grade.PASS


def test_without_the_executor_result_the_gate_is_incomplete() -> None:
    results = {FORMAT: _result(FORMAT, Grade.PASS), RULE: _result(RULE, Grade.PASS)}
    gate = evaluate_check_gate(POLICY, results)
    assert gate.grade is Grade.UNKNOWN and gate.reason == "CHECK_EVIDENCE_INCOMPLETE"


def test_a_failing_code_test_fails_the_criterion() -> None:
    results = {FORMAT: _result(FORMAT, Grade.PASS), RULE: _result(RULE, Grade.PASS),
               CODE: _result(CODE, Grade.FAIL, "execution_receipt")}
    assert evaluate_check_gate(POLICY, results).grade is Grade.FAIL


def test_no_use_site_skips_executor_receipts() -> None:
    """Every place that filters check receipts by kind must accept executor receipts."""

    import agent_orchestrator.orchestrator as package

    root = Path(package.__file__).parent
    offenders = []
    for name in ("assurance_review_consumer.py", "assurance_validity.py", "assurance_check_use.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                values = [c.value for c in node.comparators if isinstance(c, ast.Constant)]
                if "local_check_receipt" in values:
                    offenders.append(f"{name}:{node.lineno}")
    assert offenders == []


def test_the_expected_adapter_follows_the_receipt_kind() -> None:
    from agent_orchestrator.orchestrator.assurance_check_import import _SOURCES

    assert _SOURCES["local_check_receipt"][3] == "assurance-local-check-adapter"
    assert _SOURCES["execution_receipt"][3] == "assurance-executor-check-adapter"
