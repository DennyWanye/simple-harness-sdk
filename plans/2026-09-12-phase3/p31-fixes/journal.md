# P3.1 遗留修复 · 记录

## 0. handoff（每次提交时更新）

- 2026-09-12：
  - 已写好 `program.md`、本切片的 plan 和 acceptance（第 1 版），计划交独立评审中。
  - 在评审给出结论之前，只写测试草稿，不改源码。这是第 7 步的教训。
  - 测试草稿已写好：`drafts/test_p31_fixes.py`，覆盖 FX-1..FX-5，接口按 plan §2 设计，包括 `TaskBudgetFloor`、`validate_graph(task_floor=)`、`validate_change(task_floor=)`、`OrchestratorConfig.min_task_tokens`，以及 Planner 输入里的两个下限字段。评审结论出来后，移到 `tests/orchestrator/host_support/`，先确认它们是红的，再动手实现。
  - P3.2 相关代码的梳理已派给只读的 Explore 子代理，结果回来后写进 `p32/` 的计划。
  - 进展：
    - 计划评审 READY_WITH_CHANGES，已处置（§1）；
    - 实现完成，提交 `a84e2a4` 已推送；
    - 目前三件事在进行：全量回归（看门狗）、代码评审第 1 轮、P3.2 计划评审。
  - 下一步：
    1. 处置代码评审意见；
    2. 回归红集 ⊆ 73；
    3. 构建 wheel 0.9.11，脚本在新 scratchpad 的 `wheel-0.9.11/build-and-verify.sh`；
    4. Host 用 `pin-0911/pin.py <wheel> <commit> <epoch> 0.9.10 0.9.11 0.9.4` 改钉；
    5. 跑 Host `tests/orchestration`，更新 ARCHITECTURE。
  - 起点：SDK main `29daa9c`（0.9.10 / 0.9.3），Host main `e1f9e6cb`。
- 接手须知：
  - 真实模型只用 deepseek-flash；
  - 全量回归要带看门狗（scratchpad 里的 `watchdog.py`，思路是用 subprocess 起 pytest，超时就杀掉整个进程组）；
  - 同一时间只跑一个 pytest；
  - 后台全量回归期间，不要往 `tests/` 里放依赖尚未实现代码的测试。

## 1. 计划评审处置

第 1 轮评审结论为 READY_WITH_CHANGES（独立评审子代理，opus）。全部接受，已落进 plan 与 acceptance 第 2 版。

| 编号 | 处置 |
|---|---|
| P1-1 `upsert_artifact` 在行已存在时什么都不写（ON CONFLICT DO NOTHING） | 新增 `Store.update_artifact_verification(artifact_id, status)`，用 UPDATE 改 json 列。FX-5 用新开的只读连接从库里读回来再断言 |
| P1-2 下限没有算候选数 | 下限改为 `k × (base + critic)`（critic 部分只在 policy 含 critic_review 时计入），k 取 Mission 绑定策略里的 `candidates_per_task`。CommitService 不再在构造时定死一个下限，而是注入 `TaskBudgetFloor(base, critic)` 与一个 `candidates_for(mission_id)` 回调，提交时按 Mission 计算。补 k=2 的测试 |
| P2-1 base 用的是 config 的值，没考虑 profile 覆盖 | base 取 config 与所有 profile 的 `default_max_output_tokens` 中的最大值 |
| P2-2 "结构上可行"的说法写过头了 | 计划改为：下限是必要条件，只保证第一个 Attempt 加上它的 Critic 能预留成功，不保证之后的修复和重试 |
| P2-3 缺省值没有写 | `validate_graph` / `validate_change` / CommitService 的 `task_floor` 缺省为 None，即不检查，只由 Orchestrator 注入；检查只遍历 `proposal.tasks`，不碰合成模板那段检查 |
| P2-4 Manager 的输入里看不到下限 | Manager 输入包加上 `min_task_tokens` 两个字段 |
| P2-5 停止原因没写明 | 预算池连一个 Task 的下限都容不下时，停止原因是 `planning_failed`；补测试，断言失败原因里有 `task_budget_below_floor` |
| P2-6 "同一事务"没有断言手段 | 用故障点 `after_accept_before_supersede` 注入崩溃，断言回滚后仍是 UNVERIFIED。另外，`fail_result` 把被判 FAIL 的结果的产物标为 REJECTED，以区别于"从没验过"；被取代的候选保持 UNVERIFIED |
| 真实模型风险 | 真实调参 `REAL_KNOBS` 下，含 critic 的下限会升到几万 tokens，真实 Planner 可能要多试几次。计划写明这一点；本仓库的真实 opt-in 测试把 `max_planning_attempts` 调到 3。Host 的门口默认预算是 400000，通常够用 |
| F-ORCH-2 | 评审确认"设计如此"成立：原文 §25.2 和理论第 09 章里，RETRY_WAIT → PENDING 指的是另起一个新的 Attempt；代码里 `TERMINAL_ATTEMPT` 含 RETRY_WAIT。Host 读模型怎么显示，记为遗留建议 |
| FX-7 | 验收写明：Host 门口已经补了默认预算，原生路径走不到 null 预算，所以 F-ORCH-1 由 SDK 测试证明 |

## 2. 实现与测试

先写测试：`tests/orchestrator/host_support/test_p31_fixes.py` 收集时报 ImportError（`TaskBudgetFloor` 还不存在），红。

实现改动：

| 位置 | 改动 |
|---|---|
| `graph/task_graph.py` | 新增 `TaskBudgetFloor(base, critic).floor_for(policy, candidates)` 和 `floor_refusal(...)`；`validate_graph` 新增 `task_floor=None, candidates=1` 两个参数，逐 Task 检查生效预算，即显式写的值或从池里分到的份额 |
| `graph/changes.py` | `validate_change` 新增同样两个参数，在预算池检查之后，对每个新增 Task 的最终预算（含缺省份额）做下限检查 |
| `context/context_builder.py` | Planner 的 `budget_for_tasks` 与 Manager 包的 `budget_floor` 都带上 `min_task_tokens` / `min_task_tokens_with_critic_review` |
| `orchestrator/commit_service.py` | 构造参数新增 `task_floor`、`candidates_for`，两处 validate 调用传入 `candidates=self._candidates(mission.id)`；`accept_result` 把产物标 VERIFIED，`fail_result` 标 REJECTED，都在各自的事务里完成 |
| `storage/store.py` | 新增 `update_artifact_verification`，用 UPDATE 改 json 列；新增常量 `ARTIFACT_VERIFICATION_STATES` |
| `orchestrator/event_handler.py` | 新增 `_budget_floor_rule`：base 取配置与各 profile 的 `default_max_output_tokens` 中的最大值；新增 `_candidates_for`（取绑定策略里的 `candidates_per_task`）与 `_budget_floor`；注入 CommitService，并传给两种输入包 |
| `runtime/assembly.py` | 新增 `OrchestratorConfig.min_task_tokens`（None / 0 / 正数），负数报错 |
| `governance/policies.py` | 在 `SNAPSHOT_FIELDS` 里把 `min_task_tokens` 登记为 include：它影响哪些任务图会被接受。第一轮测试就是因为新字段没有登记而失败的 |
| 版本 | 0.9.11 / 0.9.4，同步 `public-api.json` 与 CHANGELOG |

测试与检查结果：
- ruff 与 mypy（80 个源文件）都干净。
- 第一轮受影响测试集：128 passed 之后，在 step07 碰到快照字段登记的失败，修好后重跑。
- 第二轮挂在 `step05/test_step05_review_fixes.py::test_p1_3_a_crash_between_proposal_and_commit_still_applies_the_change_once`，10 分钟不动，由人工结束进程。排查如下：
  - fixture `demo_dynamic_dag_provider` 里 Manager 的 `add_task` 预算是 E=20000（format、rule）和 B2=30000（含 code_test），`candidates_per_task` 默认为 1，都高于下限，所以不是下限拒绝造成的。
  - 单独跑这条测试，1.97 s 通过。
  - 它不在已知失败基线里。
  - 这条测试用了 `lease_seconds=0.3`，挂住那一次，mypy 和 ruff 同时在跑，初步判断是时序敏感。
  - 已加 `faulthandler_timeout=120` 和 900 s 看门狗，整批重跑以确认。
- 第三轮（带看门狗）：278 passed / 3 skipped / 1 failed，用时 170 s。step05 那条这次正常通过，印证它是时序敏感，不是这次改动造成的。
  - 失败的是 step09 的结构测试 `test_no_decision_point_reads_a_whitelisted_value_from_the_configuration`。原因：`_candidates_for` 在读不到时退回读 `self._config.candidates_per_task`，而这条测试规定，第 9 步之后，白名单里的值只能从 Mission 绑定的策略里读。
  - 修复：改为只读 `policy_for(mission_id)["candidates_per_task"]`。策略里的参数总是解析完整的，这个键一定存在。
  - 修复后，step09 的策略绑定测试加上本切片的测试，31 passed；ruff 与 format 干净。
- 提交：`a84e2a4`（代码、测试、文档），另有 `6871376`（P3.2 的计划），都已推送。

## 3. 回归与 wheel

（待填）

## 4. 代码评审处置

（待填）

## 5. 遗留

- 预算超支没有单独发可观测事件，放到 P3.5 处理。
- 角色模板没有写入下限说明，P3.4 考虑是否升提示词版本。
