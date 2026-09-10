# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""BaseAgent Slice 1 demo: a main Agent delegates a task to a child Agent (real model).

Usage::

    SH_BASEURL=https://host/v1 SH_APIKEY=... SH_MODEL=model-name \\
        .venv/bin/python examples/base_agent_delegation.py "你的复杂任务"

Falls back to the Host repository ``.env`` (BASEURL / APIKEY).  The API key is never
printed; the script asserts it does not appear in its own output.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "agents"))

from real_provider_config import build_real_provider, resolve_real_provider  # noqa: E402

from simple_harness.agents import AgentConfig, AgentLimits, build_agent_runtime  # noqa: E402
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization  # noqa: E402
from simple_harness.agents.tools.delegate import DELEGATE_TOOL_NAME  # noqa: E402


async def main(task: str) -> int:
    config = resolve_real_provider()
    if config is None:
        print("no provider configured: set SH_BASEURL / SH_APIKEY or fill the Host .env")
        return 2
    nonce = secrets.token_hex(6)
    ports = AgentRuntimePorts(
        provider=build_real_provider(config),
        authorization=AllowAllAuthorization(),
        database_path=str(Path(tempfile.mkdtemp()) / "demo.db"),
        model=config.model,
        child_instructions_template=(
            "你是被委派的工作 Agent。用不超过 150 字完成交给你的目标，"
            f"并在结论的最后一行原样写出：验证码 {nonce}"
        ),
    )
    main_config = AgentConfig(
        name="main",
        instructions=(
            "你是主 Agent。收到任务后先用 agent_delegate 把任务委派出去（delegation_id 用 d-1），"
            "然后综合它的结论作答，并把它结论里的验证码一字不改地放在最后一行。"
        ),
        model_profile_ref=config.model,
        tool_names=(DELEGATE_TOOL_NAME,),
        limits=AgentLimits(delegation_wait_seconds=170.0),
    )
    lines: list[str] = []
    async with build_agent_runtime(ports) as runtime:
        agent = await runtime.create(main_config, creation_key="demo-main")
        result = await agent.ask(task, input_id="demo-1", timeout=200)
        answer = result.public_output.content if result.public_output else ""
        lines.append(f"model={config.model} turn={result.turn_id} state={result.state.value}")
        lines.append(f"delegations={result.delegation_count} nonce_relayed={nonce in str(answer)}")
        lines.append("--- final answer ---")
        lines.append(str(answer))
    output = "\n".join(lines)
    assert config.api_key not in output, "API key leaked into demo output"
    print(output)
    return 0 if nonce in output else 1


if __name__ == "__main__":
    sys.exit(
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "请比较三种排序算法并推荐一种。"))
    )
