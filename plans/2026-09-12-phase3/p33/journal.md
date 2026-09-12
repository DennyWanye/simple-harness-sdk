# P3.3 非代码 Mission 与证据闭环 · 记录

## 0. 交接（冷会话先读这一节）

- **当前位置**：计划第 3 版已定稿（两轮各两位独立评审，四份原文在 `reports/`，处置表在 `plan.md` §7）。**切片 A、B 已完成（SDK 源码）；下一步 C**。
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
