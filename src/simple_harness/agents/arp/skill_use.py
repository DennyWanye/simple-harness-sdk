# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Skill load / execute (ARP-EXEC-1.1.1 §9.8–9.9, SKILL-CATALOGUE §5).

``load`` binds one admitted Skill to the current Session/Turn/call as a ``SkillUse`` of
mode INSTRUCTIONS and records which instruction files (path + sha256) were read. The
text itself reaches the model only through the next prepared request: the composer
renders every distinct file once into section E and the manifest names the use
receipt and the file artefacts. Loading never executes anything.

``execute`` returns the instruction text for INSTRUCTIONS skills, hands SCRIPT skills to
the approved runner port with a fixed argv (only the whole tokens ``{input_json}`` /
``{output_json}`` are substituted, by the executor), validates input against the
skill's input schema and output against its output schema, and refuses WORKFLOW skills
by name until an original executor is deployed. Exit 0 without a valid output is a
failure; an unconfirmed end is UNKNOWN, never a success.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from simple_harness.execution.sqlite.base_agent import turns

from . import catalogue as cat
from . import store
from .codec import canonical, check, validate
from .errors import ArpError
from .pins import Pin
from .ports import ScriptRun, ScriptRunReceipt
from .rules import effective_permissions
from .strict import digest, plain

INSTRUCTION_ROLES = ("INSTRUCTIONS", "REFERENCE")
MAX_INPUT_BYTES = 65536
INLINE_TEXT_MAX = 32000
SCRIPT_TIMEOUT_MS = 60_000
ARGV_TOKENS = ("{input_json}", "{output_json}")
EXECUTE_TOOL_ID = "skill_execute"


@dataclass(slots=True)
class SkillUseService:
    catalogue: cat.CatalogueService
    skills: Any  # SkillImporter
    uow: Any
    clock_ms: Callable[[], int]
    clock: Callable[[], float]
    approval_ref: Pin
    owner_mode: str
    script_runner: Any | None = None
    # Budget gate for load (§9.8): distinct loaded instruction tokens of the Session may
    # never exceed the skill share of the context capacity, so a load can never brick
    # the Session's next prepare. Both are bound at assembly.
    count: Callable[[str], int] | None = None
    skill_capacity: Callable[[], int] | None = None
    loaded_blocks: Callable[[store.SessionRow], tuple[Any, ...]] | None = None

    # ---- lookups ----------------------------------------------------------------------------

    def usable_skill(self, pin: Pin) -> tuple[cat.RevisionRow, cat.ActivationRow, Mapping[str, Any]]:
        """An ADMITTED, currently usable Skill with a complete lock, else a named refusal."""

        connection = self.catalogue.connection
        revision = cat.resolve_pin(connection, self.catalogue.namespace_id, pin)
        if revision.entry_kind != "SKILL":
            raise ArpError("CATALOGUE_KIND_MISMATCH", "the request names a non-Skill entry")
        activation = cat.read_activation(connection, self.catalogue.namespace_id, "SKILL", revision.entry_id, revision.revision)
        if activation is None:
            raise ArpError("CATALOGUE_STALE", "skill has no lifecycle row")
        if activation.state == "QUARANTINED":
            raise ArpError("SKILL_TRIAL_REQUIRED", f"skill {revision.entry_id}@{revision.revision} is quarantined")
        ok, reasons = self.catalogue.usable(activation, now_ms=self.clock_ms())
        if not ok:
            raise ArpError("SKILL_NOT_ADMITTED", f"skill {revision.entry_id}@{revision.revision} is not usable: {', '.join(reasons)}", detail={"reasons": reasons})
        lock = self.skills.latest_lock(revision)
        if lock is None or not lock["complete"]:
            raise ArpError("DEPENDENCY_UNRESOLVED", "skill has no complete dependency lock")
        return revision, activation, lock

    def _turn_id(self, session: store.SessionRow) -> str:
        turn = self.uow.read_open_agent_turn(session.agent_id)
        if turn is not None:
            return str(turn.turn_id)
        rows = turns.list_turns(self.uow.database.connection, session.agent_id)
        if not rows:
            raise ArpError("STATE_COMBINATION_INVALID", "no Turn to bind the skill use to")
        return str(rows[-1].turn_id)

    def _binding_refs(self, session: store.SessionRow) -> tuple[Pin, Pin]:
        connection = self.uow.database.connection
        context = store.latest_context(connection, session.session_id)
        if context is not None:
            return Pin("input_manifest", context.context_id, 0, context.manifest_hash), Pin.from_json(context.manifest["tool_snapshot_ref"])
        snapshot = cat.latest_tool_snapshot(connection, session.session_id)
        tool_snapshot = Pin("tool_snapshot", f"{session.agent_id}:tools", 0, digest([])) if snapshot is None else cat.ToolExposureService.snapshot_pin(snapshot)
        return Pin("input_manifest", f"session:{session.session_id}", session.generation, digest({"session": session.session_id, "generation": session.generation})), tool_snapshot

    def _owner_contract(self, session: store.SessionRow) -> Pin:
        return Pin("policy", f"{session.profile_id}:owner-mode:{self.owner_mode}", session.profile_revision, session.profile_hash)

    @staticmethod
    def call_ref(session: store.SessionRow, call_id: str) -> Pin:
        return Pin("invocation", f"{session.agent_id}:{call_id}", 0, digest({"agent": session.agent_id, "call": call_id}))

    def _use(self, session: store.SessionRow, *, turn_id: str, call_id: str, revision: cat.RevisionRow, activation: cat.ActivationRow, lock: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
        input_manifest, tool_snapshot = self._binding_refs(session)
        use_key = f"{turn_id}:{call_id}:{mode}"
        body = {
            "schema_version": 1,
            "use_id": "use-" + digest({"session": session.session_id, "key": use_key})[:32],
            "session_id": session.session_id,
            "turn_id": turn_id,
            "skill_ref": revision.pin.to_json(),
            "bundle_digest": revision.bundle_root_ref.content_hash if revision.bundle_root_ref is not None else digest(revision.body),
            "owner_contract_ref": self._owner_contract(session).to_json(),
            "input_manifest_ref": input_manifest.to_json(),
            "dependency_lock_refs": [self.skills.lock_pin(lock).to_json()],
            "tool_snapshot_ref": tool_snapshot.to_json(),
            "evaluation_ref": None if activation.evaluation_ref is None else activation.evaluation_ref.to_json(),
            "authority_refs": [self.approval_ref.to_json()],
            "mode": mode,
            "reservation_fact_ref": None,
        }
        value = check("SkillUse", body)
        with self.uow.database.transaction() as txn:
            return store.put_skill_use_locked(txn, use=value, use_key=use_key, original_call_ref=self.call_ref(session, call_id))

    # ---- load (§9.8) -----------------------------------------------------------------------

    def load(self, session: store.SessionRow, *, call_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        value = check("SkillLoadRequest", plain(request))
        revision, activation, lock = self.usable_skill(Pin.from_json(value["skill_ref"]))
        turn_id = self._turn_id(session)
        files = self.instruction_files(revision)
        self._budget_gate(session, revision, files)
        use = self._use(session, turn_id=turn_id, call_id=call_id, revision=revision, activation=activation, lock=lock, mode="INSTRUCTIONS")
        with self.uow.database.transaction() as txn:
            store.append_original_receipt_locked(
                txn, run_id=session.agent_id, kind="skill_load", receipt_key=str(use["use_id"]),
                body={"use_id": use["use_id"], "skill_ref": revision.pin.to_json(), "call_ref": self.call_ref(session, call_id).to_json(), "files": files},
                now=self.clock(),
            )
        return use

    @staticmethod
    def instruction_files(revision: cat.RevisionRow) -> list[dict[str, Any]]:
        """Only the skill's instruction file is loaded into E; reference files are read
        through the details/artifact path, never bulk-loaded into every request."""

        main = str(revision.body["instructions_path"])
        items = [f for f in revision.body["files"] if f["relative_path"] == main and f["role"] == "INSTRUCTIONS"]
        if not items:
            raise ArpError("SKILL_MANIFEST_CONFLICT", f"instructions_path {main} is not an INSTRUCTIONS file of the skill")
        return [{"relative_path": f["relative_path"], "sha256": f["sha256"], "size_bytes": int(f["size_bytes"])} for f in items]

    def _budget_gate(self, session: store.SessionRow, revision: cat.RevisionRow, files: list[dict[str, Any]]) -> None:
        if self.count is None or self.skill_capacity is None or self.loaded_blocks is None:
            return
        existing = self.loaded_blocks(session)
        known = {(b.skill_ref.id, b.skill_ref.revision, b.path, b.artifact_ref.content_hash) for b in existing}
        total = sum(int(b.tokens) for b in existing)
        for item in files:
            key = (revision.entry_id, revision.revision, str(item["relative_path"]), str(item["sha256"]))
            if key in known:
                continue
            total += self.count(self.skills.read_file(revision, key[2]).decode("utf-8", "replace"))
        limit = int(self.skill_capacity())
        if total > limit:
            raise ArpError("REQUIRED_CONTEXT_TOO_LARGE", f"loaded skill instructions would take {total} tokens, above the skill share {limit}", detail={"tokens": total, "limit": limit})

    # ---- execute (§9.9) ----------------------------------------------------------------------

    def execute(self, session: store.SessionRow, *, call_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        value = check("SkillExecutionRequest", plain(request))
        revision, activation, lock = self.usable_skill(Pin.from_json(value["skill_ref"]))
        kind = str(revision.body["implementation"]["kind"])
        tool_ref = self._execute_tool_ref()
        self._require_exposed(session, revision)
        if kind == "WORKFLOW":
            raise ArpError("WORKFLOW_UNAVAILABLE", "no original workflow executor is deployed for this runtime")
        turn_id = self._turn_id(session)
        call_ref = self.call_ref(session, call_id)
        if kind == "INSTRUCTIONS":
            use = self._use(session, turn_id=turn_id, call_id=call_id, revision=revision, activation=activation, lock=lock, mode="INSTRUCTIONS")
            path = str(revision.body["instructions_path"])
            data = self.skills.read_file(revision, path)
            sha256 = hashlib.sha256(data).hexdigest()
            text = data.decode("utf-8", "replace")
            if len(text) <= INLINE_TEXT_MAX:
                view = self._view(call_ref, tool_ref, "SUCCEEDED", inline_text=text, representation="INLINE", source_hash=sha256)
            else:
                view = self._view(call_ref, tool_ref, "SUCCEEDED", representation="REFERENCED", source_hash=sha256, artifact=Pin("artifact", f"skill:{revision.entry_id}@{revision.revision}:{path}", 0, sha256))
            return self._record(session, use, view, runner_receipt=None)
        # SCRIPT
        if self.script_runner is None:
            raise ArpError("RUNNER_UNAVAILABLE", "no approved script executor is bound to this runtime")
        implementation = revision.body["implementation"]
        runner_ref = Pin.from_json(implementation["runner_ref"])
        self._require_runner(runner_ref)
        argv = self._argv(implementation["argv_template"])
        arguments = plain(value["arguments"])
        input_schema = cat.resolve_pin(self.catalogue.connection, self.catalogue.namespace_id, Pin.from_json(revision.body["input_schema_ref"]))
        validate(arguments, dict(input_schema.body), path="$.arguments")
        input_json = canonical(arguments)
        if len(input_json) > MAX_INPUT_BYTES:
            raise ArpError("REQUEST_BYTES_LIMIT", f"script input exceeds {MAX_INPUT_BYTES} bytes")
        script_path = str(implementation["script_path"])
        script_bytes = self.skills.read_file(revision, script_path)
        use = self._use(session, turn_id=turn_id, call_id=call_id, revision=revision, activation=activation, lock=lock, mode="SCRIPT")
        run = ScriptRun(
            skill_ref=revision.pin, runner_ref=runner_ref, script_path=script_path, script_bytes=script_bytes, argv=argv, input_json=input_json,
            workspace_key=f"skill/{session.session_id}/{use['use_id']}", timeout_ms=SCRIPT_TIMEOUT_MS, output_limit_bytes=int(self._result_limit(tool_ref)),
        )
        receipt = self.script_runner.run(run)
        if not isinstance(receipt, ScriptRunReceipt):
            raise ArpError("SOURCE_UNAVAILABLE", "script runner returned no receipt")
        view = self._script_view(call_ref, tool_ref, revision, receipt)
        return self._record(session, use, view, runner_receipt=receipt)

    def _execute_tool_ref(self) -> Pin:
        revision = cat.latest_revision(self.catalogue.connection, self.catalogue.namespace_id, "TOOL", EXECUTE_TOOL_ID)
        if revision is None:
            raise ArpError("TOOL_NOT_EXPOSED", f"{EXECUTE_TOOL_ID} is not in the catalogue")
        return revision.pin

    def _result_limit(self, tool_ref: Pin) -> int:
        revision = cat.resolve_pin(self.catalogue.connection, self.catalogue.namespace_id, tool_ref)
        return int(revision.body.get("result_bytes_limit", 65536))

    def _require_exposed(self, session: store.SessionRow, revision: cat.RevisionRow) -> None:
        """Permission intersection (§8 ToolGateway): the skill's required tools must all be
        in the Session's current tool snapshot; a skill never widens what the model may call."""

        snapshot = cat.latest_tool_snapshot(self.uow.database.connection, session.session_id)
        exposed = frozenset() if snapshot is None else frozenset(f"{t['tool_ref']['id']}@{t['tool_ref']['revision']}" for t in snapshot["tools"])
        required = frozenset(f"{r['id']}@{r['revision']}" for r in revision.body["required_tool_refs"])
        if required and effective_permissions(required, exposed) != required:
            missing = sorted(required - exposed)
            raise ArpError("TOOL_NOT_EXPOSED", f"required tools are not exposed to this session: {', '.join(missing)}", detail={"missing": missing})

    def _require_runner(self, runner_ref: Pin) -> None:
        connection = self.catalogue.connection
        try:
            runner = cat.resolve_pin(connection, self.catalogue.namespace_id, runner_ref)
        except ArpError as error:
            raise ArpError("RUNNER_UNAVAILABLE", f"runner {runner_ref.id} is not in the catalogue") from error
        activation = cat.read_activation(connection, self.catalogue.namespace_id, "PROVIDER", runner.entry_id, runner.revision)
        if activation is None or activation.state != "ADMITTED":
            raise ArpError("RUNNER_UNAVAILABLE", f"runner {runner_ref.id} is not admitted")

    @staticmethod
    def _argv(template: Any) -> tuple[str, ...]:
        argv: list[str] = []
        for token in template:
            text = str(token)
            if ("{" in text or "}" in text) and text not in ARGV_TOKENS:
                raise ArpError("SKILL_MANIFEST_CONFLICT", f"argv token {text!r}: only whole {ARGV_TOKENS[0]} / {ARGV_TOKENS[1]} tokens are substituted")
            argv.append(text)
        if "{output_json}" not in argv:
            raise ArpError("SKILL_MANIFEST_CONFLICT", "argv_template names no {output_json}")
        return tuple(argv)

    def _script_view(self, call_ref: Pin, tool_ref: Pin, revision: cat.RevisionRow, receipt: ScriptRunReceipt) -> dict[str, Any]:
        if receipt.terminal == "UNKNOWN":
            return self._view(call_ref, tool_ref, "UNKNOWN", error_code="UNKNOWN_REQUIRES_RECONCILIATION", artifact=receipt.stdout_ref)
        if receipt.terminal == "TIMEOUT":
            return self._view(call_ref, tool_ref, "FAILED", error_code="SKILL_OUTPUT_INVALID", inline_text="script stopped at the timeout", artifact=receipt.stderr_ref)
        if receipt.terminal != "EXITED" or receipt.exit_code is None:
            raise ArpError("SOURCE_UNAVAILABLE", "script runner receipt has no terminal state")
        if receipt.exit_code != 0:
            return self._view(call_ref, tool_ref, "FAILED", error_code="SKILL_OUTPUT_INVALID", inline_text=f"script exited with {receipt.exit_code}", artifact=receipt.stderr_ref)
        if receipt.output is None or receipt.truncated:
            return self._view(call_ref, tool_ref, "FAILED", error_code="SKILL_OUTPUT_INVALID", inline_text="exit 0 without a complete output file", artifact=receipt.stderr_ref)
        try:
            output = json.loads(receipt.output.decode("utf-8"))
            schema = cat.resolve_pin(self.catalogue.connection, self.catalogue.namespace_id, Pin.from_json(revision.body["output_schema_ref"]))
            validate(output, dict(schema.body), path="$.output")
        except (UnicodeDecodeError, ValueError, ArpError) as error:
            detail = error.code if isinstance(error, ArpError) else type(error).__name__
            return self._view(call_ref, tool_ref, "FAILED", error_code="SKILL_OUTPUT_INVALID", inline_text=f"output is not valid against the output schema ({detail})", artifact=receipt.stderr_ref)
        text = canonical(output).decode("utf-8")
        sha256 = hashlib.sha256(receipt.output).hexdigest()
        if len(text) <= INLINE_TEXT_MAX:
            return self._view(call_ref, tool_ref, "SUCCEEDED", inline_text=text, representation="INLINE", source_hash=sha256)
        return self._view(call_ref, tool_ref, "SUCCEEDED", representation="REFERENCED", source_hash=sha256, artifact=Pin("artifact", f"skill-output:{call_ref.id}", 0, sha256))

    @staticmethod
    def _view(call_ref: Pin, tool_ref: Pin, status: str, *, inline_text: str = "", representation: str | None = None, source_hash: str | None = None, error_code: str | None = None, artifact: Pin | None = None) -> dict[str, Any]:
        if representation is None:
            representation = "INLINE" if inline_text else ("REFERENCED" if artifact is not None else "NONE")
        return check(
            "ToolResultView",
            {
                "schema_version": 1,
                "call_ref": call_ref.to_json(),
                "tool_ref": tool_ref.to_json(),
                "status": status,
                "result_artifact_ref": None if artifact is None else artifact.to_json(),
                "operation_intent_ref": None,
                "inline_text": inline_text,
                "representation": representation,
                "source_hash": source_hash,
                "error_code": error_code,
            },
        )

    def _record(self, session: store.SessionRow, use: Mapping[str, Any], view: Mapping[str, Any], *, runner_receipt: ScriptRunReceipt | None) -> dict[str, Any]:
        body = {"use_id": use["use_id"], "view": dict(view), "runner_receipt_ref": None if runner_receipt is None else runner_receipt.receipt_ref.to_json()}
        with self.uow.database.transaction() as txn:
            store.append_original_receipt_locked(txn, run_id=session.agent_id, kind="skill_execute", receipt_key=f"{use['use_id']}:{view['call_ref']['id']}", body=body, now=self.clock())
        return dict(view)


__all__ = ("EXECUTE_TOOL_ID", "INLINE_TEXT_MAX", "INSTRUCTION_ROLES", "MAX_INPUT_BYTES", "SkillUseService")
