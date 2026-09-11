# 第 5 步验收标准（MUST 9 条 = ORCH-BUILD §7.4 S5-01～09）

| ID | 场景 | 必须观察到的结果 | 判定 | ORIGINAL-30 |
|---|---|---|---|---|
| S5-01 | Worker 提出必要子任务 | Worker 的 `proposed_tasks` 只保存在结果/事件里，正式图不变；只有 Manager 的 `<graph_change_proposal>` 经 `commit_graph_change` 校验后才新增任务（`TaskGraphChanged` v1→v2，`TaskCommitted(source=change)`，新任务 `parent_task_ids`/`basis.result_id` 指向来源）；Worker 无任何改图工具 | `test_manager_decisions.py::test_s5_01`、闭环 | — |
| S5-02 | no_progress 反复出现 | 同一 Task 累计 ≥ `no_progress_limit` 后 Manager 必须换角色/拆小；仍空提案 → `stop_task(NO_PROGRESS)`、Mission FAILED(`no_progress`)；替代链 ≤ `max_supersede_chain`，任务总数不超上限（不无限分裂） | `test_manager_decisions.py::test_s5_02_*` | 30-17 |
| S5-03 | 两个 Manager 基于相同旧版本提交 | 第一个生效（v→v+1）；第二个 `stale_base`：不相交 → 自动重基后生效（回执 `rebased_from`）；相交 → 拒绝并重新决策一次；两次已生效修改都保留 | `test_graph_changes.py::test_s5_03_*` | — |
| S5-04 | 新依赖形成环 / 目标漂移 | 整份提案拒绝（`TaskGraphChangeRejected(reason=cycle|goal_drift)` 含路径/原因），`graph_version` 不变，正式图不变 | `test_graph_changes.py::test_s5_04_*` | 30-06 |
| S5-05 | 修改 B 分支 | A/D 已 COMPLETED 的任务不新建 Attempt、无新 `BudgetReserved`、provider 调用数不变；C 的依赖在位改到 B2；B CANCELLED、B2 新建；Mission 用 v2 完成 | 闭环 `test_dynamic_dag_closure.py::test_s5_05` | — |
| S5-06 | 被替代 Worker 迟到提交 | B 的在途候选被 CANCELLED 后其结果 `ResultRejected(superseded)` 只记历史；不批准当前任务；usage 导入、预留结算 | 闭环 `test_s5_06` | — |
| S5-07 | 同一建议重复发送 | 同一提案（哈希 + base）两次投递 → 同一回执、一次 `TaskGraphChanged`；同一触发结果只建一个 manager intent；proposal/result/commit 可关联（`basis.result_id`、receipt.change_id） | `test_graph_changes.py::test_s5_07`、`test_manager_decisions.py::test_s5_07` | — |
| S5-08 | 超过深度 / 单 Agent 建议数 / 预算 | 拒绝并给出维度与剩余（`TaskGraphChangeRejected(reason=depth|proposals|budget)`），拒绝反馈进入下一次 Manager 包；不绕限制 | `test_graph_changes.py::test_s5_08_*`、`test_manager_decisions.py::test_s5_08_feedback` | 30-13 |
| S5-09 | 低优先级可执行 Task 长期等待 | 并发 1 下，高优先级任务持续存在时，低优先级 READY 任务因 `waiting_age` 上升在 `aging_window` 内获得 Attempt；`allocator-v1` 打分记入事件 `AllocationDecided` | `test_allocator_priority.py::test_s5_09` | — |

附加门槛：`tests/orchestrator`（step02–05）全绿；SDK 全量红集 ⊆ 基线；schema v2 库升级 v3；安装 wheel 后跑 `tests/orchestrator` 与 `python -m agent_orchestrator demo --scenario dynamic-dag --provider fixtures --evidence-dir evidence/s5`（证据含 §14.3 七个文件 + `graph_history.json`）；真实模型（deepseek-flash）演示记录（至少"执行中改图后同一 Mission 完成、已完成任务未重跑"）；独立 review 处置；推送 origin main 且本地干净。
