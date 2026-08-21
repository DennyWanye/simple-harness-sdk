# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

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
    assert "simple_harness/testing/arm64_candidate.py" in names
    assert any(name.endswith(".dist-info/licenses/LICENSES/Apache-2.0.txt") for name in names)
    assert any(name.endswith(".dist-info/licenses/NOTICE") for name in names)
    assert all(name.startswith("simple_harness/") or ".dist-info/" in name for name in names)


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
    assert "src/simple_harness/testing/arm64_candidate.py" in relative
    assert "tests/artifact/test_import_purity.py" in relative


def test_required_workflows_are_present() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / ".github/workflows/ci.yml").is_file()
    assert (root / ".github/workflows/release.yml").is_file()
    assert (root / ".github/workflows/release-candidate-conformance.yml").is_file()


def test_fresh_schema_v3_is_a_self_contained_static_artifact() -> None:
    root = Path(__file__).resolve().parents[2]
    sqlite_root = root / "src/simple_harness/execution/sqlite"
    loader = (sqlite_root / "schema.py").read_text(encoding="utf-8")
    migrations = sqlite_root / "migrations"
    fresh_sql = (migrations / "0003_fresh.sql").read_text(encoding="utf-8")

    assert 'joinpath("0003_fresh.sql")' in loader
    assert "0001_initial.sql" not in loader
    assert "0002_context_authority.sql" not in loader
    assert loader.count(".read_text(") == 1

    legacy_tables: set[str] = set()
    for legacy_name in ("0001_initial.sql", "0002_context_authority.sql"):
        legacy_sql = (migrations / legacy_name).read_text(encoding="utf-8")
        legacy_tables.update(re.findall(r"CREATE TABLE ([a-z_]+)", legacy_sql))
    fresh_tables = set(re.findall(r"CREATE TABLE ([a-z_]+)", fresh_sql))
    assert legacy_tables <= fresh_tables
    assert {
        "execution_users",
        "memory_outbox",
        "context_preparation_staging",
    } <= fresh_tables
    assert fresh_sql.count("CREATE TABLE execution_sessions") == 1
