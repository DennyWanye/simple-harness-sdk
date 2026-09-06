"""Real public admission, source-owned crash points, and public recovery controls.

Empty schema2 here is an explicit trusted no-recall attestation, not missing data.
Actual Memory receipt behavior is covered by test_context_use_public_memory.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

import simple_harness as h

from .test_context_use_public_memory import UnusedAuthorization, UnusedTools, digest


class EmptyAuthority:
    authority_scope_ref = "required-scope-1"

    async def authorize_recall_context_use(self, request):
        raise AssertionError("explicit empty attestation needs no Memory request")

    async def prepare_snapshot(self, request):
        messages = (h.Message(h.MessageRole.USER, "public no-recall input"),)
        payload = dict(
            messages=[
                dict(
                    role="user",
                    content="public no-recall input",
                    name=None,
                    call_id=None,
                    metadata={},
                )
            ],
            tools=[],
            temperature=None,
            max_output_tokens=128,
            metadata={},
        )
        return h.RunContextSnapshot(
            "empty-snapshot",
            request.run_id.value,
            request.provider_turn_ordinal,
            request.prior_context_revision,
            1,
            {},
            messages,
            (),
            None,
            128,
            {},
            digest(payload),
            schema_version=2,
            recall_subject="subject-1",
            recall_intents=(),
        )


class Provider:
    def __init__(self):
        self.calls = []

    async def invoke(self, request, *, cancel):
        self.calls.append(request)
        from simple_harness.providers import ProviderResponse, ProviderUsage

        return ProviderResponse(
            request.request_id,
            h.Message(h.MessageRole.ASSISTANT, "done"),
            model="consumer-model",
            usage=ProviderUsage(10, 2, 12),
            finish_reason="stop",
        )


def ports(path, provider, scope):
    authority = EmptyAuthority() if scope is not None else None
    if authority is not None:
        authority.authority_scope_ref = scope
    return h.ConsumerRuntimePorts(
        provider=provider,
        tool_executor=UnusedTools(),
        authorization=UnusedAuthorization(),
        database_path=str(path),
        recall_context_use_authority=authority,
        run_context_authority=authority,
    )


CRASH = r"""
import asyncio,os,sys
import simple_harness as h
from simple_harness.runtime import RunStart
from tests.integration.runtime.test_context_use_admission import ports,Provider
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
from simple_harness.runtime.react_checkpoint import DurableReactCheckpoint
point=sys.argv[2]
if point=='accepted_before_driver':
    original=SqliteExecutionUnitOfWork.create_with_start_snapshot
    def crash(self,*args,**kwargs):
        original(self,*args,**kwargs)
        os._exit(0)
    SqliteExecutionUnitOfWork.create_with_start_snapshot=crash
elif point=='accepted_command':
    original=SqliteExecutionUnitOfWork.submit_start_command
    def crash(self,*args,**kwargs):
        original(self,*args,**kwargs)
        os._exit(0)
    SqliteExecutionUnitOfWork.submit_start_command=crash
else:
    original=DurableReactCheckpoint._write
    def crash(self,run_id,lease,expected_version,state):
        result=original(self,run_id,lease,expected_version,state)
        if state.source_schema_version==(6 if point=='schema6_before_pin' else 7):
            # Abandoned activation explicitly releases its owned lease; no expiry wait,
            # no result/checkpoint rewriting. The required-mode fact must survive it.
            self._port.release_runtime_lease(lease,now=self._clock())
            os._exit(0)
        return result
    DurableReactCheckpoint._write=crash
async def main():
    runtime=await h.build_consumer_runtime(ports(sys.argv[1],Provider(),'required-scope-1'))
    await runtime.__aenter__()
    client=h.RunClient(runtime)
    if point=='accepted_command':
        await client.submit_start(h.StartCommandIntent('fixture-namespace','fixture-projection','start-command',
            h.RunId('admission-run'),h.RequestId('root-request'),'root-turn',
            h.ConversationTurnInput(h.AgentIdentity('d','hh','subject-1','session'),
                h.Message(h.MessageRole.USER,'public no-recall input'),'public no-recall input'),
            input={'messages':[{'role':'user','content':'public no-recall input'}],
                'capability_snapshot':{'tools':[]},'max_output_tokens':128}))
    else:
        await client.start(RunStart(h.ExecutionSessionId('session'),h.RunId('admission-run'),h.RequestId('root-request'),
            turn_id='root-turn',tool_catalog_generation=1,input={'messages':[{'role':'user','content':'public no-recall input'}],
                'capability_snapshot':{'tools':[]},'max_output_tokens':128}))
        await runtime.wait_idle(h.RunId('admission-run'))
    raise AssertionError('crash point not reached')
asyncio.run(main())
"""


@pytest.mark.parametrize(
    "point",
    ["accepted_before_driver", "accepted_command", "schema6_before_pin", "schema7_after_pin"],
)
def test_required_mode_survives_pre_driver_crash_and_rejects_port_downgrade(tmp_path, point):
    path = tmp_path / "execution.sqlite"
    child = subprocess.run(
        [sys.executable, "-c", CRASH, str(path), point],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=15,
        env=os.environ.copy(),
    )
    assert child.returncode == 0, child.stderr

    async def run():
        for scope in (None, "changed-scope"):
            provider = Provider()
            runtime = await h.build_consumer_runtime(ports(path, provider, scope))
            try:
                with pytest.raises(ValueError, match="context_use_admission_scope_differs"):
                    await runtime.__aenter__()
                assert not provider.calls
            finally:
                await runtime.__aexit__(None, None, None)
        provider = Provider()
        runtime = await h.build_consumer_runtime(ports(path, provider, "required-scope-1"))
        try:
            await runtime.__aenter__()
            await runtime.wait_idle(h.RunId("admission-run"))
            for _ in range(200):
                result = runtime.client.query(h.RunId("admission-run"))
                if result is not None and result.state.value in (
                    "completed",
                    "failed",
                    "cancelled",
                ):
                    break
                await asyncio.sleep(0.01)
            assert result.state.value == "completed", result
            assert len(provider.calls) == 1
            view = runtime.client.read_provider_context_use(
                h.RunId("admission-run"), h.RequestId("admission-run:provider-turn:1")
            )
            assert view.requests == view.receipts == ()
            assert view.authority_scope_ref == "required-scope-1" and view.handoff_attempt == 1
        finally:
            await runtime.__aexit__(None, None, None)

    asyncio.run(run())
