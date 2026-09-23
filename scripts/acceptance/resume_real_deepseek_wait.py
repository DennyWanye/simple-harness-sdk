#!/usr/bin/env python3
"""Resume one interrupted real-provider H1-I WAIT probe without reseeding it.

The original probe is immutable.  This script opens its existing runtime database,
rebuilds only the explicit hierarchical assembly against the original repository,
and uses the authenticated host-test issuer API to bind an otherwise ungranted wake
Planner request.  It never writes a model response or a Task state directly.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FULL_TARGET = ROOT / "tests" / "orchestrator" / "full_target"
for path in (ROOT / "src", ROOT / "tests" / "agents", FULL_TARGET):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_h1i_production_entry as h1i  # noqa: E402
import test_real_provider_hierarchical_smoke as real_smoke  # noqa: E402
from real_provider_config import build_real_provider, resolve_real_provider  # noqa: E402

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi  # noqa: E402
from agent_orchestrator.governance.permissions import Principal  # noqa: E402
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore  # noqa: E402
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore  # noqa: E402
from agent_orchestrator.testing.fixtures import role_of  # noqa: E402

DEFAULT_EVIDENCE = ROOT / ".local-test-evidence" / "2026-09-21" / "h1i-real-wait-175206"
MAX_SECONDS = 240
MAX_PHYSICAL_CALLS = 20


class SerialRealProvider:
    """Counts calls and permits exactly one physical provider call at a time."""

    def __init__(self, delegate: Any, *, max_calls: int) -> None:
        self._delegate = delegate
        self._serial = asyncio.Semaphore(1)
        self._max_calls = max_calls
        self.cap_reached = asyncio.Event()
        self.physical_calls = 0
        self.roles: list[str] = []

    async def invoke(self, request: Any, *, cancel: Any) -> Any:
        async with self._serial:
            if self.physical_calls >= self._max_calls:
                # Do not manufacture a provider failure after handoff.  The runner
                # observes this event, cancels its own loop, and records that origin.
                self.cap_reached.set()
                await asyncio.Event().wait()
            self.physical_calls += 1
            self.roles.append(role_of(request))
            return await self._delegate.invoke(request, cancel=cancel)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--max-seconds", type=int, default=MAX_SECONDS)
    parser.add_argument("--max-calls", type=int, default=MAX_PHYSICAL_CALLS)
    args = parser.parse_args()
    if args.max_seconds < 1 or args.max_calls < 1:
        parser.error("--max-seconds and --max-calls must be positive")
    args.evidence = args.evidence.resolve()
    return args


def _existing_mission_id(evidence: Path) -> str:
    summary = evidence / "summary.json"
    if summary.is_file():
        value = json.loads(summary.read_text(encoding="utf-8")).get("mission_id")
        if value:
            return str(value)
    raise RuntimeError("summary.json has no mission_id; refusing to choose a Mission")


def _resume_output_path(evidence: Path) -> Path:
    return evidence / f"summary-resume-{time.time_ns()}.json"


def _exit_code(status: str, mission_status: str | None) -> int:
    return 0 if status == "TERMINAL" and mission_status == "COMPLETED" else 1


def _resume_permitted(
    issued: list[dict[str, str]],
    authorised_pending: list[str],
    pending_root_reviews: list[dict[str, str]],
) -> bool:
    return bool(issued or authorised_pending or pending_root_reviews)


async def main() -> int:
    args = _arguments()
    evidence = args.evidence
    output = _resume_output_path(evidence)
    started = time.monotonic()
    summary: dict[str, Any] = {
        "status": "INCOMPLETE",
        "source_evidence": str(evidence),
        "limits": {"seconds": args.max_seconds, "additional_physical_calls": args.max_calls},
        "known_limitations": [
            "Resumes the existing SDK probe; its original summary is preserved.",
            "Nonterminal results are PARTIAL/ERROR, never full gate acceptance.",
        ],
    }
    config = resolve_real_provider()
    if config is None:
        summary.update(status="BLOCKED", obstacle="real_provider_config_unavailable")
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2
    try:
        mission_id = _existing_mission_id(evidence)
        raw_provider = build_real_provider(config, timeout=120)
        provider = SerialRealProvider(raw_provider, max_calls=args.max_calls)
        runtime = h1i.OrchestratorConfig(
            evidence_root=evidence / "runtime",
            model=config.model,
            max_concurrency=1,
            max_concurrent_model_calls=1,
            default_max_output_tokens=4096,
            test_timeout_seconds=120,
            turn_deadline_seconds=args.max_seconds,
        )
        async with h1i.Orchestrator(
            runtime, provider, critic_wait_seconds=args.max_seconds
        ) as loop:
            mission = loop.store.get_mission(mission_id)
            if mission is None:
                raise RuntimeError("summary Mission is absent from the existing runtime database")
            environment = h1i.build_planning_world(
                mission.id,
                domains=("code",),
                semantics=h1i.HtnStore(loop.store),
                deployed_layers=h1i.deployed_layers(runtime.deployment_policy),
                observers=h1i.code_observers(evidence / "repo", allow_test_execution=True),
            )
            loop.install_hierarchical(planning=environment)

            decisions = PlanningDecisionStore(loop.store)
            admissions = PlanningAdmissionStore(loop.store)
            issued: list[dict[str, str]] = []
            authorised_pending: list[str] = []
            pending_root_reviews = [
                {
                    "intent_id": intent.intent_id,
                    "prompt_version": str(intent.config.get("prompt_version", "")),
                    "state": intent.state,
                }
                for intent in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if intent.mission_id == mission.id
                and str(intent.config.get("role", "")) == "root_reviewer"
            ]
            for intent in loop.store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
            ):
                if intent.mission_id != mission.id or intent.kind != "plan":
                    continue
                request = decisions.get_planning_request_for_intent(intent.intent_id)
                if request is None:
                    continue
                if admissions.get_request_binding(request.request_id) is not None:
                    authorised_pending.append(request.request_id)
                    continue
                # This is an explicit host-test issuer action.  It binds the exact
                # persisted request identity; it neither fabricates a grant nor edits
                # the Planner intent/package that the model will receive.
                receipt = PlanningAuthorizationApi(
                    loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
                ).issue(
                    mission.id,
                    command_id=f"resume-host-grant:{evidence.name}:{request.request_id}",
                    request_id=request.request_id,
                )
                issued.append({"request_id": request.request_id, "grant_id": receipt.grant_id})
            if not _resume_permitted(issued, authorised_pending, pending_root_reviews):
                summary.update(
                    status="BLOCKED", obstacle="no runnable pending Planner or root-review request"
                )
            else:
                runner = asyncio.create_task(loop.run(max_cycles=400))
                cap_wait = asyncio.create_task(provider.cap_reached.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {runner, cap_wait},
                        timeout=args.max_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cap_wait in done:
                        summary["interruption_origin"] = "runner_call_cap_cancel"
                        runner.cancel()
                        await asyncio.gather(runner, return_exceptions=True)
                        summary["status"] = "PARTIAL"
                    elif runner in done:
                        await runner
                    else:
                        summary["interruption_origin"] = "runner_timeout_cancel"
                        runner.cancel()
                        await asyncio.gather(runner, return_exceptions=True)
                        summary["status"] = "TIMEOUT"
                except TimeoutError:
                    summary["status"] = "TIMEOUT"
                else:
                    if summary["status"] == "INCOMPLETE":
                        final = loop.store.get_mission(mission.id)
                        summary["status"] = (
                            "TERMINAL"
                            if final is not None
                            and str(final.status) in {"COMPLETED", "FAILED", "CANCELLED"}
                            else "PARTIAL"
                        )
                finally:
                    cap_wait.cancel()
                    await asyncio.gather(cap_wait, return_exceptions=True)
                    if not runner.done():
                        runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
                summary["issued_host_test_grants"] = issued
                summary["existing_authorised_requests"] = authorised_pending
            summary["pending_root_review_requests"] = pending_root_reviews
            events = loop.store.list_events(mission.id)
            summary.update(
                mission_id=mission.id,
                model=config.model,
                elapsed_seconds=round(time.monotonic() - started, 3),
                physical_provider_calls=provider.physical_calls,
                provider_roles=provider.roles,
                mission_status=str(loop.store.get_mission(mission.id).status),
                stop_reason=str(loop.store.get_mission(mission.id).stop_reason),
                usage=real_smoke._account(loop, mission.id),
                accepted_outputs=[
                    event.payload.get("accepted_outputs")
                    for event in events
                    if event.type == "AcceptanceCommitted"
                ],
                decisions=[
                    event.payload for event in events if event.type == "PlanningDecisionEvaluated"
                ],
                wakes=[event.payload for event in events if event.type == "PlanningWaitWoken"],
            )
    except Exception as error:  # no provider settings appear in the receipt or stdout
        summary.update(status="ERROR", error_type=type(error).__name__, error=str(error)[:500])
    finally:
        summary["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    key: summary.get(key)
                    for key in ("status", "elapsed_seconds", "physical_provider_calls", "obstacle")
                },
                ensure_ascii=False,
            )
        )
    return _exit_code(str(summary["status"]), summary.get("mission_status"))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
