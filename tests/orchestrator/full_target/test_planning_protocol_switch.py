# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-S red tests: the Mission planning-protocol switch and durable binding."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from agent_orchestrator.contracts import Budget, ContractError
from agent_orchestrator.contracts.planning_decisions import (
    LEGACY_PLANNING_PROTOCOL as CONTRACT_LEGACY_PLANNING_PROTOCOL,
)
from agent_orchestrator.contracts.planning_decisions import (
    PLANNING_DECISION_V1 as CONTRACT_PLANNING_DECISION_V1,
)
from agent_orchestrator.orchestrator.commit_service import (
    LEGACY_PLANNING_PROTOCOL,
    PLANNING_DECISION_V1,
    CommitRejected,
    CommitService,
    MissionConflict,
    MissionSpec,
    sha256_hex,
)
from agent_orchestrator.orchestrator.planning_protocol_binding import (
    planning_protocol_for_mission,
    planning_protocol_replay_conflict,
)
from agent_orchestrator.storage.store import Store


def _spec_with_field_set_behind_the_constructor(key: str, protocol_version: str) -> MissionSpec:
    """A spec whose ``planning_protocol_version`` slot was assigned directly (§8.1).

    ``object.__setattr__`` writes the frozen field without ``__post_init__`` — the same
    escape hatch a deserialiser, a hand-built record or ``dataclasses.replace`` on a
    non-slots class has.  ``to_json()`` still derives the key from that slot, so the
    document (and therefore ``spec_hash``) is the one the *forged* name produces; the door
    must refuse the value regardless, before the document is digested or stored.
    """

    spec = _spec(key)
    object.__setattr__(spec, "planning_protocol_version", protocol_version)
    return spec


def _spec(key: str, **kwargs: object) -> MissionSpec:
    return MissionSpec(
        goal="g",
        success_criteria=("ok",),
        tenant_id="tenant",
        idempotency_key=key,
        budget=Budget(max_tokens=1000, max_attempts=1),
        **kwargs,
    )


def test_default_spec_json_bytes_and_hash_are_unchanged() -> None:
    spec = _spec("bytes")
    expected = {
        "goal": "g",
        "success_criteria": ["ok"],
        "tenant_id": "tenant",
        "idempotency_key": "bytes",
        "stop_conditions": ["verification_passed", "budget_exhausted"],
        "allowed_tools": [],
        "risk_level": "sandbox",
        "budget": {
            "max_tokens": 1000,
            "max_cost_micros": None,
            "max_attempts": 1,
            "max_runtime_seconds": None,
            "max_concurrency": None,
            "max_tool_calls": None,
        },
        "task_kind": "code",
        "workspace_seed": {},
    }
    assert spec.to_json() == expected
    encoded = json.dumps(expected, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert hashlib.sha256(encoded.encode()).hexdigest() == (
        "d0903d35e0062418f244304251be0f3f87a8885e3bef5170c5b6a30f3b0dd161"
    )


def test_new_protocol_json_key_and_unknown_values_are_rejected() -> None:
    spec = _spec("round-trip", planning_protocol_version=PLANNING_DECISION_V1)
    assert spec.to_json()["planning_protocol_version"] == PLANNING_DECISION_V1
    with pytest.raises(ContractError, match="planning protocol"):
        _spec("unknown", planning_protocol_version="planning-decision-v99")
    for invalid in ([], {}):
        with pytest.raises(ContractError, match="planning protocol"):
            _spec("invalid-type", planning_protocol_version=invalid)


def test_new_protocol_creation_writes_one_binding_with_frozen_hash(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    mission, created = service.create_mission(
        _spec("new", planning_protocol_version=PLANNING_DECISION_V1)
    )
    assert created
    binding = planning_protocol_for_mission(store, mission.id)
    assert binding is not None
    assert binding["protocol_version"] == PLANNING_DECISION_V1
    assert binding["package_version"] == 4
    assert binding["prompt_version"] == "planner-hierarchical-v8"
    expected = {
        "protocol_version": PLANNING_DECISION_V1,
        "package_version": 4,
        "prompt_version": "planner-hierarchical-v8",
    }
    assert (
        binding["binding_hash"]
        == hashlib.sha256(
            json.dumps(expected, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    )
    assert (
        store.connection.execute(
            "SELECT count(*) FROM mission_planning_protocols WHERE mission_id = ?", (mission.id,)
        ).fetchone()[0]
        == 1
    )


def test_legacy_creation_has_no_binding(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    mission, _ = CommitService(store).create_mission(_spec("legacy"))
    assert planning_protocol_for_mission(store, mission.id) is None
    assert LEGACY_PLANNING_PROTOCOL == "legacy-plan-proposal-v1"
    assert LEGACY_PLANNING_PROTOCOL == CONTRACT_LEGACY_PLANNING_PROTOCOL
    assert PLANNING_DECISION_V1 == CONTRACT_PLANNING_DECISION_V1


def test_binding_is_transactional_on_creation_failure(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    observed: dict[str, object] = {}

    def fail_after_binding(mission: object) -> None:
        observed["binding"] = planning_protocol_for_mission(store, mission.id)  # type: ignore[attr-defined]
        raise CommitRejected("boom")

    service._reserve_mission_system_pools = fail_after_binding  # type: ignore[method-assign]
    with pytest.raises(CommitRejected, match="boom"):
        service.create_mission(
            _spec(
                "rollback",
                planning_protocol_version=PLANNING_DECISION_V1,
            )
        )
    assert observed["binding"] is not None
    assert (
        store.connection.execute("SELECT count(*) FROM mission_planning_protocols").fetchone()[0]
        == 0
    )
    assert store.find_mission("tenant", "rollback") is None


def test_replay_is_idempotent_and_protocol_cannot_change(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    spec = _spec("idem", planning_protocol_version=PLANNING_DECISION_V1)
    mission, created = service.create_mission(spec)
    again, replayed = service.create_mission(spec)
    assert again == mission and not replayed
    with pytest.raises(MissionConflict, match="different specification"):
        service.create_mission(replace(spec, planning_protocol_version=LEGACY_PLANNING_PROTOCOL))
    # The legacy name produces a *different* document, so the spec hash already refuses it;
    # the binding is asked independently (below) because that is the check that survives a
    # Host which only compares charter bytes.
    with pytest.raises(MissionConflict):
        service.create_mission(
            _spec_with_field_set_behind_the_constructor("idem", LEGACY_PLANNING_PROTOCOL)
        )
    assert planning_protocol_replay_conflict(store, mission.id, LEGACY_PLANNING_PROTOCOL) == (
        f"mission {mission.id} is durably bound to protocol {PLANNING_DECISION_V1!r}/package 4,"
        f" not {LEGACY_PLANNING_PROTOCOL!r}/package 4"
    )
    assert planning_protocol_replay_conflict(store, mission.id, PLANNING_DECISION_V1) is None
    assert store.find_mission("tenant", "idem")[1] == sha256_hex(spec.to_json())
    assert (
        store.connection.execute(
            "SELECT protocol_version FROM mission_planning_protocols WHERE mission_id = ?",
            (mission.id,),
        ).fetchone()[0]
        == PLANNING_DECISION_V1
    )


def test_legacy_mission_cannot_be_replayed_as_new_protocol(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    spec = _spec("legacy-first")
    mission, _ = service.create_mission(spec)
    with pytest.raises(MissionConflict, match="different specification"):
        service.create_mission(replace(spec, planning_protocol_version=PLANNING_DECISION_V1))
    # The new protocol under an existing key is refused whichever door it comes through:
    # as a spec (the document changed, so the spec hash disagrees) and as the same wire
    # name re-sent, which must stay the same Mission.
    with pytest.raises(MissionConflict, match="different specification"):
        service.create_mission(
            _spec_with_field_set_behind_the_constructor("legacy-first", PLANNING_DECISION_V1)
        )
    stored_hash = store.find_mission("tenant", "legacy-first")[1]
    assert stored_hash == sha256_hex(spec.to_json())
    # The legacy name written out explicitly is the *same* document (``to_json`` omits the
    # default), so this is the case where the spec hash agrees and only the stored binding
    # decides: it must agree too, or every Host that names the default would start getting
    # conflicts after an upgrade.
    explicit = replace(spec, planning_protocol_version=LEGACY_PLANNING_PROTOCOL)
    assert explicit.to_json() == spec.to_json()
    assert sha256_hex(explicit.to_json()) == stored_hash
    now, replayed = service.create_mission(explicit)
    assert now == mission and replayed is False
    assert planning_protocol_for_mission(store, mission.id) is None
    assert (
        store.connection.execute("SELECT count(*) FROM mission_planning_protocols").fetchone()[0]
        == 0
    )


def test_policy_snapshot_digest_does_not_include_planning_protocol(tmp_path) -> None:
    from agent_orchestrator.governance.policies import policy_snapshot
    from agent_orchestrator.runtime.assembly import OrchestratorConfig

    legacy = _spec("policy-legacy")
    enabled = replace(
        legacy,
        idempotency_key="policy-enabled",
        planning_protocol_version=PLANNING_DECISION_V1,
    )
    assert enabled.to_json() != legacy.to_json()
    first = policy_snapshot(OrchestratorConfig(evidence_root=tmp_path / "legacy"))
    second = policy_snapshot(OrchestratorConfig(evidence_root=tmp_path / "enabled"))
    assert first["hash"] == second["hash"]
    assert "planning_protocol_version" not in first["config"]
    assert "planning_protocol_version" not in json.dumps(first)

    store = Store.open(tmp_path / "policy.db")
    service = CommitService(store)
    legacy_mission, _ = service.create_mission(legacy)
    enabled_mission, _ = service.create_mission(enabled)
    legacy_binding = store.get_mission_policy(legacy_mission.id)
    enabled_binding = store.get_mission_policy(enabled_mission.id)
    assert legacy_binding is not None and enabled_binding is not None
    legacy_policy = store.get_policy_version(legacy_binding["version_id"])
    enabled_policy = store.get_policy_version(enabled_binding["version_id"])
    assert legacy_policy is not None and enabled_policy is not None
    assert legacy_policy["params_hash"] == enabled_policy["params_hash"]
    assert legacy_policy["params"] == enabled_policy["params"]
    assert "planning_protocol_version" not in legacy_policy["params"]
    assert "planning_protocol_version" not in enabled_policy["params"]


def test_binding_survives_a_new_connection(tmp_path) -> None:
    path = tmp_path / "orchestrator.db"
    first = Store.open(path)
    mission, _ = CommitService(first).create_mission(
        _spec("restart", planning_protocol_version=PLANNING_DECISION_V1)
    )
    first.close()
    second = Store.open(path)
    assert (
        planning_protocol_for_mission(second, mission.id)["protocol_version"]
        == PLANNING_DECISION_V1
    )


# ---------------------------------------------------------------------------------------
# Orchestrator hand-off: the cases the first pass left undecided.
# ---------------------------------------------------------------------------------------


def test_commit_service_refuses_a_spec_that_bypassed_the_constructor(tmp_path) -> None:
    """A spec whose field was set behind the constructor's back is still refused (§8.1).

    ``dataclasses.replace`` does re-run ``__post_init__`` (it calls the constructor), so
    that path already raises; ``object.__setattr__`` does not, and that is the door the
    service itself must close.  Without the guard the unknown name would be digested into
    ``spec_hash`` and written to the library.
    """

    store = Store.open(tmp_path / "orchestrator.db")
    forged = _spec_with_field_set_behind_the_constructor("forged", "planning-decision-v99")
    assert forged.planning_protocol_version == "planning-decision-v99"
    assert forged.to_json()["planning_protocol_version"] == "planning-decision-v99"
    with pytest.raises(CommitRejected, match="planning protocol"):
        CommitService(store).create_mission(forged)
    assert store.find_mission("tenant", "forged") is None


def test_the_two_protocol_constants_are_the_frozen_online_names() -> None:
    """§8.1 renames nothing: the wire names are identical on both sides of the imports."""

    from agent_orchestrator.orchestrator.planning_protocol_binding import PLANNING_PROTOCOLS

    assert PLANNING_DECISION_V1 == "planning-decision-v1"
    assert LEGACY_PLANNING_PROTOCOL == "legacy-plan-proposal-v1"
    assert PLANNING_PROTOCOLS == frozenset({"planning-decision-v1", "legacy-plan-proposal-v1"})
    assert PLANNING_DECISION_V1 == CONTRACT_PLANNING_DECISION_V1
    assert LEGACY_PLANNING_PROTOCOL == CONTRACT_LEGACY_PLANNING_PROTOCOL


def test_legacy_mission_created_with_the_default_keeps_its_spec_hash(tmp_path) -> None:
    """The golden bytes are not just a ``to_json`` claim: the stored Mission hash matches.

    ``_spec("bytes")`` with the default protocol is digested to the frozen golden hash and
    then created: the row the library keeps must be that document, not a document that grew
    a ``planning_protocol_version`` key on the way in.
    """

    spec = _spec("bytes")
    document = {
        "goal": "g",
        "success_criteria": ["ok"],
        "tenant_id": "tenant",
        "idempotency_key": "bytes",
        "stop_conditions": ["verification_passed", "budget_exhausted"],
        "allowed_tools": [],
        "risk_level": "sandbox",
        "budget": {
            "max_tokens": 1000,
            "max_cost_micros": None,
            "max_attempts": 1,
            "max_runtime_seconds": None,
            "max_concurrency": None,
            "max_tool_calls": None,
        },
        "task_kind": "code",
        "workspace_seed": {},
    }
    assert spec.to_json() == document
    expected = hashlib.sha256(
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert expected == "d0903d35e0062418f244304251be0f3f87a8885e3bef5170c5b6a30f3b0dd161"
    store = Store.open(tmp_path / "orchestrator.db")
    mission, created = CommitService(store).create_mission(spec)
    assert created
    row = store.connection.execute(
        "SELECT spec_hash FROM missions WHERE mission_id = ?", (mission.id,)
    ).fetchone()
    assert row["spec_hash"] == expected


def test_binding_comes_from_the_stored_table_not_from_the_config_attribute(tmp_path) -> None:
    """Recovery reads the library, never the running configuration (§8.2).

    Two Missions share one store here.  Both are the same frozen policy generation (the
    same ``params_hash``, the same document that carries the Planner prompt version), yet
    the legacy Mission has no row and the second Mission does: a read that consulted
    anything but ``mission_planning_protocols`` — an environment variable, a config
    attribute, another Mission's binding, the policy a recovery already has in hand — would
    answer the same for both.
    """

    path = tmp_path / "orchestrator.db"
    store = Store.open(path)
    service = CommitService(store)
    legacy, _ = service.create_mission(_spec("recovery-legacy"))
    enabled, _ = service.create_mission(
        _spec("recovery-enabled", planning_protocol_version=PLANNING_DECISION_V1)
    )
    legacy_policy = store.get_policy_version(str(store.get_mission_policy(legacy.id)["version_id"]))
    enabled_policy = store.get_policy_version(
        str(store.get_mission_policy(enabled.id)["version_id"])
    )
    assert legacy_policy is not None and enabled_policy is not None
    assert legacy_policy["params_hash"] == enabled_policy["params_hash"]
    assert (
        legacy_policy["params"]["prompt_versions"]["planner"]
        == (enabled_policy["params"]["prompt_versions"]["planner"])
    )
    assert "planning-decision-v1" not in json.dumps(legacy_policy)
    assert "planning-decision-v1" not in json.dumps(enabled_policy)
    assert planning_protocol_for_mission(store, legacy.id) is None
    store.close()

    reopened = Store.open(path)
    assert planning_protocol_for_mission(reopened, legacy.id) is None
    binding = planning_protocol_for_mission(reopened, enabled.id)
    assert binding is not None and binding["protocol_version"] == PLANNING_DECISION_V1
    assert (
        reopened.connection.execute("SELECT count(*) FROM mission_planning_protocols").fetchone()[0]
        == 1
    )
    reopened.close()


def test_a_replayed_new_protocol_mission_keeps_exactly_one_binding_row(tmp_path) -> None:
    """Idempotency must not accumulate rows nor rewrite ``created_at`` (§8.2)."""

    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    spec = _spec("replay-row", planning_protocol_version=PLANNING_DECISION_V1)
    mission, _ = service.create_mission(spec)
    first = planning_protocol_for_mission(store, mission.id)
    service.create_mission(spec)
    service.create_mission(replace(spec, goal="g", success_criteria=("ok",)))
    rows = store.connection.execute(
        "SELECT * FROM mission_planning_protocols WHERE mission_id = ?", (mission.id,)
    ).fetchall()
    assert len(rows) == 1
    assert planning_protocol_for_mission(store, mission.id) == first


def test_the_durable_binding_ignores_the_ambient_environment(tmp_path, monkeypatch) -> None:
    """§8.2: the mode is never guessed from the environment, on write or on read."""

    monkeypatch.setenv("SIMPLE_HARNESS_PLANNING_PROTOCOL", PLANNING_DECISION_V1)
    monkeypatch.setenv("PLANNING_PROTOCOL_VERSION", LEGACY_PLANNING_PROTOCOL)
    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    legacy, _ = service.create_mission(_spec("env-legacy"))
    enabled, _ = service.create_mission(
        _spec("env-enabled", planning_protocol_version=PLANNING_DECISION_V1)
    )
    assert planning_protocol_for_mission(store, legacy.id) is None
    assert planning_protocol_for_mission(store, enabled.id)["protocol_version"] == (
        PLANNING_DECISION_V1
    )


# ---------------------------------------------------------------------------------------
# Reachability inside the slice's own allowlist (§8.1 "the charter names the wire").
# ---------------------------------------------------------------------------------------


def test_the_protocol_switch_is_reachable_without_editing_the_request_parser() -> None:
    """The switch must be reachable from the files this slice is allowed to touch.

    ``api/missions.py`` is not on the allowlist, so the request parser must stay byte
    for byte as it was.  That is only acceptable if the parser is not the *only* way to
    name the wire: the field is part of ``MissionSpec``, so any caller that builds a
    spec — the ``__main__`` CLI, the evaluation runners, the Host's own record reader —
    can name it, and the parser simply leaves the dataclass default in place.

    This pins the negative half too: the parser must keep *dropping* the key, because
    reaching in there is what the allowlist forbids.
    """

    from agent_orchestrator.api.missions import spec_from_request

    named = spec_from_request(
        "tenant", {"idempotency_key": "x", "planning_protocol_version": PLANNING_DECISION_V1}
    )
    plain = spec_from_request("tenant", {"idempotency_key": "x"})
    # The key is dropped, not defaulted: the two documents are byte for byte the same,
    # so a request cannot smuggle the wire in through HTTP and the legacy bytes hold.
    assert named.to_json() == plain.to_json()
    assert named.planning_protocol_version == LEGACY_PLANNING_PROTOCOL
    assert "planning_protocol_version" not in named.to_json()
    named = MissionSpec(
        goal="g",
        success_criteria=("ok",),
        tenant_id="tenant",
        idempotency_key="named",
        budget=Budget(max_tokens=1000, max_attempts=1),
        planning_protocol_version=PLANNING_DECISION_V1,
    )
    assert named.planning_protocol_version == PLANNING_DECISION_V1
    assert named.to_json()["planning_protocol_version"] == PLANNING_DECISION_V1


def test_the_slice_touches_no_file_outside_its_allowlist() -> None:
    """Guard against the repair silently re-adding the out-of-allowlist mapping.

    The gate fails the whole slice on a single out-of-allowlist file, so the mapping in
    ``api/missions.py`` cannot come back.  Reading the file we are not allowed to change
    is enough to prove it: the parser must not mention the field at all.
    """

    import inspect

    from agent_orchestrator.api.missions import spec_from_request

    source = inspect.getsource(spec_from_request)
    assert "planning_protocol_version" not in source


# ---------------------------------------------------------------------------------------
# The two assertions the independent review found missing (P1-1, P1-2).
# ---------------------------------------------------------------------------------------


def test_the_binding_hash_names_the_protocol_and_is_sensitive_to_it() -> None:
    """The digest must be the *three*-field document, for either wire name (§8.2).

    The creation test pins the digest of the new protocol only, and the constant it
    compares against carries ``planning-decision-v1`` twice — once as the well-known
    ``PLANNING_PROTOCOL_BINDING`` entry and once as the requested name — so a digest
    computed from the constant alone lands on the same value.  Compute the expected
    document here from the *argument* instead, and pin that the two names differ: a
    hash that ignores the protocol name would collide the two binding identities, which
    contradicts the per-mission ``protocol_version`` column it is stored next to.
    """

    from agent_orchestrator.orchestrator.planning_protocol_binding import (
        planning_protocol_binding_hash,
    )

    for protocol in (LEGACY_PLANNING_PROTOCOL, PLANNING_DECISION_V1):
        document = {
            "protocol_version": protocol,
            "package_version": 4,
            "prompt_version": "planner-hierarchical-v8",
        }
        expected = hashlib.sha256(
            json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        assert planning_protocol_binding_hash(protocol) == expected
    assert planning_protocol_binding_hash(LEGACY_PLANNING_PROTOCOL) != (
        planning_protocol_binding_hash(PLANNING_DECISION_V1)
    )


def test_the_snapshot_mechanism_would_expose_a_config_field_marked_include(
    tmp_path, monkeypatch
) -> None:
    """Make the ``policy_snapshot`` guard discriminating instead of vacuous (P1-2).

    ``test_policy_snapshot_digest_does_not_include_planning_protocol`` asserts that a key
    the config does not even have is absent, which proves nothing: ``SNAPSHOT_FIELDS`` is
    consulted by *config field name*, so the guard only bites once a field the config
    really carries is marked ``include``.  Mark a real field, show the digest moves, then
    show that the switch is not that field — the guard is about the mechanism.
    """

    from agent_orchestrator.governance import policies
    from agent_orchestrator.runtime.assembly import OrchestratorConfig

    witness = "owner_id"
    assert policies.SNAPSHOT_FIELDS.get(witness) != "include"
    baseline = policies.policy_snapshot(OrchestratorConfig(evidence_root=tmp_path / "base"))
    assert witness not in baseline["config"]

    with monkeypatch.context() as patch:
        patch.setitem(policies.SNAPSHOT_FIELDS, witness, "include")
        promoted = policies.policy_snapshot(OrchestratorConfig(evidence_root=tmp_path / "base"))
    assert witness in promoted["config"]
    assert promoted["hash"] != baseline["hash"]

    # …and the switch itself is not a config field, so no ``include`` row can smuggle it
    # into the digest: this is the fact the weaker assertion was trying (and failing) to
    # state.
    assert "planning_protocol_version" not in policies.SNAPSHOT_FIELDS
    assert "planning_protocol_version" not in {
        field.name for field in __import__("dataclasses").fields(OrchestratorConfig)
    }
