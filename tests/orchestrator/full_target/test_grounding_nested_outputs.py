"""2026-09-28 真机：合成方法的发布步骤把 wordfreq 与 readme 两步的产出放进一个 delivery 列表。
数据流（准入/编译/导出共用）逐个读出嵌套产出并变成 DATA 需求，但拆分时参数解析把嵌套产出
当成必须当场有值而拒绝，同一原生决定被拒三次、任务失败。含产出的参数应整体留作符号。"""

import json
from pathlib import Path

from agent_orchestrator.contracts.htn import MethodContract
from agent_orchestrator.planning.htn.grounding import data_flows, resolve_arguments

FIXTURE = Path(__file__).parent / "fixtures" / "htn" / "nested_output_method.json"


def test_a_list_of_upstream_outputs_is_left_for_the_compiler():
    method = MethodContract.from_json(json.loads(FIXTURE.read_text()))
    publish = next(step for step in method.steps if step.local_id == "publish")
    assert resolve_arguments(publish, {}, path="step[publish]") == {}
    flow = data_flows(method)
    assert ("wordfreq", "delivery", "publish", "delivery") in flow
    assert ("readme", "delivery", "publish", "delivery") in flow
