# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for conformance CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simple_harness.testing.cli import (
    load_host_factory,
    parse_host_factory,
    run_conformance_cli,
    validate_suites,
)


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
    exit_code = run_conformance_cli(["--host", "invalid_spec", "--suite", "provider"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Error" in captured.err


def test_run_conformance_cli_invalid_suite(capsys):
    """Test CLI with invalid suite name."""
    exit_code = run_conformance_cli(["--host", "json:dumps", "--suite", "invalid"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Invalid suite names" in captured.err


def test_run_conformance_cli_nonexistent_module(capsys):
    """Test CLI with non-existent module."""
    exit_code = run_conformance_cli(
        ["--host", "nonexistent_xyz:factory", "--suite", "provider"]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Error loading host factory" in captured.err


def test_run_conformance_cli_success_without_json(capsys):
    """Test CLI with valid arguments but no JSON output."""
    exit_code = run_conformance_cli(["--host", "json:dumps", "--suite", "provider"])
    # Returns 1 because implementation is incomplete
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Simple Harness SDK Conformance Testing" in captured.out
    assert "Host: json:dumps" in captured.out
    assert "Suites: provider" in captured.out


def test_run_conformance_cli_success_with_json(capsys, tmp_path):
    """Test CLI with JSON output file."""
    json_file = tmp_path / "report.json"
    exit_code = run_conformance_cli(
        ["--host", "json:dumps", "--suite", "provider,tool", "--json", str(json_file)]
    )
    assert exit_code == 1  # Incomplete implementation
    captured = capsys.readouterr()
    assert "Report written to" in captured.out

    # Verify JSON file was created
    assert json_file.exists()
    report = json.loads(json_file.read_text())
    assert report["protocol_version"] == "1.0.0"
    assert report["host"] == "json:dumps"
    assert report["suites"] == ["provider", "tool"]
    assert report["status"] == "not_implemented"


def test_run_conformance_cli_multiple_suites(capsys):
    """Test CLI with multiple comma-separated suites."""
    exit_code = run_conformance_cli(
        ["--host", "json:dumps", "--suite", "provider,tool,runtime,workflow"]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Suites: provider, tool, runtime, workflow" in captured.out


def test_run_conformance_cli_version(capsys):
    """Test CLI --version flag."""
    with pytest.raises(SystemExit) as exc_info:
        run_conformance_cli(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "1.0.0" in captured.out
