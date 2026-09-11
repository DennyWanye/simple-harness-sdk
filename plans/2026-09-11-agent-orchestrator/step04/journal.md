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
