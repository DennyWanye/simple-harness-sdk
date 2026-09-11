# 第 5 步 · 执行记录

## 1. 关键裁决

独立 review（claude-opus-5，只读；原文 `reports/plan-review-round1.md`）1 P0 / 8 P1 / 14 P2，处置落在 plan §6 / §6.1：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| R1 | P0 | BLOCKED 任务在位改依赖破坏 `ordinal ≡ 拓扑序`，产物合并方向反转/伪冲突 | D5-3'：`ancestors/merge_accepted` 改为按依赖边的拓扑序（ordinal 只平局） |
| R2 | P1 | §7.4 "C2" 改成 "C 在位改依赖" 未登记 | D5-3' 登记（纲要 §7.3 允许的细化） |
| R3 | P1 | 预算池只算已结算，在途预留被清空 | D5-2'：已结算 + 在途预留计入 |
| R4 | P1 | paused 任务让 Mission 永不终结 | D5-4'：判定排除 paused；完成时 READY 的 paused 任务 CANCELLED(not_needed) |
| R5 | P1 | "停止路线"不可达且未登记 | D5-4'：cancel 只对无依赖者任务；有 BLOCKED 依赖者用 pause |
| R6 | P1 | 变更路径缺兄弟 outputs 冲突检查 | D5-2'：`validate_change` 做 D3-7' 检查 |
| R7 | P1 | 拒绝反馈无触发路径 | D5-10'：任何拒绝 → `:retry-1` 带 rejections 再请求一次 |
| R8 | P1 | `_open_conflict` 是 graph_version 第二写入者 | D5-10'：冲突开启也写 `graph_changes` 行 |
| R9 | P1 | §29.3 尺度未定义、抹掉冲突任务优先级 | D5-8'：importance = priority / max；conflict 资格优先 |
| R10 | P2 | 多操作顺序/互斥未定义 | 实现：add → retarget → supersede 的依赖者改指向 → 优先级/暂停/角色 → 取消 → unblock；同一任务在一份提案里只能出现一种结构操作（校验） |
| R11 | P2 | 漂移规则文本与落地不一致 | plan D5-2 改为"被依赖 ∨ 替代 ∨ 细化（parent_task_ids）"；综合任务依赖永不改 |
| R12 | P2 | 缺实施约定登记节 | plan §6.1 |
| R13 | P2 | 缺关闭开关 | D5-15 `dynamic_graph` |
| R14 | P2 | `max_manager_rounds`/`MANAGEMENT_EXHAUSTED` 未进决定表 | D5-7' |
| R15 | P2 | blocked/proposed_subtasks 绕过有限次数 | D5-7'：空提案受 `no_progress_limit` 约束；rounds 上限兜底 |
| R16 | P2 | 非 candidate 的 claims 归宿 | §6.1：REJECTED 历史 |
| R17 | P2 | `stop_task` 语义 | §6.1 |
| R18 | P2 | 被替代 turn 未主动撤销 | §6.1：`_release_attempt(cancel=True)` 协作取消 + unbind |
| R19 | P2 | 初始图无深度限制 | D5-2'：`MAX_GRAPH_DEPTH` |
| R20 | P2 | 去重签名混用 key/id | 保留（key 短、id 长，不会碰撞；登记） |
| R21 | P2 | 变更后重跑 `_unblock` 未写明 | plan D5-3 已含；实现即如此 |
| R22 | P2 | §7.2 两项能力降级未登记 | §6.1（Best-of-N 由 candidates_per_task 承担；角色配比常量登记不调度） |
| R23 | P2 | 演示只覆盖新图 | §6.1：`graph_history.json` 含旧产物 |

## 2. 执行记录

| 切片 | 提交 | 内容 | 测试 |
|---|---|---|---|
| A 图变更 | `7981109` | `graph/changes.py`（提案模型/词汇/整体校验）、`commit_graph_change`（CAS、重基、幂等回执、替代/在位改写/暂停/取消/角色、`graph_changes` 表 schema v3）、判定/前沿/终结任务对 CANCELLED 的处理、`ready_at` | `test_graph_changes.py` 8 |
| B 触发与 Manager | `9c13d6a` | 非 candidate 结果 `record_outcome_result`（历史、RETRY_WAIT、claims REJECTED）→ `_request_management`（kind=manager 的 service intent，按 trigger 去重，包 = 触发/反馈/受影响子图/限制/知识/拒绝反馈）；`MANAGER` 模板与 `<graph_change_proposal>` 解析（basis 由系统填）；管理门控；任何拒绝 `:retry-1` 一次；`no_progress_limit` → `NO_PROGRESS`，`max_manager_rounds` → `MANAGEMENT_EXHAUSTED`；§29.2 角色变体与 `role_for_task`；`dynamic_graph` 开关；R1/R3/R4/R6/R8/R10/R19 处置 | `test_manager_decisions.py` 5 |
| C Allocator | `46d47d7` | §29.3 公式原权重、尺度版本化 `allocator-v1`、资格检查先行、冲突任务先、等待满窗口的饥饿保护、打分冻结进 intent | `test_allocator_priority.py` 5 |
| D 闭环与演示 | 本提交 | `observability/graph_history.py` + 证据 `graph_history.json`；CLI `demo --scenario dynamic-dag`；S5-05/S5-06 闭环（候选 2、被替代候选迟到提交）；真实模型 opt-in 测试 | `test_dynamic_dag_closure.py` 2、`test_real_provider_dynamic_dag.py`（opt-in） |

