# SPDX-License-Identifier: Apache-2.0
"""Original Commit import of actually executed local verifier assertions.

The deployment registry supplies exact registered CheckSpecs and the current
root guard. This module never accepts a Host/model boolean as a check result.
Original result, input manifest, CAS and event/receipt writers remain owners.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..artifacts.store import ArtifactStore
from ..assurance.check_specs import (
    DOCUMENT_NAMES,
    EXECUTOR_LAYERS,
    LOCAL_LAYERS,
    CheckSpec,
    layer_spec,
)
from ..assurance.codec import AssuranceError, canonical, decode, digest, fingerprint, text
from ..assurance.executor_checks import OUTPUT_SCHEMA
from ..assurance.local_checks import RecordedLocalCheck, validate_local_check
from ..assurance.refs import AssuranceRef, Pin
from ..contracts import Artifact
from ..storage.assurance_reads import AssuranceReader
from ..storage.assurance_work import atomic
from ..verification.assurance_local import LocalLayerBinding, LocalVerificationRecorder

if TYPE_CHECKING:
    from .commit_service import CommitService


@dataclass(frozen=True, slots=True)
class _SourceNames:
    payload_key: str
    ref_kind: str
    event_type: str
    receipt_kind: str
    commit_prefix: str
    artifact_prefix: str
    artifact_type: str
    artifact_dir: str


_LOCAL_NAMES = _SourceNames(
    "local_check",
    "local_check_receipt",
    "AssuranceLocalCheckFinished",
    "AssuranceLocalCheckImported",
    "assurance-local-check:",
    "assurance-check-output",
    "assurance-local-check-output",
    ".assurance/checks",
)
_EXECUTOR_NAMES = _SourceNames(
    "executor_check",
    "execution_receipt",
    "AssuranceExecutionImported",
    "AssuranceExecutorCheckImported",
    "assurance-executor-check:",
    "assurance-executor-check-output",
    "assurance-executor-check-output",
    ".assurance/executor-checks",
)


@dataclass(frozen=True, slots=True)
class RegisteredLayer:
    """Exact CheckSpec whose payload declares only the implemented layer assertion."""

    layer: str
    binding: LocalLayerBinding

    def require_registered(self, reader: AssuranceReader) -> None:
        metadata = reader.read_exact_metadata(self.binding.spec_ref)
        event = decode(metadata.body_json)
        definition = event["payload"]
        spec = CheckSpec.from_json(definition)
        if (
            spec != layer_spec(self.layer, self.binding.implementation_hash)
            or spec.assertion_key != self.binding.assertion_key
            or spec.max_runtime_ms != self.binding.max_runtime_ms
        ):
            raise AssuranceError("CHECKER_REGISTRY_IDENTITY")


class LocalCheckImporter:
    def __init__(
        self,
        commit: CommitService,
        *,
        tenant_id: str,
        result_ref: AssuranceRef,
        input_manifest_ref: AssuranceRef,
        environment_hash: str,
        recorder_implementation_hash: str,
        registry: Mapping[str, RegisteredLayer],
        cas: ArtifactStore,
        require_current_root: Callable[[], None],
        bind_scoped_check: Callable[[AssuranceRef], AssuranceRef],
    ) -> None:
        if result_ref.kind != "result" or input_manifest_ref.kind != "input_manifest":
            raise AssuranceError("CHECK_SOURCE_IDENTITY")
        stored = commit.store.get_result(result_ref.pin.id)
        if stored is None:
            raise AssuranceError("SOURCE_UNAVAILABLE", result_ref.pin.id)
        self.commit = commit
        self.mission_id = stored.envelope.mission_id
        self.reader = AssuranceReader(commit.store, tenant_id=tenant_id, mission_id=self.mission_id)
        self.result_ref = result_ref
        self.manifest_ref = input_manifest_ref
        self.environment_hash = digest(environment_hash)
        self.recorder_hash = digest(recorder_implementation_hash)
        self.registry = dict(registry)
        if not set(self.registry) <= {*LOCAL_LAYERS, *EXECUTOR_LAYERS} or any(
            key != value.layer for key, value in self.registry.items()
        ):
            raise AssuranceError("CHECKER_REGISTRY_IDENTITY")
        self.cas = cas
        self.require_current_root = require_current_root
        if not callable(bind_scoped_check):
            raise AssuranceError("CHECK_SCOPE_IMPORTER_REQUIRED")
        self.bind_scoped_check = bind_scoped_check
        self._validate_sources()

    def _validate_sources(self) -> None:
        self.require_current_root()
        with self.commit.store.read_view() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM assurance_mission_bindings WHERE mission_id=?",
                    (self.mission_id,),
                ).fetchone()
                is None
            ):
                raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
            self.reader.read_exact_metadata(self.result_ref)
            manifest = decode(self.reader.read_exact_metadata(self.manifest_ref).body_json)
            if (
                manifest.get("schema") != "assurance-local-verification-input-v1"
                or manifest.get("result_id") != self.result_ref.pin.id
                or manifest.get("subject_hash") != self.result_ref.pin.content_hash
                or manifest.get("mission_id") != self.mission_id
                or manifest.get("tenant_id") != self.reader.tenant_id
            ):
                raise AssuranceError("CHECK_INPUT_MANIFEST_MISMATCH")
            for entry in self.registry.values():
                entry.require_registered(self.reader)

    def _outside_transaction(self) -> None:
        if self.commit.store.connection.in_transaction:
            raise AssuranceError("LOCAL_CHECK_INSIDE_TRANSACTION")
        self._validate_sources()

    def recorder(self) -> LocalVerificationRecorder:
        return LocalVerificationRecorder(
            mission_id=self.mission_id,
            subject_hash=self.result_ref.pin.content_hash,
            input_manifest_hash=self.manifest_ref.pin.content_hash,
            environment_hash=self.environment_hash,
            recorder_implementation_hash=self.recorder_hash,
            bindings={
                key: entry.binding for key, entry in self.registry.items() if key in LOCAL_LAYERS
            },
            now_ms=lambda: int(self.commit.store.now * 1000),
            assert_outside_transaction=self._outside_transaction,
            persist=self.import_actual,
            executor_bindings={
                key: entry.binding for key, entry in self.registry.items() if key in EXECUTOR_LAYERS
            },
            persist_executor=self.import_executor,
        )

    def import_actual(self, recorded: RecordedLocalCheck) -> AssuranceRef:
        """Trusted recorder sink: exact replay imports once, a changed run conflicts."""
        return self._import(recorded, executor=False, document=None)

    def import_executor(
        self, recorded: RecordedLocalCheck, document: Mapping[str, Any]
    ) -> AssuranceRef:
        """Trusted sink for a run the sandbox executor already made; never re-executes."""
        if not isinstance(document, Mapping) or document.get("schema") != OUTPUT_SCHEMA:
            raise AssuranceError("CHECK_ASSERTION_INVALID")
        return self._import(recorded, executor=True, document=dict(document))

    def _import(
        self, recorded: RecordedLocalCheck, *, executor: bool, document: dict[str, Any] | None
    ) -> AssuranceRef:
        self._outside_transaction()
        body = decode(recorded.receipt_json)
        spec = AssuranceRef.from_json(body["check_spec_ref"], kinds={"check_spec"})
        entries = [entry for entry in self.registry.values() if entry.binding.spec_ref == spec]
        if len(entries) != 1 or (entries[0].layer in EXECUTOR_LAYERS) != executor:
            raise AssuranceError("CHECKER_REGISTRY_IDENTITY")
        registered = entries[0]
        # Validate the whole receipt through the same decoder as its consumer.
        validate_local_check(
            body,
            expected_spec=spec,
            expected_subject_hash=self.result_ref.pin.content_hash,
            expected_mission_id=self.mission_id,
            assertion_key=registered.binding.assertion_key,
        )
        if (
            body["recorder_implementation_hash"] != self.recorder_hash
            or body["input_manifest_hash"] != self.manifest_ref.pin.content_hash
            or body["environment_hash"] != self.environment_hash
            or body["assertions"] != [item.to_json() for item in recorded.outputs]
        ):
            raise AssuranceError("CHECK_SOURCE_IDENTITY")
        if any(item.assertion_key != registered.binding.assertion_key for item in recorded.outputs):
            raise AssuranceError("CHECK_ASSERTION_INVALID")
        if executor:
            # The assertion output is exactly the executor facts document.
            if len(recorded.outputs) != 1 or recorded.outputs[0].output != canonical(
                document
            ).encode("utf-8"):
                raise AssuranceError("CHECK_ASSERTION_INVALID")
        names = _EXECUTOR_NAMES if executor else _LOCAL_NAMES
        # The actual output bytes go to the original CAS outside the write lock.
        stored = self.commit.store.get_result(self.result_ref.pin.id)
        if stored is None:
            raise AssuranceError("SOURCE_UNAVAILABLE", self.result_ref.pin.id)
        outputs = []
        run_id = text(body["run_nonce"])
        # IDs/paths are derived from a canonical digest, not arbitrary run text.
        run_key = fingerprint({"mission_id": self.mission_id, "run_nonce": run_id})
        for ordinal, output in enumerate(recorded.outputs):
            if output.output is None:
                continue
            blob_hash = self.cas.put_bytes(output.output)
            outputs.append(
                Artifact(
                    id=f"{names.artifact_prefix}:{run_key}:{ordinal}",
                    mission_id=self.mission_id,
                    task_id=stored.envelope.task_id,
                    attempt_id=stored.envelope.attempt_id,
                    type=names.artifact_type,
                    path=f"{names.artifact_dir}/{run_key}/{ordinal}.json",
                    version=1,
                    content_hash=blob_hash,
                    size_bytes=len(output.output),
                    produced_by="orchestrator",
                    storage_uri=str(self.cas.path_for(blob_hash)),
                    created_at=body["finished_at_ms"] / 1000,
                )
            )
        payload: dict[str, Any] = {
            names.payload_key: body,
            "result_ref": self.result_ref.to_json(),
            "input_manifest_ref": self.manifest_ref.to_json(),
            "outputs": [
                AssuranceRef("artifact", Pin(a.id, a.version, a.content_hash)).to_json()
                for a in outputs
            ],
        }
        if executor:
            assert document is not None
            payload["execution"] = {
                "targets": list(document["targets"]),
                "execution_ids": [
                    None if row["receipt"] is None else row["receipt"]["execution_id"]
                    for row in document["runs"]
                ],
                "environment_digests": sorted(
                    {
                        str(row["receipt"]["environment_digest"])
                        for row in document["runs"]
                        if row["receipt"] is not None
                    }
                ),
                "execution_state": document["execution_state"],
            }
        payload_hash = fingerprint(payload)
        commit_id = f"{names.commit_prefix}{run_key}"
        with atomic(self.commit.store):
            self._validate_sources()
            old = self.commit.store.get_receipt(commit_id)
            if old is not None:
                if (
                    set(old)
                    != {"mission_id", "source_hash", "event_ref", "run_nonce", "result_ref"}
                    or old.get("source_hash") != payload_hash
                    or old.get("mission_id") != self.mission_id
                    or old.get("run_nonce") != run_id
                    or old.get("result_ref") != self.result_ref.to_json()
                ):
                    raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT", commit_id)
                ref = AssuranceRef.from_json(old["event_ref"], kinds={names.ref_kind})
                event = decode(self.reader.read_exact_metadata(ref).body_json)
                receipt_row = self.commit.store.connection.execute(
                    "SELECT kind,subject_id,base_version,proposal_hash FROM commit_receipts "
                    "WHERE commit_id=?",
                    (commit_id,),
                ).fetchone()
                if (
                    event["payload"] != payload
                    or event["task_id"] != stored.envelope.task_id
                    or event["attempt_id"] != stored.envelope.attempt_id
                    or receipt_row["kind"] != names.receipt_kind
                    or receipt_row["subject_id"] != self.result_ref.pin.id
                    or receipt_row["base_version"] != 0
                    or receipt_row["proposal_hash"] != payload_hash
                ):
                    raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT", commit_id)
                self.bind_scoped_check(ref)
                return ref
            for artifact in outputs:
                existing = self.commit.store.get_artifact(artifact.id)
                if existing is not None and canonical(existing.to_json()) != canonical(
                    artifact.to_json()
                ):
                    raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT", artifact.id)
                self.commit.store.upsert_artifact(artifact)
            event = self.commit._emit(
                names.event_type,
                self.mission_id,
                key=run_key,
                task_id=stored.envelope.task_id,
                attempt_id=stored.envelope.attempt_id,
                payload=payload,
            )
            ref = AssuranceRef(names.ref_kind, Pin(event.id, 0, fingerprint(event.to_json())))
            receipt = {
                "mission_id": self.mission_id,
                "source_hash": payload_hash,
                "event_ref": ref.to_json(),
                "run_nonce": run_id,
                "result_ref": self.result_ref.to_json(),
            }
            self.commit.store.insert_receipt(
                commit_id=commit_id,
                kind=names.receipt_kind,
                subject_id=self.result_ref.pin.id,
                base_version=0,
                proposal_hash=payload_hash,
                receipt=receipt,
            )
            self.bind_scoped_check(ref)
            return ref


class AssuranceLocalChecks:
    """Deployment-owned adapter used by the original verification entry point.

    Only two local algorithm definitions are installed. No policy is approved here,
    and neither definition attests arbitrary semantic criteria or executor outcomes.
    """

    def __init__(
        self,
        commit: CommitService,
        *,
        tenant_id: str,
        cas: ArtifactStore,
        require_current_root: Callable[[], None],
    ) -> None:
        import hashlib
        import platform
        import sys
        from pathlib import Path

        self.commit = commit
        self.tenant_id = text(tenant_id)
        self.cas = cas
        self.require_current_root = require_current_root
        self.code_root = Path(__file__).parent.parent
        # Code-owned paths, not module/ref names supplied by a request.
        self.paths = (
            "verification/verifier_router.py",
            "verification/deterministic_checks.py",
            "verification/domain_handlers.py",
            "verification/assessments.py",
            "verification/adapters.py",
            "verification/evidence_resolver.py",
            "verification/assurance_local.py",
            "assurance/local_checks.py",
            "assurance/executor_checks.py",
            "assurance/check_specs.py",
            "runtime/sandbox.py",
            "runtime/tool_gateway.py",
            "assurance/check_bindings.py",
            "orchestrator/assurance_local_checks.py",
            "orchestrator/assurance_check_import.py",
        ) + tuple("assurance/schemas/" + name for name in sorted(DOCUMENT_NAMES))
        self.hashes = {
            path: hashlib.sha256((self.code_root / path).read_bytes()).hexdigest()
            for path in self.paths
        }
        self.checker_hash = fingerprint(self.hashes)
        self.recorder_hash = fingerprint(
            {
                path: self.hashes[path]
                for path in self.paths
                if "assurance" in path or path == "assurance/local_checks.py"
            }
        )
        self.environment_hash = fingerprint(
            {
                "python": sys.version,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "implementation": platform.python_implementation(),
                "checker_sources": self.hashes,
            }
        )
        # Trusted deployment registration only; not a Host/model-selectable importer.
        commit._assurance_check_importer = self

    def import_check_locked(
        self,
        *,
        mission_id: str,
        execution_ref: AssuranceRef,
        completion_scope: AssuranceRef,
    ) -> AssuranceRef:
        from .assurance_check_import import import_local_check_locked

        return import_local_check_locked(
            self,
            mission_id=mission_id,
            execution_ref=execution_ref,
            completion_scope=completion_scope,
        )

    def _require_deployment(self) -> None:
        import hashlib

        self.require_current_root()
        for path, expected in self.hashes.items():
            if hashlib.sha256((self.code_root / path).read_bytes()).hexdigest() != expected:
                raise AssuranceError("CHECKER_DEPLOYMENT_CHANGED", path)

    def _read_registry(self, mission_id: str) -> dict[str, RegisteredLayer]:
        """Read deployed registrations only; a validity read must not create facts."""
        reader = AssuranceReader(self.commit.store, tenant_id=self.tenant_id, mission_id=mission_id)
        registry = {}
        with self.commit.store.read_view() as connection:
            reader._mission_locked(connection)
            for layer in (*LOCAL_LAYERS, *EXECUTOR_LAYERS):
                definition = layer_spec(layer, self.checker_hash).to_json()
                key = fingerprint({"mission_id": mission_id, "definition": definition})
                row = connection.execute(
                    "SELECT * FROM commit_receipts WHERE commit_id=?",
                    ("assurance-check-spec:" + key,),
                ).fetchone()
                if row is None:
                    raise AssuranceError("CHECKER_UNAVAILABLE", layer)
                body = decode(row["receipt_json"])
                ref = AssuranceRef.from_json(body.get("event_ref"), kinds={"check_spec"})
                if (
                    row["kind"] != "AssuranceCheckSpecRegistered"
                    or row["subject_id"] != ref.pin.id
                    or row["base_version"] != 0
                    or row["proposal_hash"] != fingerprint(definition)
                    or body
                    != {
                        "mission_id": mission_id,
                        "definition_hash": fingerprint(definition),
                        "event_ref": ref.to_json(),
                    }
                ):
                    raise AssuranceError("CHECKER_REGISTRY_IDENTITY")
                entry = RegisteredLayer(
                    layer,
                    LocalLayerBinding(
                        ref,
                        definition["assertion_key"],
                        self.checker_hash,
                        definition["max_runtime_ms"],
                    ),
                )
                entry.require_registered(reader)
                registry[layer] = entry
        return registry

    def _registry(self, mission_id: str) -> dict[str, RegisteredLayer]:
        registry = {}
        with atomic(self.commit.store):
            reader = AssuranceReader(
                self.commit.store, tenant_id=self.tenant_id, mission_id=mission_id
            )
            reader._mission_locked(self.commit.store.connection)
            for layer in (*LOCAL_LAYERS, *EXECUTOR_LAYERS):
                definition = layer_spec(layer, self.checker_hash).to_json()
                key = fingerprint({"mission_id": mission_id, "definition": definition})
                receipt_id = "assurance-check-spec:" + key
                old = self.commit.store.get_receipt(receipt_id)
                if old is None:
                    event = self.commit._emit(
                        "AssuranceCheckSpecRegistered",
                        mission_id,
                        key=key,
                        payload=definition,
                    )
                    ref = AssuranceRef("check_spec", Pin(event.id, 0, fingerprint(event.to_json())))
                    self.commit.store.insert_receipt(
                        commit_id=receipt_id,
                        kind="AssuranceCheckSpecRegistered",
                        subject_id=event.id,
                        base_version=0,
                        proposal_hash=fingerprint(definition),
                        receipt={
                            "mission_id": mission_id,
                            "definition_hash": fingerprint(definition),
                            "event_ref": ref.to_json(),
                        },
                    )
                else:
                    if old.get("mission_id") != mission_id or old.get(
                        "definition_hash"
                    ) != fingerprint(definition):
                        raise AssuranceError("CHECKER_REGISTRY_IDENTITY")
                    ref = AssuranceRef.from_json(old["event_ref"], kinds={"check_spec"})
                entry = RegisteredLayer(
                    layer,
                    LocalLayerBinding(
                        ref,
                        definition["assertion_key"],
                        self.checker_hash,
                        definition["max_runtime_ms"],
                    ),
                )
                entry.require_registered(reader)
                registry[layer] = entry
        return self._read_registry(mission_id)

    def prepare(self, inputs: dict[str, Any]) -> LocalVerificationRecorder:
        """Receives actual VerifierRouter arguments, never a Host API document."""
        from ..storage.htn_store import HtnStore
        from .completion_inputs import load_completion_result_inputs

        self._require_deployment()
        if self.commit.store.connection.in_transaction:
            raise AssuranceError("LOCAL_CHECK_INSIDE_TRANSACTION")
        if inputs.get("tenant_id") != self.tenant_id:
            raise AssuranceError("REF_SCOPE_MISMATCH")
        mission_id = text(inputs["mission_id"])
        ref = AssuranceRef("result", Pin(inputs["result_id"], 0, inputs["subject_hash"]))
        reader = AssuranceReader(self.commit.store, tenant_id=self.tenant_id, mission_id=mission_id)
        with atomic(self.commit.store) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM assurance_mission_bindings WHERE mission_id=?",
                    (mission_id,),
                ).fetchone()
                is None
            ):
                raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
            metadata = reader.read_exact_metadata(ref)
            frozen = load_completion_result_inputs(
                self.commit.store, self.commit.store.get_result(ref.pin.id)
            )
            if frozen is None:
                raise AssuranceError("CHECK_SCOPE_UNAVAILABLE")
            scope_ref = AssuranceRef(
                "completion_scope", Pin(frozen.scope.scope_id, 0, frozen.scope.content_hash())
            )
            envelope = decode(metadata.body_json)
            if (
                envelope != inputs.get("envelope")
                or envelope["task_id"] != inputs.get("task_id")
                or envelope["attempt_id"] != inputs.get("attempt_id")
            ):
                raise AssuranceError("CHECK_INPUT_MANIFEST_MISMATCH")
            registry = self._registry(mission_id)
            manifest_hash = HtnStore(self.commit.store).insert_input_manifest(
                mission_id,
                inputs["task_id"],
                inputs,
                attempt_id=inputs["attempt_id"],
            )
        importer = LocalCheckImporter(
            self.commit,
            tenant_id=self.tenant_id,
            result_ref=ref,
            input_manifest_ref=AssuranceRef("input_manifest", Pin(manifest_hash, 0, manifest_hash)),
            environment_hash=self.environment_hash,
            recorder_implementation_hash=self.recorder_hash,
            registry=registry,
            cas=self.cas,
            require_current_root=self.require_current_root,
            bind_scoped_check=lambda execution_ref: self.commit.import_assurance_check_locked(
                mission_id=mission_id, execution_ref=execution_ref, completion_scope=scope_ref
            ),
        )
        return importer.recorder()
