# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Read-only observers for the ``code`` pilot domain (§7.3 seed library, §6.6).

Five observers over the six predicates the seed ``code`` domain declares.  Each one
answers by running a **read-only command** against an isolated worktree and nothing
else: no checkout, no fetch, no commit, no stash, no file written — and no
``.pytest_cache`` either, which is why the collection command carries
``-p no:cacheprovider``.

The allowlist is the mechanism, not the intention.  :data:`READ_ONLY_GIT` and
:data:`READ_ONLY_PYTHON` name the exact sub-commands this module may run, and
:meth:`_Command.run` refuses anything else *before* spawning a process — so
"observers only read" is a check rather than a comment, and adding a mutating
command to an observer fails a test instead of mutating a repository.

Every command has a timeout.  A timeout, a missing binary and a command that
reported something inconclusive are all ``OBSERVER_UNAVAILABLE``: not being able to
look is a third answer, never the proposition being false (AER §8.2).

``code.working-tree-clean`` is the domain's CLOSED predicate, and it is the reason
this file exists in P2 rather than P6: ``git status --porcelain`` genuinely
*enumerates* the worktree, so an empty answer is a complete query and a non-empty
one is an authoritative negative — the one observation shape §6.6 C28 lets a closed
domain conclude FALSE from.
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 - read-only commands from a fixed allowlist, see _Command.run
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ....contracts.models import ContractError
from ....knowledge.predicates import PredicateSignature
from . import Observation, denial, observed, unavailable

#: This module's own version, recorded on every observation so a later replay can
#: tell which reader produced it (AER §8.1).
OBSERVER_VERSION = "code-observers-v1"

#: The git sub-commands an observer may run.  Every one of them only reads; the
#: mutating half of git (checkout, fetch, commit, stash, clean, reset, ...) is
#: absent on purpose and :meth:`_Command.run` refuses it.
READ_ONLY_GIT: frozenset[str] = frozenset(
    {"rev-parse", "status", "diff", "log", "ls-files", "show-ref", "cat-file"}
)

#: The python sub-commands an observer may run.  ``pytest --collect-only`` imports
#: test modules, which is a read of the repository; running the tests themselves is
#: gated separately (:attr:`CodeObserverConfig.allow_test_execution`).
READ_ONLY_PYTHON: frozenset[str] = frozenset({"pytest"})

#: The **only** tokens an observer command may carry before its ``--`` separator,
#: per sub-command.  This is the other half of the allowlist and the half the P2.3c
#: review found missing: checking the sub-command alone let ``--output=…`` and
#: ``--junitxml=…`` through, and those are read-only sub-commands that *write files*.
#: A flag is how a command is steered, so no flag is ever taken from a caller — the
#: set is fixed here and the operands go after ``--``.
TRUSTED_ARGUMENTS: Mapping[str, frozenset[str]] = {
    "rev-parse": frozenset({"--is-inside-work-tree", "--verify", "--quiet"}),
    "status": frozenset({"--porcelain"}),
    "diff": frozenset({"--numstat"}),
    "log": frozenset({"--oneline", "--max-count=1"}),
    "ls-files": frozenset({"--cached"}),
    "show-ref": frozenset({"--verify", "--quiet"}),
    "cat-file": frozenset({"-e", "-t"}),
    "pytest": frozenset({"--collect-only", "-q", "--no-header", "-p", "no:cacheprovider", "--co"}),
}

#: The separator after which a token is an *operand* rather than a flag.  Everything
#: after it must not start with ``-``: that is what stops an operand from turning
#: into a flag (``--junitxml=out.xml`` looks exactly like a test target otherwise).
OPERAND_SEPARATOR = "--"

DEFAULT_TIMEOUT_SECONDS = 20.0

#: The whole of one ``observe()`` call, not one command.  An observer that runs two
#: or three commands must still answer inside a bound (§6.6: an observation that
#: never returns is not an observation), so the budget is per observer and each
#: command gets whatever is left of it.
DEFAULT_OBSERVER_BUDGET_SECONDS = 60.0

#: How much of a worktree this module will copy to get a read-only place to import
#: test modules in.  A repository bigger than this answers OBSERVER_UNAVAILABLE
#: rather than being collected in place, because importing a test module runs module
#: level code and that code may write.
DEFAULT_MAX_COPY_BYTES = 256 * 1024 * 1024

#: Never copied into the read-only snapshot: git's own state, caches, and the two
#: directories that make a copy pointlessly enormous.
COPY_EXCLUDES: tuple[str, ...] = (
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
)

#: Above this many changed lines a changeset is "too large for one review pass".
#: A deployment convention, versioned with :data:`OBSERVER_VERSION`, not a law.
DEFAULT_CHANGESET_LINE_LIMIT = 400


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What a read-only command answered, with its two failure modes kept apart."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    #: The executable is not on this machine.  Not a fact about the repository.
    missing: bool = False

    @property
    def usable(self) -> bool:
        return not (self.timed_out or self.missing)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(line for line in self.stdout.splitlines() if line.strip())


Runner = Callable[[Sequence[str], Path, float], CommandResult]


def run_read_only(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
    """Run one command, capture it, and never let it wait forever."""

    try:
        completed = subprocess.run(  # noqa: S603 - argv is allowlisted by _Command.run
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(exit_code=-1, missing=True)
    except subprocess.TimeoutExpired:
        return CommandResult(exit_code=-1, timed_out=True)
    except OSError as error:  # pragma: no cover - platform dependent
        return CommandResult(exit_code=-1, stderr=str(error), missing=True)
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def operand(value: object, name: str) -> str:
    """One caller-supplied command **operand**, or a refusal.

    An operand is a path, a ref or a test target.  It may not start with ``-``:
    ``--junitxml=out.xml`` is a perfectly good-looking test target and a file write,
    and the P2.3c review demonstrated exactly that.  It also may not be blank, carry
    a NUL, or contain a newline — a shell is never involved here, but a newline in a
    ref makes an argument list unreadable in a receipt.
    """

    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise ContractError(f"{name} names nothing to read")
    if text.startswith("-"):
        raise ContractError(
            f"{name} {value!r} starts with '-'; an observer takes operands, never flags — "
            "a flag is how a read-only command is made to write (§6.6)"
        )
    if "\0" in text or "\n" in text or "\r" in text:
        raise ContractError(f"{name} {value!r} contains a control character")
    return text


@dataclass(frozen=True, slots=True)
class CodeObserverConfig:
    """Where to look, for how long, and with which reader."""

    root: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    runner: Runner = run_read_only
    #: ``pytest --collect-only`` tells us whether a target *exists*; whether it fails
    #: needs the target to run.  Collection *imports* the test module, which runs its
    #: module-level code, so even collection happens in a throwaway copy — and
    #: running the tests themselves is opt-in and off by default: without it
    #: ``code.test-is-failing`` answers OBSERVER_UNAVAILABLE rather than guessing.
    allow_test_execution: bool = False
    changeset_line_limit: int = DEFAULT_CHANGESET_LINE_LIMIT
    python_executable: str = field(default_factory=lambda: sys.executable)
    #: The budget for one whole ``observe()`` call.  See
    #: :data:`DEFAULT_OBSERVER_BUDGET_SECONDS`.
    budget_seconds: float = DEFAULT_OBSERVER_BUDGET_SECONDS
    max_copy_bytes: int = DEFAULT_MAX_COPY_BYTES
    #: Set by :meth:`SuiteObserver._read` while it holds a throwaway copy, so the
    #: commands of that one observation run there instead of in the real worktree.
    workspace: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if float(self.timeout_seconds) <= 0:
            raise ContractError("an observer command needs a positive timeout")
        if float(self.budget_seconds) <= 0:
            raise ContractError("an observer needs a positive budget")
        if int(self.changeset_line_limit) < 1:
            raise ContractError("changeset_line_limit must be at least 1")
        if int(self.max_copy_bytes) < 1:
            raise ContractError("max_copy_bytes must be at least 1")
        if self.workspace is not None:
            object.__setattr__(self, "workspace", Path(self.workspace))

    @property
    def working_root(self) -> Path:
        """Where commands actually run: the throwaway copy if there is one."""

        return self.root if self.workspace is None else self.workspace


class BudgetExhausted(RuntimeError):
    """The whole-observation budget ran out before this command could start."""


class _Budget:
    """One observation's wall-clock budget, spent across however many commands."""

    def __init__(self, seconds: float) -> None:
        self._deadline = time.monotonic() + float(seconds)

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    def slice(self, per_command: float) -> float:
        left = self.remaining()
        if left <= 0:
            raise BudgetExhausted("the observation budget is spent")
        return min(float(per_command), left)


class _Command:
    """The allowlist gate.  Nothing in this module spawns a process around it.

    Two halves, and the review found the second one missing:

    1. the **sub-command** must be one of the reading ones;
    2. every token before ``--`` must be one of :data:`TRUSTED_ARGUMENTS` for that
       sub-command, and every token after it must be an operand rather than a flag.

    Without (2) the gate is decoration: ``git diff --numstat --output=x`` and
    ``pytest --collect-only --junitxml=x`` both pass a sub-command check and both
    write a file.
    """

    def __init__(self, config: CodeObserverConfig, budget: _Budget | None = None) -> None:
        self._config = config
        self._budget = budget or _Budget(config.budget_seconds)

    @property
    def root(self) -> Path:
        return self._config.working_root

    @property
    def budget(self) -> _Budget:
        return self._budget

    def run(self, argv: Sequence[str]) -> CommandResult:
        items = [str(item) for item in argv]
        if not items:
            raise ContractError("an observer command needs an executable")
        program = Path(items[0]).name
        rest = items[1:]
        if program == "git":
            allowed, arguments = READ_ONLY_GIT, rest
        elif items[0] == self._config.python_executable and rest[:1] == ["-m"]:
            allowed, arguments = READ_ONLY_PYTHON, rest[1:]
        else:
            raise ContractError(
                f"observer command {items[0]!r} is not one of the read-only programs this "
                "module may run; an observer reads the world and does not change it (§6.6)"
            )
        subcommand = arguments[0] if arguments else ""
        if subcommand not in allowed:
            raise ContractError(
                f"{program} {subcommand!r} is not a read-only sub-command; the allowlist is "
                f"{sorted(allowed)} and a mutating command is refused before it is spawned"
            )
        self._check_arguments(program, subcommand, arguments[1:])
        if not self.root.is_dir():
            return CommandResult(exit_code=-1, missing=True, stderr=f"{self.root} is not a dir")
        try:
            timeout = self._budget.slice(self._config.timeout_seconds)
        except BudgetExhausted:
            return CommandResult(exit_code=-1, timed_out=True, stderr="observer budget spent")
        return self._config.runner(items, self.root, timeout)

    @staticmethod
    def _check_arguments(program: str, subcommand: str, tail: Sequence[str]) -> None:
        trusted = TRUSTED_ARGUMENTS.get(subcommand, frozenset())
        seen_separator = False
        for token in tail:
            if token == OPERAND_SEPARATOR:
                if seen_separator:
                    raise ContractError(
                        f"{program} {subcommand}: '{OPERAND_SEPARATOR}' appears twice"
                    )
                seen_separator = True
                continue
            if seen_separator:
                if token.startswith("-"):
                    raise ContractError(
                        f"{program} {subcommand}: operand {token!r} starts with '-'; after "
                        f"'{OPERAND_SEPARATOR}' a token is a path or a ref, never a flag"
                    )
                continue
            if token not in trusted:
                raise ContractError(
                    f"{program} {subcommand}: argument {token!r} is not one of this "
                    f"sub-command's trusted arguments {sorted(trusted)}; an observer never "
                    "takes a flag from a caller, because a flag is how a reading command is "
                    "made to write (§6.6)"
                )

    def git(self, subcommand: str, *flags: str, operands: Sequence[str] = ()) -> CommandResult:
        return self.run(["git", subcommand, *flags, *_operands(operands)])

    def pytest(self, *flags: str, operands: Sequence[str] = ()) -> CommandResult:
        return self.run(
            [self._config.python_executable, "-m", "pytest", *flags, *_operands(operands)]
        )


def _operands(operands: Sequence[str]) -> list[str]:
    if not operands:
        return []
    return [OPERAND_SEPARATOR, *(operand(item, "operand") for item in operands)]


def _directory_bytes(root: Path, limit: int) -> int | None:
    """Total size of the files that would be copied, or ``None`` past ``limit``."""

    total = 0
    excluded = set(COPY_EXCLUDES)
    for path in root.rglob("*"):
        if any(part in excluded for part in path.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        try:
            total += path.stat().st_size
        except OSError:  # pragma: no cover - a file that vanished mid-walk
            continue
        if total > limit:
            return None
    return total


@contextmanager
def read_only_copy(root: Path, *, limit: int) -> Iterator[Path | None]:
    """A throwaway copy of ``root`` to import code in, or ``None`` if it is too big.

    Collecting a test target imports its module, and a module's top level may write
    files.  An observer may not, so collection never happens in the real worktree:
    it happens here, and the copy is deleted whatever happens.  ``.git`` is excluded
    both because it is large and because a copy of it is not a repository an observer
    should be able to touch at all.
    """

    if _directory_bytes(root, limit) is None:
        yield None
        return
    target = Path(tempfile.mkdtemp(prefix="sh-observer-"))
    try:
        shutil.copytree(
            root,
            target / "workspace",
            ignore=shutil.ignore_patterns(*COPY_EXCLUDES),
            symlinks=False,
            ignore_dangling_symlinks=True,
        )
        yield target / "workspace"
    finally:
        shutil.rmtree(target, ignore_errors=True)


class _CodeObserver:
    """Shared plumbing: the id, the predicates, and the dispatch to one reader."""

    observer_name = ""
    predicates: tuple[str, ...] = ()

    def __init__(self, config: CodeObserverConfig) -> None:
        self._config = config
        #: Replaced per ``observe()`` call, so one observation's budget is its own.
        self._command = _Command(config)

    @property
    def observer_id(self) -> str:
        return self.observer_name

    @property
    def root(self) -> Path:
        return self._config.root

    def predicate_ids(self) -> tuple[str, ...]:
        return self.predicates

    def observe(
        self,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        predicate = signature.predicate_ref.id
        if predicate not in self.predicates:
            raise ContractError(
                f"{self.observer_id} does not observe {predicate!r}; it reads "
                f"{sorted(self.predicates)}"
            )
        # One budget per observation (§6.6: an observation that never returns is not
        # an observation).  Every command of this call spends from it, so an observer
        # that runs three commands still answers inside the bound.
        self._command = _Command(self._config, _Budget(self._config.budget_seconds))
        try:
            return self._read(predicate, signature, arguments, now_ms=now_ms)
        except BudgetExhausted:
            return unavailable(
                self.observer_id,
                predicate,
                f"the observation budget of {self._config.budget_seconds}s was spent before "
                "this observer could answer",
            )

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:  # pragma: no cover - every subclass overrides it
        raise NotImplementedError

    def _unusable(self, predicate: str, result: CommandResult, what: str) -> Observation:
        if result.timed_out:
            why = "timed out"
        elif result.missing:
            why = "could not be run on this machine"
        else:
            why = f"exited {result.exit_code}"
        return unavailable(
            self.observer_id,
            predicate,
            f"{what} {why}; not being able to look is not the proposition being false",
        )


class RepoObserver(_CodeObserver):
    """Is there a usable checkout here, and is it clean?

    ``code.working-tree-clean`` is CLOSED, so a non-empty ``git status --porcelain``
    is recorded as an *authoritative negative* over the worktree scope: the command
    enumerates the whole tree, which is exactly the completeness claim §6.6 C28 asks
    for before a closed domain may say FALSE.
    """

    observer_name = "code.repo-observer"
    predicates: tuple[str, ...] = ("code.repo-checked-out", "code.working-tree-clean")

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        if predicate == "code.repo-checked-out":
            return self._checked_out(signature, arguments, now_ms=now_ms)
        return self._clean(signature, arguments, now_ms=now_ms)

    def _checked_out(
        self, signature: PredicateSignature, arguments: Mapping[str, Any], *, now_ms: int
    ) -> Observation:
        result = self._command.git("rev-parse", "--is-inside-work-tree")
        if result.missing and not self.root.is_dir():
            # The workspace is not there at all, which *is* the answer to "is this
            # repository checked out in a usable workspace".
            return observed(
                signature,
                arguments,
                polarity=False,
                observer_id=self.observer_id,
                now_ms=now_ms,
                detail=f"{self.root} does not exist",
                observer_version=OBSERVER_VERSION,
            )
        if not result.usable:
            return self._unusable("code.repo-checked-out", result, "git rev-parse")
        inside = result.exit_code == 0 and result.stdout.strip() == "true"
        return observed(
            signature,
            arguments,
            polarity=inside,
            observer_id=self.observer_id,
            now_ms=now_ms,
            detail=f"git rev-parse --is-inside-work-tree said {result.stdout.strip()!r}",
            observer_version=OBSERVER_VERSION,
        )

    def _clean(
        self, signature: PredicateSignature, arguments: Mapping[str, Any], *, now_ms: int
    ) -> Observation:
        result = self._command.git("status", "--porcelain")
        if not result.usable or result.exit_code != 0:
            return self._unusable("code.working-tree-clean", result, "git status --porcelain")
        if not result.lines:
            return observed(
                signature,
                arguments,
                polarity=True,
                observer_id=self.observer_id,
                now_ms=now_ms,
                detail="git status --porcelain listed nothing",
                observer_version=OBSERVER_VERSION,
            )
        return denial(
            signature,
            arguments,
            observer_id=self.observer_id,
            now_ms=now_ms,
            coverage_scope=f"worktree:{self.root}",
            detail=f"{len(result.lines)} path(s) are modified",
            observer_version=OBSERVER_VERSION,
        )


class WorkspaceObserver(RepoObserver):
    """The second observer ``code.working-tree-clean`` lists (§6.6 C28).

    A CLOSED predicate needs at least one authoritative observer and may have
    several; keeping a second identity means a deployment that separates "the git
    checkout" from "the agent's workspace" can attribute the denial to the right one
    instead of borrowing the other's name.
    """

    observer_name = "code.workspace-observer"
    predicates: tuple[str, ...] = ("code.working-tree-clean",)


class HistoryObserver(_CodeObserver):
    """Has the commit that introduced the regression been identified?

    Read-only: a finished ``git bisect`` leaves ``refs/bisect/bad`` behind, so asking
    whether that ref resolves answers the predicate without starting, advancing or
    resetting a bisect.
    """

    observer_name = "code.history-observer"
    predicates: tuple[str, ...] = ("code.regression-commit-known",)

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        result = self._command.git(
            "rev-parse", "--verify", "--quiet", operands=("refs/bisect/bad",)
        )
        if not result.usable:
            return self._unusable(predicate, result, "git rev-parse refs/bisect/bad")
        found = result.exit_code == 0 and bool(result.stdout.strip())
        return observed(
            signature,
            arguments,
            polarity=found,
            observer_id=self.observer_id,
            now_ms=now_ms,
            detail=(
                f"refs/bisect/bad resolves to {result.stdout.strip()[:12]}"
                if found
                else "refs/bisect/bad does not resolve"
            ),
            observer_version=OBSERVER_VERSION,
        )


class ChangesetObserver(_CodeObserver):
    """How big is this changeset — too large to review, or reviewable in one pass?

    One reader answers both predicates from one ``git diff --numstat``, which is why
    they cannot disagree with each other: a changeset the observer calls reviewable
    is exactly one it does not call too large.
    """

    observer_name = "code.changeset-observer"
    predicates: tuple[str, ...] = (
        "code.changeset-too-large",
        "code.changeset-reviewable",
    )

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        try:
            changeset = operand(arguments.get("changeset", ""), "the changeset argument")
        except ContractError as refused:
            return unavailable(self.observer_id, predicate, str(refused))
        result = self._command.git("diff", "--numstat", operands=(changeset,))
        if not result.usable or result.exit_code != 0:
            return self._unusable(predicate, result, f"git diff --numstat {changeset}")
        touched = _numstat_lines(result.lines)
        if touched is None:
            return unavailable(
                self.observer_id,
                predicate,
                "git diff --numstat produced output this observer cannot read",
            )
        too_large = touched > int(self._config.changeset_line_limit)
        polarity = too_large if predicate == "code.changeset-too-large" else not too_large
        return observed(
            signature,
            arguments,
            polarity=polarity,
            observer_id=self.observer_id,
            now_ms=now_ms,
            detail=(
                f"{touched} changed line(s) against a limit of "
                f"{int(self._config.changeset_line_limit)}"
            ),
            observer_version=OBSERVER_VERSION,
        )


def _numstat_lines(lines: Sequence[str]) -> int | None:
    """Total added + removed lines, or ``None`` when the output is not numstat.

    A binary file's numstat row is ``-\t-\tpath``; it contributes no line count and
    is not a parse failure, so it is skipped rather than turned into UNKNOWN.
    """

    total = 0
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 3:
            return None
        added, removed = parts[0], parts[1]
        if added == "-" and removed == "-":
            continue
        try:
            total += int(added) + int(removed)
        except ValueError:
            return None
    return total


class SuiteObserver(_CodeObserver):
    """Is the named test currently failing?

    Collection alone cannot answer this.  ``pytest --collect-only`` says whether the
    target *exists*, and a target nobody can collect makes the question unanswerable
    — which is ``OBSERVER_UNAVAILABLE``, not "the test passes".  Deciding that a test
    fails needs the test to run, which executes repository code, so it is behind
    :attr:`CodeObserverConfig.allow_test_execution` and off by default; the run is
    still non-mutating (``-p no:cacheprovider``, no ``--last-failed`` state).
    """

    observer_name = "code.test-observer"
    predicates: tuple[str, ...] = ("code.test-is-failing",)

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        try:
            target = operand(arguments.get("test", ""), "the test argument")
        except ContractError as refused:
            return unavailable(self.observer_id, predicate, str(refused))
        with read_only_copy(self._config.root, limit=self._config.max_copy_bytes) as workspace:
            if workspace is None:
                return unavailable(
                    self.observer_id,
                    predicate,
                    f"the worktree is larger than {self._config.max_copy_bytes} bytes, so there "
                    "is no throwaway copy to import test modules in; collecting in place would "
                    "run module-level code against the real workspace",
                )
            command = _Command(replace(self._config, workspace=workspace), self._command.budget)
            return self._read_in(command, predicate, signature, arguments, target, now_ms=now_ms)

    def _read_in(
        self,
        command: _Command,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        target: str,
        *,
        now_ms: int,
    ) -> Observation:
        collected = command.pytest(
            "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider", operands=(target,)
        )
        if not collected.usable:
            return self._unusable(predicate, collected, "pytest --collect-only")
        if collected.exit_code != 0:
            return unavailable(
                self.observer_id,
                predicate,
                f"pytest could not collect {target!r} (exit {collected.exit_code}); whether it "
                "fails is unknown, which is not the same as it passing",
            )
        if not self._config.allow_test_execution:
            return unavailable(
                self.observer_id,
                predicate,
                f"{target!r} is collectable, but deciding whether it fails needs it to run and "
                "test execution is not enabled for this observer",
            )
        ran = command.pytest("-q", "--no-header", "-p", "no:cacheprovider", operands=(target,))
        if not ran.usable or ran.exit_code not in (0, 1):
            return self._unusable(predicate, ran, "pytest")
        return observed(
            signature,
            arguments,
            polarity=ran.exit_code == 1,
            observer_id=self.observer_id,
            now_ms=now_ms,
            detail=f"pytest exited {ran.exit_code} for {target!r}",
            observer_version=OBSERVER_VERSION,
        )


def code_observers(
    root: Path | str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runner: Runner = run_read_only,
    allow_test_execution: bool = False,
    changeset_line_limit: int = DEFAULT_CHANGESET_LINE_LIMIT,
    budget_seconds: float = DEFAULT_OBSERVER_BUDGET_SECONDS,
    max_copy_bytes: int = DEFAULT_MAX_COPY_BYTES,
) -> tuple[_CodeObserver, ...]:
    """The five ``code`` observers, all pointed at one isolated worktree."""

    config = CodeObserverConfig(
        root=Path(root),
        timeout_seconds=timeout_seconds,
        runner=runner,
        allow_test_execution=allow_test_execution,
        changeset_line_limit=changeset_line_limit,
        budget_seconds=budget_seconds,
        max_copy_bytes=max_copy_bytes,
    )
    return (
        RepoObserver(config),
        WorkspaceObserver(config),
        HistoryObserver(config),
        ChangesetObserver(config),
        SuiteObserver(config),
    )


#: Predicate id → the observer ids that can read it.  Asserted against the seed
#: domain's ``predicates.json`` by the suite, so an observer cannot drift away from
#: the declaration that lists it.
CODE_OBSERVER_COVERAGE: Mapping[str, tuple[str, ...]] = {
    "code.repo-checked-out": ("code.repo-observer",),
    "code.working-tree-clean": ("code.repo-observer", "code.workspace-observer"),
    "code.regression-commit-known": ("code.history-observer",),
    "code.changeset-too-large": ("code.changeset-observer",),
    "code.changeset-reviewable": ("code.changeset-observer",),
    "code.test-is-failing": ("code.test-observer",),
}


__all__ = (
    "CODE_OBSERVER_COVERAGE",
    "COPY_EXCLUDES",
    "DEFAULT_CHANGESET_LINE_LIMIT",
    "DEFAULT_MAX_COPY_BYTES",
    "DEFAULT_OBSERVER_BUDGET_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "OBSERVER_VERSION",
    "OPERAND_SEPARATOR",
    "READ_ONLY_GIT",
    "READ_ONLY_PYTHON",
    "TRUSTED_ARGUMENTS",
    "BudgetExhausted",
    "ChangesetObserver",
    "CodeObserverConfig",
    "CommandResult",
    "HistoryObserver",
    "RepoObserver",
    "Runner",
    "SuiteObserver",
    "WorkspaceObserver",
    "code_observers",
    "operand",
    "read_only_copy",
    "run_read_only",
)
