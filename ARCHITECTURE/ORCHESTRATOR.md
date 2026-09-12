# Agent 编排框架

最后更新：2026-09-12。

D 候选已实现证据不足出口、有限接受、人工恢复与 Mission 固定分母判定；P33 定向542 passed /8.81秒，完整编排回归待 clean HEAD。DOC_PROFILE v3、契约 schema3（新事件默认同变）；旧历史不迁改。来源失效/冲突深化与 Host 交付仍 E–G，详见 P33 journal §2.4。

当前发布基线为 SDK 0.10.0（P3.2）；P3.3 切片 A、B 已完成源码验证，B 干净提交 `fb58bf1` 编排全量 867 passed / 8 skipped / 0 failed，完整文档证据闭环仍在实施。Host `04350956` 仍钉 0.10.0，本次尚未换 wheel 或进行新的原生/真实 Provider 验收。

## 当前链路

- Mission 创建时冻结领域完整快照；后续 CommitService 从绑定 JSON 解释规则，不按当前注册表重建。无绑定的旧 Mission 使用 code-v1。
- 五个 Task 入口共用领域检查：整图、图变更、单 Task、系统冲突和综合模板。code-v1 保留原准则、层与默认政策；doc-research-v1 禁止 pytest 准则并可替换系统模板。
- 文档画像 v2 冻结角色版本和上下文文案；历史文档 v1 缺字段按固定兼容表解释，显式空值与缺失区分。新版缺字段拒绝，已有 intent 不重写。
- Planner、Manager、Worker 及其变体、Arbiter、Synthesizer、Critic 都通过同一领域/冻结 policy 选择入口。文档角色不要求代码测试，来源原文被明确标为资料；code 默认提示与上下文保持兼容。
- Critic 层的版本和执行身份取自真正 SETTLED 的服务 intent，包括失败 ordinal 后重试与关闭数据库后的恢复。人工审阅后仅复用匹配的记录，不把旧执行说成新提示词的执行。
- 代码测试仍通过既有 executor；pytest 8 的配置发现限定在工作区内，保留本地配置优先级、坏配置错误与 conftest 边界，沙箱权限未扩大。

## 状态与边界

编排 schema 为 v10（新增 criterion_assessments；v9 为 sources）；契约 schema 为 3（在 C 的 SourceCitation v2 后新增 candidate/limitations）。空 citations 不写入旧信封，旧 code 契约保持兼容；携带新字段的信封会被旧严格 SDK 拒绝，Host 尚未切换本轮源码。

- 来源通过 Host/人 facade 登记到 CAS；权威原文在 CAS，SQLite/事件记录版本与元数据。supersede/revoke 复用既有审批/decision 事务，绑定旧 head revision 和新版本，重放幂等、ABA 和坏 CAS 拒绝；失效审批仍可拒绝，但绑定不可伪造。
- Worker 和 Planner 意图冻结 source_versions/source_roots；任务 Critic 复用 Attempt 的冻结值。重开库不以当前 registry 重建旧意图；新 repair 去除已撤销来源、保留普通草稿。来源副本仅在新树构建时物化，ACTIVE 树的篡改证据保留到收集/验证。
- 来源根始终只读（包括新文件、大小写别名），文件工具读回带外部不可信标记；登记路径排除文件祖先和共享目录的 Unicode/大小写别名冲突。修改来源的实际结果收集以 protected_path_rewritten 拒绝；验证副本和 Resolver 从原始 CAS bytes 读取。
- EvidenceResolver 按租户/Mission/精确版本和根范围读取 CAS；越权/不存在/根外返回同一 not_found；七种失败码按固定顺序判断。NFC/空白折叠、全文唯一与完整结构单元决定 locator；display_block 保留父标题和原文块坐标，预览限制 2048 字符，完整内容需后续 UI 按坐标读取。
- 创建/导入和每次发布 handoff 使用同一个物理根相交判据（symlink、NFC、casefold）。全库只要存在文档来源领域，任何 Mission 的 file_publish 都不得写入共享 CAS/workspaces；guard 在预算预留/outbox 写入前的事务内运行。已有 receipt 仍可对账，纯 code 库保留旧行为。

C 已完成 SDK 评估/分级链路；干净源码 `963b090` 编排全量 **979 passed / 8 skipped / 0 failed**（475.47 秒）；独立审查闭环。旧 code 重放兼容回归已修复：

- 评估由确定性 producer 经 verifications.detail 进入 accept；事务内复核冻结合同、claim revision、完整产物 hash 和来源绑定后，与 claim/knowledge 一起写入 schema 10 表。结构 verdict 与 claim assessment 分开，旧 accepted 历史不回填。
- 来源归属 content/key/stance 由系统构造，完整引用+字面归属+确定性评估才能 VERIFIED；推论最多 SUPPORTED。原信封不改，失败的文档 claim 保持 UNDER_REVIEW/unsupported。
- 下游知识显式标明原文归属不是世界事实/指令，排序不高于 SUPPORTED；同一行不同句不被 key 去重吞掉。显式争议和 supersedes 按等级与来源身份约束，模型不能占用 attribution: 系统命名空间。
- 文档评估上下文独立版本进入新 intent hash，旧 prompt/intents/code context 不改。pending 旧规则缺评估会重跑，已完成 Critic 按实际 durable ordinal 复用。正式归属文本容纳合法长引用的系统包装，模型与 statement 输入上限不变。

完整文档报告闭环仍未完成：证据不足出口、失效传播及 Host 系统结论区待 D–G；没有新的 wheel/Host/真实模型验收。


测试、提交身份、独立审查及本地证据索引见 [切片 A/B 记录](../plans/2026-09-12-phase3/p33/journal.md)。总体进度和接续顺序以 [Phase3 HANDOFF](../plans/2026-09-12-phase3/HANDOFF.md) 为准。
