# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed verifier for the frozen H7 runtime/workflow stage split."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORACLE = "tests/conformance/test_full_runtime_seam.py"
RUNTIME_MARKER = "h7_runtime_gate"
WORKFLOW_MARKER = "h7_workflow_terminal_gate"
INVALID_TERMINAL_NODE = (
    f"{ORACLE}::test_terminal_public_rejects_unknown_or_unbounded_state"
)

RUNTIME_NODES = (
    f"{ORACLE}::test_capability_snapshot_preserves_raw_failure_contract",
    f"{ORACLE}::test_provider_tool_kernel_effect_and_same_driver_order",
    f"{ORACLE}::test_reopen_reconciles_provider_and_tool_unknown_without_replay",
    f"{ORACLE}::test_attached_child_failure_wakes_parent_and_delivers_correlated_terminal",
)
WORKFLOW_NODES = (
    f"{ORACLE}::test_failed_terminal_projects_only_strict_public_fields",
    f"{INVALID_TERMINAL_NODE}[unknown-top-level-field]",
    f"{INVALID_TERMINAL_NODE}[unknown-metric]",
    f"{INVALID_TERMINAL_NODE}[negative-metric]",
    f"{INVALID_TERMINAL_NODE}[diagnostic-not-allowlisted]",
    f"{INVALID_TERMINAL_NODE}[duplicate-stage]",
    f"{INVALID_TERMINAL_NODE}[retry-action-not-allowlisted]",
    f"{ORACLE}::test_legacy_terminal_without_public_projection_is_byte_shape_compatible",
)
ALL_NODES = RUNTIME_NODES + WORKFLOW_NODES

_MISSING_MODULE = re.compile(
    r"ModuleNotFoundError: No module named "
    r"['\"]simple_harness\.workflow(?:\.(?:native|errors))?['\"]"
)
_MISSING_AUTHORITY = re.compile(
    r"ImportError: cannot import name "
    r"['\"](?:NativeWorkflowExecutable|InvalidStatePatch)['\"] from "
    r"['\"]simple_harness\.workflow(?:\.(?:native|errors))?['\"]"
)
_AUTHORITY_IMPORTS = {
    "native": (
        "from simple_harness.workflow.native import NativeWorkflowExecutable",
        "NativeWorkflowExecutable",
    ),
    "errors": (
        "from simple_harness.workflow.errors import InvalidStatePatch",
        "InvalidStatePatch",
    ),
}


class GateFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    output: str


def _run_pytest(arguments: Sequence[str]) -> CommandResult:
    command = (sys.executable, "-m", "pytest", *arguments)
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return CommandResult(command, completed.returncode, completed.stdout)


def _run_python(source: str, *, python_path: Path | None = None) -> CommandResult:
    command = (sys.executable, "-c", source)
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if python_path is not None:
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(python_path)
            if not existing
            else f"{python_path}{os.pathsep}{existing}"
        )
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return CommandResult(command, completed.returncode, completed.stdout)


def _fail(message: str, result: CommandResult | None = None) -> GateFailure:
    if result is None:
        return GateFailure(message)
    rendered = " ".join(result.command)
    return GateFailure(f"{message}\nCOMMAND: {rendered}\n{result.output}")


def _node_lines(output: str) -> tuple[str, ...]:
    prefix = f"{ORACLE}::"
    return tuple(line.strip() for line in output.splitlines() if line.startswith(prefix))


def _collect(marker: str | None = None) -> tuple[str, ...]:
    arguments = ["--collect-only", "-q", ORACLE]
    if marker is not None:
        arguments.extend(("-m", marker))
    result = _run_pytest(arguments)
    if result.returncode != 0:
        raise _fail("H7 oracle collection failed", result)
    return _node_lines(result.output)


def _verify_collection() -> None:
    runtime = _collect(RUNTIME_MARKER)
    workflow = _collect(WORKFLOW_MARKER)
    all_nodes = _collect()
    if runtime != RUNTIME_NODES:
        raise GateFailure(f"runtime node drift: expected={RUNTIME_NODES!r} actual={runtime!r}")
    if workflow != WORKFLOW_NODES:
        raise GateFailure(
            f"workflow node drift: expected={WORKFLOW_NODES!r} actual={workflow!r}"
        )
    if all_nodes != ALL_NODES:
        raise GateFailure(f"full node drift: expected={ALL_NODES!r} actual={all_nodes!r}")
    if len(set(all_nodes)) != 12:
        raise GateFailure("H7 oracle must contain exactly 12 unique nodes")


def _require_green(arguments: Sequence[str], label: str) -> None:
    result = _run_pytest((*arguments, "-q"))
    if result.returncode != 0:
        raise _fail(f"{label} must be GREEN", result)
    lowered = result.output.lower()
    if any(status in lowered for status in (" skipped", " xfailed", " xpassed")):
        raise _fail(f"{label} may not skip or xfail", result)


def _exception_lines(output: str) -> tuple[str, ...]:
    lines: list[str] = []
    for raw_line in output.splitlines():
        error_match = re.match(r"^\s*E\s+(.+)$", raw_line)
        line = error_match.group(1).strip() if error_match else raw_line.strip()
        if re.match(r"^(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception):", line):
            lines.append(line)
    return tuple(lines)


def _is_approved_missing(line: str) -> bool:
    return bool(_MISSING_MODULE.search(line) or _MISSING_AUTHORITY.search(line))


def _is_workflow_source_path(path: str) -> bool:
    return bool(
        re.search(
            r"(?:^|[/\\])(?:src[/\\])?simple_harness[/\\]workflow[/\\]"
            r"[^\s:\"]+\.py$",
            path,
        )
    )


def _direct_traceback_frames(output: str) -> tuple[tuple[str, int], ...]:
    return tuple(
        (match.group(1), int(match.group(2)))
        for match in re.finditer(
            r'^\s*File "([^"]+)", line (\d+), in [^\n]+$',
            output,
            flags=re.MULTILINE,
        )
        if "importlib" not in match.group(1)
    )


def _pytest_traceback_frames(output: str) -> tuple[tuple[str, int], ...]:
    return tuple(
        (match.group(1), int(match.group(2)))
        for match in re.finditer(
            r"^([^\s\n][^:\n]*\.py):(\d+):(?: in [^\n]+| [^\n]+)$",
            output,
            flags=re.MULTILINE,
        )
        if "importlib" not in match.group(1)
    )


def _probe_authority(key: str, *, python_path: Path | None = None) -> bool:
    statement, authority = _AUTHORITY_IMPORTS[key]
    result = _run_python(statement, python_path=python_path)
    if result.returncode == 0:
        return False
    exception_lines = _exception_lines(result.output)
    frames = _direct_traceback_frames(result.output)
    if (
        result.returncode != 1
        or len(exception_lines) != 1
        or not _is_approved_missing(exception_lines[0])
        or "Traceback (most recent call last):" not in result.output
        or not frames
        or frames[-1] != ("<string>", 1)
        or any(_is_workflow_source_path(path) for path, _ in frames)
        or authority not in statement
    ):
        raise _fail(
            f"isolated T4.3 authority import failed for an unapproved reason: {authority}",
            result,
        )
    return True


def _verify_spoof_resistance() -> None:
    """Prove source-raised lookalikes fail while missing symbols remain valid."""

    with tempfile.TemporaryDirectory(prefix="h7-authority-spoof-") as raw_root:
        root = Path(raw_root)
        package = root / "simple_harness"
        workflow = package / "workflow"
        workflow.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (workflow / "__init__.py").write_text("", encoding="utf-8")
        for key, (_, authority) in _AUTHORITY_IMPORTS.items():
            module = workflow / f"{key}.py"
            module.write_text(
                "raise ModuleNotFoundError("
                "\"No module named 'simple_harness.workflow'\""
                ")\n",
                encoding="utf-8",
            )
            try:
                _probe_authority(key, python_path=root)
            except GateFailure:
                pass
            else:
                raise GateFailure(
                    f"spoofed top-level exception was accepted for {authority}"
                )

            module.write_text("# authority intentionally absent\n", encoding="utf-8")
            if not _probe_authority(key, python_path=root):
                raise GateFailure(
                    f"missing symbol ImportError was not accepted for {authority}"
                )


def _probe_pending_authorities() -> dict[str, bool]:
    """Import each frozen authority in a fresh interpreter before trusting pytest."""

    missing = {key: _probe_authority(key) for key in _AUTHORITY_IMPORTS}
    if not missing["native"]:
        raise GateFailure(
            "runtime stage requires NativeWorkflowExecutable to remain the exact "
            "workflow RED authority"
        )
    return missing


def _expected_import_origin(node: str, missing: dict[str, bool]) -> tuple[str, int]:
    if node.startswith(INVALID_TERMINAL_NODE) and missing["errors"]:
        statement = _AUTHORITY_IMPORTS["errors"][0]
    else:
        statement = _AUTHORITY_IMPORTS["native"][0]
    matches = [
        number
        for number, line in enumerate((ROOT / ORACLE).read_text().splitlines(), start=1)
        if line.strip() == statement
    ]
    if len(matches) != 1:
        raise GateFailure(
            f"frozen authority import line drift: {statement!r} matches={matches!r}"
        )
    return statement, matches[0]


def _require_t43_pending(node: str, missing: dict[str, bool]) -> None:
    result = _run_pytest((node, "-q", "--tb=long"))
    if result.returncode != 1:
        raise _fail("workflow node must be the one expected T4.3 RED", result)
    lowered = result.output.lower()
    if "1 failed" not in lowered:
        raise _fail("workflow node did not produce exactly one pytest failure", result)
    if any(status in lowered for status in (" skipped", " xfailed", " xpassed")):
        raise _fail("workflow node may not skip or xfail", result)
    exception_lines = _exception_lines(result.output)
    if not exception_lines:
        raise _fail("workflow RED did not name a pending T4.3 authority", result)
    if len(exception_lines) != 1:
        raise _fail("workflow RED must contain exactly one terminal exception", result)
    for line in exception_lines:
        if not _is_approved_missing(line):
            raise _fail(f"workflow RED used an unapproved exception: {line}", result)
    expected_statement, expected_line = _expected_import_origin(node, missing)
    frames = _pytest_traceback_frames(result.output)
    if (
        expected_statement not in result.output
        or f"{ORACLE}:{expected_line}:" not in result.output
        or not frames
        or frames[-1] != (ORACLE, expected_line)
        or any(_is_workflow_source_path(path) for path, _ in frames)
    ):
        raise _fail(
            "workflow RED traceback did not originate at the frozen import factory line",
            result,
        )


def verify(stage: str) -> None:
    _verify_collection()
    _verify_spoof_resistance()
    if stage == "runtime":
        _require_green((ORACLE, "-m", RUNTIME_MARKER), "H7 runtime group")
        missing = _probe_pending_authorities()
        for node in WORKFLOW_NODES:
            _require_t43_pending(node, missing)
        return
    _require_green((ORACLE, "-m", RUNTIME_MARKER), "H7 runtime group")
    _require_green((ORACLE, "-m", WORKFLOW_MARKER), "H7 workflow group")
    _require_green((ORACLE,), "H7 full group")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("runtime", "workflow"), required=True)
    arguments = parser.parse_args()
    try:
        verify(arguments.stage)
    except GateFailure as error:
        print(f"FULL_RUNTIME_STAGE_FAIL stage={arguments.stage}\n{error}")
        return 1
    print(
        f"FULL_RUNTIME_STAGE_PASS stage={arguments.stage} "
        "runtime=4 workflow=8 total=12"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
