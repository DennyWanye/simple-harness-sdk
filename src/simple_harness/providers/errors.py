# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Stable, minimal-disclosure provider failures."""

from __future__ import annotations

from simple_harness.contracts.errors import HarnessError


class ProviderError(HarnessError):
    """Base class for failures at the provider transport/protocol boundary."""

    error_code = "provider_error"
    default_message = "Provider request failed."
    default_retryable = False
    __slots__ = ("status_code",)

    def __init__(
        self,
        *,
        public_message: str | None = None,
        retryable: bool | None = None,
        status_code: int | None = None,
        private_cause: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(
            self.error_code,
            public_message or self.default_message,
            retryable=self.default_retryable if retryable is None else retryable,
            private_cause=private_cause,
        )


class ProviderAuthenticationError(ProviderError):
    __slots__ = ()
    error_code = "provider_authentication_failed"
    default_message = "Provider authentication failed."


class ProviderPaymentRequiredError(ProviderError):
    __slots__ = ()
    error_code = "provider_payment_required"
    default_message = "Provider payment or quota is required."


class ProviderRateLimitError(ProviderError):
    __slots__ = ()
    error_code = "provider_rate_limited"
    default_message = "Provider rate limit exceeded."
    default_retryable = True


class ProviderServerError(ProviderError):
    __slots__ = ()
    error_code = "provider_server_error"
    default_message = "Provider service failed."
    default_retryable = True


class ProviderTimeoutError(ProviderError):
    __slots__ = ()
    error_code = "provider_timeout"
    default_message = "Provider request timed out."
    default_retryable = True


class ProviderCancelledError(ProviderError):
    __slots__ = ()
    error_code = "provider_cancelled"
    default_message = "Provider request was cancelled."


class ProviderTransportError(ProviderError):
    __slots__ = ()
    error_code = "provider_transport_error"
    default_message = "Provider transport failed."
    default_retryable = True


class ProviderRequestRejectedError(ProviderError):
    __slots__ = ()
    error_code = "provider_request_rejected"
    default_message = "Provider rejected the request."


class ProviderProtocolError(ProviderError):
    __slots__ = ()
    error_code = "provider_protocol_error"
    default_message = "Provider returned an invalid response."
