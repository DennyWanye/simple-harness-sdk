# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path, PurePosixPath
import tarfile
import zipfile

from conftest import BuildArtifacts


FORBIDDEN_ANYWHERE = {
    ".git",
    ".local-test-evidence",
    ".pytest_cache",
    ".venv",
    "__pycache__",
}
FORBIDDEN_TOP_LEVEL = {"build", "dist"}


def _assert_clean(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        assert not FORBIDDEN_ANYWHERE.intersection(path.parts), name
        relative_parts = path.parts[1:]
        assert not relative_parts or relative_parts[0] not in FORBIDDEN_TOP_LEVEL, name
        assert path.suffix not in {".pyc", ".pyo"}, name


def test_wheel_contains_only_package_and_distribution_metadata(
    reproducible_artifacts: BuildArtifacts,
) -> None:
    wheel = next(reproducible_artifacts.first.glob("*.whl"))
    assert wheel.name.endswith("-py3-none-any.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    _assert_clean(names)
    assert "simple_harness/__init__.py" in names
    assert any(
        name.endswith(".dist-info/licenses/LICENSES/Apache-2.0.txt")
        for name in names
    )
    assert any(name.endswith(".dist-info/licenses/NOTICE") for name in names)
    assert all(
        name.startswith("simple_harness/")
        or ".dist-info/" in name
        for name in names
    )


def test_sdist_has_explicit_clean_source_surface(
    reproducible_artifacts: BuildArtifacts,
) -> None:
    sdist = next(reproducible_artifacts.first.glob("*.tar.gz"))
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
    _assert_clean(names)
    relative = ["/".join(PurePosixPath(name).parts[1:]) for name in names]
    assert "pyproject.toml" in relative
    assert "src/simple_harness/__init__.py" in relative
    assert "tests/artifact/test_import_purity.py" in relative


def test_required_workflows_are_present() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / ".github/workflows/ci.yml").is_file()
    assert (root / ".github/workflows/release.yml").is_file()
    assert (root / ".github/workflows/release-candidate-conformance.yml").is_file()
