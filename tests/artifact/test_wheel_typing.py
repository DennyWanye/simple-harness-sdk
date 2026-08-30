# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import BuildArtifacts


def test_installed_wheel_exposes_strict_agent_memory_and_manifest_types(
    reproducible_artifacts: BuildArtifacts,
    tmp_path: Path,
) -> None:
    wheel = next(reproducible_artifacts.first.glob("*.whl")).resolve()
    virtualenv = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(virtualenv)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = virtualenv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "from simple_harness import (\n"
        "    AgentIdentity, AgentMemoryPort, CallId, CommittedTurn,\n"
        "    ContextAssemblyDecision, ContextFragment, ContextRouteState,\n"
        "    DisclosureContext, EffectId,\n"
        "    LongTermMemoryType, MemoryAnalysisExecutorPort, RecallPlan,\n"
        "    RunContextAuthorityPort, RunContextAuthorityRequest, RunContextSnapshot,\n"
        "    RunId, SanitizedEvidenceEnvelope, TaskExecutionAuthorityPort,\n"
        "    TaskExecutionEnvelope, TaskExecutionEnvelopeRequest,\n"
        "    TaskScopeMutationPlan, TaskScopeProposal, TaskScopeRoute,\n"
        "    ToolEffectClass, ToolExecutionPolicy, ToolRouteRequirement,\n"
        "    ToolTaskScopeRequirement,\n"
        ")\n"
        "from simple_harness.execution.sqlite import (\n"
        "    ExecutionMigrationManifest, LegacyDisposition, LegacyIdentityBinding,\n"
        "    LegacyIdentityMap, MigrationManifestEntry, migrate_execution_v3_to_v4,\n"
        ")\n\n"
        "def inspect(port: AgentMemoryPort, manifest: ExecutionMigrationManifest) -> str:\n"
        "    return manifest.digest\n\n"
        "class ContextAuthority:\n"
        "    async def prepare_snapshot(\n"
        "        self, request: RunContextAuthorityRequest\n"
        "    ) -> RunContextSnapshot:\n"
        "        raise NotImplementedError(request)\n\n"
        "class TaskAuthority:\n"
        "    async def issue_envelope(\n"
        "        self, request: TaskExecutionEnvelopeRequest\n"
        "    ) -> TaskExecutionEnvelope:\n"
        "        raise NotImplementedError(request)\n\n"
        "identity: AgentIdentity = AgentIdentity('deployment', 'household', 'actor', 'session')\n"
        "binding: LegacyIdentityBinding = LegacyIdentityBinding(\n"
        "    'old-user', 'old-session', identity\n"
        ")\n"
        "identity_map: LegacyIdentityMap = LegacyIdentityMap.from_bindings((binding,))\n"
        "disposition: LegacyDisposition = LegacyDisposition.DEFERRED_TURN\n"
        "entry_type: type[MigrationManifestEntry] = MigrationManifestEntry\n"
        "migrator = migrate_execution_v3_to_v4\n"
        "turn_type: type[CommittedTurn] = CommittedTurn\n"
        "context_fragment_type: type[ContextFragment] = ContextFragment\n"
        "context_decision_type: type[ContextAssemblyDecision] = ContextAssemblyDecision\n"
        "disclosure_type: type[DisclosureContext] = DisclosureContext\n"
        "evidence_type: type[SanitizedEvidenceEnvelope] = SanitizedEvidenceEnvelope\n"
        "recall_type: type[RecallPlan] = RecallPlan\n"
        "analysis_port_type: type[MemoryAnalysisExecutorPort] = MemoryAnalysisExecutorPort\n"
        "task_proposal_type: type[TaskScopeProposal] = TaskScopeProposal\n"
        "task_mutation_type: type[TaskScopeMutationPlan] = TaskScopeMutationPlan\n"
        "memory_type: LongTermMemoryType = LongTermMemoryType.SEMANTIC\n"
        "route: TaskScopeRoute = TaskScopeRoute.CREATE_NEW\n"
        "policy: ToolExecutionPolicy = ToolExecutionPolicy(\n"
        "    'host:read', 'a' * 64, ToolEffectClass.NON_PROJECT_EFFECT,\n"
        "    ToolRouteRequirement.OPTIONAL, ToolTaskScopeRequirement.OPTIONAL,\n"
        ")\n"
        "context_request: RunContextAuthorityRequest = RunContextAuthorityRequest(\n"
        "    RunId('run-1'), 1, 0, ContextRouteState.UNROUTED, None, 'b' * 64,\n"
        ")\n"
        "task_request: TaskExecutionEnvelopeRequest = TaskExecutionEnvelopeRequest(\n"
        "    RunId('run-1'), 'call-1', 'effect-1', 'raw-1', 1, 0, 'read', policy, None,\n"
        ")\n"
        "envelope: TaskExecutionEnvelope = TaskExecutionEnvelope(\n"
        "    RunId('run-1'), CallId('call-1'), EffectId('effect-1'), 'raw-1',\n"
        "    1, 0, 'read', 'host:read', 'a' * 64, None, None, None, None, None,\n"
        "    None, 'effect-1',\n"
        ")\n"
        "context_authority: RunContextAuthorityPort = ContextAuthority()\n"
        "task_authority: TaskExecutionAuthorityPort = TaskAuthority()\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("MYPYPATH", None)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--python-executable",
            str(python),
            str(consumer),
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
