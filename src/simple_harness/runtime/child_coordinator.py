# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Typed public child facade bound to one Runtime."""

from __future__ import annotations

from simple_harness.contracts import thaw_json

from .child_runs import ChildLaunchRequest, ChildRunHandle


class ChildCoordinator:
    def __init__(self, runtime) -> None:
        self._runtime = runtime

    async def launch(self, request: ChildLaunchRequest) -> ChildRunHandle:
        self._runtime._require_started()
        launch = thaw_json(request.launch_payload)
        snapshot = thaw_json(request.start_snapshot)
        if not isinstance(launch, dict) or not isinstance(snapshot, dict):
            raise TypeError("child launch payloads must remain JSON objects")
        result = self._runtime._uow.claim_profile_launch_and_commit_child(
            ticket_id=request.ticket.ticket_id,
            expected_catalog_generation=request.ticket.catalog_generation,
            launch_request=launch,
            command_id=request.command_id,
            child_run_id=request.child_run_id,
            request_id=request.request_id,
            attachment_policy=request.attachment_policy,
            start_snapshot=snapshot,
            event_id=f"{request.child_run_id}:created",
            now=self._runtime._now(),
        )
        run = self._runtime._uow.read_run(result.child_run_id)
        if run is None:
            raise RuntimeError("durable child launch did not create a Run")
        if run.state.value not in {"completed", "failed", "cancelled"}:
            run = await self._runtime._activate(run.run_id)
            self._runtime._schedule(run.run_id)
        return ChildRunHandle(run, result.ticket, result.command)


__all__ = ("ChildCoordinator",)
