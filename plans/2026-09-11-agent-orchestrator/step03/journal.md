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
（回填）

## 4. 证据
（回填）

## 5. 遗留
（回填）

## 6. 终态
（回填）
