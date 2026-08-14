# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Capability builder host protocol and contracts.

This module provides the host-side authority for generated capability builds:
admission, validation, and finalization. The actual builder agent runs as a
child workflow; this module validates its output and routes it to the appropriate
destination (capability manager or candidate receipt store).
"""

from .builder_contracts import (
    BuildOperationKind,
    CapabilityBuildCandidateAdmissionV1,
    CapabilityBuildCompletion,
    CapabilityBuildEvidence,
    CapabilityBuildLaunch,
    CapabilityBuildLineage,
    CapabilityBuildSearchEvidence,
    CapabilityManagerInstallRequest,
)
from .builder_errors import CapabilityBuildError

__all__ = [
    "BuildOperationKind",
    "CapabilityBuildCandidateAdmissionV1",
    "CapabilityBuildCompletion",
    "CapabilityBuildError",
    "CapabilityBuildEvidence",
    "CapabilityBuildLaunch",
    "CapabilityBuildLineage",
    "CapabilityBuildSearchEvidence",
    "CapabilityManagerInstallRequest",
]
