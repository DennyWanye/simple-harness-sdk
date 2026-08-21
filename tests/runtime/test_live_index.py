# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

from simple_harness.runtime.live_index import LiveRunIndex


def test_waiter_observes_completed_run_until_next_event_loop_turn() -> None:
    async def scenario() -> None:
        index = LiveRunIndex()

        async def complete() -> None:
            return None

        index.schedule("child-run", complete())
        await index.wait("child-run")

        assert index.active_run_ids() == ("child-run",)
        await asyncio.sleep(0)
        assert index.active_run_ids() == ()

    asyncio.run(scenario())
