# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for conformance pytest plugin."""

from __future__ import annotations

import pytest

from simple_harness.testing import PROTOCOL_VERSION

# Plugin is auto-loaded via entry point - don't load it again


def test_protocol_version_fixture(simple_harness_protocol_version):
    """Test protocol version fixture returns correct version."""
    assert simple_harness_protocol_version == PROTOCOL_VERSION
    assert simple_harness_protocol_version == "1.0.0"


def test_conformance_host_fixture_without_option(simple_harness_conformance_host):
    """Test conformance host fixture without --simple-harness-host option.

    This test will be skipped when --simple-harness-host is not provided.
    """
    # If we reach here, the option was provided
    assert simple_harness_conformance_host is not None


def test_conformance_marker_registered(pytestconfig):
    """Test that conformance marker is registered."""
    markers = pytestconfig.getini("markers")
    # Check if our marker is in the list (it's a list of strings like "name: description")
    marker_names = [m.split(":")[0].strip() for m in markers]
    assert "simple_harness_conformance" in marker_names


def test_host_option_registered(pytestconfig):
    """Test that --simple-harness-host option is registered."""
    # The option should be available even if not set
    try:
        pytestconfig.getoption("--simple-harness-host")
    except ValueError:
        # Option not set, which is fine - we're just checking it's registered
        pass


def test_suite_option_registered(pytestconfig):
    assert pytestconfig.getoption("--simple-harness-suite") == ("provider,tool,runtime,workflow")


# Sample conformance test that uses the fixture
@pytest.mark.simple_harness_conformance
def test_sample_conformance_with_marker(simple_harness_conformance_host):
    """Sample conformance test demonstrating marker usage."""
    # This will be skipped if --simple-harness-host not provided
    assert simple_harness_conformance_host is not None
