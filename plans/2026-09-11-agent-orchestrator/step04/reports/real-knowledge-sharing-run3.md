# 第 4 步 · 真实模型运行 3（DeepSeek deepseek-v4-pro，unpriced 记账；HEAD 7303469）

- 时间：2026-09-11；用例 tests/orchestrator/step04/test_real_provider_knowledge_sharing.py（--run-real-provider）；耗时 498.64 s；结果 **Mission FAILED（max_attempts_reached，综合任务）**
- 已成立的证据（本步真实模型要求的两条中的第一条）：Planner 一次通过；task-2/task-3 各产生 4 条经 code_test 覆盖的 **VERIFIED 知识**，汇总任务 task-4 再产 8 条（共 16 条）；汇总任务 task-4 在上下文里拿到全部知识并在 used_knowledge 引用（**8 个 KnowledgeUsed**）后通过验收——「新任务真的使用了其他分支的已验证知识」在真实模型上成立。
- 未成立：综合任务两次 Attempt 的 Result Envelope 都不合规（第 1 次缺 task_id/attempt_id/outcome/summary，第 2 次 outcome 不在枚举内）→ envelope_invalid ×2 → max_attempts_reached → Mission FAILED。根因：ARBITER/SYNTHESIZER 模板只写「字段与 Worker 相同」而没有重述信封 schema；修复：两份模板逐字给出全部字段与 outcome 枚举（arbiter-v2 / synthesizer-v2），重跑见运行 4（按用户指示改用 deepseek-v4-flash）。
- 健壮性：task-1、task-3 的第一次 turn 都遇到 provider 工具调用解析错误（provider_protocol_error/tool_parse），编排层记 ResultRejected(turn_failed) 后重试成功（运行 2 的 jsonable 修复有效）。

  - mission-334a671d89622b68:task-1：kind=work status=COMPLETED deps=0 attempts=2
    goal: Read contract/CONTRACT.md, impls/impl_a.py, impls/impl_b.py, tests/test_comparison.py, and docs/vendor_notes.md. Identify the contract clauses that ma
  - mission-334a671d89622b68:task-2：kind=work status=COMPLETED deps=1 attempts=1
    goal: Using tests/probe/probe_plan.json, write tests/probe/test_impl_a_probe.py for impl_a. The probe tests must record whether each clause is supported ins
  - mission-334a671d89622b68:task-3：kind=work status=COMPLETED deps=1 attempts=2
    goal: Using tests/probe/probe_plan.json, write tests/probe/test_impl_b_probe.py for impl_b. The probe tests must record whether each clause is supported ins
  - mission-334a671d89622b68:task-4：kind=work status=COMPLETED deps=3 attempts=1
    goal: Read tests/probe/probe_plan.json, tests/probe/claims_impl_a.json, tests/probe/claims_impl_b.json, contract/CONTRACT.md, docs/vendor_notes.md, and test
  - mission-334a671d89622b68:task-5：kind=synthesis status=FAILED deps=1 attempts=2
    goal: 综合各分支已验证结论，产出 comparison.json 与 COMPARISON.md 对比报告

- Attempt mission-334a671d89622b68:task-1:attempt-1：RETRY_WAIT role=worker failure=turn_failed {'error_code': 'provider_protocol_error', 'error_type': 'ProviderProtocolError', 'source_kind': 'tool_parse'}
- Attempt mission-334a671d89622b68:task-1:attempt-2：COMPLETED role=worker failure=None 
- Attempt mission-334a671d89622b68:task-2:attempt-1：COMPLETED role=worker failure=None 
- Attempt mission-334a671d89622b68:task-3:attempt-1：RETRY_WAIT role=worker failure=envelope_invalid block_missing: no <result_envelope> block in the output
- Attempt mission-334a671d89622b68:task-3:attempt-2：COMPLETED role=worker failure=None 
- Attempt mission-334a671d89622b68:task-4:attempt-1：COMPLETED role=worker failure=None 
- Attempt mission-334a671d89622b68:task-5:attempt-1：RETRY_WAIT role=synthesizer failure=envelope_invalid result is missing required fields: ['attempt_id', 'outcome', 'summary', 'task_id']
- Attempt mission-334a671d89622b68:task-5:attempt-2：RETRY_WAIT role=synthesizer failure=envelope_invalid result.outcome is not one of ['candidate', 'blocked', 'failure', 'proposed_subtasks', 'no_progress']

- 知识：16 条 VERIFIED（key：['impl_a.basic', 'impl_a.empty_input', 'impl_a.missing_equals', 'impl_a.trailing_separator', 'impl_b.basic', 'impl_b.empty_input', 'impl_b.missing_equals', 'impl_b.trailing_separator']），KnowledgeUsed 8 次；冲突：0（两份实现各自的探针结论无同 key 反 stance）
- 结算 tokens 合计：365982
- 事件序列（去重压缩）：MissionCreated → MissionPlanning → BudgetReserved → AgentCreated → InputSubmitted → TaskCommitted → TaskGraphCommitted → MissionActivated → IntentSettled → BudgetReleased → AttemptCreated → AttemptClaimed → AttemptStarted → ResultRejected → ResultSubmitted → HeartbeatReceived → VerificationStarted → VerificationLayerRecorded → VerificationPassed → TaskUnblocked → TaskCompleted → KnowledgeCommitted → KnowledgeUsed → TaskFailed → MissionFailed
