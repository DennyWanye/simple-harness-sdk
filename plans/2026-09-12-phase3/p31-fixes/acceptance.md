# P3.1 遗留修复 · 验收（第 1 版）

| 编号 | 场景 | 什么算对 | 证据 |
|---|---|---|---|
| FX-1 | 关口有下限 | 生效预算低于 `floor_for(policy)` 的 Task 被 `validate_graph` 拒绝，原因含 `task_budget_below_floor`、所需下限与组成。其中下限等于 base，政策含 critic_review 时再加上 critic 预留。刚好等于下限的放行。`min_task_tokens=0` 时关闭下限 | `test_p31_fixes.py` |
| FX-2 | 图变更有下限 | `validate_change` 对新增 Task 的最终预算（含缺省份额）做同样的检查，不达下限抛 `GraphChangeRejected("budget")` | 同上 |
| FX-3 | 反馈与重规划 | Planner 输入的 `budget_for_tasks` 带出 `min_task_tokens`、`min_task_tokens_with_critic_review`；被拒后，下一次 Planner 输入的 `planning_rejected` 带下限原因；第二版图给足预算后，Mission 完成 | 同上 |
| FX-4 | 系统任务不受约束 | 合成任务照常追加，预算来自模板与继承 | 同上 |
| FX-5 | 产物验证状态 | 被接受结果的产物为 VERIFIED；FAIL 或被取代的结果，其产物保持 UNVERIFIED；两者都在同一事务里完成 | 同上 |
| FX-6 | 不回退 | 全量回归红集 ⊆ 基线 73；回放与对账扫描（第 8 步）照常通过；ruff 与 mypy 干净 | journal |
| FX-7 | 交付 | wheel 0.9.11 在干净环境验证；Host 改钉后 `tests/orchestration` 全部通过；Host ARCHITECTURE 写明下限与 RETRY_WAIT 的语义；推送 | journal；Host 记录 |
