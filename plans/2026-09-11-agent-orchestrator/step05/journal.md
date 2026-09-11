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


## 3. 代码 review 处置

独立 review（claude-opus-5，只读；原文 `reports/code-review-round1.md`）1 P0 / 5 P1 / 14 P2；处置提交 `ccc99af`，决定性测试 `tests/orchestrator/step05/test_step05_review_fixes.py`（10 条）：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| P0-1 | P0 | `pause_task` 作用于 ACTIVE/VERIFYING 任务 → Mission 静默悬挂 | **修复**：`validate_change` 只允许 READY/BLOCKED 暂停，执行中的路线必须 supersede 或 cancel（plan §7 D5-16）；`test_p0_1_*` |
| P1-1 | P1 | retarget 引用本提案被替代的旧 id，提交后依赖指向 CANCELLED 任务 | **修复**：提交时按 `{key→id, 旧id→替代者id}` 翻译；`test_p1_1_*` |
| P1-2 | P1 | `manager_after_failures` 无读取点；PASS+proposed_tasks 不触发管理决策 | **修复**：`_verify` 增加两处触发——accept 后 `proposed:<result>`（Mission 仍 ACTIVE 时）、FAIL 后 `failures:<task>:<n>`（任务仍可重试时；用尽 max_attempts 的由 `_decide` 停止）（D5-17）；`test_p1_2_*` 两条 |
| P1-3 | P1 | manager intent 在 commit 前 SETTLED，崩溃后决策丢失 | **修复**：与 planner 同路径——commit 成功/拒绝/keep 之后才 settle；新增故障点 `before_graph_change`；`test_p1_3_*`（崩溃→重启后同一 turn 被再收集，提案只提交一次，provider 未被再调用） |
| P1-4 | P1 | acceptance 写的 `AllocationDecided` 不存在；"在窗口内"与实现"等满窗口"不一致 | **修复**：`create_attempt` 发 `AllocationDecided`（payload = 冻结分数）；acceptance S5-09 与 plan D5-8'' 统一为"等满一个 aging_window 后提升"；`test_p1_4_*` |
| P1-5 | P1 | 真实模型证据未闭合 | **闭合**：运行 3（`reports/real-dynamic-dag-run3.md`）真实 Manager 改图 v1→v2 后同一 Mission 完成 |
| P2-1 | P2 | `affected_task_ids` 不含隐式改指向者 | **修复**：隐式改指向者并入；`unblocked` 仍为单独字段（重基判据未并入，见 §5） |
| P2-2 | P2 | `ancestors` 未跳过 CANCELLED | **修复**：显式跳过（纵深防御）；`test_p1_1_*` 断言祖先集合 |
| P2-3 | P2 | `graph_history` 历史版本用当前边 | **登记**（§6.1、§5 遗留）：版本快照的 `dependencies` 为当前边，边的变化以 `change.operations` 为准 |
| P2-4 | P2 | 冲突任务/单任务缺 `graph_version`、单任务缺 `ready_at` | **修复**：单任务路径写 `ready_at` + `context.graph_version=1`；冲突任务写当前 graph_version；`test_p2_4_*` |
| P2-5 | P2 | `is_noop()` 死代码、口径不一 | **修复**：删除死代码；判定只在 `_collect_manager`（操作级，因整体校验在 Commit 事务内） |
| P2-6 | P2 | blocked 触发的空提案不受 `no_progress_limit` 约束 | **文本修正**：D5-7' 改为"blocked/proposed 触发的空提案由 `max_manager_rounds` 兜底" |
| P2-7 | P2 | 缺省预算吃掉整个剩余池 | **修复**：缺省份额 ≤ min(剩余/新任务数, 池/max(4, 现有+新任务数))；`test_p2_7_*` |
| P2-8 | P2 | `dynamic_graph=False` 无测试 | **补测**：`test_p2_8_*` |
| P2-9 | P2 | 替代链上限只有人造 context 单测 | **补测**：`test_p2_9_*`（真实提交路径 B→B2→B3，第三次拒绝） |
| P2-10 | P2 | `terminal_task` 按 ordinal 取叶子 | **修复**：按拓扑序取最后一个非冲突叶子；`test_graph_changes.py` 期望改为 C |
| P2-11 | P2 | Allocator 资格缺"账户有剩余" | **登记**（§6.1）：预算资格下沉到 `_next_attempt`（BudgetExhausted → 停止） |
| P2-12 | P2 | §7.2 模块落点未登记 | **登记**（§6.1） |
| P2-13 | P2 | `duplication_score` 口径 | **登记**（§6.1）：尺度定义为"规范化 goal 完全相同" |
| P2-14 | P2 | journal 测试计数 | **回写**（§4） |

## 4. 证据

- **确定性测试**：`tests/orchestrator`（step02–05）137 passed / 4 skipped（真实模型 opt-in）；step05 30 条（含评审处置 10 条）。
- **SDK 全量回归**（评审处置后 HEAD `ccc99af`，脚本 scratchpad `regress/run.sh`）：58 failed / 2177 passed / 9 skipped / 15 errors，红集 73 条 = 基线，**0 新红**（处置前 `07327ef` 同为 73 条 = 基线）。
- **wheel 0.9.3（第一次构建，源提交 `07327ef`）**：`simple_harness_sdk-0.9.3-py3-none-any.whl` sha256 `7142a04573eab2ae4bc6c400508969937f34934d4ed85d15a93b857bf65781e3`；干净 venv 安装后 390 passed / 7 skipped / 1 failed（唯一失败为基线已知红 `test_execution_v3_to_v4_migration::test_completed_null_continuation_*`）；安装态 `demo --scenario dynamic-dag --provider fixtures` → COMPLETED（verification_passed，graph_version 2）。若代码 review 处置改动代码则重建并以最终 sha 为准。
- **真实模型（deepseek-flash）**：运行 1（`reports/real-dynamic-dag-run1.md`，Planner 前置消解歧义、无改图）→ 运行 2（`run2.md`，blocked 触发管理决策但 Manager 空响应，暴露 `max_output_tokens_ceiling`）→ **运行 3（`run3.md`，真实 Manager 改图 v1→v2，同一 Mission 完成）**。

## 5. 遗留

- `graph_history.json` 的历史版本快照用任务当前边（P2-3）；边的历史以 `graph_changes.operations` 为准。
- 重基判据 `touched` 未并入 `unblocked` 任务（P2-1 后半）；由"重基后对当前图整份重校验"兜住。
- 冲突任务的 `context.graph_version` 只有代码路径，无单独测试（step 4 冲突测试覆盖其创建）。
- Allocator 预算资格下沉（P2-11）、`duplication_score` 口径（P2-13）为登记项，未改实现。
