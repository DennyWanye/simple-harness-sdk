# SPDX-License-Identifier: Apache-2.0
"""Original Attempt transaction integration and future-handoff TaskGraph fences."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from ..artifacts.input_bindings import InputManifest, TargetRules
from ..artifacts.taskgraph_inputs import decode_frozen_manifest, decode_target_rules, encode_target_rules, require_current_manifest_use
from ..artifacts.versioning import ArtifactConflict, UpstreamInput, manifest_upstream_inputs
from ..contracts.models import Attempt, ContractError, sha256_hex
from ..graph.attempt_inputs import AttemptInputBinding, FrozenAttemptInputs
from ..graph.eligibility import EligiblePrimitiveTask, admit_for_dispatch
from ..graph.network_codec import decode
from ..graph.revision_records import HistoricalRevision
from ..graph.task_network import TaskNetworkSnapshot
from ..storage.htn_store import HtnStore
from ..storage.store import DispatchIntent, Store, StoreConflict, StoreError
from ..storage.taskgraph_attempt_inputs import TaskGraphAttemptInputStore
from ..storage.taskgraph_store import TaskGraphStore
from .hierarchical_dispatch import HierarchicalDispatch, NetworkView


def taskgraph_enabled(store: Store, mission_id: str) -> bool:
    row = store.connection.execute("SELECT kernel_version FROM taskgraph_policy_bindings WHERE mission_id=?", (mission_id,)).fetchone()
    if row is not None and row[0] != "taskgraph-exec-v2":
        raise StoreError("TASKGRAPH_KERNEL_UNSUPPORTED")
    return row is not None


def _mission_judge_tasks(store: Store, intent: DispatchIntent, view_id: str) -> set[str]:
    """Resolve the original Critic producer's view ID, which is not an Attempt.

    A missing Attempt alone never grants this exception: require the registered
    Mission judgment copy and its original Mission-account reservation event.
    Fence the actual artifact producers, not a task guessed from a view name.
    """
    prefix = f"{intent.mission_id}:judge:"
    ordinal = intent.subject_id.removeprefix(prefix)
    is_judge = (intent.subject_id.startswith(prefix) and ordinal.isascii()
                and ordinal.isdecimal() and int(ordinal) > 0)
    workspace = store.get_workspace(f"{view_id}-verify")
    rows = store.connection.execute(
        "SELECT task_id,attempt_id FROM events WHERE mission_id=? AND type='BudgetReserved' "
        "AND json_extract(payload_json,'$.subject_id')=? "
        "AND json_extract(payload_json,'$.kind')='critic'",
        (intent.mission_id, intent.subject_id)).fetchall()
    if (intent.kind != "critic" or not is_judge
            or workspace is None or workspace["kind"] != "judge"
            or workspace["mission_id"] != intent.mission_id or workspace["attempt_id"]
            or workspace["state"] != "ACTIVE"
            or len(rows) != 1 or tuple(rows[0]) != (None, None)):
        raise StoreError("TASKGRAPH_SERVICE_ATTEMPT_IDENTITY_MISSING")
    task_ids: set[str] = set()
    for artifact_id in workspace["detail"].get("artifacts", []):
        artifact = store.get_artifact(artifact_id)
        if artifact is None or artifact.mission_id != intent.mission_id:
            raise StoreError("TASKGRAPH_MISSION_JUDGE_ARTIFACT_IDENTITY_MISSING")
        task_ids.add(artifact.task_id)
    return task_ids


def require_taskgraph_unfenced(store: Store, mission_id: str, task_id: str) -> None:
    if not taskgraph_enabled(store, mission_id):
        return
    found = store.connection.execute(
        "SELECT 1 FROM taskgraph_convergence_targets t JOIN taskgraph_convergence_jobs j ON j.job_id=t.job_id "
        "WHERE t.mission_id=? AND t.task_id=? AND j.state IN ('FENCED','WAITING','READY') LIMIT 1",
        (mission_id, task_id)).fetchone()
    if found is not None:
        raise StoreConflict("TASKGRAPH_TARGET_FENCED")


def require_taskgraph_attempt_handoff(store: Store, intent: DispatchIntent, *,
                                      selection_inputs: tuple[UpstreamInput, ...] | None = None) -> None:
    """Recheck frozen input identity plus current control; no latest input resolution."""
    if intent.kind != "attempt" or not taskgraph_enabled(store, intent.mission_id):
        return
    attempt = store.get_attempt(intent.subject_id)
    if attempt is None or attempt.mission_id != intent.mission_id:
        raise StoreError("TASKGRAPH_ATTEMPT_UNAVAILABLE")
    require_taskgraph_unfenced(store, intent.mission_id, attempt.task_id)
    row = store.connection.execute("SELECT * FROM taskgraph_attempt_inputs WHERE attempt_id=?", (attempt.id,)).fetchone()
    if (row is None or row["mission_id"] != intent.mission_id or row["task_id"] != attempt.task_id
            or row["intent_id"] != intent.intent_id or row["creation_key"] != intent.creation_key
            or row["input_id"] != intent.input_id or row["frozen_input_hash"] != intent.input_hash
            or attempt.creation_key != intent.creation_key or attempt.input_id != intent.input_id
            or attempt.input_hash != intent.input_hash):
        raise StoreError("TASKGRAPH_FROZEN_ATTEMPT_IDENTITY_MISMATCH")
    # Global plan changes are irrelevant if this actual input/contract/generation
    # is preserved. Current execution permission still comes from the caller.
    binding = HtnStore(store).task_semantics_of(intent.mission_id, attempt.task_id)
    if (binding is None or int(binding.contract_revision) != row["binding_revision"]
            or binding.contract_hash != row["contract_hash"]
            or int(binding.dispatch_generation) != row["dispatch_generation"]
            or int(binding.input_binding_revision) != row["input_binding_revision"]):
        raise StoreConflict("TASKGRAPH_ATTEMPT_CONTROL_STALE")
    try:
        origin = AttemptInputBinding(**dict(row))
        if sha256_hex(origin.identity_json()) != origin.origin_hash:
            raise StoreError("TASKGRAPH_FROZEN_ORIGIN_HASH_MISMATCH")
    except ContractError as error:
        raise StoreError("TASKGRAPH_FROZEN_ORIGIN_CORRUPT") from error
    pin = store.connection.execute(
        "SELECT task_id,binding_revision,binding_hash FROM taskgraph_member_pins "
        "WHERE mission_id=? AND revision=? AND occurrence_id=?",
        (intent.mission_id, origin.source_revision, origin.occurrence_id)).fetchone()
    if pin is None or tuple(pin) != (attempt.task_id, origin.binding_revision, binding.content_hash()):
        raise StoreError("TASKGRAPH_FROZEN_ORIGIN_PIN_MISMATCH")
    manifest = store.connection.execute("SELECT manifest_json FROM input_manifests WHERE manifest_hash=?",
                                        (row["manifest_hash"],)).fetchone()
    if manifest is None or hashlib.sha256(str(manifest[0]).encode()).hexdigest() != row["manifest_hash"]:
        raise StoreError("TASKGRAPH_FROZEN_INPUT_UNAVAILABLE")
    document = json.loads(manifest[0])
    if canonical_json(document) != manifest[0]:
        raise StoreError("TASKGRAPH_FROZEN_INPUT_CORRUPT")
    try:
        frozen = decode_frozen_manifest(document)
        if str(frozen.consumer_task_ref) != attempt.task_id:
            raise StoreError("TASKGRAPH_FROZEN_INPUT_CONSUMER_MISMATCH")
        _intent_inputs(intent, frozen, source_revision=row["source_revision"],
                       manifest_hash=row["manifest_hash"], selection_inputs=selection_inputs)
    except (ContractError, ArtifactConflict) as error:
        raise StoreError("TASKGRAPH_FROZEN_INPUT_CORRUPT") from error


def _intent_inputs(intent: DispatchIntent, manifest: InputManifest, *,
                   source_revision: int, manifest_hash: str,
                   network: TaskNetworkSnapshot | None = None,
                   selection_inputs: tuple[UpstreamInput, ...] | None = None) -> tuple[UpstreamInput, ...]:
    frozen = intent.config.get("taskgraph_inputs")
    version = frozen.get("version") if isinstance(frozen, Mapping) else None
    keys = {"version", "source_revision", "manifest_hash", "target_rules"}
    if version == 2:
        keys.add("data_inputs")
    if (not isinstance(frozen, Mapping) or set(frozen) != keys
            or type(version) is not int or version not in (1, 2)
            or type(frozen["source_revision"]) is not int or frozen["source_revision"] != source_revision
            or frozen["manifest_hash"] != manifest_hash):
        raise StoreError("TASKGRAPH_DISPATCH_INPUT_IDENTITY_MISMATCH")
    rules = decode_target_rules(frozen["target_rules"])
    if intent.config.get("selection_decision_id") is not None and selection_inputs is None:
        raise StoreError("TASKGRAPH_SELECTION_SOURCE_REQUIRED")
    if intent.config.get("selection_decision_id") is None and selection_inputs:
        raise StoreError("TASKGRAPH_SELECTION_IDENTITY_MISSING")
    data_inputs = tuple(manifest_upstream_inputs(manifest, rules, network=network))
    if version == 2:
        raw = frozen["data_inputs"]
        if not isinstance(raw, (list, tuple)):
            raise StoreError("TASKGRAPH_FROZEN_DATA_INPUTS_INVALID")
        materialized = tuple(UpstreamInput.from_json(item) for item in raw)
        from collections import Counter
        base = Counter(canonical_json(item.to_json()) for item in data_inputs)
        actual = Counter(canonical_json(item.to_json()) for item in materialized)
        if not base <= actual or any(item.task_id not in {x.task_id for x in data_inputs} for item in materialized):
            raise StoreError("TASKGRAPH_FROZEN_DATA_PRODUCER_MISMATCH")
        data_inputs = materialized
    expected = (*data_inputs, *(selection_inputs or ()))
    if canonical_json(intent.config.get("inputs")) != canonical_json([item.to_json() for item in expected]):
        raise StoreError("TASKGRAPH_DISPATCH_INPUT_SET_MISMATCH")
    message = intent.config.get("message")
    if not isinstance(message, Mapping) or sha256_hex(message) != intent.input_hash:
        raise StoreError("TASKGRAPH_DISPATCH_MESSAGE_HASH_MISMATCH")
    return tuple(expected)


def _require_materialization_receipt(store: Store, intent: DispatchIntent) -> None:
    frozen = intent.config.get("taskgraph_inputs")
    if not isinstance(frozen, Mapping) or frozen.get("version") != 2:
        return  # v1 had only the immutable DATA manifest, without workspace overlays.
    command_id = "taskgraph-materialization:" + intent.subject_id
    receipt = store.get_receipt(command_id)
    row = store.connection.execute(
        "SELECT kind,subject_id,proposal_hash FROM commit_receipts WHERE commit_id=?", (command_id,)).fetchone()
    if (receipt is None or row is None or row["kind"] != "TaskGraphAttemptMaterialized"
            or row["subject_id"] != intent.subject_id or sha256_hex(receipt) != row["proposal_hash"]
            or receipt.get("attempt_id") != intent.subject_id or receipt.get("mission_id") != intent.mission_id
            or receipt.get("intent_id") != intent.intent_id or receipt.get("input_hash") != intent.input_hash
            or canonical_json(receipt.get("binding")) != canonical_json(dict(frozen))
            or canonical_json(receipt.get("inputs")) != canonical_json(intent.config.get("inputs"))):
        raise StoreError("TASKGRAPH_MATERIALIZATION_RECEIPT_MISMATCH")


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedTaskGraphAttempt:
    admission: EligiblePrimitiveTask
    manifest: InputManifest
    target_rules_json: str
    data_inputs_json: str

    def intent_binding(self) -> dict[str, Any]:
        return {"version": 2, "source_revision": int(self.admission.plan_revision),
                "manifest_hash": sha256_hex(self.manifest.to_json()),
                "data_inputs": json.loads(self.data_inputs_json),
                "target_rules": json.loads(self.target_rules_json)}


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskGraphAttemptContext:
    inputs: FrozenAttemptInputs
    history: HistoricalRevision
    manifest: InputManifest
    network: TaskNetworkSnapshot
    upstream: tuple[UpstreamInput, ...]


ExecutionRecheck = Callable[[Store, NetworkView, EligiblePrimitiveTask, InputManifest,
                             Mapping[str, Any], Sequence[Mapping[str, Any]], str], None]


class TaskGraphDispatchBinding:
    def __init__(self, store: Store, *, hierarchical: Callable[[str], HierarchicalDispatch], history: TaskGraphStore,
                 recheck_execution: ExecutionRecheck,
                 recheck_handoff: Callable[[Store, DispatchIntent], None],
                 recheck_settlement: Callable[[Store, str, str], None]) -> None:
        if not callable(hierarchical) or history.store is not store:
            raise ValueError("TaskGraph dispatch readers must share the original Store")
        self.store, self.dispatch_for, self.history = store, hierarchical, history
        self.recheck_execution = recheck_execution
        if not callable(recheck_handoff):
            raise ValueError("TaskGraph requires current handoff permission checks")
        self.recheck_handoff = recheck_handoff
        if not callable(recheck_settlement):
            raise ValueError("TaskGraph requires original physical settlement checks")
        self.recheck_settlement = recheck_settlement
        self.inputs = TaskGraphAttemptInputStore(store, revision_reader=history.read_revision)

    def require_handoff(self, intent: DispatchIntent) -> None:
        if not self.store.connection.in_transaction:
            raise StoreError("TASKGRAPH_HANDOFF_TRANSACTION_REQUIRED")
        selection_inputs = None
        if intent.kind == "attempt":
            _require_materialization_receipt(self.store, intent)
            attempt = self.store.get_attempt(intent.subject_id)
            if attempt is None:
                raise StoreError("TASKGRAPH_ATTEMPT_UNAVAILABLE")
            selection_inputs = self.selection_materials(attempt.task_id, intent.config.get("selection_decision_id"))
        require_taskgraph_attempt_handoff(self.store, intent, selection_inputs=selection_inputs)
        if intent.kind == "attempt":
            self.recheck_handoff(self.store, intent)
            context = self.read_attempt(intent.mission_id, intent.subject_id)
            moment = int(self.store.now * 1000)
            require_current_manifest_use(context.manifest, context.network,
                accepted=self.dispatch_for(intent.mission_id).accepted_outputs(intent.mission_id, context.network),
                policy=self.dispatch_for(intent.mission_id).resolution_policy_for(intent.mission_id, now_ms=moment),
                witnesses=self.dispatch_for(intent.mission_id).input_witnesses(intent.mission_id, context.inputs.binding.task_id))
        else:
            # Original service producers carry explicit Task/goal/Attempt links.
            # Fencing their next request does not discard an already handed-off
            # reply or move its charge to another account.
            task_ids = {intent.config[key] for key in ("task_id", "goal_task_id")
                        if intent.config.get(key) is not None}
            attempt_id = intent.config.get("attempt_id")
            if attempt_id is not None:
                attempt = self.store.get_attempt(attempt_id)
                if attempt is None:
                    task_ids.update(_mission_judge_tasks(self.store, intent, attempt_id))
                elif attempt.mission_id != intent.mission_id:
                    raise StoreError("TASKGRAPH_SERVICE_ATTEMPT_IDENTITY_MISSING")
                else:
                    task_ids.add(attempt.task_id)
            for task_id in task_ids:
                task = self.store.get_task(task_id)
                if task is None:
                    from ..contracts.htn import TaskForm
                    semantic = HtnStore(self.store).task_semantics_of(intent.mission_id, task_id)
                    if semantic is None or semantic.form is not TaskForm.COMPOUND:
                        raise StoreError("TASKGRAPH_SERVICE_TASK_IDENTITY_MISSING")
                elif task.mission_id != intent.mission_id:
                    raise StoreError("TASKGRAPH_SERVICE_TASK_IDENTITY_MISSING")
                require_taskgraph_unfenced(self.store, intent.mission_id, task_id)

    def selection_materials(self, task_id: str, decision_id: object) -> tuple[UpstreamInput, ...]:
        if decision_id is None:
            return ()
        if not isinstance(decision_id, str) or not decision_id:
            raise StoreError("TASKGRAPH_SELECTION_DECISION_INVALID")
        task = self.store.get_task(task_id)
        receipt = self.store.get_receipt(decision_id)
        row = self.store.connection.execute("SELECT kind,subject_id FROM commit_receipts WHERE commit_id=?",
                                            (decision_id,)).fetchone()
        if (task is None or receipt is None or row is None or tuple(row) != ("selection_decision", task_id)
                or receipt.get("task_id") != task_id or receipt.get("receipt_id") != decision_id
                or receipt.get("action") != "synthesize"):
            raise StoreError("TASKGRAPH_SELECTION_DECISION_SOURCE_MISMATCH")
        # Original Selection verifies the persisted candidate hashes and original
        # artifact bytes. These materials remain separate from accepted DATA.
        artifacts = self.dispatch_for(task.mission_id).commit.selection_input_artifacts(decision_id)
        if any(item.mission_id != task.mission_id or item.task_id != task_id for item in artifacts):
            raise StoreError("TASKGRAPH_SELECTION_ARTIFACT_OWNER_MISMATCH")
        return tuple(UpstreamInput(item.task_id, item.path, item.content_hash, item.id) for item in artifacts)

    def prepare(self, task_id: str, *, intent_config: Mapping[str, Any],
                inputs: Sequence[Mapping[str, Any]], input_hash: str,
                selection_decision_id: str | None = None) -> PreparedTaskGraphAttempt:
        if not self.store.connection.in_transaction:
            raise StoreError("TASKGRAPH_DISPATCH_REQUIRES_ATTEMPT_TRANSACTION")
        task = self.store.get_task(task_id)
        if task is None:
            raise StoreError("TASKGRAPH_TASK_MISSING")
        require_taskgraph_unfenced(self.store, task.mission_id, task_id)
        moment = int(self.store.now * 1000)
        view = self.dispatch_for(task.mission_id).read(task.mission_id, now_ms=moment)
        record = self.history.read_revision(task.mission_id, int(view.network.plan_revision)).record
        candidates = [item for item in view.network.occurrences if str(item.task_id) == task_id]
        if len(candidates) != 1 or view.accepted is None:
            raise StoreError("TASKGRAPH_DISPATCH_SOURCE_INCOMPLETE")
        occurrence = candidates[0]
        # NEXT-TG-1.0 §5.2: the resolution this read's report was judged against.
        result = view.resolutions.get(occurrence.occurrence_id)
        if result is None or result.problems or result.manifest is None or not result.manifest.is_frozen:
            raise StoreError("TASKGRAPH_INPUT_NOT_FROZEN")
        manifest = result.manifest
        rules = self.dispatch_for(task.mission_id).target_rules_for(task.id)
        if not isinstance(rules, TargetRules):
            raise StoreError("TARGET_RULES_UNAVAILABLE")
        data_inputs = self.dispatch_for(task.mission_id).overlay_attempt_inputs(task.mission_id,
            manifest_upstream_inputs(manifest, rules, network=view.network))
        expected = (*data_inputs,
                    *self.selection_materials(task_id, selection_decision_id))
        if canonical_json([dict(item) for item in inputs]) != canonical_json([item.to_json() for item in expected]):
            raise StoreConflict("TASKGRAPH_DISPATCH_INPUT_SET_MISMATCH")
        message = intent_config.get("message")
        if not isinstance(message, Mapping) or sha256_hex(message) != input_hash:
            raise StoreConflict("TASKGRAPH_DISPATCH_MESSAGE_HASH_MISMATCH")
        admission = admit_for_dispatch(view.reports[occurrence.occurrence_id], view.views[occurrence.occurrence_id],
                                       view.plan, manifest, now_ms=moment)
        pin = next((item for item in record.pins.member_pins if item.occurrence_id == str(occurrence.occurrence_id)), None)
        if pin is None or pin.task_id != task_id or pin.binding_revision != int(admission.contract_revision):
            raise StoreConflict("TASKGRAPH_DISPATCH_PIN_STALE")
        # Actual execution authority, current operation/running-work completeness,
        # current budgets/resources and the materialized request/manifest relationship
        # are rechecked by the original installed execution adapter, never plan grant.
        if intent_config.get("selection_decision_id") not in (None, selection_decision_id):
            raise StoreConflict("TASKGRAPH_EXECUTION_SELECTION_IDENTITY_CHANGED")
        execution_config = {**dict(intent_config), "selection_decision_id": selection_decision_id}
        self.recheck_execution(self.store, view, admission, manifest, execution_config, inputs, input_hash)
        prepared = PreparedTaskGraphAttempt(admission=admission, manifest=manifest,
                                            target_rules_json=canonical_json(encode_target_rules(rules)),
                                            data_inputs_json=canonical_json([item.to_json() for item in data_inputs]))
        supplied = intent_config.get("taskgraph_inputs")
        if supplied is not None and canonical_json(supplied) != canonical_json(prepared.intent_binding()):
            raise StoreConflict("TASKGRAPH_DISPATCH_INPUT_IDENTITY_MISMATCH")
        return prepared

    def read_attempt(self, mission_id: str, attempt_id: str) -> TaskGraphAttemptContext:
        """Read the recorded origin for recovery/review, without resolving current inputs."""
        with self.store.read_view():
            frozen = self.inputs.get_attempt_inputs(mission_id, attempt_id)
            history = self.history.read_revision(mission_id, frozen.binding.source_revision)
            network = decode(history.record.document.to_json()).snapshot
            manifest = decode_frozen_manifest(frozen.manifest)
            if str(manifest.consumer_task_ref) != frozen.binding.task_id:
                raise StoreError("TASKGRAPH_FROZEN_INPUT_CONSUMER_MISMATCH")
            intent = self.store.get_intent_for_subject(attempt_id)
            if intent is None or intent.intent_id != frozen.binding.intent_id:
                raise StoreError("TASKGRAPH_DISPATCH_INTENT_UNAVAILABLE")
            _require_materialization_receipt(self.store, intent)
            upstream = _intent_inputs(intent, manifest,
                                      source_revision=frozen.binding.source_revision,
                                      manifest_hash=frozen.binding.manifest_hash, network=network,
                                      selection_inputs=self.selection_materials(frozen.binding.task_id,
                                          intent.config.get("selection_decision_id")))
            return TaskGraphAttemptContext(inputs=frozen, history=history, manifest=manifest,
                                           network=network, upstream=upstream)

    def record(self, prepared: PreparedTaskGraphAttempt, attempt: Attempt, intent: DispatchIntent) -> None:
        admission = prepared.admission
        if (str(admission.task_id) != attempt.task_id or str(admission.mission_id) != attempt.mission_id
                or intent.subject_id != attempt.id):
            raise StoreConflict("TASKGRAPH_DISPATCH_ATTEMPT_MISMATCH")
        digest = HtnStore(self.store).insert_input_manifest(attempt.mission_id, attempt.task_id,
            prepared.manifest.to_json(), attempt_id=attempt.id,
            input_binding_revision=int(admission.input_binding_revision))
        self.inputs.insert_attempt_inputs(attempt_id=attempt.id, occurrence_id=str(admission.occurrence_id),
            source_revision=int(admission.plan_revision), binding_revision=int(admission.contract_revision),
            manifest_hash=digest, created_at=self.store.now)
        receipt = {"version": 2, "kind": "TaskGraphAttemptMaterialized", "attempt_id": attempt.id,
                   "mission_id": attempt.mission_id, "intent_id": intent.intent_id,
                   "input_hash": intent.input_hash, "binding": prepared.intent_binding(),
                   "inputs": [dict(item) for item in intent.config["inputs"]]}
        self.store.insert_receipt(commit_id="taskgraph-materialization:" + attempt.id,
            kind="TaskGraphAttemptMaterialized", subject_id=attempt.id, base_version=attempt.version,
            proposal_hash=sha256_hex(receipt), receipt=receipt)
