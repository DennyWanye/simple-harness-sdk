# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for protocol version compatibility."""

from __future__ import annotations

from simple_harness.testing import PROTOCOL_VERSION


def test_protocol_version_format():
    """Test protocol version follows semantic versioning."""
    parts = PROTOCOL_VERSION.split(".")
    assert len(parts) == 3, "Protocol version must be MAJOR.MINOR.PATCH"
    major, minor, patch = parts
    assert major.isdigit(), "Major version must be numeric"
    assert minor.isdigit(), "Minor version must be numeric"
    assert patch.isdigit(), "Patch version must be numeric"


def test_protocol_version_is_stable():
    """Test protocol version is at 1.0.0 or later."""
    major = int(PROTOCOL_VERSION.split(".")[0])
    assert major >= 1, "Protocol version must be 1.0.0 or later for SDK release"


def test_protocol_version_current():
    """Test current protocol version is 1.0.0."""
    assert PROTOCOL_VERSION == "1.0.0"


def test_protocol_version_compatibility_check():
    """Test protocol version compatibility logic.

    Major version mismatch should be treated as incompatible.
    Minor/patch differences within same major are compatible.
    """

    def parse_version(version: str) -> tuple[int, int, int]:
        major, minor, patch = version.split(".")
        return int(major), int(minor), int(patch)

    def is_compatible(sdk_version: str, host_version: str) -> bool:
        """Check if host version is compatible with SDK version."""
        sdk_major, _, _ = parse_version(sdk_version)
        host_major, _, _ = parse_version(host_version)
        # Major version must match
        return sdk_major == host_major

    # Compatible versions (same major)
    assert is_compatible("1.0.0", "1.0.0")
    assert is_compatible("1.0.0", "1.1.0")
    assert is_compatible("1.0.0", "1.0.1")
    assert is_compatible("1.2.3", "1.5.0")

    # Incompatible versions (different major)
    assert not is_compatible("1.0.0", "2.0.0")
    assert not is_compatible("2.0.0", "1.0.0")
    assert not is_compatible("1.5.0", "0.9.0")


def test_protocol_version_fail_closed():
    """Test that major version mismatch fails closed.

    This documents the expected behavior: if a host implementation
    reports a different major protocol version, the conformance
    framework should reject it rather than attempt compatibility.
    """

    def should_reject(sdk_version: str, host_version: str) -> bool:
        """Return True if host should be rejected due to version mismatch."""
        sdk_major = int(sdk_version.split(".")[0])
        host_major = int(host_version.split(".")[0])
        return sdk_major != host_major

    # These should all be rejected
    assert should_reject("1.0.0", "2.0.0")
    assert should_reject("2.0.0", "1.0.0")
    assert should_reject("1.5.0", "0.9.0")

    # These should not be rejected
    assert not should_reject("1.0.0", "1.0.0")
    assert not should_reject("1.0.0", "1.1.0")
    assert not should_reject("1.2.3", "1.5.0")
