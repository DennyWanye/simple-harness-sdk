from __future__ import annotations

from types import SimpleNamespace

from h1h_stage_runner import CASE_IDS, IMPLEMENTED_NODEIDS, gate_exit_code, manifest, run


def test_v14_h1h_manifest_registers_all_36_cases() -> None:
    assert len(CASE_IDS) == 36
    assert set(CASE_IDS) == {
        *(f"A{i:02d}" for i in range(1, 9)),
        *(f"O{i:02d}" for i in range(1, 11)),
        *(f"P{i:02d}" for i in range(1, 11)),
        *(f"I{i:02d}" for i in range(1, 9)),
    }


def test_all_cases_are_explicitly_registered_before_execution() -> None:
    rows = {item["case_id"]: item for item in manifest()}
    assert {item["status"] for item in rows.values()} == {"NOT_RUN"}
    assert set(rows) == set(IMPLEMENTED_NODEIDS)
    assert all(
        (rows[case_id]["nodeid"] is None) == (rows[case_id]["status"] == "NOT_COVERED")
        for case_id in rows
    )


def test_corrected_matrix_keeps_partial_and_uncovered_cases_honest() -> None:
    rows = {item["case_id"]: item for item in manifest()}
    assert sum(row["coverage"] == "MATCH" for row in rows.values()) == 36
    assert sum(row["coverage"] == "PARTIAL" for row in rows.values()) == 0
    assert sum(row["coverage"] == "MISMATCH" for row in rows.values()) == 0
    assert rows["A02"]["coverage"] == "MATCH"
    assert rows["A07"]["nodeid"].endswith("test_issue_command_replay_and_changed_input_conflict")
    assert rows["O01"]["nodeid"].endswith(
        "test_o01_store_complete_empty_has_digest_and_read_error_is_not_empty"
    )
    assert rows["O04"]["coverage"] == "MATCH"
    assert "cross-tenant Mission corruption" in rows["O04"]["coverage_note"]
    assert rows["I03"]["nodeid"].endswith(
        "test_i03_process_exit_between_grant_and_side_binding_recovers_neither"
    )
    assert rows["A03"]["coverage"] == "MATCH"
    assert rows["A08"]["coverage"] == "MATCH"
    assert "other-principal" in rows["A08"]["coverage_note"]
    assert rows["O08"]["coverage"] == "MATCH"
    assert rows["O09"]["nodeid"].endswith("test_o09_real_t0_materialization_rolls_back_link_fault_and_replays_once")
    assert rows["O09"]["status"] == "NOT_RUN"


def test_all_matched_cases_require_their_mapped_tests_to_pass(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "h1h_stage_runner.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0)
    )
    rows = manifest()
    run(rows, tmp_path)
    assert all(row["test_status"] == ("PASS" if row["nodeid"] else "NOT_RUN") for row in rows)
    assert sum(row["status"] == "PASS" for row in rows) == 36
    assert next(row for row in rows if row["case_id"] == "I03")["status"] == "PASS"
    assert next(row for row in rows if row["case_id"] == "O09")["status"] == "PASS"
    assert gate_exit_code(rows) == 0


def test_failed_test_makes_the_gate_fail_even_for_a_matched_requirement(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "h1h_stage_runner.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=1)
    )
    rows = manifest()
    run(rows, tmp_path)
    assert gate_exit_code(rows) == 1
