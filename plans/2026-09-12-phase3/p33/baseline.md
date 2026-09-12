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

## 本机全量暴露的问题

`fdc9c91` 干净提交的首次全量：**632 passed / 3 failed / 8 skipped，493.62 s**，原始日志 `orchestrator-full.log`。失败未忽略：

1. step09 结构断言仍匹配旧的 `template_for(role_for_task(...))`。改为核对统一的 Mission 选择入口及冻结 policy 传参；结构检查与既有实际 runtime 选版控制 **2 passed / 1.35 s**。
2. P32 沙箱内 pytest 的配置发现向上读到仓库 `pyproject.toml`，触发权限拒绝。在实际执行器里限定配置搜索边界，仍支持工作区自己的五种 pytest 8 配置、优先级与错误语义；固定 rootdir/confcutdir，不扩大 sandbox 权限。
3. step03 vanished-executor 测试假定 after_submit 后原 B 没消费脚本，实际 SDK 关闭期间可以推进队列。原 B 消费了 write 而新 B 只剩后续步骤，所以 CAS 忠实登记 stub。为该 case 增加 provider hold 与零调用断言；保留 LOST→COMPLETED、A 不重跑和最终调用次数，生产恢复代码不改。

后两条在隔离导入 `a4aae8c` 原源码时均复现（`baseline-red-check.log`，**2 failed / 1.87 s**）；不冒称本轮引入，也不把它们塞进原机“整仓 73”红集。修复的 16 条配置场景加这两条原失败：**18 passed / 7.01 s**，`regression-repair.log`。仍需修复后干净提交的编排全量。

## 切片 B 开工基线与 oracle

起点 SDK `9a4d986`（A 源码全量 `1eaa91f`：651 passed / 8 skipped；A 文档提交后 P33 74 passed）。
B 主代理负责 CAS 来源的运行时冻结、工作区保护和集成；Kepler 负责来源命令/存储/审批；Ohm 负责 citation 契约/确定性解析。
共享 main 分文件工作，无 worktree、无 stash；仅主代理串行 pytest。oracle 先写入各 `test_p33_source*.py`、`test_p33_citations.py`、`test_p33_evidence_resolver.py`。

- 来源命令：Host/人身份和租户隔离；同键重放、冲突拒绝；更替/撤销走原审批事务；旧 head/revision 与 CAS 同时校验；保留历史、ABA 不冒用旧授权，三个来源事件回放与快照相等。
- Resolver：唯一入口按登记版本读取 CAS，七种失败码按固定优先级；越权/不存在/根外逐字段同一 not_found；全文唯一、NFC 空白折叠、完整句/段落/列表/表格/标题，系统收紧坐标并保留完整块索引。
- Runtime：新 Attempt 冻结当前来源 map；旧 Attempt/重启不变；审批改版本后仅新任务看到新版；撤销后新 repair 保留草稿但不得继承旧来源。
- 来源根只读，包括新文件和大小写别名；真实 SDK 工具拒写；绕工具改写并报 artifact 在 collection 拒绝，解析仍读取 CAS。ACTIVE 重绑定不得抹掉篡改证据。
- 字节保护不经 512KB 文本工具限额或换行转换；旧 code-v1 不加来源字段、不改提示/上下文/准则语义。新 citation 会令旧严格 SDK 拒绝，契约 schema 明确升至 2。

原始证据在 `.local-test-evidence/2026-09-12/p33-b-resume/`，未入 Git。初始开发失败与误红均保留：workspace 4 fail/1 pass→5 pass；冻结 map 1 fail→1 pass；目录 prefix 2 fail/1 pass，首修大小写仍失败，最终 3 pass；撤销继承与 ACTIVE 重绑定各 1 fail→1 pass；trust work/verify 2 fail→green。来源命令首次 35 pass；contract/resolver/runtime/workspace 首批 88 pass。
结果 evidence gate 首次 14 fail 为 fixture 缺 allowed_tools，不冒称产品缺陷；修 fixture 后与 runtime 合计 29 pass。阶段性 P33 核心 218 pass/5.44s，仍不是 B 最终全量或真实 Provider/原生验收。
