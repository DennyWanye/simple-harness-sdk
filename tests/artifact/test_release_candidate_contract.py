# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _candidate_module():
    path = ROOT / "scripts/build/reproducibility.py"
    spec = importlib.util.spec_from_file_location("candidate_reproducibility", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provenance_module():
    path = ROOT / "scripts/build/authoritative_provenance.py"
    spec = importlib.util.spec_from_file_location("authoritative_provenance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _clean_project(root: Path) -> Path:
    repository = root / "source"
    package = repository / "src/simple_harness"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from .version import __version__\n", encoding="utf-8"
    )
    (package / "version.py").write_text('__version__ = "0.1.1"\n', encoding="utf-8")
    (repository / "NOTICE").write_text("Simple Harness SDK\n", encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        """[build-system]
requires = ["hatchling==1.32.0"]
build-backend = "hatchling.build"
[project]
name = "simple-harness-sdk"
dynamic = ["version"]
requires-python = ">=3.11"
[tool.hatch.version]
path = "src/simple_harness/version.py"
[tool.hatch.build.targets.wheel]
packages = ["src/simple_harness"]
[tool.hatch.build.targets.sdist]
include = ["/NOTICE", "/pyproject.toml", "/src"]
""",
        encoding="utf-8",
    )
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "candidate@example.invalid")
    _git(repository, "config", "user.name", "Candidate Test")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "candidate source")
    return repository


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_version_has_one_runtime_authority() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package = (ROOT / "src/simple_harness/__init__.py").read_text(encoding="utf-8")
    version = (ROOT / "src/simple_harness/version.py").read_text(encoding="utf-8")

    assert 'dynamic = ["version"]' in pyproject
    assert 'path = "src/simple_harness/version.py"' in pyproject
    assert 'from .version import __version__' in package
    assert '__version__ = "' in version
    build_script = (
        ROOT / "scripts/build/reproducibility.py"
    ).read_text(encoding="utf-8")
    assert 'VERSION = "0.1.1"' not in build_script
    assert 'src/simple_harness/version.py' in build_script
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert 'simple_harness.__version__ == "0.1.0"' not in ci
    assert 'src/simple_harness/version.py' in ci


def test_ci_builds_one_authoritative_artifact_for_python_311_to_313() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert workflow.count("uv build --out-dir dist") == 1
    assert "SOURCE_DATE_EPOCH" not in workflow
    assert 'python: ["3.11", "3.12", "3.13"]' in workflow
    assert "authoritative-distributions" in workflow
    assert "authoritative_provenance.py emit" in workflow
    assert "authoritative_provenance.py verify" in workflow


def test_release_only_uploads_verified_frozen_bytes() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "artifact_run_id:" in workflow
    assert "candidate_commit:" in workflow
    assert "wheel_sha256:" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "authoritative_provenance.py verify" in workflow
    assert "gh release upload" in workflow
    assert "uv build" not in workflow
    assert "push:" not in workflow
    assert "softprops/action-gh-release" not in workflow


def test_authoritative_provenance_is_canonical_and_detects_tampering(
    tmp_path: Path,
) -> None:
    module = _provenance_module()
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "simple_harness_sdk-0.2.0-py3-none-any.whl"
    sdist = dist / "simple_harness_sdk-0.2.0.tar.gz"
    wheel.write_bytes(b"wheel-bytes")
    sdist.write_bytes(b"sdist-bytes")
    commit = "a" * 40
    module.emit(dist, source_commit=commit, build_utc="2026-08-21T10:11:12Z")
    module.verify(dist, source_commit=commit, version="0.2.0")
    build_info = (dist / "BUILD_INFO.txt").read_text(encoding="utf-8").splitlines()
    assert [line.partition("=")[0] for line in build_info] == list(module.KEYS)
    sums = (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ", 1)[1] for line in sums] == sorted((wheel.name, sdist.name))
    assert all("  " in line for line in sums)
    wheel.write_bytes(b"tampered")
    with pytest.raises(module.ProvenanceError, match="checksum differs"):
        module.verify(dist, source_commit=commit, version="0.2.0")


def test_candidate_workflow_accepts_identity_inputs_and_never_publishes() -> None:
    workflow = (
        ROOT / ".github/workflows/release-candidate-conformance.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "candidate_commit:" in workflow
    assert "artifact_sha256:" in workflow
    assert "macos-14" in workflow
    assert "windows-latest" in workflow
    assert "ubuntu-24.04-arm" in workflow
    assert "simple_harness.testing" in workflow
    assert '--artifact-sha256 "$EXPECTED_SHA256"' in workflow
    assert "IMPORT_PURITY_AND_SCHEMA_REOPEN_PASS" in workflow
    assert "Database.open(path)" in workflow
    assert "reopened.schema_version == SCHEMA_VERSION" in workflow
    assert "softprops/action-gh-release" not in workflow
    assert "pypa/gh-action-pypi-publish" not in workflow


def test_build_candidate_generates_a_consistent_attestation_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _candidate_module()
    repository = _clean_project(tmp_path)
    output = tmp_path / "candidate"
    monkeypatch.setattr(module, "ROOT", repository)
    monkeypatch.setattr(module, "VERSION", "0.1.1")

    manifest = module.build_candidate(output, planned_tag="v0.1.1")

    build_info = json.loads((output / "BUILD_INFO.json").read_text(encoding="utf-8"))
    sbom = json.loads((output / "SBOM.spdx.json").read_text(encoding="utf-8"))
    persisted_manifest = json.loads(
        (output / "candidate-manifest.json").read_text(encoding="utf-8")
    )
    assert persisted_manifest == manifest
    assert build_info["version"] == sbom["packages"][0]["versionInfo"] == "0.1.1"
    assert build_info["planned_tag"] == manifest["planned_tag"] == "v0.1.1"
    assert build_info["commit"] == manifest["commit"] == _git(repository, "rev-parse", "HEAD")
    assert build_info["artifacts"] == manifest["artifacts"]
    assert (output / "NOTICE").read_text(encoding="utf-8") == "Simple Harness SDK\n"
    for line in (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        assert _sha256(output / name) == digest
    assert manifest["build_info_sha256"] == _sha256(output / "BUILD_INFO.json")
    assert manifest["sbom_sha256"] == _sha256(output / "SBOM.spdx.json")
    assert manifest["notice_sha256"] == _sha256(output / "NOTICE")
    assert manifest["sha256sums_sha256"] == _sha256(output / "SHA256SUMS")

    with pytest.raises(RuntimeError, match="output directory must be empty"):
        module.build_candidate(output, planned_tag="v0.1.1")


def test_build_candidate_rejects_untracked_build_input_and_wrong_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _candidate_module()
    repository = _clean_project(tmp_path)
    monkeypatch.setattr(module, "ROOT", repository)
    monkeypatch.setattr(module, "VERSION", "0.1.1")

    with pytest.raises(RuntimeError, match="planned_tag must be exactly v0.1.1"):
        module.build_candidate(tmp_path / "wrong-tag", planned_tag="v9.9.9")

    (repository / "plans").mkdir()
    (repository / "plans/local-note.md").write_text("not packaged\n", encoding="utf-8")
    assert module._dirty_build_paths() == ()
    (repository / "src/simple_harness/untracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="tracked or untracked build inputs"):
        module.build_candidate(tmp_path / "dirty", planned_tag="v0.1.1")


def test_build_candidate_rejects_existing_tag_on_another_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _candidate_module()
    repository = _clean_project(tmp_path)
    monkeypatch.setattr(module, "ROOT", repository)
    monkeypatch.setattr(module, "VERSION", "0.1.1")
    _git(repository, "tag", "v0.1.1")
    notice = repository / "NOTICE"
    notice.write_text("Simple Harness SDK candidate\n", encoding="utf-8")
    _git(repository, "add", "NOTICE")
    _git(repository, "commit", "-qm", "advance candidate")

    with pytest.raises(RuntimeError, match="existing tag v0.1.1 points to"):
        module.build_candidate(tmp_path / "retag", planned_tag="v0.1.1")
