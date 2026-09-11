# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Connectors to external systems (ORCH-BUILD §9.1–9.2, §12.3; original §15, §21–22;
plan D7-6).

A connector is the only way the orchestrator touches a system outside the workspace.
The model never calls one: the Action Executor does, after the policy and the approvals
allow it (plan D7-1).  Every operation declares its risk level (original §22, L0–L3);
a connector that cannot deduplicate by idempotency key and answer "did this key
happen?" (reconciliation) is never enabled for L2/L3 work (ORCH §12.3: "不支持幂等/核对的
高风险连接器保持禁用或人工处理").

``TestConfigService`` is the dedicated *test* service of the step-7 demo (ORCH §9.1
"修改测试配置项"): a file-backed key/value store with an idempotency ledger.  Passing
against it grants nothing for production.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from simple_harness.contracts import canonical_json

LEVELS = ("L0", "L1", "L2", "L3")


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else len(LEVELS) - 1  # unknown = strictest


def params_hash(params: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(params)).encode("utf-8")).hexdigest()


class ConnectorRejected(RuntimeError):
    """A definite refusal: the external system did *not* apply the operation."""


class ConnectorTransportError(RuntimeError):
    """The outcome is unknown: the operation may or may not have been applied."""


@dataclass(frozen=True, slots=True)
class OperationSpec:
    name: str
    level: (
        str  # L0 read-only / L1 sandbox reversible / L2 modification / L3 payment-delete-sensitive
    )
    required_params: tuple[str, ...] = ()
    mutates: bool = True
    # plan D7-2': "state" sets the target to a value (running it again changes nothing
    # more); "event" makes a new fact each time (append, pay).  Step 7 runs state only.
    kind: str = "state"
    cost_micros_ceiling: int | None = None  # per call; None = unpriced (never 0)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "required_params": list(self.required_params),
            "mutates": self.mutates,
            "kind": self.kind,
            "cost_micros_ceiling": self.cost_micros_ceiling,
        }


@dataclass(frozen=True, slots=True)
class Receipt:
    """What the external system says happened for one idempotency key."""

    idempotency_key: str
    connector: str
    operation: str
    target: str
    params_hash: str
    applied: bool
    before: Any = None
    after: Any = None
    service_ref: str = ""
    applied_at: float = 0.0

    @property
    def receipt_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_json(with_hash=False)).encode("utf-8")
        ).hexdigest()

    def to_json(self, *, with_hash: bool = True) -> dict[str, Any]:
        body = {
            "idempotency_key": self.idempotency_key,
            "connector": self.connector,
            "operation": self.operation,
            "target": self.target,
            "params_hash": self.params_hash,
            "applied": self.applied,
            "before": self.before,
            "after": self.after,
            "service_ref": self.service_ref,
            "applied_at": self.applied_at,
        }
        if with_hash:
            body["receipt_hash"] = self.receipt_hash
        return body

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Receipt:
        return cls(
            idempotency_key=str(value["idempotency_key"]),
            connector=str(value["connector"]),
            operation=str(value["operation"]),
            target=str(value["target"]),
            params_hash=str(value["params_hash"]),
            applied=bool(value["applied"]),
            before=value.get("before"),
            after=value.get("after"),
            service_ref=str(value.get("service_ref", "")),
            applied_at=float(value.get("applied_at", 0.0)),
        )


class Connector(Protocol):
    """``supports_reconciliation`` says a ``lookup`` can be asked at all; how much its
    answer is worth is ``lookup_authority`` (P3.2 plan v3 D7, review round 2 P2-6):

    * ``"authoritative"`` — applying an operation and recording its idempotency key happen
      atomically, one key is never executed concurrently, and the record cannot be rolled
      back by anyone else.  Only then does "no record" mean "it never started", which is
      what lets the system hand the action off again with the same key.
    * ``"best_effort"`` (the default) — the service may simply not know.  A lookup that
      finds nothing leaves the action UNKNOWN for a person; it is never retried blindly.

    An action at L2 or above may only run on an authoritative connector.
    """

    name: str
    operations: Mapping[str, OperationSpec]
    supports_idempotency: bool
    supports_reconciliation: bool
    lookup_authority: str  # "authoritative" | "best_effort"

    def normalize_target(self, target: str) -> str: ...

    def execute(
        self, operation: str, target: str, params: Mapping[str, Any], *, idempotency_key: str
    ) -> Receipt: ...

    def lookup(self, idempotency_key: str) -> Receipt | None: ...


@dataclass
class TestConfigService:
    """The step-7 test service (ORCH §9.1): ``read`` L0, ``set`` L2, ``delete`` L3.

    State and the idempotency ledger live in one JSON file; an operation repeated with
    the same idempotency key returns the original receipt and is not applied again.
    Fault injection for the tests: ``lose_receipt_after_apply`` applies the change and
    then raises a transport error (the caller cannot tell it happened);
    ``reject_next`` refuses the next operation without applying it."""

    path: Path
    name: str = "test_config"
    supports_idempotency: bool = True
    supports_reconciliation: bool = True
    # P3.2 D7: state and ledger live in one file written under an exclusive lock, so a key
    # that is not in the ledger really was never applied
    lookup_authority: str = "authoritative"
    lose_receipt_after_apply: int = 0
    reject_next: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    __test__ = False  # not a pytest test class

    operations: Mapping[str, OperationSpec] = field(
        default_factory=lambda: {
            "read": OperationSpec("read", "L0", (), mutates=False),
            "set": OperationSpec("set", "L2", ("value",)),
            "delete": OperationSpec("delete", "L3", ()),
        }
    )

    def normalize_target(self, target: str) -> str:
        return ".".join(part.strip() for part in str(target).strip().split("."))

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """One writer at a time, across threads and processes (plan D7-5': the ledger
        write and the change are one atomic replace, so ``lookup`` is authoritative)."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.path.with_suffix(".lock"), "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"config": {}, "ledger": {}, "applied_count": 0}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)

    def state(self) -> dict[str, Any]:
        with self._exclusive():
            return self._load()

    def execute(
        self, operation: str, target: str, params: Mapping[str, Any], *, idempotency_key: str
    ) -> Receipt:
        import time

        with self._exclusive():
            self.calls.append(
                {"operation": operation, "target": target, "idempotency_key": idempotency_key}
            )
            if self.reject_next > 0:
                self.reject_next -= 1
                raise ConnectorRejected(f"test_config refused {operation} {target}")
            spec = self.operations.get(operation)
            if spec is None:
                raise ConnectorRejected(f"unknown operation {operation}")
            missing = [p for p in spec.required_params if p not in params]
            if missing:
                raise ConnectorRejected(f"{operation}: missing params {missing}")
            state = self._load()
            existing = state["ledger"].get(idempotency_key)
            if existing is not None:  # the same key never applies twice
                receipt = Receipt.from_json(existing)
            else:
                config = state["config"]
                before = config.get(target)
                if operation == "set":
                    config[target] = params["value"]
                elif operation == "delete":
                    config.pop(target, None)
                after = config.get(target)
                receipt = Receipt(
                    idempotency_key=idempotency_key,
                    connector=self.name,
                    operation=operation,
                    target=target,
                    params_hash=params_hash(params),
                    applied=spec.mutates,
                    before=before,
                    after=after,
                    service_ref=f"test-config#{len(state['ledger']) + 1}",
                    applied_at=time.time(),
                )
                if spec.mutates:
                    state["ledger"][idempotency_key] = receipt.to_json(with_hash=False)
                    state["applied_count"] = int(state.get("applied_count", 0)) + 1
                    self._save(state)
            if self.lose_receipt_after_apply > 0 and spec.mutates:
                self.lose_receipt_after_apply -= 1
                raise ConnectorTransportError("test_config: connection reset after apply")
            return receipt

    def lookup(self, idempotency_key: str) -> Receipt | None:
        with self._exclusive():
            entry = self._load()["ledger"].get(idempotency_key)
            return None if entry is None else Receipt.from_json(entry)


@dataclass
class PaymentConnectorStub:
    """A high-risk connector without idempotency or reconciliation: the deployment must
    refuse to enable it (ORCH §12.3; original §21.2 "财务 Agent → 不能直接支付")."""

    name: str = "payment"
    supports_idempotency: bool = False
    supports_reconciliation: bool = False
    operations: Mapping[str, OperationSpec] = field(
        default_factory=lambda: {"pay": OperationSpec("pay", "L3", ("amount",), kind="event")}
    )

    def normalize_target(self, target: str) -> str:
        return str(target).strip()

    def execute(self, operation, target, params, *, idempotency_key):  # type: ignore[no-untyped-def]
        raise ConnectorRejected("payment connector is not enabled in this build")

    def lookup(self, idempotency_key: str) -> Receipt | None:
        return None


__all__ = (
    "LEVELS",
    "Connector",
    "ConnectorRejected",
    "ConnectorTransportError",
    "OperationSpec",
    "PaymentConnectorStub",
    "Receipt",
    "TestConfigService",
    "level_rank",
    "params_hash",
)
