# 第 8 步 · 事件覆盖探查（只读）

- 时间：2026-09-11；代码 main `8b1cf3e`（0.9.5 / 0.7.0）
- 做法：在 scratchpad 用 fixtures 跑一次 `demo --scenario static-dag`（5 个 Task 的静态 DAG，全部一次通过），统计 `events.jsonl` 里每类事件的条数与 payload 字段（`@task_id` / `@attempt_id` 表示事件信封上带了对应 id）。不写仓库、不改库。

## 1. 结果

| 事件 | 条数 | payload 字段 |
|---|---|---|
| MissionCreated | 1 | budget, goal, spec_hash |
| MissionPlanning | 1 | （无） |
| TaskCommitted | 5 | @task_id, commit_id, dependencies, key, proposal, source |
| TaskGraphCommitted | 1 | commit_id, edges, task_ids, terminal_task_id, warnings |
| MissionActivated | 1 | task_ids |
| TaskUnblocked | 4 | @task_id, dependencies, unblocked_by |
| AllocationDecided | 5 | @task_id, @attempt_id, allocator_version, parts, score, task_id, tier |
| ModelRouted | 5 | @task_id, @attempt_id, escalated_from, fallback_from, model, profile_id, reason, router_version, runtime_profile_id |
| BudgetReserved / BudgetReleased | 6 / 6 | subject_id、tokens / settled_tokens、settled_cost_micros、unpriced、settled_tool_calls |
| AttemptCreated | 5 | @task_id, @attempt_id, feedback, inputs, model, ordinal, retry_of, role |
| AttemptClaimed / AttemptStarted | 5 / 5 | owner / receipt |
| AgentCreated | 6 | agent_id, expected_turn_id, kind |
| HeartbeatReceived | 23 | lease_expires_at, liveness, owner |
| ResultSubmitted | 5 | @task_id, @attempt_id, artifacts, claims, outcome, result_id |
| VerificationStarted | 5 | result_id |
| VerificationLayerRecorded | 30 | layer, status, summary, verifier_version |
| VerificationPassed | 5 | result_id, layers, claims |
| TaskCompleted | 5 | @task_id, artifacts, result_id, superseded, unblocked |
| IntentSettled | 6 | intent_id, kind, state |
| MissionSuccessJudged | 1 | judgments, met |
| MissionCompleted | 1 | final_report, stop_reason |

## 2. 对 D8-2 投影表的含义

- 可由事件直接决定：Mission 状态与 stop_reason（MissionCreated / Activated / Completed / Failed / Cancelled）；Task 的创建、依赖、解锁、完成（含 accepted result 与 artifacts）；Attempt 的创建（角色、模型、retry_of）、领取、开始；Result 的提交与验证结论、各层状态与验证器版本；预算预留与结算；路由。
- 需要推导：Attempt 的 COMPLETED 没有独立事件，由 `VerificationPassed`（同 attempt_id）推出；被取代 / 取消的 Attempt 有 `AttemptSuperseded` / `AttemptCancelled`（本次未出现）。
- 没有事件：Task 的 READY → ACTIVE → VERIFYING 中间态（只有终态事件）。按 D8-2 只比较终态，中间态标 `not_covered` 或不列入正式状态。
- 本次场景没有覆盖：冲突、知识、动态改图、动作与审批、人工、失败与重试——需要在测试里用对应场景再核一遍。

## 3. 另外三个演示（knowledge-sharing、dynamic-dag、approval-action）新出现的事件

| 场景 | 事件与 payload 字段 |
|---|---|
| knowledge-sharing | ClaimDisputed（claim_id, contradicts, key, other_status, reason）；ConflictOpened（claim_ids, conflict_id, graph_version, key, reserve_tokens, task_id）；ConflictResolved（basis, confirmed, conflict_id, key, resolution_knowledge_id, resolved_claims, superseded）；KnowledgeCommitted ×6（key, knowledge_id, stance, supersedes, verifier）；KnowledgeUsed ×6（knowledge_id, result_id, source_attempt, source_task, version）；SynthesisGated |
| dynamic-dag | ManagementRequested / ManagementDecided；OutcomeRecorded（outcome, summary）；TaskGraphChanged（affected_task_ids, basis, cancelled, new_task_ids, operations, superseded, from/to_version …）；TaskSuperseded（reason, replaced_by）；TaskDependenciesRewritten（from, to, graph_version） |
| approval-action | ActionProposed（action_id, action_key, version, level, params_hash, artifact_hash …）；ApprovalRequested；ApprovalGranted（receipt_hash, grant_count, state）；ActionHandedOff（idempotency_key, decision_receipts, handoff）；ActionSucceeded（receipt_hash, state）；MissionCriteriaJudged |

知识、冲突、改图、动作与审批的终态都能由事件决定。

## 4. 用量事实的主体命名（归因对账用）

- static-dag 库（只读打开 `mode=ro`）：`imported_usage` 列为 usage_ref, subject_id, mission_id, input_tokens, output_tokens, cost_micros, unpriced, unknown, imported_at；主体为 `<mission>:planner:<n>` 与 `<mission>:task-<k>:attempt-<n>`（每个模型调用一行）；`budget_reservations` 主体相同。总用量 3150 tokens，全部 unpriced（cost_micros 为 null）。
- 由前几步代码可知的其他主体：Critic `<attempt>:critic:<n>`、Mission judge `<mission>:judge…`、Manager `<mission>:manager:<n>`、动作预留 `action:<action_key>`（只有预留与结算，没有模型用量）。
- 结论：按主体前缀可以把用量归到"成功路径上的 Attempt / 路径外的 Attempt / 服务调用（planner、manager、judge、task critic）"，三者之和应等于 Mission 的 `imported_usage` 总和。
