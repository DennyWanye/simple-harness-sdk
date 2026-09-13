# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Doc9 input aliases expand against the original frozen criterion catalogs.

This is input parsing, never evidence assessment or fuzzy correction of an ID.
Raw Provider output remains in its SDK journal; persisted Claims use full IDs.
"""

from collections.abc import Mapping
from typing import Any

from ..contracts import ContractError, Mission
from .assessments import (
    _frozen_contract,
    criterion_id,
    mission_contract_revision,
    mission_criterion_catalog,
    task_contract_revision,
)


def expand_document_claim_refs(
    raw: Mapping[str, Any], *, intent_config: Mapping[str, Any], mission: Mission,
) -> dict[str, Any]:
    claims = raw.get("claims")
    if not isinstance(claims, list) or not any(
        isinstance(claim, Mapping) and any(
            key in claim for key in ("criterion_refs", "mission_criterion_refs")
        ) for claim in claims
    ):
        return dict(raw)
    contract = _frozen_contract(intent_config)
    if raw.get("task_id") != contract["task_id"]:
        raise ContractError("document refs do not match frozen Task identity")
    revision = task_contract_revision(contract)
    if intent_config.get("task_contract_revision", revision) != revision:
        raise ContractError("document refs frozen Task revision differs")
    mission_catalog = [dict(item) for item in mission_criterion_catalog(mission)]
    if (intent_config.get("mission_contract_revision") != mission_contract_revision(mission)
            or intent_config.get("mission_criteria") != mission_catalog):
        raise ContractError("document refs frozen Mission catalog differs")
    catalogs = {
        "criterion": [criterion_id(revision, index, text)
                      for index, text in enumerate(contract["success_criteria"], 1)],
        "mission_criterion": [item["criterion_id"] for item in mission_catalog],
    }
    expanded = []
    for original in claims:
        if not isinstance(original, Mapping):
            raise ContractError("document claim must be an object")
        claim = dict(original)
        for prefix, ids in catalogs.items():
            key, canonical = prefix + "_refs", prefix + "_ids"
            if key not in claim:
                continue
            if canonical in claim:
                raise ContractError(f"{key} cannot coexist with {canonical}")
            refs = claim.pop(key)
            if (not isinstance(refs, list)
                    or any(type(ref) is not int or not 1 <= ref <= len(ids) for ref in refs)
                    or len(refs) != len(set(refs))):
                raise ContractError(f"{key} must contain unique frozen criterion ordinals")
            claim[canonical] = [ids[ref - 1] for ref in refs]
        expanded.append(claim)
    return {**raw, "claims": expanded}
