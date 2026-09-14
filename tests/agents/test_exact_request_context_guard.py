"""Exact chat-template framing must enforce a shared window before handoff."""

from types import SimpleNamespace

import pytest

from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.port import ContextRequiredContentTooLarge, RequestGuard
from simple_harness.contracts import Message, MessageRole, RequestId
from simple_harness.providers import ProviderRequest


def request():
    return ProviderRequest(
        RequestId("local-window"), (Message(MessageRole.USER, "tiny visible text"),)
    )


def test_exact_template_overhead_rejects_before_provider():
    tokenizer = SimpleNamespace(
        fingerprint="local-exact",
        count_text=lambda text: 1,
        count_request_tokens=lambda req: 228353,
    )
    guard = RequestGuard(
        SimpleNamespace(latest_agent_context_selection=lambda run: None),
        tokenizer=tokenizer,
        policy=ContextPolicy(
            max_input_tokens=262144,
            max_total_tokens=262144,
            output_reserve=32768,
            safety_margin=1024,
            render_slack_tokens=0,
        ),
    )
    with pytest.raises(ContextRequiredContentTooLarge):
        guard.check(request(), run_id="run")
    assert guard.last_request_tokens == 228353


def test_exact_boundary_passes():
    tokenizer = SimpleNamespace(
        fingerprint="local-exact",
        count_text=lambda text: 1,
        count_request_tokens=lambda req: 228352,
    )
    guard = RequestGuard(
        SimpleNamespace(latest_agent_context_selection=lambda run: None),
        tokenizer=tokenizer,
        policy=ContextPolicy(
            max_input_tokens=262144,
            max_total_tokens=262144,
            output_reserve=32768,
            safety_margin=1024,
            render_slack_tokens=0,
        ),
    )
    guard.check(request(), run_id="run")
    assert guard.last_request_tokens == 228352


@pytest.mark.parametrize("value", [-1, True, 1.5, "5"])
def test_invalid_exact_counter_is_not_silently_downgraded(value):
    tokenizer = SimpleNamespace(
        fingerprint="bad", count_text=lambda text: 1, count_request_tokens=lambda req: value
    )
    guard = RequestGuard(None, tokenizer=tokenizer, policy=ContextPolicy())
    with pytest.raises(ValueError):
        guard.count(request())


def test_legacy_counter_keeps_existing_framing():
    tokenizer = SimpleNamespace(fingerprint="legacy", count_text=lambda text: 3)
    guard = RequestGuard(None, tokenizer=tokenizer, policy=ContextPolicy())
    assert guard.count(request()) == 7
