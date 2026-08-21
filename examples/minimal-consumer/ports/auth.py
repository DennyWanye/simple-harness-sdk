# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Authorization port that always allows tool execution.

In a real application, implement user permission dialogs.
"""

from simple_harness.runtime import (
    AuthorizationRequest,
    AuthorizationResult,
)


class AlwaysAllowAuthorization:
    """Always allow tool execution (for demo purposes)."""

    async def request_authorization(
        self,
        request: AuthorizationRequest,
    ) -> AuthorizationResult:
        """Always allow tool calls."""

        # In a real app, show UI dialog to user:
        # - Tool name
        # - Arguments
        # - Risk level
        # - Allow/Deny buttons

        return AuthorizationResult.allow()
