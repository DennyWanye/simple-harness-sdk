# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from simple_harness import (
    CancelCommandIntent,
    CommandReceipt,
    CommandSnapshot,
    ContinueCommandIntent,
    RunClient,
    StartCommandIntent,
)


async def exercise(
    client: RunClient,
    start: StartCommandIntent,
    continuation: ContinueCommandIntent,
    cancel: CancelCommandIntent,
) -> tuple[CommandReceipt, CommandReceipt, CommandReceipt, CommandSnapshot]:
    return (
        await client.submit_start(start),
        await client.submit_continue(continuation),
        await client.submit_cancel(cancel),
        await client.get_command(start.command_id),
    )
