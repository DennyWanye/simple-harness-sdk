# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

from simple_harness import providers


def test_provider_public_api_matches_frozen_snapshot() -> None:
    snapshot = json.loads(
        Path(__file__).with_name("provider-public-api.json").read_text(encoding="utf-8")
    )
    assert list(providers.__all__) == snapshot["simple_harness.providers"]
    for name in providers.__all__:
        assert getattr(providers, name) is not None
