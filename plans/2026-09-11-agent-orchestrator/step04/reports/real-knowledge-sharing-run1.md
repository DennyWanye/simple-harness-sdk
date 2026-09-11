# 第 4 步 · 真实模型运行 1（DeepSeek deepseek-v4-pro，unpriced 记账；HEAD 986820d）

- 时间：2026-09-11；用例 tests/orchestrator/step04/test_real_provider_knowledge_sharing.py（--run-real-provider）；耗时 534.52 s；结果 **未完成（Mission 仍 ACTIVE）**
- 结论：不是编排语义失败，而是 `Orchestrator.run(max_cycles=10_000)` 把等待轮次也计入上限——真实模型一次 turn 数分钟，轮询把 10,000 轮耗尽后 `run()` 提前返回，两个探针任务的第二次 Attempt 仍在 RUNNING。修复：`max_cycles` 只计有进展的轮次（提交 `8cdb06b`），重跑见运行 2。
- Planner：第 1 次提案把 2,000,000 预算全部分给任务、未留系统预留 600,000 → `TaskGraphRejected(budget)` 并反馈 remaining=1,400,000；第 2 次提案通过（D3-2' 在真实模型上再次成立）。顺手改进：Planner 包直接给出 `budget_for_tasks`（planner-v3，`986820d`）。
- 图：读合同 → (探针 impl_a ‖ 探针 impl_b) → 汇总 → 综合任务（系统追加，依赖唯一叶子）。

  - mission-d4109010f4f81491:task-1：kind=work status=COMPLETED deps=[] attempts=1
    goal: Read contract/CONTRACT.md, tests/test_comparison.py, impls/impl_a.py, and impls/impl_b.py to extract the four contract clauses (basic, empty_input, trailing_sep
  - mission-d4109010f4f81491:task-2：kind=work status=ACTIVE deps=['mission-d4109010f4f81491:task-1'] attempts=2
    goal: Use tests/probe/contract_clauses.json to write tests/probe/test_impl_a_probe.py for impls/impl_a.py, run it with run_tests, and record per-clause pass/fail resu
  - mission-d4109010f4f81491:task-3：kind=work status=ACTIVE deps=['mission-d4109010f4f81491:task-1'] attempts=2
    goal: Use tests/probe/contract_clauses.json to write tests/probe/test_impl_b_probe.py for impls/impl_b.py, run it with run_tests, and record per-clause pass/fail resu
  - mission-d4109010f4f81491:task-4：kind=work status=BLOCKED deps=['mission-d4109010f4f81491:task-2', 'mission-d4109010f4f81491:task-3'] attempts=0
    goal: Aggregate reports/impl_a_probe_results.json and reports/impl_b_probe_results.json into comparison.json with the required per-implementation per-clause passes/fa
  - mission-d4109010f4f81491:task-5：kind=synthesis status=BLOCKED deps=['mission-d4109010f4f81491:task-4'] attempts=0
    goal: 综合各分支已验证结论，产出 comparison.json 与 COMPARISON.md 对比报告

- Attempt mission-d4109010f4f81491:task-1:attempt-1：COMPLETED role=worker failure=None 
- Attempt mission-d4109010f4f81491:task-2:attempt-1：RETRY_WAIT role=worker failure=verification_failed pytest failed: 2 failed, 3 passed in 0.02s
- Attempt mission-d4109010f4f81491:task-2:attempt-2：RUNNING role=worker failure=None 
- Attempt mission-d4109010f4f81491:task-3:attempt-1：RETRY_WAIT role=worker failure=turn_failed {"error_code": "provider_protocol_error", "error_type": "ProviderProtocolError", "source_kind": "tool_parse"}
- Attempt mission-d4109010f4f81491:task-3:attempt-2：RUNNING role=worker failure=None 

- 观察：task-2 第一次 Attempt 的探针测试按合同期望断言而非按实际行为断言（2 failed / 5），code_test FAIL 后进入修复；task-3 第一次 Attempt 因 provider 工具调用解析错误（provider_protocol_error/tool_parse）turn FAILED 后重试；两者都是既定的重试路径。
- 事件序列（去重压缩）：MissionCreated → MissionPlanning → BudgetReserved → AgentCreated → InputSubmitted → TaskGraphRejected → IntentSettled → BudgetReleased → TaskCommitted → TaskGraphCommitted → MissionActivated → AttemptCreated → AttemptClaimed → AttemptStarted → ResultSubmitted → HeartbeatReceived → VerificationStarted → VerificationLayerRecorded → VerificationPassed → TaskUnblocked → TaskCompleted → ResultRejected → VerificationFailed
- 预留/结算：planner:1 SETTLED settled=7664; planner:2 SETTLED settled=6003; task-1:attempt-1 SETTLED settled=22252; task-2:attempt-1 SETTLED settled=41287; task-2:attempt-2 RESERVED settled=None; task-3:attempt-1 SETTLED settled=9777; task-3:attempt-2 RESERVED settled=None
