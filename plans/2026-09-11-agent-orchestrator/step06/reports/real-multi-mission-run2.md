# 第 6 步 · 真实多执行池运行 2（DeepSeek deepseek-flash ×2 池，unpriced 记账；HEAD a2ce656，代码 review 处置之后）

- 时间：2026-09-11 下午；同运行 1 的命令与配置（两个执行池都是 `deepseek-flash`，`small` 跑 Worker、`large` 跑 Planner / Critic / Manager）；耗时 79.0 s；结果 **两个 Mission 都 COMPLETED（verification_passed）**，测试 1 passed。
- 本次运行验证代码 review 后加强的断言（review P1-7）：每个 Attempt 的 Agent **只存在于它自己执行池的执行库里**（另一个池的库里查无此 Agent，`only_in_own_pool = true` 共 5 个 Attempt 全部成立）；全部服务 intent（Planner、Critic）都在 `large` 池；两个 Mission 都必须完成（不再是"任一完成"）。

| Mission | 任务 | Attempt（池 → 结果） | 服务 |
|---|---|---|---|
| mission-ec3df867f69e566a（analysis.md + DOCS.md） | 2 | task-1 small → COMPLETED；task-2 small → COMPLETED | plan、critic ×2，全在 large |
| mission-c4ca4b9ed363330c（parse_kv） | 3 | task-1 / task-2 / task-3 均 small → COMPLETED | plan、critic，全在 large |

- 回显：全部 `deepseek-flash`；本次没有 Attempt 失败，所以没有升级（运行 1 已给出真实升级证据）。
- 背压：`pending_verifications` 升到高水位 2 → `BackpressureRaised`，降到 0 → `BackpressureCleared`，一次完整的升起与清除。
- 配额：Global 账户结算 163722 tokens，在途预留 0，共 5 个 Attempt。
- 密钥检查：以真实密钥值逐字节比对运行目录全部文件，并用词边界模式扫描，**命中 0**。
