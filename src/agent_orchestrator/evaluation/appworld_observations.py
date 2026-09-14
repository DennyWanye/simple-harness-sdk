# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-owned AppWorld execution provenance, never an API-state attestation.

A receipt proves only that this episode ran the bound code and received the
bound shell output. Printed JSON, including text that looks like an API reply,
remains arbitrary Python output. A returned string may itself be a traceback;
returned_unverified does not attest Python or API semantic success. Independently
authenticated API-state evidence requires a separate authority and is not supplied
by this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import uuid4


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AppWorldExecutionReceipt:
    receipt_id: str
    run_id: str
    episode_id: str
    task_id: str
    execution_id: str
    world_version: int
    code_sha256: str
    output_sha256: str | None
    execution_status: str
    save_status: str
    evidence_kind: str = "untrusted_execution_output"


class AppWorldObservationLedger:
    """Episode-private registry; callers receive detached immutable receipts."""

    def __init__(self, *, run_id: str, task_id: str) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.episode_id = uuid4().hex
        self.world_version = 0
        self._receipts: dict[str, AppWorldExecutionReceipt] = {}

    def advance_world(self) -> int:
        # Advance before a possibly failing execute/restore: partial effects
        # cannot leave an older observation trusted as current.
        self.world_version += 1
        return self.world_version

    def new_execution_id(self) -> str:
        return uuid4().hex

    def record(
        self,
        *,
        execution_id: str,
        code: str,
        output: str | None,
        execution_succeeded: bool,
        save_succeeded: bool,
    ) -> AppWorldExecutionReceipt:
        if execution_id in self._receipts:
            raise ValueError("execution receipt identity already exists")
        receipt = AppWorldExecutionReceipt(
            receipt_id=uuid4().hex,
            run_id=self.run_id,
            episode_id=self.episode_id,
            task_id=self.task_id,
            execution_id=execution_id,
            world_version=self.world_version,
            code_sha256=_digest(code),
            output_sha256=_digest(output) if execution_succeeded and output is not None else None,
            execution_status="returned_unverified" if execution_succeeded else "failed",
            save_status="succeeded" if save_succeeded else "failed",
        )
        self._receipts[execution_id] = receipt
        return replace(receipt)

    def list_receipts(self, *, current_only: bool = False) -> tuple[AppWorldExecutionReceipt, ...]:
        receipts: Iterable[AppWorldExecutionReceipt] = self._receipts.values()
        if current_only:
            receipts = (r for r in receipts if r.world_version == self.world_version)
        return tuple(replace(r) for r in receipts)

    def get_receipt(self, execution_id: str) -> AppWorldExecutionReceipt | None:
        receipt = self._receipts.get(execution_id)
        return replace(receipt) if receipt is not None else None

    def verify(
        self,
        receipt: AppWorldExecutionReceipt,
        *,
        run_id: str,
        episode_id: str,
        task_id: str,
        execution_id: str,
        code: str,
        output: str,
        world_version: int,
    ) -> bool:
        if (
            type(receipt) is not AppWorldExecutionReceipt
            or type(receipt.world_version) is not int
            or type(world_version) is not int
            or not isinstance(code, str)
            or not isinstance(output, str)
        ):
            return False
        stored = self._receipts.get(execution_id)
        return (
            stored is not None
            and stored == receipt
            and receipt.run_id == run_id == self.run_id
            and receipt.episode_id == episode_id == self.episode_id
            and receipt.task_id == task_id == self.task_id
            and receipt.execution_id == execution_id
            and receipt.world_version == world_version == self.world_version
            and receipt.code_sha256 == _digest(code)
            and receipt.output_sha256 == _digest(output)
            and receipt.execution_status == "returned_unverified"
            and receipt.save_status == "succeeded"
        )


__all__ = ("AppWorldExecutionReceipt",)
