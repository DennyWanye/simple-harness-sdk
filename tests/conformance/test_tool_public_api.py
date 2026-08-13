# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import simple_harness.tools as tools


def test_tool_public_api_matches_snapshot() -> None:
    snapshot = json.loads(
        (Path(__file__).with_name("tool-public-api.json")).read_text(encoding="utf-8")
    )

    assert snapshot["module"] == tools.__name__
    assert sorted(tools.__all__) == snapshot["symbols"]
    assert all(hasattr(tools, symbol) for symbol in snapshot["symbols"])
