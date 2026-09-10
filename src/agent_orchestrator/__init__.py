# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Agent orchestration framework built on ``simple_harness.agents`` (BaseAgent).

Step 2 (ORCH-BUILD-v1.0 §4): the reliable single-Task Mission closure.  The
package is a modular monolith laid out after the design's §27; orchestration
state lives in its own SQLite library and is written only by the Commit Service.
"""

from .version import __version__

__all__ = ("__version__",)
