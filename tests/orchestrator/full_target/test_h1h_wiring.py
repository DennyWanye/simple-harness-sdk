# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-H wiring: the durable request identity survives the bounded format retry.

The new protocol asks the Planner again **only** for a reply the codec could not
read, it asks **at most once**, and the retry is the *same* §34 request seen again
at attempt ordinal 1 — not a fresh request.  The retry therefore reuses the
existing "proposal unreadable -> re-ask with the repair hint" ladder rather than
opening a second loop, and the binding table keeps the id of the intent that
actually answered.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.context.context_builder import TaskPackage
from agent_orchestrator.contracts import Budget
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import (
    PLANNING_DECISION_V1,
    PlanningDecisionStatus,
)
from agent_orchestrator.orchestrator.commit_service import (
    PLANNING_DECISION_V1 as MISSION_PLANNING_DECISION_V1,
)
from agent_orchestrator.orchestrator.commit_service import (
    CommitService,
    MissionSpec,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.hierarchical_dispatch import append_hierarchical_event
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import DispatchIntent, Store


def _intent(intent_id: str, *, ordinal: int = 1, mission_id: str = "mission") -> DispatchIntent:
    return DispatchIntent(
        intent_id=intent_id,
        kind="plan",
        subject_id=f"{mission_id}:planner:{ordinal}",
        mission_id="mission",
        state="PENDING",
        version=1,
        creation_key=f"{mission_id}:planner:{ordinal}",
        input_id="attempt-input",
        input_hash="a" * 64,
        config={"ordinal": ordinal},
        expected_turn_id=None,
        agent_id=None,
        receipt=None,
        lease_owner=None,
        lease_expires_at=None,
        replays=0,
        created_at=123.0,
    )


class _Mode:
    def scope_epochs(self, mission_id: str) -> dict[str, int]:
        assert mission_id
        return {"mission": 7}

    def semantics(self) -> object:
        return SimpleNamespace(latest_requirements_revision=lambda mission_id: None)


def _package() -> TaskPackage:
    return TaskPackage(
        text="sealed",
        context_version="ctx-test",
        package={
            "context_builder_version": "context-builder-v4",
            "mode": "hierarchical",
            "planning_protocol": {
                "protocol": PLANNING_DECISION_V1,
                "enabled_decision_types": ["REFINE"],
            },
            "planning_subjects": [{"subject_key": "subject-a", "occurrence_id": "o1"}],
            "visible_refs": [
                {
                    "kind": "task",
                    "id": "task-a",
                    "semantic_revision": 1,
                    "content_hash": "b" * 64,
                }
            ],
            "package_version": 4,
            "plan": {"plan_revision": 3},
        },
    )


def _mission(store: Store, *, protocol: str):
    mission, _ = CommitService(store).create_mission(
        MissionSpec(
            goal="goal",
            success_criteria=("done",),
            tenant_id="tenant",
            idempotency_key=protocol,
            budget=Budget(max_tokens=1000, max_attempts=1),
            planning_protocol_version=protocol,
        )
    )
    return mission


def _orchestrator(store: Store) -> Orchestrator:
    orchestrator = object.__new__(Orchestrator)
    orchestrator._store = store
    # This protocol seam bypasses full runtime assembly but still journals real
    # raw bytes in the same content-addressed store used by production.
    orchestrator._assembled = SimpleNamespace(
        workspaces=SimpleNamespace(artifact_store=ArtifactStore(store.path.parent / "artifacts"))
    )
    orchestrator._owner = "test-owner"
    # The refusal paths write a line to the progress log; a plain list is all they need.
    orchestrator.progress_log = []
    orchestrator._settle_intent = lambda intent, state: None
    orchestrator._settle_service_if_known = lambda subject_id, mission_id: None

    async def legacy_rejection(intent, *, reason, detail):
        append_hierarchical_event(
            store,
            "PlanningRejected",
            intent.mission_id,
            key=intent.intent_id,
            payload={"ordinal": 1, "reason": reason, "detail": dict(detail)},
        )

    orchestrator._planning_rejected = legacy_rejection
    return orchestrator


def _bind(orchestrator: Orchestrator, mission, intent, *, request_id: str | None = None) -> None:
    object.__setattr__(intent, "mission_id", mission.id)
    orchestrator._bind_hierarchical_planning_request(
        intent=intent,
        mission=mission,
        new_mode=_Mode(),
        package=_package(),
        template=SimpleNamespace(
            prompt_version="planner-hierarchical-v9", instructions="prompt"
        ),
        request_id=request_id,
    )


def _evaluate(orchestrator: Orchestrator, mission, intent, text: str) -> None:
    asyncio.run(
        orchestrator._collect_plan_decision(intent, SimpleNamespace(), mission, text, _Mode())
    )


def _rows(store: Store, mission_id: str, event_type: str) -> list:
    return [event for event in store.list_events(mission_id) if event.type == event_type]


def _open_round(
    store: Store,
    mission,
    orchestrator: Orchestrator,
    *,
    ordinal: int,
    intent_id: str,
    text: str,
    request_id: str | None = None,
) -> DispatchIntent:
    """Dispatch one Planner round for real: the intent is in the library, then bound."""

    intent = _intent(intent_id, ordinal=ordinal, mission_id=mission.id)
    object.__setattr__(intent, "mission_id", mission.id)
    store.insert_intent(intent)
    _bind(orchestrator, mission, intent, request_id=request_id)
    _evaluate(orchestrator, mission, intent, text)
    return intent


def _read_format_failure(store: Store, mission, text: str):
    """Run one unreadable round at ordinal 1 and return (orchestrator, intent)."""

    orchestrator = _orchestrator(store)
    first = _open_round(
        store, mission, orchestrator, ordinal=1, intent_id="planner-1", text=text
    )
    return orchestrator, first


_UNREADABLE = "<planning_decision>{not-json}</planning_decision>"


def test_format_retry_carries_the_original_request_id(tmp_path) -> None:
    """The retry resolves to the original request_id — it is the same §34 request."""

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    orchestrator, first = _read_format_failure(store, mission, _UNREADABLE)

    # The first round refused the reply as unreadable, which is what authorises the ask.
    attempt_zero = PlanningDecisionStore(store).get_planning_decision_by_attempt(
        first.intent_id, 0
    )
    assert attempt_zero is not None
    assert attempt_zero["status"] == "UNREADABLE"

    # The ladder opens the next ordinal as its own intent; the identity is the first's.
    retry = _intent("plan-request-retry", ordinal=2, mission_id=mission.id)
    object.__setattr__(retry, "mission_id", mission.id)
    store.insert_intent(retry)

    assert (
        orchestrator._planning_request_retry_id(
            intent=retry, mission=mission, new_mode=_Mode()
        )
        == first.intent_id
    )


def test_the_retry_answers_the_openers_request_at_ordinal_one(tmp_path) -> None:
    """H1-H item 3: the re-ask is the same request, so its decision lands on that row.

    The retry's *decision* is the observable half of "same request_id"; the row's
    ``intent_id`` column, which would name the retry, is the blocked half and is asserted
    separately below.
    """

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    orchestrator, first = _read_format_failure(store, mission, _UNREADABLE)

    retry = _intent("plan-request-retry", ordinal=2, mission_id=mission.id)
    object.__setattr__(retry, "mission_id", mission.id)
    store.insert_intent(retry)
    _bind(orchestrator, mission, retry, request_id=first.intent_id)

    # The retry answers the opener's request, so its evaluation is attempt ordinal 1 of
    # that request — not attempt 0 of a request of its own.
    _evaluate(orchestrator, mission, retry, _UNREADABLE)

    attempts = [
        PlanningDecisionStore(store).get_planning_decision_by_attempt(first.intent_id, ordinal)
        for ordinal in (0, 1)
    ]
    assert [row is not None for row in attempts] == [True, True]
    assert [row["status"] for row in attempts] == ["UNREADABLE", "UNREADABLE"]

    evaluated = _rows(store, mission.id, "PlanningDecisionEvaluated")
    assert [event.payload["attempt_ordinal"] for event in evaluated] == [0, 1]
    assert {event.payload["request_id"] for event in evaluated} == {first.intent_id}


def test_the_retry_moves_the_intent_id_column_to_the_answering_intent(tmp_path) -> None:
    """Item 3, the identity half: the column names the intent that actually answered.

    The retry re-answers the opener's *request*, so the row keeps that request's id and
    its ``created_at``; only ``intent_id`` moves, and it moves through the store's one
    constrained operation rather than a second row.  The orchestrator never writes
    ``planning_requests`` itself.
    """

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    orchestrator, first = _read_format_failure(store, mission, _UNREADABLE)

    retry = _intent("plan-request-retry", ordinal=2, mission_id=mission.id)
    object.__setattr__(retry, "mission_id", mission.id)
    store.insert_intent(retry)
    _bind(orchestrator, mission, retry, request_id=first.intent_id)

    binding = PlanningDecisionStore(store).get_planning_request(first.intent_id)
    assert binding is not None
    # Still one row — the retry did not open a second request — and its intent_id names
    # the retry, which is the intent that answered.  The request identity is unchanged.
    assert binding.intent_id == retry.intent_id
    assert binding.request_id == first.intent_id
    assert binding.created_at == first.created_at
    rows = store.connection.execute(
        "SELECT request_id, intent_id FROM planning_requests"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(first.intent_id, retry.intent_id)]


def test_the_format_retry_is_bounded_to_one_ask(tmp_path) -> None:
    """Ordinal 2 is the only re-ask: a third round does not inherit the identity."""

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    orchestrator, first = _read_format_failure(store, mission, _UNREADABLE)

    retry = _intent("plan-request-retry", ordinal=2, mission_id=mission.id)
    object.__setattr__(retry, "mission_id", mission.id)
    store.insert_intent(retry)

    # The retry has now been spent: it still answers the same request, but its own
    # ordinal is the last one §5.2 allows, so a further round opens a new request.
    assert orchestrator._planning_format_retry_remaining(intent=retry, mission=mission) == 0
    assert orchestrator._format_retry_exhausted(
        intent=retry, reason="proposal_unreadable", mission=mission
    )

    third = _intent("plan-request-third", ordinal=3, mission_id=mission.id)
    assert (
        orchestrator._planning_request_retry_id(
            intent=third, mission=mission, new_mode=_Mode()
        )
        is None
    )


def test_a_non_format_rejection_does_not_reuse_the_request_id(tmp_path) -> None:
    """Only a codec refusal authorises the re-ask; another refusal opens a new request."""

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    orchestrator = _orchestrator(store)

    # The first round's reply *decoded* and was refused by admission, so its attempt 0
    # is REJECTED rather than UNREADABLE.  A reply the protocol could read is a different
    # answer from one it could not, and reusing its identity would present a new question
    # as the old one.
    first = _intent("planner-1", ordinal=1, mission_id=mission.id)
    object.__setattr__(first, "mission_id", mission.id)
    store.insert_intent(first)
    _bind(orchestrator, mission, first)
    PlanningDecisionStore(store).record_planning_decision(
        request_id=first.intent_id,
        attempt_ordinal=0,
        raw_output_hash="c" * 64,
        decision_id="pd-" + "2" * 24,
        status=PlanningDecisionStatus.REJECTED,
        rejection_codes=("SUBJECT_NOT_IN_REQUEST",),
        detail={"problems": []},
    )

    retry = _intent("plan-request-retry", ordinal=2, mission_id=mission.id)
    object.__setattr__(retry, "mission_id", mission.id)
    store.insert_intent(retry)

    # Ordinal 2 is inside the bound, so the only thing that can refuse the carry is the
    # prior verdict not being a codec refusal.
    assert (
        orchestrator._planning_request_retry_id(
            intent=retry, mission=mission, new_mode=_Mode()
        )
        is None
    )


def test_a_legacy_round_never_gains_a_format_retry(tmp_path) -> None:
    """A legacy Mission has no §34 request row, so the ladder is untouched for it."""

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol="legacy-plan-proposal-v1")
    orchestrator = _orchestrator(store)
    intent = _intent("legacy-planner-2", ordinal=2, mission_id=mission.id)

    assert (
        orchestrator._planning_request_retry_id(
            intent=intent, mission=mission, new_mode=_Mode()
        )
        is None
    )
    assert orchestrator._format_retry_exhausted(
        intent=intent, reason="proposal_unreadable", mission=mission
    ) is False


def test_the_unreadable_refusal_reports_the_retry_it_still_has(tmp_path) -> None:
    """The durable refusal says whether this round was the retry or the end of it."""

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    _orchestrator_obj, first = _read_format_failure(store, mission, _UNREADABLE)

    rejected = _rows(store, mission.id, "PlanningRejected")
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "proposal_unreadable"
    assert rejected[0].payload["detail"]["format_retry_remaining"] == 1

    evaluated = _rows(store, mission.id, "PlanningDecisionEvaluated")
    assert len(evaluated) == 1
    assert evaluated[0].payload["request_id"] == first.intent_id
    assert evaluated[0].payload["attempt_ordinal"] == 0


def test_current_executable_filter_accepts_h4_and_rejects_unknown_types() -> None:
    """H4 executes graph repair; old-package refusal is checked in the collector."""
    orchestrator = object.__new__(Orchestrator)
    enabled = orchestrator._executable_decision_types(
        ["REFINE", "BIND_EXISTING_GOAL", "REPAIR/PROPOSE_SUCCESSOR", "WAIT", "UNKNOWN"]
    )
    assert enabled == frozenset({"REFINE", "BIND_EXISTING_GOAL", "REPAIR/PROPOSE_SUCCESSOR", "WAIT"})

    # A package that names no executable row fails closed instead of silently widening
    # the phase to decisions that were absent from the frozen package.
    assert orchestrator._executable_decision_types(["UNKNOWN"]) == frozenset()
    assert orchestrator._executable_decision_types([]) == frozenset()
    assert orchestrator._executable_decision_types([]) == frozenset()


def test_a_first_round_never_inherits_an_identity(tmp_path) -> None:
    """Ordinal 1 opens its request; only a re-ask may answer an existing one."""

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    orchestrator = _orchestrator(store)
    first = _open_round(
        store, mission, orchestrator, ordinal=1, intent_id="planner-1", text=_UNREADABLE
    )

    assert (
        orchestrator._planning_request_retry_id(
            intent=first, mission=mission, new_mode=_Mode()
        )
        is None
    )


def test_a_retry_may_not_change_anything_but_the_answering_intent(tmp_path) -> None:
    """A second request row for the same id with a new package is refused (§34)."""

    from agent_orchestrator.contracts.models import ContractError

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    orchestrator, first = _read_format_failure(store, mission, _UNREADABLE)

    retry = _intent("plan-request-retry", ordinal=2, mission_id=mission.id)
    object.__setattr__(retry, "mission_id", mission.id)
    store.insert_intent(retry)

    # The same request id, but the reply was produced against a different plan revision:
    # that is a new question wearing the old request's identity, and it must not bind.
    drifted = _package()
    drifted.package["plan"] = {"plan_revision": 9}
    raised = False
    try:
        orchestrator._bind_hierarchical_planning_request(
            intent=retry,
            mission=mission,
            new_mode=_Mode(),
            package=drifted,
            template=SimpleNamespace(
                prompt_version="planner-hierarchical-v8", instructions="prompt"
            ),
            request_id=first.intent_id,
        )
    except ContractError:
        raised = True
    assert raised, "a retry that changed the package must not reuse the request identity"


def test_a_decision_round_without_a_bound_request_fails_closed(tmp_path) -> None:
    """The hot path refuses to evaluate a reply it cannot admit against (§34)."""

    from agent_orchestrator.contracts.models import ContractError

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol=MISSION_PLANNING_DECISION_V1)
    orchestrator = _orchestrator(store)
    unbindable = _intent("planner-unbound", ordinal=1, mission_id=mission.id)
    object.__setattr__(unbindable, "mission_id", mission.id)
    store.insert_intent(unbindable)

    raised = False
    try:
        _evaluate(orchestrator, mission, unbindable, _UNREADABLE)
    except ContractError:
        raised = True
    assert raised, "an unbound new-protocol round must not be evaluated"


def test_the_admission_context_reports_this_rounds_remaining_format_retries(
    tmp_path,
) -> None:
    """§39: the counter the planner reads back is this round's own remaining re-ask.

    Driven against a real hierarchical world, because the value has to come out of the
    same context assembly the live path uses — a constant would satisfy a unit test of
    the helper and still tell the model the wrong thing.
    """

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    import test_root_review_repair_library as repair  # noqa: E402

    world = repair._seeded(tmp_path, key="h1h-retry-budget", alt=False)
    orchestrator = _orchestrator(world.store)
    object.__setattr__(orchestrator, "_commit", world.service)
    # The prompt is not what this test measures; the package, the live world and the
    # registry are, and those are all real below.
    orchestrator._hierarchical_planner_template = lambda mission_id: SimpleNamespace(
        prompt_version="planner-hierarchical-v8", instructions="prompt"
    )

    # The fixture builds a mission without the new protocol, so the durable binding is
    # written through the same production helper the HTTP door uses.  It must happen
    # *before* the package is built: the builder reads the binding to decide whether the
    # package is the legacy shape or H1-D's package-5-with-§38-fields shape.
    from agent_orchestrator.orchestrator.planning_protocol_binding import (
        bind_planning_protocol,
    )

    bind_planning_protocol(world.store, world.mission.id, PLANNING_DECISION_V1)
    orchestrator._config = type(
        "Cfg",
        (),
        {
            "max_planning_attempts": 3,
            "max_root_review_repairs": 1,
            "method_selection_policy": "MODEL_ON_MULTIPLE",
            "hierarchical_repair_enabled": True,
        },
    )()

    # The opener: attempt ordinal 0, so the whole retry is still ahead of it.
    opener = _intent("planner-1", ordinal=1, mission_id=world.mission.id)
    object.__setattr__(opener, "mission_id", world.mission.id)
    world.store.insert_intent(opener)
    package = orchestrator._hierarchical_planner_package(
        world.dispatch, world.mission, ordinal=1
    )
    # H1 §38 adds five fields; H2's formal Views require their own package/prompt
    # pairing and must not be injected into this H1 request.
    assert "planner_package_v1" not in package.package
    assert package.package["planning_protocol"]["protocol"] == PLANNING_DECISION_V1
    object.__setattr__(
        opener, "config", {**opener.config, "planning_package": dict(package.package)}
    )
    orchestrator._bind_hierarchical_planning_request(
        intent=opener,
        mission=world.mission,
        new_mode=world.dispatch,
        package=package,
        template=SimpleNamespace(
            prompt_version="planner-hierarchical-v9", instructions="prompt"
        ),
    )
    # H1-H Blocker A: the new protocol request must carry a real planning grant;
    # the context builder intentionally fails closed when the side binding is absent.
    from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
    from agent_orchestrator.governance.permissions import Principal

    PlanningAuthorizationApi(
        world.store, tenant_id=world.mission.tenant_id, principal=Principal("test-owner")
    ).issue(world.mission.id, command_id="grant-h1h-retry", request_id=opener.intent_id)

    # Authority and operation producers must observe one deferred Store view;
    # nested reader views join that same connection rather than opening a second
    # snapshot.
    from contextlib import contextmanager

    original_read_view = world.store.read_view
    read_connections: list[object] = []

    @contextmanager
    def counted_read_view():
        with original_read_view() as connection:
            read_connections.append(connection)
            yield connection

    world.store.read_view = counted_read_view
    context = orchestrator._hierarchical_admission_context(
        intent=opener, mission=world.mission, new_mode=world.dispatch, raw_text="{}"
    )
    assert context.retry_budgets.same_request_format_retries_remaining == 1
    assert len(read_connections) >= 2
    assert len({id(connection) for connection in read_connections}) == 1

    # The current Views label maps to the current integer package version (one pairing).
    from agent_orchestrator.runtime.role_templates import (
        PLANNING_DECISION_PACKAGE_LABEL, PLANNING_DECISION_PACKAGE_VERSION,
    )

    assert package.package["package_version"] == PLANNING_DECISION_PACKAGE_LABEL
    assert context.package_version == PLANNING_DECISION_PACKAGE_VERSION == 8
    binding_row = PlanningDecisionStore(world.store).get_planning_request(opener.intent_id)
    assert binding_row is not None
    assert binding_row.package_version == PLANNING_DECISION_PACKAGE_VERSION

    # The retry: attempt ordinal 1, which is the last one §5.2 allows.  Its own request
    # row is written too, because the intent_id reassignment onto the opener's row is the
    # blocked part; what this test measures is the counter, not the identity.
    retry = _intent("planner-1-retry", ordinal=2, mission_id=world.mission.id)
    object.__setattr__(retry, "mission_id", world.mission.id)
    world.store.insert_intent(retry)
    object.__setattr__(
        retry, "config", {**retry.config, "planning_package": dict(package.package)}
    )
    orchestrator._bind_hierarchical_planning_request(
        intent=retry,
        mission=world.mission,
        new_mode=world.dispatch,
        package=package,
        template=SimpleNamespace(
            prompt_version="planner-hierarchical-v9", instructions="prompt"
        ),
        request_id=opener.intent_id,
    )
    retry_context = orchestrator._hierarchical_admission_context(
        intent=retry, mission=world.mission, new_mode=world.dispatch, raw_text="{}"
    )
    assert retry_context.retry_budgets.same_request_format_retries_remaining == 0


def test_the_http_field_reaches_the_mission_spec_and_its_binding(tmp_path) -> None:
    """§8.1: the charter's protocol field is real behaviour, not a source string.

    Driven through the same ``spec_from_request`` the HTTP door uses, then committed, so
    the assertion is about the stored row rather than about the text of a module.
    """

    from agent_orchestrator.api.missions import MissionRequestError, spec_from_request
    from agent_orchestrator.orchestrator.planning_protocol_binding import (
        planning_protocol_for_mission,
    )

    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)

    enabled = spec_from_request(
        "tenant",
        {
            "goal": "goal",
            "success_criteria": ["done"],
            "idempotency_key": "http-new",
            "planning_protocol_version": PLANNING_DECISION_V1,
        },
    )
    assert enabled.planning_protocol_version == PLANNING_DECISION_V1
    mission, created = service.create_mission(enabled)
    assert created
    assert planning_protocol_for_mission(store, mission.id)["protocol_version"] == (
        PLANNING_DECISION_V1
    )

    # An omitted key is the legacy default and writes no binding at all.
    legacy = spec_from_request(
        "tenant",
        {"goal": "goal", "success_criteria": ["done"], "idempotency_key": "http-legacy"},
    )
    legacy_mission, _ = service.create_mission(legacy)
    assert planning_protocol_for_mission(store, legacy_mission.id) is None

    # A present-but-unknown value is refused at the door, as a real error.
    with pytest.raises(MissionRequestError):
        spec_from_request(
            "tenant",
            {
                "goal": "goal",
                "idempotency_key": "http-bad",
                "planning_protocol_version": "planning-decision-v99",
            },
        )
    with pytest.raises(MissionRequestError):
        spec_from_request(
            "tenant",
            {
                "goal": "goal",
                "idempotency_key": "http-bad-type",
                "planning_protocol_version": 4,
            },
        )


def test_a_legacy_round_never_writes_a_planning_decision_event(tmp_path) -> None:
    """§37: a legacy Mission must gain no ``PlanningDecision*`` event at all.

    The legacy reply is evaluated through the *same* collector the new protocol uses,
    so the isolation is the branch inside it and not a missing call.
    """

    store = Store.open(tmp_path / "orchestrator.db")
    mission = _mission(store, protocol="legacy-plan-proposal-v1")
    orchestrator = _orchestrator(store)
    intent = _intent("legacy-planner-1", ordinal=1, mission_id=mission.id)
    object.__setattr__(intent, "mission_id", mission.id)
    store.insert_intent(intent)

    # A legacy Mission has no request row, so the new-protocol collector refuses to
    # evaluate at all — which is exactly the isolation being pinned.
    from agent_orchestrator.contracts.models import ContractError

    with pytest.raises(ContractError):
        _evaluate(orchestrator, mission, intent, _UNREADABLE)

    assert [
        event
        for event in store.list_events(mission.id)
        if event.type.startswith("PlanningDecision")
    ] == []


@pytest.mark.parametrize(
    "raw",
    ["planner-package-unknown", True, None],
    ids=["unknown-string", "bool", "none"],
)
def test_an_unpairable_package_version_is_refused(raw) -> None:
    """§34/§9: only the exact v5 label and a real integer may become a binding.

    The label is translated, so an unknown string must not reach the column as text; and
    ``True``/``False`` must not.  ``bool`` is an ``int`` subclass and type-checks clean,
    so it must be refused explicitly rather than by ``isinstance(raw, int)`` alone.  The
    helper is called directly: this is the unit that decides the column's value.
    """

    with pytest.raises(ContractError):
        Orchestrator._planning_decision_package_version({"package_version": raw})
