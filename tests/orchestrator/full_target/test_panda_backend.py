# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""P2.2 red tests for the PANDA / HDDL backend adapter.

Two layers are covered without installing anything:

* the default machine, where no PANDA binary is configured, must report
  ``SOLVER_UNAVAILABLE`` instead of raising or quietly passing;
* executable stub scripts under ``fixtures/panda`` reproduce the exact stdout
  of pandaPIparser and pandaPIengine for each documented outcome, so the
  adapter's classification, timeout and crash handling are exercised for real
  subprocesses.

One optional test runs against a genuine pandaPIparser when the environment
provides it.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_orchestrator.planning.htn.backends.panda import (
    ENV_ENGINE,
    ENV_GROUNDER,
    ENV_PARSER,
    Availability,
    PandaPlanFormatError,
    PandaToolchain,
    SolveStatus,
    VerificationStatus,
    check_fragment,
    parse_plan,
)

FIXTURES = Path(__file__).parent / "fixtures" / "panda"

DOMAIN_STRIPS = (FIXTURES / "domain_strips_po.hddl").read_text(encoding="utf-8")
PROBLEM_STRIPS = (FIXTURES / "problem_strips_po.hddl").read_text(encoding="utf-8")
PLAN_REUSE = (FIXTURES / "plan_reuse_occurrences.plan").read_text(encoding="utf-8")
PLAN_MALFORMED = (FIXTURES / "plan_malformed.plan").read_text(encoding="utf-8")
PLAN_BAD_ROOT = (FIXTURES / "plan_bad_root.plan").read_text(encoding="utf-8")
PLAN_NO_ROOT = (FIXTURES / "plan_no_root.plan").read_text(encoding="utf-8")
DOMAIN_NUMERIC = (FIXTURES / "domain_numeric.hddl").read_text(encoding="utf-8")
DOMAIN_DURATIVE = (FIXTURES / "domain_durative.hddl").read_text(encoding="utf-8")
DOMAIN_CONDITIONAL = (FIXTURES / "domain_conditional.hddl").read_text(encoding="utf-8")


def stub(name: str) -> str:
    """Absolute path of one stub executable."""
    path = FIXTURES / name
    assert path.is_file(), f"missing stub fixture: {path}"
    assert os.access(path, os.X_OK), f"stub fixture is not executable: {path}"
    return str(path)


def toolchain(
    *,
    parser: str | None = None,
    grounder: str | None = None,
    engine: str | None = None,
) -> PandaToolchain:
    """Build a toolchain through the documented environment variables only."""
    env: dict[str, str] = {}
    if parser is not None:
        env[ENV_PARSER] = parser
    if grounder is not None:
        env[ENV_GROUNDER] = grounder
    if engine is not None:
        env[ENV_ENGINE] = engine
    return PandaToolchain.from_env(env)


def full_stub_chain(*, engine: str, parser: str = "stub_parser_true.py") -> PandaToolchain:
    return toolchain(
        parser=stub(parser),
        grounder=stub("stub_grounder.py"),
        engine=stub(engine),
    )


# --- binaries absent ---------------------------------------------------------


def test_availability_reports_every_missing_tool() -> None:
    report = toolchain().availability()
    assert report.available is False
    assert set(report.missing) == {"parser", "grounder", "engine"}
    assert report.versions == {}
    assert report.digests == {}


def test_verify_plan_without_binaries_is_solver_unavailable() -> None:
    result = toolchain().verify_plan(DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=5.0)
    assert result.status is VerificationStatus.SOLVER_UNAVAILABLE
    assert result.status is not VerificationStatus.VERIFIED
    assert result.witness is None
    assert result.traces == ()
    assert "parser" in result.detail


def test_solve_without_binaries_is_solver_unavailable() -> None:
    result = toolchain().solve(DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=5.0)
    assert result.status is SolveStatus.SOLVER_UNAVAILABLE
    assert result.plan_text is None
    assert result.witness is None
    assert result.toolchain.missing == ("parser", "grounder", "engine")


def test_env_var_pointing_at_a_non_executable_file_counts_as_missing() -> None:
    report = toolchain(parser=str(FIXTURES / "stub_not_executable.txt")).availability(
        required=("parser",)
    )
    assert report.available is False
    assert report.missing == ("parser",)


def test_bare_name_resolves_through_path_and_unknown_name_does_not() -> None:
    assert toolchain(parser="python3").availability(required=("parser",)).available is True
    unknown = toolchain(parser="sh-panda-no-such-binary").availability(required=("parser",))
    assert unknown.available is False


# --- toolchain identity ------------------------------------------------------


def test_availability_records_binary_digest_and_version() -> None:
    parser = stub("stub_parser_true.py")
    report = toolchain(parser=parser).availability(required=("parser",))
    assert report.available is True
    assert report.missing == ()
    expected = hashlib.sha256(Path(parser).read_bytes()).hexdigest()
    assert report.digests["parser"] == expected
    assert report.versions["parser"] == "pandaPIparser stub 0.0-test"
    assert report.paths["parser"] == parser


def test_availability_can_skip_the_version_probe() -> None:
    report = toolchain(parser=stub("stub_parser_true.py")).availability(
        required=("parser",), probe_versions=False
    )
    assert report.versions["parser"] == "unknown"
    assert len(report.digests["parser"]) == 64


# --- verification ------------------------------------------------------------


def test_verify_plan_accepts_the_true_verdict_and_returns_a_witness() -> None:
    result = toolchain(parser=stub("stub_parser_true.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    assert result.status is VerificationStatus.VERIFIED
    assert result.witness is not None
    assert len(result.traces) == 1
    assert result.elapsed_s >= 0.0
    assert len(result.stdout_sha256) == 64
    assert len(result.stderr_sha256) == 64
    assert isinstance(result.toolchain, Availability)


def test_verify_plan_passes_domain_problem_plan_in_that_order() -> None:
    result = toolchain(parser=stub("stub_parser_true.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    argv = result.traces[0].argv
    assert argv[1:3] == ("--verify", "-C")
    assert [Path(item).name for item in argv[3:]] == ["domain.hddl", "problem.hddl", "plan.txt"]


def test_verify_plan_inputs_live_in_a_temporary_directory_that_is_removed() -> None:
    result = toolchain(parser=stub("stub_parser_true.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    written = Path(result.traces[0].argv[3])
    assert written.name == "domain.hddl"
    assert not written.exists()
    assert Path(__file__).parent not in written.parents


def test_verify_plan_survives_ansi_coloured_output() -> None:
    result = toolchain(parser=stub("stub_parser_coloured_true.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    assert result.status is VerificationStatus.VERIFIED


def test_verify_plan_rejects_the_false_verdict() -> None:
    result = toolchain(parser=stub("stub_parser_false.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    assert result.status is VerificationStatus.REJECTED
    assert result.witness is None
    assert result.traces[0].exit_code == 1


def test_verify_plan_timeout_is_not_a_rejection() -> None:
    result = toolchain(parser=stub("stub_hang.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=1.0
    )
    assert result.status is VerificationStatus.TIMEOUT
    assert result.traces[0].timed_out is True
    assert result.traces[0].exit_code is None
    assert result.witness is None


def test_verify_plan_crash_is_tool_error_carrying_the_exit_code() -> None:
    result = toolchain(parser=stub("stub_crash.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    assert result.status is VerificationStatus.TOOL_ERROR
    assert "3" in result.detail
    assert "bad_alloc" in result.traces[0].stderr_excerpt


def test_verify_plan_lets_the_rejection_win_when_both_verdicts_are_printed() -> None:
    result = toolchain(parser=stub("stub_parser_true_and_false.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    assert result.status is VerificationStatus.REJECTED
    assert result.witness is None


def test_verify_plan_unrecognized_output_is_tool_error_not_a_verdict() -> None:
    result = toolchain(parser=stub("stub_parser_garbage.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    assert result.status is VerificationStatus.TOOL_ERROR


def test_verify_plan_refuses_an_unsupported_fragment_before_running_anything() -> None:
    result = toolchain(parser=stub("stub_parser_true.py")).verify_plan(
        DOMAIN_NUMERIC, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    assert result.status is VerificationStatus.UNSUPPORTED_FEATURE
    assert "numeric-fluents" in result.unsupported_features
    assert result.traces == ()


# --- solving -----------------------------------------------------------------


def test_solve_reports_solved_with_a_back_translated_plan_and_witness() -> None:
    result = full_stub_chain(engine="stub_engine_solved.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0
    )
    assert result.status is SolveStatus.SOLVED
    assert result.plan_text is not None
    assert result.witness is not None
    assert [trace.tool for trace in result.traces] == [
        "parser",
        "grounder",
        "engine",
        "parser",
    ]
    assert result.traces[-1].argv[1:3] == ("-c", "-C")


def test_solve_reports_proven_unsolvable_only_when_the_engine_proves_it() -> None:
    result = full_stub_chain(engine="stub_engine_unsolvable.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0
    )
    assert result.status is SolveStatus.UNSOLVABLE_PROVEN
    assert result.plan_text is None


def test_solve_reports_search_limit_when_the_engine_budget_runs_out() -> None:
    result = full_stub_chain(engine="stub_engine_limit.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0
    )
    assert result.status is SolveStatus.SEARCH_LIMIT_REACHED
    assert result.status is not SolveStatus.UNSOLVABLE_PROVEN


def test_solve_never_turns_an_exhausted_budget_into_a_proof() -> None:
    result = full_stub_chain(engine="stub_engine_limit_and_unsolvable.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0
    )
    assert result.status is SolveStatus.SEARCH_LIMIT_REACHED


def test_solve_forwards_the_search_limit_as_the_engine_time_budget() -> None:
    result = full_stub_chain(engine="stub_engine_limit.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0, search_limit=7
    )
    engine_argv = result.traces[2].argv
    assert "--timelimit=7" in engine_argv


def test_solve_rejects_a_non_positive_search_limit_without_raising() -> None:
    result = full_stub_chain(engine="stub_engine_limit.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0, search_limit=0
    )
    assert result.status is SolveStatus.TOOL_ERROR
    assert "search_limit" in result.detail
    assert result.traces == ()


def test_solve_engine_timeout_is_timeout_not_unsolvable() -> None:
    result = full_stub_chain(engine="stub_hang.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=1.0
    )
    assert result.status is SolveStatus.TIMEOUT
    assert result.traces[-1].timed_out is True


def test_solve_grounder_crash_is_tool_error() -> None:
    chain = toolchain(
        parser=stub("stub_parser_true.py"),
        grounder=stub("stub_crash.py"),
        engine=stub("stub_engine_solved.py"),
    )
    result = chain.solve(DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0)
    assert result.status is SolveStatus.TOOL_ERROR
    assert "grounder" in result.detail
    assert len(result.traces) == 2


def test_solve_refuses_to_classify_a_back_end_without_status_lines() -> None:
    result = full_stub_chain(engine="stub_engine_sat_style.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0
    )
    assert result.status is SolveStatus.TOOL_ERROR
    assert "progression" in result.detail


def test_solve_fails_when_back_translation_fails() -> None:
    chain = full_stub_chain(engine="stub_engine_solved.py", parser="stub_parser_garbage.py")
    result = chain.solve(DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0)
    assert result.status is SolveStatus.TOOL_ERROR
    assert "back-translation" in result.detail


def test_solve_refuses_an_unsupported_fragment_before_running_anything() -> None:
    result = full_stub_chain(engine="stub_engine_solved.py").solve(
        DOMAIN_DURATIVE, PROBLEM_STRIPS, timeout_s=30.0
    )
    assert result.status is SolveStatus.UNSUPPORTED_FEATURE
    assert "durative-actions" in result.unsupported_features
    assert result.traces == ()


def test_captured_streams_are_bounded_but_hashed_in_full() -> None:
    result = full_stub_chain(engine="stub_engine_loud_solved.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0
    )
    engine_trace = result.traces[2]
    assert len(engine_trace.stdout_excerpt.encode("utf-8")) == 4096
    excerpt_digest = hashlib.sha256(engine_trace.stdout_excerpt.encode("utf-8")).hexdigest()
    assert engine_trace.stdout_sha256 != excerpt_digest
    assert result.status is SolveStatus.SOLVED


def test_solve_classifies_and_extracts_from_the_whole_stream_not_the_excerpt() -> None:
    result = full_stub_chain(engine="stub_engine_big_plan.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=60.0
    )
    assert result.status is SolveStatus.SOLVED
    engine_trace = result.traces[2]
    # The status line and the whole plan sit far past the excerpt bound.
    assert len(engine_trace.stdout_excerpt.encode("utf-8")) == 4096
    assert "- Status: Solved" not in engine_trace.stdout_excerpt
    witness = result.witness
    assert witness is not None
    assert len(witness.primitives) == 400
    assert witness.roots == ("400",)
    assert witness.primitives[-1].name == "act399"
    assert witness.primitives[-1].arguments == ("crate", "depot")


def test_solve_reports_the_last_stage_streams() -> None:
    result = full_stub_chain(engine="stub_engine_solved.py").solve(
        DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=30.0
    )
    assert result.status is SolveStatus.SOLVED
    converter_stdout = b"plan back-translation done\n"
    assert result.stdout_sha256 == hashlib.sha256(converter_stdout).hexdigest()
    assert result.stdout_sha256 == result.traces[-1].stdout_sha256
    assert result.stdout_sha256 != result.traces[2].stdout_sha256
    assert result.stdout_sha256 != result.traces[1].stdout_sha256


def test_every_stage_shares_one_budget_instead_of_getting_its_own() -> None:
    chain = toolchain(
        parser=stub("stub_sleepy_parser.py"),
        grounder=stub("stub_sleepy_grounder.py"),
        engine=stub("stub_engine_solved.py"),
    )
    # The grounder wants 3.5s and would fit inside a fresh 4.5s budget; it only
    # fails because the parser already spent 2s of the one shared budget.
    result = chain.solve(DOMAIN_STRIPS, PROBLEM_STRIPS, timeout_s=4.5)
    assert result.status is SolveStatus.TIMEOUT
    assert result.traces[-1].tool == "grounder"
    assert result.traces[-1].timed_out is True
    assert result.traces[0].timed_out is False
    assert result.elapsed_s < 9.0


def test_a_timeout_reaps_the_whole_process_group() -> None:
    marker = Path("/tmp/sh-panda-grandchild.marker")  # noqa: S108 - see the stub
    marker.unlink(missing_ok=True)
    try:
        result = toolchain(parser=stub("stub_spawns_grandchild.py")).verify_plan(
            DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=1.0
        )
        assert result.status is VerificationStatus.TIMEOUT
        # The grandchild would write its marker 3s in if it outlived the kill.
        time.sleep(4.0)
        assert not marker.exists(), "a grandchild survived the timeout"
    finally:
        marker.unlink(missing_ok=True)


def test_a_file_that_cannot_be_executed_is_a_tool_error_not_an_exception() -> None:
    result = toolchain(parser=str(FIXTURES / "stub_bad_format.bin")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    assert result.status is VerificationStatus.TOOL_ERROR
    assert "could not launch" in result.detail
    assert result.traces[0].launch_error is not None


def test_relative_binary_paths_are_absolutised_before_the_cwd_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(FIXTURES)
    result = toolchain(parser=os.path.join(".", "stub_parser_true.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=30.0
    )
    assert result.status is VerificationStatus.VERIFIED
    assert Path(result.traces[0].argv[0]).is_absolute()


# --- fragment detection ------------------------------------------------------


def test_pure_strips_partial_order_domain_is_supported() -> None:
    report = check_fragment(DOMAIN_STRIPS, PROBLEM_STRIPS)
    assert report.supported is True
    assert report.unsupported_features == ()
    assert ":hierarchy" in report.declared_requirements
    assert ":method-preconditions" in report.declared_requirements


def test_subtask_ordering_operator_is_not_mistaken_for_a_numeric_comparison() -> None:
    assert "(< t1 t2)" in DOMAIN_STRIPS
    assert "(< st1 st2)" in PROBLEM_STRIPS
    assert check_fragment(DOMAIN_STRIPS, PROBLEM_STRIPS).supported is True


def test_numeric_fluents_are_refused_by_name() -> None:
    report = check_fragment(DOMAIN_NUMERIC, PROBLEM_STRIPS)
    assert report.supported is False
    assert report.unsupported_features == ("numeric-fluents",)
    assert any("(:functions" in note for note in report.detection_notes)


def test_durative_actions_are_refused_by_name() -> None:
    report = check_fragment(DOMAIN_DURATIVE, PROBLEM_STRIPS)
    assert report.supported is False
    assert report.unsupported_features == ("durative-actions",)


def test_conditional_and_universal_features_are_listed_individually() -> None:
    report = check_fragment(DOMAIN_CONDITIONAL, PROBLEM_STRIPS)
    assert report.supported is False
    assert set(report.unsupported_features) == {
        "conditional-effects",
        "universal-preconditions",
        "universal-quantification",
    }


def test_an_undeclared_conditional_effect_is_still_caught() -> None:
    domain = DOMAIN_STRIPS.replace(
        ":effect (visited ?l))",
        ":effect (when (visited ?l) (visited ?l)))",
    )
    report = check_fragment(domain, PROBLEM_STRIPS)
    assert "conditional-effects" in report.unsupported_features


@pytest.mark.parametrize(
    ("snippet", "feature"),
    [
        ("(when(at ?p ?l) (sealed ?p))", "conditional-effects"),
        ("(forall(?p - package) (sealed ?p))", "universal-quantification"),
        ("(exists(?p - package) (sealed ?p))", "existential-quantification"),
    ],
)
def test_probes_catch_the_no_space_spelling(snippet: str, feature: str) -> None:
    domain = DOMAIN_STRIPS.replace(":effect (visited ?l))", f":effect {snippet})")
    report = check_fragment(domain, PROBLEM_STRIPS)
    assert report.supported is False
    assert feature in report.unsupported_features


def test_commented_out_features_do_not_trip_the_detector() -> None:
    domain = DOMAIN_STRIPS + "\n; (:functions (fuel) - number)\n"
    assert check_fragment(domain, PROBLEM_STRIPS).supported is True


# --- decomposition witness ---------------------------------------------------


def test_witness_keeps_every_occurrence_of_a_repeated_action() -> None:
    witness = parse_plan(PLAN_REUSE)
    assert len(witness.primitives) == 4
    repeated = witness.occurrences_of("load crate depot")
    assert len(repeated) == 2
    assert [item.plan_id for item in repeated] == ["0", "2"]
    assert [item.position for item in repeated] == [0, 2]


def test_witness_maps_each_occurrence_to_its_own_method_application() -> None:
    witness = parse_plan(PLAN_REUSE)
    mapping = {
        occurrence.plan_id: None if step is None else step.plan_id
        for occurrence, step in witness.occurrence_parents()
    }
    assert mapping == {"0": "5", "1": "5", "2": "6", "3": "6"}
    first, second = witness.occurrences_of("load crate depot")
    assert witness.parent_of(first.plan_id) != witness.parent_of(second.plan_id)


def test_witness_records_roots_methods_and_the_plan_hash() -> None:
    witness = parse_plan(PLAN_REUSE)
    assert witness.roots == ("4",)
    top = next(step for step in witness.decompositions if step.plan_id == "4")
    assert top.task_name == "__top"
    assert top.method_name == "__top_method"
    assert top.children == ("5", "6")
    deliver = next(step for step in witness.decompositions if step.plan_id == "5")
    assert deliver.task_signature == "deliver crate depot dock"
    assert witness.plan_sha256 == hashlib.sha256(PLAN_REUSE.encode("utf-8")).hexdigest()


def test_witness_parses_the_ipc_sample_without_a_closing_marker() -> None:
    plan = "==>\n0 pickup a\nroot 1\n1 achieve-goal a -> m-achieve 0\n"
    witness = parse_plan(plan)
    assert len(witness.primitives) == 1
    assert witness.roots == ("1",)


def test_parse_plan_refuses_a_root_that_names_no_task_in_the_plan() -> None:
    # Every decomposition child exists here, so only the root check can fire.
    with pytest.raises(PandaPlanFormatError, match="root"):
        parse_plan(PLAN_BAD_ROOT)


def test_parse_plan_requires_a_root_line_by_default() -> None:
    with pytest.raises(PandaPlanFormatError, match="no root line"):
        parse_plan(PLAN_NO_ROOT)
    relaxed = parse_plan(PLAN_NO_ROOT, require_root=False)
    assert relaxed.roots == ()


def test_verification_is_the_only_mode_that_accepts_a_rootless_plan() -> None:
    result = toolchain(parser=stub("stub_parser_true.py")).verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_NO_ROOT, timeout_s=30.0
    )
    assert result.status is VerificationStatus.VERIFIED
    assert result.witness is not None
    assert result.witness.roots == ()


def test_parse_plan_refuses_a_malformed_plan() -> None:
    with pytest.raises(PandaPlanFormatError):
        parse_plan(PLAN_MALFORMED)


def test_parse_plan_refuses_duplicate_plan_ids() -> None:
    with pytest.raises(PandaPlanFormatError):
        parse_plan("==>\n0 a\n0 b\nroot 0\n<==\n")


def test_parse_plan_refuses_an_empty_body() -> None:
    with pytest.raises(PandaPlanFormatError):
        parse_plan("==>\n<==\n")


# --- optional real binary ----------------------------------------------------


def _real_parser_available() -> bool:
    env: Mapping[str, str] = os.environ
    return PandaToolchain.from_env(env).availability(required=("parser",)).available


@pytest.mark.skipif(
    not _real_parser_available(),
    reason=f"no real pandaPIparser configured via {ENV_PARSER}",
)
def test_real_pandapiparser_verifies_the_modelled_sample() -> None:
    result = PandaToolchain.from_env().verify_plan(
        DOMAIN_STRIPS, PROBLEM_STRIPS, PLAN_REUSE, timeout_s=120.0
    )
    assert result.status is VerificationStatus.VERIFIED, result.detail
    assert result.toolchain.digests["parser"]
    assert result.witness is not None
    assert len(result.witness.occurrences_of("load crate depot")) == 2
