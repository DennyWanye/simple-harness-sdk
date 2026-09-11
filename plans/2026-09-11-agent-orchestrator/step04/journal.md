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

## 4. 证据

- **fixtures 演示**：`python -m agent_orchestrator demo --scenario knowledge-sharing --provider fixtures --evidence-dir <dir> --idempotency-key demo-s4 --max-concurrency 1` → Mission COMPLETED（verification_passed，2.18 s）；任务 [work, work, work, synthesis, conflict] 全部 COMPLETED 各 1 次 Attempt；冲突 `impl_a.empty_input` RESOLVED；知识 6 条 VERIFIED（A 的两条被 B 与综合任务复用、B 的两条被综合任务复用、仲裁结论、综合一致性结论）；血缘含 6 条知识；证据目录含 §14.3 的 7 个文件 + `knowledge.json` + `lineage.json` + 各 Attempt 产物。
- **确定性测试**：`tests/orchestrator`（step02 + step03 + step04）101 passed, 3 skipped（真实模型 opt-in）；step04 37 条。
- **SDK 全量回归**（脚本 scratchpad `regress/run.sh`，忽略 3 个 memory-sdk 模块）：HEAD `043e8e0` 58 failed / 2141 passed / 8 skipped / 15 errors；review 处置后 HEAD `7303469` 58 failed / 2147 passed / 8 skipped / 15 errors；两次红集都是 73 条 = 基线 `baseline-known-failures.txt`，**0 新红**。

## 3. 独立 review（代码）

独立 review（claude-opus-5，只读；原文 `reports/code-review-round1.md`）：1 P0、4 P1、13 P2；15 项核查通过（唯一写入者、accept 原子、§25.3/Task 状态机无新边、`ordinal ≡ 拓扑序`、版本幂等、两段引用校验、整树 pytest 不定级、双实例幂等、可见性模板等）。处置：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| P0-1 | P0 | 冲突任务预算只给 tokens/attempts，Mission 限定其它维度时 `open_account` 抛 `BudgetError` 回滚整个 accept、异常逃出循环 | `inherit_limits`：系统模板继承 Mission 所有受限维度；`_open_conflict` 捕获 `BudgetError` → `ConflictOpenDeferred(budget_unavailable)`，accept 永不因预算回滚（`test_step04_review_round1.py` 前两条） |
| P1-2 | P1 | 综合模板预算同源问题，且 `validate_graph` 不校验它 | 同上继承；`validate_graph` 校验综合模板预算 `fits_within`（`GraphRejected(budget)`，第三条测试） |
| P1-3 | P1 | `is_untrusted` 不折叠 `./`，`./docs/x` 绕过标记 | 新 `artifacts/paths.py::normalise_workspace_path/under_prefix`，网关与分级共用一套归一化 |
| P1-4 | P1 | `pytest:./docs/x` 被判可信 → SUPPORTED | 同上；负例覆盖 `./docs`、`docs/../docs`、`pytest:./docs` |
| P1-5 | P1 | v1 库含 L3-2 重复行时唯一索引建不起来 | 迁移 v2 前 `_renumber_artifact_lineage`（按 created_at, artifact_id 重排 version，id/hash 不变）；含重复行的 v1 库测试 |
| P2-6 | P2 | trust 维度恒为常数 | `TRUST={"VERIFIED":1.0}` 并在 plan §6.1 登记"本版本只排 VERIFIED" |
| P2-7 | P2 | 去重缺 `duplicate_of` | `dropped["duplicate_of"]` 记录被丢弃 id → 代表 id |
| P2-8 | P2 | verifier 拿到争议正文而非证据引用 | `disputed_claims` 带 `evidence`；verifier 模板去掉 `content` |
| P2-9 | P2 | `_open_conflict` 末尾 `_unblock` 空操作且注释误导 | 删除该行，注释改为事实（由 accept 的 `_unblock(unblocked_by=task)` 解锁） |
| P2-10 | P2 | `check_arbitration` 不校验探针路径前缀 | 探针必须在 `arbitration/<key>/` 下 |
| P2-11 | P2 | Raw Logs 无消费者；`foreign_ids`/`useful_for`/`branch_summary_for` 死代码 | synthesizer 包加 `raw_logs` 引用（`Blackboard.raw_refs`）；删除三处死代码 |
| P2-12 | P2 | `assert_no_secrets` 只查键名；预留只覆盖 token 维度 | 登记 §6.1 已知边界（凭证断言是字段级；成本维度无系统预留，第 6 步预算硬限时一并） |
| P2-13 | P2 | 取代时抬高被取代记录 version | 不再抬版本（version 是身份） |
| P2-14 | P2 | 三处恒真/死代码断言 | 清理 |
| P2-15 | P2 | `Task` 加 dict 字段后不可哈希 | `Task.__hash__ = hash((id, version))` |
| P2-16 | P2 | R11 不可达性无断言 | 闭环测试断言所有 `ConflictOpened.seq` < 终结任务 `TaskCompleted.seq` |
| P2-17 | P2 | D4-21 改变 S3-08 语义、旧路径无覆盖 | step03 acceptance 加注（S3-08b 改为真实耗尽）；`head_room ≤ 0` 分支即 S3-08b 所走路径 |
| P2-18 | P2 | Judge 消歧只在 plan | `judge_mission` docstring 写明与理论 04-7 的 Judge 不同 |

真实运行发现（与 review 无关、同批修复）：SDK turn 失败时 `error.output_cap_escalations` 是元组，`reject_result` 的 `_object` 校验抛 `ContractValidationError` 逃出 `run()`（运行 2）；新增 `contracts.jsonable` 在进入正式记录前把 SDK 结构转成纯 JSON（`reject_result`/`record_planning_rejected`/`mark_attempt_timed_out`/`fail_planning` 入口统一做）。运行 1 的 `max_cycles` 只计进展轮次已在 `8cdb06b` 修复。
- **wheel 0.9.2**（脚本 scratchpad `wheel-0.9.2/build-and-verify.sh`，源提交 `7303469`，`SOURCE_DATE_EPOCH` 可复现）：`simple_harness_sdk-0.9.2-py3-none-any.whl` sha256 `c58696265288cf9efe5528c5788d5320ce8354a736e5bc232f85388a0019c0d1`；干净 venv（Python 3.12）安装后 `tests/orchestrator tests/agents tests/unit/contracts` + 迁移测试：370 passed / 6 skipped / 1 failed——唯一失败 `test_execution_v3_to_v4_migration.py::test_completed_null_continuation_*` 是基线已知红（`baseline-known-failures.txt` 第 23 行，0.9.1 wheel 验证时同样失败）；安装态 `demo --scenario knowledge-sharing --provider fixtures` → COMPLETED（verification_passed）。
- **真实模型运行**（用例 `test_real_provider_knowledge_sharing.py --run-real-provider`，凭证 `deepseek.env`，脚本 scratchpad `real-s4-run{1..5}/run.sh` 带 `\bsk-` 脱敏；报告 `reports/real-knowledge-sharing-run{1,2,3,5}.md`）：
  - 运行 1（pro，534 s）：`run(max_cycles)` 把等待轮次计入上限而提前返回 → 修复 `8cdb06b`；顺带 Planner 包给出 `budget_for_tasks`（`986820d`）。
  - 运行 2（pro，329 s）：SDK turn 错误里的元组进入正式记录抛 `ContractValidationError` → 修复 `contracts.jsonable`（`7303469`）。
  - 运行 3（pro，499 s）：知识复用成立（8 个 `KnowledgeUsed`，16 条 VERIFIED），综合任务两次信封不合规 → 模板重述完整 schema（arbiter-v2 / synthesizer-v2，`098703b`）。
  - 运行 4（`deepseek-v4-flash`，16 s）：端点回显 `deepseek-flash` → D10' `model_echo_mismatch` 停止；DeepSeek 官方端点 `/models` 只有 `deepseek-flash` 与 `deepseek-v4-pro`，用户指示的 flash 模型 id 是 **`deepseek-flash`**。
  - **运行 5（`deepseek-flash`，192 s）：Mission COMPLETED（verification_passed）**：6 个 Task 全部 COMPLETED（4 个 Planner 任务 + 综合 + 系统开出的冲突任务）；Claim 分布 VERIFIED 28 / SUPPORTED 1 / UNDER_REVIEW 2 / DISPUTED 1 / SUPERSEDED 1，知识 29 条；`KnowledgeUsed` 33 次；综合任务的一条 Claim 与已验证知识同 key 反 stance → `ClaimDisputed` → `ConflictOpened`（预留 300k）→ Arbiter 探针 `arbitration/comparison.summary/test_probe.py` → `ConflictResolved(basis=code_test)`（被取代 1 条）→ Mission 判定 pytest/file 均 met；血缘含终结任务、知识、Agent 与争议 Claim；结算 481,520 tokens（unpriced）。本步真实模型要求的两条证据（知识被复用 + 综合再验收）都成立，冲突→仲裁→外部验证也在真实模型上走通。

## 5. 遗留

| # | 事项 | 归属 |
|---|---|---|
| L4-1 | 冲突检测只识别显式 `contradicts` 与同 key 反 stance；语义相反但 key 不同的 Claim 不识别（plan §6.1） | 第 5 步（Critic 冲突报告）/ 第 8 步评测 |
| L4-2 | 检索是确定性词元打分、只排 VERIFIED（trust 因子恒定）；无 embedding；`max_knowledge_items=12` 之外的知识只在 `dropped.over_limit` 里可见 | 第 6 步（规模）/ 第 8 步（评测检索质量） |
| L4-3 | 系统预留只覆盖 token 维度；成本维度上综合/冲突任务无预留；`assert_no_secrets` 只查字段名 | 第 6 步（预算硬限、密钥扫描） |
| L4-4 | 真实模型下 `provider_protocol_error/tool_parse`（DeepSeek 工具调用解析失败）频繁触发 turn FAILED → 重试；重试可用但每次浪费一个 Attempt 与费用；SDK 侧无自动纠错 | 第 6 步（模型路由/升级策略） |
| L4-5 | 真实模型的同 key 知识重复（汇总任务把探针结论重新提交为自己的 Claim → 同 key 同 stance 的第二条 VERIFIED 知识，检索去重后只提供一条）；是否应在 accept 时合并为复用而不是新知识 | 第 5 步 |
| L4-6 | `knowledge_sharing=False` 只在 fixtures 上测过；真实模型下未跑 | 视需要 |
| L4-7 | 第 3 步遗留 L3-1（真实 Planner 偏好链式拆分）、L3-3～L3-6 未变；L3-2 已在本步关闭 | 各自归属 |
| L4-8 | 真实运行中 Mission 判定的 `pytest:tests/test_comparison.py` 目标要求 `comparison.json` 的 `knowledge` 非空——这是演示种子对综合产物的约束，不是通用规则 | 演示范围 |
