# 第 3 步 · 执行记录

## 1. 关键裁决

独立 review（claude-opus-5，只读；原文 `reports/plan-review-round1.md`）22 条发现，处置落在 plan §7：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | P0 | 多候选与 `create_attempt`/§25.1 互斥 | D3-5'（Task 状态派生；open 候选上限；accept 走两条合法边） |
| 2 | P0 | BLOCKED→CANCELLED 是新增边 | D3-13'（BLOCKED 不转换，随 Mission 终态终止） |
| 3 | P0 | owner 语义让接管退化成 LOST | D3-10'（owner_scope 常量；owner 每实例唯一；等 SDK 租约过期） |
| 4 | P0 | 同名产物冲突在演示图上必触发 | D3-7'（diff 产物、拓扑序覆盖、并行分支才冲突、静态 outputs 检查） |
| 5 | P0 | 下游重开作弊路径 | D3-7'（上游产物受保护） |
| 6 | P0 | accept/unblock 两事务的死锁窗口 | D3-6'（同事务 + recover 自愈） |
| 7 | P0 | supersede 崩溃窗口活锁 | D3-6'（同事务；迟到结果只记历史） |
| 8 | P1 | 丢弃已提交候选违背理论 10-6 | D3-5'（保留 StoredResult 与产物） |
| 9 | P1 | 僵尸执行者仍可写工作区 | D3-17（unbind） |
| 10 | P1 | 信号量等待被判停滞 | D3-4' |
| 11 | P1 | Mission 级判定收窄成单终结任务 | D3-9'（整合副本） |
| 12 | P1 | Mission 池耗尽归罪 Task | D3-12' |
| 13 | P1 | 预算校验过严、真实 Planner 易被连环拒 | D3-2'（归一 + 反馈） |
| 14 | P1 | 候选是否吃 max_attempts 未定 | D3-5'（计入） |
| 15 | P1 | 迟到费用归账 | D3-6'（recover/判定前重导入结算） |
| 16 | P1 | 双实例锁竞争 | D3-10'（StoreBusy） |
| 17 | P2 | artifact.version 未真正递增 | D3-8' |
| 18 | P2 | 去重过脆 | D3-16 |
| 19 | P2 | 老化/背压静默丢弃 | D3-18（登记推迟第 6 步） |
| 20 | P2 | max_planning_attempts 无旋钮、无反馈 | D3-2' |
| 21 | P2 | Context Builder 硬编码 | 切片 B 一并改 |
| 22 | P2 | graph_version 无落脚点 | D3-18 |

## 2. 执行记录

| 切片 | 提交 | 内容 | 测试 |
|---|---|---|---|
| A 图与 Commit | `7960af3`、`f0d7e31` | `TaskGraphProposal`/`TaskNode`（planner-v2 输出合同，单 `<task_proposal>` 兼容为单节点图）、依赖检查（Kahn 拓扑序、环/缺失/自依赖）、去重、整图校验（预算逐维与求和、工具、形状、层已部署）；`commit_task_graph` 原子提交 + 可重放回执（根 READY、其余 BLOCKED、`graph_version`）、`TaskGraphRejected` 在回滚事务之外落库、`unblock_dependents` | `test_task_graph.py` 3、`test_commit_graph.py` 3 |
| B 调度与传递 | `6170a02`、`e7be878`、`06becaa`、`684b80a` | Frontier + 有界 Allocator（并发上限、`candidates_per_task`）；Commit Service 按 §7 重做：`create_attempt` 多候选（open < candidates；候选计入 max_attempts；候选按份额预留）、Task 状态由 Attempt 集合派生、`accept_result` = 接受 + 替代落选候选（结果保留为历史）+ 解锁依赖者**同一事务**、`_cascade_stop`（READY/ACTIVE/VERIFYING 取消，BLOCKED 不动）、`fail_mission`（Mission 池耗尽不归罪 Task）、`heal_mission`（前沿重算、终态 Task 下遗留候选收口）、`record_late_result`；产物：`merge_accepted`（拓扑序应用祖先产物，依赖链上覆盖合法、并行分支不同 hash 即 `artifact_conflict`）、上游输入冻结进 intent 并注入工作区、受保护集合 = 种子测试 + 上游输入 − Task 声明的 `outputs`、产物集合 = 信封列出 ∪ 相对初始输入的 diff、版本按 (mission, path) 血缘；Mission 判定在整合副本上（pytest/file/独立 Critic）；owner 模型（`OWNER_SCOPE` 常量、每实例 owner_id、`lease ≥ 2×sdk_lease`）；`StoreBusy`；D3-17 终态 Attempt 立即 `gateway.unbind` + 协作取消 | `test_static_dag_closure.py` 8（S3-01/02/03/05/06/08a/08b/08c） |
| B' 双实例 | `076a4f3` | S3-04 两实例同库并行；S3-07a 崩溃后新 owner 接管同一 Attempt/同一 SDK turn；S3-07b 执行者不可见 → LOST + 新 Attempt；`Store.arm(skip=n)` 计数故障点；派发阶段 Agent 消失 → LOST | `test_multi_scheduler.py` 3 |
| C 演示与真实模型 | `9ddb3dd` | CLI `demo --scenario static-dag`（fixtures/env）、报告含各 Task；真实模型 opt-in 测试 | `test_cli_static_dag.py` 1、`test_real_provider_static_dag.py`（opt-in） |
| D 收尾 | `caaa789` + 本文档 | 版本 0.9.1 / agent_orchestrator 0.3.0、CHANGELOG、公开 API 快照版本、testcase 归档、独立代码 review、wheel | — |

实现中的裁决（补充 §1）：
- **上游产物保护与"桩→实现"的矛盾**：D3-7' 把注入下游的上游产物全部保护，但演示图本身就是 A 写桩、B/C 换实现。裁决：TaskNode/Task 增加 `outputs`（Task 声明会改写的路径）；上游输入中被声明为本 Task `outputs` 的路径不受保护；Graph Commit 对互不依赖的两个 Task 声明同一 `outputs` 路径静态拒绝（`artifact_conflict`）；Planner 模板说明该规则。未声明就改写上游文件 → `rule_check` FAIL（P0-5 的作弊路径仍然封死）。
- **Task 级 code_test 只跑本 Task 的 `pytest:` 目标**：第 2 步把 Mission 的 pytest 目标也塞进每次 Task 验证；DAG 下这会让 B 因 C 未完成而失败。Mission 级 pytest 目标改在整合副本上由判定阶段运行（D3-9'）。
- **候选的预留份额**：每个候选预留 `min(attempt_reserve_tokens, task.max_tokens // candidates_per_task)`，否则第二个候选必然 `budget_exhausted`。
- **S3-07 "在途 turn 接管"的边界**：在 provider 调用中途杀死执行者会留下 UNKNOWN 出站调用，SDK 按 S2-08 fail-closed（阻塞、预留保持）——这是既定语义，不是接管失败。因此接管用"提交 turn 后、记录回执前"的崩溃点演示（同一 Attempt、同一 turn 由新 owner 完成）；"执行者不可见"用把 SDK 绑定改成外部 scope + turn 置 failed 模拟。
- **迟到结果**：SUPERSEDED/CANCELLED 的 Attempt 若 turn 仍完成，只记 `ResultRejected(reason=superseded)` 历史，费用照常导入结算。
- **判定 Critic 复用**：单 Task Mission 的整合副本与该 Task 的验收副本相同，复用其 critic_review 判定；多 Task 一律新跑判定 Critic（主体 `<mission>:judge:<n>`，记 Mission 账户）。

## 3. 独立 review（代码）

独立子代理（claude-opus-5，只读）审 `e7be878..9ddb3dd`，原文 `reports/code-review-round1.md`：P0 ×2、P1 ×9、P2 ×10，结论 SHIP-AFTER-P0-P1。处置（提交 `b02b113`、`d919ba5`；决定性测试 `test_static_dag_closure.py::test_r1_*` 与更新后的 S3-03/S3-04/S3-05）：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | P0 | `stop_task` 对 READY Task 抛 `IllegalTransition`（运行期 `artifact_conflict`、首个 Attempt 预留失败两条路径），循环崩溃、Mission 悬空 | `stop_task` 对 READY/VERIFYING 先走合法边到 ACTIVE 再 FAILED；测试 `test_r1_runtime_artifact_conflict_stops_the_task_cleanly`（B/C 未声明 outputs 却写同一路径不同内容 → D 分配时 Task FAILED、Mission `artifact_conflict`、E 仍 BLOCKED） |
| 2 | P0 | 整合副本目录 `<mission>-verify` 被另一实例 `rmtree`，S3-04 下可能得出错误的 `mission_criteria_unmet` | 判定树按实例命名 `<mission>-judge-<owner>-verify`；`_decide` 判定前重读 Mission（不用本轮快照）；`judge_mission` 本身幂等 |
| 3 | P1 | 验证期只续租一次，`lease_seconds` 内验证未完就可能被接管，过期 owner 仍能 accept/fail | `accept_result`/`fail_result` 增加 `owner` 参数，事务内校验当前活租约；验证每记录一层、Critic 轮询每拍都 `_hold_lease`；测试 `test_r1_stale_owner_cannot_commit_a_verdict` |
| 4 | P1 | `_verify` 的终态判断是 TOCTOU；`IllegalTransition`/`CommitRejected` 会打死 `run()` | accept/fail 周围捕获两类异常丢弃裁决；`_cycle` 把 `CommitRejected`/`IllegalTransition` 当作"库被别人改了、本轮跳过" |
| 5 | P1 | D3-6'' 未实现：`_close_attempt` 把 SUBMITTED intent 直接关掉，迟到结果分支不可达 | `b02b113`：SUBMITTED intent 保持打开直到 turn 结束；终态 Mission 也采集（`_collect_after_stop`）；费用导入后才结算预留；S3-05 断言收紧 |
| 6 | P1 | 信封"列出"受保护路径即可绕过保护并顺着依赖链传播 | 列出的受保护路径若 hash 与保护内容不同 → `reject_result(protected_path_rewritten)`，与验证策略无关；受保护路径永不登记为产物；测试 `test_r1_protected_upstream_path_listed_as_artifact_is_rejected` |
| 7 | P1 | intent 里的 `attempt_id` 是调用方预测值 | `create_attempt` 用权威 id 覆写 intent config；测试断言 |
| 8 | P1 | `_run_critic` 派发被别人持有时热旋 | 无进展则 `sleep(poll)`，以 `critic_wait_seconds` 为界 |
| 9 | P1 | 三处空断言（S3-03 `x==x`、S3-05 `late == [] or …`、S3-04 "both dispatched"） | S3-03 比对 A 的已接受产物 hash；S3-05 断言迟到事件恰一条 + intent SETTLED + turn 已提交或已取消；S3-04 断言每个 Attempt 恰一条 `AttemptClaimed`/`AttemptStarted` |
| 10 | P1 | 两个新崩溃点无测试 | `test_r1_fault_points_accept_transaction_and_after_task_completed`：事务内崩溃全部回滚（Task 仍 VERIFYING、B/C BLOCKED、结果 RUNNING）后重放接受同一结果；提交后崩溃 B/C 已 READY、A 不重跑 |
| 11 | P1 | `max_concurrency` 跨实例未强制 | `create_attempt(max_open_attempts=)` 事务内统计 Mission 全部 open Attempt；测试 `test_r1_mission_wide_concurrency_is_enforced_in_the_commit` |
| 12 | P2 | `accept_result` 无条件结算 | 有 unknown usage 不结算 |
| 13 | P2 | unbind/cancel 不在转换处；`cancel_mission` 无释放对应 | 已由 #5 覆盖：终态 Attempt 的在途 turn 由循环发现后立即 unbind+cancel，直至 turn 结束再采集 |
| 14 | P2 | `break` 应为 `continue` | 已改 |
| 15 | P2 | 受保护上游文件缺失时静默不保护 | 改为 fail closed（`ArtifactConflict`） |
| 16 | P2 | `_bind_workspace` 的 `ArtifactConflict` 未捕获 | `_dispatch` 捕获 → Attempt LOST(`upstream_artifact_missing`) + `stop_task(artifact_conflict)` |
| 17 | P2 | 空 try/except | 删 |
| 18 | P2 | `recover()` 无 `StoreBusy` 守卫 | 加守卫 |
| 19 | P2 | Mission 池 attempts 维度的停止原因与 D3-12' 文本不一致 | 按 plan：Mission 账户任何维度 → `budget_exhausted`（detail 带维度）；S3-08c 更新 |
| 20 | P2 | 同 Task 两个候选同一路径版本号可能相同 | 未做，登记 §5 |
| 21 | P2 | S3-07 的 0.6 s 租约在慢机器上可能在验证中过期 | 已由 #3 的持续续租缓解；登记 §5 |

## 4. 证据

| 项 | 结果 |
|---|---|
| fixtures 决定性测试 | `tests/orchestrator` **64 passed, 2 skipped**（step02 45 + step03 19：图 3、图提交 3、闭环 8（S3-01/02/03/05/06/08a/b/c）、review 回归 5、双实例 3、CLI 1；2 个 opt-in 真实模型用例跳过） |
| SDK 全量回归 | 73 红 ⊆ 基线，**0 新红**（2104 passed；`public-api.json` 版本 0.9.1，SDK 公共 API 无变化） |
| mypy | `src/agent_orchestrator` 46 文件 0 错 |
| 真实模型（DeepSeek `deepseek-v4-pro`，unpriced 记账） | run1 COMPLETED（138 s，2 Task 链，下游注入 4 个上游产物，43 967 tokens）；run2 COMPLETED（77 s，35 956 tokens）；run3（review 处置后）COMPLETED（210 s，第 1 次图提案被拒 → 带反馈第 2 次通过，40 324 tokens，4 笔预留全部 SETTLED）。报告 `reports/real-static-dag-run{1,2,3}.md`。三次 Planner 都拆成两节点链（L3-1） |
| 发布物 | `simple_harness_sdk-0.9.1-py3-none-any.whl`，源提交 `d919ba5`，`SOURCE_DATE_EPOCH=1789063729`，sha256 **`7f7552404c91453244c071f0713f5fef90b38d7db0126ae7908071ddb8b79392`**；干净 venv（uv, py3.12，wheel + pytest + tiktoken）从归档源根跑 `tests/orchestrator + tests/agents + tests/unit/contracts + tests/execution 的 v10/迁移`：**327 passed, 5 skipped, 1 failed（基线已知红 `test_execution_v3_to_v4_migration.py::test_completed_null_continuation…`）**；安装后 `python -m agent_orchestrator demo --scenario static-dag --provider fixtures --max-concurrency 2` → COMPLETED，5 个 Task 全部 COMPLETED |
| 独立 review | plan review 22 条（§1）、代码 review 21 条（§3）全部处置或登记 |

## 5. 遗留

| # | 事项 | 归属 |
|---|---|---|
| L3-1 | 真实 Planner（DeepSeek）对 textkit Mission 三次都拆成两节点链（实现→交付），未自发拆出并行分支；并行路径由 fixtures 与 CLI 演示覆盖 | 第 5 步（动态拆解）再评估 Planner 提示 |
| L3-2 | 同一 Task 两个候选改同一路径时 `artifact.version` 可能同号（`upsert_artifact` 无 (mission,path,version) 唯一约束） | 第 4 步（综合/合并）一并做 |
| L3-3 | 在途 provider 调用中途丢失执行者 = SDK UNKNOWN 出站调用，按 S2-08 保持阻塞；接管只对"已提交未开跑/两次调用之间"的 turn 成立 | 既定语义；第 6 步做对账通道 |
| L3-4 | §19.4 老化、§18.5 背压上限未做（D3-18） | 第 6 步 |
| L3-5 | 双实例同库仅在同一事件循环内验证（S3-04/S3-07）；跨进程锁竞争只有 `StoreBusy` 包装，未做压力测试 | 第 6 步 |
| L3-6 | 第 2 步遗留 L2-1/L2-2/L2-4/L2-6/L2-7 未变（Critic 层序、format_check 恒 PASS、无网络隔离、unpriced 记账、预留非硬上限） | 各自归属不变 |

## 6. 终态

**VERDICT: SHIPPED**（2026-09-11，SDK main `d919ba5` 起 + 本文档提交；版本 simple_harness 0.9.1 / agent_orchestrator 0.3.0）

| 验收 | 结果 |
|---|---|
| S3-01 A→(B‖C)→D→E 并行执行、下游含上游产物、整合判定 | PASS（fixtures + CLI demo + wheel 演示；真实 DeepSeek 三次 COMPLETED，但为两节点链） |
| S3-02 环路整图拒绝、Planner 带反馈重提 | PASS（fixtures；真实 run3 第 1 次提案被拒后第 2 次通过） |
| S3-03 B 不重做、C 修复、D 等待 | PASS |
| S3-04 两个 Orchestrator 同时领取 | PASS（同一事件循环内两实例；每 Attempt 恰一次 AttemptClaimed/AttemptStarted、SDK 每 Agent 一个 turn、Mission 恰完成一次） |
| S3-05 两个候选：先 PASS 者接受、另一个 SUPERSEDED（取消回执、费用结算、迟到结果只记历史） | PASS |
| S3-06 局部全过、整体不达标 → `mission_criteria_unmet` | PASS |
| S3-07 失去编排租约：接管同一 turn 不重跑；执行者不可见 → LOST + 新 Attempt；已完成 Task 不重跑 | PASS（07a/07b；在途 provider 调用中途丢失执行者按 S2-08 语义阻塞，见 L3-3） |
| S3-08 预算不足：图拒绝说明维度；Task 预留失败 → Task FAILED、BLOCKED 不动；Mission 池耗尽不归罪 Task | PASS |
| 遗留 | §5 L3-1～L3-6 |
