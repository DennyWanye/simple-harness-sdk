# P3.3 非代码 Mission 与证据闭环 · 记录

## 0. 交接（冷会话先读这一节）

- **当前位置**：计划第 3 版已定稿（两轮各两位独立评审，四份原文在 `reports/`，处置表在 `plan.md` §7）。**切片 A–E 已完成（SDK源码）；E cf40b8e全量1199 passed /8 skipped /0failed；下一步F**。
- **中心断言**：文档领域的 VERIFIED 只意味着「这份文件的这个版本的这几行里，逐字写着这句话」，且要在记录层 / 消费层 / 交付层三层同时成立。详见 `plan.md` §0。
- **切片顺序**：A 领域画像与五处闸门 → B 来源与证据解析 → C 评估记录与分级 → D adapter 与证据不足出口 → E 冲突与失效 → F 回归与 wheel → G Host 与原生验收。
- **每切片完成即跑 `tests/orchestrator` 全量**（不等切片 F），并同步更新本文件。
- 真实模型只用 `deepseek-flash`；记录与回复一律中文。

## 1. 计划评审

### 1.1 第 1 轮（A 威胁模型 READY_WITH_CHANGES / B 代码一致性 NOT_READY）

共 10 条 P0、18 条 P1、9 条 P2，全部采纳或给出裁决理由，处置表见 `plan.md` §7。最关键的三条：

1. **来源字节没有权威副本**（A P0-2）：验证副本写入顺序是 seed → inputs → **artifacts** → protected，而 `_protected_seed`（`event_handler.py:2407`）只保护 `tests/` 与 `pytest:` 目标——Worker 把来源改写后登记成 artifact，就覆盖了验证副本里的来源。第 1 版只靠"重算 hash 等于登记表版本"兜底，而那条检查是当作 A08 的新鲜度检查写的，没意识到它实际承担防篡改职责。
2. **引文"区间内字面包含"能剥离否定词**（A P0-3）：来源写「我们**不**建议采用方案 A」，引 `"建议采用方案 A"` 字面成立。
3. **系统模板硬编码 pytest**（B P0-3）：`conflict_task` 的准则、`CONFLICT_POLICY`、synthesis 默认政策全都写死了 pytest / `code_test`。文档领域只设下限挡不住——系统会自己建出被自己闸门拒掉、或者永远跑不完的任务。

唯一不采纳的是 A P1-5（按 claim 类型分叉判冲突），取 B P0-1 的更严方案：范围只加注不豁免。理由是 A 的分叉在 attribution 上放行，而 `checked_scope` 缺省未定仍会静默关掉 code 领域的冲突检测。

### 1.2 第 2 轮（两位均 READY_WITH_CHANGES）

两位独立指出同一个中心 P0：**第 2 版的断言为假**——被约束的只有 `content`，而 `key` / `stance` / `supersedes` / `contradicts` 仍是模型自由填的，且检索、冲突判定、上下文装配**全都按 `key` 工作**（`commit_service.py:1585`、`conflicts.py:51`、`retrieval.py:172`）。另外三条：

- INCONCLUSIVE 在"能否被接受"的语义下比 FAIL **宽松**，所以"只能下调等级"的约束写错了层次，必须写在**层状态**上；且"来源与主张矛盾 → FAIL"不能由模型 adapter 判。
- 报告正文与 claim 之间没有任何绑定 → 结论区改由系统按 claim 渲染。
- 仲裁这条路上还有**四处** pytest 硬编码：`_open_conflict` 的部署闸门（`commit_service.py:1735`，纯文档部署里每个冲突都 DEFERRED）、`check_arbitration` 的目录约束、Arbiter 角色模板、`context_builder` 文案。

三份评审给的代码依据我都逐条核过，**全部属实**。

## 2. 实施

### 2.1 切片 A：领域画像与五处闸门

新增 `governance/domains.py`（**不叫 `profiles`**——该词在本仓库已指运行时模型画像）：

- `DomainProfileV1` 能**替换**而不只设下限：`default_policy` / `conflict_template` / `synthesis_default_policy` / `runs_layers` / `external_check`。
- `runs_layers` 是**显式声明**而不是从 `default_policy` 推断出来的。第一版实现用推断，会顺带拒掉 `code-v1` 今天能通过的 `formal_check`——那会破坏 A07（未部署的 formal 层应当在既有的 `deployed_layers` 闸门被拒、并在 router 里 ERROR，而不是被领域闸门多拒一次）。`test_p33_a13` 是这条的钉子。
- 注册两份：`code-v1`（逐字复刻今天的常量，含 `SYSTEM_DEFAULT_POLICY` 与 `CONFLICT_POLICY`）与 `doc-research-v1`。`LEGACY_DOMAIN` 就是 `code-v1` 本身，不另设第二个 id。
- `MissionSpec.domain` 在 `to_json()` 里**只在非默认时出现**，否则升级后 Host 重发同一个请求会因为 `spec_hash` 变化而拿到 `MissionConflict`（`test_p33_a16` 钉住）。

五处闸门（第 2 轮评审纠正了第 1 版说的"三处"）：

| # | 位置 | 覆盖 |
|---|---|---|
| 1 | `graph/task_graph.py::validate_graph` | 整图提案 |
| 2 | `graph/changes.py::validate_change` | 图变更 |
| 3 | `commit_service.py::_check_task_proposal` | 单 Task 提案 / Manager `add_task` |
| 4 | `commit_service.py` 冲突模板的 `insert_task` | 系统模板 · 冲突 |
| 5 | `commit_service.py` 综合模板的 `insert_task` | 系统模板 · 综合 |

4 与 5 走新的 `_check_system_template()`：系统自己写的任务和模型提的任务过同一个检查——一个领域会拒绝的模板是**这里的 bug**，不该等到四个切片之后变成"一个永远完不成的任务"才被发现。

仲裁路径上的两处硬编码也在本切片前移处理（第 2 轮 B P2-4：原计划放切片 E，中间四个切片里文档冲突任务必 FAIL）：

- `check_arbitration` 增加 `domain` 参数。code 领域判据一字不变；`decides_with == "human_review"` 的领域不再要求引一条它本来就不允许出现的 `pytest:` 证据。
- `_open_conflict` 的部署闸门从写死 `code_test` 改为按画像的 `decides_with` 判断。

schema 迁移**追加式**推进，一个切片一版：v8 `mission_domains`（本切片）、v9 `sources`、v10 `criterion_assessments`。

回放（D9）：`MissionCreated` payload 带 `domain_id`，`FORMAL_FIELDS["mission"]` 与 `OPTIONAL_FIELDS` 各加一项，`formal_from_snapshot` 与 `Store.snapshot` 同步补 `mission_domain`（照 `actions`/`approvals` 用 `has_table()` 守卫，否则旧库读新代码会抛）。

## 3. 回归

### 3.1 切片 A 早期原机记录（历史）

`tests/orchestrator` 全量：**603 passed, 8 skipped, 0 failed（360.8 秒）**。8 个 skip 全部是需要 `--run-real-provider` 的真实端点用例，与本切片无关。

第一次跑的时候**整套悬挂了一个多小时**，根因是我自己引入的缺陷，记在这里：

- 我给 `code-v1` 画像设了 `planner_floor=("format_check",)`，而今天的代码**没有任何政策下限**。`step08/test_ablation.py::test_review_p1_3_an_ablation_that_leaves_no_layer_is_never_a_pass` 提交的政策是 `("critic_review",)` 一层，被新闸门拒 → Planner 重试 → `RoleScriptedProvider` 只配了一个 planner 步、脚本耗尽 → SDK UNKNOWN 出站调用 → 悬挂。
- 这正是 plan §6 风险表里登记的那条（fixtures 悬挂），第一次就被我自己踩中。
- 处置：`code-v1.planner_floor` 改回空（下限是文档领域的概念）；补钉子 `test_p33_a14`，并在 `test_p33_a13` 之外**单独钉住下限**——原来那条只钉了 `runs_layers`，所以没拦住。
- 教训：**任何加在既有路径上的新校验，先问"今天的 fixtures 会不会被它拒"**，而不是只问"新领域对不对"。

定位方式（工具在 scratchpad）：`-o faulthandler_timeout=N` 当看门狗是错的（只打栈不杀进程），pytest 输出接 `tail` 会全缓冲。改用 `subprocess.Popen(...).wait(timeout=...)` + `kill()`，输出直接写文件，逐套件 → 逐文件 → 逐用例二分。

## 4. 真实与原生

（待填）

## 5. 遗留

计划里已登记 F-P33-1..4，见 `acceptance.md` 退出门槛一节。


## 6. 2026-09-12 本机切片 A 收口

### 实际完成与审查

- 完成画像 role/context 冻结、11 角色接线、注册表变化后的 snapshot 读取，以及其余四个入口的真实 commit rollback 控制。code 默认提示和政策语义保持兼容。
- Ohm 独立审查发现 Critic 旧 intent 错标新版本的 P1，已修复实际 SETTLED ordinal 的来源记录/人工复用，并通过两条重启控制。新版画像缺字段拒绝、固定 doc prompt 派生版本两条 P2 也已处置。
- Kepler 对 A 累计 diff 独立审查 ACCEPT；对后续 pytest 配置边界修复再次审查 ACCEPT，无未解决 P0/P1。四入口测试由主代理复核；恢复 fixture 的修复未改生产状态机或降低“不重跑 A”的预期。
- Push 前文档审查的 P2 已修：testcase 中整图入口改为正确测试文件，并将其独立合法对照与其余四入口的同实例 rollback/重试区分，证据范围不扩大。
- 本机首次全量三个失败全部处置，包含原源码对照；原因与未伪造的 red/green 边界见 [baseline.md](baseline.md)。当前 source 与冻结计划的差异只是必要的回归修复，没有删除 B–G 的 MUST。

### 提交与最终验证

- 起点：`a4aae8c23a2b72b9f2b07c62986fc7dd39f36cdf`。
- 实现：`fdc9c91d98410287ec0645ccbdee43a38cbf44e7`。
- 修正与**被测源码 HEAD**：`1eaa91f67b93eacaa7f5862a595421bb20d828a9`，开始测试时工作树为空。
- 命令：`.venv/bin/python -m pytest tests/orchestrator -q -x --basetemp .local-test-evidence/2026-09-12/p33-a-resume/final-tmp`，外部进程组 watchdog 900 s。
- **651 passed / 8 skipped / 0 failed，490.43 s**；8 个既有真实 Provider 用例要求 `--run-real-provider`，本次未启用。
- 类型检查：85 源文件通过；改动范围 Ruff / diff-check 通过。两轮 pytest 进程组均已退出，无残留。
- [分步验收索引](testcase.md)；[当前架构事实](../../../ARCHITECTURE/ORCHESTRATOR.md)。完整 P3.3 尚未完成，当前只关闭 A 源码验证；F/wheel、G/Host 与真实模型门保留。

### 本地原始证据索引

目录：`.local-test-evidence/2026-09-12/p33-a-resume/`。以下均 ignored，仅本机保存，未提交原始日志、数据库或机器报告。

| 文件 | SHA-256 | 结论 |
|---|---|---|
| baseline.log | 01b21240cd135e06bcb54ecf3fa86246caff4be90464b7f7a0facfe1527f0768 | 实现前定向 66 passed |
| orchestrator-full.log | f0e55511ecb821540323dbe76f570e142997f4e42bf2617aad7d0d76fed33cfc | 首次全量 3 failed / 632 passed / 8 skipped |
| baseline-red-check.log | 0f51d2c7cc18eacb4b535e5243680d61bea6607750a5ec305226ddde2a13bd5e | 原源码复现两个本机问题 |
| regression-repair.log | fcc40070f6cc3487485917991face1c0c388078e14696b3655e9ed5c2bf75190 | 16 配置场景加两原失败：18 passed |
| orchestrator-final.log | e74b7b6efab590e6a9b30761d523273af8509ff2ca86a34a5cd57254f3668fae | 干净源码最终编排全量通过 |
| mypy-final.log | 9ad320a85ca21c0eff33d60d04b500d1f61878a0511449d431cc57cbc0d0a44d | 85 文件通过 |

### 继续 B

来源 Store/facade/审批由一位代理负责，SourceCitation/resolver 由另一位负责，主代理负责 workspace 与 Attempt 冻结接线。接口与目录隔离的实际证明范围见 [HANDOFF](../HANDOFF.md) §3.3；不另开审批系统。

真实测试凭据已在 Host 主仓 ignored `.env` 的 `DEEPSEEKER_APIKEY` 字段找到，只注入进程；模型固定 `deepseek-flash`。本节没有声称执行了真实模型调用。

片状态：A SOURCE_VERIFIED；整体 P3.3 IN_PROGRESS。沿用整体 journal，不制造片级发布 receipt。

## 2.2 切片 B 实施中

schema v9、来源三个 Host 命令、CAS 原文、SourceCitation v2 契约、EvidenceResolver、Worker/Planner/任务 Critic 冻结来源 map 已接入。
运行时按来源根保护写入，verification copy 从登记原始 bytes 重建；新 repair 去除已撤销来源，ACTIVE 树保留篡改证据。
文档结果的 pytest/tool-run evidence 在 CommitService 拒绝；实际收集入口把拒绝落为 ResultRejected，不能令调度器因异常退出。

独立审查 Ohm（未自审 resolver）发现 3 P1/2 P2：撤销资料继承、大小写读路径丢信任标记、重启已审批发布绕过隔离、失效审批无法拒绝、来源路径别名/祖先冲突。
前两项已修；发布 handoff 与来源审批/路径修复进行中。源码定向 PASS 不代表 B 完成；待修复审查、干净提交编排全量与交接回写。

### B 审查修复与提交前验证

- 来源审查 3 P1/2 P2 已修：new repair 不继承 revoked 原文；work/verify 大小写读取仍带 trust；全库发布 handoff 前的事务内隔离；reject 保留 binding 完整性但不要求当前 head/CAS；路径逐组件 NFC/casefold 冲突检查。
- 额外保留 ACTIVE 来源篡改证据；发布根大小写别名同样检查。
- Resolver 独立审查发现四处 P1，10 个 oracle 初跑 7 failed/3 passed；修复列表内缩进标题、表格后标题、单列表格、引号内句终符后通过。主审第五处 ATX 含管道符标题被单列表吞掉，新增反例先红再绿，独立复核通过。未把无空行 setext 表格行另算缺陷。
- Source 最终 74 条 + Resolver 56 条已通过；发布 guard 19 条已通过。P33 提交前全定向 **288 passed / 5.72s**（precommit.log）；mypy **87 源文件通过**；本次 22 个 Python 文件 Ruff 通过。首次全目录 Ruff 的 5 项是未改的历史文件 import 顺序，不扩大本次修改。
- 本片新增与修改源码将先提交为 clean HEAD，再跑一次完整 tests/orchestrator（P33-36）；B 尚未终态，不据此发布 wheel或声称真实模型通过。

- 收尾审查补 P2：来源撤销后的文件↔目录替换被旧 clone 挡住。改为仅新树在安装 inputs 前清理来源根；ACTIVE 树不动。真实 facade/register/revoke/Attempt/reopen 控制三参数通过。初跑 2 个拓扑错误是产品 red，另一个 markdown context 被误当 JSON 是测试夹具错误，分开记录。
- 最后定向 **300 passed / 8.13s**（`tests/orchestrator/p33` + `p32/test_p32_workspace_registry.py` + `step02/test_workspace_and_gateway.py`，final-smoke.log），其中 P33 290 条。
- Kepler 对发布 guard 和 main 根接线给出独立 ACCEPT；Ohm 对恢复、信任标记、Source 审批/路径及 Resolver 五处 P1 给出指定范围 ACCEPT，最后拓扑 P2 修复亦获追加 ACCEPT，无剩余 B 审查问题。

### B 干净源码验收完成

- 源码 `fb58bf1c6e5ad92bb7e64791e24c786282684058`，运行前后 Git clean；完整 `tests/orchestrator -q -x --basetemp .local-test-evidence/2026-09-12/p33-b-full`：**867 passed / 8 skipped / 0 failed，488.39 s**。外层看门狗耗时 488.71 s，进程组 531 已退出且无遗留进程。
- 8 个 skip 均为现有 `--run-real-provider` 用例；本片未换 wheel、未启动 Host、未做真实 Provider/UI 验收。C–G 继续，P3.3 整体不宣称完成。
- 最终定向：P33（290）+ p32 workspace registry + step02 workspace/gateway，共 **300 passed / 8.13 s**；mypy 87 文件、改动 Python Ruff、diff check 通过。独立累计审查见 `reports/code-review-b.md`，无剩余 P1/P2。
- 原始证据永久 ignored；以下路径相对仓库根，只有摘要与哈希进入 Git。

| 本地证据 | SHA-256 |
|---|---|
| `.local-test-evidence/2026-09-12/p33-b-resume/orchestrator-full.log` | `d7605cf9cfab9eb3607374522355c22d6096c2aca7c6b6556f189a1142af4b5d` |
| `.local-test-evidence/2026-09-12/p33-b-resume/orchestrator-full.json` | `cca8560947a030ea696abdffc99f1cbe457575dbfa4d188b75bc9a939e8031b3` |
| `.local-test-evidence/2026-09-12/p33-b-resume/final-smoke.log` | `2c4c78fc43f5ca085917150454988a6a46f74eaa5c863f92dc69c1d6c059574c` |
| `.local-test-evidence/2026-09-12/p33-b-resume/topology-red.log` | `a453e016f001f4dc02e5a7e116823da051bb997089dcbce4e9a5fde5ea2003d6` |
| `.local-test-evidence/2026-09-12/p33-b-resume/topology-green.log` | `cc188d2ce92a6ec8f290de37a2d0707283d220cd55b48b198cd8ea3f2ac42de3` |

计时：此前开发未逐项计时，不以测试时间代替开发总耗时。2026-09-12 17:09 +08:00 恢复执行 B 文档/推送收尾；后续按阶段记录起止。

## 2.3 切片 C：评估记录与文档分级

2026-09-12 17:14 +08:00 开始；17:42 核心实现、主要审查与定向回归约 28 分钟。B 本轮 17:09–17:14 收尾约 5 分钟：文档提交 `a26e6a5` 已推送，提交后 P33 290 passed / 5.48 s，远程 0 ahead / 0 behind。

- 开工前完成两位独立挑战并冻结细化/oracle：[slice-c-readiness.md](slice-c-readiness.md)。沿用同一 P3.3 整体 run，不缩原 MUST、不生成片 receipt；D–G 未完成。
- schema 10 追加 criterion_assessments；实际 SDK dispatch→CAS resolver→rule detail→record→accept 同事务写 assessment/claim/knowledge。binding 校验冻结 Task 语义、当前 claim revision、全部产物 hash、tenant/Mission/Attempt/result 与来源集合；caller PASS 不可替代记录。
- 结构准则单列完整 criterion_verdicts，不制造空 claim assessment，不为文档结论晋级。cite 仅证明来源引用，free 仅接受字面绑定；全部提交引用均需解析。
- 文档 VERIFIED 仅限 content 与完整引文字面相等的来源归属；系统构造 content/key/stance，保存原信封。statement 封顶 SUPPORTED。来源声明的下游标记、排序及同一行不同句去重边界接通；显式压制和 supersedes 检查同级证据/来源身份。
- 模型不能占用 attribution: 系统 key；无支持的 explicit contradicts 被记为拒绝。长引文系统 wrapper 超 20k 的真实 accept 失败已修：仅正式归属记录容纳完整 path/quote/locator；模型输入与 statement 原上限不动，原文不截断。
- pending 旧规则 PASS 无评估时从冻结来源真重跑；旧 Critic 的 SETTLED intent/ordinal/prompt 正确复用。DONE/PASS 历史只允许相同重放，不重分级/回填。评估不进入正式回放；VerificationLayerRecorded 仍只发摘要，完整 detail 持久化在 verifications。
- 候选动作结构检查要求存在通过既有 handler 的匹配候选，scope 仅 schema/deployment/charter，非动作已执行。

### C 已执行的门与首次失败

| 门/反例 | 当前结果 | 本地证据（相对 `.local-test-evidence/2026-09-12/p33-c/`） |
|---|---|---|
| 文档 context 独立版本、原 code context 保留 | 首次缺 doc_assessment KeyError；接线后通过 | context-red-verified.log、first-combined.log |
| 评估/helper/grade/context/消费组合 | 71 passed / 0.29 s | first-combined.log |
| 实际 SDK 引用/推论/缺引用/一好一坏/保留 key/动作范围 | 9 passed / 0.75 s | runtime-value.log |
| 长合法引文 | accept 先报 claim.content exceeds 20000；修复后通过，另补关库重开与原上限反例 3 passed / 0.38 s | long-quote-red.log、long-quote-green.log、long-quote-reopen.log |
| 旧 Critic 与人工恢复 | 原控制 2 passed；加真实来源与旧规则重跑后 2 passed / 0.95 s | recovery-baseline.log、recovery-integrated.log |
| side artifact + 精确结构 scope | 首次 4 failed / 3 passed；修复后随累计集通过 | side-scope-red.log、p33-third.log |
| P33 累计与旧 fixture | 第一轮 286 passed 后缺规则记录；第二轮 393 passed 后共享 helper 的显式 CAS 未透传；第三轮 399 passed / 6.18 s | p33-first.log、p33-second.log、p33-third.log |
| P33 + step04 最终定向 | **445 passed / 1 skipped / 24.95 s**；真实 Provider 门未启用 | cheap-final.log |
| 类型/静态 | mypy 89 源文件通过；主改动 Ruff 通过；legacy grade 函数 AST 与 a26e6a5 一致（仅重命名） | mypy-first.log；可复核 git show/AST 对照 |

测试夹具错误与产品反例分开：最初 context/消费测试的 helper 返回值或字段名误用、缺必填构造字段不算产品 red；commits 首跑在并行实施中碰未接完参数不算原始缺陷。artifact_hash 篡改最初用 upsert 被 DO NOTHING 忽略，已先断言真实读回变化再验证。A 闸门 4 的旧 caller PASS 前置换为实际来源/producer/record，全部事务回滚 oracle 保留。B 撤销控制改为撤销真正被冲突一方引用的来源，Conflict/Task/Claim/Knowledge 不变与 replay 原断言保留；两个 fixture 增强已独立审查。

当前 C 已实现，干净提交全量仍待运行，未换 wheel、未进行 Host/真实 Provider 验收。来源失效、证据不足与系统报告的剩余义务分别留 E/D/G。

### C 首次干净全量与兼容修复（17:52）

源码 `673837d1834a81d7d70bdc6bcf5ab011a2aad9bf` 首次完整编排早停：637 passed / 1 skipped / 1 failed，177.07 秒（watchdog 177.34 秒）。`step02/test_recovery_matrix.py::test_s2_03_replays_do_not_duplicate` 在已接受 code 结果再次记录摘要时被新不可变保护拒绝。这是 C 的真实旧代码兼容回归，不改旧测试预期。

修复：仅文档领域执行新增 accepted record/fail 历史保护；原 code 重放接口保持原行为。原失败用例与文档接受/回放套件 29 passed，1.62 秒；mypy 89 files 和 Ruff 通过。修复后仍须干净提交完整回归，不将首次 637 条计为完整通过。

- `.local-test-evidence/2026-09-12/p33-c/orchestrator-full.log` SHA-256 `15a0b19b989376bc25f95db47257ec2dba339ba8ad9b53c4bb1cf8f707fd0dd3`

- `.local-test-evidence/2026-09-12/p33-c/orchestrator-full.json` SHA-256 `59a1736d9b95c6a70027c1b766c3ed739150398da3a8f00608b69ffcd17b5985`

- `.local-test-evidence/2026-09-12/p33-c/replay-fix.log` SHA-256 `684efba1f9aaac09d99a7375d09fe508a34a39ebcd82e4de4633185531f68d07`

### C 干净完整回归完成（18:02）

修复源码 `963b0903bab2a6d3bc645184966919df667e614c` 完整编排 **979 passed / 8 skipped / 0 failed**，pytest 475.47 秒、watchdog 475.74 秒。PG 10668 正常退出，剩余进程为空。8 个 skip 全为未启用的真实 Provider 门。首次真实兼容失败已保留，未改原 step02 预期；Kepler/Ohm 均复核限定 doc 的修复 ACCEPT。

本片核心：派发冻结 → 引用解析 → 完整评估记录 → accept 事务 → 文档分级/知识投影 → 下游来源提醒，以及历史/恢复/压制通道反例。mypy 89 files、修改 Python 的 Ruff、独立累计审查与架构回写完成。C 为 SDK 源码里程碑，不是 P3.3 整体完成；D/E/F/G 与 Host/wheel/真实 flash 仍待实施。

C 17:14–18:02 约 48 分钟，含实现、定向验证、评审及两轮完整回归；后者测试运行合计 652.54 秒（首轮早停 177.07 + 最终 475.47）。不把两轮测试时间当作全部开发耗时，也不把首轮已通过项累计为额外覆盖。

- `.local-test-evidence/2026-09-12/p33-c/orchestrator-full-fixed.log` SHA-256 `fd85ee7e5521f86cedf63357fe33d4f8334068e3b13988c30b1d27fbceb8f5a5`

- `.local-test-evidence/2026-09-12/p33-c/orchestrator-full-fixed.json` SHA-256 `a0aaf607b837b72f985368d866a5c5248321a1fb3cef797db38f4a2e24681632`

### C 推送与 D 开始（18:04）

C 文档提交 `ddf922fe16156ffd031ea8bdb19dab418c2910fa` 后 P33 **402 passed / 6.83 秒**（watchdog 7.06 秒），PG 15843 无残留，发送差异 key 模式匹配 0；SDK main 推送成功，HEAD/origin/main 0/0。C 含收尾约 50 分钟（17:14–18:04）。D 执行细化由 Kepler/Ohm 只读挑战确认，见 slice-d-readiness.md，18:04 开始实施。

## 2.4 切片 D：证据不足与有限接受

18:04 开始实施；当前候选通过定向与独立审查收口，准备冻结源码完整编排回归，尚非 D 完成。细化及先于实现的 D01–D09 oracle 见 slice-d-readiness.md。

- DOC3 冻结 citation_integrity@v2/source_coverage@v1；六层不变，unknown/crash/非法 PASS/损坏绑定 ERROR。coverage 从实际 CAS 取数；普通确定性 PASS 不触发 coverage。
- 严格 candidate 与结构化 limitations；仅有真实 INCONCLUSIVE、全部引用有效及完整局限可有限接受，claim UNDER_REVIEW/insufficient_evidence，无正式 Knowledge。同 claim 的 PASS 不遮盖 INCONCLUSIVE。
- 缺局限的实际失败计入已有 Attempt，限额前置于预算 reserve；runtime 避免通用 Manager 重建 Task 绕过限额。同结果重复失败不双计；人工升级 fresh/reuse 共用一次额度并要求同结果真实批准。
- Mission 固定原始准则分母，严格超过 0.5 才停止：FAILED + final_report.result=INSUFFICIENT；判定在 judge/发布前，事务内重算。文件/动作继续真实检查，root arbitration 保留既有 Critic 路径；来源挂载留 G。
- 接受历史评估需实际产物 bytes/hash/非符号链接检查；确定性同 key 反 stance 涉及 INCONCLUSIVE 时在 verification/accept/failure 推断实时复查，不能转证据不足。
- 契约 schema3 为已推送 C schema2 的后继；新 Event 默认 schema 和后续 VERSION_SOURCES 同步变化，历史事件/旧 intent/旧空信封字节不迁改。旧 C 测试明确固定 DOC2；DOC3 由 D 套件覆盖。legacy grade AST 与 ddf922f 相同。

| 验证及反例 | 实际结果 | 本地日志（p33-d/） |
|---|---|---|
| 缺 D 分级/context | 4 产品反例失败、1通过；实现后通过 | consumption-red.log |
| 初步整合 | 93 passed / 0.36s | integrity-first.log |
| 已接受产物损坏/丢失/符号链接 | 先3失败，修后3 passed / 0.13s | artifact-red.log、artifact-green.log |
| DOC3 reused Critic 人工额度 | doc先失败、code正对照通过；修复后并入127绿集 | critic-quota-red.log |
| root arbitration 实际 judge | 先 FAILED vs COMPLETED；恢复原 Critic 后通过 | arbitration-red.log |
| 损坏冻结绑定分类 | SQL真实故障注入先 FAIL vs ERROR；修复后通过 | binding-error-red.log |
| runtime/接受事务/评估组合 | 127 passed / 2.41s | integration-third.log |
| P33 全量便宜门 | 542 passed / 8.81s | p33-final.log |
| 类型/静态 | mypy 91 source files、修改 Python Ruff 通过 | mypy-final.log |

首次 dependency ImportError/接口尚未接通不算产品 red；损坏绑定首次用 update_intent 被不可变 config 保护忽略，后改真实 SQL 故障注入。随后的缩进错误只影响新增测试，已修；integration-third 才是最终绿集。原 C fixture 缺新增 read API 与默认画像升级的旧版断言分别修为明确历史绑定，保留原预期。accept 的实际硬失败 guard 最初位置遮住 rule FAIL 的 CommitRejected，调整至真实性校验之后，原 oracle 不改。

原始证据永久 ignored，未调用真实 Provider，未更换 Host wheel，未计入原生 UI 验收。

| 本地证据 | SHA-256 |
|---|---|
| `.local-test-evidence/2026-09-12/p33-d/artifact-red.log` | `c2b154bf6077f8c1e6d11dbcac87460bfc1fc0a7f66accbb54d0d066f68dbed5` |
| `.local-test-evidence/2026-09-12/p33-d/artifact-green.log` | `fbbf95fa6aa44c84e3793103c430a7b2d9d64cfff1e8b96a9b45f7abe33a5be7` |
| `.local-test-evidence/2026-09-12/p33-d/critic-quota-red.log` | `08566313e7e80b12a01ad3740b0fc7906f6bec974f192c47b1e053ad994f1afe` |
| `.local-test-evidence/2026-09-12/p33-d/arbitration-red.log` | `10aedc8366690f1ed973d25eb246c7f6b95313f56561b58015ae17d55ba52671` |
| `.local-test-evidence/2026-09-12/p33-d/binding-error-red.log` | `1f6897f475714ba2a3bd01226d336b888e34f3c8f1adc2202c04a3db648f3ce9` |
| `.local-test-evidence/2026-09-12/p33-d/integration-third.log` | `6ac1117464f9186fa53e4c0cb5d08026b2f9c8f5d5b6c6fe3f8b4ee675127f30` |
| `.local-test-evidence/2026-09-12/p33-d/p33-final.log` | `0f6ecdab4d82da3f20d5c8c65efb2b0eaa327f6520b6fac4f34667a63acb3e54` |
| `.local-test-evidence/2026-09-12/p33-d/mypy-final.log` | `fe0ce6e3b1983c3e150c09b415036599a0bee120f2cad38e219412ce21d84a67` |

### D 干净完整回归完成（18:51）

D 干净源码 `d3d3fd8650acc8b837dc4c0e093ab95068d054ff` 完整编排 **1119 passed / 8 skipped / 0 failed**（446.64秒），watchdog446.86秒，PG18131无残留。8项skip为未启用真实Provider。D为SDK源码里程碑，E–G、Host/wheel/真实flash仍未完成。

D18:04–18:51约47分钟，含实现、审查、定向验证和本轮完整回归；测试耗时7分26.64秒包含在内，不另算开发时间。无剩余独立审查P0/P1/P2，legacy grade AST未变，mypy91文件/Ruff/diff通过。待本次文档提交后短冒烟与推送。

- `.local-test-evidence/2026-09-12/p33-d/orchestrator-full.log` SHA-256 `a28110339f53c617eaa2154914cc95332f3e78e36971cfae3f1068962702704e`
- `.local-test-evidence/2026-09-12/p33-d/orchestrator-full.json` SHA-256 `1d30f55d02056b5dcf838c88cdd9a68b51f3179da0524fab95f902df2b46eafa`

### D推送与E启动（18:53）

fc43d6f提交后P33 542 passed /8.72秒（watchdog8.90秒），PG23067无残留，发送secret模式计数0。main已推送、与origin/main 0/0。D合计约49分钟。E独立挑战及先行oracle见slice-e-readiness，18:53正式启动。

## 2.5 切片 E 实施中

18:53启动，执行细化与先行oracle见slice-e-readiness.md；原始AC不改。主统一pytest，Kepler负责接受/人工写侧，Ohm负责来源依赖/检索，主负责实际runtime与摘要/context。

- 首条实际SDK反例：两个文档分支与实际synthesis均完成，但综合Knowledge没有source_versions并集，1 failed /0.57秒（lineage-runtime-red.log）。此流程实际读来源、写产物、调用确定性Critic，不是原生/真实Provider。
- 实际冲突流程反例：两有来源支持的反stance声明触发ConflictTask，Arbiter读两源/写报告/Critic通过，但只生成review而非arbitration；1 failed /0.50秒（arbitration-runtime-red.log，确切运行时间以日志为准）。旧的直接topic读取KeyError改为显式kind/topic比较，不改预期。
- source_commits初次11失败是fixture task_contract tuple无法JSON化，单列fixture-red（source-commits-red.log）。修列表后source+仲裁组合18 failed /15 passed /1.01秒：新接受未拦旧源、CAS故障未ERROR、缺来源并集/争议scope、普通review路径及成员变更等真实反例（commits-arbitration-red.log）。
- 新helper首次缺module属于dependency red，不算行为证明。实现后来源依赖+摘要+原step04检索/context **48 passed /5.67秒**（dependencies-first.log）。先前summary-legacy的rank.stale TypeError发生于新接口未接完，已被此组合绿集覆盖。
- 摘要只读过滤：knowledge及sources.knowledge排除stale，自身产出或真实used_knowledge命中的Task历史正文改为固定失效提醒，uncertainty保留ID/reasons并重算hash；无stale旧hash不变。consumer摘要先真实1失败/0.22秒，修后两项0.21秒；独立Ohm ACCEPT。_gather将ERROR转RetrievalUnavailable，并在已人工解决争议的context附上限定范围marker。

E仍未完成，接受/人工事务整合与独立审查、干净完整回归、文档推送尚待；无新wheel/Host/Provider调用。

### E 定向验证与审查收口（19:30）

实际来源接受+综合runtime13 passed /1.02秒；人工事务+runtime初批28 passed /2.60秒；初次P33/旧人工636 passed /19.03秒。之后新增两项旧review恢复，最终638 passed /21.01秒。上述重叠，不相加作为覆盖数量。

- 非人工两次FAIL后的耗尽仲裁实际red 1failed/3deselected0.68秒，修后1passed/3deselected0.65秒。此前测试漏传attempt history导致结果身份错误，修fixture后才得到有效行为反例。
- 旧普通审核批准恢复实际red 1failed/1passed/4deselected0.94秒；先前两次owner不同导致_verify返回False属于fixture问题。固定同owner并真实关库重开后，证明批准分支没有生成仲裁。修复只清除docConflict旧GRANTED的human PASS，保留有效非人工reuse和REJECTED语义。最终runtime6+旧human16合计22 passed /10.39秒。
- 两位独立审查最终ACCEPT，详见reports/code-review-e.md。mypy92 sourcefiles、修改Python Ruff通过。KnowledgeIndex.check与legacy grade函数源码比对基线不变。
- E仍待干净源码完整编排回归；无新wheel/Host/真实模型。

| 本地证据（p33-e/） | SHA-256 |
|---|---|
| `lineage-runtime-red.log` | `b69d8a73135b8e4943b45914763362ff93e2ad690a5946a2743e3c36b549e5a2` |
| `arbitration-runtime-red.log` | `d33d61544ff838a61470c936420e643f829444a348f411a1d58a369d228e1c03` |
| `commits-arbitration-red.log` | `e3ff08290d18691dfe6cc40f3ddadea09127fd4ccd7dd65f7d54e10ee5f220ea` |
| `dependencies-first.log` | `24742798d653224971ad7305d00af3563f2796da1f85262174f1533c9e954bb1` |
| `source-integration-first.log` | `0f1d2b885e58686f6084c885e65e1a59dfab432d03d5e21977463979800f7e84` |
| `arbitration-integration-first.log` | `cd77064ae311b926a4a915148d01b95928fb49e3e86216e8f93d476edefb7856` |
| `arbitration-exhaustion-red.log` | `1a44108673c5f0b8313fa37f8dbfe822a4ad0ae470b678ef874371075f7e047f` |
| `arbitration-exhaustion-green.log` | `671b0f74ab85aeaa379bef77b729329e9a9220b17f1f595b7fada0dccebf08df` |
| `legacy-review-red.log` | `f3a71fb0c8d857dd4a106c2ad50439d0e55eedd2a43d11e90c727b31306c9fc9` |
| `legacy-review-green.log` | `6cb33b51d06070a7de87bf4c97308b3a8a37da18a309411266a058e24f0341c8` |
| `p33-final.log` | `c490ddf6ebd03fa857046bc0eaa0538296ef313bdb7f00884b21d3b09a328415` |
| `mypy-final.log` | `4ceb57b647f65ad9bc4a2ac12b1f1d95323ae55da4304fb3e5014cf75bf56502` |

### E干净完整回归完成（19:38）

E 干净源码 `cf40b8ec86a2f307d0f8b8f89cf7f0166e5de121` 完整编排 **1199 passed /8 skipped /0 failed**（481.36秒），watchdog481.63秒，PG25036无残留。8项skip为未启用真实Provider。A–E完成SDK源码验证；F/G、wheel、Host与真实flash仍未完成。

E18:53–19:38约45分钟，含8分01.36秒完整测试。定向、独立审查、类型静态及架构回写完成；待文档提交后短冒烟与推送。尚未开始F正式计时；此前F/G只读准备与E重叠，不另加总。

- `.local-test-evidence/2026-09-12/p33-e/orchestrator-full.log` SHA-256 `f941d3ab742f639c6220c2e55a863b255dbd77226ee21f5450ac403c4efbcef3`

- `.local-test-evidence/2026-09-12/p33-e/orchestrator-full.json` SHA-256 `de67e2ed23db0412439ace70983c0ba720bc5a34853f52ef18916db7aaa0a207`

- `.local-test-evidence/2026-09-12/p33-e/process-check.json` SHA-256 `ff62e4a8f66af0e87b279755f146dad42c68c73a58358b99eb3f6c9476243062`

### E推送与F启动（19:40）

53a08ac文档提交后P33 622 passed /13.37秒（watchdog13.63），PG30480无残留。发送secret模式计数0，main推送成功。E18:53–19:40约47分钟。F19:40正式开始，见slice-f-readiness.md。G工作量上调为初步16–32工程小时，原2–4小时估计撤回；实际需SDK/Host多层接线和最终新制品，尚未完成。

## 2.6 切片F进行中（19:55）

候选ab09885（两个runtime0.11.0）首次整仓：80failed /3209passed /14skipped /18errors，546.07秒，watchdog546.50秒，PG30733无残留。没有ignore/maxfail，全部收集错误保留。实际原始红集98nodeid已落本机current-reds.json。

同依赖环境以a4aae8c归档源码和原tests逐项复跑98项：75failed /5passed /18errors，7.56秒，PG36140无残留；两个包实际导入归档src，identity文件保留。baseline-red.json继承runner的source_head字段表示控制仓ab09885，已追加actual_source_head与说明；原log不改，不把控制仓HEAD混称旧源码。

5项新增失败全部是发布版本快照遗漏：current public-api.json仍为0.10.0；只更新version→0.11.0，其余导出列表逐字未变，历史snapshot未动。独立Kepler ACCEPT。

15项agents失败在新旧源码均为缺可选tiktoken。本机测试env安装tiktoken0.14.0/regex2026.9.10，未改变SDK生产依赖。API快照+相关agents合计21 passed /4.64秒。剩余60失败、18错误仍须同依赖逐项对照及最终干净整仓核实，不宣称全绿或“等于原73”。

历史差额初查：原73=58fail+15旧版target setup；当前额外2个runtime memory committed断言同样在a4aae8c复现，另3个Memory SDK缺包collection也独立登记。原机汇总不取代本机实测。

- `.local-test-evidence/2026-09-12/p33-f/repository.log` SHA-256 `569c09b7a09e8bd127d2c1695d3e4147898e7e766f997fc92dc38b3fd67ede9b`
- `.local-test-evidence/2026-09-12/p33-f/repository.xml` SHA-256 `c51d5178d8f6691639c56a49b244b954780b846b993d1588e54e267c9a8da244`
- `.local-test-evidence/2026-09-12/p33-f/repository.json` SHA-256 `1f86a13bf4dee68c23ed5e46f11236b8a33f57d8360967932ed6494ca6fc6f80`
- `.local-test-evidence/2026-09-12/p33-f/current-reds.json` SHA-256 `f7d67de856690a171bb1ec3e2302d1a40edb257f4a61e6d3424d145f6da0c1db`
- `.local-test-evidence/2026-09-12/p33-f/baseline-identity.json` SHA-256 `c900a2a27920304fdf2449e1959b932875f1604787735aac0cba400b2c991326`
- `.local-test-evidence/2026-09-12/p33-f/baseline-red.log` SHA-256 `fca8573adaa8325e096f55b95bda36a18c678c8829041a71d6717f8fc2bde5a3`
- 原首轮 `.local-test-evidence/2026-09-12/p33-f/baseline-red.xml` 已被第二轮runner覆盖，以下旧哈希仅存历史、不作为可用证据；首轮证明使用完整baseline-red.log/JSON。原 SHA-256 `bc92048f6753ee63c5b2a31938f41b36882f67a4299d7d0b2b5603aa6622ae66`
- `.local-test-evidence/2026-09-12/p33-f/baseline-red.json` SHA-256 `c2cd59a5dffd46466e62c403e64c250ebcf3a59d2953f3225ef286af5680f87f`
- `.local-test-evidence/2026-09-12/p33-f/environment-and-version.log` SHA-256 `ba10dc734c4d2953c982e6573d246054da7f50bb31be9971a6f176fe3d7eb076`

### F最终验证（20:17）

F 验证完成（保留既有红集）：干净源码 `5bcca08fe666b8e20524206b76ce2afbba63db4d` 整仓3241 passed /60 failed /18 errors /13 skipped（547.16秒）；与同依赖旧源码a4aae8c的78项红集按kind+nodeid完全相同，新增0。0.11.0安装验证1402 passed /11 skipped /1既有迁移失败（506.99秒）；304包文件逐字匹配，258实际加载模块均来自安装包且哈希一致。F不是整仓全绿或新正式发布；G、Host与真实flash仍未完成。

- 新旧源码相同测试依赖下红集精确相等，red-comparison.json双向差集为空。raw78比原机73多2条同样在a4aae8c的runtime Memory断言及3条缺Memory SDK收集错误，差额未隐藏。
- baseline补依赖后复跑98项：60failed /20passed /18errors /7.44秒，PG36326无残留。原21控制通过，tiktoken还使原test_context_journal整个模块开始收集，新增12通过；因此final为3241而非仅3229。
- final完整pytest547.16秒（watchdog547.59），PG36349无残留。安装组合506.99秒（watchdog507.50），PG41614无残留；唯一红assert10==7与baseline逐字原因相同。两轮全量测试时间546.07+547.16秒，重叠覆盖不相加计数。
- 可复现wheel757d6fb1…，source5bcca08，SOURCE_DATE_EPOCH=0。twine、依赖兼容、304包文件bytes、258实际module来源/hash检查通过。静态制品独立审查确认sdist698预期输入匹配。没有tag/publish/newHost/真实模型。
- 证据修正：第二次baseline runner复用了XML固定路径，第一XML已被覆盖。首轮完整log、JSON、原始红集与import identity均保留；第二XML存为baseline-after-dependency.xml，correction.json明确此事，后续runner已按label分离XML/temp。不得引用首轮已不存在的XML作为实物证明。
- 本片总计待推送后记录；G准备期间与F测试重叠，不另加耗时。

- `.local-test-evidence/2026-09-12/p33-f/repository-final.log` SHA-256 `2323f27910a99526d193ca432cf926a4c2636c5b12911c28b7458cc58d4b9709`
- `.local-test-evidence/2026-09-12/p33-f/repository-final.xml` SHA-256 `546eefcf265171fc4ae5b0f18e240e7c0f175fe039cfc404ebf3ff110a9f9f35`
- `.local-test-evidence/2026-09-12/p33-f/repository-final.json` SHA-256 `7c1523f48ac41d3c5d942aa26eae9481013e82d0ae5572666ad39d56f5d869ec`
- `.local-test-evidence/2026-09-12/p33-f/red-comparison.json` SHA-256 `3c76bbb92d2c5bf1bb73ccb520349b427b90dfb921249223b04771aeabf4860a`
- `.local-test-evidence/2026-09-12/p33-f/baseline-after-dependency.log` SHA-256 `5336b2866e44e0eba097ae3bb1f8fbfd9c24f0af7d8281bf4ba7f74d7df053ef`
- `.local-test-evidence/2026-09-12/p33-f/baseline-after-dependency.xml` SHA-256 `edee503c03b70694dec274b03f5ed0f4c8f9eb186d430b84db2aa819f9e1586b`
- `.local-test-evidence/2026-09-12/p33-f/baseline-after-dependency.json` SHA-256 `a7dc34002da5d87305c0d046ec8f27259e70fdc2d61a1358d23909f54099eaea`
- `.local-test-evidence/2026-09-12/p33-f/baseline-xml-correction.json` SHA-256 `ea745bcd29346b53cdd12ea62b028533a467bc13d840401a5dc5c846580c0894`
- `.local-test-evidence/2026-09-12/p33-f/build.log` SHA-256 `0bcf5cb54fd26e3b3654c5c24846ee681508c3509c4e71066729d69b95157dbd`
- `.local-test-evidence/2026-09-12/p33-f/candidate/candidate-manifest.json` SHA-256 `20fc0e86bb06fe807eb7a853a0b4b2d95449a01fb8efbf6764ca6d48c38b4c74`
- `.local-test-evidence/2026-09-12/p33-f/twine.log` SHA-256 `3d2aeaf0e398fdece2c508467c1ff9c12a036a7956aa935cbd603150a28cd769`
- `.local-test-evidence/2026-09-12/p33-f/installed-preflight.json` SHA-256 `372cc8d97fed7447e0b3324b940d7540cb726c8299e1ace64e6a501312163ab6`
- `.local-test-evidence/2026-09-12/p33-f/installed-full.log` SHA-256 `6f47c42b8314a3076c616cfe19ee4bffebffec48161a610e4cd75a6d7fb5db6e`
- `.local-test-evidence/2026-09-12/p33-f/installed-full.xml` SHA-256 `6bd111d939fcfb5ec66f823cd923f38201af35855d089e6cbb8eb4b775b0f3cc`
- `.local-test-evidence/2026-09-12/p33-f/installed-full.json` SHA-256 `a14dd2cebebb12cc75ac2a29d0ed5fa714e4d885130f14efd86defad37213d89`
- `.local-test-evidence/2026-09-12/p33-f/installed-origins.json` SHA-256 `1d7153c4dbacda1af4f0cf72d0fc1fca5fd88965e43e7feda96fd4d504144e35`
- `.local-test-evidence/2026-09-12/p33-f/installed-process-check.json` SHA-256 `af2488be5442025f8dcdb397177f0e01d9115a90b41b2405530f5e31008b0842`
