# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public provider contracts and adapters."""

from .base import (
    CancelToken,
    Provider,
    ProviderContinuationCapability,
    ProviderContinuationMode,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
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
from .reconciliation import (
    ProviderAccountingIdentity,
    ProviderAccountingObservation,
    ProviderAccountingPort,
    ProviderAccountingState,
    ProviderReconciliationObservation,
    ProviderReconciliationPort,
    ProviderReconciliationState,
)
from .redaction import SecretRedactor

__all__ = (
    "ProviderAccountingIdentity",
    "ProviderAccountingObservation",
    "ProviderAccountingPort",
    "ProviderAccountingState",
    "CancelToken",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderAuthenticationError",
    "ProviderCancelledError",
    "ProviderContinuationCapability",
    "ProviderContinuationMode",
    "ProviderError",
    "ProviderPaymentRequiredError",
    "ProviderProtocolError",
    "ProviderRateLimitError",
    "ProviderReconciliationObservation",
    "ProviderReconciliationPort",
    "ProviderReconciliationState",
    "ProviderRequest",
    "ProviderRequestRejectedError",
    "ProviderResponse",
    "ProviderServerError",
    "ProviderTarget",
    "ProviderTimeoutError",
    "ProviderToolCall",
    "ProviderToolSpec",
    "ProviderTransportError",
    "ProviderUsage",
    "Secret",
    "SecretRedactor",
)
