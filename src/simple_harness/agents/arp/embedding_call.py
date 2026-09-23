"""One embedding call = one persisted intent + one persisted receipt (C2 / J2).

The intent is written *before* the port is called under the call key. A restart
that finds the intent without a receipt records the call as UNKNOWN instead of
calling again: the coordinator then degrades (query) or retries under a fresh
attempt ordinal (index job), never behind the caller's back. Receipt fields are
observed, not assumed: elapsed time is measured, pricing/usage are taken from
the port when it declares them and left null otherwise.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from . import store
from .codec import check
from .pins import Pin
from .strict import digest

CALL_KIND = "embedding_call"
INTENT_KIND = "embedding_intent"


def read_call(connection: Any, call_key: str) -> Mapping[str, Any] | None:
    return store.read_original_receipt(connection, kind=CALL_KIND, receipt_key=call_key)


def perform_call(
    uow: Any,
    *,
    run_id: str,
    call_key: str,
    purpose: str,
    texts: Sequence[str],
    port: Any,
    deployment_ref: Pin,
    clock: Callable[[], float],
    clock_ms: Callable[[], int],
    fault: Callable[[str], None] | None = None,
    fault_name: str | None = None,
) -> Mapping[str, Any]:
    """Return the persisted receipt for ``call_key`` (existing, UNKNOWN-after-crash, or fresh)."""

    connection = uow.database.connection
    existing = read_call(connection, call_key)
    if existing is not None:
        return existing
    input_hash = digest(list(texts))
    intent = store.read_original_receipt(connection, kind=INTENT_KIND, receipt_key=call_key)
    if intent is not None:
        # The call may have left the process before its result was persisted.
        status, output, detail, elapsed = "UNKNOWN", None, "result_lost_before_receipt", 0
    else:
        with uow.database.transaction() as txn:
            store.append_original_receipt_locked(
                txn, run_id=run_id, kind=INTENT_KIND, receipt_key=call_key,
                body={"call_key": call_key, "purpose": purpose, "input_hash": input_hash, "input_count": len(texts)}, now=clock(),
            )
        if fault is not None and fault_name is not None:
            fault(fault_name)
        started = clock_ms()
        try:
            output = [list(map(float, v)) for v in port.embed(list(texts))]
            status, detail = "SUCCEEDED", None
            if len(output) != len(texts):
                status, output, detail = "FAILED", None, "output_count_mismatch"
        except Exception as error:  # noqa: BLE001 - the failure is recorded, never hidden
            status, output, detail = "FAILED", None, type(error).__name__
        elapsed = max(0, clock_ms() - started)
    usage = _usage_of(port)
    receipt = {
        "schema_version": 1,
        "invocation_ref": Pin("invocation", call_key, 0, digest({"call": call_key})).to_json(),
        "resource_ref": deployment_ref.to_json(),
        "purpose": purpose,
        "input_hash": input_hash,
        "input_count": len(texts),
        "status": status,
        "output_ref": None if output is None else Pin("artifact", f"embedding:{call_key}", 0, digest(output)).to_json(),
        "input_tokens": usage.get("input_tokens"),
        "elapsed_ms": elapsed,
        "pricing_mode": usage.get("pricing_mode", "NO_PROVIDER_CHARGE"),
        "cost_micros": usage.get("cost_micros"),
        "usage_ref": Pin("receipt", f"usage:{call_key}", 0, digest({"call": call_key, "status": status, "usage": usage})).to_json(),
    }
    check("EmbeddingCallReceipt", receipt)
    body = {**receipt, "output": output, "detail": detail}
    with uow.database.transaction() as txn:
        store.append_original_receipt_locked(txn, run_id=run_id, kind=CALL_KIND, receipt_key=call_key, body=body, now=clock())
    return body


def record_lost_call_locked(connection: Any, *, run_id: str, call_key: str, intent: Mapping[str, Any], deployment_ref: Pin, now: float) -> Mapping[str, Any]:
    """An intent whose result never came back (and whose job will not run again) is
    settled as an UNKNOWN receipt in the same ledger a re-run would have written."""

    receipt = {
        "schema_version": 1,
        "invocation_ref": Pin("invocation", call_key, 0, digest({"call": call_key})).to_json(),
        "resource_ref": deployment_ref.to_json(),
        "purpose": str(intent["purpose"]),
        "input_hash": str(intent["input_hash"]),
        "input_count": int(intent["input_count"]),
        "status": "UNKNOWN",
        "output_ref": None,
        "input_tokens": None,
        "elapsed_ms": 0,
        "pricing_mode": "NO_PROVIDER_CHARGE",
        "cost_micros": 0,
        "usage_ref": Pin("receipt", f"usage:{call_key}", 0, digest({"call": call_key, "status": "UNKNOWN", "usage": {}})).to_json(),
    }
    check("EmbeddingCallReceipt", receipt)
    body = {**receipt, "output": None, "detail": "result_lost_before_receipt"}
    store.append_original_receipt_locked(connection, run_id=run_id, kind=CALL_KIND, receipt_key=call_key, body=body, now=now)
    return body


def list_open_intents(connection: Any, run_id: str) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """Intents of one Run that have no call receipt yet: ``(call_key, intent body)``."""

    rows = connection.execute(
        "SELECT payload_json FROM run_events WHERE run_id=? AND kind=? ORDER BY durable_seq", (run_id, f"arp.{INTENT_KIND}.v1")
    ).fetchall()
    result = []
    for (raw,) in rows:
        payload = store.load_json(raw)
        key = str(payload["receipt_key"])
        if read_call(connection, key) is None:
            result.append((key, payload["body"]))
    return tuple(result)


def _usage_of(port: Any) -> dict[str, Any]:
    """What the port itself declares about its last call; nothing is invented.

    The contract fixes ``NO_PROVIDER_CHARGE`` ⇔ ``cost_micros == 0``; a port that does
    not declare ``pricing_mode = "METERED"`` is unmetered by that definition. A metered
    port must report ``last_usage`` (input_tokens / cost_micros); missing values stay null.
    """

    usage: dict[str, Any] = {}
    metered = getattr(port, "pricing_mode", None) == "METERED"
    usage["pricing_mode"] = "METERED" if metered else "NO_PROVIDER_CHARGE"
    usage["cost_micros"] = None if metered else 0
    last = getattr(port, "last_usage", None)
    if isinstance(last, Mapping):
        tokens = last.get("input_tokens")
        if isinstance(tokens, int) and tokens >= 0:
            usage["input_tokens"] = tokens
        cost = last.get("cost_micros")
        if metered and isinstance(cost, int) and cost >= 0:
            usage["cost_micros"] = cost
    return usage


def receipt_pin(call_key: str, receipt: Mapping[str, Any]) -> Pin:
    return Pin("receipt", f"{CALL_KIND}:{call_key}", 0, digest({k: v for k, v in receipt.items() if k != "output"}))


__all__ = ("CALL_KIND", "INTENT_KIND", "list_open_intents", "perform_call", "read_call", "receipt_pin", "record_lost_call_locked")
