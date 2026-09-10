# 验收标准：Slice 5 · 并发、恢复、迁移与发布

| AC | 条款 | 判据 | 地位 | 覆盖 | 脚本 |
|---|---|---|---|---|---|
| **AC1** | finalize 任意事务点异常不形成"输入已确认但结果丢失"；残留冻结窗口关闭 | 最终 CAS 之后、driver 返回之前崩溃：结果已 stage、Run 不 FAILED、重启后 committed 且 provider 只调 1 次 | 决定性 | BA31 | `test_slice5_recovery.py::test_crash_after_final_cas_never_regenerates_the_answer`；S1 `test_turn_finalize.py` 5 例 fault 注入仍绿 |
| **AC2** | 输入到达与 IDLE 转换竞争不丢唤醒 | 在 `_abandon_run_authority` 内部投递 → 仍被处理 | 必须 | BA32 | `test_input_arriving_while_the_idle_drive_releases_is_not_lost` + S2 突发用例 |
| **AC3** | 旧 lease 实例迟到不能新 handoff 或覆盖新提交 | 接管者经调和后答复；旧执行者放开后结果行仍 1 行、内容是新主人的 | 必须 | BA33 | `test_stale_lease_holder_cannot_overwrite_the_new_owner` |
| **AC4** | 不同 Turn 计数重启不撞请求 ID；同 Turn 恢复不重复预留 | 跨重启 request_id 为 provider-turn 1/2/3、invocation 3 行 | 必须 | BA34 | `test_request_ids_stay_unique_across_restarts_and_turns` + S2 `test_unknown_provider_resumes_the_same_turn…` |
| **AC5** | 多 Agent 不超模型/工具并发与队列限制 | 6 个 Agent 并发，模型在途 ≤ 2 且全部完成；队列配额（S2） | 必须 | BA35 | `test_concurrency_caps_are_enforced_fairly_across_agents` |
| **AC6** | 低成本 `session_history` 查询有可观察成本或调用限制 | 第 3 次查询触发 `react_max_tool_calls_exceeded`；effect 账本 2 行 | 必须 | BA36 | `test_history_queries_are_bounded_tool_calls` |
| **AC7** | 数据库副本迁移后旧 API/旧快照/旧 ledger 回归通过 | v9 库上跑 legacy Run → 迁移 v10 → 旧 Run/事件/快照可读、BaseAgent 可用、回执可重放、备份被篡改可检出、v7 拒绝 | 必须 | BA37 | `tests/execution/test_execution_v9_to_v10_migration.py`（4 例） |
| **AC8** | 外部工具响应丢失不盲重试；取消不宣称撤销 | 工具执行 1 次后丢响应 → UNKNOWN，reconcile 不重执行，Host 调和后继续；取消（S2） | 必须 | BA39 | `test_lost_tool_response_is_never_blindly_retried` + S2 `test_cancel_does_not_claim_to_undo_real_effects` |
| AC9 | 源码测试与 exact-wheel 测试分别留证 | 0.8.0 wheel sha256 + 干净 venv 安装后套件通过 | 次要 | BA40 | journal §4 |

非功能：`start_snapshot.py`/`start_mode.py` sha 不变；`react_loop.py`/`react_checkpoint.py` 仅新增可选 companion 参数（legacy 路径不传）；legacy ReAct 集成测试仍绿；回归 ⊆ 基线；mypy 0；独立 review。
