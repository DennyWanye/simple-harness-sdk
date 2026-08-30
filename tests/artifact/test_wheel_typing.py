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
        "    AgentIdentity, AgentMemoryPort, CommittedTurn, ContextAssemblyDecision,\n"
        "    ContextFragment, DisclosureContext,\n"
        "    LongTermMemoryType, MemoryAnalysisExecutorPort, RecallPlan,\n"
        "    SanitizedEvidenceEnvelope, TaskScopeMutationPlan, TaskScopeProposal,\n"
        "    TaskScopeRoute,\n"
        ")\n"
        "from simple_harness.execution.sqlite import (\n"
        "    ExecutionMigrationManifest, LegacyDisposition, LegacyIdentityBinding,\n"
        "    LegacyIdentityMap, MigrationManifestEntry, migrate_execution_v3_to_v4,\n"
        ")\n\n"
        "def inspect(port: AgentMemoryPort, manifest: ExecutionMigrationManifest) -> str:\n"
        "    return manifest.digest\n\n"
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
        "route: TaskScopeRoute = TaskScopeRoute.CREATE_NEW\n",
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
