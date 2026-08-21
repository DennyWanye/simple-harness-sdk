# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""SDK-owned assertions for consumer conformance observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from simple_harness.contracts import JsonValue

from .contracts import CaseObservation

Verifier = Callable[[Mapping[str, JsonValue]], None]


def _fail(name: str, expected: object, actual: object) -> None:
    raise AssertionError(f"{name}: expected {expected!r}, observed {actual!r}")


def _equal(values: Mapping[str, JsonValue], name: str, expected: object) -> None:
    actual = values.get(name)
    if actual != expected:
        _fail(name, expected, actual)


def _same(values: Mapping[str, JsonValue], left: str, right: str) -> None:
    first, second = values.get(left), values.get(right)
    if not isinstance(first, str) or not first or first != second:
        _fail(f"{left}/{right}", "matching non-empty identities", (first, second))


def _at_least(values: Mapping[str, JsonValue], name: str, minimum: int) -> None:
    actual = values.get(name)
    if isinstance(actual, bool) or not isinstance(actual, int) or actual < minimum:
        _fail(name, f">={minimum}", actual)


def _sequence(values: Mapping[str, JsonValue], name: str, expected: Sequence[str]) -> None:
    actual = values.get(name)
    if not isinstance(actual, (list, tuple)) or tuple(actual) != tuple(expected):
        _fail(name, tuple(expected), actual)


def _provider_physical(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "physical_calls", 1)
    _same(values, "request_id", "response_request_id")


def _provider_error(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "physical_calls", 1)
    code = values.get("error_code")
    if code not in {
        "provider_transport_error",
        "provider_server_error",
        "provider_protocol_error",
    }:
        _fail("error_code", "a typed provider error", code)
    _equal(values, "raw_body_exposed", False)


def _provider_usage(values: Mapping[str, JsonValue]) -> None:
    _at_least(values, "trusted_total_tokens", 1)
    _equal(values, "unknown_usage", None)


def _provider_redaction(values: Mapping[str, JsonValue]) -> None:
    secret, public = values.get("secret"), values.get("public_text")
    if not isinstance(secret, str) or not secret:
        _fail("secret", "non-empty canary", secret)
    if not isinstance(public, str) or secret in public:
        _fail("public_text", "text without secret canary", public)
    _equal(values, "raw_body_exposed", False)


def _tool_schema(values: Mapping[str, JsonValue]) -> None:
    for name in ("closed", "bounded", "reserved_fields_rejected"):
        _equal(values, name, True)


def _tool_five_state(values: Mapping[str, JsonValue]) -> None:
    _sequence(values, "states", ("succeeded", "partial", "rejected", "failed", "unknown"))


def _tool_reconcile(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "initial_state", "unknown")
    final = values.get("final_state")
    if final not in {"succeeded", "partial", "rejected", "failed"}:
        _fail("final_state", "a reconciled terminal state", final)
    _equal(values, "physical_calls_before", values.get("physical_calls_after"))


def _tool_result_boundaries(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "accepted_results", 1)
    _at_least(values, "rejected_results", 3)
    _equal(values, "physical_calls", 1)


def _runtime_no_tool(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "terminal_state", "completed")
    _at_least(values, "provider_calls", 1)
    _equal(values, "tool_calls", 0)


def _runtime_one_tool(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "terminal_state", "completed")
    _at_least(values, "provider_calls", 2)
    _equal(values, "tool_calls", 1)
    _equal(values, "correlation_match", True)


def _runtime_multi(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "terminal_state", "completed")
    _at_least(values, "provider_calls", 3)
    _at_least(values, "tool_calls", 2)
    _equal(values, "unique_call_ids", True)


def _runtime_session(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "reopened", True)
    _same(values, "session_before", "session_after")


def _runtime_hitl(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "physical_calls_before", 0)
    _equal(values, "physical_calls_after", 1)
    _equal(values, "decision", "approved")
    _equal(values, "durable", True)


def _runtime_delivery(values: Mapping[str, JsonValue]) -> None:
    _at_least(values, "attempts", 2)
    _equal(values, "deliveries", 1)
    _equal(values, "settled", True)


def _runtime_budget(values: Mapping[str, JsonValue]) -> None:
    _sequence(
        values,
        "terminations",
        ("max_turns", "max_tool_calls", "wall_clock", "cost", "repeated_tool"),
    )


def _restart_without_replay(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "reopened", True)
    _equal(values, "physical_calls_before", values.get("physical_calls_after"))
    _equal(values, "reconciled", True)


def _workflow_host(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "registered", True)
    _equal(values, "completed", True)
    definition_id = values.get("definition_id")
    if not isinstance(definition_id, str) or not definition_id:
        _fail("definition_id", "non-empty identity", definition_id)


def _official(profile_key: str) -> Verifier:
    def verify(values: Mapping[str, JsonValue]) -> None:
        _equal(values, "profile_key", profile_key)
        _equal(values, "completed", True)

    return verify


def _workflow_ticket(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "forged_ticket_rejected", True)
    _equal(values, "fingerprint_rejected", True)
    _equal(values, "child_runs", 0)


def _workflow_reopen(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "reopened", True)
    _same(values, "run_before", "run_after")
    _equal(values, "physical_calls_before", values.get("physical_calls_after"))
    _equal(values, "completed", True)


def _conversation_contract(values: Mapping[str, JsonValue]) -> None:
    for name in (
        "dto_round_trip",
        "structured_preserved",
        "projection_text_only",
        "stable_statuses",
    ):
        _equal(values, name, True)


def _conversation_schema(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "schema_version", 4)
    _equal(values, "history_rows", 1)
    for name in ("fresh_only", "foreign_keys", "reopened"):
        _equal(values, name, True)


def _conversation_outbox(values: Mapping[str, JsonValue]) -> None:
    _equal(values, "atomic", True)
    _equal(values, "replayed_source_event", True)
    _at_least(values, "sink_calls", 2)
    _equal(values, "settled", True)
    _equal(values, "fake_terminal_intents", 0)


VERIFIERS: Mapping[str, Verifier] = {
    "provider.physical_request": _provider_physical,
    "provider.typed_error": _provider_error,
    "provider.usage": _provider_usage,
    "provider.redaction": _provider_redaction,
    "tool.schema": _tool_schema,
    "tool.five_state": _tool_five_state,
    "tool.reconcile": _tool_reconcile,
    "tool.malformed_duplicate_late": _tool_result_boundaries,
    "runtime.no_tool": _runtime_no_tool,
    "runtime.one_tool": _runtime_one_tool,
    "runtime.multi_turn_tool": _runtime_multi,
    "runtime.session_persistence": _runtime_session,
    "runtime.hitl": _runtime_hitl,
    "runtime.delivery": _runtime_delivery,
    "runtime.budget": _runtime_budget,
    "runtime.restart_without_replay": _restart_without_replay,
    "workflow.host_owned": _workflow_host,
    "workflow.official_durable_task": _official("workflow.durable_task"),
    "workflow.official_personal_v1": _official("workflow.personal_v1"),
    "workflow.official_capability_build": _official("workflow.capability_build"),
    "workflow.ticket_fingerprint": _workflow_ticket,
    "workflow.reopen": _workflow_reopen,
    "conversation.contract": _conversation_contract,
    "conversation.schema_identity": _conversation_schema,
    "conversation.outbox_recovery": _conversation_outbox,
}


def verify_observation(observation: CaseObservation) -> None:
    try:
        verifier = VERIFIERS[observation.case_id]
    except KeyError as error:
        raise AssertionError(f"no SDK verifier for {observation.case_id}") from error
    verifier(observation.values)


__all__ = ("VERIFIERS", "verify_observation")
