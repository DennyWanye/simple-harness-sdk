# 代码评审第 1 轮（独立评审，opus，只读）

- 日期：2026-09-11
- 对象：提交 `627b90e`，对比基准 `e0fd62c`（SDK 0.9.8 / agent_orchestrator 0.9.1 Host 接线支持）
- 结论：**SHIP_WITH_FIXES**
- 处置：见 `../journal.md` §4

## 1. 总评

开关关闭时，编排运行期没有找到在本机执行模型代码的路径：
- 唯一的子进程是 `tool_gateway.py:118` 的 `run_pytest`，它有三个调用点：
  - `deterministic_checks.py:195`，前面由 `verifier_router.py:204` 拦住；
  - `event_handler.py:3314`；
  - `tool_gateway.py:330`。
- `VerifierRouter` 和 `WorkspaceToolGateway` 只有两个构造点（`event_handler.py:256`、`assembly.py:370`），开关都已传入。

问题有两处：一是"条件从未验证却 PASS"的完整性缺口；二是 SA-4 与 SA-2 的测试口径不足。

## 2. 发现

### P1

**P1-1　关闭时 Task 级 `pytest:` 条件没人判定，Task 却能 PASS**
- 相关位置：
  - 三个判定点只检查验证层：`task_graph.py:263-275`、`changes.py:526`、`commit_service.py:767`；
  - `deterministic_checks.py:149-156` 的 `rule_check` 只看 `file:` 条件。
- 失败场景：
  - 关闭状态下，Planner 或 Manager 提出 `success_criteria=["pytest:tests/test_a.py"]`、策略为 format / rule / critic 的 Task，会被接受；
  - 只读的 Critic 给出 PASS，Task 以 COMPLETED 收尾，而这条条件从来没有运行过；
  - 引用这个测试文件的 Claim，会按 `claims.py` 评为 SUPPORTED。
- 这正是计划自己用来否定"冲突任务只去掉 code_test"的那个理由。
- 建议：部署层不含 code_test 时，三个判定点、合成模板和 add_task 都拒绝 Task 级 `pytest:` 条件；对已有的旧 Task，`rule_check` 把这类条件判为 FAIL。

**P1-2　SA-4 按原文没有达成**
- 验收 `acceptance.md:14` 要求"走到人工仲裁请求"。
- 但 `request_arbitration` 只在两处调用：
  - `event_handler.py:2078`：冲突 Task 用完了尝试次数；
  - `event_handler.py:2118`：判定出现分歧。
- DEFERRED 路径（`commit_service.py:1718-1736`）只发 `ConflictOpenDeferred`。结果是 Claim 永远停在 DISPUTED，界面上没有可操作的东西。
- 不会死锁：合成门只看 OPEN 状态（`event_handler.py:2891`）。
- 建议二选一：关闭时开一个 conflict 仲裁请求；或者改写 SA-4 的文字。

**P1-3　SA-2 的判定标准分不出新旧行为**
- `test_local_code_execution.py:216-236` 用的场景，在开关打开时本来也不会跑 pytest，放到 `e0fd62c` 上同样通过。
- 间谍本身覆盖完整：`evaluation.py:292` 是延迟导入，读的是已被 patch 的模块属性。
- 网关的纵深防御分支 `tool_gateway.py:330` 没有测试覆盖。
- 建议：
  - 按开关的开 / 关参数化：打开时标记文件存在，关闭时不存在；
  - 直接测试 `WorkspaceToolGateway(local_code_execution=False)` 的 `run_tests`，断言拒绝码。

### P2

- **P2-1　评测不受开关约束。** `evaluation.py:422` 自建的部署政策默认开启执行，`_oracle_check`（`:315`）总是跑 pytest；CLI 经 `__main__.py:1296/1360` 到 `gates.py:179` 可以触发。建议默认拒绝，或写入 Host 文档。
- **P2-2　旧库沿用旧提示词。** 已有 ACTIVE 策略的旧库继续使用 planner-v3 / manager-v1，而 `prompt_versions` 不在 `CONFIG_DERIVED` 中，不记漂移。开关关闭时，v3 仍会列出 code_test，第一次规划大概率白白被拒。另外，`role_templates.py:305-307` 那段"每个角色只登记一个模板"的注释已经过时。
- **P2-3　`CONTEXT_BUILDER_VERSION` 没升。** 它仍是 v3（`context_builder.py:46`），但 Planner 和 Manager 的包内容已经变了，建议升到 v4。回放读的是存储值，不受影响；新的延后原因也不需要改回放投影。
- **P2-4　`create_mission` 的 docstring 与行为不符。** docstring 说"每个拒绝都是 `MissionRequestError`"，但 `MissionConflict` 会原样抛出；`submit_mission` 也没有调用 `validate_spec`。对已有调用方而言，行为只在开关关闭时有变化。
- **P2-5　合成模板在入口处没有检查。** 模板里显式写了 code_test 或 `pytest:` 时，入口不拒绝，要到执行后才以 ERROR 失败。
- **P2-6　最小配置会报错。** `DeploymentPolicy(local_code_execution=False)` 在默认的 `allowed_tools`（含 `run_tests`）下直接报错。
- **P2-7　有三条集成路径缺测试。** 关闭时的 `validate_change` add_task 缺省策略、合成缺省策略、`_check_task_proposal`，都没有集成测试。`__main__.py` 里直接构造 CommitService 的几处不建 Task，没有发现与编排实例不一致的问题。

## 3. 评审者运行过的命令

- `git diff e0fd62c 627b90e`，以及只读的 grep / sed；
- `pytest tests/orchestrator/host_support`：18 passed。
