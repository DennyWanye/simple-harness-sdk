# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""One canonical form for workspace-relative paths (code review P1-3 / P1-4): the tool
gateway's untrusted-source check and the claim evidence grading must agree on what
``./docs/x``, ``docs//x`` or ``docs\\x`` denote, or a prefix rule can be bypassed."""

from __future__ import annotations

from pathlib import PurePosixPath


def normalise_workspace_path(path: str) -> str:
    """``./docs/./x`` → ``docs/x``; leading slashes dropped; ``..`` kept verbatim so the
    workspace resolver (which refuses it) still sees it."""

    text = str(path).replace("\\", "/").strip()
    parts = [part for part in PurePosixPath(text).parts if part not in {".", "", "/"}]
    return "/".join(parts)


def under_prefix(path: str, prefixes: tuple[str, ...] | list[str]) -> bool:
    normalised = normalise_workspace_path(path)
    for prefix in prefixes:
        clean = normalise_workspace_path(prefix)
        if clean and (normalised == clean or normalised.startswith(clean + "/")):
            return True
    return False


__all__ = ("normalise_workspace_path", "under_prefix")
