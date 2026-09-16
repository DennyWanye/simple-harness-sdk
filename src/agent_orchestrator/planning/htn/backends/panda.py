# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""PANDA / HDDL backend adapter.

This module is the only place in the runtime that shells out to the PANDA HTN
toolchain. It never writes planning facts: it takes HDDL text in and returns
typed results plus a decomposition witness. HDDL *export* belongs to the
compiler slice; this adapter only consumes text.

Boundaries enforced here (complete-plan v1.4 section 7.4 and annex TG section 12):

* ``SOLVER_UNAVAILABLE`` is explicit. A missing binary is never a PASS and never
  silently degrades to an internal search. There is no hidden PANDA-to-SHOP2
  shortcut: the declared fragment is the fragment we run.
* ``UNSOLVABLE_PROVEN`` is only returned when the engine itself reports that the
  problem was proven unsolvable. Timeouts map to ``TIMEOUT`` and exhausted
  search budgets map to ``SEARCH_LIMIT_REACHED``; neither is evidence that a
  real-world goal is impossible.
* Unsupported HDDL features are refused up front with the offending feature
  names instead of being quietly dropped.
* Every primitive *occurrence* is preserved in the witness. Two identical
  grounded actions reached through two method occurrences stay two entries; a
  shortened execution trace must never masquerade as the original plan.

Toolchain usage
---------------

Binary locations come from explicit constructor arguments or from the
environment variables ``SH_PANDA_PARSER`` / ``SH_PANDA_GROUNDER`` /
``SH_PANDA_ENGINE``. A value containing a path separator is used verbatim; a
bare program name is resolved through ``PATH`` with :func:`shutil.which`. No
other directory is probed.

Command lines, and where each claim comes from. Everything below was read from
the ``master`` branch of the three repositories on 2026-09-16; the upstream
commit ids were not captured, so nothing here cites a file line number. A
re-pin against a fixed commit belongs with the acceptance run that first uses a
real binary, per the "fixed solver version" rule.

* **Translate** (README of pandaPIparser,
  https://github.com/panda-planner-dev/pandaPIparser/blob/master/README.md)::

      pandaPIparser domain.hddl problem.hddl problem.htn

* **Verify** (positional order stated in ``src/options.ggo``,
  https://github.com/panda-planner-dev/pandaPIparser/blob/master/src/options.ggo,
  and in the IPC format note; the README does not mention it)::

      pandaPIparser --verify -C domain.hddl problem.hddl plan.txt

  ``-C``/``--no-colour`` matters: without it the verdict token is wrapped in
  ANSI escapes by ``src/util.cpp``. This adapter passes ``-C`` *and* strips
  ANSI sequences before matching, because the flag is source-derived rather
  than documented. Only the long form ``--verify`` is used: ``-verify`` would
  be read as the short option ``-v`` with the optional argument ``erify``,
  which is outside its ``0``/``1``/``2`` value set.

  The verdict line is ``Plan verification result: true`` or
  ``... false`` with exit code 0 / 1 respectively. That comes from the
  verification branch of ``src/main.cpp``, not from any published document, so
  the adapter classifies on the marker first and only uses the exit code to
  raise ``TOOL_ERROR`` when no marker is present.

* **Ground** (README of pandaPIgrounder,
  https://github.com/panda-planner-dev/pandaPIgrounder/blob/master/README.md)::

      pandaPIgrounder problem.htn problem.sas

  The grounder never reports unsolvability; a non-zero exit is a tool error.

* **Search** (README of pandaPIengine,
  https://github.com/panda-planner-dev/pandaPIengine/blob/master/README.md)::

      pandaPIengine [--timelimit=SECONDS] problem.sas

  The default progression search prints exactly one of
  ``- Status: Solved`` / ``- Status: Timeout`` /
  ``- Status: Proven unsolvable``
  (the result block in ``src/search/PriorityQueueSearch.h``). **The exit code
  carries no result information** -- ``src/SearchEngine.cpp`` returns 0 for all
  three outcomes --
  so the status line is the only classification source. The SAT / BDD / DP
  back ends do not print those lines at all; this adapter therefore supports
  the default progression search only and reports anything else as
  ``TOOL_ERROR`` rather than guessing.

* **Back-translate** (README of pandaPIengine). The grounder rewrites the
  model, so the engine's raw plan is *not* a valid plan for the original HDDL.
  A solved run is only reported as ``SOLVED`` after::

      pandaPIparser -c -C raw.plan converted.plan

  succeeds. The witness is always built from the converted plan, never from
  the grounded-model trace.

Search budget
-------------

pandaPIengine exposes a wall-clock budget (``-t`` / ``--timelimit``, seconds,
default 1800) and no node limit. ``search_limit`` is therefore that seconds
budget, and the two exhaustion outcomes stay distinguishable:

* the engine hit its own configured budget and said ``- Status: Timeout``
  -> ``SEARCH_LIMIT_REACHED``;
* this adapter killed the process at ``timeout_s`` -> ``TIMEOUT``.

Neither is ever ``UNSOLVABLE_PROVEN``.

Plan file format
----------------

http://ipc2020.hierarchical-task.net/data/format.pdf, linked from
https://ipc2020.hierarchical-task.net/benchmarks/output-format; IPC 2023 reuses
it. The parser searches for ``==>``; everything before it is ignored. Then come
primitive actions in execution order, a ``root`` line, then decompositions, then
an optional ``<==``::

    ==>
    0 pickup a
    1 putdown a
    root 2
    2 achieve-goal arg -> m-achieve 0 1
    <==

Lines before ``root`` are primitive actions (``<id> <name> <args...>``); lines
after it are decompositions (``<id> <task> <args...> -> <method> <child-ids>``).
Decomposition lines need not be sorted by id and the closing ``<==`` is
optional. Blank lines and ``;`` comment lines are ignored, which is a superset
of the published grammar and is noted here rather than hidden.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "Availability",
    "DecompositionStep",
    "DecompositionWitness",
    "DEFAULT_FRAGMENT",
    "ENV_ENGINE",
    "ENV_GROUNDER",
    "ENV_PARSER",
    "FragmentReport",
    "PandaPlanFormatError",
    "PandaToolchain",
    "PrimitiveOccurrence",
    "ProcessTrace",
    "SolveResult",
    "SolveStatus",
    "SupportedFragment",
    "VerificationResult",
    "VerificationStatus",
    "check_fragment",
    "parse_plan",
]

ENV_PARSER = "SH_PANDA_PARSER"
ENV_GROUNDER = "SH_PANDA_GROUNDER"
ENV_ENGINE = "SH_PANDA_ENGINE"

TOOL_PARSER = "parser"
TOOL_GROUNDER = "grounder"
TOOL_ENGINE = "engine"

#: Captured stream bytes kept inline. Everything else is represented by its hash.
EXCERPT_LIMIT_BYTES = 4096

#: Bound on the best-effort ``--version`` probe used for toolchain identity.
VERSION_PROBE_TIMEOUT_S = 5.0

_UNKNOWN_VERSION = "unknown"

# --- stdout classification markers -------------------------------------------
# Exact literals emitted by the upstream binaries, lower-cased for matching.
# Each is sourced from the upstream C++ rather than from a published contract,
# so the adapter never invents near-miss variants: unrecognised output becomes
# TOOL_ERROR instead of a guess.

#: pandaPIparser src/main.cpp, verification verdict.
_VERIFY_TRUE_MARKER = "plan verification result: true"
_VERIFY_FALSE_MARKER = "plan verification result: false"

#: pandaPIengine src/search/PriorityQueueSearch.h, mutually exclusive outcomes.
_ENGINE_SOLVED_MARKER = "- status: solved"
_ENGINE_TIMEOUT_MARKER = "- status: timeout"
_ENGINE_UNSOLVABLE_MARKER = "- status: proven unsolvable"

#: Printed when the engine aborts search at its own --timelimit budget.
_ENGINE_LIMIT_MARKERS: tuple[str, ...] = (
    _ENGINE_TIMEOUT_MARKER,
    "reached time limit - stopping search",
)

#: ANSI CSI sequences. pandaPIparser colours its verdict unless -C is given.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_PLAN_OPEN = "==>"
_PLAN_CLOSE = "<=="


class VerificationStatus(StrEnum):
    """Outcome of an external plan verification run."""

    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    SOLVER_UNAVAILABLE = "SOLVER_UNAVAILABLE"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    TIMEOUT = "TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"


class SolveStatus(StrEnum):
    """Outcome of an external solve run."""

    SOLVED = "SOLVED"
    UNSOLVABLE_PROVEN = "UNSOLVABLE_PROVEN"
    SEARCH_LIMIT_REACHED = "SEARCH_LIMIT_REACHED"
    TIMEOUT = "TIMEOUT"
    SOLVER_UNAVAILABLE = "SOLVER_UNAVAILABLE"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    TOOL_ERROR = "TOOL_ERROR"


class PandaPlanFormatError(ValueError):
    """The plan text is not a well-formed IPC hierarchical plan."""


# --- plan / witness ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrimitiveOccurrence:
    """One position of a grounded primitive action inside a plan.

    ``position`` is the index in the emitted action sequence. Two entries with an
    identical :attr:`signature` are two distinct occurrences and are never merged.
    """

    position: int
    plan_id: str
    name: str
    arguments: tuple[str, ...]

    @property
    def signature(self) -> str:
        """Grounded action text, identical for repeated occurrences."""
        return " ".join((self.name, *self.arguments))


@dataclass(frozen=True, slots=True)
class DecompositionStep:
    """One method application: an abstract task occurrence and its children."""

    plan_id: str
    task_name: str
    task_arguments: tuple[str, ...]
    method_name: str
    children: tuple[str, ...]

    @property
    def task_signature(self) -> str:
        """Grounded abstract task text."""
        return " ".join((self.task_name, *self.task_arguments))


@dataclass(frozen=True, slots=True)
class DecompositionWitness:
    """Primitive sequence plus the decomposition tree that produced it."""

    primitives: tuple[PrimitiveOccurrence, ...]
    decompositions: tuple[DecompositionStep, ...]
    roots: tuple[str, ...]
    plan_sha256: str

    def occurrences_of(self, signature: str) -> tuple[PrimitiveOccurrence, ...]:
        """Every occurrence of one grounded action, in plan order."""
        return tuple(item for item in self.primitives if item.signature == signature)

    def parent_of(self, plan_id: str) -> DecompositionStep | None:
        """The method application that introduced ``plan_id``, if any."""
        for step in self.decompositions:
            if plan_id in step.children:
                return step
        return None

    def occurrence_parents(
        self,
    ) -> tuple[tuple[PrimitiveOccurrence, DecompositionStep | None], ...]:
        """Occurrence-to-method mapping, one entry per occurrence."""
        return tuple((item, self.parent_of(item.plan_id)) for item in self.primitives)


def _strip_plan_lines(plan_text: str) -> list[str]:
    lines: list[str] = []
    for raw in plan_text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def parse_plan(plan_text: str, *, require_root: bool = True) -> DecompositionWitness:
    """Parse an IPC 2020/2023 hierarchical plan into a decomposition witness.

    Raises :class:`PandaPlanFormatError` when the structure is unusable. The
    parser deliberately refuses to repair anything: a witness that cannot be
    rebuilt from the plan text is not a witness.

    ``require_root`` is on by default because the published grammar makes the
    ``root`` line mandatory and a solve result without one is not a
    hierarchical plan. Only plan verification, where the caller hands us text
    it did not produce, may switch it off explicitly.

    ``PandaPlanFormatError`` stays a plain :class:`ValueError` rather than
    joining the ``ContractError`` family: it reports malformed bytes from a
    third-party process, not a violated internal contract, and this module is
    deliberately free of contract imports so the backend can be used before
    the HTN contracts land.
    """
    lines = _strip_plan_lines(plan_text)
    if _PLAN_OPEN in lines:
        lines = lines[lines.index(_PLAN_OPEN) + 1 :]
    if _PLAN_CLOSE in lines:
        lines = lines[: lines.index(_PLAN_CLOSE)]
    if not lines:
        raise PandaPlanFormatError("empty plan body")

    primitives: list[PrimitiveOccurrence] = []
    decompositions: list[DecompositionStep] = []
    roots: tuple[str, ...] = ()
    seen_ids: set[str] = set()
    position = 0

    for line in lines:
        if line.startswith("root"):
            tokens = line.split()
            if len(tokens) < 2:
                raise PandaPlanFormatError("root line without any root task id")
            roots = tuple(tokens[1:])
            continue
        if "->" in line:
            head, _, tail = line.partition("->")
            head_tokens = head.split()
            tail_tokens = tail.split()
            if len(head_tokens) < 2 or not tail_tokens:
                raise PandaPlanFormatError(f"malformed decomposition line: {line!r}")
            plan_id = head_tokens[0]
            if plan_id in seen_ids:
                raise PandaPlanFormatError(f"duplicate plan id: {plan_id!r}")
            seen_ids.add(plan_id)
            decompositions.append(
                DecompositionStep(
                    plan_id=plan_id,
                    task_name=head_tokens[1],
                    task_arguments=tuple(head_tokens[2:]),
                    method_name=tail_tokens[0],
                    children=tuple(tail_tokens[1:]),
                )
            )
            continue
        tokens = line.split()
        if len(tokens) < 2:
            raise PandaPlanFormatError(f"malformed primitive line: {line!r}")
        plan_id = tokens[0]
        if plan_id in seen_ids:
            raise PandaPlanFormatError(f"duplicate plan id: {plan_id!r}")
        seen_ids.add(plan_id)
        primitives.append(
            PrimitiveOccurrence(
                position=position,
                plan_id=plan_id,
                name=tokens[1],
                arguments=tuple(tokens[2:]),
            )
        )
        position += 1

    for step in decompositions:
        for child in step.children:
            if child not in seen_ids:
                raise PandaPlanFormatError(
                    f"decomposition {step.plan_id!r} references unknown child {child!r}"
                )
    if require_root and not roots:
        raise PandaPlanFormatError("plan has no root line")
    for root in roots:
        if root not in seen_ids:
            raise PandaPlanFormatError(f"root {root!r} is not a task in the plan")

    return DecompositionWitness(
        primitives=tuple(primitives),
        decompositions=tuple(decompositions),
        roots=roots,
        plan_sha256=hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
    )


# --- supported fragment ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FragmentReport:
    """Result of the conservative HDDL fragment check."""

    supported: bool
    declared_requirements: tuple[str, ...]
    unsupported_features: tuple[str, ...]
    detection_notes: tuple[str, ...]


_REQUIREMENT_RE = re.compile(r":[a-zA-Z][a-zA-Z0-9-]*")

#: Lexical probes run on comment-stripped text. Each entry is
#: ``(needle, feature-name)``; ``needle`` is matched case-insensitively as a
#: plain substring. Comparison operators are deliberately absent because ``(<``
#: is also the HDDL subtask ordering operator.
_FEATURE_PROBES: tuple[tuple[str, str], ...] = (
    ("(:functions", "numeric-fluents"),
    ("(increase ", "numeric-fluents"),
    ("(decrease ", "numeric-fluents"),
    ("(scale-up ", "numeric-fluents"),
    ("(scale-down ", "numeric-fluents"),
    ("(assign ", "numeric-fluents"),
    ("(:durative-action", "durative-actions"),
    (":duration", "durative-actions"),
    ("(at start", "durative-actions"),
    ("(at end", "durative-actions"),
    ("(over all", "durative-actions"),
    ("(when ", "conditional-effects"),
    ("(when(", "conditional-effects"),
    ("(forall ", "universal-quantification"),
    ("(forall(", "universal-quantification"),
    ("(exists ", "existential-quantification"),
    ("(exists(", "existential-quantification"),
    ("(:derived", "derived-predicates"),
    ("(preference ", "preferences"),
    ("(:constraints", "preferences"),
    ("(probabilistic ", "probabilistic-effects"),
    ("(total-cost", "action-costs"),
)


@dataclass(frozen=True, slots=True)
class SupportedFragment:
    """The HDDL subset this adapter claims to support.

    Detection is conservative and purely lexical, on comment-stripped text:

    * every ``:requirement`` flag outside :attr:`requirements` is reported;
    * a fixed table of syntactic probes reports features that a domain may use
      without declaring the matching flag.

    Known limits of that boundary, stated rather than hidden: a predicate, task
    or method literally named ``when``/``forall``/``exists`` is reported as
    unsupported (false positive, refusing rather than guessing), and a feature
    that is neither declared nor expressed with these tokens is not detected.
    The check is a gate, not a parser; the external parser remains authoritative.
    """

    name: str
    requirements: frozenset[str]
    probes: tuple[tuple[str, str], ...]

    def check(self, domain_text: str, problem_text: str) -> FragmentReport:
        """Report unsupported features found in the domain/problem pair."""
        combined = _strip_hddl_comments(domain_text) + "\n" + _strip_hddl_comments(problem_text)
        lowered = combined.lower()

        declared: list[str] = []
        unsupported: dict[str, None] = {}
        notes: list[str] = []

        for block in _requirement_blocks(lowered):
            for flag in _REQUIREMENT_RE.findall(block):
                if flag in declared:
                    continue
                declared.append(flag)
                if flag not in self.requirements:
                    feature = flag.lstrip(":")
                    unsupported.setdefault(feature, None)
                    notes.append(f"declared requirement {flag} is outside fragment {self.name!r}")

        for needle, feature in self.probes:
            if needle in lowered:
                if feature not in unsupported:
                    unsupported.setdefault(feature, None)
                notes.append(f"lexical probe {needle!r} matched -> {feature}")

        return FragmentReport(
            supported=not unsupported,
            declared_requirements=tuple(declared),
            unsupported_features=tuple(sorted(unsupported)),
            detection_notes=tuple(notes),
        )


def _strip_hddl_comments(text: str) -> str:
    return "\n".join(line.split(";", 1)[0] for line in text.splitlines())


def _requirement_blocks(lowered: str) -> list[str]:
    blocks: list[str] = []
    cursor = 0
    while True:
        start = lowered.find("(:requirements", cursor)
        if start < 0:
            return blocks
        end = lowered.find(")", start)
        if end < 0:
            blocks.append(lowered[start:])
            return blocks
        blocks.append(lowered[start + len("(:requirements") : end])
        cursor = end + 1


#: Total-order and partial-order methods, typed objects, STRIPS preconditions
#: and effects. Nothing numeric, temporal, conditional or quantified.
DEFAULT_FRAGMENT = SupportedFragment(
    name="strips-typed-hierarchy",
    requirements=frozenset(
        {
            ":strips",
            ":typing",
            ":hierarchy",
            ":htn",
            ":negative-preconditions",
            ":equality",
            ":method-preconditions",
            ":htn-method-prec",
        }
    ),
    probes=_FEATURE_PROBES,
)


def check_fragment(
    domain_text: str,
    problem_text: str,
    *,
    fragment: SupportedFragment = DEFAULT_FRAGMENT,
) -> FragmentReport:
    """Check an HDDL domain/problem pair against the supported fragment."""
    return fragment.check(domain_text, problem_text)


# --- toolchain ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Availability:
    """Which PANDA binaries are usable, and exactly which bytes they are."""

    available: bool
    missing: tuple[str, ...]
    versions: Mapping[str, str]
    digests: Mapping[str, str]
    paths: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ProcessTrace:
    """Bounded, hashed record of one external process invocation.

    The excerpts are a diagnostic aid, not the classification input: they stop
    at :data:`EXCERPT_LIMIT_BYTES`, while the hashes cover the whole stream.
    Status classification and plan extraction always read the complete output.
    """

    tool: str
    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    elapsed_s: float
    stdout_sha256: str
    stderr_sha256: str
    stdout_excerpt: str
    stderr_excerpt: str
    launch_error: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of :meth:`PandaToolchain.verify_plan`."""

    status: VerificationStatus
    witness: DecompositionWitness | None
    stdout_sha256: str
    stderr_sha256: str
    elapsed_s: float
    toolchain: Availability
    traces: tuple[ProcessTrace, ...]
    unsupported_features: tuple[str, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class SolveResult:
    """Outcome of :meth:`PandaToolchain.solve`."""

    status: SolveStatus
    plan_text: str | None
    witness: DecompositionWitness | None
    stdout_sha256: str
    stderr_sha256: str
    elapsed_s: float
    toolchain: Availability
    traces: tuple[ProcessTrace, ...]
    unsupported_features: tuple[str, ...]
    detail: str


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _excerpt(payload: bytes) -> str:
    return payload[:EXCERPT_LIMIT_BYTES].decode("utf-8", errors="replace")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_binary(value: str | os.PathLike[str] | None) -> Path | None:
    """Resolve an explicit path or a PATH-resolvable program name.

    A value containing a path separator must already point at an executable
    file. A bare name goes through ``PATH``. Nothing else is searched. The
    result is always absolute, because every tool runs with its working
    directory set to a fresh temporary directory.
    """
    if value is None:
        return None
    text = os.fspath(value).strip()
    if not text:
        return None
    if os.sep in text or (os.altsep and os.altsep in text):
        candidate = Path(text).expanduser().resolve()
    else:
        found = shutil.which(text)
        if found is None:
            return None
        candidate = Path(found).expanduser().resolve()
    # Absolute from here on: the tools run with cwd set to a temporary
    # directory, so a relative path that resolves today would vanish there.
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def _probe_version(path: Path) -> str:
    """Best-effort version string. Never raises; identity of record is sha256."""
    with tempfile.TemporaryDirectory(prefix="sh-panda-probe-") as raw_dir:
        env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C", "HOME": raw_dir}
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [str(path), "--version"],
                cwd=raw_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=VERSION_PROBE_TIMEOUT_S,
                check=False,
                shell=False,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError):
            return _UNKNOWN_VERSION
    for stream in (completed.stdout, completed.stderr):
        text = stream.decode("utf-8", errors="replace").strip()
        if text:
            return text.splitlines()[0][:200]
    return _UNKNOWN_VERSION


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """One process invocation: the bounded public record plus the full streams.

    The full streams never leave this module. They exist so classification and
    plan extraction read everything the tool printed instead of the excerpt.
    """

    trace: ProcessTrace
    stdout: bytes
    stderr: bytes


def _trace(
    tool: str,
    argv: Sequence[str],
    *,
    exit_code: int | None,
    timed_out: bool,
    elapsed_s: float,
    stdout: bytes,
    stderr: bytes,
    launch_error: str | None = None,
) -> ProcessTrace:
    return ProcessTrace(
        tool=tool,
        argv=tuple(argv),
        exit_code=exit_code,
        timed_out=timed_out,
        elapsed_s=elapsed_s,
        stdout_sha256=_sha256_bytes(stdout),
        stderr_sha256=_sha256_bytes(stderr),
        stdout_excerpt=_excerpt(stdout),
        stderr_excerpt=_excerpt(stderr),
        launch_error=launch_error,
    )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the child's whole session, then the child, tolerating races."""
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass


@dataclass(frozen=True, slots=True)
class PandaToolchain:
    """Adapter around the pandaPI parser / grounder / engine binaries."""

    parser: Path | None = None
    grounder: Path | None = None
    engine: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PandaToolchain:
        """Build a toolchain from ``SH_PANDA_*`` environment variables."""
        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            parser=_resolve_binary(source.get(ENV_PARSER)),
            grounder=_resolve_binary(source.get(ENV_GROUNDER)),
            engine=_resolve_binary(source.get(ENV_ENGINE)),
        )

    @classmethod
    def from_paths(
        cls,
        *,
        parser: str | os.PathLike[str] | None = None,
        grounder: str | os.PathLike[str] | None = None,
        engine: str | os.PathLike[str] | None = None,
    ) -> PandaToolchain:
        """Build a toolchain from explicit paths or PATH-resolvable names."""
        return cls(
            parser=_resolve_binary(parser),
            grounder=_resolve_binary(grounder),
            engine=_resolve_binary(engine),
        )

    def _binary(self, tool: str) -> Path | None:
        if tool == TOOL_PARSER:
            return self.parser
        if tool == TOOL_GROUNDER:
            return self.grounder
        if tool == TOOL_ENGINE:
            return self.engine
        raise ValueError(f"unknown PANDA tool: {tool!r}")

    def availability(
        self,
        *,
        required: Sequence[str] = (TOOL_PARSER, TOOL_GROUNDER, TOOL_ENGINE),
        probe_versions: bool = True,
    ) -> Availability:
        """Report which of ``required`` tools resolve to an executable file."""
        missing: list[str] = []
        versions: dict[str, str] = {}
        digests: dict[str, str] = {}
        paths: dict[str, str] = {}
        for tool in required:
            binary = self._binary(tool)
            if binary is None:
                missing.append(tool)
                continue
            paths[tool] = str(binary)
            try:
                digests[tool] = _sha256_file(binary)
            except OSError:
                missing.append(tool)
                paths.pop(tool, None)
                continue
            versions[tool] = _probe_version(binary) if probe_versions else _UNKNOWN_VERSION
        return Availability(
            available=not missing,
            missing=tuple(missing),
            versions=versions,
            digests=digests,
            paths=paths,
        )

    # -- external process plumbing --------------------------------------------

    @staticmethod
    def _run(tool: str, argv: Sequence[str], *, cwd: Path, timeout_s: float) -> _RunOutcome:
        """Run one tool. No shell, explicit argv, bounded, captured and hashed.

        The child gets its own session so a timeout can reap the whole process
        group, matching ``runtime/sandbox.py``. The complete streams travel in
        the returned outcome; only bounded excerpts reach :class:`ProcessTrace`.
        """
        started = time.monotonic()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "LC_ALL": "C",
            "HOME": str(cwd),
            "TMPDIR": str(cwd),
        }
        timed_out = False
        exit_code: int | None = None
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                list(argv),
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except OSError as error:
            # ENOEXEC, ETXTBSY, permission problems: a typed TOOL_ERROR, never
            # an exception escaping the adapter.
            return _RunOutcome(
                trace=_trace(
                    tool,
                    argv,
                    exit_code=None,
                    timed_out=False,
                    elapsed_s=time.monotonic() - started,
                    stdout=b"",
                    stderr=b"",
                    launch_error=f"{type(error).__name__}: {error}",
                ),
                stdout=b"",
                stderr=b"",
            )
        with process:
            try:
                stdout, stderr = process.communicate(timeout=timeout_s)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_group(process)
                stdout, stderr = process.communicate()
        return _RunOutcome(
            trace=_trace(
                tool,
                argv,
                exit_code=None if timed_out else exit_code,
                timed_out=timed_out,
                elapsed_s=time.monotonic() - started,
                stdout=stdout,
                stderr=stderr,
            ),
            stdout=stdout,
            stderr=stderr,
        )

    # -- public operations -----------------------------------------------------

    def verify_plan(
        self,
        domain_text: str,
        problem_text: str,
        plan_text: str,
        *,
        timeout_s: float = 60.0,
        fragment: SupportedFragment = DEFAULT_FRAGMENT,
    ) -> VerificationResult:
        """Verify a hierarchical plan with pandaPIparser's verification mode."""
        started = time.monotonic()
        report = fragment.check(domain_text, problem_text)
        availability = self.availability(required=(TOOL_PARSER,))
        if not report.supported:
            return _verification(
                VerificationStatus.UNSUPPORTED_FEATURE,
                availability,
                (),
                started,
                report.unsupported_features,
                "; ".join(report.detection_notes),
            )
        if not availability.available:
            return _verification(
                VerificationStatus.SOLVER_UNAVAILABLE,
                availability,
                (),
                started,
                (),
                f"missing PANDA binaries: {', '.join(availability.missing)}",
            )

        parser = self.parser
        assert parser is not None  # guaranteed by availability.available

        with tempfile.TemporaryDirectory(prefix="sh-panda-verify-") as raw_dir:
            work = Path(raw_dir)
            domain = _write(work / "domain.hddl", domain_text)
            problem = _write(work / "problem.hddl", problem_text)
            plan = _write(work / "plan.txt", plan_text)
            outcome = self._run(
                TOOL_PARSER,
                [str(parser), "--verify", "-C", str(domain), str(problem), str(plan)],
                cwd=work,
                timeout_s=timeout_s,
            )

        trace = outcome.trace
        traces = (trace,)
        if trace.launch_error is not None:
            return _verification(
                VerificationStatus.TOOL_ERROR,
                availability,
                traces,
                started,
                (),
                f"could not launch the verifier: {trace.launch_error}",
            )
        if trace.timed_out:
            return _verification(
                VerificationStatus.TIMEOUT, availability, traces, started, (), "verifier timed out"
            )

        verdict = _verify_verdict(outcome)
        if verdict is None:
            return _verification(
                VerificationStatus.TOOL_ERROR,
                availability,
                traces,
                started,
                (),
                f"unrecognized verifier output (exit code {trace.exit_code})",
            )
        if verdict is False:
            return _verification(
                VerificationStatus.REJECTED,
                availability,
                traces,
                started,
                (),
                "verifier reported the plan invalid",
            )

        try:
            # Verification is the explicit mode where a plan may legitimately
            # arrive without a root line; solving never accepts that.
            witness = parse_plan(plan_text, require_root=False)
        except PandaPlanFormatError as error:
            return _verification(
                VerificationStatus.TOOL_ERROR,
                availability,
                traces,
                started,
                (),
                f"verifier accepted a plan we cannot build a witness from: {error}",
            )
        return _verification(
            VerificationStatus.VERIFIED, availability, traces, started, (), "", witness=witness
        )

    def solve(
        self,
        domain_text: str,
        problem_text: str,
        *,
        timeout_s: float = 300.0,
        search_limit: int | None = None,
        fragment: SupportedFragment = DEFAULT_FRAGMENT,
    ) -> SolveResult:
        """Run parser, grounder and engine over an HDDL domain/problem pair.

        ``timeout_s`` bounds the whole pipeline and is shared by every stage.
        ``search_limit`` is a number of **seconds**, forwarded to the engine as
        ``--timelimit``: pandaPIengine offers no node limit, and this adapter
        does not invent one. Neither a timeout nor an exhausted budget is ever
        reported as ``UNSOLVABLE_PROVEN``.
        """
        started = time.monotonic()
        report = fragment.check(domain_text, problem_text)
        availability = self.availability()
        if search_limit is not None and search_limit <= 0:
            return _solve(
                SolveStatus.TOOL_ERROR,
                availability,
                (),
                started,
                (),
                f"search_limit must be a positive number of seconds, got {search_limit}",
            )
        if not report.supported:
            return _solve(
                SolveStatus.UNSUPPORTED_FEATURE,
                availability,
                (),
                started,
                report.unsupported_features,
                "; ".join(report.detection_notes),
            )
        if not availability.available:
            return _solve(
                SolveStatus.SOLVER_UNAVAILABLE,
                availability,
                (),
                started,
                (),
                f"missing PANDA binaries: {', '.join(availability.missing)}",
            )

        parser, grounder, engine = self.parser, self.grounder, self.engine
        assert parser is not None and grounder is not None and engine is not None

        traces: list[ProcessTrace] = []
        raw_plan: str | None = None
        converted_plan: str | None = None

        with tempfile.TemporaryDirectory(prefix="sh-panda-solve-") as raw_dir:
            work = Path(raw_dir)
            domain = _write(work / "domain.hddl", domain_text)
            problem = _write(work / "problem.hddl", problem_text)
            parsed = work / "problem.htn"
            grounded = work / "problem.sas"

            stages: tuple[tuple[str, list[str]], ...] = (
                (TOOL_PARSER, [str(parser), "-C", str(domain), str(problem), str(parsed)]),
                (TOOL_GROUNDER, [str(grounder), str(parsed), str(grounded)]),
                (TOOL_ENGINE, _engine_argv(engine, grounded, search_limit)),
            )
            outcome = None
            for tool, argv in stages:
                # One shared budget: every stage sees what the earlier ones left.
                remaining = timeout_s - (time.monotonic() - started)
                outcome = self._run(tool, argv, cwd=work, timeout_s=max(remaining, 0.0))
                trace = outcome.trace
                traces.append(trace)
                if trace.launch_error is not None:
                    return _solve(
                        SolveStatus.TOOL_ERROR,
                        availability,
                        tuple(traces),
                        started,
                        (),
                        f"could not launch the {tool}: {trace.launch_error}",
                    )
                if trace.timed_out:
                    return _solve(
                        SolveStatus.TIMEOUT,
                        availability,
                        tuple(traces),
                        started,
                        (),
                        f"{tool} exceeded the adapter timeout of {timeout_s}s",
                    )
                if tool != TOOL_ENGINE and trace.exit_code != 0:
                    return _solve(
                        SolveStatus.TOOL_ERROR,
                        availability,
                        tuple(traces),
                        started,
                        (),
                        f"{tool} failed with exit code {trace.exit_code}",
                    )

            assert outcome is not None  # the stage tuple is never empty
            status, detail = _engine_outcome(outcome)
            if status is not SolveStatus.SOLVED:
                return _solve(status, availability, tuple(traces), started, (), detail)

            # Read the full stream, not the excerpt: a real engine prints far
            # more than EXCERPT_LIMIT_BYTES before the plan block.
            raw_plan = _extract_plan(_decode(outcome.stdout))
            if raw_plan is None:
                return _solve(
                    SolveStatus.TOOL_ERROR,
                    availability,
                    tuple(traces),
                    started,
                    (),
                    "engine reported Solved but printed no plan block",
                )

            # The grounder rewrites the model, so the engine's plan is not a
            # plan for the original HDDL. Back-translate before claiming SOLVED.
            raw_path = _write(work / "raw.plan", raw_plan)
            converted_path = work / "converted.plan"
            remaining = timeout_s - (time.monotonic() - started)
            convert = self._run(
                TOOL_PARSER,
                [str(parser), "-c", "-C", str(raw_path), str(converted_path)],
                cwd=work,
                timeout_s=max(remaining, 0.0),
            ).trace
            traces.append(convert)
            if convert.launch_error is not None:
                return _solve(
                    SolveStatus.TOOL_ERROR,
                    availability,
                    tuple(traces),
                    started,
                    (),
                    f"could not launch the plan converter: {convert.launch_error}",
                )
            if convert.timed_out:
                return _solve(
                    SolveStatus.TIMEOUT,
                    availability,
                    tuple(traces),
                    started,
                    (),
                    "plan back-translation exceeded the adapter timeout",
                )
            if convert.exit_code != 0 or not converted_path.is_file():
                return _solve(
                    SolveStatus.TOOL_ERROR,
                    availability,
                    tuple(traces),
                    started,
                    (),
                    "plan back-translation to the original HDDL model failed "
                    f"(exit code {convert.exit_code})",
                )
            converted_plan = converted_path.read_text(encoding="utf-8")

        try:
            # A solved plan without a root task is not a hierarchical plan.
            witness = parse_plan(converted_plan, require_root=True)
        except PandaPlanFormatError as error:
            return _solve(
                SolveStatus.TOOL_ERROR,
                availability,
                tuple(traces),
                started,
                (),
                f"engine plan cannot be turned into a witness: {error}",
            )
        return _solve(
            SolveStatus.SOLVED,
            availability,
            tuple(traces),
            started,
            (),
            "",
            plan_text=converted_plan,
            witness=witness,
        )


def _engine_argv(engine: Path, grounded: Path, search_limit: int | None) -> list[str]:
    """Engine command line. The only budget pandaPIengine offers is seconds.

    ``search_limit`` is validated by the caller, which turns a non-positive
    value into a typed ``TOOL_ERROR`` rather than an exception.
    """
    argv = [str(engine)]
    if search_limit is not None:
        argv.append(f"--timelimit={search_limit}")
    argv.append(str(grounded))
    return argv


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _plain(text: str) -> str:
    """Drop ANSI colouring so classification does not depend on -C landing."""
    return _ANSI_RE.sub("", text)


def _decode(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def _haystack(outcome: _RunOutcome) -> str:
    """Complete stdout and stderr, ANSI-free and lower-cased, for classification.

    Deliberately not the excerpt: a real pandaPIengine prints heuristic and
    search statistics that can push the status line past the excerpt bound.
    """
    return _plain(_decode(outcome.stdout) + "\n" + _decode(outcome.stderr)).lower()


def _contains_any(haystack: str, needles: Iterable[str]) -> bool:
    return any(needle in haystack for needle in needles)


def _verify_verdict(outcome: _RunOutcome) -> bool | None:
    """``false`` wins over ``true``: a rejection is never softened."""
    haystack = _haystack(outcome)
    if _VERIFY_FALSE_MARKER in haystack:
        return False
    if _VERIFY_TRUE_MARKER in haystack:
        return True
    return None


def _extract_plan(text: str) -> str | None:
    """Cut the IPC plan block out of engine stdout. ``<==`` is optional."""
    lines = _plain(text).splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == _PLAN_OPEN)
    except StopIteration:
        return None
    end = next(
        (
            index
            for index, line in enumerate(lines)
            if index > start and line.strip() == _PLAN_CLOSE
        ),
        len(lines) - 1,
    )
    return "\n".join(lines[start : end + 1]) + "\n"


def _engine_outcome(outcome: _RunOutcome) -> tuple[SolveStatus, str]:
    """Classify one pandaPIengine run from its status line.

    The exit code is deliberately the last resort: pandaPIengine returns 0 for
    solved, timeout and proven-unsolvable alike.
    """
    trace = outcome.trace
    haystack = _haystack(outcome)
    # A found solution wins over a trailing budget message; a budget message
    # wins over the unsolvable marker, so an exhausted search can never be
    # reported as a proof that the goal is impossible.
    if _ENGINE_SOLVED_MARKER in haystack:
        return SolveStatus.SOLVED, ""
    if _contains_any(haystack, _ENGINE_LIMIT_MARKERS):
        return (
            SolveStatus.SEARCH_LIMIT_REACHED,
            "engine stopped at its own search budget, which is not a proof",
        )
    if _ENGINE_UNSOLVABLE_MARKER in haystack:
        return (
            SolveStatus.UNSOLVABLE_PROVEN,
            "engine reported the grounded problem proven unsolvable",
        )
    if trace.exit_code != 0:
        return SolveStatus.TOOL_ERROR, f"engine failed with exit code {trace.exit_code}"
    return (
        SolveStatus.TOOL_ERROR,
        "no pandaPIengine status line found; only the default progression "
        f"search is supported (exit code {trace.exit_code})",
    )


def _stream_hashes(traces: tuple[ProcessTrace, ...]) -> tuple[str, str]:
    if not traces:
        return _EMPTY_SHA256, _EMPTY_SHA256
    last = traces[-1]
    return last.stdout_sha256, last.stderr_sha256


def _verification(
    status: VerificationStatus,
    availability: Availability,
    traces: tuple[ProcessTrace, ...],
    started: float,
    unsupported: tuple[str, ...],
    detail: str,
    *,
    witness: DecompositionWitness | None = None,
) -> VerificationResult:
    stdout_sha256, stderr_sha256 = _stream_hashes(traces)
    return VerificationResult(
        status=status,
        witness=witness,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
        elapsed_s=time.monotonic() - started,
        toolchain=availability,
        traces=traces,
        unsupported_features=unsupported,
        detail=detail,
    )


def _solve(
    status: SolveStatus,
    availability: Availability,
    traces: tuple[ProcessTrace, ...],
    started: float,
    unsupported: tuple[str, ...],
    detail: str,
    *,
    plan_text: str | None = None,
    witness: DecompositionWitness | None = None,
) -> SolveResult:
    stdout_sha256, stderr_sha256 = _stream_hashes(traces)
    return SolveResult(
        status=status,
        plan_text=plan_text,
        witness=witness,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
        elapsed_s=time.monotonic() - started,
        toolchain=availability,
        traces=traces,
        unsupported_features=unsupported,
        detail=detail,
    )
