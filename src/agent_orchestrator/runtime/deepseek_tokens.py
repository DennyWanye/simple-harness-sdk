# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pinned official V4.1 text counting for an explicitly bound DeepSeek deployment.

Optional dependency and tokenizer bytes are installed by the deployment, never
downloaded on import/startup. The official renderer counts the public wire input.
Thinking tool continuations may add previous reasoning on the server: admission
MUST additionally reserve every prior reported output token for this Agent. The
counter alone is not a complete charge bound and cannot account for unknown calls.
"""

from __future__ import annotations

from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from simple_harness.contracts import canonical_json
from simple_harness.providers import ProviderRequest
from simple_harness.providers.openai_compatible import openai_chat_request_payload

RECIPE_VERSION = "0.1.1"
RECIPE_COMMIT = "8cadfede7063c896b944e7bae05daa3549ae97ea"
TOKENIZER_SHA256 = "81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99"
TOKENIZER_URL = (
    "https://raw.githubusercontent.com/deepseek-ai/deepseek-recipe/"
    + RECIPE_COMMIT
    + "/static/tokenizers/v41/tokenizer.json"
)


class DeepSeekV41TokenEstimator:
    """Official text tokenizer and request renderer; no credentials or HTTP client.

    A deployment must bind this only to api.deepseek.com / deepseek-flash, keep
    the fingerprint with its requests, and stop on any observed reservation
    overrun. Other model aliases and multimodal input require separate profiles.
    """

    requires_prior_output_reserve = True
    bound_protocol = "deepseek-v41-chat-text-plus-prior-output-v1"

    def __init__(
        self, tokenizer_path: Path, *, model: str = "deepseek-flash",
        tool_schema_mode: str = "legacy",
    ) -> None:
        if tool_schema_mode not in {"legacy", "deepseek-strict-v1"}:
            raise ValueError("unsupported DeepSeek tool schema mode")
        if model != "deepseek-flash":
            raise ValueError("unsupported model for the pinned DeepSeek V4.1 counter")
        if version("deepseek-recipe") != RECIPE_VERSION:
            raise ValueError("DeepSeek recipe version differs from the pinned counting profile")
        raw = Path(tokenizer_path).read_bytes()
        if sha256(raw).hexdigest() != TOKENIZER_SHA256:
            raise ValueError("DeepSeek tokenizer bytes differ from the pinned counting profile")
        # Optional native dependency; constructing an unrelated profile never imports it.
        from deepseek_recipe import DeepseekV41Encoding, Tokenizer

        self._model = model
        self._tool_schema_mode = tool_schema_mode
        self._tokenizer = Tokenizer.from_str(raw.decode("utf-8"))
        self._encoding = DeepseekV41Encoding().with_tokenizer(self._tokenizer)
        self.fingerprint = (
            "deepseek-v41:"
            + sha256(
                canonical_json(
                    {
                        "recipe_version": RECIPE_VERSION,
                        "recipe_commit": RECIPE_COMMIT,
                        "tokenizer_sha256": TOKENIZER_SHA256,
                        "model": model,
                        "bound_protocol": self.bound_protocol,
                        "requires_prior_output_reserve": True,
                        "serializer": ("openai-chat-payload-v1" if tool_schema_mode == "legacy"
                                       else "openai-chat-deepseek-strict-v1"),
                    }
                ).encode("utf-8")
            ).hexdigest()
        )

    def count_text(self, text: str) -> int:
        return len(self._tokenizer.encode(text))

    @property
    def tool_schema_mode(self) -> str:
        return self._tool_schema_mode

    def estimate_input_tokens(self, request: ProviderRequest) -> int:
        if any(not isinstance(message.content, str) for message in request.messages):
            raise ValueError("the pinned DeepSeek counter supports text messages only")
        from deepseek_recipe import ChatCompletionRequest, ConversionOptions

        payload = openai_chat_request_payload(
            request, model=self._model, tool_schema_mode=self._tool_schema_mode,
        )
        converted = ChatCompletionRequest(payload).convert(ConversionOptions())
        return len(self._encoding.encode(converted.conversation))


__all__ = ("DeepSeekV41TokenEstimator", "TOKENIZER_SHA256", "TOKENIZER_URL")
