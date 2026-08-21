# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Packaged dependency identity for SDK workflow manifests.

The release process updates this digest from the repository ``uv.lock``.  The
wheel needs only the identity, not the private checkout lockfile contents.
"""

from __future__ import annotations

import re

SDK_DEPENDENCY_LOCK_HASH = "9b1bebc8f2bf55dcd1b263b6688ad24c3c713df320d6eaaf441b716f7d8c4ea0"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def validate_dependency_lock_hash(value: object) -> str:
    """Return a strict lowercase SHA-256 dependency identity or fail closed."""

    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("dependency_lock_hash must be a lowercase SHA-256 digest")
    return value


validate_dependency_lock_hash(SDK_DEPENDENCY_LOCK_HASH)

__all__ = ("SDK_DEPENDENCY_LOCK_HASH", "validate_dependency_lock_hash")
