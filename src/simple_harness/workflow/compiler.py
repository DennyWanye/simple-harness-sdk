# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Compiler facade kept separate from immutable graph definitions."""

from .definition import compile_workflow
from .errors import WorkflowDefinitionError

WorkflowCompileError = WorkflowDefinitionError


__all__ = ("WorkflowCompileError", "compile_workflow")
