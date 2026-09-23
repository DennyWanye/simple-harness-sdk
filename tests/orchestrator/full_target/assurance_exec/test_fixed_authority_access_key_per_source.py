# SPDX-License-Identifier: Apache-2.0
"""The production current-read authority keys its ACCESS witness per source.

Host real-model runs 8 and 9 (2026-09-23, Grok lane): every TASK_CONTENT review
preparation failed with RECHECK_REQUIRED out of ``_merge_reads`` because
``FixedPrincipalAuthority`` issued one ACCESS key per (caller, mission, use) with a
per-source fingerprint, so any read set with two or more sources conflicted with
itself. The seam fixture's authority always keyed per source, which is why no
seam caught it.
"""

from __future__ import annotations

import asyncio

from agent_orchestrator.assurance.certificates import UseIdentity
from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_assembly import (
    AssuranceDeploymentPorts,
    install_assurance,
)
from agent_orchestrator.orchestrator.assurance_check_use import _merge_reads
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

TENANT = "tenant-access-key"
PRINCIPAL = Principal("access-key-user")


def test_two_sources_get_two_access_keys_and_merge_without_conflict(tmp_path):
    async def scenario():
        cfg = OrchestratorConfig(evidence_root=tmp_path / "root")

        def root_setup(orch):
            orch.commit.install_assurance_root(principal=PRINCIPAL, tenant_id=TENANT, command_id="install")

        def assembly(orch):
            install_assurance(orch, AssuranceDeploymentPorts(
                tenant_id=TENANT, principal=PRINCIPAL, select_profile=lambda spec: AssurancePolicy(),
                host_fingerprint="ef" * 32))

        async with Orchestrator(cfg, RoleScriptedProvider({}), assurance_root_setup=root_setup,
                                startup_assembly=assembly) as orch:
            mission, _ = orch.commit.create_mission(MissionSpec(
                goal="access key per source", success_criteria=("one",), tenant_id=TENANT,
                idempotency_key="assured-access-key",
                orchestration_semantics_version="hierarchical",
                planning_protocol_version="planning-decision-v1"))
            authority = orch._assurance_reviews.consumer.authority
            identity = UseIdentity(mission.id, "REVIEW", "review-key", "scope-x",
                                   PRINCIPAL.principal_id, "DISCLOSE", "root-x")
            first = authority(identity, AssuranceRef("result", Pin("result-1", 0, "1" * 64)))
            second = authority(identity, AssuranceRef("task", Pin("task-1", 1, "2" * 64)))
            assert first.access.channel == second.access.channel == "ACCESS"
            assert first.access.key != second.access.key
            assert first.access.fingerprint != second.access.fingerprint
            # The same source read twice is one observation; two sources are two.
            merged = _merge_reads([first.access, first.policy, second.access, second.policy, first.access])
            assert len([item for item in merged if item.channel == "ACCESS"]) == 2
            assert len([item for item in merged if item.channel == "POLICY"]) == 1

    asyncio.run(scenario())
