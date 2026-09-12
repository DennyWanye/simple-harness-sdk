# Agent 编排框架 · 交接（Phase3 进行中）

- 日期：2026-09-12
- 上一份交接（`plans/2026-09-11-agent-orchestrator/HANDOFF.md`，停在第 2–9 步收官）已删除，本文件取代它。
- 仓库与分支：
  - SDK `simple-harness-sdk`，`main`（本文件所在仓库）
  - Host `simple_harness`，`main` = `04350956`，**干净且与远端同步；P3.3 还没有碰 Host**
- 长期规则：专业术语先查 Host `plans/taskSys2/agent-orchestration-theory/` 的定义再写代码；真实模型只用 `deepseek-flash`（**绝不**用 deepseek-v4-pro）；记录与回复一律中文；技术取舍交独立评审子代理裁决并记录；测试先行；回归红集 ⊆ 基线 73；每切片提交推送并同步更新本文件；不用 `git stash`；同一时间只跑一个 pytest。
- 安全：绝不打印或提交 API 密钥。本机真实测试从 Host 主仓 ignored `.env` 的 `DEEPSEEKER_APIKEY` 注入进程；该字段已确认存在。旧机 `.local-test-evidence/2026-09-07/credentials/deepseek.env` 本机不存在，不要据此判断无 key。模型固定 `deepseek-flash`，endpoint 按真实测试配置核实，密钥不复制到配置或证据。提交前扫 `\bsk-[A-Za-z0-9_-]{20,}` **只打印计数**。

---

## 1. 整体进度

| 版本 | 状态 |
|---|---|
| 第一阶段 BaseAgent（S1–S5）+ Host 钉版 | ✅ SHIPPED |
| 第二阶段 ORCH-BUILD 第 2–9 步 | ✅ SHIPPED |
| Phase3 **P3.1** 真实 App Mission 控制闭环 | ✅ SHIPPED |
| Phase3 **P3.1 遗留修复** | ✅ SHIPPED（SDK 0.9.11） |
| Phase3 **P3.2** 隔离执行与真实受控交付 | ✅ SHIPPED（SDK 0.10.0，SDK `48e441a`，Host `04350956`） |
| Phase3 **P3.3** 非代码 Mission 与证据闭环 | 🔨 **进行中——计划第 3 版已定稿，切片 A、B 已完成 SDK 源码验证；C 已实现、干净全量验收中** |
| Phase3 P3.4 / P3.5 | 未开始 |

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
| C | schema v10 `criterion_assessments`、评估记录传递、`grade_claim` v2、attribution 记录/消费/压制通道（Host 交付层仍 G） | 已实现；定向 445 passed / 1 skipped，干净全量待完成 |
| D | adapter 常量表、三个文档 adapter、**层状态上的硬约束**、INCONCLUSIVE 七条边界、结构化 `limitations`、Mission 级 INSUFFICIENT | 未开始 |
| E | 冲突范围加注、文档领域人工裁决、`KnowledgeIndex.stale`、检索排除 | 未开始 |
| F | 全量回归、wheel 干净环境验证 | 未开始 |
| G | Host 钉版、接线、系统渲染结论区、真实 flash 原生验收 | 未开始 |

**每个切片完成即跑 `tests/orchestrator` 全量**，不等切片 F。

---

## 3. 切片 A、B 已完成，下一步 C

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
- B 证据和 SHA-256 索引见 `p33/journal.md` §2.2；下面 A 的 651 项是历史切片基线。C 已实现并有真实 SDK 定向链路验证，干净全量待完成，P3.3 整体及 wheel、Host/真实模型仍未验收。

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
