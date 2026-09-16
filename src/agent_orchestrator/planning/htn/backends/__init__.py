# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""External HTN solver backends.

Each backend is an adapter around a third-party toolchain. Backends never write
planning facts; they return typed results that a caller may turn into a proposal.
"""
