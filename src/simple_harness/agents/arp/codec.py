# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strict structural + semantic codec over the frozen ``runtime-plane.schema.json``.

The schema file is the single field source (FIELD-CONTRACTS §1).  ``validate``
implements exactly the JSON-Schema subset the contract uses (``$ref``, ``oneOf``,
``anyOf``, ``const``, ``enum``, typed scalars with bool≠int and finite numbers,
``required``/``additionalProperties: false``, array/string/number limits,
``uniqueItems``).  ``validate_semantics`` adds the context-free rules of the
contract (FIELD-CONTRACTS §5); current authorisation and database identity remain
producer/reader obligations and are never simulated here.

``decode(name, raw)`` is the only entry for bytes coming from a database column, a
Host request or a model tool call: size limit → strict parse → structure → semantics.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from functools import lru_cache
from importlib import resources
from typing import Any, Mapping

from . import purge_rules, retrieval_status
from .errors import ArpError
from .rules import complete_disposal, embedding_mode, validate_bundle_paths
from .strict import canonical, digest, parse_strict

Json = Mapping[str, Any]

_LARGE_8MIB = ("ContextManifest", "ProtocolGroupSnapshot", "ContextRecallRequest")
_MAX_BYTES: Mapping[str, int] = {
    **{name: 8 * 1024 * 1024 for name in _LARGE_8MIB},
    "ContextRecallResult": 2 * 1024 * 1024,
    "PurgeProgress": 16 * 1024,
}
DEFAULT_MAX_BYTES = 256 * 1024
PAGE_TYPES = (
    "SearchPage",
    "ManagementSearchPage",
    "ContextSearchPage",
    "HistoryReadPage",
    "CataloguePage",
    "SkillDetailsPage",
    "ContextManifestPage",
)
SEARCH_PAGE_TYPES = ("SearchPage", "ManagementSearchPage", "ContextSearchPage")
CATALOGUE_ITEM_TYPES = (
    "CatalogueSummary",
    "CapabilityCatalogueItem",
    "ToolCatalogueItem",
    "SkillCatalogueItem",
)


def _load(name: str) -> Any:
    raw = resources.files(__package__).joinpath(f"contracts/{name}").read_bytes()
    return json.loads(raw.decode("utf-8"))


@lru_cache(maxsize=1)
def schema() -> Mapping[str, Any]:
    document = _load("runtime-plane.schema.json")
    if not isinstance(document, dict) or "$defs" not in document:
        raise RuntimeError("runtime-plane schema is missing $defs")
    return document


@lru_cache(maxsize=1)
def definitions() -> Mapping[str, Any]:
    return schema()["$defs"]


@lru_cache(maxsize=1)
def host_verbs() -> Mapping[str, Any]:
    return _load("host-verbs.json")


@lru_cache(maxsize=1)
def model_tools() -> Mapping[str, Any]:
    return _load("model-tools.json")


@lru_cache(maxsize=1)
def event_catalogue() -> Mapping[str, Any]:
    return _load("event-catalogue.json")


def definition(name: str) -> Mapping[str, Any]:
    try:
        return definitions()[name]
    except KeyError as error:
        raise RuntimeError(f"unknown ARP schema type {name!r}") from error


def max_bytes_for(name: str) -> int:
    return _MAX_BYTES.get(name, DEFAULT_MAX_BYTES)


# ---- structural ------------------------------------------------------------------

_TYPE_PREDICATES = {
    "null": lambda v: v is None,
    "boolean": lambda v: type(v) is bool,
    "integer": lambda v: type(v) is int,
    "number": lambda v: type(v) in (int, float) and math.isfinite(v),
    "string": lambda v: type(v) is str,
    "array": lambda v: type(v) is list,
    "object": lambda v: type(v) is dict,
}


def _resolve(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    while "$ref" in spec:
        spec = definition(spec["$ref"].split("/")[-1])
    return spec


def validate(value: Any, spec: Mapping[str, Any], *, path: str = "$") -> None:
    """Structural validation of ``value`` against a schema fragment (or ``$ref``)."""

    spec = _resolve(spec)
    for keyword in ("oneOf", "anyOf"):
        if keyword in spec:
            passed = 0
            for branch in spec[keyword]:
                try:
                    validate(value, branch, path=path)
                    passed += 1
                except ArpError:
                    continue
            if passed < 1 or (keyword == "oneOf" and passed != 1):
                raise ArpError("UNION_MISMATCH", field_path=path)
            return
    if "const" in spec and (type(value) is not type(spec["const"]) or value != spec["const"]):
        raise ArpError("CONST", field_path=path)
    if "enum" in spec and not any(type(value) is type(v) and value == v for v in spec["enum"]):
        raise ArpError("ENUM", field_path=path)
    types = spec.get("type", [])
    types = [types] if isinstance(types, str) else types
    if types and not any(_TYPE_PREDICATES[t](value) for t in types):
        raise ArpError("TYPE", field_path=path)
    if type(value) is dict:
        properties = spec.get("properties", {})
        missing = set(spec.get("required", ())) - value.keys()
        if missing:
            raise ArpError("MISSING_FIELD", field_path=f"{path}.{sorted(missing)[0]}")
        additional = spec.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], path=f"{path}.{key}")
            elif additional is False:
                raise ArpError("UNKNOWN_FIELD", field_path=f"{path}.{key}")
            elif isinstance(additional, dict):
                validate(item, additional, path=f"{path}.{key}")
    elif type(value) is list:
        if len(value) > spec.get("maxItems", 10**9) or len(value) < spec.get("minItems", 0):
            raise ArpError("ARRAY_LIMIT", field_path=path)
        if spec.get("uniqueItems") and len({canonical(v) for v in value}) != len(value):
            raise ArpError("DUPLICATE_ITEM", field_path=path)
        items = spec.get("items", {})
        for index, item in enumerate(value):
            validate(item, items, path=f"{path}[{index}]")
    elif type(value) is str:
        if len(value) > spec.get("maxLength", 10**9) or len(value) < spec.get("minLength", 0):
            raise ArpError("STRING_LIMIT", field_path=path)
        if "pattern" in spec and not re.search(spec["pattern"], value):
            raise ArpError("STRING_PATTERN", field_path=path)
    elif type(value) in (int, float):
        if (
            not math.isfinite(value)
            or value < spec.get("minimum", float("-inf"))
            or value > spec.get("maximum", float("inf"))
        ):
            raise ArpError("NUMBER_LIMIT", field_path=path)


# ---- semantic ---------------------------------------------------------------------


def _need(ok: object, code: str = "STATE_COMBINATION_INVALID") -> None:
    if not ok:
        raise ArpError(code)


def _validate_nested(value: Any, spec: Mapping[str, Any]) -> None:
    if "$ref" in spec:
        validate_semantics(spec["$ref"].split("/")[-1], value)
        return
    for keyword in ("oneOf", "anyOf"):
        if keyword in spec:
            for branch in spec[keyword]:
                try:
                    validate(value, branch)
                except ArpError:
                    continue
                _validate_nested(value, branch)
                break
            return
    if isinstance(value, dict):
        properties = spec.get("properties", {})
        additional = spec.get("additionalProperties", {})
        for key, child in value.items():
            sub = properties.get(key, additional)
            if isinstance(sub, dict):
                _validate_nested(child, sub)
    elif isinstance(value, list):
        items = spec.get("items", {})
        for child in value:
            _validate_nested(child, items)


def validate_semantics(name: str, value: Any) -> None:  # noqa: C901 - one rule table
    """Context-free semantic rules; nested named types are checked recursively."""

    _validate_nested(value, definition(name))
    need = _need
    if name in PAGE_TYPES:
        need(value["has_more"] == (value["next_cursor"] is not None))
    if name in SEARCH_PAGE_TYPES:
        retrieval_status.validate_page(value)
    if name == "RetrievalReceipt":
        retrieval_status.validate_receipt(value)
    if name == "SearchCoverage":
        retrieval_status.validate_coverage(value)
    if name == "ContextRecallResult":
        retrieval_status.validate_aggregate(value)
    if name == "RetrievalSummary":
        retrieval_status.validate_summary(value)
    if name == "ContextRecallRequest":
        limits = value["limits"]
        need(
            value["deadline_ms"] == value["created_at_ms"] + limits["total_budget_ms"],
            "RECALL_BINDING_INVALID",
        )
        ids = sorted(set(value["mandatory_group_ids"]) | set(value["protected_group_ids"]))
        need(value["exclusions_hash"] == digest(ids), "RECALL_BINDING_INVALID")
        need(
            value["query_hash"] == hashlib.sha256(value["query_text"].encode("utf-8")).hexdigest(),
            "RECALL_BINDING_INVALID",
        )
        need(
            (value["embedding_resource_ref"] is None) == (value["embedding_fingerprint"] is None),
            "RECALL_BINDING_INVALID",
        )
        need(
            len(value["mandatory_group_ids"]) == len(set(value["mandatory_group_ids"]))
            and len(value["protected_group_ids"]) == len(set(value["protected_group_ids"])),
            "DUPLICATE_ITEM",
        )
    if name == "ContextRecallCheckpoint":
        need(value["created_at_ms"] <= value["last_observed_at_ms"], "RECALL_BINDING_INVALID")
        need(
            value["next_wake_at_ms"] <= value["deadline_ms"]
            and value["scanned_chunks"] <= value["snapshot_chunks"],
            "RECALL_BINDING_INVALID",
        )
        need(
            len(value["page_refs"])
            == value["scan_pages_committed"] + value["result_pages_committed"],
            "RECALL_BINDING_INVALID",
        )
        need(
            (value["terminal_error_code"] is not None) == (value["phase"] in ("BLOCKED", "STALE")),
            "RECALL_BINDING_INVALID",
        )
        if value["phase"] == "PREPARING":
            need(
                value["mode"] is None and not value["page_refs"] and value["cursor_token"] is None,
                "RECALL_BINDING_INVALID",
            )
        if value["phase"] in ("SCANNING", "FETCHING_RESULTS", "READY"):
            need(
                value["index_snapshot_ref"] is not None and value["mode"] is not None,
                "RECALL_BINDING_INVALID",
            )
        if value["phase"] == "READY":
            need(
                value["cursor_token"] is None and value["result_pages_committed"] >= 1,
                "RECALL_BINDING_INVALID",
            )
    if name == "ContextRecallProgress":
        need(
            value["scanned_chunks"] <= value["snapshot_chunks"]
            and value["next_wake_at_ms"] <= value["deadline_ms"],
            "RECALL_BINDING_INVALID",
        )
    if name == "PurgeProgress":
        purge_rules.validate_progress(value)
    if name == "SessionView":
        progress = value["purge_progress"]
        if progress is not None:
            purge_rules.validate_progress(progress)
            need(
                progress["session_id"] == value["session_id"]
                and progress["agent_id"] == value["agent_id"]
                and progress["control_generation"] == value["generation"],
                "PURGE_IDENTITY_MISMATCH",
            )
        if value["state"] in ("DRAINING", "PURGING", "PURGED"):
            need(progress is not None, "PURGE_STATE_INVALID")
            phases = {
                "DRAINING": ("DRAINING",),
                "PURGING": ("RENAME_PENDING", "RENAMED", "DELETE_CONFIRMED"),
                "PURGED": ("DELETE_CONFIRMED",),
            }
            need(progress["phase"] in phases[value["state"]], "PURGE_STATE_INVALID")  # type: ignore[index]
            if value["state"] == "PURGED":
                need(progress["blocking"] is None, "PURGE_STATE_INVALID")  # type: ignore[index]
        else:
            need(progress is None, "PURGE_STATE_INVALID")
    if name == "HistoryReadRequest":
        need(value["seq_from"] <= value["seq_to"], "HISTORY_BYTE_RANGE_INVALID")
    if name == "HistorySlice":
        need(
            0 <= value["utf8_start"] <= value["utf8_end"] <= value["total_utf8_bytes"],
            "HISTORY_BYTE_RANGE_INVALID",
        )
        raw = value["text"].encode("utf-8")
        need(len(raw) == value["utf8_end"] - value["utf8_start"], "HISTORY_BYTE_RANGE_INVALID")
        need(hashlib.sha256(raw).hexdigest() == value["slice_hash"], "REF_IDENTITY_MISMATCH")
        need(
            value["record_complete"]
            == (value["utf8_start"] == 0 and value["utf8_end"] == value["total_utf8_bytes"])
        )
    if name == "HistoryReadPage":
        need(value["requested_seq_from"] <= value["requested_seq_to"])
        for item in value["items"]:
            validate_semantics("HistorySlice", item)
    if name == "TokenReceipt":
        if value["prior_output_reserve_tokens"] > 0:
            need(value["prior_basis_ref"] is not None, "PRIOR_RESERVE_UNAVAILABLE")
        need(value["wire_input_tokens"] <= value["max_input_budget"], "FINAL_CONTEXT_OVERFLOW")
    if name == "JournalGroup":
        need(value["seq_from"] <= value["seq_to"])
        need(len(set(value["record_ids"])) == len(value["record_ids"]))
        if value["kind"] == "CLOSED_TOOL":
            need(
                value["closed"]
                and value["closure_receipt_ref"] is not None
                and len(set(value["call_ids"])) == len(value["call_ids"])
                and len(set(value["result_call_ids"])) == len(value["result_call_ids"])
                and set(value["call_ids"]) == set(value["result_call_ids"])
            )
        if value["kind"] in ("OPEN_TAIL", "OPAQUE_REQUIRED"):
            need(value["mandatory"])
    if name == "ProtocolGroupSnapshot":
        for group in value["groups"]:
            validate_semantics("JournalGroup", group)
        group_ids = [g["group_id"] for g in value["groups"]]
        need(len(group_ids) == len(set(group_ids)))
        need(
            set(value["mandatory_group_ids"])
            == {g["group_id"] for g in value["groups"] if g["mandatory"]}
        )
        need(
            any(
                g["kind"] == "USER_ANCHOR" and g["mandatory"] and g["turn_id"] == value["current_turn_id"]
                for g in value["groups"]
            )
        )
    if name == "ContextManifest":
        receipt = value["token_receipt"]
        validate_semantics("TokenReceipt", receipt)
        need(value["planned_request_hash"] == receipt["request_hash"], "REQUEST_HASH_MISMATCH")
        need(
            value["input_token_charge"] == receipt["wire_input_tokens"]
            and value["prior_output_reserve_tokens"] == receipt["prior_output_reserve_tokens"],
            "BAD_TOKEN_RECEIPT",
        )
        need(value["reserved_output_tokens"] == receipt["requested_output_tokens"], "BAD_TOKEN_RECEIPT")
        need(value["recent_complete_turn_count"] == len(set(value["selected_turn_ids"])))
    if name in CATALOGUE_ITEM_TYPES:
        need(value["kind"].lower() == value["definition_ref"]["kind"], "CATALOGUE_KIND_MISMATCH")
        need(len(canonical(value)) <= 4096, "ITEM_TOO_LARGE")
        if value["access_view"] == "MODEL":
            need("credential_ref_name" not in value and "endpoint_namespace" not in value)
    if name in ("Skill", "SkillDetailsPage"):
        validate_bundle_paths(x["relative_path"] for x in value["files"])
    if name == "ContextSettingsView":
        need(value["view_revision"] == value["adoption_revision"])
    if name == "ContextSummaryView":
        summary = value["retrieval_summary"]
        if summary is not None:
            retrieval_status.validate_summary(summary)
            need(
                value["retrieval_status"] == summary["status"]
                and value["recall_count"] == summary["selected_count"],
                "RETRIEVAL_STATE_INVALID",
            )
        elif value["context_id"] is not None:
            need(
                value["retrieval_status"] == "NOT_REQUESTED" and value["recall_count"] == 0,
                "RETRIEVAL_STATE_INVALID",
            )
        if value["context_id"] is None:
            need(summary is None, "RETRIEVAL_STATE_INVALID")
            need(
                value["manifest_ref"] is None
                and value["manifest_policy_ref"] is None
                and value["last_count_mode"] is None
            )
    if name == "RuntimeProfile":
        embedding_mode(
            value["context_policy"]["embedding_required_for_activation"],
            value["allow_lexical_degradation"],
            creating=True,
            available=True,
        )
        validate_semantics("Policy", value["context_policy"])
    if name == "Policy":
        need(set(value["section_soft_caps"]) == set("ABCDE"), "POLICY_INVALID")
        need(value["embedding_overlap_tokens"] < value["embedding_chunk_tokens"], "POLICY_INVALID")
    if name == "DependencyLock":
        need(value["complete"] == (len(value["unresolved"]) == 0))
    if name == "CollectionWitness":
        need(value["count"] == len(value["refs"]))
        need(value["set_hash"] == digest(value["refs"]), "REF_IDENTITY_MISMATCH")
    if name == "CompleteSessionDisposal":
        for witness in value["collections"]:
            validate_semantics("CollectionWitness", witness)
        need(value["all_safe"] == complete_disposal(value["collections"]))
    if name == "PreparedRequestDisposition":
        if value["replacement_ordinal"] is not None:
            need(
                value["action"] == "CANCEL_UNSENT"
                and value["no_send_receipt_ref"] is not None
                and value["reservation_settlement_ref"] is not None,
                "REQUEST_UNSENT_TERMINATION_REQUIRED",
            )
        if value["action"] == "RECONCILE_ORIGINAL":
            need(value["replacement_ordinal"] is None)
    if name == "EmbeddingCallReceipt":
        need((value["status"] == "SUCCEEDED") == (value["output_ref"] is not None))
        if value["pricing_mode"] == "NO_PROVIDER_CHARGE":
            need(value["cost_micros"] == 0)
        if value["status"] == "UNKNOWN":
            need(value["output_ref"] is None)
    if name == "HostRequest":
        table = host_verbs()
        if value["verb"] not in table:
            raise ArpError("UNSUPPORTED_HOST_VERB")
        entry = table[value["verb"]]
        need((value["payload"] is None) != (value["payload_ref"] is None), "HOST_PAYLOAD_MISMATCH")
        payload = value["payload"]
        if payload is not None:
            validate(payload, definition(entry["request_type"]), path="$.payload")
            validate_semantics(entry["request_type"], payload)
            if value["verb"] == "agent_context_settings_update":
                need(
                    value["expected_revision"] == payload["expected_adoption_revision"],
                    "EXPECTED_REVISION_MISMATCH",
                )
            if "cursor" in payload:
                need(value["cursor"] == payload["cursor"], "CURSOR_REQUEST_MISMATCH")
            if "limit" in payload:
                need(value["limit"] == payload["limit"], "CURSOR_REQUEST_MISMATCH")
    if name == "HostResponse":
        table = host_verbs()
        if value["request_verb"] not in table:
            raise ArpError("UNSUPPORTED_HOST_VERB")
        entry = table[value["request_verb"]]
        if value["error"] is not None:
            need(not value["items"] and value["command_receipt"] is None)
        else:
            need(len(value["items"]) == 1, "HOST_PAYLOAD_MISMATCH")
            item = value["items"][0]
            validate(item, definition(entry["response_type"]), path="$.items[0]")
            validate_semantics(entry["response_type"], item)
            need(
                entry["access"] != "WRITE" or value["command_receipt"] is not None,
                "HOST_PAYLOAD_MISMATCH",
            )
            if "next_cursor" in item:
                need(value["next_cursor"] == item["next_cursor"])


# ---- entry points --------------------------------------------------------------------


def check(name: str, value: Any) -> Any:
    """Structural + semantic validation of an in-memory value; returns it unchanged."""

    validate(value, definition(name))
    validate_semantics(name, value)
    return value


def decode(name: str, raw: bytes) -> Any:
    """Bytes → validated value for the named contract type."""

    value = parse_strict(raw, max_bytes=max_bytes_for(name))
    return check(name, value)


def encode(name: str, value: Any) -> bytes:
    """Validated value → canonical bytes (what gets stored and hashed)."""

    check(name, value)
    raw = canonical(value)
    if len(raw) > max_bytes_for(name):
        raise ArpError("DOCUMENT_TOO_LARGE")
    return raw


def content_hash(name: str, value: Any) -> str:
    return hashlib.sha256(encode(name, value)).hexdigest()


__all__ = (
    "CATALOGUE_ITEM_TYPES",
    "DEFAULT_MAX_BYTES",
    "PAGE_TYPES",
    "check",
    "content_hash",
    "decode",
    "definition",
    "definitions",
    "encode",
    "event_catalogue",
    "host_verbs",
    "max_bytes_for",
    "model_tools",
    "schema",
    "validate",
    "validate_semantics",
)
