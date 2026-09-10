# 第 3 步验收标准（MUST 8 条 = ORCH-BUILD §5.4 S3-01～08）

| ID | 场景 | 必须观察到的结果 | 判定 |
|---|---|---|---|
| S3-01 | A→(B‖C)→D→E 正常执行 | A 验收后 B、C **并行**在途（同时存在两个 RUNNING Attempt）；D 在 B、C 都 COMPLETED 前不创建 Attempt；E 之后 Mission 级判定通过；下游工作区含上游已接受产物且 hash 一致 | `test_static_dag_closure.py::test_s3_01` |
| S3-02 | Planner 给出 A→B→A | 整份图拒绝（`TaskGraphRejected` 含环路径），正式库无 Task；Planner 重提一次后可通过 | `test_task_graph.py`、`test_s3_02` |
| S3-03 | B 完成而 C 失败 | B 不重做（Attempt 数不变、provider 调用不变）；C 走修复 Attempt；D 在 C 修复通过前保持 BLOCKED | `test_s3_03` |
| S3-04 | 两个 Orchestrator 同时领取 | 每个 Attempt 只有一个 owner；SDK 里每 Attempt 恰一个 Agent/Turn；Mission 恰完成一次 | `test_multi_scheduler.py::test_s3_04` |
| S3-05 | 同一 Task 两个候选 | 第一个合法 PASS 被接受，另一个 SUPERSEDED（有取消回执与费用记录）；不接受第二个结果 | `test_s3_05` |
| S3-06 | 局部全过、整体不达标 | 所有 Task COMPLETED 但 Mission 级 pytest/准则不满足 → Mission FAILED `mission_criteria_unmet` | `test_s3_06` |
| S3-07 | 运行中失去编排租约 | 新 owner 接管在途 turn 不重跑；执行者不可见 → LOST + 新 Attempt；已完成 Task 不重跑；旧执行迟到结果不被接受 | `test_multi_scheduler.py::test_s3_07` |
| S3-08 | 预算不足 | Σ 子预算 > 父预算 → 图拒绝并说明维度；运行期预留失败 → Task FAILED `budget_exhausted` 可解释 | `test_task_graph.py`、`test_s3_08` |

附加门槛：累计回归（step02 + step03）全绿；SDK 全量红集 ⊆ 基线；安装 wheel 后跑 `tests/orchestrator` 与 `demo --scenario static-dag`；真实模型演示记录；独立 review 处置。
