# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Repository-wide pytest options.

``--run-real-provider`` opts into tests marked ``real_provider`` (they call a real LLM
endpoint).  Without the flag they are skipped, so the default regression command and
``addopts`` stay untouched (Slice 1 closure E19).
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-real-provider",
        action="store_true",
        default=False,
        help="run tests marked real_provider against a real LLM endpoint",
    )
    parser.addoption(
        "--run-real-embedding",
        action="store_true",
        default=False,
        help="run tests marked real_embedding against a real embedding model",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-real-provider"):
        skip = pytest.mark.skip(reason="needs --run-real-provider")
        for item in items:
            if "real_provider" in item.keywords:
                item.add_marker(skip)
    if not config.getoption("--run-real-embedding"):
        skip_embedding = pytest.mark.skip(reason="needs --run-real-embedding")
        for item in items:
            if "real_embedding" in item.keywords:
                item.add_marker(skip_embedding)
