# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Local, synthetic tokenizer checks; no downloads, provider calls, or credentials."""

from __future__ import annotations

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

import agent_orchestrator.runtime.hf_chat_tokens as counting
from simple_harness.agents.wire import restore_tool_calls
from simple_harness.contracts import CallId, ContentBlock, Message, MessageRole, RequestId
from simple_harness.providers import ProviderRequest, ProviderToolSpec
from simple_harness.providers.openai_compatible import openai_chat_request_payload


class FakeTokenizer:
    chat_template = "local frozen chat template"

    def __init__(self):
        self.calls = []
        self.text_calls = []

    def encode(self, text, *, add_special_tokens):
        self.text_calls.append((text, add_special_tokens))
        return [1] * len(text)

    def apply_chat_template(
        self, messages, *, tokenize, add_generation_prompt, return_dict=False, **kwargs
    ):
        assert return_dict is False
        self.calls.append((messages, tokenize, add_generation_prompt, kwargs))
        # An easily checked fake: calls and tools affect the resulting count.
        calls = sum(len(message.get("tool_calls", ())) for message in messages)
        return [1] * (3 * len(messages) + 7 * calls + 5 * len(kwargs.get("tools", ())))


@pytest.fixture
def local_tokenizer(tmp_path, monkeypatch):
    directory = tmp_path / "tokenizer"
    directory.mkdir()
    files = {
        "tokenizer.json": b"synthetic vocabulary",
        "tokenizer_config.json": b'{"chat_template":"local frozen chat template"}',
        "config.json": b'{"model_type":"qwen4_exp"}',
        "chat_template.jinja": b"local frozen chat template",
    }
    for name, data in files.items():
        (directory / name).write_bytes(data)
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    tokenizer = FakeTokenizer()
    loads = []

    def from_pretrained(*args, **kwargs):
        loads.append((args, kwargs))
        return tokenizer

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained),
        ),
    )
    versions = {"transformers": "5.16.1", "tokenizers": "0.23.2"}
    monkeypatch.setattr(counting, "version", versions.__getitem__)
    return directory, manifest, tokenizer, loads, versions


def _estimator(local, **kwargs):
    directory, manifest, *_ = local
    return counting.HFChatTokenEstimator(
        directory,
        model="qwen38-flash-next",
        expected_files=manifest,
        **kwargs,
    )


def _request(messages=None, tools=()):
    return ProviderRequest(
        RequestId("hf-local-test"), messages or (Message(MessageRole.USER, "hello"),), tools=tools
    )


def test_offline_load_text_and_actual_body_without_tools(local_tokenizer):
    directory, _, tokenizer, loads, _ = local_tokenizer
    estimator = _estimator(local_tokenizer)
    assert loads == [
        (
            (str(directory),),
            {
                "local_files_only": True,
                "trust_remote_code": False,
            },
        )
    ]
    assert estimator.bound_protocol == "hf-chat-template-v1"
    assert estimator.request_counting_protocol == "hf-chat-template-exact-wire-v1"
    assert estimator.requires_prior_output_reserve is False
    assert estimator.tool_schema_mode == "legacy"
    assert estimator.count_text("中文 ok") == len("中文 ok")
    assert tokenizer.text_calls == [("中文 ok", False)]
    request = _request()
    assert estimator.estimate_input_tokens(request) == 3
    assert tokenizer.calls == [
        (
            openai_chat_request_payload(request, model="qwen38-flash-next")["messages"],
            True,
            True,
            {},
        )
    ]
    assert estimator.count_request_tokens(request) == 3


def test_durable_tool_calls_and_tool_schema_are_counted_from_wire_body(local_tokenizer):
    estimator = _estimator(local_tokenizer)
    tokenizer = local_tokenizer[2]
    messages, fallbacks = restore_tool_calls(
        (
            Message(MessageRole.USER, "read the file"),
            Message(MessageRole.ASSISTANT, "", metadata={"provider_turn_ordinal": 1}),
            Message(MessageRole.TOOL, "contents", name="read_file", call_id=CallId("call-1")),
        ),
        {1: {"call-1": {"name": "read_file", "arguments": {"path": "notes.txt"}}}},
    )
    assert fallbacks == 0
    request = _request(
        messages, tools=(ProviderToolSpec("read_file", "Read a file", {"type": "object"}),)
    )
    wire = openai_chat_request_payload(request, model="qwen38-flash-next")
    for message in wire["messages"]:
        for call in message.get("tool_calls", ()):
            call["function"]["arguments"] = json.loads(call["function"]["arguments"])
    assert estimator.estimate_input_tokens(request) == 3 * 3 + 7 + 5
    assert estimator.count_request_tokens(request) == 3 * 3 + 7 + 5
    sent_messages, tokenize, generation_prompt, kwargs = tokenizer.calls[-1]
    assert sent_messages == wire["messages"]
    assert kwargs == {"tools": wire["tools"]}
    assert tokenize and generation_prompt
    assert sent_messages[1]["tool_calls"][0]["id"] == "call-1"
    assert sent_messages[1]["tool_calls"][0]["function"]["arguments"] == {"path": "notes.txt"}
    # Counting uses an HF copy; the HTTP contract still carries JSON strings.
    assert (
        openai_chat_request_payload(request, model="qwen38-flash-next")["messages"][1][
            "tool_calls"
        ][0]["function"]["arguments"]
        == '{"path": "notes.txt"}'
    )
    assert sent_messages[2]["tool_call_id"] == "call-1"
    assert estimator.estimate_input_tokens(_request((messages[0],))) == 3


def test_fingerprint_binds_files_model_versions_and_frozen_kwargs(local_tokenizer):
    directory, manifest, tokenizer, _, versions = local_tokenizer
    kwargs = {"enable_thinking": False, "switches": [1]}
    original = _estimator(local_tokenizer, chat_template_kwargs=kwargs)
    assert (
        original.fingerprint
        == _estimator(
            local_tokenizer,
            chat_template_kwargs={"switches": [1], "enable_thinking": False},
        ).fingerprint
    )
    kwargs["switches"].append(2)
    original.estimate_input_tokens(_request())
    assert tokenizer.calls[-1][3] == {"enable_thinking": False, "switches": [1]}
    assert (
        original.fingerprint != _estimator(local_tokenizer, chat_template_kwargs=kwargs).fingerprint
    )
    assert (
        original.fingerprint
        != counting.HFChatTokenEstimator(
            directory,
            model="other-alias",
            expected_files=manifest,
            chat_template_kwargs={"enable_thinking": False, "switches": [1]},
        ).fingerprint
    )

    class OtherCountingProtocol(counting.HFChatTokenEstimator):
        request_counting_protocol = "different-request-counter"

    assert (
        original.fingerprint
        != OtherCountingProtocol(
            directory,
            model="qwen38-flash-next",
            expected_files=manifest,
            chat_template_kwargs={"enable_thinking": False, "switches": [1]},
        ).fingerprint
    )
    versions["transformers"] = "5.16.2"
    assert (
        original.fingerprint
        != _estimator(
            local_tokenizer,
            chat_template_kwargs={"enable_thinking": False, "switches": [1]},
        ).fingerprint
    )
    versions["transformers"] = "5.16.1"
    versions["tokenizers"] = "0.23.3"
    assert (
        original.fingerprint
        != _estimator(
            local_tokenizer,
            chat_template_kwargs={"enable_thinking": False, "switches": [1]},
        ).fingerprint
    )
    (directory / "chat_template.jinja").write_bytes(b"new template")
    new_manifest = dict(manifest)
    new_manifest["chat_template.jinja"] = hashlib.sha256(b"new template").hexdigest()
    assert (
        original.fingerprint
        != counting.HFChatTokenEstimator(
            directory,
            model="qwen38-flash-next",
            expected_files=new_manifest,
            chat_template_kwargs={"enable_thinking": False, "switches": [1]},
        ).fingerprint
    )


def test_tampering_missing_template_manifest_and_links_fail_before_load(local_tokenizer):
    directory, manifest, _, loads, _ = local_tokenizer
    (directory / "config.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _estimator(local_tokenizer)
    assert not loads
    manifest["config.json"] = hashlib.sha256(b"changed").hexdigest()
    (directory / "added_tokens.json").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="does not cover"):
        _estimator(local_tokenizer)
    (directory / "added_tokens.json").unlink()
    (directory / "chat_template.jinja").unlink()
    (directory / "chat_template.jinja").symlink_to(directory / "tokenizer.json")
    with pytest.raises(ValueError, match="linked file"):
        _estimator(local_tokenizer)
    assert not loads


def test_multimodal_rejected_before_template_and_bad_options(local_tokenizer):
    estimator = _estimator(local_tokenizer)
    tokenizer = local_tokenizer[2]
    request = _request(
        (Message(MessageRole.USER, (ContentBlock("image_url", {"image_url": "secret-content"}),)),)
    )
    with pytest.raises(ValueError, match="text messages only"):
        estimator.estimate_input_tokens(request)
    assert not tokenizer.calls
    with pytest.raises(ValueError, match="reserved keys"):
        _estimator(local_tokenizer, chat_template_kwargs={"tools": []})
    with pytest.raises(ValueError, match="reserved keys"):
        _estimator(local_tokenizer, chat_template_kwargs={"truncation": True})
    with pytest.raises(ValueError, match="reserved keys"):
        _estimator(local_tokenizer, chat_template_kwargs={"chat_template": "unsafe"})
    with pytest.raises(ValueError, match="must be JSON"):
        _estimator(local_tokenizer, chat_template_kwargs={"private": object()})


def test_loader_and_template_errors_do_not_disclose_source(local_tokenizer, monkeypatch):
    directory, manifest, tokenizer, _, _ = local_tokenizer

    def fail_load(*args, **kwargs):
        raise RuntimeError("secret-token and user content")

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=fail_load),
        ),
    )
    with pytest.raises(ValueError, match="could not be constructed") as error:
        _estimator(local_tokenizer)
    assert "secret-token" not in str(error.value)
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **kw: tokenizer),
        ),
    )

    def fail_template(*args, **kwargs):
        raise RuntimeError("secret-token and user content")

    tokenizer.apply_chat_template = fail_template
    estimator = counting.HFChatTokenEstimator(
        directory,
        model="qwen38-flash-next",
        expected_files=manifest,
    )
    with pytest.raises(ValueError, match="could not count request") as error:
        estimator.estimate_input_tokens(_request())
    assert "secret-token" not in str(error.value)
