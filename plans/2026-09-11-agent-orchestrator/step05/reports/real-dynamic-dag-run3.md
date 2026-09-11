# 第 5 步 · 真实模型运行 3（DeepSeek deepseek-flash，unpriced 记账；HEAD 698e6fa）

- 时间：2026-09-11 11:55–11:58；用例同运行 2（目标不预先揭示歧义），`default_max_output_tokens=8192`、`max_output_tokens_ceiling=32768`；耗时 184.20 s；结果 **Mission COMPLETED（verification_passed），graph_version 1 → 2**。
- 这是附加门槛要求的真实证据：**执行中由真实 Manager 改图，同一 Mission 在新图上完成**。
  1. task-1（分析）的 Worker 返回 **blocked**：测试断言能确定 `parse_line` 的签名/返回结构，但"完整格式说明"依赖 spec 明示"尚未确认"的时间戳格式与未定义的边界语义；按"不猜测"返回 blocked，并提出两个"在 spec/INPUT.md 中补定义"的前置任务。
  2. `OutcomeRecorded` → `ManagementRequested(outcome:result-a6735070a19ea555)` → 真实 Manager（manager-v1）产出可通过整体校验的 `<graph_change_proposal>`：**没有采纳** Worker 提出的"改 spec"两个子任务，理由是通过标准由 `tests/test_recorder.py` 直接判定、改 spec 会引入测试未要求的判定依据；改为 `add_task task-1b-narrow-analysis`（角色 simplifier，预算 60000，`parent_task_ids=[task-1]`）+ `supersede_task task-1` + `retarget_dependencies task-2 → [新任务]`、`task-4 → [新任务, task-2]`。
  3. Commit 一个事务应用（`TaskDependenciesRewritten` ×2 → `TaskSuperseded` → `TaskGraphChanged 1→2` → `ManagementDecided(changed)`），task-1 ACTIVE → CANCELLED（`replaced_by=task-6`），旧任务无已接受产物（`old_work.artifacts=[]`）。
  4. 新任务 task-6 第一次 Attempt 信封无效（`envelope_invalid` → RETRY_WAIT），第二次通过；随后 task-2 / task-4 / task-3 / task-5 各一次 Attempt 完成；`MissionSuccessJudged` → `MissionCompleted`。
- "已完成任务未重跑"在本次运行里是空真（改图发生在第一个任务上，改图前没有已完成任务）；该性质由 fixtures 闭环 `test_dynamic_dag_closure.py::test_s5_05_and_s5_06_*` 判定，真实运行 3 的证据是 task-2…task-5 各只有 1 个 Attempt、没有任何任务被重新创建 Attempt。
- 观察：flash 在 Manager 角色上一次给出了合法提案（运行 2 的空响应问题由 `max_output_tokens_ceiling` 放宽解决）；Manager 的 rationale 质量高于 Worker 的 proposed_tasks（后者想改输入性 spec）。

- 任务（6 个；task-1 被 task-6 替代）：
  - task-1：kind=work status=CANCELLED deps=0 attempts=1（blocked → superseded）
  - task-6：kind=work status=COMPLETED deps=0 attempts=2 role=simplifier supersedes=task-1
  - task-2：kind=work status=COMPLETED deps=[task-6] attempts=1
  - task-4：kind=work status=COMPLETED deps=[task-6, task-2] attempts=1
  - task-3：kind=work status=COMPLETED deps=[task-2] attempts=1
  - task-5：kind=work status=COMPLETED deps=[task-2, task-3, task-4] attempts=1
- Attempt：
  - task-1:attempt-1：RETRY_WAIT role=worker failure=outcome_blocked
  - task-6:attempt-1：RETRY_WAIT role=simplifier failure=envelope_invalid
  - task-6:attempt-2：COMPLETED role=simplifier
  - task-2 / task-3 / task-4 / task-5 各 attempt-1：COMPLETED role=worker
- 结算 tokens 合计：263894（unpriced；Mission 账户 attempts_created=7）
- 事件计数（节选）：OutcomeRecorded 1 / ManagementRequested 1 / ManagementDecided 1 / TaskGraphChanged 1 / TaskSuperseded 1 / TaskDependenciesRewritten 2 / TaskCompleted 5 / VerificationPassed 5 / KnowledgeCommitted 5 / MissionCompleted 1。
- 证据目录 `graph_history.json`：v1（planner）5 任务 + v2（change，basis=blocked 结果，old_work 含 task-1）6 任务；`lineage.json`、`costs.json`、`events.jsonl` 齐全；证据里不含任何密钥（grep `sk-[A-Za-z0-9_-]{20,}` = 0）。
