**最后更新：2026-09-13 12:59 CST — 系统Worker原额度增长与Critic结构反馈。** 真实FIRST smoke（4ba53f4）两次Manager/片段F/独立B/修复C均完成，最终S因名义60K Worker预留无法动用原120K系统hold剩余额度而失败（330.401秒/452199tokens）；原失败保留。新逐请求增长仅从同Task原hold按差额原子转入，保留各活跃候选的冻结Critic token/费用最低额；初始分派和背压不变，UNKNOWN不增长/返还，实际已知结算仅一次返还未用额度。初版全量转移Worker room被独审指出并发/背压风险后撤回，不交付。当前预算65PASS/5.49s，含实际SDK请求、费用、兄弟候选、UNKNOWN和返还控制；独立Sol审查无确定P1/P2。Critic默认升v3，v2及文档历史字节保留；mission_criteria仅允许原Mission条件，结构错误仅向下一独立v3 service给出白名单反馈，严格解析/两次费用不变。真实COMPARE此前一次错误混入Task条件导致额外22205tokens复核；新冷热/历史模板81PASS/3.77s，Astra独审无P1/P2。测试初稿空claims触发rule_check而非Critic、超时2FAIL，以及旧canonical聚合误引用新默认模板的1FAIL均保留；修复夹具与显式旧critic-v2映射，原hash未改。Mypy117/ruff通过；新已提交态真实对照与最终全量待，整体未完成，不打包/P36/推送。

**最后更新：2026-09-13 12:48 CST — 完整原生验证背压。** snapshot-v23（SDK4ba53f4/Host5a939d6b）真实UI提交7个短来源Mission，2个实际Verifier有界等待。持久采样观察2RUNNING+2PENDING达到总上限4，BackpressureRaised seq140；首个验证转待人后再派发的Worker预留减为10000，随后总数降至低水位2，BackpressureCleared seq192，恢复20000，间隔20.222秒。UI读取Raised/Cleared历史、逐一打开83字节review.md并复核通过，7Mission全部COMPLETED/各900结算/0预留；42Provider记录/0rehandoff/105journal在人审前后不变。峰值未及时截屏，峰值与减速由同一次真实运行的持久事件/采样/预算证明；不是手动释放，两个Verifier均20秒自动解阻后运行真实Critic。进程组正常退出无残留，生命周期512.800秒。Host `.local-test-evidence/2026-09-13/p33-g/source-ui-pressure-v24/case-summary.json` SHA256 `616bb3deed0ca3c76a9212ec9c4fb67582ca120a4311c459a1d78c5743fdb5bd`。关闭P35-A03原生阈值/减速/排空缺口，保留最终回归与新发现的system Worker尾部增长问题；整体未完成，不打包/P36/推送。

**最后更新：2026-09-13 12:36 CST — 长Context原生交付与冷读通过。** snapshot-v23（SDK4ba53f4/Host5a939d6b）通过原生文件选择器导入原97,200字节来源，Mission `mission-cb2cd1cb76fc88ee` 正式交付，2550结算/0预留。12个8,100字节页面全部保留在真实SDK journal，8个Worker请求出现持久Context轮转，原instructions/user_input及完整工具组保留，实际Provider观察hash与selection逐一相符。UI打开380字节REPORT.md（SHA256 `c4f9c9eb9c57dca52dd45cf720f66092a0f401f335899a6ad69a263184b3c9d9`），首段/原约束/末段引用完整读取；冷启动读同报告和约束引用，51事件/17Provider记录/0rehandoff/37journal/17selections完全不变。原生/冷读生命周期299.810/271.067秒，两进程组正常退出无残留。受控Provider机制证据，不代表真实模型记忆质量。Host `.local-test-evidence/2026-09-13/p33-g/source-ui-context-v23/case-summary.json` SHA256 `7a0b1f773879e3f07b99f5f16d2d85a7f815e7427fd4a8c0df87e09a78fa1e0b`。关闭P35-A07当前原生轮转/冷读缺口，整体仍待完整压力与最新回归，不打包/P36/推送。

**Last updated: 2026-09-13 11:48 CST — Manager admission and late accounting.** Real DeepSeek fixed pairv2 exposed Manager authority rejection: referenced failed Attempt is historical evidence, while its service intent owns current authority. Guard now validates exact evidence Task/Mission identity without requiring that old Attempt live; own authority/lease/pool/fingerprint/budget/cancel checks remain. Initial28PASS1.73s. Related270-case sweep268PASS2FAIL2SKIP89.51s exposed a second defect: real Manager task_id was missing from older late-accounting fixture; with it present, recovery wrongly expected Task-funded account though Manager is Mission-funded. Recovery now preserves evidence Task validation but checks original Mission account for Manager. Actual hot/cold receipt import35PASS3.05s, terminal business and original invocation unchanged, no Task money charged. Parent intermediate fixture NameError and both original failures retained. Mypy117/ruffPASS. Real search test keeps all materials/root2M/A-B240K budgets and aligns output ceiling to Host8K (old32K required65536 while only60000available);8pure configPASS. Corrected paid pair and final current full suite pending. See journal; no packaging/P36/push.

**Last updated: 2026-09-13 11:35 CST.** Current source cumulative non-network1762PASS plus16journal/contextPASS, approvedCOMPARE native andcoldPASS. Corrected UI wording77PASS/nativepending. Real fixedDeepSeekpairv2 found Manager authority_rejected before outbound; botharms retainedbudgetFAIL, guarded27/12calls,210702/65800tokens. Astrahigh child fixingidentity, no additional paid reroll until deterministic verification. NativeContext/fullthresholdpressure waiting usable UI afternoWindowsAvailable; no Missionclaimed. See p33journal11:35 entry for paths/hashes/timing. Phase3 remainsOPEN; no packaging/P36/push.

**当前补充 — 2026-09-13 11:05 CST：** P34原固定FIRST/COMPARE真实deepseek-flash两臂分别283.621s/79.057s、550469/108311 tokens，均budget_exhausted；Mission总额未耗尽，子Task额度不足。账本与Provider用量一致，无重复计费证据。测试原配置没有provider token grants，不能代表Host已接入的逐请求准入。现已保持原任务/材料/2M总预算与A/B限额不变，接入同一固定官方tokenizer的Context与Provider estimator，明确记录ZERO_GRANTS/UNKNOWN/EXERCISED；纯配置7PASS/.22s、ruff通过，未重跑付费组。原两次失败完整保留：SDK .local-test-evidence/2026-09-13/p34-real-search-value-8dc3876aadb546e0baa9148a0095122e/。P34价值门仍OPEN。

**最后更新：2026-09-13 10:51 CST — 源码原生负载、独立恢复与搜索链验收。** snapshot-v17（SDK0a1a050/Hostc61744d6）：三Mission/两物理槽中第三任务真实UI取消，释放前后无实际Provider调用；官方backup/restore到独立userdata后，成功/取消/待人三状态及96事件、14 Provider记录（13succeeded/1claimed）、34journal完全相同，恢复副本真实UI复核后正式交付，调用不增、rehandoff0。B2 summary SHA256 `0d4e3c76fbbf35ddc7ccda3eb5241e71147c92e234f9814ab01db25460591efb`。原生Verifier压力v19：UI显示第三Result PENDING，UI时点持久事件对应2RUNNING+1PENDING，20秒自动释放后3任务均交付；手动marker未观察，不声称pending上限4饱和。summary SHA256 `c67edef26d8360b5feb53f8c068d05904f1a2f54e2545326976a1b911611f6c7`。FIRST原生v20：保留A三次失败，F仅核选中片段，Manager改C依赖为F+B，C实际测试通过，S读取已验证C并实际测试通过；UI打开final.md，冷启动同hash `bc04ba9b12d5ab4e0729599c2cce15ca42d715152ea84e81484f0f78ac1c73c3`、26调用/0rehandoff/62journal/192全局事件不变。summary SHA256 `7ebfacc0d9cc21f1d3989598f13b320fdc97d423fc81095f37a581e7463a3479`。均为受控Provider机制验收，不冒称真实模型质量。Host证据根 `.local-test-evidence/2026-09-13/p33-g/`，对应source-ui-b2-restored-v18b/source-ui-pressure-v19/source-ui-search-v20。只读回放replay-all-native-v2.json：27数据库/35Mission/62观察，PASS零差异/错误，.730s，无调用/效果变化，SHA256 `a2cc5a33ba08f4dfe1c1c6cb3452148d37367b15dc2f003dcf829ef23f57382b`。当前SDK826c0e1五项回归修复56PASS/20.03s，新全量待；P33累计关联/P34/P35仍OPEN。真实FIRST/COMPARE原固定pair两臂预算失败已保留，测试漏接原生精确tokenizer/逐请求准入的配置正在修正，尚未重跑。长Context原生rotation与批准COMPARE UI待。不打包/P36/推送。

**最后更新：2026-09-13 10:33 CST — 全量回归发现的恢复与预算分类修复。** 全量 g-doc9-orchestrator-full-v5 为1746 PASS/5 FAIL/12 SKIP，623.89s，未通过。缺失冻结runtime pool时，恢复跳过绑定并保留原SUBMITTED turn；必需Critic冷却时进入有界等待，Worker可路由不再重置Critic等待起点；保护尾部的Attempt额度耗尽改用BudgetExhausted(attempts)，让冲突任务按原合同转人工，避免误报runtime_unavailable。策略结构断言区分搜索角色读取与Mission绑定的模板选择。五个受影响文件组56 PASS/20.03s（runner20.32s），包含原5失败、健康Critic拒绝对照和预算四表不变检查；mypy117源文件与ruff通过。源码测试已通过，新全量与该修复的原生UI仍待；P33/P34/P35整体OPEN，不打包/P36/推送。证据在SDK .local-test-evidence/2026-09-12/p33-g/g-full-regression-five-fixes-v1.*。

**N1 original-source acceptance — 2026-09-13 10:09 CST: PASS.** Source snapshot v16 (SDK aada164 / Host ba6be341), doc9, same two original files and original 400000/12 goal/budget: Mission mission-0b12722003e0b883 COMPLETED/verification_passed in291.398s, one Worker Attempt,320365 settled/0 reserved. Actual assistant journal submitted ordinal refs; canonical Claims:11 VERIFIED source attributions,3 SUPPORTED analyses,1 UNDER_REVIEW structural statement. REPORT SHA256 `812114f5b9f1252a56d43e4ff6815961a031aeec01e62da3f751103c0c9c3e52`. Parent opened report and both-source citations in native UI, including complete HA-12 row and 99-character conditional unit; full CAS report matched displayed hash. Parent and independent Terra medium review PASS: build/startup failure records acknowledged; historical verification is not current installation evidence. Same-source cold UI reread preserved artifact/citations/status and14 Provider records (11 succeeded/3 failed),0 rehandoff,60 events and SDK journal counts. Native/cold carrier lifecycles651.361/104.222s include manual inspection, both exit0/no residual. Host evidence `.local-test-evidence/2026-09-13/p33-g/source-ui-n1-v16/case-summary.json` SHA256 `195b17df5a66ee13937410abbbe59b4e75c255a49fa6766ee75808afc1972bde`. Historical failed N1 runs retained. This closes current N1 content/native/cold gate, not P33 cumulative audit or overall P34/P35. No packaging/P36/push.

**Updated 2026-09-13 10:02 CST — queued cancellation and resumed stall timing.** Legacy calls now acquire physical slots before durable SDK handoff and recheck Mission/intent under the Orchestrator Store transaction; cancelled queued Planners cannot issue a new request. Existing usage and UNKNOWN semantics remain. Liveness reads effective admission; a persisted blocked-to-unblocked transition starts one new stall window without inventing SDK progress or renewing it on ordinary heartbeats. Actual two-slot/three-Mission cancel, accepted response, UNKNOWN, six-stall-window waiting, completion/cancel and true post-queue stall: 8 PASS/7.71s. Affected budget/lease/replay:44 PASS/32.97s; mypy117 and ruff PASS. Sol and Terra high independent limited reviews ACCEPT. Raw logs under SDK `.local-test-evidence/2026-09-12/p33-g/`: g-local-queue-liveness-v4.log SHA256 `4f05f5ebb5de92615dd68b964f93b9027ad2303e153ed47f1c6c8b30c2519b6b`; g-queue-admission-affected-v5.log SHA256 `22b45b6d736420ebc05d647b5bdd6c2a2bf61bd73523ab18aea166f871e736c0`. Earlier configuration and actual queue-exit failures remain. Native load validation is pending. Doc9 current/legacy profile follow-up37 PASS/.43s. P34 only seven material-mechanics checks/1.99s so far, not real-model value. P33/P34/P35 remain OPEN; no packaging/P36/push.

**最后更新：2026-09-13 09:54 CST — doc9 冻结序号输入。** 新文档任务默认doc9；Result提交允许criterion_refs/mission_criterion_refs按冻结Task/原Mission目录严格展开，正式Claim仍保存完整ID，原始SDK journal不改；互斥、越界、错身份、旧doc8/code、坏完整hash拒绝，alias不增加证据或放宽验证。旧doc8 canonical SHA保持。当前parser/历史profile53 PASS/1.01s（g-doc9-parser-v4，runner1.36s），先前受影响prompt/域/queue组合95 PASS/3.89s，ruff通过，doc9独立静态复审ACCEPT。原始资料N1v15(doc8)仍FAILED：首提交第18条Claim完整hash抄漏字符，重试Task预算不足；284857已结算/0预留，未接受报告仍有过度概括缺口。新增提示词要求核对开头/历史反例，但尚不证明质量修复；doc9同资料/400000/12真实UI待验。当前另行排队取消/队列退出stall修复未随本切片验收。P33/P34/P35整体OPEN，不打包/P36/推送。

**Last updated: 2026-09-13 08:51 CST - document scope review successor.**

New document Missions bind profile8: result roles use v4 guidance and Critic v3; Planner/Manager and code-domain behavior remain unchanged. Full-source negative/absence claims and old/new status records require scope-aware analysis; core contradictions should block report acceptance. This is model guidance, not deterministic truth proof. Published doc1-7 bytes, tools, source binding, citation integrity and actual Critic proof stay intact. Main84PASS2.24s includes frozen doc5-8 Worker/Critic identities across reopen and hashed doc7/old prompts; independent Astra review found no P1/P2. Native real N1v14 formally passed but manual report quality failed on an overbroad absence claim; original400000/12 recheck remains OPEN.

# Agent 编排框架 · 交接（Phase3 进行中）

**最后更新：2026-09-13 07:59 CST — 当前源码综合回归。** 综合文档/P34/P35与受影响旧恢复、Manager、路由和启动检查1147 PASS/113.92s（runner114.42s），151文件mypy通过；随后仅移除未使用import及格式化测试，ruff通过。前批1144PASS/2FAIL的文档日志体积回归已修复，未删负例。A03复合压力控制1PASS/1.61s：实际RAISED阻断fresh Worker，Arbiter使用原conflict pool的HELD额度，公开人工仲裁后独立Synthesis/Critic完成、队列最终NORMAL；知识ID仅来自实际Provider请求。Terra独审限定接受；该用例明确使用历史doc4，不证明原20k预留全部转移或S首次调用时已经NORMAL。当前doc7由Host独立场景覆盖，原生仲裁及整体Phase3剩余价值/压力UI仍OPEN；不打包/P36/推送。


## 当前接续：2026-09-13 07:40 CST

以下当前记录覆盖后文00:35等历史快照。Host本地HEAD `6ce08c14`，SDK在`d1947b5`后继续工作树开发；不能把未提交生产改动当冻结版本。用户暂停打包、发布、P3.6和推送；源码Tauri UI验收。主会话固定GPT-6 Astra/high；最多3子代理，Luna/Terra/Sol/Astra按任务复杂度选择，记录模型、缓存区分用量、耗时、返工和主验结果。

- P3.3：N1v9真实deepseek-flash原材料报告及同源冷重开通过（220.968s、242431已结算/0预留、13出站），N2长表/N4反例/N6不足与来源撤销均有源码原生记录。当前doc7仲裁软件路径14PASS/25.55s；新预算表单85PASS/1.33s、类型检查和独审通过，原生仲裁仍待验。16个已关闭原生库全部17Mission+16部署事件流只读回放0差异；不是未来运行/完整执行库回放通过。
- P3.4：角色Context、候选比较、局部片段路径已实现并局部验收。自动Manager跨分支正向已跑通；独审发现合法重试绑定、准则全覆盖、依赖身份3项P1，Sol修复中，Astra独审。不宣称完整动态搜索价值/原生门关闭。
- P3.5：FIRST/原价账务、跨profile物理容量、队列跨租约及原Mission期限、验证容量背压、真实OSkill、孤立进程清理、长Context冷恢复、多库备份已有定向证据。最新外部动作cold+backup1PASS/8.35s。Context prewarm修复定向2PASS/6.29s、相关46PASS/12.27s。压力+仲裁优先级复合oracle和源码UI取消/在途结算仍待完成。
- 最新综合v20为1133PASS/2FAIL/96.74s；两个失败已定向修正，后继新增修复尚未跑最终全量，不将旧总数当当前全绿。
- 原始证据均在两个仓库ignored `.local-test-evidence`；新证据不得入Git。主唯一测试/UI/provider runner，子代理不运行测试；防熄屏仍运行。先完成跨分支P1和受影响回归，提交/冻结源码，再原生仲裁及剩余价值/压力门；不清理掉当前工作。


- 日期：2026-09-13
- 上一份交接（`plans/2026-09-11-agent-orchestrator/HANDOFF.md`，停在第 2–9 步收官）已删除，本文件取代它。
- 仓库与分支：
  - SDK `simple-harness-sdk`，`main`（本文件所在仓库）
  - Host `simple_harness`，`main` = `e690bdcf`；源码后端/UI及显式SDK源码身份已提交，本机G功能仍在验收。远程是否已推送须实际核对。
- 长期规则：专业术语先查 Host `plans/taskSys2/agent-orchestration-theory/` 的定义再写代码；真实模型只用 `deepseek-flash`（**绝不**用 deepseek-v4-pro）；记录与回复一律中文；技术取舍交独立评审子代理裁决并记录；测试先行；回归红集 ⊆ 基线 73；每切片提交推送并同步更新本文件；不用 `git stash`；同一时间只跑一个 pytest。
- 安全：绝不打印或提交 API 密钥。本机真实测试从 Host 主仓 ignored `.env` 的 `DEEPSEEKER_APIKEY` 注入进程；该字段已确认存在。旧机 `.local-test-evidence/2026-09-07/credentials/deepseek.env` 本机不存在，不要据此判断无 key。模型固定 `deepseek-flash`，endpoint 按真实测试配置核实，密钥不复制到配置或证据。提交前扫 `\bsk-[A-Za-z0-9_-]{20,}` **只打印计数**。

---

**当前决定**：继续完成P3.1–P3.5功能；P3.6不在本次范围，暂停打包/发布，直接源码Tauri UI验收。主防熄屏进程仍在。实际真实模型固定deepseek-flash，不把受控Provider或源码载体当安装包通过。

**2026-09-13 00:35 CST接续**：P3.3 G仍未完成。Host e690bdcf；SDK基于a5c8fca的未提交修复使用显式editable-source加载，原0.11.1 wheel保留，不重新构建。新doc profile5/角色prompt v2强制真实Critic审阅并绑定实际SDK输出proof，旧doc3/4与v1 prompt保留。N1两次真实任务均未通过：v2因Claim字段错误和Task预算耗尽FAIL；v3因长文件只给Context预览无法读全，主在UI取消，现场保留。v3还暴露Critic每秒约20条心跳、取消/120秒外层超时后孤立调用等缺口。

分页工具（原文hash绑定、Unicode偏移、保留CRLF、每页权限与完整ToolResult<=2000bytes）、续租同事务终态检查/半期节流、Critic取消后结果费用收集已实现。首批39项通过/1.15秒；全P33加旧workspace/recovery矩阵869项通过/34.79秒；94源码文件mypy通过。最后发现的AGENT_CREATED丢submit回执取消窗口及超时控制正补独立冷恢复测试，因此869不能替代该后续增量。通过后重新冻结SDK身份，继续N1原始资料、实际UI引用/报告及冷重开，再完成N2–N6/O4。当前具体证据和时间在p33/journal.md及Host G journal，不以早期总PASS数冒充G完成。

## 1. 整体进度

| 版本 | 状态 |
|---|---|
| 第一阶段 BaseAgent（S1–S5）+ Host 钉版 | ✅ SHIPPED |
| 第二阶段 ORCH-BUILD 第 2–9 步 | ✅ SHIPPED |
| Phase3 **P3.1** 真实 App Mission 控制闭环 | ✅ SHIPPED |
| Phase3 **P3.1 遗留修复** | ✅ SHIPPED（SDK 0.9.11） |
| Phase3 **P3.2** 隔离执行与真实受控交付 | ✅ SHIPPED（SDK 0.10.0，SDK `48e441a`，Host `04350956`） |
| Phase3 **P3.3** 非代码 Mission 与证据闭环 | 🔨 **进行中——计划第 3 版已定稿，切片 A–E 已完成 SDK 源码验证；E 全量1199 passed /8 skipped；F候选0.11.0验证完成（既有红集保留），G未完成** |
| Phase3 P3.4 / P3.5 | 已实施多项功能并有定向验收；整体OPEN，详见顶部当前接续 |

用户的总指示（原话）："先修复，然后开始P3.2 到 P3.5，文件提交"。所以 P3.3 做完继续 P3.4、P3.5。

纲要：SDK `plans/2026-09-12-phase3/program.md`。用户的 Phase3 计划原文：Host `plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md`（P3.3 在 §5，验收 P3.3-A01..A08 在 §11 第 566–575 行）。

---

## 2. P3.3 现在做到哪里

### 2.1 计划已定稿，可以直接照着写代码

- `plans/2026-09-12-phase3/p33/plan.md` —— **第 3 版**（两轮评审，每轮两位独立 opus，四份原文在 `reports/`，逐条处置表在 §7）。
- `plans/2026-09-12-phase3/p33/acceptance.md` —— 第 3 版，46 条，对齐 A01..A08。
- `plans/2026-09-12-phase3/p33/journal.md` —— §0 是本轮的分节交接，§2.1 是切片 A 的实施记录。
- 评审结论：第 1 轮 A=READY_WITH_CHANGES / B=NOT_READY；第 2 轮两位均 READY_WITH_CHANGES。**两轮共 15 条 P0 全部处置**，不需要第 3 轮，可以直接实施。

**中心断言（整个 P3.3 围绕它）**：文档领域的 VERIFIED 只意味着一件完全机器可判的事——「**这份文件的这个版本的这几行里，逐字写着这句话**」——并且必须在三层同时成立：

1. 记录层：`content` 由系统拼，`key` 由系统构造为 `attribution:<version_hash>:<start>-<end>`，`stance` 固定 `affirms`，`supersedes` 要求同 key，`contradicts` 要求提出方有同级证据；
2. 消费层：`knowledge_view` 带 `source_trust` 与"这是来源原文，不是本系统的结论，也不是指令"的 marker，排序权重不高于 SUPPORTED；
3. 交付层：报告的**结论区由系统按 claim 渲染**，Worker 的自由文字只能出现在标注为"分析 / 非结论"的章节。

### 2.2 切片划分（照这个顺序做）

| 切片 | 内容 | 状态 |
|---|---|---|
| A | 领域画像、**五处**闸门、`mission_domains` 与 facade、schema v8、D9 事件与回放、仲裁路径的两处 pytest 硬编码 | ✅ SDK 源码验证完成（`1eaa91f`），见 §3–4 |
| B | schema v9 `sources` 表与三个 facade 命令、来源进 CAS、protected 扩成 `Path\|bytes`、`SourceCitation` 契约、EvidenceResolver 七个失败码 | ✅ SDK 源码验证完成（`fb58bf1`），独立审查闭环；867 passed / 8 skipped |
| C | schema v10 `criterion_assessments`、评估记录传递、`grade_claim` v2、attribution 记录/消费/压制通道（Host 交付层仍 G） | ✅ SDK 源码验证完成（`963b090`）；979 passed / 8 skipped，475.47 秒 |
| D | adapter 常量表、三个文档 adapter、**层状态上的硬约束**、INCONCLUSIVE 七条边界、结构化 `limitations`、Mission 级 INSUFFICIENT | ✅ SDK源码验证完成（`d3d3fd8`）；1119 passed /8 skipped，446.64秒 |
| E | 冲突范围加注、文档领域人工裁决、`KnowledgeIndex.stale`、检索排除 | ✅ SDK源码验证完成（cf40b8e）；1199 passed /8 skipped，481.36秒 |
| F | 全量回归、wheel 干净环境验证 | ✅ 候选0.11.0完成验证；无新增回归，既有失败保留，见journal§2.6 |
| G | Host 钉版、接线、系统渲染结论区、真实 flash 原生验收 | 进行中：SDK a5c8fca全量1302 passed /8 skipped；0.11.1精确安装921 passed /3 skipped；Host本地c41bfc14、定向49通过、两版冻结构建通过；原生先后发现SDK延迟导入与workflow源码缺包，均已修并7项专项通过，第三版构建中。真实模型文档任务与完整原生验收未完成，G尚未推送 |

**每个切片完成即跑 `tests/orchestrator` 全量**，不等切片 F。

---

## 3. 切片 A–D 已完成，下一步 E

### 3.1 已完成（代码已提交）

- **新增 `src/agent_orchestrator/governance/domains.py`**：`DomainProfileV1` + `ConflictTemplateV1`，注册 `code-v1` 与 `doc-research-v1`，`check_against_domain()` 是五处闸门共用的那一个检查。
- **五处闸门全部装上**：
  1. `graph/task_graph.py::validate_graph`（整图提案）
  2. `graph/changes.py::validate_change`（图变更）
  3. `commit_service.py::_check_task_proposal`（单 Task 提案 / Manager `add_task`）
  4. + 5. `commit_service.py::_check_system_template`，挂在冲突模板与综合模板的两处 `insert_task` 上
- **领域冻结**：`MissionSpec.domain`、schema v8 `mission_domains` 表、`Store.bind_mission_domain` / `get_mission_domain`、`CommitService.domain_for()`、facade `OPEN_FIELDS` 加 `domain`、`api/missions.py` 校验未知领域。
- **系统模板可替换**：`planning/manager.conflict_task` 接受 `template`，准则与政策来自画像；综合任务的默认政策来自 `domain.synthesis_default_policy`。
- **仲裁路径的两处硬编码**（原计划在切片 E，评审要求前移）：`check_arbitration` 增加 `domain` 参数（code 领域判据一字不变；`decides_with == "human_review"` 的领域不再要求引 `pytest:`）；`_open_conflict` 的部署闸门从写死 `code_test` 改为按画像的 `decides_with` 判断。
- **回放（D9 的一半）**：`MissionCreated` payload 带 `domain_id`；`FORMAL_FIELDS["mission"]` 与 `OPTIONAL_FIELDS` 各加一项；`formal_from_snapshot` 与 `Store.snapshot` 同步补 `mission_domain`（带 `has_table()` 守卫）。
- **早期测试（历史）**：`tests/orchestrator/p33/` 三个文件 25 条（`test_p33_domains.py` / `test_p33_domain_binding.py` / `test_p33_arbitration_domain.py`）。

### 3.2 本机完成的余项与恢复修复

- 11 个文档角色的专属模板、工具集合和 domain context 已接入实际 dispatch；code-v1 默认提示与冻结 policy 选版保持兼容。
- `domain_for()` 从绑定 JSON 读取完整快照；文档画像 v2 冻结 role/context，新版缺字段拒绝。历史 doc-v1 缺字段使用固定兼容表，已有 intent 不重写。
- 旧 Critic 幂等复用时，层版本来自真正 SETTLED 的 ordinal；关闭/重开库与人工审阅后复用都有决定性控制。
- P33-09 五入口准则拒绝覆盖齐，闸门 2–5 另补 rollback 与同实例修正重试；结果 evidence kind 校验仍属于 B/C，不能扩大 A 的完成范围。
- 全量暴露的 pytest 上级配置问题已修；消失执行器 fixture 加显式 gate，生产恢复逻辑未改。首次失败及原源码对照详见 `p33/baseline.md`。
- 提交：`fdc9c91`（冻结角色和 Critic 来源）、`1eaa91f`（pytest 配置边界及回归修正）。架构事实源见 `ARCHITECTURE/ORCHESTRATOR.md`。

### 3.3 B 已实现的接口与边界

- Store：`get_source(mission_id, path, version_hash=None)`；指定 hash 读历史版本，不指定读有效版本。`list_sources(mission_id, active_only=False)` 返回确定顺序。
- resolver 接收系统冻结的 tenant/Mission/source_versions/source_roots；精确登记版本缺失统一 not_found，已登记但不在冻结集才 stale_source。CAS 共用取数口，不能改读工作区或当前 head。
- supersede/revoke 复用 Facade.decide → ApprovalApi → decide_approval 与现有审批表，L2；批准时同事务检查旧版本、记录决定、更新来源与事件。
- 发布根与实际 CAS/来源挂载根作物理路径不相交检查；逻辑 `sources/` 不与物理目录直接比较。这是写入边界，不声称证明用户复制内容的原创性。
- Citation 文案后续增加时发布新 prompt/profile 版本；不能修改已冻结 doc prompt v1。

---

## 4. 当前测试状态

- ✅ **B 干净提交 `fb58bf1c6e5ad92bb7e64791e24c786282684058`：编排全量 867 passed / 8 skipped / 0 failed，488.39 s**；专项 300 passed / 8.13 s。8 个 skip 均为未启用的真实 Provider 门。审查无剩余 P1/P2；测试进程组 531 已退出，无遗留子进程。
- B 证据和 SHA-256 索引见 `p33/journal.md` §2.2；下面 A 的 651 项是历史切片基线。C 已完成 SDK 源码验证（963b090 全量 979/8/0），P3.3 整体及 wheel、Host/真实模型仍未验收。

- ✅ **干净提交 `1eaa91f67b93eacaa7f5862a595421bb20d828a9`：编排全量 651 passed / 8 skipped / 0 failed，490.43 s**。8 个 skip 均要求 `--run-real-provider`，不冒称真实模型验收。
- ✅ 定向 P3.3 最初 58 passed；之后 16 条配置边界场景加两条原失败 18 passed；类型检查 85 文件、改动范围 Ruff、diff-check 均通过。最终全量包含全部新控制。
- ✅ 两轮独立累计 diff 审查，无未解决 P0/P1；测试进程组已退出，未遗留子进程。
- 原机早期 603 passed / 8 skipped 为历史；本机首次全量 632 passed / 3 failed / 8 skipped 保留在记录里，已逐项修复。
- 整仓 `tests/` 的历史 73 条红集仍未在本机重跑，按计划切片 F 核对；本次不将两个本机复现问题并入历史红集。
- 本地证据索引：`.local-test-evidence/2026-09-12/p33-a-resume/`；`orchestrator-final.log` SHA-256 `e74b7b6efab590e6a9b30761d523273af8509ff2ca86a34a5cd57254f3668fae`。完整小型结论见 `p33/journal.md`、分步入口见 `p33/testcase.md`。

### 4.1 跑回归的正确姿势（这一轮踩过）

- **不要**用 `-o faulthandler_timeout=N` 当看门狗：它只打印栈、**不杀进程**，挂住时看上去像"还在跑"（这轮因此空转了一个多小时）。
- **不要**把 pytest 输出接进 `tail`：全缓冲，进度完全不可见。
- 用 `scratchpad/full.py` 那种 `subprocess.Popen(...).wait(timeout=...)` + `proc.kill()` 的看门狗，输出直接写文件。挂住时逐套件、再逐文件二分定位。

---

## 5. 实现时必须知道的代码事实（评审查出来的，都已逐条核过）

这些是本轮最贵的信息，重做一遍要花掉两轮评审的成本：

1. **来源字节必须有权威副本**。验证副本的写入顺序是 seed → inputs → **artifacts** → protected（`artifacts/workspace.py:318-343`），而 `_protected_seed`（`orchestrator/event_handler.py:2407`）只保护 `tests/` 与 `pytest:` 目标，且唯一数据源是 `mission.final_report["workspace_seed"]`。所以 Worker 把来源改写后登记成 artifact 就能覆盖验证副本里的来源。→ 来源必须进 CAS，解析器**与 adapter 都只从 CAS 读**，用 `ArtifactStore.read(content_hash)`（`artifacts/store.py:125`）；**不是** `read_verified`，那个是模块级函数、参数是 `Artifact`。
2. **`_protected_seed` 与"来源走 inputs"互斥**。protected 的重建走 `write_text`（512KB + 纯文本）。切片 B 要把 protected 扩成 `Mapping[str, Path | bytes]` 并按来源根前缀判定；顺带补 `workspace.py:152` 的 `write_bytes` 缺失的 `writable` 检查。
3. **`key`/`stance` 是模型自由填的且原样进知识库**（`commit_service.py:1585`），而检索（`context/retrieval.py:172`，`TRUST={"VERIFIED":1.0}`）、冲突判定（`verification/conflicts.py:51`）、上下文装配（`context/context_builder.py:211`「只把 VERIFIED 当事实」）**全都按 `key` 工作**。只约束 `content` 等于没约束。
4. **`supersedes` 不校验 key**（`commit_service.py:1532-1547`）；**`contradicts` 是免费的降级通道**（`conflicts.py:47-49` + `commit_service.py:1663-1666`，对方 SUPPORTED 会被强制打成 DISPUTED）。
5. **层状态词表没有 INCONCLUSIVE**（`deterministic_checks.py:29-32`），而 `passed` 只认 PASS / NEEDS_HUMAN（`verifier_router.py:251-257`）。在"能否被接受"的语义下 `FAIL → INCONCLUSIVE` 是**放宽**，所以"只能下调等级"的硬约束必须写在层状态上，不能写在 adapter verdict 上。
6. **accept 只收到 PASS 的层**（`event_handler.py:2182-2187`），且 accept 时验证副本已不在手上。评估记录只能搭 `LayerResult.detail` 便车经 `record_verification_layer` 落 `verifications` 表，再在 accept 事务里重放；`verifications` 是 `UNIQUE(result_id, layer)`，**一个 layer 只有一行**。
7. **accept 开头还有一次 TOCTOU 的 `KnowledgeIndex.check`**（`commit_service.py:3003`），非空直接 `fail_result`。失效来源的判定**绝不能**混进 `check()`，否则一个已通过全部六层的结果会在 accept 被判失败。要新开 `KnowledgeIndex.stale()`。
8. **Mission 级判定树不含 inputs**（`event_handler.py:3521-3545`，只挂 `workspace_seed` + 已接受 artifacts）。来源移出 seed 后判定树看不到来源，而文档 Mission 的 Mission 级准则基本是自由文本 → `needs_critic` 必为真 → 整个 Mission 的成败由一个**看不到来源的 judge Critic** 决定。
9. **回放加新 kind 要同步三处**：`apply()`、`FORMAL_FIELDS`、`formal_from_snapshot`（`observability/replay.py:481`）+ `Store.snapshot`（照 `actions`/`approvals` 加 `has_table()` 守卫）。漏了 `formal_from_snapshot`，`compare()` 的反向检查会把每一条新对象都算成 mismatch。`revoked` 用**布尔**不用时间戳（`compare()` 是精确比较，两侧时钟不同就是永久 mismatch）。
10. **`runs_layers` 必须显式声明，不能从 `default_policy` 推断**。推断会顺带拒掉 `code-v1` 今天能通过的 `formal_check`，破坏 A07（未部署的 formal 层应当在既有 `deployed_layers` 闸门被拒并在 router 里 ERROR，而不是被领域闸门多拒一次）。`test_p33_a13` 是这条的钉子。
11. **`MissionSpec.to_json()` 里 `domain` 只在非默认时出现**，否则升级后 Host 重发同一请求会因 `spec_hash` 变化拿到 `MissionConflict`（`test_p33_a16` 钉住）。
12. **fixtures 悬挂**：脚本耗尽直接抛 AssertionError（`testing/fixtures.py:104,514,524`）。历史上多次"新增校验让旧脚本多走一轮 → 耗尽 → SDK UNKNOWN 出站调用 → 悬挂"。切片 B/C/E 每一个都可能触发，所以每切片完成即跑全量。
13. **真实模型写不出合规 citation 的可行性风险**：表格单元格与冒号句（`：` 不是句终符）只能整行整引，而 P3.3 的场景恰以表格与冒号句为主；模型的省力反应是**改结论去迁就可引的句子**，比引用失败更糟。切片 G 要先用真实文档做一次可行性 spike，不合格就回头调文法**而不是调结论**。

14. **领域画像不能给 `code-v1` 设政策下限**。今天的代码**没有任何下限**：step 8 的消融用例会提交只有 `("critic_review",)` 一层的政策。加了下限 → 提案被闸门拒 → Planner 重试 → scripted provider 脚本耗尽 → SDK UNKNOWN 出站调用 → **整套回归悬挂**。这是本轮真实踩到的第 12 条风险，钉子是 `test_p33_a14`。推论：**任何加在既有路径上的新校验，都要先问"今天的 fixtures 会不会被它拒"**。

---

## 6. 本轮的裁决（不要重新讨论）

- **不采纳**"按 claim 类型分叉判冲突"，取更严的方案：范围**只加注不豁免**，`checked_scope` 缺省 = 全域；"各自成立"只能是 Conflict Task 的裁决结果。
- **砍掉** statement → VERIFIED 这条路（"两个以上互不隶属来源"机器判不了），文档领域 statement 封顶 SUPPORTED。
- **砍掉** `span_not_minimal` 失败码（改由系统收紧区间）、`uncited_conclusion` 检查（挡不住真实误导又误报）、`checked_scope` 的交集代数（没有消费者）。
- **`out_of_scope` 并入 `not_found`**（两个可区分的码等于泄露来源根结构）。
- 句终符只留 `。！？`：`；` 与拉丁 `.` 会重新打开"剥离前提"（`e.g. ` / `U.S. ` 是假终符）。拉丁句子整行引。
- **`CONTRACT_SCHEMA_VERSION` bump 到 2**（`ClaimProposal.citations` 让 `from_json` 的严格未知键拒绝对旧 SDK 生效）。
- **`VERIFIER_VERSION` 不 bump**（层语义未变；bump 会让挂起中的 Mission 恢复时层不可复用，`human_review.py:66-68`）。
- **A03 在本轮只对文档领域关闭**：`code-v1` 的 `covering_target` 路径前缀判定原样保留 → 遗留 **F-P33-1**，留到 P3.4。

---

## 7. 遗留

- P3.3 已登记：F-P33-1（`code-v1` 路径前缀覆盖判定）、F-P33-2（二进制来源无登记通道）、F-P33-3（`tool-run:` 不可核验：`call_key = run_id:call_id`，模型写信封时两个 id 都不知道）、F-P33-4（混合领域 Mission）。
- P3.2 的 F-P32-1..7 在 `p32/journal.md` §6（最重要的是 F-P32-1 宿主崩溃后逃逸进程认不出来、F-P32-2 候选 schema 没进模型输入包，建议放 P3.5）。
- P3.1 的 F-ORCH-1..7 在 Host `plans/2026-09-11-orchestrator-host-integration/journal.md` §5。

## D 最新交接（18:51）

D 干净源码 `d3d3fd8650acc8b837dc4c0e093ab95068d054ff` 完整编排 **1119 passed / 8 skipped / 0 failed**（446.64秒），watchdog446.86秒，PG18131无残留。8项skip为未启用真实Provider。D为SDK源码里程碑，E–G、Host/wheel/真实flash仍未完成。

E只读挑战已完成：直接citation新接受检查与stale used_knowledge分离；Knowledge来源依赖用同路径多版本并集；普通review不可代替doc冲突仲裁；已有HumanOverride/ConflictResolvedByHuman可承载contextual裁决，不升级Claim。开始E前写slice-e-readiness并保留全部原AC。

### E最新接续（19:38）

E 干净源码 `cf40b8ec86a2f307d0f8b8f89cf7f0166e5de121` 完整编排 **1199 passed /8 skipped /0 failed**（481.36秒），watchdog481.63秒，PG25036无残留。8项skip为未启用真实Provider。A–E完成SDK源码验证；F/G、wheel、Host与真实flash仍未完成。
E从18:53开始，完成后继续F整仓红集逐nodeid核对和0.11.0安装验证。G仍需SDK判定树挂源/原子来源创建/引用读取，必须按最终源码重建制品，不能沿用A–E wheel身份。

### F最新接续（20:17）

F 验证完成（保留既有红集）：干净源码 `5bcca08fe666b8e20524206b76ce2afbba63db4d` 整仓3241 passed /60 failed /18 errors /13 skipped（547.16秒）；与同依赖旧源码a4aae8c的78项红集按kind+nodeid完全相同，新增0。0.11.0安装验证1402 passed /11 skipped /1既有迁移失败（506.99秒）；304包文件逐字匹配，258实际加载模块均来自安装包且哈希一致。F不是整仓全绿或新正式发布；G、Host与真实flash仍未完成。

wheel SHA-256 `757d6fb195187bebf393b37482ee308587007e4f198aa4a36132901c35f8a96e`，本机p33-f/candidate。Host仍04350956/SDK0.10.0。G需SDK原子创建与citation_read、DOC_PROFILE v4 successor的Mission源目录/当前消费/恢复、Host系统报告和来源操作，以及最终制品与真实原生flash。G只读准备在本机p33-f/g-preparation.md；尚未实施，不把F wheel称作G最终制品。
