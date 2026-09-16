# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The seed method library: acceptance fixtures, not a global method library (§7.3).

Two pilot domains ship here as *data* — ``code`` and ``appworld`` — each with its
schemas, its predicate observer signatures, its task types and its methods.  They
exist to demonstrate the generality claim of §7.1: the planner decomposes both
domains with no domain knowledge of its own, and a third domain is added by
dropping four JSON files in a directory.

They are written by a human and go through the same admission protocol as any
other submission (:func:`~.loader.admit_domain`); nothing here is promoted past
``TRIAL_ADMITTED``, and a successful trial does not make any of it a global
method (§7.3 step 6).
"""

from .loader import (
    SEED_ROOT,
    SeedDomain,
    admit_domain,
    available_domains,
    install_domain,
    load_domain,
    load_domain_path,
    seed_content_hash,
)

__all__ = (
    "SEED_ROOT",
    "SeedDomain",
    "admit_domain",
    "available_domains",
    "install_domain",
    "load_domain",
    "load_domain_path",
    "seed_content_hash",
)
