# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for conformance CLI."""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

import pytest
from test_suite_runner import GOOD_VALUES

from simple_harness.testing.cli import (
    load_host_factory,
    parse_host_factory,
    run_conformance_cli,
    validate_suites,
)

ARTIFACT_SHA = "b" * 64


def test_parse_host_factory_valid():
    """Test parsing valid host factory spec."""
    module_name, factory_name = parse_host_factory("my.module:build_host")
    assert module_name == "my.module"
    assert factory_name == "build_host"


def test_parse_host_factory_missing_colon():
    """Test parsing spec without colon."""
    with pytest.raises(ValueError, match="Invalid host factory spec"):
        parse_host_factory("my.module.build_host")


def test_parse_host_factory_empty_module():
    """Test parsing spec with empty module."""
    with pytest.raises(ValueError, match="must be non-empty"):
        parse_host_factory(":build_host")


def test_parse_host_factory_empty_factory():
    """Test parsing spec with empty factory."""
    with pytest.raises(ValueError, match="must be non-empty"):
        parse_host_factory("my.module:")


def test_load_host_factory_valid():
    """Test loading valid factory function."""
    factory = load_host_factory("json", "dumps")
    assert callable(factory)
    assert factory is json.dumps


def test_load_host_factory_invalid_module():
    """Test loading from non-existent module."""
    with pytest.raises(ImportError, match="Cannot import module"):
        load_host_factory("nonexistent_module_xyz", "factory")


def test_load_host_factory_invalid_attribute():
    """Test loading non-existent attribute."""
    with pytest.raises(AttributeError, match="has no attribute"):
        load_host_factory("json", "nonexistent_function")


def test_validate_suites_valid():
    """Test validating valid suite names."""
    validate_suites(["provider", "tool"])
    validate_suites(["runtime", "workflow"])
    validate_suites(["provider", "tool", "runtime", "workflow"])


def test_validate_suites_invalid():
    """Test validating invalid suite names."""
    with pytest.raises(ValueError, match="Invalid suite names"):
        validate_suites(["provider", "invalid_suite"])


def test_run_conformance_cli_missing_host(capsys):
    """Test CLI with missing --host argument."""
    with pytest.raises(SystemExit) as exc_info:
        run_conformance_cli(["--suite", "provider"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "required" in captured.err.lower()


def test_run_conformance_cli_missing_suite(capsys):
    """Test CLI with missing --suite argument."""
    with pytest.raises(SystemExit) as exc_info:
        run_conformance_cli(["--host", "json:dumps"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "required" in captured.err.lower()


def test_run_conformance_cli_invalid_host_spec(capsys):
    """Test CLI with invalid host spec format."""
    exit_code = run_conformance_cli(
        ["--host", "invalid_spec", "--suite", "provider", "--artifact-sha256", ARTIFACT_SHA]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Error" in captured.err


def test_run_conformance_cli_invalid_suite(capsys):
    """Test CLI with invalid suite name."""
    exit_code = run_conformance_cli(
        ["--host", "json:dumps", "--suite", "invalid", "--artifact-sha256", ARTIFACT_SHA]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Invalid suite names" in captured.err


def test_run_conformance_cli_nonexistent_module(capsys):
    """Test CLI with non-existent module."""
    exit_code = run_conformance_cli(
        [
            "--host",
            "nonexistent_xyz:factory",
            "--suite",
            "provider",
            "--artifact-sha256",
            ARTIFACT_SHA,
        ]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Error loading host factory" in captured.err


def _write_host_module(tmp_path: Path, *, status: str = "pass") -> str:
    module_name = "consumer_host_" + hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:12]
    module = tmp_path / f"{module_name}.py"
    module.write_text(
        textwrap.dedent(
            f"""
            from simple_harness.testing import CaseObservation, ConformanceCaseUnavailable, ConformanceHostMetadata

            VALUES = {GOOD_VALUES!r}

            class Suite:
                async def _case(self, case_id):
                    if {status!r} == "skip":
                        raise ConformanceCaseUnavailable("physical seam unavailable")
                    if {status!r} == "error":
                        raise RuntimeError("physical seam failed")
                    values = dict(VALUES[case_id])
                    if {status!r} == "fail" and case_id == "provider.physical_request":
                        values["physical_calls"] = 0
                    return CaseObservation(case_id=case_id, values=values, evidence={{"receipt": "ok"}})
                async def physical_request(self): return await self._case("provider.physical_request")
                async def typed_error(self): return await self._case("provider.typed_error")
                async def usage(self): return await self._case("provider.usage")
                async def redaction(self): return await self._case("provider.redaction")
                async def schema(self): return await self._case("tool.schema")
                async def five_state(self): return await self._case("tool.five_state")
                async def reconcile(self): return await self._case("tool.reconcile")
                async def malformed_duplicate_late(self): return await self._case("tool.malformed_duplicate_late")
                async def no_tool(self): return await self._case("runtime.no_tool")
                async def one_tool(self): return await self._case("runtime.one_tool")
                async def multi_turn_tool(self): return await self._case("runtime.multi_turn_tool")
                async def session_persistence(self): return await self._case("runtime.session_persistence")
                async def hitl(self): return await self._case("runtime.hitl")
                async def delivery(self): return await self._case("runtime.delivery")
                async def budget(self): return await self._case("runtime.budget")
                async def restart_without_replay(self): return await self._case("runtime.restart_without_replay")
                async def host_owned(self): return await self._case("workflow.host_owned")
                async def official_durable_task(self): return await self._case("workflow.official_durable_task")
                async def official_personal_v1(self): return await self._case("workflow.official_personal_v1")
                async def official_capability_build(self): return await self._case("workflow.official_capability_build")
                async def ticket_fingerprint(self): return await self._case("workflow.ticket_fingerprint")
                async def reopen(self): return await self._case("workflow.reopen")
                async def aclose(self):
                    return None

            class Context:
                async def __aenter__(self):
                    self.suite = Suite()
                    return self.suite
                async def __aexit__(self, *args):
                    await self.suite.aclose()

            class Host:
                metadata = ConformanceHostMetadata(
                    protocol_version="1.0.0",
                    host_name="consumer-fixture",
                    host_version="1.2.3",
                    capabilities=frozenset({{"provider", "tool", "runtime", "workflow"}}),
                )
                def open_suite(self, name):
                    return Context()

            def build_host():
                return Host()
            """  # noqa: E501
        ),
        encoding="utf-8",
    )
    return f"{module_name}:build_host"


def test_run_conformance_cli_success_without_json(capsys, tmp_path, monkeypatch):
    """CLI executes the shared provider runner."""
    monkeypatch.syspath_prepend(str(tmp_path))
    host = _write_host_module(tmp_path)
    exit_code = run_conformance_cli(
        ["--host", host, "--suite", "provider", "--artifact-sha256", ARTIFACT_SHA]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Simple Harness SDK Conformance Testing" in captured.out
    assert f"Host: {host}" in captured.out
    assert "Suites: provider" in captured.out
    assert "PASS" in captured.out


def test_run_conformance_cli_success_with_json(capsys, tmp_path, monkeypatch):
    """Test CLI with JSON output file."""
    monkeypatch.syspath_prepend(str(tmp_path))
    host = _write_host_module(tmp_path)
    json_file = tmp_path / "report.json"
    exit_code = run_conformance_cli(
        [
            "--host",
            host,
            "--suite",
            "provider,tool",
            "--json",
            str(json_file),
            "--artifact-sha256",
            ARTIFACT_SHA,
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Report written to" in captured.out

    # Verify JSON file was created
    assert json_file.exists()
    report = json.loads(json_file.read_text())
    assert report["protocol_version"] == "1.0.0"
    assert report["host"] == {"name": "consumer-fixture", "version": "1.2.3"}
    assert report["suites"] == ["provider", "tool"]
    assert report["status"] == "pass"
    assert report["cases"]


def test_run_conformance_cli_multiple_suites(capsys, tmp_path, monkeypatch):
    """Test CLI with multiple comma-separated suites."""
    monkeypatch.syspath_prepend(str(tmp_path))
    host = _write_host_module(tmp_path)
    exit_code = run_conformance_cli(
        [
            "--host",
            host,
            "--suite",
            "provider,tool,runtime,workflow",
            "--artifact-sha256",
            ARTIFACT_SHA,
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Suites: provider, tool, runtime, workflow" in captured.out


@pytest.mark.parametrize("status", ["skip", "fail", "error"])
def test_run_conformance_cli_required_non_pass_is_nonzero(status, capsys, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    host = _write_host_module(tmp_path, status=status)

    assert (
        run_conformance_cli(
            ["--host", host, "--suite", "provider", "--artifact-sha256", ARTIFACT_SHA]
        )
        == 1
    )
    assert "FAIL" in capsys.readouterr().out


def test_run_conformance_cli_version(capsys):
    """Test CLI --version flag."""
    with pytest.raises(SystemExit) as exc_info:
        run_conformance_cli(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "1.0.0" in captured.out
