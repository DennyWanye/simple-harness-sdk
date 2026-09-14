# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Offline, manifest-bound Hugging Face chat-template input counter.

The caller binds the deployment model and exact local tokenizer assets. Server
template/token accounting parity is a separate deployment acceptance condition.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

from simple_harness.contracts import JsonValue, canonical_json
from simple_harness.providers import ProviderRequest
from simple_harness.providers.openai_compatible import openai_chat_request_payload

_REQUIRED_FILES = frozenset({"config.json", "tokenizer_config.json", "tokenizer.json"})
_TOKENIZER_FILES = _REQUIRED_FILES | frozenset(
    {
        "chat_template.jinja",
        "chat_template.json",
        "special_tokens_map.json",
        "added_tokens.json",
        "vocab.json",
        "merges.txt",
        "vocab.txt",
        "tokenizer.model",
        "sentencepiece.bpe.model",
    }
)
_RESERVED_KWARGS = frozenset(
    {
        "tools",
        "tokenize",
        "add_generation_prompt",
        "chat_template",
        "tokenizer_kwargs",
        "return_dict",
        "return_tensors",
        "truncation",
        "max_length",
        "padding",
        "continue_final_message",
        "return_assistant_tokens_mask",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class HFChatTokenEstimator:
    """Count the text-only OpenAI chat body with a pinned local HF template.

    ``expected_files`` maps filenames in ``tokenizer_path`` to their exact SHA-256
    digests. It must cover every present tokenizer/template input from the supported
    filename set; embedded templates are covered by ``tokenizer_config.json``.
    """

    requires_prior_output_reserve = False
    bound_protocol = "hf-chat-template-v1"
    request_counting_protocol = "hf-chat-template-exact-wire-v1"

    def __init__(
        self,
        tokenizer_path: Path,
        *,
        model: str,
        expected_files: Mapping[str, str],
        chat_template_kwargs: Mapping[str, JsonValue] | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("HF chat counter requires a model name")
        if not isinstance(expected_files, Mapping):
            raise ValueError("HF chat counter requires a SHA-256 file manifest")
        manifest = dict(expected_files)
        if not _REQUIRED_FILES.issubset(manifest) or any(
            not isinstance(name, str)
            or name not in _TOKENIZER_FILES
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            for name, digest in manifest.items()
        ):
            raise ValueError("HF chat tokenizer manifest has missing or invalid filenames/digests")
        if chat_template_kwargs is None:
            chat_template_kwargs = {}
        if not isinstance(chat_template_kwargs, Mapping) or any(
            not isinstance(key, str) or key in _RESERVED_KWARGS for key in chat_template_kwargs
        ):
            raise ValueError("HF chat template kwargs must be JSON with no reserved keys")
        try:
            frozen_kwargs = canonical_json(dict(chat_template_kwargs))
        except (TypeError, ValueError):
            raise ValueError("HF chat template kwargs must be JSON") from None

        directory = Path(tokenizer_path)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("HF chat tokenizer directory must be a local directory")
        self._verify_files(directory, manifest)
        try:
            transformers_version = version("transformers")
            tokenizers_version = version("tokenizers")
            # Optional dependency: neither import nor tokenizer creation happens
            # until the full local manifest has been checked.
            AutoTokenizer = import_module("transformers").AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(
                str(directory),
                local_files_only=True,
                trust_remote_code=False,
            )
        except Exception:
            raise ValueError("HF chat local tokenizer could not be constructed") from None
        self._verify_files(directory, manifest)
        try:
            template = tokenizer.chat_template
        except Exception:
            raise ValueError("HF chat local template could not be read") from None
        if not isinstance(template, str) or not template:
            raise ValueError("HF chat tokenizer requires a local chat template")

        self._tokenizer = tokenizer
        self._model = model
        self._template_kwargs_json = frozen_kwargs
        self.fingerprint = (
            "hf-chat-template:"
            + sha256(
                canonical_json(
                    {
                        "files": {name: digest for name, digest in manifest.items()},
                        "model": model,
                        "transformers_version": transformers_version,
                        "tokenizers_version": tokenizers_version,
                        "template_kwargs": json.loads(frozen_kwargs),
                        "tool_schema_mode": self.tool_schema_mode,
                        "serializer": "openai-chat-payload-v1",
                        "hf_tool_arguments": "decoded-json-object-v1",
                        "bound_protocol": self.bound_protocol,
                        "request_counting_protocol": self.request_counting_protocol,
                        "requires_prior_output_reserve": self.requires_prior_output_reserve,
                        "count_text_add_special_tokens": False,
                        "tokenize": True,
                        "return_dict": False,
                        "add_generation_prompt": True,
                    }
                ).encode("utf-8")
            ).hexdigest()
        )

    @staticmethod
    def _verify_files(directory: Path, manifest: dict[str, str]) -> None:
        try:
            # Transformers can also discover a template from this directory;
            # this filename-only manifest deliberately does not support it.
            if (directory / "chat_templates").exists() or (
                directory / "chat_templates"
            ).is_symlink():
                raise ValueError("HF chat tokenizer has an unmanifested template directory")
            present = {p.name for p in directory.iterdir() if p.name in _TOKENIZER_FILES}
            if present != set(manifest):
                raise ValueError("HF chat tokenizer manifest does not cover local tokenizer files")
            for name, digest in manifest.items():
                path = directory / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError(
                        "HF chat tokenizer manifest references a missing or linked file"
                    )
                if sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError("HF chat tokenizer SHA-256 mismatch")
        except OSError:
            raise ValueError("HF chat tokenizer files could not be read") from None

    @property
    def tool_schema_mode(self) -> str:
        return "legacy"

    def count_text(self, text: str) -> int:
        if not isinstance(text, str):
            raise ValueError("HF chat counter supports text only")
        try:
            return len(self._tokenizer.encode(text, add_special_tokens=False))
        except Exception:
            raise ValueError("HF chat tokenizer could not count text") from None

    def estimate_input_tokens(self, request: ProviderRequest) -> int:
        if any(not isinstance(message.content, str) for message in request.messages):
            raise ValueError("HF chat counter supports text messages only")
        # This same serializer restores durable assistant provider_tool_calls and
        # emits the actual legacy tool schemas used by the HTTP adapter.
        payload = openai_chat_request_payload(
            request,
            model=self._model,
            tool_schema_mode=self.tool_schema_mode,
        )
        # HF templates consume structured arguments; OpenAI HTTP transports them
        # as JSON strings. vLLM performs the same conversion before rendering.
        # Decode valid JSON only: no syntax repair and no mutation of the request.
        try:
            for message in payload["messages"]:
                for call in message.get("tool_calls", ()):
                    function = call["function"]
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("invalid tool arguments")
                    function["arguments"] = arguments
        except (KeyError, TypeError, ValueError):
            raise ValueError("HF chat template requires valid object tool arguments") from None
        kwargs = json.loads(self._template_kwargs_json)
        if "tools" in payload:
            kwargs["tools"] = payload["tools"]
        try:
            ids = self._tokenizer.apply_chat_template(
                payload["messages"],
                tokenize=True,
                add_generation_prompt=True,
                return_dict=False,
                **kwargs,
            )
            if not isinstance(ids, list) or any(
                not isinstance(token_id, int) or isinstance(token_id, bool) for token_id in ids
            ):
                raise ValueError("invalid token IDs")
            return len(ids)
        except Exception:
            raise ValueError("HF chat template could not count request") from None

    def count_request_tokens(self, request: ProviderRequest) -> int:
        """Exact visible request count for RequestGuard's optional wire seam."""
        return self.estimate_input_tokens(request)


__all__ = ("HFChatTokenEstimator",)
