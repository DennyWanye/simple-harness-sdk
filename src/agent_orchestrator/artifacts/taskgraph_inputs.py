# SPDX-License-Identifier: Apache-2.0
"""Decode persisted, fully resolved inputs without consulting today's resolver."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.htn import BoundInput, OccurrenceId, SourceRevisionPolicy, TaskRef
from ..contracts.models import ContractError
from ..contracts.semantic_base import VersionedRef
from .input_bindings import (
    AcceptedOutputsIndex, DisclosureState, InputManifest, ResolutionPolicy,
    ResolvedInputBinding, ResourceIdentity, TargetRules, _check_schema, _check_witness,
)
from ..contracts.evidence_state import ValidityWitness, WitnessPurpose
from ..graph.task_network import TaskNetworkSnapshot


def require_current_manifest_use(manifest: InputManifest, network: TaskNetworkSnapshot, *,
                                 accepted: AcceptedOutputsIndex, policy: ResolutionPolicy,
                                 witnesses: Mapping[str, ValidityWitness]) -> None:
    """Check permission for the exact frozen inputs; never resolve a newer set."""
    if (not manifest.is_frozen or not policy.require_witness or policy.allow_unknown_scope
            or policy.witness_purposes != frozenset({WitnessPurpose.START})):
        raise ContractError("TASKGRAPH_INPUT_CURRENTNESS_POLICY_INVALID")
    members = [spec for spec in network.occurrences if spec.task_id == manifest.consumer_task_ref]
    if len(members) != 1:
        raise ContractError("TASKGRAPH_INPUT_CONSUMER_AMBIGUOUS")
    consumer = network.binding_for_occurrence(members[0].occurrence_id)
    requirements = {item.requirement_id: item for item in network.data_requirements
                    if item.consumer_occurrence == members[0].occurrence_id}
    ports = {item.port_key: item for item in consumer.input_ports}
    for frozen in manifest.bindings:
        requirement = requirements.get(frozen.requirement_id)
        if (requirement is None or requirement.input_port != frozen.input_port
                or requirement.producer_occurrence != frozen.producer_occurrence
                or requirement.output_port != frozen.output_port or frozen.input_port not in ports):
            raise ContractError("TASKGRAPH_INPUT_FROZEN_REQUIREMENT_CHANGED")
        candidates = [item for item in accepted.at(frozen.producer_occurrence, frozen.output_port)
            if (item.producer_task_ref, item.producer_result_id, item.acceptance_id, item.artifact_id,
                item.content_hash, item.source_revision, item.support_revision, item.source_identity,
                item.schema_ref, item.disclosure_scope, item.provisional) ==
               (frozen.producer_task_ref, frozen.producer_result_id, frozen.acceptance_id, frozen.artifact_id,
                frozen.content_hash, frozen.source_revision, frozen.support_revision, frozen.source_identity,
                frozen.produced_schema_ref, frozen.disclosure_scope, frozen.provisional)]
        if len(candidates) != 1 or candidates[0].disclosure is not DisclosureState.DISCLOSABLE:
            raise ContractError("TASKGRAPH_FROZEN_INPUT_NOT_CURRENTLY_DISCLOSABLE")
        candidate = candidates[0]
        if candidate.provisional and frozen.input_port not in policy.provisional_ports:
            raise ContractError("TASKGRAPH_FROZEN_PROVISIONAL_INPUT_NOT_AUTHORIZED")
        if requirement.source_revision_policy is not SourceRevisionPolicy.PINNED:
            authorized = accepted.authorized_revision(frozen.producer_occurrence, frozen.output_port)
            if authorized is not None and authorized != frozen.source_revision:
                raise ContractError("TASKGRAPH_FROZEN_INPUT_REACCEPTANCE_REQUIRED")
        converter, schema_problem = _check_schema(requirement, ports[frozen.input_port], candidate, policy)
        if schema_problem is not None or converter != frozen.converter_ref:
            raise ContractError("TASKGRAPH_FROZEN_INPUT_SCHEMA_PERMISSION_CHANGED")
        _, witness_problem = _check_witness(requirement, candidate, manifest.consumer_task_ref, witnesses, policy)
        if witness_problem is not None:
            raise ContractError("TASKGRAPH_FROZEN_INPUT_WITNESS_NOT_CURRENT")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ContractError("TASKGRAPH_FROZEN_INPUT_INVALID_TEXT")
    value.encode("utf-8")
    return value


def _index(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ContractError("TASKGRAPH_FROZEN_INPUT_INVALID_INDEX")
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ContractError("TASKGRAPH_FROZEN_INPUT_INVALID_BOOLEAN")
    return value


def decode_frozen_manifest(value: Mapping[str, Any]) -> InputManifest:
    """Use the original BoundInput codec and require an exact canonical round trip.

    InputManifest has no persisted decoder. In particular, a missing ``pending``
    field or a bad stored hash must not turn into a successful empty manifest.
    """
    try:
        raw = _plain(value)
        if (not isinstance(raw, dict)
                or set(raw) != {"consumer_task_ref", "bindings", "pending", "manifest_hash"}
                or raw["pending"] != [] or not isinstance(raw["bindings"], list)):
            raise ContractError("TASKGRAPH_INPUT_NOT_FROZEN")
        consumer = TaskRef(_text(raw["consumer_task_ref"]))
        bindings = []
        for item in raw["bindings"]:
            bound = BoundInput.from_json(item["bound_input"])
            one = ResolvedInputBinding(
                binding_id=_text(item["binding_id"]), bound=bound,
                producer_task_ref=TaskRef(_text(item["producer_task_ref"])),
                producer_occurrence=OccurrenceId(_text(item["producer_occurrence"])),
                output_port=_text(item["output_port"]), support_revision=_index(item["support_revision"]),
                consumer_task_ref=TaskRef(_text(item["consumer_task_ref"])),
                input_port=_text(item["input_port"]), port_ordinal=_index(item["port_ordinal"]),
                source_identity=ResourceIdentity(namespace=_text(item["source_identity"]["namespace"]),
                                                 path=_text(item["source_identity"]["path"])),
                produced_schema_ref=VersionedRef.from_json(item["produced_schema_ref"]),
                read_policy=_text(item["read_policy"]), freshness_policy=_text(item["freshness_policy"]),
                disclosure_scope=_text(item["disclosure_scope"]),
                source_revision_policy=SourceRevisionPolicy(item["source_revision_policy"]),
                converter_ref=None if item["converter_ref"] is None else _text(item["converter_ref"]),
                requires_reacceptance=_boolean(item["requires_reacceptance"]),
                provisional=_boolean(item["provisional"]),
                witness_id=None if item["witness_id"] is None else _text(item["witness_id"]),
            )
            if one.consumer_task_ref != consumer:
                raise ContractError("TASKGRAPH_INPUT_CONSUMER_MISMATCH")
            if canonical_json(one.to_json()) != canonical_json(item):
                raise ContractError("TASKGRAPH_INPUT_BINDING_NOT_CANONICAL")
            bindings.append(one)
        manifest = InputManifest(consumer_task_ref=consumer, bindings=tuple(bindings))
        if canonical_json(manifest.to_json()) != canonical_json(raw):
            raise ContractError("TASKGRAPH_INPUT_MANIFEST_NOT_CANONICAL")
        return manifest
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise ContractError("TASKGRAPH_FROZEN_INPUT_DECODE_FAILED") from error


def encode_target_rules(rules: TargetRules) -> dict[str, Any]:
    return {"namespace": rules.namespace, "port_prefixes": dict(rules.port_prefixes),
            "preserve_source_namespace": rules.preserve_source_namespace,
            "case_insensitive": rules.case_insensitive}


def decode_target_rules(value: Mapping[str, Any]) -> TargetRules:
    """Target identity comes from the dispatch, never the recovery machine's OS."""
    try:
        raw = _plain(value)
        prefixes = raw["port_prefixes"]
        if not isinstance(prefixes, dict) or any(
            not isinstance(key, str) or not key or not isinstance(item, str)
            for key, item in prefixes.items()
        ):
            raise ContractError("TASKGRAPH_TARGET_RULES_INVALID")
        rules = TargetRules(namespace=_text(raw["namespace"]), port_prefixes=prefixes,
                            preserve_source_namespace=_boolean(raw["preserve_source_namespace"]),
                            case_insensitive=_boolean(raw["case_insensitive"]))
        if canonical_json(encode_target_rules(rules)) != canonical_json(raw):
            raise ContractError("TASKGRAPH_TARGET_RULES_NOT_CANONICAL")
        return rules
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise ContractError("TASKGRAPH_TARGET_RULES_UNAVAILABLE") from error
