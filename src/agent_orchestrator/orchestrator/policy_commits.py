# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The Commit Service's policy half (plan D9-2' / D9-3' / D9-8'): the registry of policy
versions, proposals, evaluations, human decisions and activations, and the version every
Mission is bound to.  A mixin of ``CommitService``, so the single writer keeps writing
every row and event inside a Store transaction (ORCH §2).

Deployment-level facts live on the sentinel timeline ``deployment``; every event key
carries something unique (an activation sequence number, an evaluation id, a receipt
hash) so a repeated change is never swallowed by idempotency (review P1-7)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts.models import sha256_hex
from ..governance.permissions import Principal, decision_receipt_hash
from ..governance.promotion import (
    DEPLOYMENT_TIMELINE,
    NON_PROMOTABLE,
    PROMOTABLE,
    PROPOSAL_TRANSITIONS,
    VERDICTS,
    code_versions,
    diff_params,
    expands,
    interpreter_versions,
    params_hash,
    step_problems,
)
from ..governance.promotion import (
    proposal_id as make_proposal_id,
)
from ..governance.promotion import (
    version_id as make_version_id,
)
from ..observability.secrets import find_secrets
from ..scheduling.backpressure import STATE_KEY, BackpressureState

if TYPE_CHECKING:
    from ..contracts import Event
    from ..storage.store import Store

IN_FLIGHT = frozenset({"CREATED", "PLANNING", "ACTIVE"})
POLICY_PATH_PREFIX = "policy/"  # plan D9-10': files an Agent may write but never apply
CONFIG_FILE_NAMES = frozenset(
    {"deployment.json", "deployment_policy.json", "orchestrator_config.json"}
)
LIBRARY_ROLE_KEY = "library_role"  # plan D9-3' (review P1-6): production | evaluation
EVIDENCE_KINDS = ("fixture", "real")


class PolicyCommitError(ValueError):
    """A registry change that is refused (the answer, never a traceback)."""


class PolicyCommitsMixin:
    if TYPE_CHECKING:
        _store: Store

        def _emit(
            self,
            event_type: str,
            mission_id: str,
            *,
            key: str,
            task_id: str | None = None,
            attempt_id: str | None = None,
            payload: Mapping[str, Any] | None = None,
            actor_type: str = ...,
            actor_id: str = ...,
        ) -> Event: ...

    # ------------------------------------------------------------ helpers
    def _policy_event(
        self,
        kind: str,
        key: str,
        payload: Mapping[str, Any],
        principal: Principal | None = None,
    ) -> None:
        actor = (
            {} if principal is None else {"actor_type": "human", "actor_id": principal.principal_id}
        )
        self._emit(kind, DEPLOYMENT_TIMELINE, key=key, payload=dict(payload), **actor)

    @staticmethod
    def _require_principal(principal: Any) -> Principal:
        if not isinstance(principal, Principal):
            raise PolicyCommitError(
                "a policy decision needs an authenticated Principal from the caller"
            )
        return principal

    @staticmethod
    def _refuse_secret_text(text: str) -> None:
        if text and find_secrets(text):
            raise PolicyCommitError("the text looks like it contains a secret and is refused")

    def _version_record(
        self,
        params: Mapping[str, Any],
        *,
        source: str,
        status: str,
        detail: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "version_id": make_version_id(params),
            "params_hash": params_hash(params),
            "params": dict(params),
            "source": source,
            "status": status,
            "interpreter_versions": interpreter_versions(),
            "detail": dict(detail or {}),
        }

    def _require_proposal(self, proposal_id: str) -> dict[str, Any]:
        proposal = self._store.get_policy_proposal(proposal_id)
        if proposal is None:
            raise PolicyCommitError(f"unknown policy proposal {proposal_id}")
        return proposal

    def _require_version(self, version_id: str) -> dict[str, Any]:
        version = self._store.get_policy_version(version_id)
        if version is None:
            raise PolicyCommitError(f"unknown policy version {version_id}")
        return version

    # ------------------------------------------------------------ seed and binding
    def seed_policy(
        self,
        params: Mapping[str, Any],
        *,
        source: str = "seed",
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The first ACTIVE version of a production library: the resolved built-in
        policy (plan D9-3').  A library that already has an ACTIVE version keeps it."""

        with self._store.transaction():
            active = self._store.active_policy()
            if active is not None:
                return active
            record = self._version_record(params, source=source, status="ACTIVE", detail=detail)
            self._store.insert_policy_version(record)
            self._store.set_policy_version_status(record["version_id"], "ACTIVE")
            seq = self._store.insert_policy_activation(
                {
                    "version_id": record["version_id"],
                    "action": "seed",
                    "previous_version_id": None,
                    "detail": dict(detail or {}),
                }
            )
            self._policy_event(
                "PolicySeeded",
                f"seed:{seq}",
                {
                    "version_id": record["version_id"],
                    "seq": seq,
                    "source": source,
                    "detail": dict(detail or {}),
                },
            )
            version = self._store.get_policy_version(record["version_id"])
            assert version is not None
            return version

    def record_policy_drift(
        self, *, config_hash: str, differences: Sequence[Mapping[str, Any]]
    ) -> None:
        """The deployment configuration names whitelisted values that differ from the
        ACTIVE version: recorded, and the ACTIVE version still governs (plan D9-3')."""

        with self._store.transaction():
            active = self._store.active_policy()
            seq = len(self._store.list_policy_activations())
            self._policy_event(
                "PolicyConfigDrift",
                f"{seq}:{config_hash}:{self._store.now}",
                {
                    "active_version_id": None if active is None else active["version_id"],
                    "config_hash": config_hash,
                    "differences": [dict(d) for d in differences],
                    "note": "改白名单项须走 policy propose / promote；生效的仍是 ACTIVE 版本",
                },
            )

    def bind_policy(
        self,
        mission_id: str,
        *,
        provider_kind: str,
        default_params: Mapping[str, Any] | None = None,
        pin: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind a new Mission to a version in its creation transaction (plan D9-3'):
        the ACTIVE one, or — only in an evaluation library — the pinned one."""

        with self._store.transaction():
            existing = self._store.get_mission_policy(mission_id)
            if existing is not None:
                return existing
            if pin is not None:
                record = self._version_record(
                    pin, source="sandbox", status="NEVER_ACTIVE", detail=None
                )
                self._store.insert_policy_version(record)
                version_id, source = record["version_id"], "sandbox"
            else:
                active = self._store.active_policy()
                if active is None:
                    if default_params is None:
                        from ..governance.promotion import resolve_params
                        from ..runtime.assembly import OrchestratorConfig

                        default_params = resolve_params(
                            OrchestratorConfig(evidence_root=self._store.path.parent)
                        )
                    active = self.seed_policy(
                        default_params, detail={"config": "default configuration (no Orchestrator)"}
                    )
                version_id, source = str(active["version_id"]), "active"
            binding = {
                "mission_id": mission_id,
                "version_id": version_id,
                "source": source,
                "provider_kind": provider_kind,
            }
            self._store.bind_mission_policy(binding)
            return binding

    # ------------------------------------------------------------ proposals
    def propose_policy(
        self,
        params: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
        source: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        """One proposal = a parameter version + its provenance (plan D9-2'): the same
        input is the same proposal; the same parameters from other history are the same
        version with another proposal."""

        if set(params) != set(PROMOTABLE):  # review P2-9: the single writer's own guard
            raise PolicyCommitError(
                f"a policy carries exactly {sorted(PROMOTABLE)}; got {sorted(params)}"
            )
        with self._store.transaction():
            record = self._version_record(params, source=source, status="NEVER_ACTIVE", detail=None)
            self._store.insert_policy_version(record)  # a known version keeps its status
            version_id = record["version_id"]
            pid = make_proposal_id(version_id, {**dict(manifest), "source": source})
            existing = self._store.get_policy_proposal(pid)
            if existing is not None:
                return {**existing, "created": False}
            base = self._store.active_policy()
            proposal = {
                "proposal_id": pid,
                "version_id": version_id,
                "state": "PROPOSED",
                "source": source,
                "manifest": dict(manifest),
                "base_version_id": None if base is None else base["version_id"],
                "diff": diff_params(base["params"], params) if base and base.get("params") else [],
                "code_versions": code_versions(),
                "last_evaluation": None,
                "approval": None,
                "principal_id": None if principal is None else principal.principal_id,
            }
            self._store.insert_policy_proposal(proposal)
            self._policy_event(
                "PolicyProposed",
                pid,
                {
                    "proposal_id": pid,
                    "version_id": version_id,
                    "source": source,
                    "base_version_id": proposal["base_version_id"],
                },
                principal,
            )
            return {**proposal, "created": True}

    def refuse_policy_proposal(self, *, manifest: Mapping[str, Any], reasons: Sequence[str]) -> str:
        """No proposal — not enough or untrustworthy history (plan D9-5' / D9-6'): the
        refusal and its reasons are on record; nothing is registered."""

        request = (
            "refusal-" + sha256_hex({"manifest": dict(manifest), "reasons": list(reasons)})[:16]
        )
        with self._store.transaction():
            self._policy_event(
                "PolicyProposalRefused",
                request,
                {"request_id": request, "reasons": list(reasons), "manifest": dict(manifest)},
            )
        return request

    # ------------------------------------------------------------ evaluation and decisions
    def record_policy_evaluation(
        self,
        proposal_id: str,
        *,
        verdict: str,
        reasons: Sequence[str],
        report_hash: str,
        baseline_version_id: str,
        code_versions: Mapping[str, str],
        evidence_kind: str,
        summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if verdict not in VERDICTS:
            raise PolicyCommitError(f"an evaluation verdict is one of {VERDICTS}, not {verdict!r}")
        if evidence_kind not in EVIDENCE_KINDS:
            raise PolicyCommitError(f"evidence_kind is one of {EVIDENCE_KINDS}")
        with self._store.transaction():
            proposal = self._require_proposal(proposal_id)
            if verdict not in PROPOSAL_TRANSITIONS[str(proposal["state"])]:
                raise PolicyCommitError(
                    f"proposal {proposal_id} is {proposal['state']}; it cannot be evaluated again"
                )
            evaluation_id = (
                "evaluation-"
                + sha256_hex({"proposal_id": proposal_id, "report_hash": report_hash})[:16]
            )
            record = {
                "evaluation_id": evaluation_id,
                "proposal_id": proposal_id,
                "verdict": verdict,
                "reasons": list(reasons),
                "report_hash": report_hash,
                "baseline_version_id": baseline_version_id,
                "code_versions": dict(code_versions),
                "evidence_kind": evidence_kind,
                "summary": dict(summary or {}),
                "at": self._store.now,
            }
            if not self._store.insert_policy_evaluation(record):
                return record  # the same report again changes nothing
            proposal.update(state=verdict, last_evaluation=record, approval=None)
            self._store.update_policy_proposal(proposal)
            self._policy_event(
                "PolicyEvaluated",
                evaluation_id,
                {
                    "proposal_id": proposal_id,
                    "evaluation_id": evaluation_id,
                    "verdict": verdict,
                    "evidence_kind": evidence_kind,
                    "baseline_version_id": baseline_version_id,
                    "reasons": list(reasons),
                },
            )
            return record

    def decide_policy(
        self,
        proposal_id: str,
        *,
        principal: Any,
        decision: str,
        nonce: str,
        note: str = "",
    ) -> dict[str, Any]:
        """A person approves or rejects a PASSED proposal; the approval binds the
        proposal, its latest evaluation and that evaluation's baseline (review P1-8)."""

        person = self._require_principal(principal)
        if decision not in {"approve", "reject"}:
            raise PolicyCommitError("a decision is approve or reject")
        if not str(nonce).strip():
            raise PolicyCommitError("a decision needs a nonce")
        self._refuse_secret_text(note)
        with self._store.transaction():
            proposal = self._require_proposal(proposal_id)
            if proposal["state"] != "PASSED":
                raise PolicyCommitError(
                    f"proposal {proposal_id} is {proposal['state']}; "
                    "only a PASSED proposal is decided"
                )
            evaluation = dict(proposal["last_evaluation"] or {})
            binding = {
                "proposal_id": proposal_id,
                "version_id": proposal["version_id"],
                "evaluation_id": evaluation.get("evaluation_id"),
                "baseline_version_id": evaluation.get("baseline_version_id"),
            }
            receipt = decision_receipt_hash(
                request_id=proposal_id,
                binding=binding,
                principal_id=person.principal_id,
                decision=decision,
                nonce=nonce,
            )
            inserted = self._store.insert_policy_decision(
                {
                    "receipt_hash": receipt,
                    "proposal_id": proposal_id,
                    "principal_id": person.principal_id,
                    "decision": decision,
                    "nonce": nonce,
                    "binding": binding,
                    "note": note,
                    "principal": person.to_json(),
                    "at": self._store.now,
                }
            )
            if not inserted:
                raise PolicyCommitError(f"nonce {nonce!r} was already used on {proposal_id}")
            approved = decision == "approve"
            proposal.update(
                state="APPROVED" if approved else "REJECTED",
                approval={**binding, "receipt_hash": receipt, "principal_id": person.principal_id}
                if approved
                else None,
            )
            self._store.update_policy_proposal(proposal)
            self._policy_event(
                "PolicyApproved" if approved else "PolicyRejected",
                receipt,
                {"proposal_id": proposal_id, "receipt_hash": receipt, **binding, "note": note},
                person,
            )
            return {"receipt_hash": receipt, "decision": decision, **binding}

    # ------------------------------------------------------------ promotion and rollback
    def promote_policy(
        self,
        proposal_id: str,
        *,
        principal: Any,
        cooldown_seconds: float,
        accept_fixture_evidence: bool = False,
    ) -> dict[str, Any]:
        """Make an APPROVED proposal's version ACTIVE (plan D9-8' / D9-9): only over
        the baseline it was evaluated against, on the code it was evaluated with, with
        real evidence (or an explicit acceptance of fixture evidence), a bounded step,
        after the cooldown, and never widening concurrency under backpressure."""

        person = self._require_principal(principal)
        with self._store.transaction():
            proposal = self._require_proposal(proposal_id)
            if proposal["state"] != "APPROVED":
                raise PolicyCommitError(
                    f"proposal {proposal_id} is {proposal['state']}; "
                    "only an APPROVED proposal is promoted"
                )
            evaluation = dict(proposal["last_evaluation"] or {})
            approval = dict(proposal["approval"] or {})
            if approval.get("evaluation_id") != evaluation.get("evaluation_id"):
                raise PolicyCommitError("the approval is for an older evaluation; approve again")
            active = self._store.active_policy()
            if active is None:
                raise PolicyCommitError("this library has no ACTIVE policy to promote over")
            if evaluation.get("baseline_version_id") != active["version_id"]:
                raise PolicyCommitError(
                    f"the evaluation's baseline {evaluation.get('baseline_version_id')} is not the "
                    f"ACTIVE version {active['version_id']}; evaluate again"
                )
            if dict(evaluation.get("code_versions") or {}) != code_versions():
                raise PolicyCommitError(
                    f"the evaluation ran on code {evaluation.get('code_versions')}, this is "
                    f"{code_versions()}; evaluate again"
                )
            version = self._require_version(str(proposal["version_id"]))
            if version["status"] == "ACTIVE":
                raise PolicyCommitError(f"version {version['version_id']} is already ACTIVE")
            fixture = evaluation.get("evidence_kind") != "real"
            if fixture and not accept_fixture_evidence and self._store.has_non_fixture_missions():
                raise PolicyCommitError(
                    "this deployment has run Missions that were not fixtures; fixture evidence "
                    "only proves the mechanism — pass accept_fixture_evidence to say so"
                )
            current, candidate = active.get("params"), version["params"]
            if current:
                problems = step_problems(current, candidate)
                if problems:
                    raise PolicyCommitError("one promotion moves too far: " + "; ".join(problems))
            changes = [
                a
                for a in self._store.list_policy_activations()
                if a["action"] in {"promote", "rollback"}
            ]
            if changes:
                since = self._store.now - float(changes[-1]["created_at"])
                if since < cooldown_seconds:
                    raise PolicyCommitError(
                        f"cooldown: the last promotion / rollback was {round(since, 1)} s ago "
                        f"(< {cooldown_seconds} s)"
                    )
            widened = expands(current, candidate) if current else []
            if (
                widened
                and BackpressureState.from_json(
                    self._store.get_scheduler_state(STATE_KEY)
                ).is_raised
            ):
                raise PolicyCommitError(f"backpressure is RAISED: no promotion widens {widened}")
            self._store.set_policy_version_status(active["version_id"], "RETIRED")
            self._store.set_policy_version_status(version["version_id"], "ACTIVE")
            activation = {
                "version_id": version["version_id"],
                "action": "promote",
                "proposal_id": proposal_id,
                "previous_version_id": active["version_id"],
                "approval_receipt": approval.get("receipt_hash"),
                "evidence_kind": evaluation.get("evidence_kind"),
                "accept_fixture_evidence": bool(accept_fixture_evidence),
                "principal_id": person.principal_id,
            }
            seq = self._store.insert_policy_activation(activation)
            proposal["state"] = "PROMOTED"
            self._store.update_policy_proposal(proposal)
            self._policy_event(
                "PolicyPromoted",
                f"{version['version_id']}:{seq}",
                {**activation, "seq": seq},
                person,
            )
            return {**activation, "seq": seq}

    def rollback_policy(
        self, *, principal: Any, to: str | None = None, reason: str = ""
    ) -> dict[str, Any]:
        """Back to a previously ACTIVE version at once (plan D9-8'): the most recent
        RETIRED one unless ``to`` names another RETIRED version; no cooldown or step
        limit; bound Missions, events and usage are left exactly as they are."""

        person = self._require_principal(principal)
        self._refuse_secret_text(reason)
        with self._store.transaction():
            active = self._store.active_policy()
            if active is None:
                raise PolicyCommitError("this library has no ACTIVE policy to roll back")
            target = to
            if target is None:
                for activation in reversed(self._store.list_policy_activations()):
                    candidate = self._store.get_policy_version(str(activation["version_id"]))
                    if (
                        candidate is not None
                        and candidate["version_id"] != active["version_id"]
                        and candidate["status"] == "RETIRED"
                    ):
                        target = candidate["version_id"]
                        break
            chosen = None if target is None else self._store.get_policy_version(target)
            if chosen is None or chosen["status"] != "RETIRED":
                raise PolicyCommitError(
                    "a rollback returns to a RETIRED (previously ACTIVE) version; "
                    f"{target or 'none'} ({chosen['status'] if chosen else 'not available'})"
                )
            still_bound = []
            for binding in self._store.list_mission_policies(active["version_id"]):
                mission = self._store.get_mission(str(binding["mission_id"]))
                if mission is not None and str(mission.status) in IN_FLIGHT:
                    still_bound.append(mission.id)
            self._store.set_policy_version_status(active["version_id"], "ROLLED_BACK")
            self._store.set_policy_version_status(chosen["version_id"], "ACTIVE")
            activation = {
                "version_id": chosen["version_id"],
                "action": "rollback",
                "previous_version_id": active["version_id"],
                "from_version_id": active["version_id"],
                "reason": reason,
                "still_bound": still_bound,
                "principal_id": person.principal_id,
            }
            seq = self._store.insert_policy_activation(activation)
            self._policy_event(
                "PolicyRolledBack",
                f"{chosen['version_id']}:{seq}",
                {**activation, "seq": seq},
                person,
            )
            return {**activation, "seq": seq}

    # ------------------------------------------------------------ library role and drift
    def library_role(self) -> str | None:
        state = self._store.get_scheduler_state(LIBRARY_ROLE_KEY)
        return None if state is None else str(state.get("role"))

    def set_library_role(self, role: str) -> None:
        """A library is production or evaluation for its whole life (plan D9-3')."""

        if role not in {"production", "evaluation"}:
            raise PolicyCommitError(f"a library is production or evaluation, not {role!r}")
        with self._store.transaction():
            current = self.library_role()
            if current is not None and current != role:
                raise PolicyCommitError(f"this is a {current} library; it cannot become {role}")
            if current is None:
                self._store.put_scheduler_state(LIBRARY_ROLE_KEY, {"role": role})

    def record_interpreter_drift(
        self, mission_id: str, *, version_id: str, differences: Sequence[Mapping[str, Any]]
    ) -> None:
        """A resumed Mission's policy is read by other code than the code it was bound
        under (plan D9-4'): said once, on the Mission's own timeline — never silent."""

        rows = [dict(d) for d in differences]
        with self._store.transaction():
            self._emit(
                "PolicyInterpreterDrift",
                mission_id,
                key=f"{version_id}:{sha256_hex(rows)[:16]}",
                payload={
                    "version_id": version_id,
                    "differences": rows,
                    "note": (
                        "绑定版本记录的解释器版本与当前代码不同；"
                        "代码无法运行旧解释器，按当前代码继续并如实记录"
                    ),
                },
            )

    def record_policy_route_unavailable(
        self, mission_id: str, *, version_id: str, dropped: Mapping[str, str]
    ) -> None:
        """A bound routing override names a profile this deployment does not have: that
        item falls back to the deployment's routing, on record (plan D9-4')."""

        with self._store.transaction():
            self._emit(
                "PolicyRouteUnavailable",
                mission_id,
                key=f"{version_id}:{mission_id}",
                payload={"version_id": version_id, "dropped": dict(dropped)},
            )

    # ------------------------------------------------------------ Agents cannot set policy
    def record_policy_suggestion_refused(
        self,
        mission_id: str,
        *,
        source: str,
        keys: Sequence[str],
        path: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Plan D9-10' (S9-06): an online Agent proposed changing policy or a core rule.
        Nothing changes; the refusal is on the Mission's timeline and a person may take
        the idea up with ``policy propose`` (source ``human:<id>``)."""

        named = sorted({str(k) for k in keys})
        digest = sha256_hex({"keys": named, "detail": dict(detail or {})})[:16]
        with self._store.transaction():
            self._emit(
                "PolicySuggestionRefused",
                mission_id,
                key=f"{source}:{path or ''}:{digest}",
                payload={
                    "source": source,
                    "path": path,
                    "keys": named,
                    "core_keys": [k for k in named if k in NON_PROMOTABLE],
                    "detail": dict(detail or {}),
                    "note": (
                        "在线 Agent 不能修改策略或核心安全 / 调度规则；"
                        "如需调整，由人用 policy propose 提出"
                    ),
                },
            )

    def refuse_policy_files(
        self, stored: Any, *, mission_id: str, task_id: str, result_id: str
    ) -> None:
        """An accepted artifact under ``policy/`` or named like a deployment configuration
        is the Agent trying to set policy: refused on record, nothing reads it."""

        for artifact_id in stored.artifacts:
            artifact = self._store.get_artifact(str(artifact_id))
            if artifact is None:
                continue
            path = str(artifact.path)
            if not (path.startswith(POLICY_PATH_PREFIX) or Path(path).name in CONFIG_FILE_NAMES):
                continue
            keys: list[str] = []
            try:
                parsed = json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
                if isinstance(parsed, Mapping):
                    keys = sorted(str(k) for k in parsed)
            except (OSError, ValueError):
                keys = []
            self.record_policy_suggestion_refused(
                mission_id,
                source="worker",
                keys=keys,
                path=path,
                detail={"task_id": task_id, "result_id": result_id, "artifact_id": artifact.id},
            )


__all__ = ("LIBRARY_ROLE_KEY", "PolicyCommitError", "PolicyCommitsMixin")
