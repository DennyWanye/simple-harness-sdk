# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public provider contracts and adapters."""

from .base import (
    CancelToken,
    Provider,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    ProviderToolSpec,
    ProviderUsage,
    Secret,
)
from .errors import (
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderError,
    ProviderPaymentRequiredError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRequestRejectedError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from .openai_compatible import OpenAICompatibleProvider
from .redaction import SecretRedactor

__all__ = (
    "CancelToken",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderAuthenticationError",
    "ProviderCancelledError",
    "ProviderError",
    "ProviderPaymentRequiredError",
    "ProviderProtocolError",
    "ProviderRateLimitError",
    "ProviderRequest",
    "ProviderRequestRejectedError",
    "ProviderResponse",
    "ProviderServerError",
    "ProviderTimeoutError",
    "ProviderToolCall",
    "ProviderToolSpec",
    "ProviderTransportError",
    "ProviderUsage",
    "Secret",
    "SecretRedactor",
)
