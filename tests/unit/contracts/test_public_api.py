# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import simple_harness
import simple_harness.contracts as contracts


def test_public_api_matches_frozen_snapshot() -> None:
    snapshot = json.loads(
        (Path(__file__).with_name("public-api.json")).read_text(encoding="utf-8")
    )
    assert list(simple_harness.__all__) == snapshot["simple_harness"]
    assert list(contracts.__all__) == snapshot["simple_harness.contracts"]
    for name in snapshot["simple_harness.contracts"]:
        assert getattr(simple_harness, name) is getattr(contracts, name)

