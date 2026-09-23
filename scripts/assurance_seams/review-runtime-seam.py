from seam_paths import SDK, EVIDENCE
import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from provider_fixture import MODEL, ScriptedProvider
from simple_harness import Message, MessageRole
from simple_harness.agents import AgentConfig, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from agent_orchestrator.assurance.codec import fingerprint
from agent_orchestrator.runtime.agent_worker import AgentBridge
from agent_orchestrator.runtime.assurance_turn_sources import read_actual_review_turn

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        provider = ScriptedProvider(['{"schema_version":2}'])
        ports = AgentRuntimePorts(provider=provider, authorization=AllowAllAuthorization(), database_path=str(Path(tmp)/'runtime.db'), model=MODEL, owner_id='assurance-runtime-seam')
        async with build_agent_runtime(ports) as runtime:
            config = AgentConfig(name='assurance-review', instructions='Return JSON.', model_profile_ref='fixture-only')
            bridge = AgentBridge(runtime, unpriced=True)
            agent_id, run_id, _ = await bridge.create(creation_key='review-source-seam', config_json=config.to_json())
            message = Message(MessageRole.USER, 'Exact evidence bytes: alpha').to_dict()
            receipt = await bridge.submit(agent_id=agent_id, input_id='review-input-seam', message_json=message)
            agent = await runtime.open(agent_id)
            await agent.wait_turn(receipt['turn_id'], timeout=5)
            intent = SimpleNamespace(agent_id=agent_id, expected_turn_id=receipt['turn_id'], creation_key='review-source-seam', input_id='review-input-seam', config={'agent_config':config.to_json(),'message':message}, input_hash=fingerprint(message))
            actual = read_actual_review_turn(bridge, intent)
            if actual.exposure_error:
                from simple_harness.contracts import RunId, RequestId, thaw_json
                from simple_harness.execution.provider_invocations import provider_invocation_id
                result = agent.get_result(receipt['turn_id'])
                reqid = next(r.removeprefix('provider-request:') for r in result.usage_refs if r.startswith('provider-request:'))
                p = runtime.uow.read_provider_invocation(provider_invocation_id(RunId(run_id), RequestId(reqid)))
                selection = runtime.uow.read_agent_context_selection_by_request(reqid)
                print('DIAG', p.state, p.handed_off_at, p.settled_at, p.request_fingerprint, fingerprint(thaw_json(p.request_json)), selection.request_hash, selection.agent_id == result.agent_id, selection.turn_id == result.turn_id)
                print('RESPONSE', thaw_json(p.response_json), 'PUBLIC', result.public_output.to_dict())
            assert actual.exposure_error is None, actual.exposure_error
            exposure = json.loads(actual.exposure_json)
            assert any(row['message'] == message for row in exposure['messages'])
            assert provider.calls == 1
            print(json.dumps({'status':'PASS','scope':'real kernel with scripted provider; no real model or Host','provider_calls':provider.calls,'source':json.loads(actual.source_json),'selection_id':exposure['selection_id'],'message_count':len(exposure['messages'])}, ensure_ascii=False))
asyncio.run(main())
