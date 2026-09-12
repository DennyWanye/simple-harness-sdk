**Source checkpoint, 2026-09-13 03:12 CST:** integrated source checks:1037 PASS/2 legacy schema FAIL (48.27s); pre-schema15 reserved-attempt read compatibility fixed, targeted9 PASS/0.36s. Includes15 candidate controls, Mission system pool7, FIRST6, priced cold1 and missing-usage boundary2. COMPARE decisions and full immutable payloads now replay; frozen candidate deadline cannot dispatch new pending candidates. Controlled Host document fixture software2 PASS/6.21s proves28 formal citations and >256KiB paging; frontend search51 PASS/1.04s and typecheck pass. Fragment branch remains5 output-conflict failures (16 other controls passed); Mission-system runtime hooks, SUCCEEDED-missing-usage settlement, N1–N6/O4 and real P34/P35 gates remain OPEN. No release packaging/P3.6. This is an incomplete development checkpoint.

**Runtime checkpoint, 2026-09-13 02:50 CST:** default FIRST Critic tail reserve/consume/release is connected to actual production dispatch and passed6 controls/0.58s. Typed denial collection3/0.36s; true two-SQLite priced cold reopen1/0.40s. Scope excludes OS-kill, future Mission-level system pools and SUCCEEDED-without-usage late accounting. P34 joint run has3 FAIL/4 PASS/5 setupERROR; source inheritance defect identified and being repaired. N1 and remaining native/full audit gates remain open. See current journals; no packaging/P3.6.

**Source budget checkpoint, 2026-09-13 02:35 CST:** public Mission snapshot now carries current ledger usage in the same read transaction (39 SDK controls/5.12s); Host projection uses settled/current reserved values instead of historical Attempt totals (22 controls/8.42s; frontend49/0.941s). Provider tail/price primitives and durable typed denial:19 controls/1.00s. FIRST tail runtime wiring and native verification remain open; no release or completion claim. First-run collection/fixture failures retained in journals.

**Latest native checkpoint, 2026-09-13 02:25 CST:** N1 v5b (Host133aaa62 / SDKdfc9b7c) FAILED: sole Task90000/4 exhausted its attempts despite Mission400000/12. Both original sources were read in4 pages; no REPORT. Eight physical calls all settled, Mission86732 tokens/current reserved0. UI44494 reservation display is a confirmed projection bug; correction and typed denial stopping are in progress. N1–N6/O4 and P3.4/P3.5 remain open; no packaging or P3.6. See current Phase3 journal for immutable evidence.

# Agent 编排框架

最后更新：2026-09-13。

**Current source state, 2026-09-13 02:08 CST: P3.3 G / P3.4 / P3.5 remain in progress.** Integration run g-source-integration-v8: 989 passed, 2 outdated profile-fixture assertions failed, 40.34s (wrapper40.80s). Only the fixture was corrected: g-profile-compat-v9 passed all20 controls in0.02s (wrapper0.23s), preserving exact historical v3/v4/v5 and rejecting unknown v7. The integration includes all18 role-context and all18 provider-admission/recovery controls; the earlier cold-owner failure is closed (focused3 PASS/0.44s and integration). Changed Python Ruff and104-source-file mypy pass. Explicit unpriced profiles have shared token/slot admission, exact owner/epoch recovery, held UNKNOWN cost and actual late usage; priced admission is explicitly refused until monetary accounting is implemented. Future Critic/synthesis tail reservation, full P3.4 selection/fragment reuse, P3.5 load/backup and N1 native acceptance remain open. N1 v4b remains a real failed run; original sources, goal, criteria and400k cap are unchanged. Packaging, release and P3.6 remain paused.

## 本轮生产链路与验证边界

新显式 RuntimeProfile 通过 `context_policy`/`tokenizer` 接入 AgentRuntimePorts；同一 `workspace_read_file` 默认最多8192 Unicode字符、完整响应最多32KiB，并满足实际 `count_message(tokenizer, TOOL Message)` 的单结果上限。默认新配置单结果16384 tokens、总输入32768、render slack为0；旧None profile保留原2048-token/2000B行为。公开只读 `resolve_profile_context_policy(config, *, profile_id='default', tokenizer=None)` 解析新旧池，原子不可覆盖的 `execution.db.context.json` 和intent的 `runtime_context` 共同冻结身份。未提供counter明确使用UpperBound，不冒称flash精确计数。这不是提高用户累计预算。

source_workload 从真实CAS读取并冻结来源hash、bytes、字符/行数和读取规模，Planner只接收规模元数据、不inline正文；坏CAS拒绝。doc6新Planner/Manager v3强调连贯任务与完整提交schema；旧已冻结profile/prompt不原版改写。55项组合通过只证明这些源码边界与既有Critic/提交控制，不证明新规划在真实flash任务中成功。

`role-visibility-v1` 为新搜索角色筛选完整条目，总计最多6项/12KiB，版本进入package hash。Explorer看候选/反证，Exploiter看正式记录与原适用范围，Connector看范围/血缘，FailureAnalyst区分真实REJECTED和失败Result中的原状态Claim；doc失败保持UNDER_REVIEW，不伪造REJECTED。正式VERIFIED来源归属保留status/trust/checked_scope，不改成UNVERIFIED，也不证明世界事实。直接与继承来源合并检查，stale/unknown排除正文、ERROR显式不可用；历史反馈不重新混入来源正文。Worker/Synthesizer/Critic原路径保持，新投影仅Worker新Attempt显式启用，Simplifier声明Worker别名，旧持久intent不重建。18项包含实际SDK Provider请求的软件对照；真实模型质量、完整恢复/整体兼容和P34后续fragment/COMPARE另验。

[本轮命令、日志hash与失败分类](../plans/2026-09-12-phase3/p33/journal.md#planning-role-local-20260913)。

## 历史版本验证记录

### 9月13日前序局部验证（历史）

大页/context组合 `g-doc6-large-pages-v1` 97 passed / 0 skipped，5.88秒（wrapper6.15）；官方本地tokenizer/provider wire组合另26 passed / 0 skipped，2.51秒（wrapper2.73）。前者覆盖新8192字符/32KiB及实际tokenizer双重上限、匹配ContextPolicy和冻结恢复，后者没有真实模型调用；均不证明累计预算guard或N1业务通过。大页core后续已获Ohm限定ACCEPT。[原命令与证据](../plans/2026-09-12-phase3/p33/journal.md#大页与匹配context配置局部验证2026-09-13)。

00:40阶段源码兼容874项通过/35.70秒，94文件mypy与改动Ruff通过；更早 `g-reading-lifecycle-v2` 为39 passed /0 skipped（旧分页34＋lease5），1.15秒（wrapper1.41）。这些是对应工作树和旧2000B分页阶段的历史证据，不覆盖后续改动或N1真实重验。

### 旧2000B分页局部验证

`workspace_read_file` 已提供有界字符分页：`offset` 按 Unicode codepoint 计数，`max_chars` 为 1–4096；续读携带同一原始 bytes 的 `expected_sha256`，文件变化拒绝。分页保留原始 CRLF/Unicode，返回 `next_offset`、行边界与原始 SHA-256；小结果保持原形状。完整 ToolResult（含来源不可信提示）序列化上限为 2000 UTF-8 bytes，每页仍经过身份、权限、只读与预算检查。本次分页不变更已冻结的 doc5/promptv2 字节。

主 runner 的 `g-reading-lifecycle-v2` 为 **39 passed / 0 skipped**：分页 34 项、Critic lease 生命周期 5 项，pytest 1.15 秒、wrapper 1.41 秒。已只读核对参数数目与关键断言：真实 gateway 页重拼原文及 hash、完整表格/长行、变更文件与权限拒绝；实际 AgentContextPort 在 UpperBoundTokenizer 与 TiktokenTokenizer 下均保留可见 TOOL 页，未走 `value_preview`，journal 无替代全文记录。此为脚本 Provider 的软件链路证据，不是模型质量验证。

命令：`.venv/bin/python -m pytest tests/orchestrator/p33/test_g_workspace_paging.py tests/orchestrator/p33/test_g_critic_lease_lifecycle.py -q`。运行基于 `a5c8fca659be8b491d4d0f3f3f5536a5e711ce48` 上的工作树（`working_tree=true`），不是该干净提交或旧安装包的验证。[原始日志](../.local-test-evidence/2026-09-12/p33-g/g-reading-lifecycle-v2.log) SHA-256：`caaa5bdb0fd9a6f22b214c2ea432b8a7dcaaf06928d48b46a1725470207a92d8`；[runner 记录](../.local-test-evidence/2026-09-12/p33-g/g-reading-lifecycle-v2.json)保存调用参数与工作树 diff hash。原始证据仅本机 ignored 保存。

### 更早切片与制品记录


G SDK源码验证里程碑（21:18 CST）：干净提交a5c8fca659be8b491d4d0f3f3f5536a5e711ce48完整编排1302 passed /8 skipped /0 failed，487.75秒（runner488.09秒），PG50040已查无残留。8项真实Provider未启用；G整体未完成。0.11.1可复现候选wheel49137655…、306包文件与709个sdist源码输入逐字匹配；Host安装组合/原生flash继续验收。

G进行中（21:09 CST）：SDK默认文档画像v4、原子创建、历史引用全文分页、Mission判定树恢复和每次发布前来源复查已实现；两个SDK范围独立审查均限定ACCEPT。串行定向744 passed /17.44秒，非完整回归。Host后端/UI已实现但尚未安装新wheel验收；前端86 passed、typecheck通过。0.11.1只是候选版本，完整编排、制品、原生deepseek-flash及46项最终审计仍待做。

F 验证完成（保留既有红集）：干净源码 `5bcca08fe666b8e20524206b76ce2afbba63db4d` 整仓3241 passed /60 failed /18 errors /13 skipped（547.16秒）；与同依赖旧源码a4aae8c的78项红集按kind+nodeid完全相同，新增0。0.11.0安装验证1402 passed /11 skipped /1既有迁移失败（506.99秒）；304包文件逐字匹配，258实际加载模块均来自安装包且哈希一致。F不是整仓全绿或新正式发布；G、Host与真实flash仍未完成。

E 干净源码 `cf40b8ec86a2f307d0f8b8f89cf7f0166e5de121` 完整编排 **1199 passed /8 skipped /0 failed**（481.36秒），watchdog481.63秒，PG25036无残留。8项skip为未启用真实Provider。A–E完成SDK源码验证；F/G、wheel、Host与真实flash仍未完成。

D 干净源码 `d3d3fd8650acc8b837dc4c0e093ab95068d054ff` 完整编排 **1119 passed / 8 skipped / 0 failed**（446.64秒），watchdog446.86秒，PG18131无残留。8项skip为未启用真实Provider。D为SDK源码里程碑，E–G、Host/wheel/真实flash仍未完成。
DOC_PROFILE v3、契约schema3；旧历史不迁改。当前生产链路新增有限接受、人工恢复、预算前限额与Mission固定分母确定性判定。见P33 journal §2.4。

当前发布基线为 SDK 0.10.0（P3.2）；P3.3 切片 A–D 已完成源码验证，D 干净提交 `d3d3fd8` 编排全量 1119 passed / 8 skipped / 0 failed，完整文档证据闭环仍在实施。Host `04350956` 仍钉 0.10.0，本次尚未换 wheel 或进行新的原生/真实 Provider 验收。

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

完整文档报告闭环仍未完成：失效传播与冲突人工裁决已由E完成SDK验证，Host系统结论区及判定树挂源待G；没有新的 wheel/Host/真实模型验收。


测试、提交身份、独立审查及本地证据索引见 [切片 A/B 记录](../plans/2026-09-12-phase3/p33/journal.md)。总体进度和接续顺序以 [Phase3 HANDOFF](../plans/2026-09-12-phase3/HANDOFF.md) 为准。

## E 当前生产链路

- 新文档接受在同事务检查实际直接引用的当前来源；撤销/替换形成 stale_source，存储故障 ERROR，历史确定性 PASS 和已接受结果保留。
- Knowledge source_versions 递归合并真实引用与 used_knowledge，保留同路径多版本；旧记录按实际已接受评估推导，unknown/cycle/坏来源不能洗成空依据。
- KnowledgeIndex.stale 独立于旧 check；检索排序前排除失效知识，当前摘要隐藏相关历史结论且保留诊断，不回写历史。
- 文档冲突侧注明来源版本和评估范围；人工请求绑定真实非人工检查、产物、当前全部成员与版本。第三成员使旧请求失效；contextual/keep/unresolved 不提升知识等级。
- 硬检查失败不进入预算耗尽的旧仲裁捷径；旧版普通审核批准恢复时重入新仲裁，旧拒绝仍有效，code 行为保留。
