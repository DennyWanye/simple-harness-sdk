# 第 4 步 · 执行记录

## 1. 关键裁决

独立 review（claude-opus-5，只读；原文 `reports/plan-review-round1.md`）23 条发现，处置落在 plan §6 / §6.1：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| R1 | P0 | 综合任务 READY→BLOCKED 是非法边 | D4-8'（依赖永不改；冲突对综合任务是**门控**，`SynthesisGated`；accept 内再守一次） |
| R2 | P0 | 动态 Conflict Task 破坏 `ordinal ≡ 拓扑序` | D4-7'（冲突任务是拓扑序末尾的叶子，不成为任何任务的依赖；终结任务 = 综合任务或最后一个非 conflict 叶子） |
| R3 | P0 | VERIFIED→DISPUTED 非法边、与投影矛盾 | D4-6'（冲突优先于分级；命中冲突封顶 DISPUTED 且不投影；VERIFIED 对手只记 `disputed_by`） |
| R4 | P0 | 冲突任务预算绕过 §18.2、accept 可能回滚 | D4-20（`conflict_reserve_tokens` 显式预留；Σ 检查含系统任务；余量不足 → `ConflictOpenDeferred`，accept 永不因预算回滚） |
| R5 | P1 | rule_check 无知识索引、验证期与 accept 的 TOCTOU | D4-4'（切片 A 已给 `rule_check(knowledge=)`；accept 内二次校验，不通过按 FAIL） |
| R6 | P1 | 整树运行前缀匹配退化成全 VERIFIED | D4-2'（空/整树目标不覆盖任何 Claim；负例测试） |
| R7 | P1 | 迁移表校验只认单行、UNIQUE 不能 ALTER | D4-15'（切片 A 已实现：历史行保留、前缀校验、备份、UNIQUE INDEX） |
| R8 | P1 | block 策略不可观察、停止原因非法、Mission 结局未写 | D4-11'（事件算进展；计数持久；`RETRIEVAL_UNAVAILABLE` 入枚举；Mission FAILED 写进 S4-07） |
| R9 | P1 | 冲突任务无 critic_review | D4-7'（加 `critic_review`；Critic 看双方证据不看自述；S4-03 断言两层 PASS） |
| R10 | P1 | 缺 Verifier 模板、explorer 无消费者 | D4-10'（verifier 模板启用于 critic_review 层；explorer 只登记） |
| R11 | P1 | 综合已 COMPLETED 后的冲突无路径 | D4-8'（静态图里构造性不可达，测试断言；accept 内 OPEN 冲突守卫） |
| R12 | P1 | 迁移/契约清单不全 | D4-15'（切片 A 已含 `Claim.stance/proposed_by/…`、`Task.kind/context`；哈希影响说明） |
| R13 | P1 | 冲突任务产物契约未定义 | D4-7'（`arbitration/<key>/` 前缀，`outputs` 声明） |
| R14 | P2 | 实施约定冒充原文 | plan §6.1 登记表 |
| R15 | P2 | Provenance 漏"由谁修改过" | §6.1（知识不可变，由 superseded_by/resolves/confirmed_by/disputed_by 链回答） |
| R16 | P2 | Judge 同名异义 | §6.1 消歧 |
| R17 | P2 | Raw Logs 层无落点 | §6.1（`Blackboard.raw_refs` 只读引用；包内无凭证断言） |
| R18 | P2 | 分支归属多根不确定 | D4-13'（ordinal 最小的根；多根归 global） |
| R19 | P2 | 冲突任务直接 READY 偏离约定 | D4-7'（先 BLOCKED 再同事务 `_unblock`） |
| R20 | P2 | 版本重分配幂等性 | D4-15'（幂等短路之后分配；已登记产物不抬版本；测试） |
| R21 | P2 | VERIFIED 只绑 code_test | §6.1 登记为收紧约定 |
| R22 | P2 | attempts 默认值、命令形式、证据文件、关闭开关 | D4-7'（max_attempts=2）、acceptance 附加门槛、D4-19 `knowledge_sharing` 开关 |
| R23 | P2 | explorer 模板与 `mission lineage` CLI 蔓延 | D4-10'、D4-17'（CLI 推迟第 8 步） |

## 2. 执行记录

| 切片 | 提交 | 内容 | 测试 |
|---|---|---|---|
| A 知识层 | `e564176` | Claim 提案扩展（key/stance/evidence/supersedes/contradicts）、Claim/Task 契约扩展、schema v2（迁移历史 + 备份 + 知识/摘要/冲突表 + 产物血缘唯一索引）、`memory/{claims,verified_knowledge,blackboard}.py`、accept 内分级与投影、`used_knowledge` 校验与复用链、合法取代、L3-2 | `test_claims_knowledge.py` 8、`test_schema_migration.py` 2、`test_artifact_versions.py` 1 |
| B 检索与上下文 | `c1cff5d` | `context/retrieval.py`（确定性打分：相关性 3 / 可信 2 / DAG 距离 1 / 新旧 0.5 / 复用 0.5，重叠系数相关性，同 key+stance 或同规范化内容去重，SUPERSEDED 只带 `superseded_by` 标记列出，Mission 边界预过滤，`retrieval-v1`）；Context Builder `context-builder-v3`：§10 全部 11 项，worker / verifier / critic / arbiter / synthesizer 模板（explorer 只登记），verifier 不含提交者 summary/confidence，`assert_no_secrets`；`context/compression.py` + `memory/summaries.py` 确定性摘要（分支归属 = ordinal 最小的根，多根归 global；带 sources/version/uncertainty；accept 事务内重算持久化）；检索失败策略 `on_retrieval_failure=block|degrade`（事件 `RetrievalUnavailable` 计入进展、计数由事件派生、超限 `retrieval_unavailable` 停止）；开关 `knowledge_sharing`；网关 `workspace_read_file` 对 `untrusted_sources` 前缀返回 `trust=untrusted_external` + notice；ARBITER / SYNTHESIZER 模板，WORKER v2 / CRITIC v2；intent 冻结 `knowledge:[{id,version}]`、`retrieval_version`、`context_builder_version`、`role`、`untrusted_sources`；fixtures `per_attempt` 脚本 | `test_retrieval_context.py` 11（排序、模板、摘要、S4-02、S4-06、S4-07×3、S4-08） |
| C 冲突仲裁 | `ff7337b` | `verification/conflicts.py`（显式 `contradicts` 或同 key 反 stance；只对已接受结果的 Claim；同 Task 不算；非投票）；accept 事务内**冲突优先于分级**：命中即封顶 DISPUTED 且不投影，VERIFIED 对手只记 `disputed_by`；`planning/manager.py::conflict_task`（末尾叶子、依赖来源 Task、BLOCKED→`_unblock`→READY、`arbitration/<key>/` 产物、`arbitration:<key>` + `pytest:<probe>` 准则、format/rule/critic/code_test、max_attempts 2、priority 10）；预算来自 `conflict_reserve_tokens`（余量记 `final_report.conflict_reserve_remaining`，不足/无预留/开关关闭 → `ConflictOpenDeferred`，accept 永不因预算回滚）；仲裁 Claim VERIFIED → `resolves`/`resolved_by`/`confirmed_by`/`superseded`，`ConflictResolved(basis=code_test)`；`check_arbitration` 规则（恰一条同 key Claim、须 pytest 证据）；冲突任务失败 → 冲突 UNRESOLVED | `test_conflicts.py` 6（含 S4-03 闭环） |
| D 综合 | `ff7337b` | `MissionSpec.synthesis` → Graph Commit 追加 `kind=synthesis` 任务（依赖全部 Planner 叶子，永不改）；`system_reserve_tokens`（综合预算 + 冲突预留）进入 `normalise_budgets` 与 §18.2 Σ 检查；OPEN 冲突门控综合任务（`SynthesisGated`，不改状态）+ accept 内守卫（`synthesis_blocked_by_open_conflict`）；`rule_check` 要求综合结果 `used_knowledge` 非空（开关关闭时不要求）；`terminal_task()`（综合任务，否则最后一个非 conflict 叶子）用于判定与血缘 | `test_synthesis.py` 4（含 S4-05 闭环） |
| E 闭环与演示 | `73af55d` | `observability/lineage.py`（终结结果 → used_knowledge → 知识 → 来源 Attempt/Agent → 递归；resolves / supersedes / confirmed_by 边；争议 Claim 作为路径上的历史）写入 `final_report.lineage` 与证据 `lineage.json`；证据 `knowledge.json`（知识/冲突/摘要）；CLI `demo --scenario knowledge-sharing`（fixtures/env）报告含知识、冲突、血缘摘要；端到端闭环（S4-01/03/05/08 同一 Mission）、CLI 测试、真实模型 opt-in 测试 | `test_knowledge_sharing_closure.py` 1、`test_cli_knowledge_sharing.py` 1、`test_real_provider_knowledge_sharing.py`（opt-in） |

实现中的裁决（补充 §1）：
- **D4-21 修复 Attempt 的预留按剩余额度**：冲突任务预算 = 预留 20k，第一次 Attempt 预留 20k 结算 300 后剩 19.7k，第二次（意见被拒后的重试）若仍按名义份额 20k 预留必然 `budget_exhausted`。裁决：预留 = min(名义份额, 账户剩余 − Critic 份额)，剩余 ≤ 0 时仍按 §18.3 失败停止（S3-08b 改为真实耗尽：坏 Attempt 两次调用结算 21k > 20k，断言不变）。预留是上限不是支出，与 ORCH §12.2 一致。
- **Critic 预留失败 = 必需层 ERROR**：`_run_critic` 的 `BudgetExhausted` 转成 `ContractError` → `critic_review` 层 ERROR → 结果 FAIL（"未运行的必需层不能算 PASS"），不再让异常逃出循环；Mission 判定阶段的 Critic 无预算 → "no independent judge ran"。
- **同 key 同 stance 的仲裁结论与被确认知识**：检索按主题去重只提供一条（被确认的原知识），综合引用的是原知识；血缘沿 `confirmed_by` 到达仲裁知识与 Arbiter 的 Agent（30-27 的"依赖的 Agent"包含仲裁者）。
- **fixtures 脚本必须按 Attempt 分配**：两个 Mission 的同 key Worker 并发时共享平铺脚本会互相消费；`TaskRoutedProvider(per_attempt=…)` 在 Attempt 首次调用时分配一份脚本。顺带观察：脚本耗尽时 provider 抛 AssertionError，SDK 把它归为 UNKNOWN 出站调用（阻塞直到 `stall_seconds`），与 S2-08 语义一致，不是编排层缺陷。
- **外部文档在 Mission 种子里**：`workspace_seed` 作为 Mission 规格的一部分进入 `final_report`，证据 `final_state.json` 里必然含文档正文；"不内联"的断言对象是上下文包（intent `message`）与 Blackboard（`knowledge.json`），两者都不含 `SYSTEM NOTICE`。
