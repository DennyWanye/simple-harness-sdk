# Agent 编排框架

最后更新：2026-09-12。

当前发布基线为 SDK 0.10.0（P3.2）；P3.3 切片 A 的源码已补齐，完整文档证据闭环仍在实施。Host `04350956` 仍钉 0.10.0，本次尚未换 wheel 或进行新的原生/真实 Provider 验收。

## 当前链路

- Mission 创建时冻结领域完整快照；后续 CommitService 从绑定 JSON 解释规则，不按当前注册表重建。无绑定的旧 Mission 使用 code-v1。
- 五个 Task 入口共用领域检查：整图、图变更、单 Task、系统冲突和综合模板。code-v1 保留原准则、层与默认政策；doc-research-v1 禁止 pytest 准则并可替换系统模板。
- 文档画像 v2 冻结角色版本和上下文文案；历史文档 v1 缺字段按固定兼容表解释，显式空值与缺失区分。新版缺字段拒绝，已有 intent 不重写。
- Planner、Manager、Worker 及其变体、Arbiter、Synthesizer、Critic 都通过同一领域/冻结 policy 选择入口。文档角色不要求代码测试，来源原文被明确标为资料；code 默认提示与上下文保持兼容。
- Critic 层的版本和执行身份取自真正 SETTLED 的服务 intent，包括失败 ordinal 后重试与关闭数据库后的恢复。人工审阅后仅复用匹配的记录，不把旧执行说成新提示词的执行。
- 代码测试仍通过既有 executor；pytest 8 的配置发现限定在工作区内，保留本地配置优先级、坏配置错误与 conftest 边界，沙箱权限未扩大。

## 状态与边界

编排 schema 为 v8（mission_domains）。来源登记/schema v9、SourceCitation、解析器、准则评估、文档分级、证据不足出口、失效传播及 Host 系统结论区尚待 B–G。领域文案和 Task 准则校验不等于这些后续能力已经完成。

测试、提交身份、独立审查及本地证据索引见 [切片 A 记录](../plans/2026-09-12-phase3/p33/journal.md)。总体进度和接续顺序以 [Phase3 HANDOFF](../plans/2026-09-12-phase3/HANDOFF.md) 为准。
