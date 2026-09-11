# Agent 编排框架 · 第 6 步（多 Mission、多模型、背压和隔离运行）测试归档

- 计划：`plans/2026-09-11-agent-orchestrator/step06/{plan,acceptance,journal}.md`
- 回归入口：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/orchestrator`（step02–06 累计）
- 真实模型（opt-in）：`--run-real-provider tests/orchestrator/step06/test_real_provider_multi_model.py`（`SH_MODEL=deepseek-flash`，两个执行池同为 flash）
- 演示：`python -m agent_orchestrator demo --scenario multi-mission --provider fixtures --evidence-dir evidence/s6`（每个 Mission 一份证据目录 `missions/<id>/`，含 §14.3 七个文件 + `trace.json` / `metrics.json` / `scheduler.json`，另有汇总 `multi-mission.json`）

| 验收 | 脚本 |
|---|---|
| S6-01 两个 Mission 同时推进、配额不互相挪用、资料隔离 | `test_multi_mission.py` |
| S6-02 放慢 Verifier：背压升起、只放行冲突/饥饿档与探索槽、清除后恢复（滞回） | `test_backpressure.py`、`test_backpressure_state.py` |
| S6-03 小模型失败后升级到强 profile，旧 Attempt 与费用保留，回显证明物理路由 | `test_model_router.py::test_s6_03_*` |
| S6-04 两个 Attempt 写同名文件各在自己的工作区，只有验证通过的成为正式版本；上游输入只读 | `test_workspace_isolation.py` |
| S6-05 权限四方交集、§21.1 检查顺序与审计、文本不能授权 | `test_governance.py::test_s6_05_*` |
| S6-06 服务不可用：降级到备用 profile / 有界等待，其他 Mission 继续 | `test_model_router.py::test_s6_06_*` |
| S6-07 tokens / 工具调用 / 运行时间任一维耗尽后停止新分配，账本正确 | `test_governance.py::test_s6_07_*`、`test_backpressure_state.py`（工具调用维度） |
| S6-08 重启多执行池：Attempt 只回到原执行池，另一模型不接管 | `test_model_router.py::test_s6_08_*` |
| S6-09 证据无密钥；每个 Result 可追到 prompt/model/retrieval/allocator/verifier 版本；未部署验证器阻塞 | `test_observability.py` |
| CLI `multi-mission` 证据目录 | `test_multi_mission_closure.py` |
