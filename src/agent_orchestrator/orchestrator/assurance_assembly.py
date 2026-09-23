# SPDX-License-Identifier: Apache-2.0
"""Production assembly of the Assurance 1.1 lane on one Orchestrator.

``install_assurance`` is the single deployment entry (spec §11 / appendix C.2):
it binds the deployment's fixed authenticated principal and tenant as the
CURRENT read authority, the original approved Requirements builder, the local
check adapter, the REVIEW runtime, the validity evaluator, the three other
consumers, the Mission factory and the durable tick, then reconciles startup
state. It is called from the deployment's ``startup_assembly`` callback, after
the root gate exists and before recovery resumes any runtime.

Nothing here approves a policy, selects a Mission profile on its own or runs a
model. The default profile selector is still
``default_assurance_profile_for_new_mission`` (None until the final delivery).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from ..assurance.certificates import PURPOSES, UseIdentity
from ..assurance.codec import AssuranceError, canonical, fingerprint, integer, text
from ..assurance.evidence import ReadItem
from ..assurance.expiry import AssuranceExpiry
from ..assurance.policy import AssurancePolicy
from ..assurance.refs import AssuranceRef
from ..assurance.root_gate import CurrentReadPermission
from ..contracts import Mission
from ..contracts.resolution import (
    AllExpr,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    EvaluationKind,
    RequirementClass,
    RequirementsRevision,
)
from ..governance.permissions import Principal
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import CONSUMERS, AssuranceWorkStore, WorkTarget
from .assurance_clock import observe_assurance_clock
from .assurance_consumers import (
    NOTIFICATION_EVENT,
    AssuranceCloseoutConsumer,
    AssuranceNotifyConsumer,
    AssuranceValidityConsumer,
)
from .assurance_factory import AssuranceMissionFactory, default_assurance_profile_for_new_mission
from .assurance_final_writer import finalize_assured_mission
from .assurance_local_checks import AssuranceLocalChecks
from .assurance_review_consumer import AssuranceReviewConsumer
from .assurance_review_pins import release_orphan_preparations
from .assurance_review_runtime import AssuranceReviewRuntime
from .assurance_tick import AssuranceTick
from .assurance_validity import AssuranceValidity

DEFAULT_READ_TTL_MS = 24 * 3600 * 1000
ACCESS_AUTHORITY_VERSION = "fixed-principal-tenant-ownership-v1"


# ------------------------------------------------------------ authority
class FixedPrincipalAuthority:
    """CURRENT read/use authority of a single-principal, single-tenant deployment.

    The ACL this deployment actually has is: the authenticated fixed principal
    may use material of Missions owned by its tenant, under the native root and
    the frozen policies. The ACCESS witness binds exactly those facts plus the
    global imported access/policy epoch; the POLICY witness binds the frozen
    Assurance policy and the deployment policy. No external ACL is consulted, so
    none is claimed.
    """

    def __init__(
        self,
        commit: Any,
        *,
        tenant_id: str,
        principal: Principal,
        policy: AssurancePolicy,
        deployment: Any | None,
        ttl_ms: int = DEFAULT_READ_TTL_MS,
    ) -> None:
        if not isinstance(principal, Principal):
            raise AssuranceError("CURRENT_AUTHENTICATED_PRINCIPAL_REQUIRED")
        self.commit = commit
        self.store = commit.store
        self.tenant_id = text(tenant_id)
        self.principal = principal
        self.policy = policy
        self.deployment = deployment
        self.ttl_ms = integer(ttl_ms, minimum=1)
        deployment_json = None
        if deployment is not None:
            deployment_json = deployment.to_json() if hasattr(deployment, "to_json") else None
        self.policy_fingerprint = fingerprint(
            {"assurance": policy.to_json(), "deployment": deployment_json}
        )

    def _grant(self, mission_id: str, ref: AssuranceRef, purpose: str) -> CurrentReadPermission:
        if purpose not in PURPOSES:
            raise AssuranceError("CURRENT_READ_AUTHORITY_REQUIRED")
        if not isinstance(ref, AssuranceRef):
            raise AssuranceError("REF_INVALID")
        gate = self.commit._assurance_root_gate
        if gate is None:
            raise AssuranceError("ROOT_AUTHORITY_UNAVAILABLE")
        with self.store.read_view():
            mission = self.store.get_mission(mission_id)
            if mission is None or mission.tenant_id != self.tenant_id:
                raise AssuranceError("ROOT_READ_NOT_AUTHORIZED")
            row, body = gate._state_locked()
            environment = self.store.connection.execute(
                "SELECT epoch,clock_generation FROM assurance_environment_state WHERE singleton=1"
            ).fetchone()
            if environment is None:
                raise AssuranceError("ASSURANCE_ENVIRONMENT_UNINITIALIZED")
            now_ms = int(self.store.now * 1000)
        access = ReadItem(
            "ACCESS",
            canonical(
                {
                    "principal": self.principal.principal_id,
                    "tenant": self.tenant_id,
                    "scope": mission_id,
                    "use": purpose,
                }
            ),
            fingerprint(
                {
                    "authority": ACCESS_AUTHORITY_VERSION,
                    "principal": self.principal.principal_id,
                    "tenant": self.tenant_id,
                    "mission": mission_id,
                    "owner_tenant": mission.tenant_id,
                    "ref": ref.to_json(),
                    "purpose": purpose,
                    "root_kind": row["kind"],
                    "root_incarnation_id": body["root_incarnation_id"],
                    "environment_epoch": environment["epoch"],
                    "clock_generation": environment["clock_generation"],
                }
            ),
        )
        policy = ReadItem(
            "POLICY",
            canonical({"policy_id": "assurance-exec-v1.1", "version": 1, "use": purpose}),
            self.policy_fingerprint,
        )
        ttl = self.ttl_ms
        approval_ttl = getattr(self.deployment, "approval_ttl_seconds", None)
        if isinstance(approval_ttl, (int, float)) and approval_ttl > 0:
            ttl = min(ttl, int(approval_ttl * 1000))
        return CurrentReadPermission(access, policy, now_ms + ttl)

    def __call__(self, identity: UseIdentity, ref: AssuranceRef) -> CurrentReadPermission:
        """The consumers' ``CurrentAuthority``: identity supplied by trusted assembly."""
        if (
            not isinstance(identity, UseIdentity)
            or identity.principal_id != self.principal.principal_id
        ):
            raise AssuranceError("CURRENT_READ_AUTHORITY_REQUIRED")
        return self._grant(identity.mission_id, ref, identity.purpose)

    def read(
        self, principal: Principal, tenant_id: str, mission_id: str, ref: AssuranceRef, purpose: str
    ) -> CurrentReadPermission:
        """``CommitService._assurance_read_authority``: the facade's actual caller."""
        if (
            not isinstance(principal, Principal)
            or principal.principal_id != self.principal.principal_id
            or tenant_id != self.tenant_id
        ):
            raise AssuranceError("ROOT_READ_NOT_AUTHORIZED")
        return self._grant(mission_id, ref, purpose)


# --------------------------------------------------------- requirements
def mission_spec_requirements(
    principal: Principal,
) -> Callable[[Mission, Any], RequirementsRevision]:
    """The original approved Requirements of a new Mission: its success criteria.

    Same document the Host's root initialisation writes (``req-<mission>-1``,
    ``c-user-<n>``, USER_EXPLICIT / REQUIRED_OUTCOME / SEMANTIC, authority =
    the authenticated principal), so the factory and the Host agree byte for
    byte on revision 1 and neither writes a second body.
    """

    def build(mission: Mission, spec: Any) -> RequirementsRevision:
        statements = tuple(spec.success_criteria)
        if not statements:
            raise AssuranceError("FACTORY_REQUIREMENTS_MISMATCH", "no success criteria")
        criteria = tuple(
            Criterion(
                f"c-user-{index + 1}",
                1,
                CriterionOrigin.USER_EXPLICIT,
                statement,
                RequirementClass.REQUIRED_OUTCOME,
                EvaluationKind.SEMANTIC,
            )
            for index, statement in enumerate(statements)
        )
        expression: Any = CriterionExpr(criteria[0].criterion_id)
        if len(criteria) > 1:
            expression = AllExpr(tuple(CriterionExpr(c.criterion_id) for c in criteria))
        return RequirementsRevision(
            revision_id=f"req-{mission.id}-1",
            mission_id=mission.id,
            revision=1,
            criteria=criteria,
            success_expression=expression,
            authority_subject=principal.principal_id,
        )

    return build


# ------------------------------------------------------ reconciliation
def activation_inventory(store: Any, mission_id: str) -> dict[str, tuple[WorkTarget, ...]]:
    """Honest activation reconciliation for a Mission created in this UoW.

    Only a Mission created by the factory can be activated (spec §11: no silent
    upgrade), so every consumer's inventory is empty by construction. That is
    verified, not assumed: any pre-existing reviewable Result, review
    invocation, certificate, root resolution or notification request means the
    row is not new and activation is refused.
    """
    checks = {
        "results": "SELECT 1 FROM results WHERE mission_id=? LIMIT 1",
        "review_invocations": (
            "SELECT 1 FROM assurance_review_invocations WHERE mission_id=? LIMIT 1"
        ),
        "certificates": "SELECT 1 FROM assurance_use_certificates WHERE mission_id=? LIMIT 1",
        "goal_resolutions": "SELECT 1 FROM goal_resolutions WHERE mission_id=? LIMIT 1",
        "closeouts": "SELECT 1 FROM assurance_closeouts WHERE mission_id=? LIMIT 1",
        "notifications": "SELECT 1 FROM events WHERE mission_id=? AND type=? LIMIT 1",
    }
    for name, sql in checks.items():
        params = (mission_id, NOTIFICATION_EVENT) if name == "notifications" else (mission_id,)
        if store.connection.execute(sql, params).fetchone() is not None:
            raise AssuranceError("ACTIVATION_RECONCILIATION_UNEXPECTED", name)
    return {consumer: () for consumer in CONSUMERS}


def reconcile_startup(orchestrator: Any, *, tenant_id: str, root_incarnation_id: str) -> dict:
    """Rebuild due work for every assured Mission of this tenant at startup.

    Lost cursors are re-initialised at 0 (replay of original Events, de-duplicated
    by stable work keys); expired USABLE certificates get their due events now
    rather than on the first business event (spec §8.4). No pending budget is
    reset and no work is invented.

    Handoff item 8: the clock is observed *before* any claim (a rollback while
    the process was down is recorded as one TimeDiscontinuity per Mission and
    the tick claims nothing until the clock is past its high-water mark again);
    abandoned PREPARING pins are released through their receipted transition;
    RUNNING claims a dead owner left behind are reported only — ``claim_due``
    reclaims coordination when the lease elapses, and the original service
    intent stays with its owner (owner retention).
    """
    store = orchestrator.store
    commit = orchestrator.commit
    work = AssuranceWorkStore(store)
    expiry = AssuranceExpiry(store)
    now_ms = int(store.now * 1000)
    clock = observe_assurance_clock(commit, now_ms=now_ms)
    missions = [
        row[0]
        for row in store.connection.execute(
            "SELECT b.mission_id FROM assurance_mission_bindings b "
            "JOIN missions m ON m.mission_id=b.mission_id WHERE m.tenant_id=? "
            "ORDER BY b.mission_id",
            (tenant_id,),
        ).fetchall()
    ]
    rebuilt, due = [], 0
    pins_released: list[str] = []
    pins_with_binding: list[str] = []
    pins_deferred: list[str] = []
    for mission_id in missions:
        if AssuranceStore(store).lane(mission_id) != "ASSURANCE_1_1":
            raise AssuranceError("ASSURANCE_CREATION_LANE_MISMATCH", mission_id)
        orphans = release_orphan_preparations(commit, mission_id=mission_id, now_ms=now_ms)
        pins_released.extend(orphans["released"])
        pins_with_binding.extend(orphans["preparing_with_binding"])
        pins_deferred.extend(orphans["deferred"])
        present = {
            row[0]
            for row in store.connection.execute(
                "SELECT consumer FROM assurance_event_cursors WHERE mission_id=?", (mission_id,)
            )
        }
        for consumer in sorted(CONSUMERS - present):
            work.initialize_cursor(mission_id, consumer, activation_seq=0, now_ms=now_ms)
            rebuilt.append(f"{mission_id}:{consumer}")
        due += len(
            expiry.emit_due(mission_id, root_incarnation_id=root_incarnation_id, now_ms=now_ms)
        )
    pending = {
        row[0]: row[1]
        for row in store.connection.execute(
            "SELECT w.state,COUNT(*) FROM assurance_pending_work w "
            "JOIN missions m USING(mission_id) "
            "WHERE m.tenant_id=? GROUP BY w.state",
            (tenant_id,),
        ).fetchall()
    }
    running = [
        f"{row[0]}:{row[1]}:{row[2]}"
        for row in store.connection.execute(
            "SELECT w.mission_id,w.consumer,w.work_key FROM assurance_pending_work w "
            "JOIN missions m USING(mission_id) WHERE m.tenant_id=? AND w.state='RUNNING' "
            "ORDER BY w.mission_id,w.consumer,w.work_key LIMIT 257",
            (tenant_id,),
        ).fetchall()
    ]
    return {
        "missions": len(missions),
        "cursors_rebuilt": rebuilt,
        "expiry_events_emitted": due,
        "pending_work": pending,
        "clock_state": clock.state,
        "clock_generation": clock.generation,
        "pins_released": pins_released,
        "pins_preparing_with_binding": pins_with_binding,
        "pins_release_deferred": pins_deferred,
        "running_claims": running,
    }


# ------------------------------------------------------------ assembly
@dataclass(frozen=True)
class AssuranceDeploymentPorts:
    tenant_id: str
    principal: Principal
    # The deployment's original approved Requirements builder; None selects
    # ``mission_spec_requirements(principal)``.
    requirements: Callable[[Mission, Any], RequirementsRevision] | None = None
    # Which new Missions take the assured lane; None consults the single default
    # selection point ``default_assurance_profile_for_new_mission``.
    select_profile: Callable[[Any], AssurancePolicy | None] | None = None
    # Host push for NOTIFY ``{mission_id, event_id, state_version}``.
    notify_transport: Callable[[Mapping[str, Any]], None] | None = None
    # Unique final writer for a READY closeout (handoff item 7); None installs
    # ``assurance_final_writer.finalize_assured_mission`` bound to this commit.
    finalizer: Callable[[str, Mapping[str, Any]], AssuranceRef | None] | None = None
    resolve_signature: Any | None = None
    admitted_rules: Mapping[str, Any] | None = None
    read_ttl_ms: int = DEFAULT_READ_TTL_MS
    # SHA-256 identity of the Host build that reads through the S25 verbs; None
    # binds the constant "unbound" digest (isolated candidates, seams).
    host_fingerprint: str | None = None


@dataclass(frozen=True)
class InstalledAssurance:
    policy: AssurancePolicy
    authority: FixedPrincipalAuthority
    local_checks: AssuranceLocalChecks
    review: AssuranceReviewConsumer
    review_runtime: AssuranceReviewRuntime
    validity: AssuranceValidity
    consumers: Mapping[str, Any]
    factory: AssuranceMissionFactory
    tick: AssuranceTick
    root_incarnation_id: str
    startup: Mapping[str, Any]
    api: Any = None


def install_assurance(orchestrator: Any, ports: AssuranceDeploymentPorts) -> InstalledAssurance:
    """Bind the four consumers, factory, authority and tick on this Orchestrator.

    Refuses a partial second installation, a missing/unauthorised root gate and
    an Orchestrator whose runtime is not assembled yet (the CAS is the original
    workspace artifact store). Construction completes before any runtime field
    is changed, so a refused constructor leaves nothing half installed.
    """
    if not isinstance(ports, AssuranceDeploymentPorts):
        raise AssuranceError("ASSURANCE_DEPLOYMENT_PORTS_REQUIRED")
    tenant_id = text(ports.tenant_id)
    if not isinstance(ports.principal, Principal):
        raise AssuranceError("CURRENT_AUTHENTICATED_PRINCIPAL_REQUIRED")
    commit, store = orchestrator.commit, orchestrator.store
    gate = orchestrator._assurance_root_gate
    if gate is None or commit._assurance_root_gate is not gate:
        raise AssuranceError("ASSURANCE_ROOT_GATE_UNBOUND")
    root = gate.require_execution().root_incarnation_id
    if (
        commit._assurance_factory is not None
        or commit._assurance_read_authority is not None
        or getattr(commit, "_assurance_validity", None) is not None
        or orchestrator._assurance_tick is not None
        or orchestrator._assurance_local_checks is not None
        or orchestrator._assurance_reviews is not None
    ):
        raise AssuranceError("ASSURANCE_ALREADY_INSTALLED")
    assembled = getattr(orchestrator, "_assembled", None)
    if assembled is None:
        raise AssuranceError("ASSURANCE_RUNTIME_NOT_ASSEMBLED")
    cas = assembled.workspaces.artifact_store
    policy = AssurancePolicy()
    deployment = getattr(getattr(orchestrator, "_config", None), "deployment_policy", None)
    authority = FixedPrincipalAuthority(
        commit,
        tenant_id=tenant_id,
        principal=ports.principal,
        policy=policy,
        deployment=deployment,
        ttl_ms=ports.read_ttl_ms,
    )
    require_root = orchestrator._require_assurance_execution_root
    local_checks = AssuranceLocalChecks(
        commit, tenant_id=tenant_id, cas=cas, require_current_root=require_root
    )
    review = AssuranceReviewConsumer(
        commit,
        tenant_id=tenant_id,
        principal_id=ports.principal.principal_id,
        authority=authority,
        cas=cas,
        check_adapter=local_checks,
    )
    review_runtime = AssuranceReviewRuntime(orchestrator, review)
    consumers = {
        "REVIEW": review,
        "VALIDITY": AssuranceValidityConsumer(commit, tenant_id=tenant_id),
        "CLOSEOUT": AssuranceCloseoutConsumer(
            commit,
            tenant_id=tenant_id,
            finalizer=ports.finalizer or partial(finalize_assured_mission, commit),
        ),
        "NOTIFY": AssuranceNotifyConsumer(
            commit, tenant_id=tenant_id, transport=ports.notify_transport
        ),
    }
    select = ports.select_profile or (lambda spec: default_assurance_profile_for_new_mission())

    def selector(spec: Any) -> bool:
        chosen = select(spec)
        if chosen is None:
            return False
        if not isinstance(chosen, AssurancePolicy) or chosen.to_json() != policy.to_json():
            raise AssuranceError("ASSURANCE_POLICY_UNREGISTERED")
        return True

    factory = AssuranceMissionFactory(
        commit,
        tenant_id=tenant_id,
        policy=policy,
        require_creation_root=require_root,
        requirements=ports.requirements or mission_spec_requirements(ports.principal),
        reconcile=lambda mission_id: activation_inventory(store, mission_id),
        selector=selector,
    )
    tick = AssuranceTick(
        orchestrator,
        consumers=consumers,
        root_incarnation_id=root,
        require_execution_root=require_root,
        tenant_id=tenant_id,
    )
    # Everything constructed; now bind, in the order the original entries read.
    orchestrator._assurance_local_checks = local_checks
    review_runtime.install()
    validity = AssuranceValidity(
        commit,
        tenant_id=tenant_id,
        principal_id=ports.principal.principal_id,
        cas=cas,
        check_adapter=local_checks,
        authority=authority,
        resolve_signature=ports.resolve_signature,
        admitted_rules=ports.admitted_rules,
    )
    from ..api.assurance import AssuranceApi

    api = AssuranceApi(
        commit,
        tenant_id=tenant_id,
        principal=ports.principal,
        validity=validity,
        host_fingerprint=ports.host_fingerprint or fingerprint({"host": "unbound"}),
    )
    commit._assurance_read_authority = authority.read
    commit._assurance_factory = factory
    orchestrator._assurance_tick = tick
    orchestrator.install_assurance_read_api(api, tenant_id=tenant_id, principal=ports.principal)
    startup = reconcile_startup(orchestrator, tenant_id=tenant_id, root_incarnation_id=root)
    return InstalledAssurance(
        policy=policy,
        authority=authority,
        local_checks=local_checks,
        review=review,
        review_runtime=review_runtime,
        validity=validity,
        consumers=consumers,
        factory=factory,
        tick=tick,
        root_incarnation_id=root,
        startup=startup,
        api=api,
    )


__all__ = (
    "AssuranceDeploymentPorts",
    "FixedPrincipalAuthority",
    "InstalledAssurance",
    "activation_inventory",
    "install_assurance",
    "mission_spec_requirements",
    "reconcile_startup",
)
