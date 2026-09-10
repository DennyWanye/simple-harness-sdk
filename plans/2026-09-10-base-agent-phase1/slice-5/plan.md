plan-status: finalized (主编排者依据 program.md S5 与 BA-v1.0 §8、§11 拆片，2026-09-10)

# Slice 5 实施计划：并发、恢复、迁移与发布

| 任务 | 内容 | 覆盖 |
|---|---|---|
| T1 BA31 | AgentTurn 结果在 ReAct 循环**最终 CAS 的同一事务**里 stage：`ReActRunInput.final_companion` → `DurableReactCheckpoint.cas(companion=)` → `uow.cas_react_checkpoint(companion=)`；driver 的 `_stage_companion` 生成与 loop 返回后**字节相同**的结果，内核后续 stage 幂等。同时内核对 base_agent Run 的 driver 异常**不再终态化**（放弃权限、输入保持 claimed、下次唤醒 finalize-first 或重驱动） | BA31 |
| T2 BA32 | 唤醒竞争：S2 的 `_pending_wakes` + 在途 drive 等待；测试在空转释放权限的瞬间投递 | BA32 |
| T3 BA33 | 过期租约执行者的迟到提交被围栏拒绝；接管者对不确定的在途调用**先调和再重发** | BA33 |
| T4 BA34 | 请求 id 跨重启单调、同 Turn 恢复不重复预留（S2 已有 + 重启用例） | BA34 |
| T5 BA35 | `AgentRuntimePorts.max_concurrent_model_calls / max_concurrent_tool_calls`：wire 与工具注册表的 FIFO 信号量 | BA35 |
| T6 BA36 | `session_history_*` 是普通工具：计入 per-turn 工具上限、落 effect 账本 | BA36 |
| T7 BA37 | `migrate_execution_to_v10`（备份优先、回执绑定备份字节、幂等重放）；`base_agent_upgrade_receipt_v1` 进 v10 DDL；根包/runtime 导出；`assemble_runtime` 对 <v10 库明确拒绝；内核扫描容忍 v9 库；旧 Run 迁移后仍可读 | BA37 |
| T8 BA39 | 工具响应丢失 → UNKNOWN 等待、不盲重试、Host 调和后继续；取消不宣称撤销（S2） | BA39 |
| T9 BA40 | 版本 0.8.0；`public-api.json` 只新增两个根导出；wheel 可复现构建并在干净 venv 里跑套件 | BA40 |
| 发布说明 | `CHANGELOG.md` 0.8.0 条目 | — |
