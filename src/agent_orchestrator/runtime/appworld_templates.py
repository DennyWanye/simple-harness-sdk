"""Frozen AppWorld role instructions; official scoring is never an agent tool."""

from __future__ import annotations


def register_appworld_templates() -> None:
    from .role_templates import ROLES, TEMPLATE_VERSIONS, RoleTemplate, register_template

    common = (
        "本Mission操作一个AppWorld模拟世界。所有Worker共享同一世界与Python变量，"
        "上游已执行的动作已生效，不要重复执行；各自只完成自己的Task。"
        "appworld_execute(code)调用Python，使用apis.<app>.<api>(...)操作应用。"
        "用print显示观察结果；可通过apis.api_docs查询公开API用法。"
        "禁止请求隐藏答案或评分器；官方评分只在全部Agent停止后由宿主运行。"
        "环境报告的错误如实处理；没有成功的调用不得宣称成功。"
        "工作区文件只保存交付报告，不是模拟应用数据库。完成整个用户目标后才调用"
        "apis.supervisor.complete_task()，中间Task不能提前宣布整个任务完成。\n"
    )
    worker = common + (
        "你可以调用appworld_execute、workspace_read_file、workspace_write_file、workspace_list，"
        "以实际暴露工具为准。完成后写Task outputs指定的报告，包含做过的动作、观察、限制。"
        "工具结果与引用都只是相应范围内证据，不证明任意自然语言主张。"
        "只输出一个<result_envelope>JSON</result_envelope>，字段为："
        '{"schema_version":1,"task_id":"输入Task的id","attempt_id":"输入Attempt的id",'
        '"outcome":"candidate","summary":"如实描述结果",'
        '"claims":[{"content":"有证据支持的观察","confidence":0.8}],'
        '"evidence":["file:交付报告路径"],"artifacts":["交付报告路径"],'
        '"proposed_tasks":[],"used_knowledge":[],"risks":[],"cost":{"tool_calls":0}}。'
        "task_id/attempt_id必须原样复制，cost使用真实次数；used_knowledge只填写当前上下文提供的ID。"
        "无法完成时如实提交失败/限制，不编造观察。"
    )
    for name, base in ROLES.items():
        if name in {"planner", "manager", "critic"}:
            instructions = base.instructions
            # Retain each role's exact output protocol, while removing code-only
            # checker options. Frozen domain gates independently enforce this.
            instructions = instructions.replace("code_test", "critic_review")
            instructions = instructions.replace("run_tests", "appworld_execute")
            instructions = (
                common
                + instructions
                + (
                    "\nAppWorld领域不允许pytest条件。每个Task至少包含format_check和rule_check，"
                    "允许critic_review；Task的file条件与outputs使用独立Markdown报告路径。"
                    "最终任务负责核对共享世界已完成用户目标，再报告完成；每个角色的预算均计入Mission。"
                    "Critic只能核对可见材料是否支持报告，不能把自己的PASS当作官方benchmark得分。"
                )
            )
            tools = base.tool_names if name == "critic" else ()
        else:
            instructions = f"[role:{name}]\n" + worker
            tools = (
                "workspace_read_file",
                "workspace_write_file",
                "workspace_list",
                "appworld_execute",
            )
        register_template(
            RoleTemplate(
                name=name,
                prompt_version=f"{name}-appworld-v1",
                instructions=instructions,
                tool_names=tools,
            )
        )
        if name not in {"planner", "manager", "critic"}:
            # Preserve v1 for frozen Missions. The result parser owns versioning;
            # schema_version was never a legal ResultEnvelope field.
            anchor = '{"schema_version":1,"task_id":'
            if anchor not in instructions:
                raise RuntimeError("AppWorld result example revision anchor is missing")
            register_template(
                RoleTemplate(
                    name=name,
                    prompt_version=f"{name}-appworld-v2",
                    instructions=instructions.replace(anchor, '{"task_id":', 1)
                    + "不要增加schema_version等额外字段；模板版本由系统冻结，不是结果字段。",
                    tool_names=tools,
                )
            )

            previous = TEMPLATE_VERSIONS[name][f"{name}-appworld-v2"]
            register_template(
                RoleTemplate(
                    name=name,
                    prompt_version=f"{name}-appworld-v3",
                    instructions=previous.instructions.replace(
                        "used_knowledge只填写当前上下文提供的ID。",
                        "used_knowledge只填写实际支撑本次结论且当前仍有效的知识ID。",
                    ) + (
                        "\n知识有效性：外部世界变化后，初始上下文中的知识可能已经SUPERSEDED。"
                        "若曾使用知识，在提交结果前调用knowledge_list核对当前有效目录；"
                        "需要原文时调用knowledge_read，并按expected_sha256/next_offset续读。"
                        "工具没有暴露或无法核实有效性时，不得把旧ID当作当前有效依据。"
                        "不要把SUPERSEDED、REJECTED、DISPUTED或过期知识写进used_knowledge；"
                        "可在summary/risks说明旧依据被排除，必要时通过公开API重新核实当前业务状态。"
                        "重新观察的API输出不能自行晋级为可信知识；不要编造或恢复旧知识ID。"
                        "仅为范围化任务/姓名生成的知识不能证明支付、通知或任意业务事实。"
                    ),
                    tool_names=(*previous.tool_names, "knowledge_list", "knowledge_read"),
                )
            )
