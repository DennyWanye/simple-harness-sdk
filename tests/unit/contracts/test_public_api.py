# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import simple_harness
import simple_harness.contracts as contracts
import simple_harness.runtime as runtime
from simple_harness.testing import arm64_candidate


def test_public_api_matches_frozen_snapshot() -> None:
    snapshot = json.loads((Path(__file__).with_name("public-api.json")).read_text(encoding="utf-8"))
    assert simple_harness.__version__ == snapshot["version"] == "0.7.7"
    assert list(simple_harness.__all__) == snapshot["simple_harness"]
    assert list(contracts.__all__) == snapshot["simple_harness.contracts"]
    assert list(runtime.__all__) == snapshot["simple_harness.runtime"]
    assert list(arm64_candidate.__all__) == snapshot["simple_harness.testing.arm64_candidate"]
    for name in snapshot["simple_harness.contracts"]:
        assert getattr(simple_harness, name) is getattr(contracts, name)
    for name in snapshot["simple_harness"]:
        if name in snapshot["simple_harness.runtime"]:
            assert getattr(simple_harness, name) is getattr(runtime, name)


def test_route_recovery_candidate_keeps_existing_public_exports() -> None:
    root = Path(__file__).parent
    previous = json.loads((root / "public-api-0.7.1.json").read_text())
    current = json.loads((root / "public-api-0.7.2.json").read_text())
    assert previous.pop("version") == "0.7.1"
    assert current.pop("version") == "0.7.2"
    assert current == previous


def test_receipt_reservation_successor_preserves_h073_exports() -> None:
    root = Path(__file__).parent
    previous = json.loads((root / "public-api-0.7.3.json").read_text())
    current = json.loads((root / "public-api.json").read_text())
    assert previous["version"] == "0.7.3" and current["version"] == "0.7.7"
    for module in previous:
        if module != "version":
            assert set(previous[module]) <= set(current[module])


def test_short_successor_preserves_h074_exports():
    root = Path(__file__).parent
    old = json.loads((root / "public-api-0.7.4.json").read_text())
    current = json.loads((root / "public-api.json").read_text())
    assert old["version"] == "0.7.4" and current["version"] == "0.7.7"
    for module in old:
        if module != "version":
            assert set(old[module]) <= set(current[module])
