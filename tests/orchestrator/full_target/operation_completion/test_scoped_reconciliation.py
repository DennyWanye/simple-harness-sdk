"""D2 scoped reconciliation with a real durable cancellation service fixture."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import operation_runtime_fixture as runtime_fixture
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.operation_payloads import NonapplicationProofKind
from agent_orchestrator.contracts.operation_reconciliation import (
    ScopedReconciliationObservationV1,
)
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from agent_orchestrator.runtime.connectors import ConnectorRejected, ConnectorTransportError, Receipt, params_hash
from agent_orchestrator.runtime.operation_payloads import ConnectorProfileRegistration
from agent_orchestrator.runtime.operation_reconciliation import (
    ReconciliationEvidence,
    ReconciliationProofError,
    stored_negative_proof,
)


def _ref(receipt_id: str, body: dict) -> TypedRef:
    return TypedRef(TypedRefKind.TOOL_RECEIPT, receipt_id, 1, content_hash_of(body))


class DurableCancellationService:
    """A test service with its own SQLite fence/cancel ledger and immutable receipts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE requests(request_id TEXT PRIMARY KEY, namespace_json TEXT NOT NULL, "
                "params_hash TEXT NOT NULL, status TEXT NOT NULL, receipt_json TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE journal(sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT "
                "NOT NULL, kind TEXT NOT NULL, document_json TEXT NOT NULL)"
            )

    def cancel_before_apply(self, request_id: str, namespace: dict, params_hash: str) -> dict:
        receipt = {
            "kind": "durable-cancellation-v1",
            "request_id": request_id,
            "namespace": namespace,
            "params_hash": params_hash,
            "final_status": "CANCELLED_BEFORE_APPLY",
            "sequence": 1,
        }
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT status,receipt_json FROM requests WHERE request_id=?",
                                  (request_id,)).fetchone()
            if previous is not None:
                if previous[0] == "CANCELLED_BEFORE_APPLY":
                    return json.loads(previous[1])
                raise ReconciliationProofError("request already crossed the send boundary")
            db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?)",
                (request_id, json.dumps(namespace, sort_keys=True), params_hash,
                 "CANCELLED_BEFORE_APPLY", json.dumps(receipt, sort_keys=True)),
            )
            db.execute(
                "INSERT INTO journal(request_id,kind,document_json) VALUES(?,?,?)",
                (request_id, "CANCEL", json.dumps(receipt, sort_keys=True)),
            )
        return receipt

    def execute(self, request_id: str, namespace: dict, params_hash: str, apply) -> object:
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,receipt_json FROM requests WHERE request_id=?",
                             (request_id,)).fetchone()
            if row is not None:
                if row[0] == "CANCELLED_BEFORE_APPLY":
                    raise ConnectorRejected("request_cancelled_before_apply")
                if row[0] == "APPLIED":
                    return Receipt.from_json(json.loads(row[1]))
                raise ConnectorTransportError("original send outcome is unknown")
            # A durable STARTED fence precedes all external effects. A crash after
            # this commit leaves cancellation unavailable, never a false negative.
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?)",
                       (request_id, json.dumps(namespace, sort_keys=True), params_hash, "STARTED", "{}"))
        receipt = apply()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE requests SET status='APPLIED',receipt_json=? WHERE request_id=?",
                       (json.dumps(receipt.to_json(), sort_keys=True), request_id))
        return receipt

    def observe(self, request_id: str) -> tuple[dict, dict]:
        stored = self.read(request_id)
        if stored is None:
            raise ReconciliationProofError("cancellation is absent")
        query = {
            "kind": "authoritative-cancellation-query-v1",
            "request_id": request_id,
            "cancellation_hash": content_hash_of(stored),
        }
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO journal(request_id,kind,document_json) VALUES(?,?,?)",
                (request_id, "QUERY", json.dumps(query, sort_keys=True)),
            )
        return stored, query

    def journal_contains(self, request_id: str, kind: str, document: dict) -> bool:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT document_json FROM journal WHERE request_id=? AND kind=?",
                (request_id, kind),
            ).fetchall()
        return json.dumps(document, sort_keys=True) in {row[0] for row in rows}

    def read(self, request_id: str) -> dict | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT receipt_json FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
        return None if row is None else json.loads(row[0])


class ScopedCancellationAdapter:
    def __init__(self, connector, profile, service: DurableCancellationService) -> None:
        self.connector = connector
        self.profile = profile
        self.service = service

    def observe(self, *, action, resolved, handoff_events, now_ms):
        self.service.cancel_before_apply(
            action["idempotency_key"], dict(self.profile.namespace), action["params_hash"]
        )
        cancellation, observation_receipt = self.service.observe(action["idempotency_key"])
        cancel_ref = _ref("cancel:" + action["idempotency_key"], cancellation)
        observation_ref = _ref("observe:" + action["idempotency_key"], observation_receipt)
        observation = ScopedReconciliationObservationV1.from_json({
            "schema_version": 1,
            "action_key": action["action_key"],
            "action_version": action["version"],
            "operation_id": str(resolved.envelope.operation_id),
            "operation_occurrence_id": str(resolved.envelope.operation_occurrence_id),
            "request_hash": str(resolved.envelope.request_hash),
            "params_hash": action["params_hash"],
            "idempotency_key": action["idempotency_key"],
            "connector_profile_hash": self.profile.content_hash(),
            "namespace": dict(self.profile.namespace),
            "normalized_target_ref": action["target"],
            "covered_handoff_ids": sorted(event.id for event in handoff_events),
            "queried_at_ms": now_ms,
            "observation_receipt_ref": observation_ref.to_json(),
            "observation_origin": "REMOTE_QUERY",
            "query_scope": {
                "namespace": dict(self.profile.namespace),
                "request_identity": action["idempotency_key"],
                "consistency_kind": "AUTHORITATIVE",
                "high_watermark": "sequence-1",
            },
            "outcome": "NOT_APPLIED_FINAL",
            "proof": {
                "kind": "SERVER_CANCELLED_BEFORE_APPLY",
                "proof_id": "cancel-proof-" + action["action_id"],
                "basis_receipt_refs": [cancel_ref.to_json()],
                "cancellation_receipt_ref": cancel_ref.to_json(),
                "server_final_status": "CANCELLED_BEFORE_APPLY",
            },
        })
        return ReconciliationEvidence(
            observation,
            {observation_ref.id: observation_receipt, cancel_ref.id: cancellation},
        )

    def verify(self, evidence, *, action, resolved, handoff_events) -> None:
        del resolved, handoff_events
        stored = self.service.read(action["idempotency_key"])
        if stored is None or stored["final_status"] != "CANCELLED_BEFORE_APPLY":
            raise ReconciliationProofError("cancellation is not durable and final")
        if stored["namespace"] != dict(self.profile.namespace):
            raise ReconciliationProofError("cancellation namespace differs")
        if stored["params_hash"] != action["params_hash"]:
            raise ReconciliationProofError("cancellation parameters differ")
        observation = evidence.receipts.get("observe:" + action["idempotency_key"])
        if not isinstance(observation, dict) or not self.service.journal_contains(
            action["idempotency_key"], "QUERY", observation
        ):
            raise ReconciliationProofError("observation query is not in the durable journal")
        if not self.service.journal_contains(
            action["idempotency_key"], "CANCEL", stored
        ):
            raise ReconciliationProofError("cancellation receipt is not in the durable journal")
        refs = evidence.observation.receipt_refs()
        if not any(
            evidence.receipts.get(ref.id) == stored for ref in refs
        ):
            raise ReconciliationProofError("original cancellation receipt is absent")


class _ProfileAdapter:
    def __init__(self, profile) -> None:
        self.profile = profile

    def operation_profile(self):
        return self.profile


class ScopedConnector:
    """The execute and cancellation decisions share one SQLite transaction protocol."""

    def __init__(self, connector, service: DurableCancellationService) -> None:
        self._connector = connector
        self.service = service
        self.name = connector.name
        self.operations = connector.operations
        self.supports_idempotency = bool(connector.supports_idempotency)
        self.supports_reconciliation = True
        self.lookup_authority = "authoritative"

    @property
    def root(self):
        return self._connector.root

    @property
    def ledger_path(self):
        return self._connector.ledger_path

    def normalize_target(self, target):
        return self._connector.normalize_target(target)

    def execute(self, operation, target, params, *, idempotency_key):
        return self.service.execute(
            idempotency_key,
            self._namespace,
            params_hash(params),
            lambda: self._connector.execute(
                operation, target, params, idempotency_key=idempotency_key
            ),
        )

    def lookup(self, idempotency_key):
        # The shared protocol is authoritative for cancellation; otherwise the wrapped
        # connector's own durable ledger remains the authority for an applied publish.
        cancelled = self.service.read(idempotency_key)
        if cancelled is not None:
            if cancelled["final_status"] != "CANCELLED_BEFORE_APPLY":
                raise ConnectorRejected("unknown scoped request state")
            return None
        return self._connector.lookup(idempotency_key)


class ScopedProfiles(runtime_fixture.BuiltinOperationProfiles):
    def __init__(self, connectors, service, original_connector_type, original_profiles_type) -> None:
        scoped = connectors["file_publish"]
        base = original_profiles_type(
            {"file_publish": original_connector_type(scoped.root, scoped.ledger_path.parent)}
        )
        self._connector = scoped
        self._source_path = Path(__file__)
        self._source_hash = hashlib.sha256(self._source_path.read_bytes()).hexdigest()
        self._implementation_ref = TypedRef(
            TypedRefKind.ARTIFACT, "scoped-cancellation-connector-source", 1, self._source_hash
        )
        self._profile = dataclasses.replace(
            base._profile,
            adapter_version=f"scoped-cancellation-{self._source_hash[:16]}",
            adapter_code_digest=self._source_hash,
            nonapplication_proofs=(NonapplicationProofKind.SERVER_CANCELLED_BEFORE_APPLY,),
            authority_source_ref=TypedRef(
                TypedRefKind.SOURCE, "scoped-cancellation-authority", 1, self._source_hash
            ),
        )
        scoped._namespace = dict(self._profile.namespace)
        self._profile_adapter = _ProfileAdapter(self._profile)
        self.scoped_adapter = ScopedCancellationAdapter(
            connectors["file_publish"], self._profile, service
        )

    def resolve_connector_operation(self, connector_id, operation_name):
        if (connector_id, operation_name) != ("file_publish", "publish"):
            return None
        return ConnectorProfileRegistration(
            adapter=self._profile_adapter,
            implementation_ref=self._implementation_ref,
            profile=self._profile,
            reconciliation_adapter=self.scoped_adapter,
        )


def _world(tmp_path, monkeypatch):
    service = DurableCancellationService(tmp_path / "cancellation.sqlite")
    holder = {}
    original_connector_type = runtime_fixture.FilePublishConnector
    original_profiles_type = runtime_fixture.BuiltinOperationProfiles
    monkeypatch.setattr(
        runtime_fixture,
        "FilePublishConnector",
        lambda root, ledger: ScopedConnector(original_connector_type(root, ledger), service),
    )

    def profiles(connectors):
        holder["profiles"] = ScopedProfiles(
            connectors, service, original_connector_type, original_profiles_type
        )
        return holder["profiles"]

    monkeypatch.setattr(runtime_fixture, "BuiltinOperationProfiles", profiles)
    fixture = runtime_fixture.materialized_file_publish(tmp_path)
    action, reason = fixture.world.service.begin_handoff(
        fixture.action["action_key"], owner="scoped-reconcile", lease_seconds=30,
        connectors=fixture.connectors, deployment=fixture.deployment,
    )
    assert action is not None and reason is None
    unknown = fixture.world.service.record_action_outcome(
        action["action_key"], owner="scoped-reconcile", outcome="unknown", error="lost reply"
    )
    return fixture, holder["profiles"].scoped_adapter, unknown


def test_registered_scoped_negative_proof_is_persisted_and_allows_not_applied(
    tmp_path, monkeypatch
) -> None:
    fixture, adapter, action = _world(tmp_path, monkeypatch)
    from agent_orchestrator.runtime.operation_reconciliation import context

    resolved, _profile, registered, events = context(
        fixture.world.store, fixture.runtime, action
    )
    evidence = adapter.observe(
        action=action, resolved=resolved, handoff_events=events,
        now_ms=int(fixture.world.store.now * 1000),
    )
    updated = fixture.world.service.record_scoped_reconciliation(
        action["action_key"], evidence=evidence, adapter=registered,
        service_authority=fixture.runtime.service_authority,
    )
    assert stored_negative_proof(fixture.world.store, updated)
    assert updated["reconcile"] == "CONFIRMED_NOT_STARTED"
    assert fixture.world.store.get_receipt(updated["scoped_reconciliation_ref"]["id"])
    with pytest.raises(ConnectorRejected, match="cancelled_before_apply"):
        fixture.connectors["file_publish"].execute(
            updated["operation"], updated["target"], updated["params"],
            idempotency_key=updated["idempotency_key"],
        )
    assert not any(fixture.publish.root.rglob("*"))


@pytest.mark.parametrize("fault", ("three_true", "missing_handoff", "wrong_hash"))
def test_caller_claims_or_inexact_evidence_cannot_create_negative_proof(
    tmp_path, monkeypatch, fault
) -> None:
    fixture, adapter, action = _world(tmp_path, monkeypatch)
    from agent_orchestrator.runtime.operation_reconciliation import context

    resolved, _profile, registered, events = context(fixture.world.store, fixture.runtime, action)
    if fault == "three_true":
        with pytest.raises((TypeError, ContractError)):
            fixture.world.service.record_scoped_reconciliation(
                action["action_key"], authoritative_not_applied=True,
                all_handoffs_covered=True, no_late_apply_proven=True,
            )
        return
    evidence = adapter.observe(
        action=action, resolved=resolved, handoff_events=events,
        now_ms=int(fixture.world.store.now * 1000),
    )
    raw = evidence.observation.to_json()
    if fault == "missing_handoff":
        raw["covered_handoff_ids"] = ["missing-handoff-event"]
    else:
        raw["params_hash"] = "0" * 64
    altered = ReconciliationEvidence(
        ScopedReconciliationObservationV1.from_json(raw), evidence.receipts
    )
    with pytest.raises(ReconciliationProofError):
        fixture.world.service.record_scoped_reconciliation(
            action["action_key"], evidence=altered, adapter=registered,
            service_authority=fixture.runtime.service_authority,
        )
    assert not stored_negative_proof(fixture.world.store, action)


def test_new_handoff_invalidates_previous_scoped_proof(tmp_path, monkeypatch) -> None:
    fixture, adapter, action = _world(tmp_path, monkeypatch)
    from agent_orchestrator.runtime.operation_reconciliation import context

    resolved, _profile, registered, events = context(fixture.world.store, fixture.runtime, action)
    evidence = adapter.observe(action=action, resolved=resolved, handoff_events=events,
                               now_ms=int(fixture.world.store.now * 1000))
    proven = fixture.world.service.record_scoped_reconciliation(
        action["action_key"], evidence=evidence, adapter=registered,
        service_authority=fixture.runtime.service_authority,
    )
    assert stored_negative_proof(fixture.world.store, proven)
    handed, reason = fixture.world.service.begin_handoff(
        action["action_key"], owner="second-handoff", lease_seconds=30,
        connectors=fixture.connectors, deployment=fixture.deployment, rehandoff=True,
    )
    assert handed is not None and reason is None and handed["handoffs"] == 2
    assert not stored_negative_proof(fixture.world.store, handed)


def test_no_registered_adapter_keeps_unknown_without_a_proof(tmp_path) -> None:
    fixture = runtime_fixture.materialized_file_publish(tmp_path)
    handed, reason = fixture.world.service.begin_handoff(
        fixture.action["action_key"], owner="no-adapter", lease_seconds=30,
        connectors=fixture.connectors, deployment=fixture.deployment,
    )
    assert handed is not None and reason is None
    unknown = fixture.world.service.record_action_outcome(
        handed["action_key"], owner="no-adapter", outcome="unknown", error="lost reply"
    )
    with pytest.raises(ReconciliationProofError, match="no registered"):
        from agent_orchestrator.runtime.operation_reconciliation import context
        context(fixture.world.store, fixture.runtime, unknown)
    assert fixture.world.store.get_action(handed["action_key"])["state"] == "UNKNOWN"
    assert not stored_negative_proof(fixture.world.store, unknown)


def test_verified_negative_with_expired_approval_finishes_original_budget(tmp_path, monkeypatch):
    import asyncio
    from agent_orchestrator.runtime.actions import ActionExecutor
    from agent_orchestrator.runtime.operation_reconciliation import context
    from agent_orchestrator.runtime.planning_operations import (
        OperationEffect, StoreOperationReader, build_operation_snapshot,
    )
    fixture, adapter, action = _world(tmp_path, monkeypatch)
    resolved, _profile, registered, events = context(fixture.world.store, fixture.runtime, action)
    evidence = adapter.observe(action=action, resolved=resolved, handoff_events=events,
                               now_ms=int(fixture.world.store.now * 1000))
    fixture.world.service.record_scoped_reconciliation(action['action_key'], evidence=evidence,
        adapter=registered, service_authority=fixture.runtime.service_authority)
    approval = fixture.world.store.get_approval(action['approval_request_id'])
    assert approval is not None
    expired_at = float(approval['expires_at']) + 1
    fixture.world.store._clock = lambda: expired_at
    executor = ActionExecutor(fixture.world.service, fixture.connectors, fixture.deployment,
                              owner='expired-rehandoff')
    [settled] = asyncio.run(executor.reconcile(fixture.world.mission.id))
    assert settled['state'] == 'FAILED' and settled['handoffs'] == 1
    assert 'approval_expired' in settled['error']
    reservation = fixture.world.service.ledger.reservation(action['reservation_subject'])
    assert reservation is not None and reservation['state'] == 'SETTLED'
    snapshot = build_operation_snapshot(fixture.world.mission.id,
                                       reader=StoreOperationReader(fixture.world.store))
    assert snapshot.effects[0][1] is OperationEffect.CONFIRMED_NOT_APPLIED
    assert not snapshot.unresolved
    assert not list(fixture.publish.root.rglob('*'))
