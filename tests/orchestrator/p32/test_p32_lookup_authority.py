# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 slice D · P32-10: an answer only counts when the service can give one (plan v3 D7;
plan review round 1 P2-2, round 2 P2-6).

A connector now says how much its ``lookup`` is worth.  ``authoritative`` means the service
records an idempotency key atomically with the effect, so "no record" really is "it never
started" and the action may be handed off again with the same key.  ``best_effort`` (the
default) means nothing of the sort: a lookup that finds nothing leaves the action UNKNOWN
for a person.  An action at L2 or above may only run on an authoritative connector.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_orchestrator.governance.policies import DeploymentPolicy, action_decision
from agent_orchestrator.runtime.actions import lookup_verdict
from agent_orchestrator.runtime.connectors import OperationSpec, Receipt, TestConfigService


class _BestEffort:
    """A connector that can be asked, but whose answer proves nothing."""

    name = "best_effort"
    supports_idempotency = True
    supports_reconciliation = True
    operations = {
        "note": OperationSpec("note", "L1", ()),
        "set": OperationSpec("set", "L2", ("value",)),
    }

    def normalize_target(self, target: str) -> str:
        return target.strip()

    def execute(self, operation, target, params, *, idempotency_key) -> Receipt:  # type: ignore[no-untyped-def]
        raise AssertionError("not used in this test")

    def lookup(self, idempotency_key: str) -> Receipt | None:
        return None


class _Authoritative(_BestEffort):
    name = "authoritative"
    lookup_authority = "authoritative"


def _receipt() -> Receipt:
    return Receipt(
        idempotency_key="action-1:v1",
        connector="authoritative",
        operation="set",
        target="a",
        params_hash="h",
        applied=True,
    )


def test_the_default_authority_is_best_effort():
    assert getattr(_BestEffort(), "lookup_authority", "best_effort") == "best_effort"
    assert TestConfigService.lookup_authority == "authoritative"


@pytest.mark.parametrize(
    "connector,found,expected",
    [
        (_Authoritative(), None, "CONFIRMED_NOT_STARTED"),
        (_Authoritative(), _receipt(), "COMPLETED"),
        (_BestEffort(), None, "STILL_UNKNOWN"),  # "I found nothing" is not "it did not happen"
        (_BestEffort(), _receipt(), "COMPLETED"),  # a receipt is a fact whoever gives it
        (None, None, "STILL_UNKNOWN"),
    ],
    ids=["auth-none", "auth-receipt", "best-none", "best-receipt", "no-connector"],
)
def test_lookup_verdict_depends_on_the_authority(connector: Any, found, expected):
    assert lookup_verdict(connector, found) == expected


def test_l2_and_above_need_an_authoritative_connector():
    deployment = DeploymentPolicy(enabled_connectors=("best_effort", "authoritative"))
    refused = action_decision(deployment, _BestEffort(), "set")
    assert refused.refused == "connector_lookup_not_authoritative"
    allowed = action_decision(deployment, _Authoritative(), "set")
    assert allowed.refused is None and allowed.level == "L2"


def test_l1_may_run_on_a_best_effort_connector():
    deployment = DeploymentPolicy(enabled_connectors=("best_effort",))
    decision = action_decision(deployment, _BestEffort(), "note")
    assert decision.refused is None and decision.level == "L1"


def test_the_test_service_stays_usable_at_l2(tmp_path):
    service = TestConfigService(tmp_path / "config.json")
    deployment = DeploymentPolicy(enabled_connectors=("test_config",))
    decision = action_decision(deployment, service, "set")
    assert decision.refused is None and decision.level == "L2"
