# SPDX-License-Identifier: Apache-2.0
"""Record actual local verifier calls, without reinterpreting a layer as content truth.

The registered assertion for format/rule is exactly that layer's existing
algorithm. Citation remains part of the document rule algorithm. Execution-based
checks use their original executor importer, never this synchronous wrapper.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..artifacts.workspace import Workspace, sha256_file
from ..assurance.checks import Grade
from ..assurance.codec import AssuranceError, canonical, digest, fingerprint, integer, text
from ..assurance.local_checks import (
    AssertionOutput,
    LocalCheckRecorder,
    RecordedLocalCheck,
    RegisteredLocalChecker,
)
from ..assurance.refs import AssuranceRef
from ..contracts import Artifact, Mission, ResultEnvelope, Task
from ..governance.domains import DomainProfileV1
from ..memory.verified_knowledge import KnowledgeIndex
from .assessments import AssessmentBindingV1
from .deterministic_checks import ERROR, LayerResult


def freeze_verifier_inputs(
    *,
    mission: Mission,
    task: Task,
    envelope: ResultEnvelope,
    artifacts: Sequence[Artifact],
    verification_copy: Workspace,
    client_result_id: str | None,
    tampered: Sequence[str],
    knowledge: KnowledgeIndex | None,
    require_synthesis_knowledge: bool,
    action_problems: Sequence[str] | None,
    local_code_execution: bool,
    domain: DomainProfileV1 | None,
    assessment_binding: AssessmentBindingV1 | None,
) -> dict[str, Any]:
    """Build from the router's ACTUAL arguments before executing its checks."""
    body = {
        "schema": "assurance-local-verification-input-v1",
        "mission_id": mission.id,
        "tenant_id": mission.tenant_id,
        "task_id": task.id,
        "attempt_id": envelope.attempt_id,
        "result_id": envelope.id,
        "subject_hash": fingerprint(envelope.to_json()),
        "task": task.to_json(),
        "envelope": envelope.to_json(),
        "artifacts": [artifact.to_json() for artifact in artifacts],
        "verification_copy": {
            "attempt_id": verification_copy.attempt_id,
            "files": {
                path: sha256_file(verification_copy.resolve(path))
                for path in verification_copy.list_files()
            },
        },
        "client_result_id": client_result_id,
        "tampered": list(tampered),
        "knowledge": None
        if knowledge is None
        else {
            "mission_id": knowledge.mission_id,
            "records": {key: row.to_json() for key, row in sorted(knowledge.records.items())},
            "claim_status": {
                key: str(value) for key, value in sorted(knowledge.claim_status.items())
            },
        },
        "require_synthesis_knowledge": require_synthesis_knowledge,
        "action_problems": None if action_problems is None else list(action_problems),
        "local_code_execution": local_code_execution,
        "domain": None if domain is None else domain.to_json(),
        "assessment_binding": None if assessment_binding is None else assessment_binding.to_json(),
    }
    canonical(body)  # The original manifest is bounded; no prefix or truncated success.
    return body


@dataclass(frozen=True, slots=True)
class LocalLayerBinding:
    spec_ref: AssuranceRef
    assertion_key: str
    implementation_hash: str
    max_runtime_ms: int = 120_000

    def __post_init__(self) -> None:
        if self.spec_ref.kind != "check_spec":
            raise AssuranceError("CHECK_SPEC_REF_REQUIRED")
        text(self.assertion_key)
        digest(self.implementation_hash)
        integer(self.max_runtime_ms, minimum=1, maximum=120_000)


class LocalVerificationRecorder:
    """Per-result trusted assembly; the bindings/sink are never request fields.

    The sink persists the actual outputs and imports the real event before the
    layer can be accepted by the original recorder. A sink failure propagates.
    """

    def __init__(
        self,
        *,
        mission_id: str,
        subject_hash: str,
        input_manifest_hash: str,
        environment_hash: str,
        recorder_implementation_hash: str,
        bindings: Mapping[str, LocalLayerBinding],
        now_ms: Callable[[], int],
        assert_outside_transaction: Callable[[], None],
        persist: Callable[[RecordedLocalCheck], AssuranceRef],
        executor_bindings: Mapping[str, LocalLayerBinding] | None = None,
        persist_executor: Callable[[RecordedLocalCheck, dict[str, Any]], AssuranceRef]
        | None = None,
    ) -> None:
        if not set(bindings) <= {"format_check", "rule_check"}:
            raise AssuranceError("LOCAL_CHECK_LAYER_UNSUPPORTED")
        if not set(executor_bindings or {}) <= {"code_test"}:
            raise AssuranceError("LOCAL_CHECK_LAYER_UNSUPPORTED")
        self.executor_bindings = dict(executor_bindings or {})
        self.persist_executor = persist_executor
        self.mission_id = text(mission_id)
        self.subject_hash = digest(subject_hash)
        self.input_manifest_hash = digest(input_manifest_hash)
        self.environment_hash = digest(environment_hash)
        self.implementation_hash = digest(recorder_implementation_hash)
        self.bindings = dict(bindings)
        self.now_ms = now_ms
        self.assert_outside_transaction = assert_outside_transaction
        self.persist = persist

    def run(self, layer: str, execute: Callable[[], LayerResult]) -> LayerResult:
        binding = self.bindings.get(layer)
        if binding is None:
            raise AssuranceError("CHECKER_UNAVAILABLE", layer)
        self.assert_outside_transaction()
        actual: LayerResult | None = None

        def checker() -> tuple[AssertionOutput, ...]:
            nonlocal actual
            result = execute()  # Exactly once, after freezing the run identity.
            if not isinstance(result, LayerResult) or result.layer != layer:
                raise AssuranceError("CHECK_ASSERTION_INVALID")
            # ERROR describes failure to execute the checker, not a false claim.
            if result.status == ERROR:
                actual = result
                raise AssuranceError("LOCAL_CHECK_EXECUTION_ERROR")
            output = canonical(result.to_json()).encode("utf-8")
            actual = result
            return (
                AssertionOutput(
                    binding.assertion_key,
                    Grade(result.status) if result.status in {"PASS", "FAIL"} else Grade.UNKNOWN,
                    output,
                ),
            )

        recorder = LocalCheckRecorder(
            {
                binding.spec_ref: RegisteredLocalChecker(
                    binding.spec_ref,
                    binding.implementation_hash,
                    checker,
                    binding.max_runtime_ms,
                )
            },
            now_ms=self.now_ms,
            implementation_hash=self.implementation_hash,
        )
        recorded = recorder.record_run(
            binding.spec_ref,
            mission_id=self.mission_id,
            subject_hash=self.subject_hash,
            input_manifest_hash=self.input_manifest_hash,
            environment_hash=self.environment_hash,
        )
        self.assert_outside_transaction()
        ref = self.persist(recorded)
        if not isinstance(ref, AssuranceRef) or ref.kind != "local_check_receipt":
            raise AssuranceError("CHECK_IMPORT_RECEIPT_REQUIRED")
        if actual is None:
            actual = LayerResult(layer, ERROR, "local checker execution failed", {})
        # Only the original LayerResult is returned, with provenance. The model
        # never supplies this ref, and an ERROR is never upgraded to PASS.
        from ..assurance.codec import decode

        if decode(recorded.receipt_json)["execution_state"] != "SUCCEEDED":
            actual = LayerResult(layer, ERROR, "local checker execution failed", {})
        return LayerResult(
            actual.layer,
            actual.status,
            actual.summary,
            {**dict(actual.detail), "assurance_local_check_ref": ref.to_json()},
        )

    def record_executor(self, layer: str, result: LayerResult) -> LayerResult:
        """Import the executor's own run of ``layer``; nothing is executed here.

        The verifier already ran pytest through the sandbox executor port; the
        per-target ExecutionReceipts inside ``result`` are the facts. A result
        without them stays UNKNOWN, and the returned LayerResult keeps the
        original status with the imported receipt ref attached.
        """
        from ..assurance.executor_checks import executor_run_facts, record_actual_run

        binding = self.executor_bindings.get(layer)
        if binding is None or self.persist_executor is None:
            raise AssuranceError("CHECKER_UNAVAILABLE", layer)
        if not isinstance(result, LayerResult) or result.layer != layer:
            raise AssuranceError("CHECK_ASSERTION_INVALID")
        self.assert_outside_transaction()
        state, verdict, document = executor_run_facts(result.to_json(), layer=layer)
        output = AssertionOutput(
            binding.assertion_key, verdict, canonical(document).encode("utf-8")
        )
        recorded = record_actual_run(
            binding.spec_ref,
            mission_id=self.mission_id,
            subject_hash=self.subject_hash,
            input_manifest_hash=self.input_manifest_hash,
            environment_hash=self.environment_hash,
            implementation_hash=self.implementation_hash,
            now_ms=self.now_ms,
            state=state,
            outputs=(output,),
        )
        ref = self.persist_executor(recorded, document)
        if not isinstance(ref, AssuranceRef) or ref.kind != "execution_receipt":
            raise AssuranceError("CHECK_IMPORT_RECEIPT_REQUIRED")
        return LayerResult(
            result.layer,
            result.status,
            result.summary,
            {**dict(result.detail), "assurance_executor_check_ref": ref.to_json()},
        )
