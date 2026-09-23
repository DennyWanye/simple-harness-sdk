# SPDX-License-Identifier: Apache-2.0
"""Bind shared work to its real frozen DATA versions before offering it to compile."""
from __future__ import annotations

from typing import Any

from ..artifacts.taskgraph_inputs import decode_frozen_manifest, require_current_manifest_use
from ..contracts.models import ContractError, sha256_hex
from ..contracts.state_machines import TERMINAL_ATTEMPT
from ..graph.taskgraph_sharing import SharedInputProof
from ..planning.htn.grounding import SharedGoalEntry
from ..runtime.planning_operations import SourceUnavailable
from ..storage.htn_store import HtnStore
from ..storage.taskgraph_attempt_inputs import TaskGraphAttemptInputStore


def read_shared_inputs(reader: Any, local: Any, entry: SharedGoalEntry,
                       acceptance: Any | None) -> SharedInputProof | None:
    """None means known ineligible for sharing; missing original rows raise.

    Grounding only knows the prospective method's DATA declaration. This reader
    supplies the complementary equality check on actual execution/Acceptance
    versions; validate_sharing also compares the exact declared incoming edges.
    """
    store = reader.store
    network = local.view.network
    binding = network.binding_for_occurrence(entry.occurrence_id)
    semantics = HtnStore(store)
    frozen = []
    if acceptance is not None:
        raw = semantics.get_input_manifest(acceptance.input_manifest_hash)
        if sha256_hex(raw) != acceptance.input_manifest_hash:
            raise SourceUnavailable("taskgraph_shared_acceptance_manifest_changed")
        frozen.append((str(acceptance.acceptance_id), decode_frozen_manifest(raw)))
    else:
        attempts = [item for item in store.list_attempts(str(entry.task_id))
                    if item.status not in TERMINAL_ATTEMPT]
        if not attempts:
            return None
        inputs = TaskGraphAttemptInputStore(store, revision_reader=reader.sources.history.read_revision)
        for attempt in attempts:
            origin = inputs.get_attempt_inputs(str(network.mission_id), attempt.id)
            if (origin.binding.occurrence_id != str(entry.occurrence_id)
                    or origin.binding.binding_revision != int(binding.contract_revision)
                    or origin.binding.input_binding_revision != int(binding.input_binding_revision)
                    or origin.binding.dispatch_generation != int(binding.dispatch_generation)):
                return None
            frozen.append((attempt.id, decode_frozen_manifest(origin.manifest)))
    digests = set()
    origins = []
    for identity, manifest in frozen:
        if manifest.consumer_task_ref != entry.task_id:
            raise SourceUnavailable("taskgraph_shared_input_consumer_changed")
        try:
            require_current_manifest_use(manifest, network, accepted=local.accepted_outputs.value,
                policy=local.source_input_policy.value,
                witnesses=local.view.licences.get(str(entry.task_id), {}))
        except ContractError:
            return None  # exact inputs exist but no longer have current use permission
        # Witness renewal changes permission evidence, not the frozen input's
        # source/Acceptance/schema/version identity. Keep the other fields exact.
        rows = [{key: value for key, value in item.to_json().items()
                 if key != "witness_id"} for item in manifest.bindings]
        digests.add(sha256_hex(rows))
        origins.append((identity, sha256_hex(manifest.to_json())))
    if len(digests) != 1:
        return None
    return SharedInputProof(occurrence_id=str(entry.occurrence_id),
        contract_revision=int(binding.contract_revision), input_binding_revision=int(binding.input_binding_revision),
        dispatch_generation=int(binding.dispatch_generation), semantic_inputs_hash=next(iter(digests)),
        origins=tuple(sorted(origins)))
