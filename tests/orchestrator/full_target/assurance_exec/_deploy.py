# SPDX-License-Identifier: Apache-2.0
"""Shared real deployment for the assurance_exec cases.

A real ``Orchestrator`` started with ``install_assurance`` as its deployment
assembly (native root, fixed principal authority, factory selector, four
consumers, NOTIFY transport captured in ``sent``). Missions whose idempotency
key starts with ``assured`` get the ASSURANCE_1_1 lane; others keep the original
protocol. No model, no Host: ``RoleScriptedProvider({})`` never answers.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_assembly import (
    AssuranceDeploymentPorts,
    install_assurance,
)
from agent_orchestrator.orchestrator.assurance_consumers import NOTIFICATION_EVENT
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

TENANT = "tenant-exec"
PRINCIPAL = Principal("exec-current-user")
HOST_FINGERPRINT = "cd" * 32


def spec(key: str, **extra) -> MissionSpec:
    return MissionSpec(
        goal="assured " + key,
        success_criteria=("the answer file is written", "it names the fixture"),
        tenant_id=TENANT,
        idempotency_key=key,
        orchestration_semantics_version="hierarchical",
        planning_protocol_version="planning-decision-v1",
        **extra,
    )


@asynccontextmanager
async def deployment(root):
    cfg = OrchestratorConfig(evidence_root=root / "root")
    sent: list = []
    installed: list = []

    def root_setup(orch):
        orch.commit.install_assurance_root(principal=PRINCIPAL, tenant_id=TENANT, command_id="install")

    def assembly(orch):
        installed.append(install_assurance(orch, AssuranceDeploymentPorts(
            tenant_id=TENANT, principal=PRINCIPAL,
            select_profile=lambda s: AssurancePolicy() if s.idempotency_key.startswith("assured") else None,
            notify_transport=sent.append, host_fingerprint=HOST_FINGERPRINT,
        )))

    async with Orchestrator(cfg, RoleScriptedProvider({}), assurance_root_setup=root_setup,
                            startup_assembly=assembly) as orch:
        yield SimpleNamespace(orch=orch, store=orch.store, commit=orch.commit, sent=sent,
                              installed=installed[0], cfg=cfg)


def emit_notification(world, mission) -> None:
    """One more durable Store event on the Mission (moves its head), as the
    original terminal writers do when they request a notification."""
    final = [e for e in world.store.list_events(mission.id) if e.type == "MissionCreated"][0]
    world.commit._emit(NOTIFICATION_EVENT, mission.id, key=final.id + ":" + str(world.store.now),
                       payload={"final_event_id": final.id, "state_version": mission.version,
                                "final_event_type": final.type})


def second_connection(store) -> sqlite3.Connection:
    """An independent SQLite connection to the same database file (another writer)."""
    connection = sqlite3.connect(str(store.path), isolation_level=None, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection
