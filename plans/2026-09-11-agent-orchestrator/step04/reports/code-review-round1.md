# 第 4 步 · 独立代码评审（round 1）

- 日期：2026-09-11 · 评审人：独立子代理（claude-opus-5，只读）
- 评审对象：`git diff d7a3bd0..HEAD -- src/agent_orchestrator tests/orchestrator/step04 tests/orchestrator/step03`
- HEAD：`986820d`（评审开始时为 `73af55d`，过程中新增 `043e8e0`、`986820d`，二者已一并覆盖）
- 锚点：step04 `plan.md` §3/§6/§6.1、`acceptance.md` S4-01…08、`journal.md` §1/§2；Host ORCH-BUILD §6/§12/§13；原文 §10/§11/§14.3–14.4/§15/§21.3/§25.3
- 本报告只记录有可核实依据的发现；P0 与 P1-3/P1-4/P1-5 附实机复现

---

## P0

### P0-1 冲突任务预算不继承 Mission 的其它受限维度 → 合法 accept 被回滚（违反 D4-20 / R4 的 P0 处置）

**发现**

`conflict_task()` 只构造 `Budget(max_tokens=tokens, max_attempts=max_attempts)`，`max_cost_micros` / `max_runtime_seconds` / `max_concurrency` 一律留 `None`。而 `Budget.fits_within()` 的语义是"父级限定了某维度而子级为 None 即不通过"，`BudgetLedger.open_account()` 在这种情况下抛 `BudgetError`。该调用位于 `_open_conflict` → `_dispute` → `_grade_and_project` → `accept_result` 的**同一个事务**内，异常会把整个 accept 回滚。

这与 D4-20 明确写下的"**accept 事务永不因预算失败回滚**"（journal §1 R4 的 P0 处置）直接冲突。

更糟的是 `BudgetError` 继承自 `StoreError`，既不是 `CommitRejected` 也不是 `StoreBusy`/`IllegalTransition`，不在 `_cycle` 的捕获集合里，会一路逃出 `Orchestrator.run()`；而且失败是确定性的（同一提案每轮都会走到同一行），Mission 会永久卡死而不是降级为 `ConflictOpenDeferred`。

**依据**

- `src/agent_orchestrator/planning/manager.py:69` — `budget=Budget(max_tokens=tokens, max_attempts=max_attempts)`
- `src/agent_orchestrator/contracts/models.py:139-146` — `fits_within`：`if theirs is not None and (mine is None or mine > theirs): return False`
- `src/agent_orchestrator/governance/budgets.py:122-129` — `open_account` 不通过即 `raise BudgetError(...)`
- `src/agent_orchestrator/orchestrator/commit_service.py:1038-1044`（`_open_conflict` 里的 `open_account`）→ `:965`（`_dispute` 调用）→ `:841-842`（`_grade_and_project` 调用）→ `:2031`（`accept_result` 事务内）
- `src/agent_orchestrator/governance/budgets.py:35-39` — `class BudgetError(StoreError)`
- `src/agent_orchestrator/orchestrator/event_handler.py:282-292` — `_cycle` 只捕获 `StoreBusy` / `CommitRejected` / `IllegalTransition`
- 实机复现（Mission 预算加一维 `max_cost_micros=1_000_000`，其余与 `knowledge_helpers.spec` 相同，A/B 同 key 反 stance）：

  ```
  ACCEPT RAISED: BudgetError task budget {'max_tokens': 20000, 'max_cost_micros': None,
    'max_attempts': 2, ...} exceeds parent {'max_tokens': 100000, 'max_cost_micros': 1000000, ...} (§18.2)
  task B status after rollback: VERIFYING
  result verdict: None
  conflicts: []
  ```
  即：B 的合法 accept 被整体回滚，Task 停在 VERIFYING，`ClaimDisputed` / `conflicts` 记录全部丢失。

- 为什么现有测试全绿：`knowledge_helpers.spec`、`test_conflicts.spec`、`test_synthesis.spec`、`COMPARE_SPEC`、`__main__.cmd_demo` 的 Mission 预算**都只设 `max_tokens` + `max_attempts`**（`tests/orchestrator/step04/knowledge_helpers.py:30`、`src/agent_orchestrator/testing/fixtures.py:708`、`src/agent_orchestrator/__main__.py:250`），恰好与 `conflict_task` 的维度集合重合，故这条路径从未被走到。

**建议处置**

1. `conflict_task()`（以及 `synthesis_task()`，见 P1-2）像 `normalise_budgets` 那样继承 Mission 在其它维度上的限额（未设则取父值）；
2. 同时在 `_open_conflict` 里把 `open_account` / 预算相关异常捕获成 `ConflictOpenDeferred(reason="budget_unavailable")`，落实 D4-20 的"accept 永不因预算回滚"——第 1 条是修因，第 2 条是把不变量写进代码而不是靠调用点自觉；
3. 补一条决定性测试：Mission 预算带 `max_cost_micros`（或 `max_runtime_seconds`）时冲突仍能开任务且 accept 成功。

---

## P1

### P1-2 综合任务模板预算同样不继承维度，且 `validate_graph` 从不校验它 → Graph Commit 抛裸 `BudgetError`

**发现**

`synthesis_task()` 的预算直接来自 `MissionSpec.synthesis["budget"]`（`Budget.from_json`），模板漏写任何一个 Mission 已限定的维度，就会在 `_commit_task_graph` 里 `open_account` 失败。`commit_task_graph` 只把 `GraphRejected` 转成 `CommitRejected`，`BudgetError` 会裸抛出去。

另外 `validate_graph` 的 §18.2 Σ 检查只遍历 `proposal.tasks`，**综合任务自身的预算从未被校验**——它只作为 `system_reserve_tokens` 的一个加数参与 Σ；若模板预算超过 Mission，也只会在 `open_account` 处以 `BudgetError` 而非 `GraphRejected("budget", ...)` 暴露，Planner 拿不到可读反馈。

**依据**

- `src/agent_orchestrator/planning/manager.py:97` — `budget = Budget.from_json(template.get("budget", {}))`
- `src/agent_orchestrator/orchestrator/commit_service.py:582-588` — 综合任务的 `open_account`
- `src/agent_orchestrator/orchestrator/commit_service.py:486-494` — `commit_task_graph` 只捕获 `GraphRejected`
- `src/agent_orchestrator/graph/task_graph.py:274-289` — Σ 检查的遍历对象是 `proposal.tasks`
- 实机复现（Mission `max_attempts=6`，synthesis 模板只给 `max_tokens`）：

  ```
  GRAPH COMMIT RAISED: BudgetError task budget {'max_tokens': 30000, 'max_attempts': None, ...}
    exceeds parent {'max_tokens': 100000, 'max_attempts': 6, ...} (§18.2)
  ```

**建议处置**：与 P0-1 同源修复（继承未设维度）；并在 `validate_graph`（或 `commit_task_graph` 前置）显式校验综合任务预算 `fits_within(mission.budget)`，不通过按 `GraphRejected("budget", ...)` 处理。

---

### P1-3 不可信内容标记可被 `./` 前缀绕过（S4-08 第一条判定可规避）

**发现**

`tool_gateway.is_untrusted()` 的"归一化"只有 `strip()` + `strip("/")` + 反斜杠替换，不折叠 `./`；而 `Workspace.resolve()` 显式允许 `"."` 片段（`any(part in {"..",""} for part in candidate.parts if part != ".")`）。因此 `workspace_read_file("./docs/vendor_notes.md")` 能读到同一份外部文档，却**不带 `trust="untrusted_external"`，也不带 `UNTRUSTED_NOTICE`**——`record["trust"]` 同样不写，网关审计里也看不出来。

这正面削弱 acceptance S4-08 的第一条"读取该文件的工具返回带 `trust=untrusted_external` 标记"；现有测试只覆盖精确路径 `docs/vendor_notes.md`。

**依据**

- `src/agent_orchestrator/runtime/tool_gateway.py:78-84` — `is_untrusted`
- `src/agent_orchestrator/artifacts/workspace.py:52-66` — `resolve` 放行 `"."` 片段
- `src/agent_orchestrator/runtime/tool_gateway.py:186-189` — 仅在 `is_untrusted` 为真时写 `trust` / `notice`
- 实机复现：

  ```
  is_untrusted('docs/vendor_notes.md', ('docs/',))      -> True
  is_untrusted('./docs/vendor_notes.md', ('docs/',))    -> False      ← 绕过
  is_untrusted('./docs/./vendor_notes.md', ('docs/',))  -> False      ← 绕过
  is_untrusted('docs//vendor_notes.md', ('docs/',))     -> True
  is_untrusted('/docs/vendor_notes.md', ('docs/',))     -> True
  is_untrusted('docs/../docs/vendor_notes.md', ...)     -> True（但 Workspace.resolve 先行拒绝，无害）

  Workspace.read_text('./docs/vendor_notes.md')         -> 'SYSTEM NOTICE…'（读到了）
  Workspace.read_text('./docs/./vendor_notes.md')       -> 'SYSTEM NOTICE…'（读到了）
  ```

**建议处置**：把标记判定建立在 `Workspace.resolve()` 已经算出的**规范相对路径**上（`target.relative_to(root)`），而不是原始字符串；或至少用 `PurePosixPath(path).parts` 去掉 `"."` 片段后再比。测试补 `./docs/...`、`docs/./...` 两个负例。

---

### P1-4 同一归一化缺陷让 `pytest:./docs/x` 被判 trusted → 仅引用外部文档的 Claim 可拿到 SUPPORTED

**发现**

`memory/claims.py::_normalise` 与 `_is_untrusted` 复用了同样弱的归一化。`parse_evidence` 对 `pytest:` 前缀的处理是"先查不可信前缀，不命中即 `TRUST_TRUSTED`"，于是 `pytest:./docs/vendor_notes.md` 被判为可信证据，`grade_claim` 直接给 `SUPPORTED`。

acceptance S4-08 明确要求"仅引用外部文档的 Claim 不 VERIFIED/**SUPPORTED**（`grade=unsupported`）"，这条被绕过。（VERIFIED 仍拿不到，因为 `covering_target` 要求目标真的被运行过，这一层防线是好的。）

**依据**

- `src/agent_orchestrator/memory/claims.py:44-46`（`_normalise`）、`:84-90`（`_is_untrusted`）、`:70-75`（`pytest:` 不命中前缀即 TRUSTED）
- 实机复现（`untrusted_prefixes=['docs/']`，`artifact_paths=['docs/vendor_notes.md','notes/x.md']`）：

  ```
  ['docs/vendor_notes.md']        -> UNDER_REVIEW ('untrusted_external')   ← 预期
  ['pytest:docs/vendor_notes.md'] -> UNDER_REVIEW ('untrusted_external')   ← 预期
  ['pytest:./docs/vendor_notes.md'] -> SUPPORTED  ('trusted')              ← 绕过
  ['./docs/vendor_notes.md']      -> UNDER_REVIEW ('unresolved')           ← 侥幸拦住（因为 artifact_paths 里没有 './docs/...'）
  ```

**建议处置**：与 P1-3 一起改成同一个规范化函数（建议抽到一处共用，网关与分级共享同一实现，避免两套语义漂移）。另附带一条 P2 级观察：按 D4-2 的约定，**任何** `pytest:<字符串>` 都算可信证据，即使该目标既不存在也没被运行过（`pytest:whatever.py` → SUPPORTED）。这是 plan 自身的约定，但与弱归一化叠加就成了洗白外部路径的通道；建议后续收紧为"目标必须能在验收副本里 `resolve` 成功"。

---

### P1-5 schema v2 的 `artifacts_lineage_idx` 唯一索引在真实 v1 库上可能建不起来

**发现**

DDL_V2 末行 `CREATE UNIQUE INDEX artifacts_lineage_idx ON artifacts(mission_id, path, version)` 是在原地升级时对既有数据执行的。而 L3-2 修复之前（第 3 步），版本是在 `_collect` 里快照时算的：两个候选并发快照会读到同一份血缘，双双写下同一个 `version` —— 这正是 `tests/orchestrator/step04/test_artifact_versions.py` 里"both candidates snapshot the same provisional version 1"所刻画的场景。也就是说，`candidates_per_task ≥ 2` 跑过的 v1 库里**大概率存在重复 `(mission_id, path, version)` 行**，迁移会以裸 `sqlite3.IntegrityError` 失败，`Store.open` 直接抛出（备份文件已生成，数据不丢，但库打不开、没有可读错误）。

`test_schema_migration.py::_v1_library` 构造的 v1 库只有一行 `missions`，`artifacts` 表是空的，因此覆盖不到这条路径；acceptance 附加门槛里的"schema v1 库可升级到 v2 …… 且旧场景回归"实质上没有被验证。

**依据**

- `src/agent_orchestrator/storage/schema.py:268` — `CREATE UNIQUE INDEX artifacts_lineage_idx ...`
- `src/agent_orchestrator/storage/store.py:225-235` — `_apply_migration` 逐句执行，无冲突处理
- `tests/orchestrator/step04/test_artifact_versions.py:47-70` — 证明 v1 语义会产生同 `(mission, path, version)`
- `tests/orchestrator/step04/test_schema_migration.py:18-32` — v1 夹具里 `artifacts` 为空
- 对照：`git show d7a3bd0:src/agent_orchestrator/orchestrator/event_handler.py:877` — 第 3 步在快照处 `versions=next_versions(...)`

**建议处置**：迁移里先做一次去重/重编号（按 `created_at, artifact_id` 对同 `(mission_id, path)` 重排 `version`）再建索引；或把 `_apply_migration` 的 `IntegrityError` 转成带修复指引的 `SchemaIncompatible`。测试补一个"含重复版本行的 v1 库"用例。

---

## P2

### P2-6 `rank_knowledge` 的 trust 维度实际是常数

`records` 只可能来自 `knowledge` 表（状态只有 VERIFIED/SUPERSEDED），`live` 又过滤成 `status == "VERIFIED"`，所以 `parts["trust"]` 恒为 `1.0`，权重 2.0 对排序完全不起作用；`TRUST` 表里的 SUPPORTED / DISPUTED / UNDER_REVIEW / PROPOSED / REJECTED 全是死条目。D4-9 的"2·可信（VERIFIED 1.0 / SUPPORTED 0.5 / DISPUTED 0.3 / UNDER_REVIEW 0.2）"在本实现里没有对应物。

依据：`src/agent_orchestrator/context/retrieval.py:31-39`、`:175`、`:193`。

建议：要么把 `TRUST` 收窄为 `{VERIFIED: 1.0}` 并在 §6.1 登记"本版本只排 VERIFIED，可信维度恒定"，要么让 explorer/critic 的候选 Claim 也走同一个打分器——不要让文档承诺一个不存在的排序因子。

### P2-7 去重丢掉了 `duplicate_of` 溯源

D4-9 要求"同 key+stance 或规范化 content 相同只留最高分，**记 `duplicate_of`**"。实现只把被丢弃的 id 平铺进 `dropped["duplicate"]`，没有"谁重复了谁"。对 `used_by` 复用链可见性的实际影响有限（被丢的记录仍可被 `used_knowledge` 合法引用，`rule_check` 不会拒），但 Agent 无法知道自己看到的那条代表了哪些同主题记录，闭环测试里"综合引用的是 A 的原知识而不是仲裁结论"这条关键行为也只能靠注释解释。

依据：`src/agent_orchestrator/context/retrieval.py:211-240`；`tests/orchestrator/step04/test_knowledge_sharing_closure.py:178-181` 的注释。

### P2-8 verifier 模板给的是争议 Claim 的正文，不是 D4-10' 说的"证据引用"

`disputed_claims()` 返回 `claim_id/status/key/stance/content/source_task/conflict_id/resolved_by/marker`，**没有 `evidence`**。D4-10' 对 verifier 的定义是"只有产物、测试输出、准则与**争议 Claim 的证据引用**"。当前实现给多了正文、给少了证据引用，方向正好相反。（arbiter 侧是对的：`task.context["sides"]` 带 `evidence`。）

依据：`src/agent_orchestrator/context/retrieval.py:243-260`、`src/agent_orchestrator/context/context_builder.py:97`、`:276-280`。

### P2-9 `_open_conflict` 末尾的 `_unblock` 在该时刻是空操作，注释误导

`_open_conflict` 最后一行 `self._unblock(mission.id, unblocked_by=None)  # D4-7': BLOCKED → READY, same transaction` 执行时，当前 Task 还是 `VERIFYING`（`accept_result` 要到 `:2037-2043` 才置 COMPLETED），而冲突任务的 `dependency_ids` 一定包含当前 Task，所以 `all(... is COMPLETED)` 必然为假——冲突任务实际是被 `:2064` 的 `_unblock(unblocked_by=task.id)` 解锁的（同一事务内，行为正确，事件也在）。这一行的副作用是会顺带扫描并解锁**其它**恰好满足条件的 BLOCKED 任务，比正常路径提前一点点。

依据：`src/agent_orchestrator/orchestrator/commit_service.py:1090` vs `:2026-2029`、`:2037-2043`、`:2064`。

建议：删掉这行或把注释改成事实（"解锁发生在 accept 的 `_unblock`"），否则后来人会以为不变量靠这里维持。

### P2-10 `check_arbitration` 不校验 pytest 目标落在 `arbitration/<key>/` 下

D4-7' 的产物契约是"仲裁的全部文件放在 `arbitration/<key>/` 前缀下（探针 `arbitration/<key>/test_probe.py`）"，但规则只检查"证据里存在任意以 `pytest:` 开头的项"。实践中被 `success_criteria` 里的 `pytest:arbitration/<key>/test_probe.py` 和 `covering_target` 兜住了（分级需要目标真被运行），但规则层的反馈比契约弱一档。

依据：`src/agent_orchestrator/verification/deterministic_checks.py:80-101`；`src/agent_orchestrator/planning/manager.py:66`。

### P2-11 §6.1 登记的 Raw Logs 层没有生产消费者；若干字段/函数是死代码

- `Blackboard`（含 §6.1/R17 承诺的 `raw_refs` 只读引用视图）只在 `tests/orchestrator/step04/test_claims_knowledge.py:113` 出现，Context Builder 从不给出 Raw Logs 引用——§10 的"只引用不直接喂"在包里没有落点。
- `KnowledgeIndex.foreign_ids`（`verified_knowledge.py:114`）永不被赋值，`check()` 里 `reference in self.foreign_ids` 是恒假分支（跨 Mission 靠 `":claim-" in reference` 兜住）。
- `KnowledgeRecord.useful_for`（`:57`）从不写入。
- `memory/summaries.py:74 branch_summary_for` 无调用者。
- `context_builder.VISIBILITY_TEMPLATES`（`:46`）只用于导出，判定用的是 `ENABLED_TEMPLATES`。

建议：要么接上（`raw_refs` 进包，至少给 synthesizer/critic），要么在 §6.1 明确降级为"本版本不交付"，不要留在 plan 里当已交付项。

### P2-12 `assert_no_secrets` 只查字段名；系统预留只覆盖 token 维度

- `assert_no_secrets` 递归只检查 Mapping 的 **key** 名，不看字符串 **值**；一份把密钥写在正文里的工作区文件（或 `test_output`）仍可原样进包。作为"不让凭证字段进上下文"的断言它是成立的，但不足以支撑"包内不含任何凭证"的更强说法（§6.1 Raw Logs 条目的措辞）。依据：`src/agent_orchestrator/context/context_builder.py:288-303`。
- `system_reserve_tokens` 只算 token（`planning/manager.py:128-135`），`validate_graph` 的 Σ 检查也只在 `max_tokens` 上加 `reserve`（`graph/task_graph.py:283`）。若 Mission 限定 `max_cost_micros`，Planner 任务会把成本池均分殆尽，综合/冲突任务的成本侧没有预留。与 D4-20 的字面（只写 tokens）一致，但值得登记为已知边界。

### P2-13 `_supersede_knowledge` 抬高了**被取代**记录的 `version`

`replace(record, status="SUPERSEDED", superseded_by=by, version=record.version + 1)` 把 K1 的 version 从 1 改成 2。而 S4-04 的判定是"早先 Attempt 的 intent 仍冻结 K1 v1"——冻结值与库里当前值从此对不上；`lineage._view` 输出的也是 v2。D4-3/D4-5 没有要求给被取代记录抬版本。

依据：`src/agent_orchestrator/orchestrator/commit_service.py:1201-1203`；`src/agent_orchestrator/observability/lineage.py:478-480`。

建议：要么不抬（版本是知识条目的身份，不是乐观锁），要么在 §6.1 写明"SUPERSEDED 会抬一版，冻结值按 Attempt 时刻读"。

### P2-14 测试卫生：三处恒真/死代码

- `tests/orchestrator/step04/test_conflicts.py:186-197`：`opinion = (... if False else ... if False else None)` 后跟 `assert opinion is None` —— 整段是死代码 + 恒真断言，疑似调试残留。
- `tests/orchestrator/step04/test_artifact_versions.py:78`：`assert OrchestratorConfig  # keep the import` —— 为保留 import 而写的恒真断言；应直接删掉 import。
- `tests/orchestrator/step04/test_retrieval_context.py:530`：`assert store.list_knowledge(...) == [] or all(...)` —— 左支被右支蕴含（`all([])` 为真），`or` 是冗余而非弱化；建议简化以免读者误判为放水。

其余被点名的"fixture 偷渡结论"担忧**不成立**：`test_retrieval_context.py:500-507` 的 `cite_supported` 从 store 直读 SUPPORTED claim id，是**刻意构造的对抗输入**（"the Worker somehow learned A's claim id"），而被断言的结论是"包里没有它"（`first_intent.config["knowledge"] == []`、正文不含该陈述）与"引用它必 FAIL"，构造方式不影响判定强度。

### P2-15 `Task` 增加 `context: Mapping` 后不再可哈希

`Task` 是 `frozen=True` 数据类，新增的 `context` 是 dict，`hash(task)` 现在抛 `TypeError: unhashable type: 'dict'`（实测）。当前代码与测试里没有把 `Task` 放进 set/dict-key 的地方（都用 `task.id`），`_dispatch_cycle:1344` 的 `t not in gated` 走的是 `__eq__` 所以正常；但这是契约层一次静默的能力回归，值得在 §6.1 登记或给 `Task` 显式 `eq`/`__hash__` 策略。

依据：`src/agent_orchestrator/contracts/models.py:267`；`src/agent_orchestrator/orchestrator/event_handler.py:1344`。

### P2-16 R11 的"构造性不可达"没有对应断言

D4-8' 承诺"'综合已 COMPLETED 后才出现冲突'在静态图里不可达，**登记为构造性边界并用测试断言**"。实际落地的是 accept 内守卫的测试（`test_synthesis.py:106-176`，覆盖"在途综合被 OPEN 冲突拒绝"）与闭环里的时序断言 `gated < k_done < s_created`（`test_knowledge_sharing_closure.py:176-181`）。对不可达性本身（例如"任何 `ConflictOpened` 的 seq 必早于终结任务的 `TaskCompleted`"）没有断言。

（代码侧核查结论：该不可达性成立——冲突只在 `accept_result` 中开启，`task.kind == "conflict"` 的 accept 跳过冲突检测（`commit_service.py:823`），综合任务依赖全部叶子，叶子的 accept 必早于综合 READY。建议把这句话变成一行断言即可。）

### P2-17 D4-21 改变了 S3-08 语义，旧路径不再有覆盖

`_next_attempt` 的 head_room 逻辑（预留 = min(名义份额, 账户剩余 − Critic 份额)）使"修复 Attempt 因名义份额不足而被拒"不再发生；`tests/orchestrator/step03/test_static_dag_closure.py:347` 靠把 `provider.usage_tokens` 调到 7000 制造**真实**耗尽来维持原断言。journal §2 已记录该裁决，但：

- step 3 的 `acceptance.md` 未同步说明 S3-08b 判定条件变了；
- `head_room <= 0`（剩余 ≤ Critic 份额）时仍按名义份额预留并触发 `BudgetExhausted` 这条分支，没有直接测试。

依据：`src/agent_orchestrator/orchestrator/event_handler.py:1506-1518`、`tests/orchestrator/step03/test_static_dag_closure.py:344-348`、journal §2 "D4-21"。

### P2-18 术语与文档

- §6.1 对 Judge 的消歧（理论 04-7 的 Judge ≠ 代码 `judge_mission`）只在 plan 里，`commit_service.judge_mission` 的 docstring（`:2080-2088`）没有这句话，读代码的人仍会误读。建议把消歧写进 docstring。
- 其余术语一致性核查通过：`Claim / Verified Knowledge / DISPUTED / Arbiter / Synthesizer` 在 `memory/claims.py`、`memory/verified_knowledge.py`、`verification/conflicts.py`、`planning/manager.py`、`runtime/role_templates.py` 的注释与原文 §11/§14.3–14.4/§25.3 一致，且都标注了出处。
- §6.1 列出的实施约定都有版本号落点：`RETRIEVAL_VERSION="retrieval-v1"`（`retrieval.py:29`）、`CONTEXT_BUILDER_VERSION="context-builder-v3"`（`context_builder.py:45`）、`SUMMARY_VERSION="summary-v1"`（`compression.py:23`）、`worker-v2` / `critic-v2` / `arbiter-v1` / `synthesizer-v1` / `planner-v3`（`role_templates.py:23-28`），且都进了 intent config 或 Attempt 记录。

---

## 核查通过的项（无发现，记录结论以免重复评审）

1. **Commit Service 唯一写入者**：全仓 `store.upsert_*/insert_*/update_*/set_result_*` 的调用点除 `commit_service.py` 外只有 `memory/summaries.py:64`（`refresh_summaries`），且它只在 `accept_result` 的事务内被调用（`commit_service.py:2065`）。`Blackboard` 门面无写方法。
2. **accept 事务原子性**：`_grade_and_project` / `_dispute` / `_open_conflict` / `_resolve_conflict` / `refresh_summaries` 全部在 `accept_result` 的同一 `Store.transaction()` 内；`Store.transaction` 用 `_depth` 实现可重入，`accept_result` 里 `return self.fail_result(...)` 的嵌套事务不会提前提交（`store.py:239-263`）。唯一会让**合法** accept 回滚的路径就是 P0-1。
3. **§25.3 Claim 状态机**：所有 `next_claim` 的目标边都合法——`_grade_and_project:832` 从 UNDER_REVIEW 到 {VERIFIED, SUPPORTED, DISPUTED, UNDER_REVIEW(不变)}；`_dispute:933` 只对 SUPPORTED/UNDER_REVIEW 打 DISPUTED（VERIFIED 对手走 `:922-930` 的纯数据标记 `disputed_by`）；`_supersede_knowledge:1206` 只在 `claim.status is VERIFIED` 时走 VERIFIED→SUPERSEDED；`next_claim(claim, None, ...)`（`:924`、`:962`、`:1013`、`:1049`、`:1107`）全部只改 `disputed_by` / `conflict_id` / `resolved_by` 数据字段。**没有新增边**（`contracts/state_machines.py:157-174` 与第 2/3 步一致）。
4. **Task 状态机无新边**：冲突任务以 BLOCKED 落库、经 `_unblock` 转 READY（合法边）；综合任务门控只写 `SynthesisGated` 事件、不改状态（`event_handler.py:1339-1344`）；accept 内守卫走 `fail_result`（VERIFYING→ACTIVE，合法）。
5. **`ordinal ≡ 拓扑序`**：综合任务在 Graph Commit 时取 `len(tasks)+1`，依赖全是更小 ordinal 的叶子；冲突任务取当时的 `len(tasks)+1`，依赖是已有工作任务，且 `terminal_task()` 与 `merge_accepted` 都不会让任何任务依赖它（测试 `test_conflicts.py:161` 有断言）。
6. **产物版本幂等**：版本在 `record_result` 事务内、幂等短路（`:1792-1794`）**之后**按 `(mission, path)` 血缘分配，且只对 `get_artifact(id) is None` 的新产物分配；重复投递不抬版本（`test_artifact_versions.py:82-104`）。
7. **`used_knowledge` 两段校验**：验证期 `rule_check(knowledge=KnowledgeIndex)`（`deterministic_checks.py:104-112`、`verifier_router.py:115-119`）+ accept 事务内复检（`commit_service.py:1990-2005`），失败按 FAIL 处理并写 `used_knowledge_stale`（`test_claims_knowledge.py:368-405`）。
8. **整树 pytest 不覆盖 Claim**：`code_test` 对整树运行记 `target=None`（`deterministic_checks.py:170`），`ran_test_targets` 归一成 `""`，`covering_target` 显式跳过 `target == ""`（`claims.py:110-123`），负例测试存在（`test_claims_knowledge.py:346-365`）。另核实 `code_test` 的 `mission_criteria` 形参在任务级验证里从未被传入（全仓无调用点），不存在"Mission 准则的测试顺带给 Task 的 Claim 定级"的漏洞。
9. **崩溃恢复**：`heal_mission` 的 `_unblock(unblocked_by=None)` 会重算前沿，冲突任务（BLOCKED、依赖已 COMPLETED）在重启后能被解锁；DEFERRED 冲突按 D4-20 不门控综合任务、由 `judge_mission` 报 `unresolved_conflicts`（`commit_service.py:2111-2116`）。
10. **双实例幂等**：`record_synthesis_gated` 的事件 key 为 `{task_id}:{conflict_id}`、`record_retrieval_unavailable` 为 `{task_id}:retrieval:{count}`，`append_event` 按 `idempotency_key` 去重（`store.py:307-314`），两个实例同轮各自计算出同一 count 只落一条事件，计数一致；计数由事件派生因而跨重启成立（`test_retrieval_context.py:634-657` 有断言）。
11. **可见性模板确实对 worker 隐藏 SUPPORTED/PROPOSED**：`KnowledgeContext.verified` 只由 `rank_knowledge` 的 VERIFIED 结果构成，`candidate_claims` 只在 `visibility in {"critic","explorer"}` 时进包，`rejected_claims` 只给 critic（`context_builder.py:99-118`）；S4-02 端到端断言 B 的包里既没有 id 也没有正文（`test_retrieval_context.py:537-543`）。
12. **verifier 模板不含提交者 summary/confidence**：`build_critic_package` 只放 `submitted_artifacts`（path/hash/size）、`test_output`、准则；`branch_summary`（其中含 `accepted_summary`）被无条件 `pop`（`context_builder.py:279`）；测试用递归 key 扫描断言 `{"summary","confidence","self_reported_confidence"}` 不出现（`test_retrieval_context.py:337-345`）。
13. **排序确定性**：`scored.sort(key=lambda item: (-item.score, item.id))` 有稳定 tie-break；去重遍历顺序即排序顺序（`retrieval.py:210-226`）。
14. **Critic 无预算 = 必需层 ERROR**：`run_critic` 把 `BudgetExhausted` 转成 `ContractError`（`event_handler.py:1120-1122`），由 router 记成 `critic_review` ERROR → 结果 FAIL；Mission 判定阶段的 Critic 无预算只降级为"no independent judge"（`:1651-1652`）。
15. **知识/摘要不内联外部正文**：`compress` 只写 task id/goal/status + 截断的 accepted summary + 知识 id/status（`compression.py:72-107`）；`knowledge_view` 只回读知识记录本身；外部文档正文只在 `workspace_seed`（Mission 规格）里，不进上下文包与 `knowledge.json`（`test_cli_knowledge_sharing.py:85-91`、`test_retrieval_context.py:750-753` 有断言）。

---

## 总体结论

第 4 步的主线设计与实现质量高：Claim → Verified Knowledge 的分级/投影、冲突优先于分级、DISPUTED 不投影、仲裁靠外部检查而非计票、综合任务门控而非改边、`ordinal ≡ 拓扑序`、accept 内的 TOCTOU 复检、检索失败的显式阻塞/降级——plan §6 的 23 条 review 处置在代码里都能找到对应实现，测试也大多是决定性的（尤其 `test_conflicts.py` 与 `test_knowledge_sharing_closure.py` 的断言链条扎实，没有发现被弱化的断言或偷渡结论的 fixture）。

阻塞项只有一个：**P0-1**——冲突任务（以及 P1-2 的综合任务）的预算不继承 Mission 在 token/attempts 之外维度上的限额，会在 `open_account` 抛出不在任何捕获集合里的 `BudgetError`，把一个合法的 accept 整体回滚，正面推翻 D4-20 "accept 事务永不因预算失败回滚" 这条 P0 处置；现有全部夹具恰好只用两个维度，所以测试全绿掩盖了它。

其次是两条安全项 **P1-3 / P1-4**：不可信标记与证据分级共用了一套过弱的路径归一化，`./docs/x` 既拿不到 `trust=untrusted_external`，又能以 `pytest:./docs/x` 换到 SUPPORTED —— S4-08 的两条判定都可绕过，且都已实机复现。**P1-5** 是迁移风险：新加的 `(mission, path, version)` 唯一索引恰好与 L3-2 缺陷本身产生的数据冲突，而迁移测试用的是空 `artifacts` 表。

建议：P0-1 与 P1-2 合并为一次修复（系统任务预算继承 + `_open_conflict` 兜底 defer + 综合任务预算进 `validate_graph`）；P1-3/P1-4 合并为"统一路径规范化"一次修复并补负例；P1-5 在迁移里加去重或给出可读错误；P2 项按成本择优，其中 P2-6/P2-7/P2-8/P2-11 属于"plan 承诺了但代码没落"，建议要么补齐要么在 §6.1 明确降级，不要留在已交付清单里。
