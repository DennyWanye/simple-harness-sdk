"""Trusted, read-only FilePublish receipt interpretation for T3."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..contracts import ContractError
from ..contracts.operation_payloads import ConnectorOperationProfileV1, OperationParametersV1
from ..runtime.connectors import Receipt
from .connectors_publish import PUBLISH_NAME, FilePublishConnector, _hash, _read_nofollow


class OutcomeReceiptError(ContractError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ObservedOperationMilestone:
    milestone: str
    source_facts: Mapping[str, Any]
    covered_handoff_ids: tuple[str, ...]


class OperationReceiptAdapter(Protocol):
    adapter_id: str
    adapter_version: str
    connector: Any
    profile: ConnectorOperationProfileV1

    def interpret(self, receipt: Receipt, *, action: Mapping[str, Any],
                  parameters: OperationParametersV1, handoff_events: Sequence[Any],
                  required_milestone: str) -> ObservedOperationMilestone: ...


class FilePublishReceiptAdapterV1:
    adapter_id = "file-publish-ledger"
    adapter_version = "1"
    executed_check_ids = frozenset({"ledger-intent", "published-file-readback", "content-hash"})

    def __init__(
        self, connector: FilePublishConnector, profile: ConnectorOperationProfileV1
    ) -> None:
        if type(connector) is not FilePublishConnector:
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "unregistered connector")
        if (
            profile.connector_id != PUBLISH_NAME
            or profile.operation_name != "publish"
            or dict(profile.receipt_adapter)
            != {"id": self.adapter_id, "version": self.adapter_version}
        ):
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "profile adapter pin")
        root_hash = __import__("hashlib").sha256(str(connector.root).encode()).hexdigest()
        ledger_hash = __import__("hashlib").sha256(str(connector.ledger_path).encode()).hexdigest()
        if dict(profile.namespace) != {
            "service_id": PUBLISH_NAME,
            "account_scope": root_hash,
            "environment": f"ledger-{ledger_hash[:24]}",
        }:
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "profile namespace")
        self.connector, self.profile = connector, profile

    def interpret(
        self,
        receipt: Receipt,
        *,
        action: Mapping[str, Any],
        parameters: OperationParametersV1,
        handoff_events: Sequence[Any],
        required_milestone: str,
    ) -> ObservedOperationMilestone:
        if required_milestone not in {"FILE_PUBLISHED", "CONTENT_HASH_VERIFIED"}:
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "unsupported milestone")
        if not isinstance(receipt, Receipt) or not receipt.applied:
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "unapplied receipt")
        if (receipt.connector, receipt.operation, receipt.target, receipt.params_hash) != (
            PUBLISH_NAME,
            "publish",
            parameters.normalized_target_ref,
            parameters.params_hash,
        ):
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "receipt identity")
        if receipt.idempotency_key != str(action.get("idempotency_key")):
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "idempotency key")
        events = tuple(handoff_events)
        if len(events) != int(action.get("handoffs", 0)) or not events:
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "handoff count")
        ids = []
        ordinals: list[int] = []
        for event in events:
            payload = getattr(event, "payload", None)
            if (
                getattr(event, "type", None) != "ActionHandedOff"
                or not isinstance(payload, Mapping)
                or payload.get("action_key") != action.get("action_key")
                or payload.get("idempotency_key") != action.get("idempotency_key")
            ):
                raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "handoff event")
            ids.append(str(getattr(event, "id")))
            ordinal = payload.get("handoff")
            if type(ordinal) is not int:
                raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "handoff ordinal")
            ordinals.append(ordinal)
            if event.mission_id != action.get("mission_id"):
                raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "handoff mission")
        if (
            len(set(ids)) != len(ids)
            or any(type(value) is not int for value in ordinals)
            or sorted(ordinals) != list(range(1, len(events) + 1))
        ):
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "handoff coverage")
        found = self.connector.lookup(receipt.idempotency_key)
        if found is None or found.to_json() != receipt.to_json():
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "ledger lookup")
        expected_hash = str(parameters.effective_params.get("content_hash", ""))
        expected_size = parameters.effective_params.get("size")
        after = receipt.after
        if (
            not isinstance(after, Mapping)
            or after.get("content_hash") != expected_hash
            or after.get("bytes") != expected_size
        ):
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "receipt after")
        path = Path(str(after.get("path", ""))).absolute()
        if (
            self.connector.root != path
            and self.connector.root not in path.parents
            or _hash(_read_nofollow(path)) != expected_hash
        ):
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "readback")
        return ObservedOperationMilestone(
            required_milestone,
            {
                "receipt": receipt.to_json(),
                "path": str(path),
                "content_hash": expected_hash,
                "bytes": expected_size,
                "ledger": str(self.connector.ledger_path),
                "executed_check_ids": sorted(self.executed_check_ids),
            },
            tuple(ids),
        )


def resolve_receipt_adapter(
    profile: ConnectorOperationProfileV1, connector: Any, *, profiles: Any = None
) -> OperationReceiptAdapter:
    """Resolve only a deployment-owned adapter matching the frozen profile.

    A model/API caller cannot name an import, supply a receipt parser, or promote
    arbitrary shell output to an external-effect fact.
    """
    from .operation_payloads import require_connector_profile
    from typing import cast
    if profiles is not None:
        require_connector_profile(profiles, connector_id=profile.connector_id,
            operation_name=profile.operation_name, expected_hash=profile.content_hash())
        registered = profiles.resolve_connector_operation(profile.connector_id, profile.operation_name)
        adapter = None if registered is None else registered.outcome_adapter
        if adapter is not None:
            if (getattr(adapter, "connector", None) is not connector
                    or not isinstance(getattr(adapter, "profile", None), ConnectorOperationProfileV1)
                    or adapter.profile.content_hash() != profile.content_hash()
                    or dict(profile.receipt_adapter) != {"id": getattr(adapter, "adapter_id", None),
                                                         "version": getattr(adapter, "adapter_version", None)}
                    or not callable(getattr(adapter, "interpret", None))):
                raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "registered adapter identity")
            return cast(OperationReceiptAdapter, adapter)
    # Preserve the built-in profile's historical binding; all other connectors
    # need their explicitly registered parser and state-reading implementation.
    return FilePublishReceiptAdapterV1(connector, profile)
