# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Assurance delta registered as migration 26 in the isolated merged candidate.

HTN descriptors 1–24 and TaskGraph descriptor 25 retain their original bytes.
This candidate is not installed in the shared Host; release identity is still WIP.
Use the existing Store runner; never executescript inside a Store transaction.
"""

from importlib.resources import files

from .assurance_barrier_schema import DDL as BARRIER_DDL
from .assurance_upgrade import DDL as UPGRADE_DDL

DDL = (
    files(__package__).joinpath("assurance_schema.sql").read_text(encoding="utf-8")
    + "\n"
    + BARRIER_DDL
    + "\n"
    + UPGRADE_DDL
)
