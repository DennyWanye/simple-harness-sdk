# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The Action Executor (plan D7-5 / D7-5'): the only caller of connectors.

It never writes the library itself.  Every state change goes through the Commit
Service: ``begin_handoff`` re-checks the approval binding, reserves the budget and writes
HANDED_OFF (the outbox) in one transaction *before* the connector is called; the call runs
in a worker thread under ``connector_timeout_seconds`` so the event loop never blocks; a
definite refusal is FAILED, anything after the hand-off that is not a matching receipt is
UNKNOWN.  UNKNOWN is never handed off blindly: ``reconcile`` asks the connector by
idempotency key and only a CONFIRMED_NOT_STARTED answer allows one more hand-off with the
*same* key (ORCH §12.1–§12.2, §12.6; SDK effects / reconciliation semantics)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .connectors import ConnectorRejected, Receipt

if TYPE_CHECKING:
    from ..governance.policies import DeploymentPolicy
    from ..orchestrator.commit_service import CommitService


class ActionExecutor:
    def __init__(
        self,
        commit: CommitService,
        connectors: Mapping[str, Any],
        deployment: DeploymentPolicy,
        *,
        owner: str,
        lease_seconds: float | None = None,
    ) -> None:
        self._commit = commit
        self._connectors = dict(connectors)
        self._deployment = deployment
        self._owner = owner
        self._timeout = float(deployment.connector_timeout_seconds)
        # the lease outlives the call's timeout: a live hand-off is never reconciled early
        self._lease = float(lease_seconds) if lease_seconds is not None else 2 * self._timeout + 5.0
        self._inflight: set[str] = set()
        self.last_refusal: dict[str, str] = {}  # action_key -> why begin_handoff said no

    @property
    def inflight(self) -> frozenset[str]:
        """Hand-offs this process is waiting on right now (D7-5': the only in-flight kind)."""

        return frozenset(self._inflight)

    def ready(self, mission_id: str) -> list[dict[str, Any]]:
        """Actions that could be handed off now: APPROVED, or L0/L1 PROPOSED."""

        return self._commit.store.list_actions(mission_id, "APPROVED", "PROPOSED")

    async def hand_off(self, action_key: str, *, rehandoff: bool = False) -> dict[str, Any] | None:
        action, _reason = self._commit.begin_handoff(
            action_key,
            owner=self._owner,
            lease_seconds=self._lease,
            connectors=self._connectors,
            deployment=self._deployment,
            rehandoff=rehandoff,
        )
        if action is None:
            self.last_refusal[action_key] = _reason or ""
            return None
        connector = self._connectors[str(action["connector"])]
        self._inflight.add(action_key)
        call = asyncio.ensure_future(
            asyncio.to_thread(
                connector.execute,
                str(action["operation"]),
                str(action["target"]),
                dict(action["params"]),
                idempotency_key=str(action["idempotency_key"]),
            )
        )
        try:
            receipt = await asyncio.wait_for(asyncio.shield(call), timeout=self._timeout)
        except ConnectorRejected as error:  # the service said no: nothing was applied
            self._inflight.discard(action_key)
            return self._commit.record_action_outcome(
                action_key, owner=self._owner, outcome="failed", error=str(error)
            )
        except Exception as error:  # noqa: BLE001 - transport, timeout: it may have happened
            self._release_when_done(action_key, call)
            return self._commit.record_action_outcome(
                action_key,
                owner=self._owner,
                outcome="unknown",
                error=f"{type(error).__name__}: {error}",
            )
        self._inflight.discard(action_key)
        return self._commit.record_action_outcome(
            action_key,
            owner=self._owner,
            outcome="succeeded",
            receipt=receipt if isinstance(receipt, Receipt) else None,
        )

    def _release_when_done(self, action_key: str, call: asyncio.Future[Any]) -> None:
        """Review P2-4: a call that outlived its timeout is still in flight until its thread
        ends — this process never asks "did it start?" before then."""

        def finished(future: asyncio.Future[Any]) -> None:
            if not future.cancelled():
                future.exception()  # retrieved: the outcome is already booked as UNKNOWN
            self._inflight.discard(action_key)

        if call.done():
            finished(call)
        else:
            call.add_done_callback(finished)

    async def reconcile(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        """UNKNOWN actions, and hand-offs whose lease lapsed without an outcome, of every
        Mission — an ended one included (review P1-4 ⑤): reality does not stop with it."""

        settled: list[dict[str, Any]] = []
        now = self._commit.store.now
        for action in self._commit.store.list_actions(mission_id, "UNKNOWN", "HANDED_OFF"):
            key = str(action["action_key"])
            if key in self._inflight:
                continue
            if action["state"] == "HANDED_OFF" and float(action.get("lease_expires_at") or 0) > now:
                continue  # a live hand-off may still be on its way
            connector = self._connectors.get(str(action["connector"]))
            receipt: Receipt | None = None
            if connector is None or not getattr(connector, "supports_reconciliation", False):
                verdict = "STILL_UNKNOWN"
            else:
                try:
                    found = await asyncio.wait_for(
                        asyncio.to_thread(connector.lookup, str(action["idempotency_key"])),
                        timeout=self._timeout,
                    )
                except Exception:  # noqa: BLE001 - the service cannot answer: still unknown
                    verdict = "STILL_UNKNOWN"
                else:
                    receipt = found if isinstance(found, Receipt) else None
                    verdict = "COMPLETED" if receipt is not None else "CONFIRMED_NOT_STARTED"
            updated = self._commit.record_reconciliation(key, verdict=verdict, receipt=receipt)
            if (
                updated["state"] == "UNKNOWN"
                and updated.get("reconcile") == "CONFIRMED_NOT_STARTED"
            ):
                again = await self.hand_off(key, rehandoff=True)
                updated = again or self._commit.store.get_action(key) or updated
            settled.append(updated)
        return settled


__all__ = ("ActionExecutor",)
