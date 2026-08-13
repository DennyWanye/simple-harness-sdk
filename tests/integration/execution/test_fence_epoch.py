# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.execution import StaleFenceError, require_current_epoch


def test_matching_positive_epoch_is_accepted() -> None:
    require_current_epoch(expected=7, current=7)


@pytest.mark.parametrize("expected,current", [(1, 2), (2, 1), (0, 1), (1, 0)])
def test_stale_or_invalid_epoch_fails_closed(expected: int, current: int) -> None:
    with pytest.raises(StaleFenceError) as caught:
        require_current_epoch(expected=expected, current=current)

    assert caught.value.code == "stale_fence_epoch"
