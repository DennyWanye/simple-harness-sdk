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
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .connectors import ConnectorRejected, Receipt

if TYPE_CHECKING:
    from ..governance.policies import DeploymentPolicy
    from ..orchestrator.commit_service import CommitService


def lookup_verdict(connector: Any, found: Receipt | None) -> str:
    """What a lookup's answer is worth (P3.2 plan v3 D7, review round 1 P2-2).

    A receipt is a fact whoever gives it.  "I found nothing" only means "it never started"
    when the connector's lookup is authoritative; otherwise the action stays UNKNOWN and a
    person decides — the system never repeats a real-world action on a maybe.
    """

    if found is not None:
        return "COMPLETED"
    if connector is None or not getattr(connector, "supports_reconciliation", False):
        return "STILL_UNKNOWN"
    if getattr(connector, "lookup_authority", "best_effort") != "authoritative":
        return "STILL_UNKNOWN"
    return "CONFIRMED_NOT_STARTED"


def publication_overlaps_storage(
    publish_root: Path | str, storage_roots: Sequence[Path | str]
) -> bool:
    """Physical root overlap, including symlinks and case/Unicode filename aliases.

    ``resolve`` follows existing parent symlinks but does not canonicalize filename
    case on macOS. Compare whole normalized path segments, never string prefixes.
    The conservative alias rule also protects deployments moving between filesystems.
    """

    def parts(path: Path | str) -> tuple[str, ...]:
        return tuple(
            unicodedata.normalize("NFC", part).casefold() for part in Path(path).resolve().parts
        )

    publisher = parts(publish_root)
    for root in storage_roots:
        protected = parts(root)
        shared = min(len(publisher), len(protected))
        if publisher[:shared] == protected[:shared]:
            return True
    return False


class ActionExecutor:
    def __init__(
        self,
        commit: CommitService,
        connectors: Mapping[str, Any],
        deployment: DeploymentPolicy,
        *,
        owner: str,
        lease_seconds: float | None = None,
        source_storage_roots: Sequence[Path | str] | None = None,
        require_execution_root: Callable[[], None] | None = None,
    ) -> None:
        self._commit = commit
        self._connectors = dict(connectors)
        self._deployment = deployment
        self._owner = owner
        self._execution_root_check = require_execution_root
        # The assembly supplies the actual CAS/workspace roots. A custom/embedded
        # deployment must not silently guess their location from the SQLite filename.
        self._source_storage_roots = (
            None if source_storage_roots is None else tuple(source_storage_roots)
        )
        self._timeout = float(deployment.connector_timeout_seconds)
        # the lease outlives the call's timeout: a live hand-off is never reconciled early
        self._lease = float(lease_seconds) if lease_seconds is not None else 2 * self._timeout + 5.0
        self._inflight: set[str] = set()
        self.last_refusal: dict[str, str] = {}  # action_key -> why begin_handoff said no

    def _require_execution_root(self) -> None:
        gate = self._commit._assurance_root_gate
        if gate is not None:
            gate.require_execution()
        if self._execution_root_check is not None:
            self._execution_root_check()

    def _external_call(self, callback, *args, **kwargs):
        # Check in the actual worker thread, after the asynchronous handoff gap.
        self._require_execution_root()
        return callback(*args, **kwargs)

    @property
    def inflight(self) -> frozenset[str]:
        """Hand-offs this process is waiting on right now (D7-5': the only in-flight kind)."""

        return frozenset(self._inflight)

    def ready(self, mission_id: str) -> list[dict[str, Any]]:
        """Actions that could be handed off now: APPROVED, or L0/L1 PROPOSED."""

        return self._commit.store.list_actions(mission_id, "APPROVED", "PROPOSED")

    def _source_publish_refusal(self, action: Mapping[str, Any]) -> str | None:
        """Run inside begin_handoff's transaction, before reservation/outbox writes.

        The protected storage is shared by the whole library, including other tenants,
        completed Missions and revoked historical sources. Do not cache a negative
        result: source/domain registration can happen after this executor was built.
        """
        if action["connector"] != "file_publish":
            return None
        store = self._commit.store
        has_sources = any(
            self._commit.domain_for(mission.id).source_roots
            or store.list_sources(mission.id, active_only=False)
            for mission in store.list_missions()
        )
        if not has_sources:
            return None  # The pure code library retains its existing publishing behavior.
        root = getattr(self._connectors.get("file_publish"), "root", None)
        if not isinstance(root, (str, Path)) or not self._source_storage_roots:
            return "source_publish_root_unavailable"
        try:
            overlaps = publication_overlaps_storage(root, self._source_storage_roots)
        except (OSError, RuntimeError, TypeError, ValueError):
            return "source_publish_root_unavailable"
        return "source_publish_root_overlap" if overlaps else None

    async def hand_off(self, action_key: str, *, rehandoff: bool = False) -> dict[str, Any] | None:
        self._require_execution_root()
        action, _reason = self._commit.begin_handoff(
            action_key,
            owner=self._owner,
            lease_seconds=self._lease,
            connectors=self._connectors,
            deployment=self._deployment,
            rehandoff=rehandoff,
            deployment_guard=self._source_publish_refusal,
        )
        if action is None:
            self.last_refusal[action_key] = _reason or ""
            return None
        connector = self._connectors[str(action["connector"])]
        self._inflight.add(action_key)
        call = asyncio.ensure_future(
            asyncio.to_thread(
                self._external_call,
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

    async def _rehandoff_scoped(self, key: str, runtime: Any) -> dict[str, Any]:
        again = await self.hand_off(key, rehandoff=True)
        if again is not None:
            return again
        return self._commit.finish_proven_nonapplication(
            key,
            reason=self.last_refusal.get(key, "current_gate_refused"),
            service_authority=runtime.service_authority,
        )

    async def reconcile(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        """Use the original scoped reconciliation protocol for every live action."""
        settled = []
        for action in self._commit.store.list_actions(mission_id, "UNKNOWN", "HANDED_OFF"):
            updated = await self.reconcile_one(str(action["action_key"]), allow_rehandoff=True)
            if updated is not None:
                settled.append(updated)
        return settled

    async def reconcile_one(self, action_key: str, *, allow_rehandoff: bool = False) -> dict[str, Any] | None:
        """Observe one exact Action; TaskGraph convergence never re-sends it."""
        self._require_execution_root()
        from ..orchestrator.action_commits import ActionCommitError
        store = self._commit.store
        if store.connection.in_transaction:
            raise ActionCommitError("action reconciliation cannot run inside a Store transaction")
        action = store.get_action(action_key)
        if action is None:
            raise ActionCommitError("action reconciliation source is unavailable")
        if action["state"] not in {"UNKNOWN", "HANDED_OFF"}:
            return action
        def require_current() -> None:
            current = store.get_action(action_key)
            fields = ("mission_id", "action_id", "version", "params_hash", "idempotency_key", "handoffs")
            if current is None or any(current.get(field) != action.get(field) for field in fields):
                raise ActionCommitError("action changed during reconciliation")
        key = str(action["action_key"])
        if key in self._inflight:
            return None
        if action["state"] == "HANDED_OFF" and float(action.get("lease_expires_at") or 0) > store.now:
            return None  # a live hand-off may still be on its way
        runtime = getattr(self._commit, "_operation_materialization_runtime", None)
        if runtime is not None:
            from .operation_reconciliation import stored_negative_proof

            if stored_negative_proof(self._commit.store, action):
                return await self._rehandoff_scoped(key, runtime) if allow_rehandoff else action
        connector = self._connectors.get(str(action["connector"]))
        receipt: Receipt | None = None
        if connector is None or not getattr(connector, "supports_reconciliation", False):
            verdict = "STILL_UNKNOWN"
        else:
            try:
                found = await asyncio.wait_for(
                    asyncio.to_thread(self._external_call, connector.lookup, str(action["idempotency_key"])),
                    timeout=self._timeout,
                )
            except Exception:  # noqa: BLE001 - the service cannot answer: still unknown
                verdict = "STILL_UNKNOWN"
            else:
                receipt = found if isinstance(found, Receipt) else None
                verdict = lookup_verdict(connector, receipt)
        # New Operation links require a scoped, registered proof. Empty lookup,
        # timeout and an expired local lease cannot establish nonapplication.
        from ..storage.planning_admission_store import PlanningAdmissionStore

        link = PlanningAdmissionStore(self._commit.store).get_operation_action_link_for_action(
            key
        )
        runtime = getattr(self._commit, "_operation_materialization_runtime", None)
        if link is not None and verdict != "COMPLETED":
            verdict = "STILL_UNKNOWN"
            if runtime is not None:
                from .operation_reconciliation import context

                try:
                    with self._commit.store.read_view():
                        resolved, _profile, adapter, events = context(
                            self._commit.store, runtime, action
                        )
                    evidence = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._external_call,
                            adapter.observe,
                            action=action,
                            resolved=resolved,
                            handoff_events=events,
                            now_ms=int(self._commit.store.now * 1000),
                        ),
                        timeout=self._timeout,
                    )
                    with store.transaction():
                        require_current()
                        updated = self._commit.record_scoped_reconciliation(
                            key, evidence=evidence, adapter=adapter,
                            service_authority=runtime.service_authority)
                except Exception:  # unavailable/incomplete protocol evidence stays unresolved
                    with store.transaction():
                        require_current()
                        updated = self._commit.record_reconciliation(key, verdict="STILL_UNKNOWN")
                else:
                    # A scoped proof does not authorize a send. The normal current
                    # authority/approval/budget/cap gates still execute below.
                    if (
                        allow_rehandoff and updated["state"] == "UNKNOWN"
                        and updated.get("reconcile") == "CONFIRMED_NOT_STARTED"
                    ):
                        updated = await self._rehandoff_scoped(key, runtime)
                    return updated
        with store.transaction():
            require_current()
            updated = self._commit.record_reconciliation(key, verdict=verdict, receipt=receipt)
        if (
            allow_rehandoff and updated["state"] == "UNKNOWN"
            and updated.get("reconcile") == "CONFIRMED_NOT_STARTED"
        ):
            again = await self.hand_off(key, rehandoff=True)
            updated = again or self._commit.store.get_action(key) or updated
        return updated


__all__ = ("ActionExecutor", "lookup_verdict", "publication_overlaps_storage")
