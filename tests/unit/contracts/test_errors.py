# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness import ErrorCode, HarnessError


def test_error_exposes_only_stable_public_fields() -> None:
    secret = "canary-api-key-never-serialize"
    error = HarnessError(
        ErrorCode.PROVIDER_UNAVAILABLE,
        "The provider is temporarily unavailable.",
        retryable=True,
        private_cause=RuntimeError(secret),
    )

    assert error.code == "provider_unavailable"
    assert str(error) == "The provider is temporarily unavailable."
    assert secret not in repr(error)
    assert secret not in str(error)
    assert error.to_dict() == {
        "schema_version": 1,
        "code": "provider_unavailable",
        "public_message": "The provider is temporarily unavailable.",
        "retryable": True,
    }
    assert secret not in repr(error.to_dict())


@pytest.mark.parametrize("code", ["UPPERCASE", "contains space", "", "a" * 65])
def test_error_codes_have_a_stable_wire_format(code: str) -> None:
    with pytest.raises(ValueError, match="error code"):
        HarnessError(code, "safe message")


def test_public_message_cannot_be_empty() -> None:
    with pytest.raises(ValueError, match="public_message"):
        HarnessError("invalid_request", "  ")
