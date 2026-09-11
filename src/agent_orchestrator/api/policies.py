# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Policy API (ORCH-BUILD §11.4 ``policy propose / evaluate / approve / promote /
rollback``; plan D9-8' / D9-10').

The caller's authenticated identity is fixed when the object is made (``principal``); it
never comes from a model parameter or a workspace file, and no Agent tool reaches this
module.  A person may propose parameters (the whitelist, within range, on top of the
ACTIVE version — "人工参数，非规则改进"), approve or reject a proposal that passed its
evaluation, promote an approved one under the deployment's cooldown, and roll back to a
previously ACTIVE version at once.  ``status`` is the per-version health report a person
reads to decide on a rollback (the rollback itself is a human action)."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from ..governance.permissions import Principal
from ..governance.policies import DeploymentPolicy
from ..governance.promotion import (
    PolicyError,
    code_versions,
    overlay,
    validate_params,
)
from ..observability.secrets import find_secrets
from ..orchestrator.commit_service import CommitService

TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class PolicyRequestError(ValueError):
    """The request is refused at the API door (nothing was written)."""


class PolicyApi:
    def __init__(
        self,
        commit: CommitService,
        principal: Principal,
        *,
        deployment: DeploymentPolicy | None = None,
        max_concurrency: int | None = None,
        profiles: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(principal, Principal):
            raise PolicyRequestError("the API needs the caller's authenticated Principal")
        self._commit = commit
        self._principal = principal
        self._deployment = deployment or DeploymentPolicy()
        self._max_concurrency = max_concurrency
        self._profiles = None if profiles is None else tuple(profiles)

    @staticmethod
    def _clean(text: str) -> str:
        if text and find_secrets(text):
            raise PolicyRequestError("the text looks like it contains a secret and is refused")
        return text

    @property
    def _store(self) -> Any:
        return self._commit.store

    # ------------------------------------------------------------ reading
    def list(self) -> dict[str, Any]:
        active = self._store.active_policy()
        return {
            "active_version_id": None if active is None else active["version_id"],
            "versions": [
                {k: v.get(k) for k in ("version_id", "status", "source")}
                for v in self._store.list_policy_versions()
            ],
            "proposals": [
                {
                    "proposal_id": p["proposal_id"],
                    "version_id": p["version_id"],
                    "state": p["state"],
                    "source": p["source"],
                    "verdict": (p.get("last_evaluation") or {}).get("verdict"),
                }
                for p in self._store.list_policy_proposals()
            ],
        }

    def show(self, identifier: str) -> dict[str, Any]:
        found = self._store.get_policy_proposal(identifier) or self._store.get_policy_version(
            identifier
        )
        if found is None:
            raise PolicyRequestError(f"no policy proposal or version {identifier!r}")
        if "proposal_id" in found:
            found = {
                **found,
                "evaluations": self._store.list_policy_evaluations(identifier),
                "decisions": self._store.list_policy_decisions(identifier),
            }
        return found

    def status(self) -> dict[str, Any]:
        """Per-version health (plan D9-8'): how the Missions bound to each version ended."""

        rows = []
        for version in self._store.list_policy_versions():
            bound = self._store.list_mission_policies(version["version_id"])
            ended: Counter[str] = Counter()
            for binding in bound:
                mission = self._store.get_mission(str(binding["mission_id"]))
                if mission is not None:
                    ended[str(mission.status)] += 1
            finished = sum(ended[s] for s in TERMINAL)
            rows.append(
                {
                    "version_id": version["version_id"],
                    "status": version["status"],
                    "source": version["source"],
                    "missions": len(bound),
                    "by_status": dict(ended),
                    "failure_rate": None if not finished else round(ended["FAILED"] / finished, 4),
                }
            )
        active = self._store.active_policy()
        return {
            "active_version_id": None if active is None else active["version_id"],
            "versions": rows,
            "activations": self._store.list_policy_activations(),
            "note": "回滚由人执行：policy rollback [--to VERSION]",
        }

    # ------------------------------------------------------------ writing
    def propose(self, partial: Mapping[str, Any], *, note: str = "") -> dict[str, Any]:
        """A person's parameters on top of the ACTIVE version (plan D9-10'): whitelist
        and ranges checked here; the proposal still needs an evaluation and approval."""

        self._clean(note)
        self._clean(json.dumps(dict(partial), ensure_ascii=False, default=str))
        active = self._store.active_policy()
        if active is None or not active.get("params"):
            raise PolicyRequestError("this library has no ACTIVE policy to propose over")
        try:
            candidate = overlay(active["params"], dict(partial))
        except PolicyError as error:
            raise PolicyRequestError(str(error)) from error
        ceiling = self._max_concurrency or int(active["params"]["mission_concurrency"])
        problems = validate_params(
            candidate, base=active["params"], max_concurrency=ceiling, profiles=self._profiles
        )
        if problems:
            raise PolicyRequestError("; ".join(problems))
        manifest = {
            "kind": "human",
            "note": "人工参数，非规则改进",
            "principal_id": self._principal.principal_id,
            "text": note,
            "partial": dict(partial),
            "base_version_id": active["version_id"],
            "code_versions": code_versions(),
        }
        return self._commit.propose_policy(
            candidate,
            manifest=manifest,
            source=f"human:{self._principal.principal_id}",
            principal=self._principal,
        )

    def approve(
        self, proposal_id: str, *, nonce: str | None = None, note: str = ""
    ) -> dict[str, Any]:
        return self._commit.decide_policy(
            proposal_id,
            principal=self._principal,
            decision="approve",
            nonce=nonce or uuid.uuid4().hex,
            note=self._clean(note),
        )

    def reject(
        self, proposal_id: str, *, nonce: str | None = None, note: str = ""
    ) -> dict[str, Any]:
        return self._commit.decide_policy(
            proposal_id,
            principal=self._principal,
            decision="reject",
            nonce=nonce or uuid.uuid4().hex,
            note=self._clean(note),
        )

    def promote(self, proposal_id: str, *, accept_fixture_evidence: bool = False) -> dict[str, Any]:
        return self._commit.promote_policy(
            proposal_id,
            principal=self._principal,
            cooldown_seconds=float(self._deployment.policy_cooldown_seconds),
            accept_fixture_evidence=accept_fixture_evidence,
        )

    def rollback(self, *, to: str | None = None, reason: str = "") -> dict[str, Any]:
        return self._commit.rollback_policy(
            principal=self._principal, to=to, reason=self._clean(reason)
        )


__all__ = ("PolicyApi", "PolicyRequestError")
