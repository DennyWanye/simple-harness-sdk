# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Typed physical boundaries for the official capability-build profile."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from simple_harness.contracts import JsonValue


class CapabilitySearchPort(Protocol):
    async def search(
        self,
        *,
        query: str,
        operation_key: str,
        admission: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]: ...


class CapabilitySourcePolicyPort(Protocol):
    async def authorize_source(
        self,
        *,
        source: str,
        operation_key: str,
        admission: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]: ...


class IsolatedBuildPort(Protocol):
    async def build(
        self,
        *,
        candidate: JsonValue,
        source_policy: Mapping[str, JsonValue],
        operation_key: str,
        admission: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]: ...


class PackageStorePort(Protocol):
    async def store(
        self,
        *,
        package: JsonValue,
        operation_key: str,
        admission: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]: ...


class CapabilityActivatePort(Protocol):
    async def activate(
        self,
        *,
        package_ref: str,
        activation_key: str,
        operation_key: str,
        admission: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]: ...


class CapabilityBuildAuthorizationPort(Protocol):
    async def authorize_build(
        self, *, operation_key: str, admission: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...


__all__ = (
    "CapabilityActivatePort",
    "CapabilityBuildAuthorizationPort",
    "CapabilitySearchPort",
    "CapabilitySourcePolicyPort",
    "IsolatedBuildPort",
    "PackageStorePort",
)
