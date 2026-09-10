# 第 2 步验收标准（MUST 8 条 = ORCH-BUILD §4.4 S2-01～08）

| ID | 场景 | 必须观察到的结果 | 判定方式 |
|---|---|---|---|
| S2-01 | 正常提交代码任务（fixtures 与真实模型各一次） | 得到真实产物（工作区文件）、真实测试结果（子进程 pytest 输出）、`TaskCompleted`/`MissionCompleted` 事件、最终交付（`final_state.json` 含 accepted_artifacts 与 hash） | `test_single_task_closure.py::test_s2_01`；CLI demo 证据目录 |
| S2-02 | Worker 第一次提交有错代码 | Task 不完成；`VerificationFailed` 带失败层与原因；新 Attempt（`retry_of` 指向旧）收到反馈；修复后再次验收 PASS；两次 Attempt 费用都归 Mission | `test_s2_02` |
| S2-03 | 重发 Mission 创建 / 同一 dispatch intent 重放 / 同一结果重复接收 / 同一 commit 重放 / 同一 usage 重复导入 / 同一 artifact 重复登记 / 同一验证层重复执行 / 同一 Planner 提案重复 Commit | 不新增逻辑重复 Mission/Task/Attempt，不重复 Reserve，不重复交付；返回原回执 | `test_recovery_matrix.py::test_s2_03_*` |
| S2-04 | Agent 创建后、SDK receipt 保存前终止编排进程 | 恢复后重放同一 intent，回到同一个 agent/turn；SDK 里只有一个 Agent、一个 Turn，无第二次模型调用 | `test_s2_04` |
| S2-05 | `ResultSubmitted` 后、Verifier 前终止 | 恢复只继续验证队列；Worker 不重跑（provider 调用次数不变） | `test_s2_05` |
| S2-06 | 达到 `max_attempts` 或预算耗尽 | 停止创建 Attempt；Mission `FAILED` 且 `stop_reason` 明确；报告已完成部分（events + final_state） | `test_s2_06_*` |
| S2-07 | Worker 输出无效 JSON / 伪造 attempt_id / 引用不存在或 hash 不符的产物 | 拒绝提交，`ResultRejected` 事件保留错误；正式 Task/Claim 不变；进入修复或停止 | `test_s2_07_*` |
| S2-08 | 真实工具/模型 UNKNOWN（provider 无回执） | Attempt 保持阻塞并核对，不把无回执当成未执行；预留不释放、成本不写零 | `test_s2_08` |

附加门槛（不占 MUST）：
- 累计回归：`tests/orchestrator` 全绿；SDK 全量回归红集 ⊆ 基线。
- 安装 wheel 后在干净 venv 跑 `tests/orchestrator/step02` 与 demo。
- 真实模型演示记录（`step02/reports/real-single-task-runN.txt`），失败也记录。
- 独立 review 处置表回填 journal。
