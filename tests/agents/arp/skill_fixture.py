"""Shared helpers for the skill lifecycle / use tests: bundles, commands, a test-only
acceptance double (stands in for the Assurance successor, never ships) and a scripted
runner double (stands in for the Host's approved executor)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

from arp_fixture import trusted_caller

from simple_harness.agents.arp import catalogue as cat
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import ScriptRun, ScriptRunReceipt
from simple_harness.agents.arp.strict import digest


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
    return buffer.getvalue()


def artifact(data: bytes) -> Pin:
    return Pin("artifact", "bundle:" + hashlib.sha256(data).hexdigest()[:12], 0, hashlib.sha256(data).hexdigest())


def install_command(runtime, data: bytes, fmt: str = "SKILL_MD") -> dict:  # type: ignore[no-untyped-def]
    service = runtime.arp.catalogue
    return {"bundle_artifact_ref": artifact(data).to_json(), "scope_ref": service.scope.pin.to_json(), "format": fmt, "expected_catalogue_revision": service.epoch()}


def md_bundle(name: str, body: str = "先读这里。") -> bytes:
    return zip_bytes({"SKILL.md": f"---\nname: {name}\ndescription: 一个说明技能。\n---\n# {name}\n\n{body}\n".encode(), "notes/ref.md": f"{name} 的参考。".encode()})


def native_bundle(runtime, *, skill_id: str, implementation: dict, files: dict[str, tuple[bytes, str]], required_tool_refs: list | None = None, input_schema: Pin | None = None, output_schema: Pin | None = None, capability: Pin | None = None, required_skill_refs: list | None = None) -> bytes:  # type: ignore[no-untyped-def]
    """A NATIVE bundle whose skill.json lists ``files`` (path → (bytes, role))."""

    report = runtime.arp.bootstrap
    schema = report.instructions_schema_ref
    manifest = {
        "schema_version": 1, "skill_id": skill_id, "version": 1, "name": skill_id, "description": f"{skill_id} 原生技能。",
        "files": [{"relative_path": p, "size_bytes": len(b), "sha256": hashlib.sha256(b).hexdigest(), "role": r} for p, (b, r) in files.items()],
        "instructions_path": "SKILL.md",
        "input_schema_ref": (input_schema or schema).to_json(), "output_schema_ref": (output_schema or schema).to_json(),
        "capability_ref": (capability or report.instructions_capability_ref).to_json(),
        "required_tool_refs": required_tool_refs or [], "required_skill_refs": required_skill_refs or [],
        "implementation": implementation,
        "requested_permission_policy_ref": Pin("policy", "p", 0, digest("p")).to_json(),
        "verification_policy_ref": report.verification_policy_ref.to_json(),
        "origin_refs": [],
    }
    return zip_bytes({"skill.json": json.dumps(manifest).encode(), **{p: b for p, (b, _) in files.items()}})


def import_skill(runtime, data: bytes, *, command: str, fmt: str = "SKILL_MD"):  # type: ignore[no-untyped-def]
    return runtime.arp.skills.import_bundle(install_command(runtime, data, fmt), data, caller=trusted_caller(), command_id=command)


def activation_of(runtime, revision):  # type: ignore[no-untyped-def]
    service = runtime.arp.catalogue
    return cat.read_activation(service.connection, service.namespace_id, "SKILL", revision.entry_id, revision.revision)


def trial_command(runtime, revision) -> dict:  # type: ignore[no-untyped-def]
    activation = activation_of(runtime, revision)
    lock = runtime.arp.skills.latest_lock(revision)
    return {
        "schema_version": 1, "skill_ref": revision.pin.to_json(), "expected_activation_revision": activation.row_version,
        "dependency_lock_ref": runtime.arp.skills.lock_pin(lock).to_json(), "evaluation_policy_ref": revision.body["verification_policy_ref"],
    }


def acceptance_pin(binding: dict, tag: str = "ok") -> Pin:
    return Pin("acceptance", f"eval-acceptance:{binding['evaluation_ref']['id']}:{tag}", 1, digest({"binding": binding["evaluation_ref"], "tag": tag}))


def admit_command(runtime, revision, binding: dict, *, tag: str = "ok") -> dict:  # type: ignore[no-untyped-def]
    return {
        "schema_version": 1, "skill_ref": revision.pin.to_json(), "evaluation_acceptance_ref": acceptance_pin(binding, tag).to_json(),
        "evaluation_policy_ref": binding["policy_ref"], "scope_ref": binding["scope_ref"],
    }


def lifecycle_command(runtime, revision, action: str, *, acceptance: Pin | None, reason: str = "test") -> dict:  # type: ignore[no-untyped-def]
    activation = activation_of(runtime, revision)
    lock = runtime.arp.skills.latest_lock(revision)
    return {
        "schema_version": 1, "action": action, "skill_ref": revision.pin.to_json(), "expected_activation_revision": activation.row_version,
        "evaluation_ref": None if acceptance is None else acceptance.to_json(), "dependency_lock_ref": runtime.arp.skills.lock_pin(lock).to_json(), "reason": reason,
    }


class AcceptingAssurance:
    """TEST DOUBLE for the Assurance successor: accepts an evaluation only when the
    acceptance pin was derived from that binding with the tag it was told to accept."""

    def __init__(self, tag: str = "ok") -> None:
        self.tag = tag
        self.calls: list[tuple[dict, Pin]] = []

    def verify(self, binding: Any, acceptance_ref: Pin) -> dict:
        self.calls.append((dict(binding), acceptance_ref))
        accepted = acceptance_ref == acceptance_pin(dict(binding), self.tag)
        return {"accepted": accepted, "evaluation_ref": dict(binding["evaluation_ref"]), "acceptance_ref": acceptance_ref.to_json()}


def admit_skill(runtime, revision, *, command: str, acceptance_tag: str = "ok") -> dict:  # type: ignore[no-untyped-def]
    """QUARANTINED → TRIAL → ADMITTED through the real commands (needs an AcceptingAssurance port)."""

    lifecycle = runtime.arp.lifecycle
    binding = lifecycle.begin_trial(trial_command(runtime, revision), caller=trusted_caller(), command_id=f"{command}:trial")
    lifecycle.admit(admit_command(runtime, revision, binding, tag=acceptance_tag), caller=trusted_caller(), command_id=f"{command}:admit")
    return binding


class ScriptedRunner:
    """TEST DOUBLE for the Host's approved script executor: returns the receipts it was
    given, in order, and records every ScriptRun it received."""

    def __init__(self, receipts: list[ScriptRunReceipt]) -> None:
        self.receipts = list(receipts)
        self.runs: list[ScriptRun] = []

    def run(self, request: ScriptRun) -> ScriptRunReceipt:
        self.runs.append(request)
        return self.receipts.pop(0)


def receipt(terminal: str = "EXITED", exit_code: int | None = 0, output: bytes | None = b"{}", *, truncated: bool = False) -> ScriptRunReceipt:
    return ScriptRunReceipt(terminal=terminal, exit_code=exit_code, output=output, receipt_ref=Pin("receipt", f"runner:{terminal}:{exit_code}", 0, digest([terminal, exit_code, None if output is None else output.hex(), truncated])), truncated=truncated)
