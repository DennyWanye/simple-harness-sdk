# P3.1 遗留修复 · 验收（第 2 版）

| 编号 | 场景 | 什么算对 | 证据 |
|---|---|---|---|
| FX-1 | 关口有下限 | 生效预算低于 `k × (base + critic)` 的 Task 被 `validate_graph` 拒绝，其中 critic 部分只在 policy 含 critic_review 时计入。拒绝原因写明 `task_budget_below_floor`、所需下限和它的组成。<br>边界：刚好等于下限的放行；k=2 时下限翻倍；`min_task_tokens=0` 时不检查；不传 `task_floor` 时行为不变；合成模板不受下限约束 | `test_p31_fixes.py` |
| FX-2 | 图变更有下限 | `validate_change` 对新增 Task 的最终预算做同样的检查，缺省分到的份额也算在内 | 同上 |
| FX-3 | 反馈与重规划 | Mission 预算为 null。Planner 输入的 `budget_for_tasks` 带着两个下限字段；被拒后，下一次输入的 `planning_rejected` 带着拒绝原因；第二版图给足预算后 Mission 完成。<br>Manager 端到端（代码评审 P1-2）：第一次变更给新 Task E 800 tokens，被拒；两次 Manager 输入包的 `budget_floor` 都是 4096 / 10096，第二次的 `rejections` 带着拒绝原因；第二次提交合法变更后 Mission 完成。<br>接线（代码评审 P2-1）：默认配置、k=2、profile 输出上限 8192、`min_task_tokens` 为 5000 或 0，这几种情况下两个下限字段的取值都有断言；传 -1 或 True 会报错 | 同上 |
| FX-4 | 提案一直低于下限，如实失败 | Planner 每次都提议低于下限的预算（例如池只有 9000 时一律写 5000）：Mission 结束为 `planning_failed`，失败原因里有 `task_budget_below_floor`。（代码评审 P2-3 指出：原措辞"池容不下"并不是这条测试真正测到的情形，已改写。） | 同上 |
| FX-4b | 提案达到下限，却超出预算池 | Planner 提议 10096，正好是含 critic 的下限，但池只有 9000：被拒的原因是"超出 Mission 预算"，`planning_failure` 里没有 `task_budget_below_floor`，停止原因如实指向预算池。另外，规划开始前就判定"池容不下一个下限"的预检，登记为后续项 | 同上 |
| FX-5 | 产物验证状态 | 用新连接从库里读回：被接受的是 VERIFIED，被判 FAIL 的是 REJECTED，被取代的保持 UNVERIFIED。被取代的那一种，由代码评审 P1-1 的测试证明：k=2 时两个候选都交了结果，接受第一个之后，第二个候选的产物仍是 UNVERIFIED。在 `after_accept_before_supersede` 注入崩溃后，库里仍是 UNVERIFIED | 同上 |
| FX-6 | 不回退 | 全量回归红集 ⊆ 基线 73；回放与对账扫描照常通过；ruff 与 mypy 干净 | journal |
| FX-7 | 交付 | wheel 0.9.11 在干净环境验证通过；Host 改钉后 `tests/orchestration` 全部通过；Host ARCHITECTURE 写明下限、产物状态和 RETRY_WAIT 的语义；已推送。<br>说明：Host 门口已经补了默认预算，原生路径走不到 null 预算，所以 F-ORCH-1 由 SDK 测试证明 | journal；Host 记录 |
