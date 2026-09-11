# Host 接线支持 · SDK 0.9.8 / agent_orchestrator 0.9.1 · 记录

- 日期：2026-09-11 起
- 计划：同目录 `plan.md`；验收：Host `simple_harness/plans/2026-09-11-orchestrator-host-integration/acceptance.md` §A（SA-1 至 SA-7）

## 0. 现在做到哪里 / 下一步（handoff，每次提交时更新）

- 2026-09-11：
  - 切片 A、B、C 已完成：测试先行写好 `tests/orchestrator/host_support/`（先红），实现后 18 条全绿，ruff 与 mypy（79 个源文件）都干净。
  - 下一步是切片 D：
    1. 版本号升到 0.9.8 / 0.9.1；
    2. 跑全量回归，红集必须 ⊆ 73 条基线；
    3. 构建 wheel，在干净 venv 中验证；
    4. 独立代码评审并处置；
    5. 推送，然后回到 Host 做 H1（钉 0.9.8）。
- 接手须知：
  - 全量回归在后台跑时，不要改源码与版本号，也不要往 `tests/` 放新测试。
  - 真实模型一律用 deepseek-flash。

## 1. 实现要点（切片 B / C）

| 位置 | 改动 |
|---|---|
| `governance/policies.py` | `DeploymentPolicy.local_code_execution: bool = True`；为 False 时 `allowed_tools` 不能包含 `run_tests`，否则构造即报错；`to_json` 输出该字段；新增 `deployed_layers(deployment)` |
| `contracts/models.py` | `SYSTEM_DEFAULT_POLICY`，以及 `default_change_policy(deployed)`。关闭本机执行时，默认策略从 `format_check / rule_check / code_test` 改为 `format_check / rule_check / critic_review`：用独立 Critic 代替测试，不留下只有格式与规则两层的空策略。放在 contracts 是为了避开 `graph.changes → planning.manager` 的循环导入 |
| `graph/task_graph.validate_graph`、`graph/changes.validate_change`、`CommitService._check_task_proposal` | 三个"验证层是否已部署"的判定点都改用部署层集合；`CommitService(deployed_layers=…)` 由 Orchestrator 按部署政策传入 |
| `graph/changes.NewTaskNode.from_json` / `add_tasks`、`planning/manager.synthesis_task` | 缺省策略由调用方按部署层给出 |
| `CommitService._open_conflict` | 部署层不含 code_test 时，`deferred_reason = "local_code_execution_disabled"`，不创建冲突 Task |
| `verification/verifier_router.py` | 开关关闭时，旧 Task 的 `code_test` 层记为 ERROR（`undeployed: true`） |
| `orchestrator/event_handler.py` | Mission 判定阶段不跑 `pytest:` 条件，判为 `met=false`，理由写明本机代码执行已关闭；新增 `create_mission` 入口，与 `submit_mission` 共用 `_check_mission_door`（动作条件检查 + pytest 条件检查）和 `_commit_mission`；Planner 与 Manager 的输入包带上 `deployed_verification_layers` |
| `runtime/tool_gateway.py`、`runtime/assembly.py` | 开关关闭时 `run_tests` 以 `local_code_execution_disabled` 拒绝（纵深防御，权限交集本来就会先把它挡掉） |
| `runtime/role_templates.py` | 升到 `planner-v4` / `manager-v2`，由旧版本经精确替换得到（锚点缺失时导入即报错）；`planner-v3` / `manager-v1` 继续登记 |
| `api/missions.py`、`__main__.py` | 新增 `spec_from_request`；`MissionApi(orchestrator=…)`；CLI 的 `mission create` 走编排入口，provider_kind 按实际记录 |

## 2. 测试

| 验收 | 测试 | 结果 |
|---|---|---|
| SA-1 | `test_a_planner_asking_for_code_test_is_refused_and_the_replan_completes` | PASS：第一版图被 `verification_policy_undeployed` 拒绝，重新规划后完成；两次 Planner 输入包都带着部署层 |
| SA-2 | `test_model_written_test_files_never_run` | PASS：Worker 写了 `conftest.py` 和 `test_probe.py`（导入时会写标记文件），但标记文件不存在，`run_pytest` 间谍的调用次数为 0 |
| SA-3 | 模板与默认策略两条测试 | PASS |
| SA-4 | `test_a_contradiction_is_deferred_without_a_conflict_task` | PASS |
| SA-5 | `test_create_mission_entry.py`，共 9 条（含 CLI 子进程） | PASS |
| SA-6 | 默认值不变；设置矛盾时报错 | PASS |
| 开关打开前建的 Mission | code_test 层记为 ERROR 且 Mission FAILED；`pytest:` 条件判为未满足，理由写明原因，停止原因为 `mission_criteria_unmet` | PASS |

过程记录：`test_a_pytest_mission_criterion_…` 第一次失败，原因是测试写错了事件名。Mission 判定发出的是 `MissionSuccessJudged`，`MissionCriteriaJudged` 属于动作路径。诊断确认实现行为正确：pytest 一次也没有运行，条件判为未满足，理由写明本机代码执行已关闭。之后修正了测试，并收紧断言：逐条核对每个条件的判定结果与停止原因。

## 3. 回归与 wheel

（待填）

## 4. 代码评审

（待填）

## 5. 遗留

（待填）
