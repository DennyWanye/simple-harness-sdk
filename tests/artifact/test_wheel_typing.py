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
        "    AgentIdentity, AgentMemoryPort, CallId, CanonicalWorkspaceRoot, CommittedTurn,\n"
        "    ContextAssemblyDecision, ContextFragment, ContextRouteState,\n"
        "    DisclosureContext, EffectId, FilesystemIdentity, FilesystemIdentityKind,\n"
        "    HostIssuedRunBindingModeSnapshot, ManualWorkspaceBindingAuthorizationReceipt,\n"
        "    ManualWorkspaceBindingChallenge,\n"
        "    LongTermMemoryType, MemoryAnalysisDeliveryAuthorityPort,\n"
        "    MemoryAnalysisDeliveryReceipt, MemoryAnalysisExecutorPort,\n"
        "    MemoryAnalysisRequest, MemoryAnalysisResult, MemoryAnalysisResultEnvelope,\n"
        "    RecallPlan,\n"
        "    RunContextAuthorityPort, RunContextAuthorityRequest, RunContextSnapshot,\n"
        "    RunBindingModeSnapshotRequest, RunId, SanitizedEvidenceEnvelope,\n"
        "    TaskExecutionAuthorityPort,\n"
        "    TaskExecutionEnvelope, TaskExecutionEnvelopeRequest,\n"
        "    TaskScopeMutationPlan, TaskScopeProposal, TaskScopeRoute,\n"
        "    ToolEffectClass, ToolExecutionPolicy, ToolRouteRequirement,\n"
        "    ToolTaskScopeRequirement, WorkspaceBindingAuthorityGrant,\n"
        "    WorkspaceBindingAuthorityPort, WorkspaceBindingProposal,\n"
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
        "class WorkspaceAuthority:\n"
        "    async def verify_manual_authorization(\n"
        "        self, proposal: WorkspaceBindingProposal,\n"
        "        challenge: ManualWorkspaceBindingChallenge,\n"
        "        receipt: ManualWorkspaceBindingAuthorizationReceipt,\n"
        "    ) -> WorkspaceBindingAuthorityGrant:\n"
        "        raise NotImplementedError(proposal, challenge, receipt)\n"
        "    async def issue_run_binding_mode_snapshot(\n"
        "        self, request: RunBindingModeSnapshotRequest,\n"
        "    ) -> HostIssuedRunBindingModeSnapshot:\n"
        "        raise NotImplementedError(request)\n"
        "    async def authorize_auto_binding(\n"
        "        self, proposal: WorkspaceBindingProposal,\n"
        "        snapshot: HostIssuedRunBindingModeSnapshot,\n"
        "    ) -> WorkspaceBindingAuthorityGrant:\n"
        "        raise NotImplementedError(proposal, snapshot)\n"
        "    async def verify_binding_grant(\n"
        "        self, proposal: WorkspaceBindingProposal,\n"
        "        grant: WorkspaceBindingAuthorityGrant,\n"
        "    ) -> None:\n"
        "        raise NotImplementedError(proposal, grant)\n\n"
        "analysis_result = MemoryAnalysisResult(\n"
        "    'job-1', 'run-1', 'c' * 64, None, {'outcome': 'no_mutation'}, 1, 1, 0, 1,\n"
        ")\n"
        "analysis_delivery = MemoryAnalysisDeliveryReceipt(\n"
        "    'delivery-1', 'host-1', 'run-1', 'job-1', 'c' * 64,\n"
        "    analysis_result.result_hash, 1, None, 'd' * 64, 1.0,\n"
        "    'host-record-1', 'e' * 64,\n"
        ")\n"
        "analysis_envelope = MemoryAnalysisResultEnvelope(analysis_result, analysis_delivery)\n\n"
        "class AnalysisExecutor:\n"
        "    async def analyze_memory(\n"
        "        self, request: MemoryAnalysisRequest,\n"
        "    ) -> MemoryAnalysisResultEnvelope:\n"
        "        return analysis_envelope\n\n"
        "class AnalysisDeliveryAuthority:\n"
        "    async def verify_analysis_delivery(\n"
        "        self, request: MemoryAnalysisRequest, envelope: MemoryAnalysisResultEnvelope,\n"
        "    ) -> None:\n"
        "        envelope.verify_request(request)\n\n"
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
        "analysis_executor: MemoryAnalysisExecutorPort = AnalysisExecutor()\n"
        "analysis_delivery_authority: MemoryAnalysisDeliveryAuthorityPort = (\n"
        "    AnalysisDeliveryAuthority()\n"
        ")\n"
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
        "task_authority: TaskExecutionAuthorityPort = TaskAuthority()\n"
        "fs_identity = FilesystemIdentity(FilesystemIdentityKind.POSIX_INODE, 'dev-1', 'ino-2')\n"
        "workspace_root = CanonicalWorkspaceRoot('root-1', '/workspace/root-1', fs_identity)\n"
        "binding_proposal = WorkspaceBindingProposal(\n"
        "    'proposal-1', 'run-1', 'actor-1', 'task-1', workspace_root, 1, 'append-1',\n"
        ")\n"
        "workspace_authority: WorkspaceBindingAuthorityPort = WorkspaceAuthority()\n",
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
