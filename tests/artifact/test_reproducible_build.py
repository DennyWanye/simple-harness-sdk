# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from conftest import BuildArtifacts, sha256


def test_builds_are_byte_for_byte_reproducible(
    reproducible_artifacts: BuildArtifacts,
) -> None:
    first = {path.name: sha256(path) for path in reproducible_artifacts.first.iterdir()}
    second = {path.name: sha256(path) for path in reproducible_artifacts.second.iterdir()}
    assert first
    assert first == second

