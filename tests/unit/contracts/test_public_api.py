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
    assert simple_harness.__version__ == snapshot["version"] == "0.6.2"
    assert list(simple_harness.__all__) == snapshot["simple_harness"]
    assert list(contracts.__all__) == snapshot["simple_harness.contracts"]
    assert list(runtime.__all__) == snapshot["simple_harness.runtime"]
    assert list(arm64_candidate.__all__) == snapshot["simple_harness.testing.arm64_candidate"]
    for name in snapshot["simple_harness.contracts"]:
        assert getattr(simple_harness, name) is getattr(contracts, name)
    for name in snapshot["simple_harness"]:
        if name in snapshot["simple_harness.runtime"]:
            assert getattr(simple_harness, name) is getattr(runtime, name)
