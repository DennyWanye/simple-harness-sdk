"""A planning reply is not made stale by Assurance's own evidence epoch (2A)."""

from __future__ import annotations

import inspect

from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.orchestrator import event_handler
from agent_orchestrator.orchestrator.taskgraph_epochs import planning_scope_digest


def test_the_assurance_evidence_epoch_is_not_counted():
    before = {"mission": 0, "assurance:mission": 4}
    after_grant = {"mission": 0, "assurance:mission": 6}
    assert planning_scope_digest(before) == planning_scope_digest(after_grant)
    assert planning_scope_digest(before) == sha256_hex({"mission": 0})


def test_every_other_scope_still_moves_the_digest():
    assert planning_scope_digest({"mission": 0}) != planning_scope_digest({"mission": 1})
    assert planning_scope_digest({"mission": 0}) != planning_scope_digest(
        {"mission": 0, "branch:a": 1})
    # the old formula, unchanged, wherever Assurance has no epoch
    assert planning_scope_digest({"mission": 2, "branch:a": 1}) == sha256_hex(
        {"branch:a": 1, "mission": 2})


def test_request_binding_and_admission_hash_through_the_same_function():
    source = inspect.getsource(event_handler)
    assert source.count("scope_epoch_digest=planning_scope_digest(epochs)") == 2
    assert "scope_epoch_digest=sha256_hex(" not in source
