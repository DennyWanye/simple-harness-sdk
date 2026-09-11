# 第 6 步 · 真实多执行池运行 1（DeepSeek deepseek-flash ×2 池，unpriced 记账；HEAD 0948c1a + 切片 E 工作区）

- 时间：2026-09-11 下午；命令 `demo --scenario multi-mission --provider env --unpriced`（opt-in 测试 `test_real_provider_multi_model.py`），耗时 82.6 s；结果 **两个 Mission 同时运行并都 COMPLETED（verification_passed）**，测试 1 passed。
- 模型：按用户指示两个执行池都用 `deepseek-flash`（`SH_MODEL=deepseek-flash`），不使用 deepseek-v4-pro。`small` 池：默认输出 8192、上限 16384，跑 Worker；`large` 池：默认输出 8192、上限 32768，跑 Planner / Critic / Manager 与升级后的 Worker。两个池各有独立执行库（`execution-small.db` / `execution-large.db`）。
- 物理路由证据：每个 Attempt 的 `runtime_profile_id` 与其池执行库里的 provider 回显一致，回显全部为 `deepseek-flash`；两个 Planner 都在 `large` 池，全部 Critic 都在 `large` 池。

| Mission | 目标 | 任务 | Attempt（池 → 结果） |
|---|---|---|---|
| mission-8b4035e59debb052 | 输入分析 analysis.md + 文档检查 DOCS.md | 2 | task-1:attempt-1 small → COMPLETED；task-2:attempt-1 small → COMPLETED |
| mission-0267facb07937634 | 实现 parse_kv 并通过测试 | 2 | task-1:attempt-1 small → COMPLETED；task-2:attempt-1 small → **verification_failed**；task-2:attempt-2 **large**（retry_of attempt-1）→ COMPLETED |

- **§9.3 升级阶梯的真实证据**：parse_kv Mission 的 task-2 在 `small` 池第一次验证失败，路由按 `escalate: small → large` 把重试放到 `large` 池，第二次通过；旧 Attempt 保留为 RETRY_WAIT，费用在账（S6-03）。两个池模型名相同，所以这次"升级"是换执行池与输出上限，不是换模型名。
- **背压的真实证据**：`verifier_workers=1`、`max_pending_verifications=2`：`pending_verifications` 两次达到高水位 2 → `BackpressureRaised`，两次降到低水位 1 → `BackpressureCleared`（滞回），期间只放行冲突/饥饿档与探索槽（S6-02）。
- **配额**：Global 账户结算 124863 tokens，在途预留 0，共创建 5 个 Attempt；两个 Mission 各自账户只记自己的预留与结算（S6-01）。
- **密钥检查**：`pytest.txt` 与证据目录用词边界模式扫描 0 命中；以真实密钥值逐字节比对 `orchestrator.db` 等全部文件 = 未出现。粗扫描在 `orchestrator.db` 的 1213 处命中全部是 `task-…` 里的 "sk-"（前两个字符都是 "ta"），不是密钥。
- profile 健康：无 `RuntimeProfileUnavailable`（DeepSeek 端点全程可用）。
