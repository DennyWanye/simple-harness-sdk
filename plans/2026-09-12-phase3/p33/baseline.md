# P3.3 本机接续基线 · 2026-09-12

- 起点：SDK `a4aae8c23a2b72b9f2b07c62986fc7dd39f36cdf`；Host `04350956`。
- 原授权：用户要求继续已定稿 Phase3 计划；沿用 HANDOFF 的切片提交推送、测试先行与单 pytest 规则。
- 当前片：A 领域画像、五入口校验、冻结与角色/context 文案；实现与证据解析 B 以后分开验收。
- 执行：主代理完成角色/领域/context 链路；独立子代理仅补四入口测试（单独新增文件），另一个独立代理负责正确性评审；pytest 全由主代理串行运行。
- 完成记录沿用既有 program/journal；本片是整体 P3.3 的中间里程碑，不生成片级发布 receipt，不重开此前计划评审。

## 本机实测

源码 import 路径为本仓库 `src/agent_orchestrator`，pytest 8.4.2。实现前执行：

```sh
.venv/bin/python -m pytest tests/orchestrator/p33/test_p33_domains.py tests/orchestrator/p33/test_p33_domain_binding.py tests/orchestrator/p33/test_p33_arbitration_domain.py tests/orchestrator/step04/test_retrieval_context.py tests/orchestrator/step08/test_replay.py tests/orchestrator/step09/test_policy_binding.py -q
```

结果：**66 passed，44.83 s**。证据索引：`.local-test-evidence/2026-09-12/p33-a-resume/baseline.log`。

## 历史记录边界

原机记录的 orchestrator 全量为 603 passed / 8 skipped，整仓既有红集 73；不把它当成本机重跑结果。当前片结束时重新跑编排全量；整仓按计划放在 F。

## 当前片决定性 oracle

- 文档领域各角色没有要求伪造 pytest 证据的提示，Worker/仲裁/综合的实际 dispatch 工具集合不含 run_tests。
- Planner/Manager 只看到部署层与冻结领域可用层的交集及领域准则；context 在 seal 前按领域替换。
- code-v1 默认 prompt/context 字节和原策略选版语义不变。
- 有绑定则读取冻结 snapshot；注册表变化和重开库不改变它。坏 snapshot 明确拒绝，不回落当前注册表。
- doc v1 缺失的新字段按固定兼容表解释，显式空映射不当作缺失；已创建 intent 的内容不重写。
- 五个入口经真实 commit 前置验证拒绝与事务回滚；结果信封的 evidence kind 检查属于 B/C 后续链路，本片不将准则检查冒称为全部结果证据验证。

新增回归 oracle 先于实现：`test_p33_domain_prompts.py` 初跑 16 failed / 1 passed；四入口回归在 `test_p33_remaining_domain_gates.py`，均纳入既有 pytest 树。

## 恢复审查与修复

- Ohm（独立审查）发现旧 Critic intent 被幂等复用时，记录层仍从当前模板取版本。修复后读取同一 Attempt 真正 SETTLED 的 ordinal；人工恢复校验版本与 intent id，缺失/歧义不伪造来源；旧 intent 和领域 snapshot 不重写。
- 决定性控制实际关闭/重开 SQLite 与 SDK，覆盖首个 ordinal 成功、首个失败后第二个成功，以及人工恢复复用和错误身份拒绝。修复后 **2 passed / 0.95 s**，日志 `critic-recovery.log`。首次运行因 fixture 空 claims 被规则层拒而超时（2 failed / 60.44 s），该误红保留在 `critic-review-first.log`，不冒称目标缺陷的 red 证据。
- 新版领域 snapshot 缺 role/context 字段必须拒绝；仅历史 doc-v1/code-v1 允许缺字段兼容。新增两条反例先红再绿，`snapshot-review-red.log`。
- 文档提示词派生绑定具体 legacy 注册版本，不随未来 code 默认版漂移。
- Kepler 独立累计 diff 静态审查：ACCEPT，未发现新增 P0/P1；全量测试仍由主代理执行。角色/freeze 接线与范围边界已审，不等于真实 Provider 或 App 验收。
- 类型检查：85 个源文件通过。当前源码提交后再执行 P33-36 编排全量；完整引用闭环仍属于 B–G。
