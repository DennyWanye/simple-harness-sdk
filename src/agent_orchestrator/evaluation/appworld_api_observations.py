# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-authorized, episode-local public API observations (never shell output)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from types import MappingProxyType
from typing import Any
from uuid import uuid4

# Host-approved public GETs only, with exact fields admitted into evidence.
# API names supplied by agents or clients cannot extend this immutable policy.
PUBLIC_READ_APIS: Mapping[tuple[str, str], tuple[str, ...]] = MappingProxyType({
    ("supervisor", "show_active_task"): ("instruction", "status", "answer"),
    ("supervisor", "show_profile"): ("first_name", "last_name"),
})

_PROFILE_RAW_FIELDS = {"first_name", "last_name", "email", "phone_number",
                       "birthday", "sex"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return sha256(canonical(value)).hexdigest()


def project(app: str, api: str, response: Any) -> dict[str, str | None]:
    fields = PUBLIC_READ_APIS.get((app, api))
    if fields is None or not isinstance(response, dict):
        raise ValueError("API response is not an allowed public read projection")
    keys = set(response)
    if (app, api) == ("supervisor", "show_profile") and keys == _PROFILE_RAW_FIELDS:
        response = {name: response[name] for name in fields}
    elif keys != set(fields):
        raise ValueError("API response has unexpected fields")
    if any(value is not None and not isinstance(value, str) for value in response.values()):
        raise ValueError("API response has an unexpected field type")
    projected: dict[str, str | None] = dict(response)
    if len(canonical(projected)) > 65536:
        raise ValueError("API response exceeds the evidence limit")
    return projected


@dataclass(frozen=True, slots=True)
class AppWorldAPIReceipt:
    receipt_id: str
    run_id: str
    task_id: str
    episode_id: str
    world_version: int
    app: str
    api: str
    parameters_sha256: str
    response_sha256: str
    service_identity: str | None
    evidence_kind: str = "host_public_api_observation"


class AppWorldAPILedger:
    """Private authority registry. A copied/forged receipt is never authority."""

    def __init__(self, *, run_id: str, task_id: str, episode_id: str) -> None:
        self.run_id, self.task_id, self.episode_id = run_id, task_id, episode_id
        self._registered: dict[str, AppWorldAPIReceipt] = {}

    def record(self, *, world_version: int, app: str, api: str,
               response: dict[str, str | None],
               service_identity: str | None) -> AppWorldAPIReceipt:
        receipt = AppWorldAPIReceipt(
            uuid4().hex, self.run_id, self.task_id, self.episode_id, world_version,
            app, api, digest({}), digest(project(app, api, response)), service_identity,
        )
        self._registered[receipt.receipt_id] = receipt
        return replace(receipt)

    def list_receipts(self, *, world_version: int | None = None) -> tuple[AppWorldAPIReceipt, ...]:
        return tuple(replace(receipt) for receipt in self._registered.values()
                     if world_version is None or receipt.world_version == world_version)

    def verify(self, receipt: AppWorldAPIReceipt, *, run_id: str, task_id: str,
               episode_id: str, world_version: int, app: str, api: str,
               response: Any, service_identity: str | None) -> bool:
        if (type(receipt) is not AppWorldAPIReceipt
                or type(receipt.world_version) is not int
                or type(world_version) is not int):
            return False
        stored = self._registered.get(receipt.receipt_id)
        if stored is None or stored != receipt:
            return False
        try:
            projected = project(app, api, response)
        except (ValueError, TypeError):
            return False
        return (receipt.run_id == run_id == self.run_id
                and receipt.task_id == task_id == self.task_id
                and receipt.episode_id == episode_id == self.episode_id
                and receipt.world_version == world_version
                and receipt.app == app and receipt.api == api
                and receipt.parameters_sha256 == digest({})
                and receipt.response_sha256 == digest(projected)
                and receipt.service_identity == service_identity
                and receipt.evidence_kind == "host_public_api_observation")


__all__ = ("AppWorldAPIReceipt", "PUBLIC_READ_APIS")
