# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c: the minimum predicate observers, and the three answers they may give.

§7.3 brings the observer set forward from P6 into P2: at least five per pilot
domain, reading only.  Four properties are pinned here:

1. **Three answers, never two.**  TRUE, FALSE and "I could not ask" are separate.
   An ``OBSERVER_UNAVAILABLE`` result carries **no** ``ObservationRecord`` at all,
   so an outage cannot be read as a polarity (AER §8.2 dimension 4).
2. **A closed-world denial is admissible or it is not built.**  ``code.working-tree-clean``
   and ``appworld.action-confirmed`` are the two CLOSED predicates of the seed
   library; a FALSE for either carries ``AUTHORITATIVE_WITH_SCOPE`` coverage, a
   scope, a watermark and an ``observer_id`` the signature lists — and
   ``knowledge.predicates.authoritative_negative_matches_observer`` agrees (§6.6 C28).
3. **Observers only read.**  Every command goes through one allowlist that refuses
   a mutating sub-command *before* spawning a process, every command has a timeout,
   and observing a worktree leaves it byte-identical.
4. **The declarations and the readers agree.**  Every observer id this package
   offers is one the seed ``predicates.json`` actually lists for that predicate, and
   every predicate of both domains has a reader.

Deterministic tests inject the command runner / the AppWorld client; two tests use
the real ``git`` on this repository to prove ``run_read_only`` works at all.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_orchestrator.contracts.evidence_state import QueryCompleteness
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.semantic_base import Provenance, TypedRefKind
from agent_orchestrator.knowledge.predicates import (
    PredicateSignature,
    WorldAssumption,
    authoritative_negative_matches_observer,
    proposition_key,
)
from agent_orchestrator.planning.htn.observers import (
    COMPLETE_COVERAGE,
    Observation,
    ObservationOutcome,
    PredicateObserver,
    denial,
    observed,
    unavailable,
)
from agent_orchestrator.planning.htn.observers.appworld import (
    APPWORLD_OBSERVER_COVERAGE,
    INTEGRITY_MARKERS,
    REFUSAL_MARKERS,
    ApiObserver,
    AppWorldObserverConfig,
    AvailabilityObserver,
    CredentialObserver,
    EntityObserver,
    ReadOutcome,
    ReceiptObserver,
    appworld_observers,
    classify_read_error,
)
from agent_orchestrator.planning.htn.observers.code import (
    CODE_OBSERVER_COVERAGE,
    COPY_EXCLUDES,
    OPERAND_SEPARATOR,
    READ_ONLY_GIT,
    READ_ONLY_PYTHON,
    TRUSTED_ARGUMENTS,
    BudgetExhausted,
    ChangesetObserver,
    CodeObserverConfig,
    CommandResult,
    HistoryObserver,
    RepoObserver,
    SuiteObserver,
    WorkspaceObserver,
    code_observers,
    operand,
    read_only_copy,
    run_read_only,
)
from agent_orchestrator.planning.htn.seed_methods import load_domain

NOW_MS = 1_700_000_000_000
REPO_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------------------
# Signatures come from the seed library, never from a hand-written copy
# --------------------------------------------------------------------------------------


def signatures(domain: str) -> dict[str, PredicateSignature]:
    return {item.predicate_ref.id: item for item in load_domain(domain).predicates}


CODE_SIGNATURES = signatures("code")
APPWORLD_SIGNATURES = signatures("appworld")


def sig(predicate: str) -> PredicateSignature:
    return CODE_SIGNATURES.get(predicate) or APPWORLD_SIGNATURES[predicate]


# --------------------------------------------------------------------------------------
# A scripted command runner
# --------------------------------------------------------------------------------------


class Scripted:
    """Answers commands from a script and records everything it was asked to run."""

    def __init__(self, answers: Mapping[str, CommandResult]) -> None:
        self.answers = dict(answers)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        self.calls.append(tuple(argv))
        for key, result in self.answers.items():
            if key in " ".join(argv):
                return result
        return CommandResult(exit_code=0, stdout="")


def ok(stdout: str = "", exit_code: int = 0) -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout)


def timed_out() -> CommandResult:
    return CommandResult(exit_code=-1, timed_out=True)


def config(tmp_path: Path, answers: Mapping[str, CommandResult], **overrides: Any) -> Any:
    runner = Scripted(answers)
    fields: dict[str, Any] = {"root": tmp_path, "runner": runner, "timeout_seconds": 1.0}
    fields.update(overrides)
    return CodeObserverConfig(**fields), runner


# ======================================================================================
# 1. Declarations and readers agree
# ======================================================================================


def test_every_code_predicate_has_a_reader() -> None:
    assert set(CODE_OBSERVER_COVERAGE) == set(CODE_SIGNATURES)
    assert len(CODE_OBSERVER_COVERAGE) >= 5


def test_every_appworld_predicate_has_a_reader() -> None:
    assert set(APPWORLD_OBSERVER_COVERAGE) == set(APPWORLD_SIGNATURES)
    assert len(APPWORLD_OBSERVER_COVERAGE) >= 5


@pytest.mark.parametrize("predicate", sorted(CODE_OBSERVER_COVERAGE))
def test_every_code_observer_is_one_the_signature_lists(predicate: str) -> None:
    listed = set(CODE_SIGNATURES[predicate].observer_ids)
    assert set(CODE_OBSERVER_COVERAGE[predicate]) <= listed


@pytest.mark.parametrize("predicate", sorted(APPWORLD_OBSERVER_COVERAGE))
def test_every_appworld_observer_is_one_the_signature_lists(predicate: str) -> None:
    listed = set(APPWORLD_SIGNATURES[predicate].observer_ids)
    assert set(APPWORLD_OBSERVER_COVERAGE[predicate]) <= listed


def test_the_factories_offer_five_observers_per_domain(tmp_path: Path) -> None:
    assert len(code_observers(tmp_path)) == 5
    assert len(appworld_observers()) == 5


def test_every_observer_satisfies_the_protocol(tmp_path: Path) -> None:
    for observer in (*code_observers(tmp_path), *appworld_observers()):
        assert isinstance(observer, PredicateObserver)
        assert observer.observer_id
        assert observer.predicate_ids()


def test_an_observer_refuses_a_predicate_it_does_not_read(tmp_path: Path) -> None:
    observer = HistoryObserver(config(tmp_path, {})[0])
    with pytest.raises(ContractError, match="does not observe"):
        observer.observe(sig("code.working-tree-clean"), {"repository": "r"}, now_ms=NOW_MS)


# ======================================================================================
# 2. The three answers of the code observers
# ======================================================================================


def test_a_checked_out_worktree_is_observed_true(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"rev-parse": ok("true\n")})
    observation = RepoObserver(conf).observe(
        sig("code.repo-checked-out"), {"repository": "repo-1"}, now_ms=NOW_MS
    )
    assert observation.available
    assert observation.polarity is True


def test_a_directory_that_is_not_a_worktree_is_observed_false(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"rev-parse": ok("", exit_code=128)})
    observation = RepoObserver(conf).observe(
        sig("code.repo-checked-out"), {"repository": "repo-1"}, now_ms=NOW_MS
    )
    assert observation.available
    assert observation.polarity is False


def test_a_missing_workspace_is_the_answer_not_an_outage(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path / "nowhere", {})
    observation = RepoObserver(conf).observe(
        sig("code.repo-checked-out"), {"repository": "repo-1"}, now_ms=NOW_MS
    )
    assert observation.polarity is False
    assert "does not exist" in observation.detail


def test_a_timed_out_command_is_unavailable_not_false(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"rev-parse": timed_out()})
    (tmp_path / "marker").write_text("x", encoding="utf-8")
    observation = RepoObserver(conf).observe(
        sig("code.repo-checked-out"), {"repository": "repo-1"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert observation.record is None
    assert observation.polarity is None
    assert "timed out" in observation.detail


def test_a_clean_worktree_is_observed_true(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"status": ok("")})
    observation = RepoObserver(conf).observe(
        sig("code.working-tree-clean"), {"repository": "repo-1"}, now_ms=NOW_MS
    )
    assert observation.polarity is True
    assert observation.record is not None
    assert observation.record.coverage is QueryCompleteness.BEST_EFFORT


def test_a_dirty_worktree_is_an_authoritative_negative(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"status": ok(" M src/a.py\n?? b.txt\n")})
    observation = RepoObserver(conf).observe(
        sig("code.working-tree-clean"), {"repository": "repo-1"}, now_ms=NOW_MS
    )
    record = observation.record
    assert record is not None
    assert record.polarity is False
    assert record.coverage is COMPLETE_COVERAGE is QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
    assert record.coverage_scope == f"worktree:{tmp_path}"
    assert record.query_watermark_ms == NOW_MS
    assert observation.authoritative_negative


def test_the_closed_world_denial_names_an_observer_the_signature_trusts(
    tmp_path: Path,
) -> None:
    conf, _runner = config(tmp_path, {"status": ok(" M a\n")})
    signature = sig("code.working-tree-clean")
    assert signature.world_assumption is WorldAssumption.CLOSED
    for observer in (RepoObserver(conf), WorkspaceObserver(conf)):
        observation = observer.observe(signature, {"repository": "r"}, now_ms=NOW_MS)
        assert observation.record is not None
        assert authoritative_negative_matches_observer(signature, observation.record)


def test_an_unreadable_status_command_is_unavailable(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"status": ok("", exit_code=128)})
    observation = RepoObserver(conf).observe(
        sig("code.working-tree-clean"), {"repository": "r"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert observation.record is None


def test_a_known_regression_commit_is_observed_true(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"refs/bisect/bad": ok("deadbeefcafe\n")})
    observation = HistoryObserver(conf).observe(
        sig("code.regression-commit-known"), {"repository": "r"}, now_ms=NOW_MS
    )
    assert observation.polarity is True
    assert "deadbeefcafe" in observation.detail


def test_an_unknown_regression_commit_is_observed_false(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"refs/bisect/bad": ok("", exit_code=1)})
    observation = HistoryObserver(conf).observe(
        sig("code.regression-commit-known"), {"repository": "r"}, now_ms=NOW_MS
    )
    assert observation.polarity is False


@pytest.mark.parametrize(
    ("numstat", "too_large", "reviewable"),
    [
        ("10\t5\tsrc/a.py\n", False, True),
        ("300\t200\tsrc/a.py\n", True, False),
        ("-\t-\tbin/image.png\n", False, True),
    ],
)
def test_the_changeset_observer_answers_both_predicates_consistently(
    tmp_path: Path, numstat: str, too_large: bool, reviewable: bool
) -> None:
    conf, _runner = config(tmp_path, {"numstat": ok(numstat)})
    observer = ChangesetObserver(conf)
    large = observer.observe(
        sig("code.changeset-too-large"), {"changeset": "HEAD~1..HEAD"}, now_ms=NOW_MS
    )
    small = observer.observe(
        sig("code.changeset-reviewable"), {"changeset": "HEAD~1..HEAD"}, now_ms=NOW_MS
    )
    assert large.polarity is too_large
    assert small.polarity is reviewable
    assert large.polarity is not small.polarity


def test_unparseable_numstat_is_unavailable_not_reviewable(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"numstat": ok("not numstat at all\n")})
    observation = ChangesetObserver(conf).observe(
        sig("code.changeset-reviewable"), {"changeset": "x"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE


def test_a_changeset_argument_that_names_nothing_is_unavailable(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {})
    observation = ChangesetObserver(conf).observe(
        sig("code.changeset-reviewable"), {"changeset": "   "}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE


def test_a_test_that_cannot_be_collected_is_unavailable_not_passing(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"collect-only": ok("", exit_code=4)})
    observation = SuiteObserver(conf).observe(
        sig("code.test-is-failing"), {"test": "tests/test_nowhere.py"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert "not the same as it passing" in observation.detail


def test_a_collectable_test_is_unavailable_while_execution_is_off(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"collect-only": ok("1 test collected\n")})
    observation = SuiteObserver(conf).observe(
        sig("code.test-is-failing"), {"test": "tests/test_a.py"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert "not enabled" in observation.detail


def test_with_execution_enabled_a_failing_target_is_observed_true(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        calls.append(tuple(argv))
        if "--collect-only" in argv:
            return ok("1 test collected\n")
        return ok("1 failed\n", exit_code=1)

    conf = CodeObserverConfig(
        root=tmp_path, runner=runner, timeout_seconds=1.0, allow_test_execution=True
    )
    observation = SuiteObserver(conf).observe(
        sig("code.test-is-failing"), {"test": "tests/test_a.py"}, now_ms=NOW_MS
    )
    assert observation.polarity is True
    assert all("no:cacheprovider" in " ".join(call) for call in calls)


def test_with_execution_enabled_a_passing_target_is_observed_false(tmp_path: Path) -> None:
    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        return ok("1 test collected\n") if "--collect-only" in argv else ok("1 passed\n")

    conf = CodeObserverConfig(
        root=tmp_path, runner=runner, timeout_seconds=1.0, allow_test_execution=True
    )
    observation = SuiteObserver(conf).observe(
        sig("code.test-is-failing"), {"test": "tests/test_a.py"}, now_ms=NOW_MS
    )
    assert observation.polarity is False


def test_an_internal_pytest_error_is_unavailable(tmp_path: Path) -> None:
    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        return ok("1 test collected\n") if "--collect-only" in argv else ok("boom", exit_code=3)

    conf = CodeObserverConfig(
        root=tmp_path, runner=runner, timeout_seconds=1.0, allow_test_execution=True
    )
    observation = SuiteObserver(conf).observe(
        sig("code.test-is-failing"), {"test": "tests/test_a.py"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE


# ======================================================================================
# 3. Read-only: the allowlist, the timeout, and the untouched worktree
# ======================================================================================


def test_the_git_allowlist_holds_only_reading_sub_commands() -> None:
    mutating = {"checkout", "commit", "fetch", "pull", "push", "stash", "clean", "reset", "add"}
    assert READ_ONLY_GIT & mutating == set()
    assert "status" in READ_ONLY_GIT and "diff" in READ_ONLY_GIT
    assert READ_ONLY_PYTHON == {"pytest"}


@pytest.mark.parametrize("subcommand", ["checkout", "commit", "reset", "clean", "stash"])
def test_a_mutating_git_command_is_refused_before_it_is_spawned(
    tmp_path: Path, subcommand: str
) -> None:
    conf, runner = config(tmp_path, {})
    from agent_orchestrator.planning.htn.observers.code import _Command

    with pytest.raises(ContractError, match="read-only sub-command"):
        _Command(conf).git(subcommand)
    assert runner.calls == []


def test_a_program_that_is_not_git_or_python_is_refused(tmp_path: Path) -> None:
    conf, runner = config(tmp_path, {})
    from agent_orchestrator.planning.htn.observers.code import _Command

    with pytest.raises(ContractError, match="read-only programs"):
        _Command(conf).run(["rm", "-rf", "/"])
    assert runner.calls == []


def test_every_observer_command_carries_the_configured_timeout(tmp_path: Path) -> None:
    seen: list[float] = []

    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        seen.append(timeout)
        return ok("")

    conf = CodeObserverConfig(root=tmp_path, runner=runner, timeout_seconds=2.5)
    RepoObserver(conf).observe(sig("code.working-tree-clean"), {"repository": "r"}, now_ms=1)
    HistoryObserver(conf).observe(
        sig("code.regression-commit-known"), {"repository": "r"}, now_ms=1
    )
    assert seen == [2.5, 2.5]


def test_a_config_without_a_timeout_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="positive timeout"):
        CodeObserverConfig(root=tmp_path, timeout_seconds=0.0)


def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_observing_a_worktree_leaves_every_byte_where_it_was(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.txt").write_text("b\n", encoding="utf-8")
    before = snapshot(tmp_path)
    conf, _runner = config(
        tmp_path,
        {"status": ok(" M a.py\n"), "rev-parse": ok("true\n"), "numstat": ok("1\t1\ta.py\n")},
    )
    for observer in code_observers(tmp_path):
        del observer
    RepoObserver(conf).observe(sig("code.repo-checked-out"), {"repository": "r"}, now_ms=NOW_MS)
    RepoObserver(conf).observe(sig("code.working-tree-clean"), {"repository": "r"}, now_ms=NOW_MS)
    ChangesetObserver(conf).observe(
        sig("code.changeset-reviewable"), {"changeset": "HEAD"}, now_ms=NOW_MS
    )
    assert snapshot(tmp_path) == before


def test_the_real_runner_reads_this_repository_without_touching_it() -> None:
    """One real subprocess, so ``run_read_only`` is exercised rather than mocked."""

    result = run_read_only(["git", "rev-parse", "--is-inside-work-tree"], REPO_ROOT, 20.0)
    if result.missing:  # pragma: no cover - a machine without git
        pytest.skip("git is not available here")
    assert result.exit_code == 0
    assert result.stdout.strip() == "true"


def test_the_real_repo_observer_answers_repo_checked_out() -> None:
    observer = RepoObserver(CodeObserverConfig(root=REPO_ROOT, timeout_seconds=20.0))
    observation = observer.observe(
        sig("code.repo-checked-out"), {"repository": "simple-harness-sdk"}, now_ms=NOW_MS
    )
    if not observation.available:  # pragma: no cover - a machine without git
        pytest.skip("git is not available here")
    assert observation.polarity is True
    assert observation.record is not None
    assert observation.record.source_ref.produced_by is Provenance.TOOL
    assert observation.record.source_ref.kind is TypedRefKind.OBSERVATION


def test_a_missing_binary_is_unavailable(tmp_path: Path) -> None:
    result = run_read_only(["git-that-does-not-exist", "status"], tmp_path, 1.0)
    assert result.missing
    assert not result.usable


def test_a_real_timeout_is_reported_as_a_timeout(tmp_path: Path) -> None:
    try:
        result = run_read_only(["sleep", "5"], tmp_path, 0.2)
    except FileNotFoundError:  # pragma: no cover - platform without sleep
        pytest.skip("no sleep binary")
    assert result.timed_out
    assert not result.usable


# ======================================================================================
# 4. The observation constructors refuse every inadmissible shape
# ======================================================================================


def test_an_unlisted_observer_may_not_observe_a_predicate() -> None:
    with pytest.raises(ContractError, match="not listed by predicate"):
        observed(
            sig("code.repo-checked-out"),
            {"repository": "r"},
            polarity=True,
            observer_id="code.test-observer",
            now_ms=NOW_MS,
        )


def test_a_closed_world_false_without_authority_is_refused() -> None:
    with pytest.raises(ContractError, match="AUTHORITATIVE_WITH_SCOPE"):
        observed(
            sig("code.working-tree-clean"),
            {"repository": "r"},
            polarity=False,
            observer_id="code.repo-observer",
            now_ms=NOW_MS,
        )


def test_a_closed_world_false_without_a_watermark_is_refused() -> None:
    with pytest.raises(ContractError):
        observed(
            sig("code.working-tree-clean"),
            {"repository": "r"},
            polarity=False,
            observer_id="code.repo-observer",
            now_ms=NOW_MS,
            coverage=COMPLETE_COVERAGE,
            coverage_scope="worktree:x",
        )


def test_an_open_world_false_needs_no_authority() -> None:
    observation = observed(
        sig("code.repo-checked-out"),
        {"repository": "r"},
        polarity=False,
        observer_id="code.repo-observer",
        now_ms=NOW_MS,
    )
    assert observation.polarity is False
    assert not observation.authoritative_negative


def test_arguments_are_type_checked_against_the_declaration() -> None:
    with pytest.raises(ContractError, match="type-check"):
        observed(
            sig("code.repo-checked-out"),
            {"repository": 17},
            polarity=True,
            observer_id="code.repo-observer",
            now_ms=NOW_MS,
        )


def test_an_unavailable_observation_may_not_carry_a_record() -> None:
    good = denial(
        sig("code.working-tree-clean"),
        {"repository": "r"},
        observer_id="code.repo-observer",
        now_ms=NOW_MS,
        coverage_scope="worktree:x",
    )
    with pytest.raises(ContractError, match="outage is not a polarity"):
        Observation(
            outcome=ObservationOutcome.OBSERVER_UNAVAILABLE,
            observer_id="code.repo-observer",
            predicate_id="code.working-tree-clean",
            record=good.record,
        )


def test_an_observed_observation_must_carry_a_record() -> None:
    with pytest.raises(ContractError, match="carries an ObservationRecord"):
        Observation(
            outcome=ObservationOutcome.OBSERVED,
            observer_id="code.repo-observer",
            predicate_id="code.working-tree-clean",
        )


def test_the_proposition_key_is_the_predicate_version_plus_its_arguments() -> None:
    signature = sig("code.repo-checked-out")
    observation = observed(
        signature,
        {"repository": "repo-1"},
        polarity=True,
        observer_id="code.repo-observer",
        now_ms=NOW_MS,
    )
    assert observation.record is not None
    assert observation.record.proposition_key == proposition_key(
        signature, {"repository": "repo-1"}
    )


def test_an_unavailable_observation_serialises_without_a_record() -> None:
    payload = unavailable("code.repo-observer", "code.repo-checked-out", "no git").to_json()
    assert payload["record"] is None
    assert payload["outcome"] == str(ObservationOutcome.OBSERVER_UNAVAILABLE)


# ======================================================================================
# 5. The appworld observers
# ======================================================================================


class FakeEpisode:
    """A read-only AppWorld stand-in that records every call it was asked to make."""

    def __init__(
        self,
        *,
        surface: Sequence[tuple[str, str]] = (("supervisor", "show_active_task"),),
        projection: Mapping[str, Any] | None = None,
        matches: int | None = None,
        confirmed: Sequence[str] | None = None,
        transport_error: bool = False,
    ) -> None:
        self.surface = list(surface)
        self.projection = dict(projection or {"instruction": "do it", "status": "active"})
        self.matches = matches
        self.confirmed = None if confirmed is None else list(confirmed)
        self.transport_error = transport_error
        self.calls: list[str] = []

    def observe_public_api(
        self, app: str, api: str, *, parameters: Mapping[str, Any] | None = None
    ) -> tuple[Any, Mapping[str, Any]]:
        self.calls.append(f"observe_public_api:{app}.{api}")
        if self.transport_error:
            raise OSError("connection refused")
        if (app, api) not in self.surface:
            raise ValueError(f"{app}.{api} is not authorized for observation")
        return object(), dict(self.projection)

    def public_read_apis(self) -> Sequence[tuple[str, str]]:
        self.calls.append("public_read_apis")
        return list(self.surface)


class FakeEntityEpisode(FakeEpisode):
    def entity_matches(self, entity: str) -> int:
        self.calls.append(f"entity_matches:{entity}")
        assert self.matches is not None
        return self.matches


class FakeLedgerEpisode(FakeEpisode):
    def confirmed_action_ids(self) -> Sequence[str]:
        self.calls.append("confirmed_action_ids")
        return list(self.confirmed or ())

    def receipt_scope(self) -> str:
        self.calls.append("receipt_scope")
        return "episode:task-1"


READ_ONLY_CALLS = frozenset(
    {
        "observe_public_api",
        "public_read_apis",
        "entity_matches",
        "confirmed_action_ids",
        "receipt_scope",
    }
)


def called_methods(episode: FakeEpisode) -> set[str]:
    return {item.split(":", 1)[0] for item in episode.calls}


def test_with_no_episode_every_appworld_observer_is_unavailable() -> None:
    for observer in appworld_observers():
        predicate = observer.predicate_ids()[0]
        observation = observer.observe(
            APPWORLD_SIGNATURES[predicate], _arguments(predicate), now_ms=NOW_MS
        )
        assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
        assert observation.record is None
        assert "not started" in observation.detail


def _arguments(predicate: str) -> dict[str, Any]:
    name = APPWORLD_SIGNATURES[predicate].parameters[0].name
    return {name: "supervisor" if name == "app" else "subject-1"}


def test_a_client_without_the_read_method_is_refused() -> None:
    with pytest.raises(ContractError, match="observe_public_api"):
        AppWorldObserverConfig(client=object())  # type: ignore[arg-type]


def test_a_reachable_app_is_observed_true() -> None:
    episode = FakeEpisode()
    observer = AvailabilityObserver(AppWorldObserverConfig(client=episode))
    observation = observer.observe(
        APPWORLD_SIGNATURES["appworld.app-reachable"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.polarity is True
    assert called_methods(episode) <= READ_ONLY_CALLS


def test_an_app_the_host_refuses_to_read_is_observed_false() -> None:
    episode = FakeEpisode(surface=[("phone", "show_active_task")])
    observer = AvailabilityObserver(AppWorldObserverConfig(client=episode))
    observation = observer.observe(
        APPWORLD_SIGNATURES["appworld.app-reachable"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.polarity is False
    assert "refused" in observation.detail


def test_a_transport_failure_is_unavailable_not_unreachable() -> None:
    episode = FakeEpisode(transport_error=True)
    observer = AvailabilityObserver(AppWorldObserverConfig(client=episode))
    observation = observer.observe(
        APPWORLD_SIGNATURES["appworld.app-reachable"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert observation.record is None


def test_valid_credentials_are_observed_true() -> None:
    episode = FakeEpisode(
        surface=[("supervisor", "show_profile")],
        projection={"first_name": "Ada", "last_name": "L"},
    )
    observer = CredentialObserver(AppWorldObserverConfig(client=episode))
    observation = observer.observe(
        APPWORLD_SIGNATURES["appworld.credentials-valid"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.polarity is True


def test_an_empty_authorised_projection_is_observed_false() -> None:
    episode = FakeEpisode(surface=[("supervisor", "show_profile")], projection={"first_name": None})
    observer = CredentialObserver(AppWorldObserverConfig(client=episode))
    observation = observer.observe(
        APPWORLD_SIGNATURES["appworld.credentials-valid"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.polarity is False


def test_the_api_observer_reads_the_declared_surface() -> None:
    episode = FakeEpisode(surface=[("phone", "show_active_task")])
    observer = ApiObserver(AppWorldObserverConfig(client=episode))
    covered = observer.observe(
        APPWORLD_SIGNATURES["appworld.api-supports-request"], {"app": "phone"}, now_ms=NOW_MS
    )
    uncovered = observer.observe(
        APPWORLD_SIGNATURES["appworld.api-supports-request"], {"app": "venmo"}, now_ms=NOW_MS
    )
    assert covered.polarity is True
    assert uncovered.polarity is False
    assert "declared read surface" in covered.detail


@pytest.mark.parametrize(
    ("matches", "ambiguous", "unique"),
    [(0, False, False), (1, False, True), (3, True, False)],
)
def test_the_entity_observer_answers_both_cardinality_predicates(
    matches: int, ambiguous: bool, unique: bool
) -> None:
    episode = FakeEntityEpisode(matches=matches)
    observer = EntityObserver(AppWorldObserverConfig(client=episode))
    left = observer.observe(
        APPWORLD_SIGNATURES["appworld.entity-ambiguous"], {"entity": "Ada"}, now_ms=NOW_MS
    )
    right = observer.observe(
        APPWORLD_SIGNATURES["appworld.entity-unique"], {"entity": "Ada"}, now_ms=NOW_MS
    )
    assert left.polarity is ambiguous
    assert right.polarity is unique
    assert called_methods(episode) <= READ_ONLY_CALLS


def test_an_episode_with_no_cardinality_read_is_unavailable() -> None:
    observer = EntityObserver(AppWorldObserverConfig(client=FakeEpisode()))
    observation = observer.observe(
        APPWORLD_SIGNATURES["appworld.entity-unique"], {"entity": "Ada"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert "entity_matches" in observation.detail


def test_a_confirmed_action_is_observed_true() -> None:
    episode = FakeLedgerEpisode(confirmed=["act-1", "act-2"])
    observer = ReceiptObserver(AppWorldObserverConfig(client=episode))
    observation = observer.observe(
        APPWORLD_SIGNATURES["appworld.action-confirmed"], {"action": "act-1"}, now_ms=NOW_MS
    )
    assert observation.polarity is True


def test_an_unconfirmed_action_is_an_authoritative_negative() -> None:
    episode = FakeLedgerEpisode(confirmed=["act-2"])
    signature = APPWORLD_SIGNATURES["appworld.action-confirmed"]
    assert signature.world_assumption is WorldAssumption.CLOSED
    observer = ReceiptObserver(AppWorldObserverConfig(client=episode))
    observation = observer.observe(signature, {"action": "act-1"}, now_ms=NOW_MS)
    record = observation.record
    assert record is not None
    assert record.polarity is False
    assert record.coverage is COMPLETE_COVERAGE
    assert record.coverage_scope == "episode:task-1"
    assert authoritative_negative_matches_observer(signature, record)


def test_an_episode_with_no_ledger_cannot_deny_a_closed_predicate() -> None:
    observer = ReceiptObserver(AppWorldObserverConfig(client=FakeEpisode()))
    observation = observer.observe(
        APPWORLD_SIGNATURES["appworld.action-confirmed"], {"action": "act-1"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert observation.record is None
    assert "UNKNOWN, not FALSE" in observation.detail


def test_no_appworld_observer_ever_calls_anything_but_a_read() -> None:
    episode = FakeLedgerEpisode(confirmed=["act-1"], matches=1)
    for observer in appworld_observers(episode):
        for predicate in observer.predicate_ids():
            try:
                observer.observe(
                    APPWORLD_SIGNATURES[predicate], _arguments(predicate), now_ms=NOW_MS
                )
            except ContractError:  # pragma: no cover - argument shape only
                continue
    assert called_methods(episode) <= READ_ONLY_CALLS
    assert not any("execute" in call for call in episode.calls)


# ======================================================================================
# 6. Mutation self-check
# ======================================================================================


def test_mutant_a_denial_built_with_best_effort_coverage_would_not_be_authoritative() -> None:
    signature = sig("code.working-tree-clean")
    real = denial(
        signature,
        {"repository": "r"},
        observer_id="code.repo-observer",
        now_ms=NOW_MS,
        coverage_scope="worktree:x",
    )
    assert real.authoritative_negative
    with pytest.raises(ContractError):
        observed(
            signature,
            {"repository": "r"},
            polarity=False,
            observer_id="code.repo-observer",
            now_ms=NOW_MS,
            coverage=QueryCompleteness.BEST_EFFORT,
            coverage_scope="worktree:x",
            query_watermark_ms=NOW_MS,
        )


def test_mutant_turning_an_outage_into_false_would_change_every_verdict(
    tmp_path: Path,
) -> None:
    (tmp_path / "keep").write_text("x", encoding="utf-8")
    conf, _runner = config(tmp_path, {"status": timed_out()})
    observation = RepoObserver(conf).observe(
        sig("code.working-tree-clean"), {"repository": "r"}, now_ms=NOW_MS
    )
    assert observation.polarity is None
    clean, _runner = config(tmp_path, {"status": ok("")})
    assert (
        RepoObserver(clean)
        .observe(sig("code.working-tree-clean"), {"repository": "r"}, now_ms=NOW_MS)
        .polarity
        is True
    )


def test_mutant_widening_the_allowlist_would_let_a_write_through(tmp_path: Path) -> None:
    conf, runner = config(tmp_path, {})
    from agent_orchestrator.planning.htn.observers.code import _Command

    command = _Command(conf)
    with pytest.raises(ContractError):
        command.git("checkout", "main")
    assert runner.calls == []
    command.git("status", "--porcelain")
    assert runner.calls == [("git", "status", "--porcelain")]


def test_mutant_an_observer_speaking_for_another_predicate_is_refused(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {"status": ok(" M a\n")})
    observer = RepoObserver(conf)
    assert observer.observe(
        sig("code.working-tree-clean"), {"repository": "r"}, now_ms=NOW_MS
    ).available
    with pytest.raises(ContractError):
        observer.observe(sig("code.test-is-failing"), {"test": "t"}, now_ms=NOW_MS)


def test_mutant_a_fabricated_receipt_provenance_would_be_model_attributed() -> None:
    observation = observed(
        sig("code.repo-checked-out"),
        {"repository": "r"},
        polarity=True,
        observer_id="code.repo-observer",
        now_ms=NOW_MS,
    )
    assert observation.record is not None
    assert observation.record.source_ref.produced_by is Provenance.TOOL
    assert observation.record.source_ref.produced_by is not Provenance.MODEL


def test_mutant_an_appworld_observer_that_guessed_a_cardinality_would_answer_unique() -> None:
    observer = EntityObserver(AppWorldObserverConfig(client=FakeEpisode()))
    observation = observer.observe(
        APPWORLD_SIGNATURES["appworld.entity-unique"], {"entity": "Ada"}, now_ms=NOW_MS
    )
    assert observation.polarity is not True
    with_index = EntityObserver(AppWorldObserverConfig(client=FakeEntityEpisode(matches=1)))
    assert (
        with_index.observe(
            APPWORLD_SIGNATURES["appworld.entity-unique"], {"entity": "Ada"}, now_ms=NOW_MS
        ).polarity
        is True
    )


def test_the_subprocess_module_is_only_reached_through_the_allowlist() -> None:
    """A direct ``subprocess.run`` anywhere in the observers would bypass the gate."""

    import agent_orchestrator.planning.htn.observers.appworld as appworld_module
    import agent_orchestrator.planning.htn.observers.code as code_module

    assert "subprocess" not in dir(appworld_module)
    source = Path(code_module.__file__).read_text(encoding="utf-8")
    assert source.count("subprocess.run(") == 1
    assert subprocess.run is not None


# ======================================================================================
# 7. Review fixes (P0-3): the allowlist covers the whole argv, not just the sub-command
# ======================================================================================


@pytest.mark.parametrize(
    "written",
    [
        "--output=/tmp/pwned.txt",
        "--output-file=/tmp/pwned.txt",
        "--junitxml=/tmp/pwned.xml",
        "--stat-graph-width=1",
        "-o/tmp/pwned.txt",
    ],
)
def test_a_flag_that_writes_a_file_is_refused_even_on_a_reading_subcommand(
    tmp_path: Path, written: str
) -> None:
    """The gap the review found: ``git diff --numstat --output=x`` writes ``x``."""

    conf, runner = config(tmp_path, {})
    from agent_orchestrator.planning.htn.observers.code import _Command

    with pytest.raises(ContractError, match="trusted arguments"):
        _Command(conf).run(["git", "diff", "--numstat", written])
    assert runner.calls == []


@pytest.mark.parametrize("written", ["--junitxml=/tmp/p.xml", "--result-log=/tmp/p.log"])
def test_a_pytest_flag_that_writes_a_file_is_refused(tmp_path: Path, written: str) -> None:
    conf, runner = config(tmp_path, {})
    from agent_orchestrator.planning.htn.observers.code import _Command

    with pytest.raises(ContractError, match="trusted arguments"):
        _Command(conf).run([conf.python_executable, "-m", "pytest", "--collect-only", written])
    assert runner.calls == []


def test_an_operand_that_looks_like_a_writing_flag_is_refused(tmp_path: Path) -> None:
    """The real attack: the operand *is* the caller's argument."""

    conf, runner = config(tmp_path, {})
    observation = ChangesetObserver(conf).observe(
        sig("code.changeset-reviewable"),
        {"changeset": "--output=/tmp/pwned.txt"},
        now_ms=NOW_MS,
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert "starts with '-'" in observation.detail
    assert runner.calls == []


def test_a_test_target_that_looks_like_a_writing_flag_is_refused(tmp_path: Path) -> None:
    conf, runner = config(tmp_path, {})
    observation = SuiteObserver(conf).observe(
        sig("code.test-is-failing"), {"test": "--junitxml=/tmp/pwned.xml"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert "starts with '-'" in observation.detail
    assert runner.calls == []


def test_a_writing_flag_operand_really_would_have_written_without_the_gate(
    tmp_path: Path,
) -> None:
    """The counter-example, run for real: prove the flag writes, then prove we refuse it.

    Without this the refusal above is a test of a string, not of a danger.
    """

    target = tmp_path / "pwned.txt"
    worktree = tmp_path / "repo"
    worktree.mkdir()
    result = run_read_only(["git", "diff", "--numstat", f"--output={target}"], worktree, 10.0)
    if result.missing:  # pragma: no cover - a machine without git
        pytest.skip("git is not available here")
    # git happily creates the file: that is exactly why the operand gate exists.
    assert target.exists()
    target.unlink()
    conf, _runner = config(worktree, {})
    from agent_orchestrator.planning.htn.observers.code import _Command

    with pytest.raises(ContractError):
        _Command(conf).run(["git", "diff", "--numstat", f"--output={target}"])
    assert not target.exists()


def test_every_operand_goes_after_the_separator(tmp_path: Path) -> None:
    conf, runner = config(tmp_path, {"numstat": ok("1\t1\ta.py\n")})
    ChangesetObserver(conf).observe(
        sig("code.changeset-reviewable"), {"changeset": "HEAD~1..HEAD"}, now_ms=NOW_MS
    )
    assert runner.calls == [("git", "diff", "--numstat", OPERAND_SEPARATOR, "HEAD~1..HEAD")]


def test_the_history_observer_passes_its_ref_as_an_operand(tmp_path: Path) -> None:
    conf, runner = config(tmp_path, {"refs/bisect/bad": ok("abc\n")})
    HistoryObserver(conf).observe(
        sig("code.regression-commit-known"), {"repository": "r"}, now_ms=NOW_MS
    )
    assert runner.calls[0] == (
        "git",
        "rev-parse",
        "--verify",
        "--quiet",
        OPERAND_SEPARATOR,
        "refs/bisect/bad",
    )


def test_the_trusted_argument_table_covers_every_allowed_subcommand() -> None:
    for subcommand in READ_ONLY_GIT:
        assert subcommand in TRUSTED_ARGUMENTS, subcommand
    for subcommand in READ_ONLY_PYTHON:
        assert subcommand in TRUSTED_ARGUMENTS, subcommand


def test_no_trusted_argument_is_one_that_writes() -> None:
    writing = ("output", "junitxml", "result-log", "log-file", "export", "into")
    for subcommand, flags in TRUSTED_ARGUMENTS.items():
        for flag in flags:
            assert not any(word in flag for word in writing), (subcommand, flag)


@pytest.mark.parametrize("bad", ["", "   ", "-x", "--flag", "a\nb", "a\0b"])
def test_the_operand_validator_refuses_everything_that_is_not_a_plain_operand(
    bad: str,
) -> None:
    with pytest.raises(ContractError):
        operand(bad, "operand")


def test_the_operand_validator_accepts_a_path_a_ref_and_a_range() -> None:
    for value in ("tests/test_a.py", "refs/bisect/bad", "HEAD~3..HEAD", "src/a b.py"):
        assert operand(value, "operand") == value


def test_a_second_separator_is_refused(tmp_path: Path) -> None:
    conf, _runner = config(tmp_path, {})
    from agent_orchestrator.planning.htn.observers.code import _Command

    with pytest.raises(ContractError, match="appears twice"):
        _Command(conf).run(["git", "status", "--porcelain", "--", "a", "--", "b"])


# ======================================================================================
# 7b. Review fixes (P0-3): collection happens in a throwaway copy, and inside a budget
# ======================================================================================


def test_collection_runs_in_a_copy_and_never_in_the_real_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    (worktree / "tests").mkdir()
    (worktree / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    seen: list[Path] = []

    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        seen.append(cwd)
        return ok("1 test collected\n")

    conf = CodeObserverConfig(root=worktree, runner=runner, timeout_seconds=1.0)
    SuiteObserver(conf).observe(
        sig("code.test-is-failing"), {"test": "tests/test_a.py"}, now_ms=NOW_MS
    )
    assert seen
    assert all(place != worktree for place in seen)
    assert all("sh-observer-" in str(place) for place in seen)


def test_a_test_module_that_writes_on_import_cannot_touch_the_worktree(
    tmp_path: Path,
) -> None:
    """Collection *imports* the module, and a module's top level may write."""

    worktree = tmp_path / "repo"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "tests" / "test_writes.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('side-effect.txt').write_text('written')\n"
        "def test_ok():\n    pass\n",
        encoding="utf-8",
    )
    before = snapshot(worktree)
    observed_result = SuiteObserver(
        CodeObserverConfig(root=worktree, timeout_seconds=60.0)
    ).observe(sig("code.test-is-failing"), {"test": "tests/test_writes.py"}, now_ms=NOW_MS)
    assert observed_result.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    # Whatever pytest did, it did it somewhere else.
    assert snapshot(worktree) == before
    assert not (worktree / "tests" / "side-effect.txt").exists()


def test_a_worktree_too_large_to_copy_is_unavailable(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    (worktree / "big.bin").write_bytes(b"x" * 4096)
    conf = CodeObserverConfig(root=worktree, timeout_seconds=1.0, max_copy_bytes=16)
    observation = SuiteObserver(conf).observe(
        sig("code.test-is-failing"), {"test": "tests/test_a.py"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert "throwaway copy" in observation.detail


def test_the_read_only_copy_excludes_git_and_the_caches(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (worktree / "__pycache__").mkdir()
    (worktree / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (worktree / "a.py").write_text("x = 1\n", encoding="utf-8")
    with read_only_copy(worktree, limit=10_000_000) as copied:
        assert copied is not None
        assert (copied / "a.py").exists()
        for excluded in COPY_EXCLUDES:
            assert not (copied / excluded).exists()
        held = copied
    assert not held.exists()


def test_the_copy_is_deleted_even_when_the_body_raises(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    (worktree / "a.py").write_text("x = 1\n", encoding="utf-8")
    held: Path | None = None
    with pytest.raises(RuntimeError):
        with read_only_copy(worktree, limit=10_000_000) as copied:
            held = copied
            raise RuntimeError("boom")
    assert held is not None
    assert not held.exists()


def test_one_observation_has_one_budget_across_its_commands(tmp_path: Path) -> None:
    """A per-command timeout is not a per-observation bound; both now exist."""

    def slow(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        time.sleep(0.05)
        return ok("1 test collected\n")

    conf = CodeObserverConfig(
        root=tmp_path,
        runner=slow,
        timeout_seconds=10.0,
        budget_seconds=0.01,
        allow_test_execution=True,
    )
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    observation = SuiteObserver(conf).observe(
        sig("code.test-is-failing"), {"test": "tests/test_a.py"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE


def test_each_command_gets_no_more_than_what_is_left_of_the_budget(tmp_path: Path) -> None:
    handed: list[float] = []

    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        handed.append(timeout)
        return ok("")

    conf = CodeObserverConfig(
        root=tmp_path, runner=runner, timeout_seconds=30.0, budget_seconds=2.0
    )
    RepoObserver(conf).observe(sig("code.working-tree-clean"), {"repository": "r"}, now_ms=1)
    assert handed and handed[0] <= 2.0


def test_a_config_without_a_budget_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="positive budget"):
        CodeObserverConfig(root=tmp_path, budget_seconds=0.0)


def test_an_exhausted_budget_is_reported_as_a_timeout_not_a_polarity(tmp_path: Path) -> None:
    from agent_orchestrator.planning.htn.observers.code import _Budget, _Command

    conf = CodeObserverConfig(root=tmp_path, timeout_seconds=1.0)
    spent = _Budget(-1.0)
    result = _Command(conf, spent).git("status", "--porcelain")
    assert result.timed_out
    with pytest.raises(BudgetExhausted):
        spent.slice(1.0)


# ======================================================================================
# 7c. Review fixes (P0-4): a reply we could not read is not a FALSE
# ======================================================================================


class BrokenEpisode(FakeEpisode):
    """An episode whose reads fail in one of the ways the host really fails."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def observe_public_api(
        self, app: str, api: str, *, parameters: Mapping[str, Any] | None = None
    ) -> tuple[Any, Mapping[str, Any]]:
        self.calls.append(f"observe_public_api:{app}.{api}")
        raise self.error


def test_a_malformed_envelope_is_unavailable_not_false() -> None:
    """The review's case: a JSON/projection failure used to come out as polarity False."""

    episode = BrokenEpisode(ValueError("API response has unexpected fields"))
    observation = AvailabilityObserver(AppWorldObserverConfig(client=episode)).observe(
        APPWORLD_SIGNATURES["appworld.app-reachable"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert observation.record is None
    assert observation.polarity is None


def test_a_world_whose_identity_moved_is_unavailable_not_false() -> None:
    episode = BrokenEpisode(ValueError("public observation world identity differs"))
    observation = AvailabilityObserver(AppWorldObserverConfig(client=episode)).observe(
        APPWORLD_SIGNATURES["appworld.app-reachable"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert "did not produce a usable answer" in observation.detail


def test_only_a_genuine_authorisation_refusal_is_a_negative_observation() -> None:
    episode = BrokenEpisode(ValueError("API is not authorized for observation"))
    observation = AvailabilityObserver(AppWorldObserverConfig(client=episode)).observe(
        APPWORLD_SIGNATURES["appworld.app-reachable"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.available
    assert observation.polarity is False


def test_an_oversize_projection_is_unavailable_not_false() -> None:
    episode = BrokenEpisode(ValueError("API response exceeds the evidence limit"))
    observation = CredentialObserver(AppWorldObserverConfig(client=episode)).observe(
        APPWORLD_SIGNATURES["appworld.credentials-valid"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE


def test_an_unrecognised_error_defaults_to_unavailable() -> None:
    episode = BrokenEpisode(ValueError("something nobody has seen before"))
    observation = CredentialObserver(AppWorldObserverConfig(client=episode)).observe(
        APPWORLD_SIGNATURES["appworld.credentials-valid"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE


def test_a_projection_that_is_not_a_mapping_is_unavailable() -> None:
    class OddEpisode(FakeEpisode):
        def observe_public_api(
            self, app: str, api: str, *, parameters: Mapping[str, Any] | None = None
        ) -> tuple[Any, Mapping[str, Any]]:
            self.calls.append("observe_public_api")
            return object(), ["not", "a", "mapping"]  # type: ignore[return-value]

    observation = AvailabilityObserver(AppWorldObserverConfig(client=OddEpisode())).observe(
        APPWORLD_SIGNATURES["appworld.app-reachable"], {"app": "supervisor"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert "rather than a field projection" in observation.detail


@pytest.mark.parametrize("marker", REFUSAL_MARKERS)
def test_every_refusal_marker_classifies_as_refused(marker: str) -> None:
    assert classify_read_error(ValueError(f"boom: {marker}")) is ReadOutcome.REFUSED


@pytest.mark.parametrize("marker", INTEGRITY_MARKERS)
def test_every_integrity_marker_classifies_as_unavailable(marker: str) -> None:
    assert classify_read_error(ValueError(f"boom: {marker}")) is ReadOutcome.UNAVAILABLE


def test_an_integrity_marker_wins_over_a_refusal_sounding_phrase() -> None:
    error = ValueError("not authorized: public observation world identity differs")
    assert classify_read_error(error) is ReadOutcome.UNAVAILABLE


def test_a_ledger_that_cannot_be_read_does_not_deny_a_closed_predicate() -> None:
    class BadLedger(FakeLedgerEpisode):
        def confirmed_action_ids(self) -> Sequence[str]:
            self.calls.append("confirmed_action_ids")
            raise ValueError("the ledger page is malformed")

    observation = ReceiptObserver(AppWorldObserverConfig(client=BadLedger())).observe(
        APPWORLD_SIGNATURES["appworld.action-confirmed"], {"action": "act-1"}, now_ms=NOW_MS
    )
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert observation.record is None


def test_a_ledger_with_no_scope_cannot_deny_a_closed_predicate() -> None:
    class ScopelessLedger(FakeLedgerEpisode):
        def receipt_scope(self) -> str:
            self.calls.append("receipt_scope")
            return "  "

    observation = ReceiptObserver(
        AppWorldObserverConfig(client=ScopelessLedger(confirmed=["act-2"]))
    ).observe(APPWORLD_SIGNATURES["appworld.action-confirmed"], {"action": "act-1"}, now_ms=NOW_MS)
    assert observation.outcome is ObservationOutcome.OBSERVER_UNAVAILABLE
    assert "no coverage scope" in observation.detail


def test_mutant_folding_every_value_error_into_refused_would_forge_a_false() -> None:
    """The shape of the wrong implementation, written out."""

    for error in (
        ValueError("API response has unexpected fields"),
        ValueError("public observation world identity differs"),
        ValueError("API response exceeds the evidence limit"),
    ):
        assert classify_read_error(error) is ReadOutcome.UNAVAILABLE
    assert (
        classify_read_error(ValueError("API is not authorized for observation"))
        is ReadOutcome.REFUSED
    )
