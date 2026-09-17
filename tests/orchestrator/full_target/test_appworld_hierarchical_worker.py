# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3d / defect D1: an AppWorld Mission in the hierarchical mode keeps its domain.

Every one of the Grok acceptance run's 20 L1 episodes reported the same thing from
inside the Worker:

    tools_and_permissions.allowed_tools includes ``appworld_execute``, but this
    attempt's exposed tool schema only contains workspace_list / workspace_read_file /
    workspace_write_file

``_hierarchical_worker_template`` returned the constant ``WORKER_HIERARCHICAL`` for
every prompt version outside a one-element frozenset, which threw away the
``worker-appworld-v3`` template the line above had selected.  ``WORKER_HIERARCHICAL``
is ``_revise(WORKER, …)`` and ``_revise`` copies ``tool_names``, so the role carried
the code domain's four tools; ``effective_tools`` iterates ``role_tools`` and
intersects, so ``appworld_execute`` — present in the Mission, Task and deployment sets
— was dropped for not being in the role's.  The prompt went with it: the Worker was
told to run pytest with ``run_tests`` while operating a simulated world.

The invariant below is the one the diagnosis called "the only one that can stop this".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_end_to_end import HIERARCHICAL_SEMANTICS, committed  # noqa: E402

from agent_orchestrator.governance.domains import APPWORLD_DOMAIN  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.runtime.appworld_templates import (  # noqa: E402
    WORKER_APPWORLD_HIERARCHICAL_VERSION,
)
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.runtime.role_templates import (  # noqa: E402
    WORKER_HIERARCHICAL_VERSION,
)
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider  # noqa: E402

APPWORLD_TOOLS = (
    "workspace_read_file",
    "workspace_write_file",
    "workspace_list",
    "appworld_execute",
)


def _attempt_intents(tmp_path, *, domain: str | None, tools):
    """Drive one real ``_decide`` and return the Worker intents it created."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = committed(
        evidence,
        key=f"p23d-worker-{domain}",
        mode=HIERARCHICAL_SEMANTICS,
        demand=True,
        domain=domain,
        tools=tools,
    )
    mission_id = world.mission.id
    world.store.close()
    config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1, test_timeout_seconds=5)

    async def case():
        async with Orchestrator(config, RoleScriptedProvider({"worker": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(mission_id)
            assert mission is not None
            await loop._decide(mission)
            return [
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if item.mission_id == mission_id and item.kind == "attempt"
            ]

    return asyncio.run(case())


def test_an_appworld_hierarchical_attempt_exposes_appworld_execute(tmp_path) -> None:
    """The one invariant that stops defect D1.

    **Mutation**: put back the unconditional ``return WORKER_HIERARCHICAL`` and the
    tool disappears from the intent, exactly as it did in the evidence pack.
    """

    intents = _attempt_intents(tmp_path, domain=APPWORLD_DOMAIN, tools=APPWORLD_TOOLS)
    assert intents, "a demanded leaf reaches the allocator and gets an intent"
    allowed = tuple(intents[0].config["allowed_tools"])
    assert "appworld_execute" in allowed, allowed


def test_the_prompt_is_the_appworld_hierarchical_one(tmp_path) -> None:
    """Restoring the tool without the words would make the L1 numbers meaningless."""

    intents = _attempt_intents(tmp_path, domain=APPWORLD_DOMAIN, tools=APPWORLD_TOOLS)
    assert intents[0].config["prompt_version"] == WORKER_APPWORLD_HIERARCHICAL_VERSION
    message = intents[0].config.get("message")
    content = str(message.get("content", "") if isinstance(message, dict) else message or "")
    assert "appworld_execute" in content
    assert "run_tests" not in content, (
        "an AppWorld Worker must not be told to run pytest; that is the code domain's "
        "prompt, and reading it while operating a simulated world is defect D1's "
        "collateral damage"
    )
    assert "declared_output_ports" in content, (
        "it is still a hierarchical Worker: decision 4's outputs field is the whole "
        "reason a hierarchical version exists"
    )


def test_a_code_domain_mission_still_gets_the_code_hierarchical_worker(tmp_path) -> None:
    """The other half: the fallback is per domain, not switched off."""

    intents = _attempt_intents(tmp_path, domain=None, tools=None or APPWORLD_TOOLS)
    assert intents[0].config["prompt_version"] == WORKER_HIERARCHICAL_VERSION


def test_the_appworld_domain_names_a_registered_hierarchical_worker() -> None:
    """A domain naming a version this build does not register is a deployment error."""

    from agent_orchestrator.governance.domains import APPWORLD_PROFILE
    from agent_orchestrator.runtime.role_templates import (
        HIERARCHICAL_WORKER_ROLE_KEY,
        hierarchical_worker_for_domain,
        hierarchical_worker_versions,
    )

    named = APPWORLD_PROFILE.role_templates[HIERARCHICAL_WORKER_ROLE_KEY]
    assert named in hierarchical_worker_versions()
    assert hierarchical_worker_for_domain(APPWORLD_PROFILE).prompt_version == named


def test_a_domain_naming_an_unregistered_version_is_refused() -> None:
    from dataclasses import replace

    from agent_orchestrator.contracts import ContractError
    from agent_orchestrator.governance.domains import APPWORLD_PROFILE
    from agent_orchestrator.runtime.role_templates import (
        HIERARCHICAL_WORKER_ROLE_KEY,
        hierarchical_worker_for_domain,
    )

    broken = replace(
        APPWORLD_PROFILE,
        role_templates={
            **dict(APPWORLD_PROFILE.role_templates),
            HIERARCHICAL_WORKER_ROLE_KEY: "worker-appworld-hierarchical-v99",
        },
    )
    import pytest

    with pytest.raises(ContractError, match="unavailable domain prompt"):
        hierarchical_worker_for_domain(broken)
