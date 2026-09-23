# SPDX-License-Identifier: Apache-2.0
"""The deployment's check-policy projector runs *before* a content review is prepared.

Host real-model run 7 (2026-09-23, mission-ac2eddca3c7a1213, Grok lane): the plan
froze the Scope and the verification ran the content review inside one ``run()``,
so the Host's after-run projection came too late and the review still reported
CHECK_POLICY_UNRESOLVED. ``AssuranceDeploymentPorts.check_policy_projector`` is
handed to the review runtime, which calls it right before preparing the review.
"""

from __future__ import annotations

import asyncio
import inspect

from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator import assurance_review_runtime as runtime_module
from agent_orchestrator.orchestrator.assurance_assembly import (
    AssuranceDeploymentPorts,
    install_assurance,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

TENANT = "tenant-projector"
PRINCIPAL = Principal("projector-user")


def _install(orch, calls):
    install_assurance(orch, AssuranceDeploymentPorts(
        tenant_id=TENANT, principal=PRINCIPAL,
        select_profile=lambda spec: AssurancePolicy(),
        check_policy_projector=calls.append, host_fingerprint="cd" * 32,
    ))


def test_projector_is_threaded_to_the_review_runtime(tmp_path):
    calls: list[str] = []

    async def scenario():
        cfg = OrchestratorConfig(evidence_root=tmp_path / "root")

        def root_setup(orch):
            orch.commit.install_assurance_root(principal=PRINCIPAL, tenant_id=TENANT, command_id="install")

        async with Orchestrator(cfg, RoleScriptedProvider({}), assurance_root_setup=root_setup,
                                startup_assembly=lambda orch: _install(orch, calls)) as orch:
            runtime = orch._assurance_reviews
            assert isinstance(runtime, runtime_module.AssuranceReviewRuntime)
            assert runtime._check_policy_projector == calls.append
            # Source pin: the projector is consulted before the review is prepared,
            # never inside a transaction (the preparation itself forbids one).
            body = inspect.getsource(runtime_module.AssuranceReviewRuntime)
            assert body.index("self._check_policy_projector(mission.id)") < body.index(
                "invocation = ensure_task_content_review(")
            assert "not self.store.connection.in_transaction" in body

    asyncio.run(scenario())


def test_without_a_projector_the_runtime_keeps_the_human_verb_as_the_only_source(tmp_path):
    async def scenario():
        cfg = OrchestratorConfig(evidence_root=tmp_path / "root")

        def root_setup(orch):
            orch.commit.install_assurance_root(principal=PRINCIPAL, tenant_id=TENANT, command_id="install")

        def assembly(orch):
            install_assurance(orch, AssuranceDeploymentPorts(
                tenant_id=TENANT, principal=PRINCIPAL, select_profile=lambda spec: AssurancePolicy(),
                host_fingerprint="cd" * 32))

        async with Orchestrator(cfg, RoleScriptedProvider({}), assurance_root_setup=root_setup,
                                startup_assembly=assembly) as orch:
            assert orch._assurance_reviews._check_policy_projector is None

    asyncio.run(scenario())
