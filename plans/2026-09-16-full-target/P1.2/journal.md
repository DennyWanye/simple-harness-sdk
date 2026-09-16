# P1.2 实施记录（义务/HTN/验收/证据/操作的存储层与迁移 16）

日期：2026-09-16｜基线：SDK main `873fd4a`（工作区含 P1.1/P2.2/P2.1b/P2.1c/P2.2b 并行未提交产物）｜执行：单个 Opus 子代理｜实际耗时：约 1 小时 10 分钟（含规范阅读约 20 分钟）

规范来源：主计划 `complete-plan.zh-CN.md` §15、§18.4、§18.5、§20 第 6 条、§23 P1.2 行；
附件 TG 设计稿 §14.1（逻辑记录/索引表）与实现稿 §11.1–11.3；附件 AER `design.zh-CN.md` §2.1、§18.2。

## 1. 交付清单

| 文件 | 行数 | 内容 |
|---|---:|---|
| `src/agent_orchestrator/storage/htn_schema.py`（新） | 607 | `DDL`（迁移 16 的全部 STRICT 表 + 索引 + 唯一/部分唯一约束）与 `TABLES`（30 张表名元组，供守卫测试与旧路径泄漏检查复用） |
| `src/agent_orchestrator/storage/obligation_store.py`（新） | 572 | `ObligationStore`、`ObligationRelationRow`、`DEFAULT_RECURSION_FUEL` |
| `src/agent_orchestrator/storage/htn_store.py`（新） | 1996 | `HtnStore` 与 `StoredMethod` / `StoredPlanRevision` / `StoredReviewRecord` / `StoredJustificationSet` / `PlanCommitReceipt` / `DirtyEntry` |
| `src/agent_orchestrator/storage/schema.py`（改，+2 行） | — | 只做两处：`from .htn_schema import DDL as DDL_V16`；`MIGRATIONS` 末尾追加 `Migration(16, "orchestrator-full-target-htn", DDL_V16)`。旧 15 条与旧 DDL 一字未动 |
| `tests/orchestrator/full_target/test_htn_store.py`（新） | 1409 | 71 条 |
| `tests/orchestrator/full_target/test_obligation_store.py`（新） | 383 | 22 条 |

`git diff --stat` = `src/agent_orchestrator/storage/schema.py | 2 ++`，其余为上列 5 个新文件（untracked）。未触碰 `storage/store.py`、`contracts/`、`orchestrator/`、`graph/`、`knowledge/`、`verification/`、`artifacts/`、`planning/`。

## 2. 表清单（迁移 16，共 30 张，全部 STRICT）

开工前核对：当时最高迁移号确为 15（`orchestrator-mission-system-tail`），故新号取 16。

| 表 | 归属/身份键 | 关键约束与索引 |
|---|---|---|
| `task_semantics` | `mission_id` | PK `(task_id, binding_revision)`（binding_revision = 绑定自带的 `contract_revision`）；`content_hash` 64 位；mission+form、mission+obligation 两个索引 |
| `method_contracts` | **跨 Mission 注册表**（模块 docstring 写明） | PK `(method_id, method_version)` 不可变；`content_hash` 唯一；`registry_status`/`author` 枚举 CHECK；`model` 作者只能 DRAFT、`TRIAL_ADMITTED` 必带 `trial_scope_mission`（DDL 级 CHECK） |
| `method_instances` | `mission_id` | PK `(mission_id, instance_id)`；goal_task+plan_revision、goal_occurrence+state、obligation 索引；state ∈ DRAFT/ADOPTED/RETIRED |
| `method_child_occurrences` | `mission_id` | PK `(instance_id, slot_key)`；复合 FK → `method_instances`；`goal_occurrence_id` + `reuse_policy` 索引（共享目标反查） |
| `plan_revisions` | `mission_id` | PK `(mission_id, revision)`；**部分唯一索引 `WHERE state='ACTIVE'`**；`base_revision < revision` |
| `plan_memberships` | `mission_id` | PK `(mission_id, revision, occurrence_id)`；FK → `plan_revisions`；task、obligation+adopted 索引 |
| `order_constraints` | `mission_id` | PK `(mission, revision, before, after)`；before/after **双向索引**；`before<>after`；FK → `plan_revisions` |
| `data_requirements` | `mission_id` | PK `(mission, revision, requirement_id)`；consumer(port)、producer(port) 索引（不设唯一：SET 端口可有多条） |
| `bound_inputs` | `mission_id` | PK `(mission, revision, requirement_id, input_binding_revision)`；复合 FK → `data_requirements`；acceptance、artifact+hash 索引 |
| `input_manifests` | `mission_id` + hash 寻址 | PK `manifest_hash`（64 位，immutable）；`(mission,task,request_id)` 部分唯一 |
| `obligations` | `mission_id` | PK `(mission_id, obligation_id)` + `obligation_id` 全局唯一索引；`lifecycle`、`failure_count`、`spent_tokens`、`spent_cost_micros`、`spent_attempts`、`fuel_limit/fuel_used/fuel_remaining`、`resolution_ref`；CHECK `fuel_used<=fuel_limit`、`fuel_remaining=fuel_limit-fuel_used`、SATISFIED 必带 `resolution_ref`、不可自任父；资金归属索引 `(mission, goal_signature_id, budget_lineage_ref)` |
| `obligation_relations` | `mission_id` | PK `(mission,parent,child,kind)`；双向 FK → `obligations`；child 反查索引 |
| `obligation_expansions` | `mission_id` | PK `(mission,obligation,method_id,parameters_digest)`（重复展开检测）；`(mission,obligation,ordinal)` 唯一（回放顺序） |
| `obligation_shape_changes` | `mission_id` | PK `(mission,obligation,ordinal)`；五种 ShapeChange 的 CHECK（往返保真需要，见偏差 3） |
| `requirements_revisions` | `mission_id` | PK `(mission_id, revision)`；`revision_id` 唯一；`content_hash` |
| `review_packages` | `mission_id`（来自 binding） | PK `package_id`；`purpose`/`review_account` 枚举 CHECK；subject(kind,id) 索引；`package_hash` |
| `review_records` | `mission_id` | PK `record_id`；**部分唯一索引 `WHERE official=1`**（同 package 只一份正式接纳）；package、reviewer 索引 |
| `criterion_evaluations` | `mission_id` | PK `(review_id, criterion_id)`；`check_receipt_hash` 由 package_id + binding + outcome 绑定生成；DDL 复刻 I07（PASS 不得配 NOT_RUN/ERROR/CANCELLED） |
| `acceptances` | `mission_id` | PK `acceptance_id`；`review_record_id` 唯一；obligation+validity、task+requirements 索引；`content_hash` 幂等 |
| `goal_resolutions` | `mission_id` | PK `resolution_id`；**`(mission,obligation)` 部分唯一 `WHERE adopted=1`**；obligation+validity 反向索引 |
| `validity_witnesses` | `mission_id` | PK `witness_id`；唯一 `(mission, consumer_kind, consumer_id, purpose, scope_id, scope_epoch, support_revision)`；`as_of_ms`/`not_after_ms`；CHECK USABLE⇒TRUE（I18） |
| `observations` | `mission_id` | PK `observation_id`；proposition+observed_at、scope+recorded_at 索引；`coverage`/`query_watermark_ms` 保留（权威否定判定用） |
| `justification_sets` | `mission_id` | PK `set_id`；`(mission,subject_kind,subject_id,member_digest)` 唯一（同一主体可有多组支持）；subject+member_revision 索引 |
| `support_members` | `mission_id` | PK `(set_id, member_kind, member_id)`；**反向支持索引** `(mission, member_kind, member_id)`；带极性 |
| `validity_epochs` | `mission_id` | PK `(mission_id, scope_id)` |
| `validity_dirty` | `mission_id` | PK `(mission,subject_kind,subject_id,epoch)`；state ∈ PENDING/RECHECKING/CLEARED；state、scope 索引 |
| `operation_identities` | `mission_id`（跨 Mission 语义写在 docstring） | PK `operation_id`；`UNIQUE(operation_id, request_hash)`——**同 OperationId 不允许第二个 request hash，由 FK 而非方法内检查兜底** |
| `operation_bindings` | `mission_id` | PK `(principal_id, scope_id, operation_occurrence_id)`；`operation_occurrence_id` 全局唯一；FK `(operation_id, request_hash)` → `operation_identities` |
| `plan_read_sets` | `mission_id` | PK `(mission, proposal_id, subject_type, subject_id)`；subject+semantic_revision 反查索引；`subject_type='read_set'` 行保存整份 read-set 以保证往返 |
| `plan_commit_receipts` | `mission_id` | PK `command_id`；`(mission, new_plan_revision)` 唯一；`intent_hash` + `read_set_hash` + `output_identity_json`；`new>base` |

## 3. 实现要点

- **组合而非继承**：`ObligationStore(store)` / `HtnStore(store)` 只持有 `Store` 实例，全部写入走 `store.transaction()`（可嵌套复用同一事务），`store.connection` 只读查询。`storage/store.py` 零 diff。
- **义务账本语义与 `contracts.obligations.ObligationLedger` 对齐**：先校验参数再写行（被拒调用不改任何一行）；`note_shape_change` 只记历史不动计数；燃料耗尽返回 `FuelStatus.BOUND_REACHED`；重复展开返回 `REPEATED_EXPANSION` 且不烧燃料。`load_ledger` 按「注册 → 失败数 → 消费 → 按 ordinal 重放展开 → 形状变更 → lifecycle」精确回放；`persist` 反向写回且**永不清零 `spent_tokens`**（纯账本没有 token 轴）。
- **单一 ACTIVE**：`activate_plan_revision` 在一个事务内先把旧 ACTIVE 置 RETIRED 再置新 ACTIVE，部分唯一索引兜底；已 RETIRED 的修订不可复活。
- **回执幂等**：`record_commit_receipt` 同 `command_id` 同 `intent_hash` 且同 read-set hash → 返回原回执；intent 或 read-set 不同 → `StoreConflict`；不同 command 产出同一 `new_plan_revision` → 唯一索引冲突。
- **不做业务判定**：模块 docstring 明确写明覆盖、环、预算的判定属于 P2.3a 的 `plan_commits`；`test_the_store_does_not_judge_a_plan_only_stores_it` 把这条当合同测。
- **旧路径隔离**：`htn_schema.TABLES` 供守卫测试遍历；`test_a_legacy_run_writes_nothing_into_the_new_tables` 用 `importlib` 按路径加载 `tests/orchestrator/step02/test_store_and_budgets.py`，直接调用其三个测试函数跑完旧流程后逐表断言为空。

## 4. 测试

| 文件 | 条数 | 覆盖 |
|---|---:|---|
| `test_htn_store.py` | 71 | 迁移 16 为新头；**旧 15 条迁移 checksum 硬编码逐条断言**；30 张新表存在且 STRICT、新库中全空；旧流程零泄漏；**副本演练**（monkeypatch 到 15 版建库 → 复制文件 → 打开副本升级到 16 → `missions`/`events` 行逐条相同、`orch_schema_migrations` 前 15 行不变且多出第 16 行、新表全空、原库不动）；升级前自动备份 `*.pre-schema-16.backup` 内容为 15 版；每张主表往返；唯一/FK/CHECK 冲突（task+revision、method 版本内容变更、instance_id、slot、plan revision、membership、order 边、data requirement、bound input 的 FK 与 binding revision、manifest 跨任务、requirements revision、review 正式接纳、record 与 package purpose 不符、acceptance 幂等与改内容、一份 review 只背书一份 acceptance、adopted resolution 单一、witness 五元组、observation、justification 空集合与重复成员、operation 同 id 异 hash = OPERATION_PAYLOAD_CONFLICT、同 occurrence 二次绑定、receipt intent/read-set/revision）；共享目标 occurrence 反查；反向支持索引 `consumers_of`；epoch 递增与 dirty 队列；read-set 往返 + 逐项索引 + 提案反查；**事务回滚一致性**（8 张表在一个失败事务内写入后全部未落，已存在的 plan revision 不受影响）与成功事务整体落盘 |
| `test_obligation_store.py` | 22 | 往返；重复注册被拒；零值起点；失败计数/消费在改名·换方法·换 Agent·后继任务之后仍累计；**被拒调用不改行**（负 attempts、count=0）；SATISFIED 缺 `resolution_ref` 被拒且状态不变；燃料按义务计、耗尽 `BOUND_REACHED` 且**落库后重开仍是 BOUND_REACHED**；重复展开不烧燃料；新义务才有新额度；关系双向索引与自指/未注册被拒；`load_ledger` 逐项等值；`persist` 往返；`persist` 不清零 `spent_tokens`；`persist` 可插入库中未见过的义务；失败事务整体回滚 |

合计 **93 条，全绿**（`pytest tests/orchestrator/full_target/test_htn_store.py tests/orchestrator/full_target/test_obligation_store.py -q` → 93 passed）。

红→绿过程中出现 2 次真实红：`ScopeEpochRead.epoch` 字段名实为 `validity_epoch`（mypy 先抓到）；支持集成员读回顺序与传入顺序不一致——修为「支持集是集合，按 (kind,id) 归一排序后再算 digest 并写入」，这同时让 digest 不再依赖调用方的列举顺序。

## 5. 门槛核对

- `pytest tests/orchestrator/full_target/{test_htn_store,test_obligation_store}.py -q` → **93 passed**。
- `pytest tests/orchestrator/step02 tests/orchestrator/step03 tests/orchestrator/p33 -q` → **1020 passed / 5 skipped / 1 failed**，唯一失败是已知的 `test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged`（Python 3.14 AST hash），**零新增失败**。
- `pytest tests/orchestrator/full_target -q --ignore=test_readiness_reasons.py` → **834 passed / 1 skipped**（`test_readiness_reasons.py` 属并行 P2.1c 未完成产物，导入 `graph.eligibility` 失败，与本片无关）。
- `ruff check`（storage + full_target 测试）、`ruff format --check`（本片 5 个文件）全过。
- `mypy src/agent_orchestrator`：17 errors / 4 files，全部为既有的可选依赖 `import-not-found` 与 `evaluation/agentdojo_runner.py` 旧问题；`storage/` 零错误（改动前后对比：本片一度引入 1 个 `ScopeEpochRead.epoch` 错误，已修）。
- `git diff --stat` = `storage/schema.py | 2 ++`；未提交、未跑全量回归（按任务书要求）。

## 6. 与任务书/规范的偏差

1. **`input_manifests` 存不可变 JSON 文档而非契约对象**。任务书要求「全部以契约对象进出」，但 `artifacts/input_bindings.py`（P2.2b 并行片）的 `InputManifest` 只有 `to_json()`/`manifest_hash()`、没有 `from_json`，且该文件本片禁止触碰、正被另一子代理修改。为避免形成「输入清单如何构成」的第二权威，本表按 `manifest_hash` 内容寻址保存 opaque canonical JSON，`insert_input_manifest` 对同 hash 幂等、对同 hash 换任务报冲突。P2.2b 定稿后可在 `HtnStore` 上加一层薄 codec，不必改表。
2. **`MethodRegistration` 在读取侧由存储层就地重建**。契约里 `MethodRegistration` 只有 `to_json()`，没有 `from_json`（见 §7 契约变更请求 1）。本片用 `MethodRef` + 枚举 + `TypedRef.from_json` 在 `HtnStore._stored_method` 内重建，语义等价，未改 contracts/。
3. **新增 `obligation_shape_changes` 表**（任务书表清单未列）。`ObligationLedger` 的 `shape_changes` 是账本状态的一部分，`load_ledger`/`persist` 要求精确往返，缺这张表就无法做到「往返」这条门槛。`ExpansionRecord` 同理落在 `obligation_expansions` 的列上（该类型无 codec）。
4. **新增 `operation_identities` 表**。AER §18.2 要求「OperationId 不允许不同 request hash」。仅靠 `operation_bindings` 的唯一索引做不到（两条不同 hash 的行都能插入），因此拆出 `operation_identities(operation_id PK, UNIQUE(operation_id, request_hash))` 并让绑定表以复合 FK 引用它——冲突由数据库拒绝，不依赖某个方法记得检查。
5. **`obligations` 增加 `spent_tokens` 与 `spent_attempts` 两列**。任务书列了 `spent_tokens`，而纯账本只有 cost/attempts 两个轴；本片把三个轴都留列，`record_spend(..., tokens=)` 只走存储层，`persist()` 不动该列。`fuel_remaining` 作为显式列并用 `CHECK(fuel_remaining=fuel_limit-fuel_used)` 自洽，避免读侧再算一次。
6. **`data_requirements` 的消费端口索引不设唯一**。TG §14.1 只说「consumer port 与 producer 索引」；`PortCardinality.SET` 的输入端口天然可以有多条来源，设唯一会把合法的集合端口挡掉。唯一性落在 `requirement_id` 上。
7. **`plan_read_sets` 额外写一行 `subject_type='read_set'`**。逐项索引行无法无损重建 `SemanticReadSet`（absence 的键被哈希化、标量轴分散），为满足「往返」在同表放一行整份 read-set。该行不参与反查（`list_read_set_items` 显式排除它）。

## 7. 契约变更请求（交 P1.1 契约持有者统一处理，本片未改 contracts/）

1. `contracts/htn.py::MethodRegistration` 缺 `from_json`。存储层与未来的注册表服务都要从持久化形态还原它，目前只能在各自模块里手写重建。建议按其余类型的样式补一个 `from_json`（字段：`method_ref`、`status`、`author`、`admission_receipt_ref`、`trial_scope_mission`）。
2. `contracts/obligations.py::ExpansionRecord` 缺 `to_json`/`from_json`。本片按 `(method_id, parameters_digest, task_id)` 三列落库，属于在存储层复刻线上形状；若后续要把展开记录放进事件 payload，建议补 codec。
3. （可选）`ObligationLedger` 没有 token 消费轴，而 §18.4 的义务表与 P1.3 的「沿 Obligation 继承已消费额度」都需要 token。本片在存储层加了 `spent_tokens`；若希望纯账本也能表达，需要在 `record_spend` 上加一个 `tokens` 参数并扩展 `ObligationAccountView`。

## 8. 后续片可以直接用的接口

- `ObligationStore.load_ledger(mission_id)` / `persist(ledger)`：P1.3 `mission_tail_commits` 的替代任务继承失败计数与已消费额度，可在同一事务里 load → 改 → persist。
- `HtnStore.activate_plan_revision` / `record_commit_receipt`：P2.3a `plan_commits`（第 9 个 mixin）的原子切换与命令幂等；业务判定仍留在 P2.3a。
- `HtnStore.consumers_of` / `read_set_consumers` / `occurrence_consumers`：P3.1 read-set 复验与 P3.6 失效传播的反向索引入口。

---

## 9. 审阅修复（2026-09-16，独立审阅"需修后合并"）

审阅结论：无阻断，约束基本落库；必改 2 高 4 中 1 低。逐条处理如下。

### 9.1 必改（高）

**1 — 部分唯一索引从未被触发（变异逃逸）。** 原测试只走 `activate_plan_revision`（先退旧再立新），删掉 `plan_revisions_active_idx` 也 93 全绿。新增三条：
- `test_a_second_revision_may_not_be_inserted_straight_into_active`：已有 ACTIVE 时直接 `insert_plan_revision(..., state="ACTIVE")` 必须 `StoreConflict`，且事后仍只有一条 ACTIVE。删掉该索引这条立刻红。
- `test_a_first_revision_may_be_inserted_active`：首条可直接 ACTIVE（确保上一条不是把功能测死）。
- `test_an_unknown_plan_revision_state_is_refused`：状态白名单。

**2 — `record_failure` 事务外读、写绝对值，并发丢更新。** 改为 SQL 相对累加 `failure_count = failure_count + ?`，存在性判定用 `cursor.rowcount == 0`（不再预读）；`record_spend` 同样去掉预读改用 rowcount；`note_shape_change`、`consume_fuel` 的 ordinal / 燃料余量 / 重复展开判定全部移入同一 `store.transaction()`，`sqlite3.IntegrityError` 统一转 `StoreConflict`；`set_lifecycle` 的读与写也合入一个事务（新增 `_require_row(connection, ...)`、`_next_ordinal(connection, ...)` 两个「事务内读」辅助）。

新增 4 条真并发测试，用 `_rival(...)` 在本方 `BEGIN IMMEDIATE` 之前让第二条连接先提交：`test_a_concurrent_failure_is_not_lost`、`test_a_concurrent_spend_is_not_lost`、`test_a_concurrent_expansion_does_not_double_spend_the_last_fuel`（燃料 1，对手先花掉 → 本方必须 `BOUND_REACHED` 而不是 ordinal 冲突）、`test_a_concurrent_shape_change_keeps_both_histories`。已用一次性变异脚本核验：把 `record_failure` 换回修复前写法，同一条测试得 1 而非 4（脚本未入仓）。

### 9.2 必改（中）

**3 — 迁移 16 钉快照。** 新增 `MIGRATION_16_CHECKSUM`（`239e8fdc…f369f62`）与 `MIGRATION_16_TABLES`（31 张表名，含本轮新增的 `input_manifest_bindings`）两个常量，`test_migration_sixteen_is_pinned_to_its_checksum_and_table_list` 逐一断言，并加 `test_the_declared_table_list_is_exactly_what_migration_sixteen_adds`（建 15 版库与 16 版库各取表名求差集 = `htn_schema.TABLES`），避免 `TABLES` 与 DDL 漂移。

**4 — 静态守卫。** 新增两条：
- `test_no_module_outside_storage_writes_the_new_tables_in_sql`：遍历 `src/agent_orchestrator/**.py`（排除 `storage/`），对 31 张表名匹配 `(FROM|INTO|UPDATE|JOIN|TABLE)\s+<table>`（**大小写敏感**，避免把 `knowledge/justifications.py` 里的英文散文 "re-select anchors from observations" 误判），断言结果 ⊆ `KNOWN_RAW_SQL_DEBT`。
- `test_the_graph_and_knowledge_layers_never_name_a_new_table_in_sql`：`graph/`、`knowledge/`、`planning/`、`verification/`、`scheduling/` 五层**无白名单**，一条 SQL 都不许有。

白名单非空，与审阅指示（"白名单为空"）有一处偏差，原因见 §9.4 —— P1.3 的 `orchestrator/obligation_commits.py` 目前有两处裸 SQL（`SELECT binding_json FROM task_semantics …`、`SELECT 1 FROM obligations …`）。为此在存储层补了两个访问器供其替换：`HtnStore.task_semantics_of(mission_id, task_id)`（Mission 范围内最新绑定）与 `ObligationStore.exists(mission_id, obligation_id)`。断言写成子集关系，P1.3 改用访问器后守卫仍绿，而任何**新**泄漏立刻红。

**5 — `input_manifests` 的 docstring 与行为自相矛盾。** 按建议取「同 hash 允许多 (mission, task) 绑定」：表拆为
- `input_manifests(manifest_hash PK, origin_mission_id, manifest_json, created_at)` —— 不可变内容，按 hash 唯一，刻意不带使用方 Mission（跨 Mission 复用 = 对同一不可变产物的一次显式读取），`origin_mission_id` 只记首次冻结者；
- `input_manifest_bindings(mission_id, task_id, manifest_hash, attempt_id, request_id, input_binding_revision, PK(mission,task,hash))` + `(mission,task,request_id)` 部分唯一 + hash 反查索引。

`insert_input_manifest` 在一个事务里写内容行（`ON CONFLICT DO NOTHING`）与绑定行；新增 `list_manifest_bindings(hash)`。测试：`test_two_tasks_with_identical_inputs_share_one_immutable_manifest`（两任务 + 跨 Mission 共享一份内容行、三条绑定）、`test_one_request_id_belongs_to_one_manifest`、`test_an_unknown_input_manifest_is_a_conflict`；原「换任务即冲突」的测试已删除（行为已按审阅改变）。表数 30 → 31。

**6 — 副本演练扩大样本。** 新增 `LEGACY_TAIL_ROWS`：迁移 7–15 建的 13 张表（`workspaces`、`mission_domains`、`sources`、`criterion_assessments`、`provider_token_grants`、`search_bindings`、`selection_rounds`、`selection_candidates`、`budget_tail_holds`、`budget_tail_transfers`、`fragment_validations`、`mission_system_tail_pools`、`mission_system_tail_tasks`）各写一行真实值（满足各自 CHECK），用普通 sqlite3 连接写入（外键默认关闭；演练关心的是字节能否原样穿过升级）。`test_upgrading_a_copy_of_a_v15_library_keeps_every_old_row` 现在比对 `missions`、`events` + 这 13 张表，并先断言样本非空。

### 9.3 低（已顺手做）

- `acceptances` / `method_instances` 的 `obligation_id` 未加 Mission 范围 FK：**保持现状并在 `htn_schema` 的注释里说明**——`obligations` 的 PK 是 `(mission_id, obligation_id)`，加复合 FK 会强制「先建义务行再写验收/实例」，而 P2.3a 的提交顺序尚未定稿；由服务层（`ObligationStore.exists`）负责。同理 `review_packages.obligation_id`。
- `list_*` 收敛 helper：未做（审阅标为可不做）。

### 9.4 契约第四轮落地后的接入（审阅指定）

- `MethodRegistration.from_json` 已有 → `method_contracts` 的 `admission_receipt_json` 列换成 **`registration_json`**（整份注册记录的 canonical JSON），`HtnStore._stored_method` 改为 `MethodRegistration.from_json(...)`，不再就地重建。新增 `test_a_hand_edited_registration_row_is_refused_by_the_contract_codec`：把行里的 `status` 手改成 `ADMITTED`（作者仍是 `model`），读取时被契约的 §7.3 规则拒绝（`ContractError`）——就地重建是抓不到这个的。
- `ExpansionRecord` 有 codec → `ObligationStore.expansions()` 改走 `ExpansionRecord.from_json({...})`。
- `record_spend` 有 tokens 轴、`ObligationAccountView` 有 `consumed_tokens` / `has_admitted_demand` → `obligations` 表新增 `demand_admitted` 列（含 `CHECK(demand_admitted=0 OR lifecycle='UNSATISFIED')`）；`account()` 填 `consumed_tokens` / `has_admitted_demand`；`load_ledger` 用 `record_spend(tokens=)` 与 `admit_demand()` 回放（先 demand 后 lifecycle）；`persist` 写回 `spent_tokens` 与 `demand_admitted`。新增 `admit_demand` / `withdraw_demand`（语义与账本一致：重复接纳被拒、无接纳不可撤回、非开放义务不可接纳）。
- 原「`persist` 永不动 `spent_tokens`」的说法与测试作废（那是账本没有 token 轴时的权宜）；改为 `test_the_token_axis_round_trips_through_the_ledger` 与 `test_persisting_an_untouched_ledger_leaves_every_axis_alone`。
- 保留 `ObligationStore.spent_tokens()`：P1.3 的 `obligation_commits.py` 已在调用它。

原 §6 偏差 1（input_manifests 不用契约对象）与 §7 契约变更请求 1、2、3 均已由本轮处理，保留原文以留痕。

### 9.5 修复后门槛

- 本片测试：`test_htn_store.py` **81 条** + `test_obligation_store.py` **30 条** = **111 passed**（原 93）。
- `tests/orchestrator/full_target` 全目录：**1334 passed / 1 skipped**（含其余并行片的测试，零失败）。
- `tests/orchestrator/step02 step03 p33`：**1020 passed / 5 skipped / 1 failed**，仍是已知的 p33 Python 3.14 AST hash，零新增失败。
- `ruff check` + `ruff format --check`（本片 5 个文件）全过；`mypy src/agent_orchestrator` 在 `storage/` **零错误**（全仓 17 个均为既有可选依赖 import-not-found）。
- `git diff --stat` 中属于本片的仍只有 `storage/schema.py | 2 ++`；其余为契约第四轮与 P1.3 的改动。
- 文件行数：`htn_schema.py` 622、`htn_store.py` 2011、`obligation_store.py` 672、`test_htn_store.py` 1770、`test_obligation_store.py` 527。

### 9.6 仍需主 session 决定

1. **白名单非空**：`KNOWN_RAW_SQL_DEBT` 含 P1.3 `orchestrator/obligation_commits.py` 的两处裸 SQL（`task_semantics`、`obligations`）。访问器已就位（`HtnStore.task_semantics_of`、`ObligationStore.exists`），请让 P1.3 替换后删掉这两个条目；在此之前守卫对任何**新**泄漏仍然有效。
2. `input_manifests` 拆表后表数变为 31，迁移 16 的 checksum 变为 `239e8fdcd6a3f4ebb6fbe0073d416c2f6607ca927dc2a324bcdd322fcf369f62`；迁移 16 尚未提交发布，故直接改 DDL 而非再加一号。若已有机器建过 16 版库，需删库重建。
