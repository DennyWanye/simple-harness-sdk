# P2.3c 第一部分 · 施工日志（2026-09-16）

## 0. 基线

动工前先锁绿色基线（HEAD `e78a8cc`，工作树含在途的 P2.3b）：

```
uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target -q
  → 1790 passed, 1 skipped in 30.30s
uv run --frozen --group dev ruff check src/agent_orchestrator tests/orchestrator/full_target
  → All checks passed!
uv run --frozen --group dev mypy src/agent_orchestrator
  → Found 17 errors in 4 files（全部是 are_benchmark / are_bridge / agentdojo 的
    import-not-found + 一条已有 misc，17 条基线）
```

另记一条基线事实，后面要用：`runtime/role_templates.py` 在 HEAD 上**本来就**
不满足 `ruff format`（`git show HEAD:… > /tmp/x.py && ruff format --check /tmp/x.py`
→ "1 file would be reformatted"）。所以本片只往它里面**追加**一段格式合规的新模板，
不对该文件跑 `ruff format`。

## 1. 读输入（按任务书顺序）

`scheduling/allocator.py`（frontier 56 / allocate 206）、`graph/eligibility.py`
（1749 行，`legacy_ready_is_not_eligibility` 在 1608 行、此前**没有正面测试**）、
`contracts/htn.py` 的 `TaskSemanticBindingV1.form`、`storage/htn_store.py`
（acceptances / goal_resolutions / validity_witnesses / record_commit_receipt）、
`storage/obligation_store.py`（`set_lifecycle` 的 SATISFIED 需 resolution_ref）、
`verification/acceptance_rules.py`（`acceptable()`、`AcceptanceSubject` 的
independence/posture 无默认）、`contracts/resolution.py`（四个事实 + ReviewPurpose
账户表）、`knowledge/predicates.py`、`contracts/evidence_state.py` 的
`ObservationRecord`、两域 `predicates.json`、`planning/htn/registry.py`
（`admit` 六步、`promote` 一律 `PROMOTION_NOT_AVAILABLE`）、`planning/planner.py`
（`parse_method_proposal`、`SYSTEM_BOUND_FIELDS`）、`runtime/role_templates.py`、
`evaluation/appworld*.py`（`observe_public_api` + `PUBLIC_READ_APIS` 冻结允许表）。

规范：主计划 §18.5（四条兼容硬约束）、§24.1 裁决 6、§25.1 裁决 4、§6.3、§7.3、
§8.1、§13 v1.4；附件 AER §6.1/§6.2/§6.3/§7；§5.3–5.4；§6.6 C28。

只读、没碰：`event_handler.py`、`hierarchical_dispatch.py`、`plan_commits.py`、
`commit_service.py`、`contracts/`、`graph/`、`storage/`、`artifacts/`。

## 2. 施工顺序与关键判断

### 2.1 allocator（+370 行，旧函数 0 行改动）

写完第一件事就是把旧函数的源码哈希打出来存进测试：

```
frontier  584 bytes  sha256 0ae7cd4c24ee902e1fac2f8e8193920a64e9ff10c408baf0f33d1d6cc366e1b8
allocate 3897 bytes  sha256 5940ab39e18168a83f9af8c422a9924758faf41ddf8793021b5a89c6cb5ab78c
```

追加完新代码后重新打印，两个哈希一字不变——这是 §18.5 硬约束 2 的机械证据。

三个判断值得记：

1. **form 门必须排在 status / admission / paused 之前。** 一开始写成「先查
   admission，再查 form」，测试 `test_the_form_gate_runs_before_the_admission…`
   立刻暴露问题：一个把 `occ-b` 的 admission 错填到 compound task id 下的映射，
   会在 form 门之前就被 admission 的 task_id 校验挡住——挡住了，但挡的理由是
   `GRAPH_INTEGRITY` 而不是 `NEEDS_REFINEMENT`。compound 的拒绝理由必须是
   NEEDS_REFINEMENT（planner 要拿它去细化），所以 form 门前移。
2. **答案取自 `legacy_ready_is_not_eligibility`，不自己写。** P2.3b 的
   `intercept_worker_dispatch` 已经在问同一个函数；两处各写一遍 form 判断，
   迟早给出两个答案。测试 `test_the_allocator_and_the_eligibility_module_give_the_same_compound_answer`
   断言 refusal 的 reason 与 detail 与那个函数逐字相同。
3. **`_pressure_keep()` 抽成函数，但旧函数体内联副本不动。** 旧 `allocate` 的
   探索配额逻辑不能改（哈希锁定），新 `allocate_v2` 又需要同一套规则。折中：
   抽一个模块级 helper 给 v2 用，再写一条测试
   `test_the_pressure_filter_agrees_with_the_legacy_inline_one`，在同一个世界上
   对照 helper 的输出与 legacy `allocate()` 的 grants。

踩到的坑：`BackpressureState` 的 level 常量是 `"RAISED"`（大写），我先按 `"raised"`
写导致 4 条测试静默地在「未升压」路径上通过；改成从 `backpressure` 导入 `RAISED`。

### 2.2 resolution_commits.py（新建 1454 行）

**最大的一处设计偏差在这里，必须记清。**

原计划把命令回执写进 `plan_commit_receipts`（`record_commit_receipt` 是任务书
点名的输入）。实际跑起来直接撞墙：

```
sqlite3.IntegrityError: CHECK constraint failed: new_plan_revision>base_plan_revision
```

那张表的 CHECK 要求新 plan revision **严格大于** base——它是 plan 形状的表，而
acceptance / goal resolution 根本不产生 plan revision。可选项只有三个：

- (a) 谎报一对 revision（`base=r, new=r+1`），让读者以为提交了一个计划版本；
- (b) 改 `storage/`，加一张接受侧回执表 —— 本片 `storage/` 只读；
- (c) 把回执从**本命令追加的那个事件**投影出来。

选 (c)，新增 `CommandReceipt`。依据是 `Store.append_event` 本身按
`idempotency_key` 幂等（"an existing idempotency_key returns the stored event"），
所以「同一命令两次 = 一次提交、一张回执」有 durable 依据；`_replayed()` 扫
`list_events` 按 payload 的 `command_id` 找回执，intent 不同则
`COMMAND_PAYLOAD_CONFLICT`。**这是契约变更请求**：第二部分应当给接受侧回执建
真表 + 迁移，或者在 ADR 里确认「事件即回执」。已写进 plan.md 的接线清单第 2 条。

另外几处判断：

- **`accept_review` 不动义务生命周期。** §25.1 裁决 4 说 ACCEPT 与 GoalResolution
  是两个动作。测试 `test_an_acceptance_leaves_the_duty_open` 锁定：accept 之后
  `lifecycle` 仍是 UNSATISFIED、`resolution_ref` 仍是 None。
- **compound 的贡献集合从库里读。** `CompoundFacts` 只接受调用方说得清的两件事
  （选中方法合法、组合义务通过）；`contributing_occurrence_ids` 由
  `list_child_occurrences` + `list_acceptances`（validity=CURRENT）算出来，命令
  声称的与库里不符即 `COMPOUND_FACTS_CONTRADICT_STORE`。这一条把
  「命令把一个不存在的孩子说成存在」变成不可能，测试
  `test_mutant_taking_contributions_from_the_command_would_talk_a_child_into_existence`
  先证明拒绝、再 accept 一次证明同一条命令就通了。
- **package / record / requirements 都按 content hash 从库里复读。** 不可变审阅锚
  的全部意义就是「公式评的是打包时冻结的那份」；只信呈递进来的副本等于没有锚。
  `test_a_package_edited_on_the_way_in_is_refused_by_content_hash`、
  `test_requirements_are_never_quietly_relaxed`（同一 revision 号、放宽了
  requirement_class → `REQUIREMENTS_MISMATCH`）。
- **根 Resolution 的交付门从不读 `Mission.status`。** 测试用
  `_check_delivery.__code__.co_names` 断言编译后的字节码里没有 `status`
  这个名字——比 grep 源码可靠（第一版 grep 源码被 docstring 里「the Mission's own
  ``status`` string is deliberately never read」这句话打败了）。
- **`decided_at_ms` 是新增的必填字段。** 第一版用 `witness.as_of_ms` 当 now 传给
  `is_fresh_for`，等于让截止时间检查恒真。改成命令显式带「做决定的当下」。
- **admitted demand 要先撤。** `obligations` 表的 CHECK 是
  `demand_admitted=0 OR lifecycle='UNSATISFIED'`，所以挂着 demand 的义务直接转
  SATISFIED 会 StoreConflict。按 TG 裁决 9（撤需求是结束共享、不是结束义务）在
  同一个事务里先 `withdraw_demand`，并记在事件 payload 的 `demand_withdrawn`。
- **事务嵌套是安全的**：`Store.transaction()` 对同一 task 可重入、只有最外层
  真正 BEGIN/COMMIT，所以 `set_lifecycle` / `withdraw_demand` 内部自己开的事务
  与外层原子。回滚测试打 `_emit`（它在两次写之后），断言
  acceptances / goal_resolutions / 回执三处都为 0、lifecycle 未动。

测试世界构造踩的坑（都是测试侧）：
`read_set.requirements_revision` 记的是 **Mission 当前**的最新 revision（世界里
是 2），不是该 subject 的 package 所基于的那个（1）——改成由
`World.read_set()` 从库里读；`validity_witnesses` 有
`(mission, consumer, purpose, scope, epoch, support_revision)` 的 UNIQUE 约束，
同一 task 的第二张 ACCEPT witness 必须换 `support_revision`；`bump_epoch` 在空表上
写的是 **0**，所以要连 bump 两次才真的越过命令读到的 0；`CriterionOutcome` 自己就
拒绝 `PASS + NOT_RUN`（invariant I07），所以「必需检查没跑」的测试要写
`UNKNOWN + NOT_RUN`；`Validity` 只有 CURRENT / STALE / REVOKED，没有 SUPERSEDED。

还有一条值得记的**产品发现**：`IndependenceFacts()`（空默认）并不是「通过」，
而是被 `independence_ok` 的 package 交叉核对抓住——package 冻结时写了
`producer_agent_ids=("agent-worker",)`，空 facts 说「没人生产过」，两者矛盾即
`FACTS_CONTRADICT_PACKAGE` → `INDEPENDENT_REVIEW_MISSING`。比我原来设想的
「默认会放行」更好，测试
`test_mutant_defaulting_the_independence_facts_is_refused_not_waved_through`
按这个真实行为重写。

### 2.3 observers（新建 1356 行）

- 「三态」在类型层面就闭合：`Observation.__post_init__` 要求 OBSERVED 必带
  record、UNAVAILABLE 必**不**带 record。停机没有极性。
- CLOSED 域的负观察在 `observed()` 构造处就被检查（复用
  `knowledge.predicates.authoritative_negative_matches_observer`），所以观察器
  没法造出一条 `atom_truth` 会当真的假否认。
- `QueryCompleteness` 枚举里能支撑否认的只有 `AUTHORITATIVE_WITH_SCOPE`，
  任务书写的「QueryCompleteness=COMPLETE」对应它；本片给它起了个说明性的别名
  `COMPLETE_COVERAGE`，并在 docstring 里写明「没有更弱的 COMPLETE 成员」。
- **只读是一条检查而不是一句注释**：所有命令走 `_Command.run` 的允许表，
  在 spawn **之前**拒绝 checkout / commit / reset / clean / stash 和任何非
  git/python 的程序；测试断言被拒时 runner 的调用记录为空。另有一条测试用
  `code.py` 源码里 `subprocess.run(` 只出现一次来锁定「没有第二条绕过允许表的路」。
- `code.test-is-failing` 老实处理：只读收集只能回答「目标存不存在」，回答
  「是否失败」得真跑测试，所以默认 UNAVAILABLE，`allow_test_execution=True` 才跑，
  且带 `-p no:cacheprovider` 不写工作树。三态因此齐全（收集不了 / 不允许跑 /
  跑出 0 或 1）。
- appworld 侧把「主机答了并拒绝这个读」（负观察）与「服务没起 / 传输失败」
  （UNAVAILABLE）严格分开。`entity-ambiguous` / `entity-unique` 是**基数**问题，
  冻结的 `PUBLIC_READ_APIS` 表达不了，所以做成可选能力 `EntityIndex`：客户端不提供
  就 UNAVAILABLE，绝不猜一个「unique」。`action-confirmed`（CLOSED）同理，没有
  可枚举的收据台账就 UNAVAILABLE 而不是便宜的 FALSE。
- 「观察不改被观察的世界」有一条直接测试：对临时目录做 (mtime_ns, bytes) 快照，
  跑完三个观察器后逐字节相同。
- 踩坑：`TestObserver` 被 pytest 当测试类收集（PytestCollectionWarning），改名
  `SuiteObserver`。

### 2.4 synthesis.py + 角色模板

- `SYSTEM_BOUND_FIELDS` 从 `planning.planner` 导入（同一份清单，不可能漂移）；
  导入方向经确认无环（planner → commit_service → plan_commits → htn.compiler，
  没有一条回到 synthesis）。
- `authority_claims()` 只查**结构键**，`goal_parameters` 子树跳过——与
  `planner._refuse_authority_claims` 同一条界线（「method 参数里恰好叫 scope 的
  是值，不是声明，否则契约就依赖域的词汇表了」）。`SynthesisRequest.__post_init__`
  自己跑这个断言，所以带 authority 字段的请求根本构造不出来。
- 模型自填状态**保留**再由准入协议拒绝（`MODEL_CLAIMED_STATUS`）并写下 REJECTED
  registration；静默改写成 DRAFT 会把这次尝试擦掉。`proposal_declares_status()`
  只是诊断，不是门。
- `accept_response` 在回执越过 TRIAL_ADMITTED 时抛错（mutation 测试用
  monkeypatch 把 registry 的 transitions 改成 ADMITTED 来验证这道保险）。
- **没有 `promote`**；测试断言 `"promote" not in dir(MethodSynthesizer)`，且
  `registry.promote` 对 EVALUATED / ADMITTED 都答 `PROMOTION_NOT_AVAILABLE`、
  registration 仍是 TRIAL_ADMITTED。
- 角色模板：新 role 名 `method_synthesizer`（不是 planner 的新版本），模板正文
  写明「你不是 Task Critic，开销记在 mission_planning 账户」、逐条列出 14 个
  系统绑定字段、要求只输出一个 `<method_proposal>` 块。测试
  `test_registering_the_new_role_left_every_other_template_alone` 断言
  `ROLES` 里每个角色的当前版本仍在 `registered_versions()` 里。
- 踩坑：`goal_type_ref` 第一版从 `GoalSignature` 现造一个 `VersionedRef`
  （用 parameter_schema 的 hash），registry 按 `(id, version, content_hash)`
  三元组匹配，hash 不对就永远检索不到候选。改成从 catalogue 里按
  `signature_id + version` 找回 `TaskTypeSpec.task_type_ref`——hash 由声明它的那份
  数据提供。

## 3. 结果

```
新增/改动源码
  scheduling/allocator.py                        +370（旧两函数 0 改动，哈希锁定）
  orchestrator/resolution_commits.py             +1454（新建）
  planning/htn/observers/__init__.py             +296（新建）
  planning/htn/observers/code.py                 +561（新建）
  planning/htn/observers/appworld.py             +499（新建）
  planning/htn/synthesis.py                      +551（新建）
  runtime/role_templates.py                      +54（只追加）
  合计 +3785 行

新增测试
  test_allocator_form_gate.py      744 行 /  71 条（≥20 ✓）
  test_resolution_commits.py      1492 行 /  80 条（≥30 ✓）
  test_predicate_observers.py      957 行 /  83 条（≥20 ✓）
  test_method_synthesis.py         701 行 /  48 条（≥15 ✓）
  合计 3894 行 / 282 条
```

（审阅修复后的最终计数见 §6 末的「修复后结果」。）

变异自证（要求 ≥6，实交 23 条 `test_mutant_*`）：allocator 6 条、
resolution_commits 7 条、observers 6 条、synthesis 4 条。每条的形状都是
「先证明真实实现拒绝，再证明换成那个看起来合理的错实现就会放行」。

测试与 lint：

```
pytest tests/orchestrator/full_target -q
  → 2091 passed, 1 skipped in 41.86s（基线 1790 + 本片 282 + 在途 P2.3b 的 19）
ruff check src/agent_orchestrator tests/orchestrator/full_target  → All checks passed!
ruff format --check <本片改动的 6 个源文件 + 4 个测试文件>       → 全部 formatted
mypy src/agent_orchestrator → Found 17 errors in 4 files（与基线同数、同文件）
```

旧模式回归（§18.5 硬约束 2 的行为证据）：

```
pytest tests/orchestrator/step02 step05 step06 step07 p34 p35 -q
  → 560 passed, 13 skipped in 202.70s
```

13 条 skip 全是环境性的既有 skip（`needs --run-real-provider` ×5、
pinned tokenizer ×8），**零新增失败**。

`ruff check src/agent_orchestrator tests/orchestrator` 另报 4 条错误，全部在
`tests/orchestrator/p33 / p34 / p35` 里、本片一行未改，属既有基线；本片自己的
10 个文件 `ruff check` + `ruff format --check` 全过。

## 4. 并发注意（现场事实）

施工中途发现 **P2.3b 正在被另一个会话实时改动**：

```
15:35:36  tests/orchestrator/full_target/test_hierarchical_event_flow.py
15:37:57  plans/2026-09-16-full-target/P2.3b/journal.md
```

后果两条，都记下来免得被误读：

1. `full_target` 全目录的条数从基线的 1791 变成 2092，差额 19 条来自
   `test_hierarchical_event_flow.py` 在途的新增，不是本片的。
2. 中途有一次 `pytest tests/orchestrator/full_target` 卡住 11 分钟
   （stack sample 显示阻塞在 `select_poll_poll`，即一个 subprocess 的
   `communicate`）。原因是两个 pytest 进程同时在同一个 git 工作树上跑
   只读 git 命令 + 同时有文件在被改写。清掉并发进程后重跑 41.86s 全绿。
   本片没有为此改任何代码。

## 5. 给第二部分的接手点

见 `plan.md` §三（六条接线清单）与 §四（四条偏差 / 一条契约变更请求）。
最要紧的一条：**接受侧命令回执目前是从事件投影出来的**
（`CommandReceipt` + `_receipt_from_event`），因为 `plan_commit_receipts` 的
CHECK 不允许 base == new。第二部分要么建表，要么把「事件即回执」写成 ADR。

---

## 6. 审阅修复（2026-09-16，独立审阅「需修后合并」）

审阅结论：allocator 与 synthesis 可原样合；resolution_commits 与 observers 有四处 P0。
以下逐条记录修了什么、为什么、以及证据。

允许范围内新增一个文件：`orchestrator/_read_set.py`；改动 `plan_commits.py` 仅限把它的
read-set 检查切到公共实现（其 116 条测试必须全绿，实测全绿）。未改 `storage/`、
`event_handler.py`、`hierarchical_dispatch.py`、`commit_service.py`。

### P0-1 read-set 只复查 5/11 通道 → 抽公共实现，两侧全覆盖

**问题**：`resolution_commits._check_reads` 手写了一份只查 requirements / goal /
acceptance / support_set / scope_epoch 的副本，漏掉 method / observation /
obligation / authority / absences / budget_grant_revision。这不是遗漏细节——漏掉的
三条正好是**改变了世界但没改变被读那一行**的三种情况：

- FACT：反驳一条 observation 是**新增**一条更晚的记录，原记录逐字节不动；
- AUTHORITY：撤销一个 approval 只动它的 version；
- OBLIGATION：重新规划一条义务只动它的 shape-change 计数。

**修法**：新建 `orchestrator/_read_set.py`，把 `StaleRead`（原 `_Stale`）、`_probe`
与 11 个通道解析器抽成 `SemanticReadSetChecker`，`plan_commits` 与
`resolution_commits` 共用。`verify()` 返回 `ReadSetVerdict`，两条 Commit 路径各自
转成自己的拒绝类型与 reason 码——这正是让 `plan_commits` 的消息字节保持不变、同时让
`resolution_commits` 补齐通道的办法。

两个刻意的配置点：

1. `allow_task_control_channels`（plan 侧关、accept 侧开）。plan 提案的 TASK id 是
   裸 task id；accept 命令的 read-set 是 `eligibility.build_read_set` 造的，TASK 通道
   里还有 `<task>#dispatch_generation` / `<task>#input_binding_revision`。关掉时这些
   id 解析不到 → unresolved，**与抽取前 plan 侧的行为逐字相同**（116 条测试是证据）。
2. `budget_grant_resolver`。P2 没有 budget grant 的库侧权威，所以 claim 非 0 时是
   **unresolved**（fail closed：复读不到的预算不是可以据以支出的预算），claim 为 0 时
   视为「没有声明」。plan 侧的 116 条测试里没有非 0 的 budget_grant_revision，所以
   加上这一通道对它是零行为变化。
3. `manager_epoch` 刻意不进公共实现：两侧都在更早的门里查它，理由是 scope 权限的答案
   （`MANAGER_EPOCH_STALE`）而不是「一串过期项里的一项」。

**plan_commits 的 116 条测试有一条在 mutation 段落 monkeypatch
`CommitService._goal_state`**。解析器搬走后该 seam 消失。保留了六个一行委托方法
（`_goal_state` … `_authority_state`）并通过 checker 的 `resolvers=` 覆盖映射接回去，
所以那条 mutation 测试仍然真的削弱了那个通道——不是把测试改成能过。

**新增测试**（在 `test_resolution_commits.py` §3b）：
`test_a_review_resting_on_a_refuted_observation_is_refused`、
`test_a_review_resting_on_a_revoked_authority_is_refused`、
`test_a_review_resting_on_a_re_planned_duty_is_refused`、method / absence /
budget_grant / dispatch-control 通道各一至两条，外加
`test_the_accept_path_and_the_plan_path_share_one_checker` 与
`test_every_channel_a_read_set_can_carry_is_re_checked`（对 `SemanticReadSet` 的字段
逐个断言在公共实现里出现过，`manager_epoch` 除外）。

### P0-2 `_check_delivery` 信命令的回执（审阅变异 R9 存活）→ 全部复读并校验

**问题**：旧实现把不合格的回执**跳过**（`continue`），只在一条都不合格时报一个笼统的
`DELIVERY_STAGE_NOT_REACHED`。于是一张引用 STALE/REVOKED acceptance、或引用别的
Mission 的 acceptance 的回执，只是「不算」——R9 变异因此存活。

**修法**：每一张呈递的回执都 `_require_valid_receipt`，任何一张不合格即
`DELIVERY_RECEIPT_INVALID`（整条命令拒绝，不是跳过）。四项检查：回执属本 mission、
它引用的 acceptance 能从库复读到、该 acceptance 属本 mission **且**属本根的
**义务闭包**、且 validity=CURRENT。

「本根 obligation」的落地读法记一下：AER §6.3 明确「发送需要**报告**已接受，而不需要
整个 Mission 已完成」，所以回执引用的 acceptance 通常是**子**贡献而不是根目标本身。
因此闭包 = 根义务 ∪ 已采用方法实例各槽位的子义务（`_root_duty_closure`）；引用闭包外
的义务即「为别的工作出的回执」，拒绝。

落库仍留第二部分。**契约变更请求**（写进 plan.md）：新增 `acceptance_commit_receipts`
与 `delivery_receipts` 两张表 + 迁移，唯一键 `(mission_id, command_id)` + `intent_hash`。

**新增测试**（§8b 六条 + 改一条）：别的 mission、未入库的 acceptance、STALE、REVOKED、
闭包外义务、「一张不合格即拒绝即使另一张本来合格」；原
`test_a_receipt_quoting_an_acceptance_nobody_stored_does_not_count` 改名为
`..._is_invalid` 并把期望改成 `DELIVERY_RECEIPT_INVALID`。

### P0-3 code 观察器允许表只看子命令 → 按完整 argv 校验

**问题**（实测确认）：旧 `run()` 只看子命令、跳过所有以 `-` 开头的实参，而
changeset / test 的实参又直接拼进 argv。于是
`git diff --numstat --output=/tmp/x` 与 `pytest --collect-only --junitxml=/tmp/x`
都通过了允许表，**并且真的写出文件**。

**修法**四条：

1. 新增 `TRUSTED_ARGUMENTS`：每个子命令**只能**携带这里列出的固定 flag。flag 是
   「命令怎么被驱使」，所以一个 flag 永远不从调用方拿。
2. 实参走 `OPERAND_SEPARATOR`（`--`）之后，且 `operand()` 拒绝空白、以 `-` 开头、
   含 NUL/换行的值。`--junitxml=out.xml` 看起来和一个测试目标一模一样，这条才是真正
   的拦截点。
3. `pytest --collect-only` 会 **import** 测试模块、跑它的模块级代码，所以收集改在
   `read_only_copy()` 的一次性副本里跑（排除 `.git` / 各种 cache），用完必删；
   工作树大于 `max_copy_bytes` 时返回 UNAVAILABLE 而不是就地收集。
4. 每个观察器有**整次观察**的预算 `_Budget`（默认 60s），每条命令只拿剩余额度；
   额度用尽是 UNAVAILABLE，不是极性。

**新增测试**（§7 / §7b 共 20 条），其中一条是真跑的反例：
`test_a_writing_flag_operand_really_would_have_written_without_the_gate` 先用真
`git diff --numstat --output=…` 证明**文件真的被创建**，再证明同一 argv 被
`_Command.run` 拒绝且文件不存在——否则「拒绝」只是对一个字符串的测试，不是对一个
危险的测试。另有
`test_a_test_module_that_writes_on_import_cannot_touch_the_worktree`：一个在 import
期写文件的测试模块，收集后工作树逐字节不变。

### P0-4 appworld `_get` 把全部 ValueError 当「拒绝」→ 三态分类

**问题**：主机的 `ValueError` 有好几种来源，旧实现一律标成 `refused:` 并被上层当作
**负观察**。于是畸形 JSON（`unexpected fields`）、世界身份不符
（`world identity differs`）、超出证据上限都变成了 FALSE——用完整性失败制造出来的
FALSE。

**修法**：新增 `REFUSAL_MARKERS` / `INTEGRITY_MARKERS` 与 `classify_read_error()`。
只有语义上「不存在/拒绝」的授权refusal 才是负观察；解析失败、身份不符、超限、
以及**任何本模块不认识的错误**一律 `OBSERVER_UNAVAILABLE`（默认就是 UNAVAILABLE：
把不认识的错误解读成「世界说不」正是 parse 失败变 FALSE 的路径）。完整性标记优先于
refusal 标记，免得 refusal 措辞出现在完整性消息里就赢。`_get` 返回
`_Read`（projection / refused / problem 三者互斥），调用方没法漏分支。

顺带两处同类问题：收据台账的 `ValueError` 也不再能变成 CLOSED 谓词的否认（不能解析的
枚举不是完整枚举）；台账没给 coverage scope 时同样 UNAVAILABLE（没有 scope 的否认
不是权威负观察，§6.6 C28）。

**新增测试**（§7c 共 14 条）：畸形 envelope、身份不符、超限、未识别错误、非 mapping
projection 各一条 UNAVAILABLE；真正的授权 refusal 一条负观察；两组 marker 参数化；
完整性优先；台账不可解析 / 无 scope 两条；一条变异自证。

### P1-5 `_replayed` 事务内全扫 → 命令派生的 key + 定向查找

事件的 `idempotency_key` 现在由**命令 id** 派生（`command_event_key` /
`command_idempotency_key`），所以 `Store.append_event` 的唯一键路径**本身**就是
「同一命令两次 = 一次提交一张回执」的保证，与查找无关。查找本身：先用
`count_events(mission_id, type)`（走索引）回答「这个 Mission 一条 accept 侧事件都没有」
——这是绝大多数情况、只读一行；只有确实有的 Mission 才按 `REPLAY_PAGE=512` 分页。
`Store` 上没有 by-key 读接口，而 `storage/` 不是本片能改的；**回执表迁移后切换为一次
keyed read**（已记入 plan.md 接线清单）。测试
`test_a_replay_lookup_does_not_read_the_whole_mission` 断言首次 accept 期间
`list_events` 调用次数为 0。

### P1-6 `_accepted_occurrences`

不再 `del duties`：读子义务的 lifecycle，CANCELLED / SUPERSEDED 的义务上的 acceptance
不是活的贡献（§6.1：已取消、或被授权替代品替换——它接受的工作已经不是本方法要的工作）。
返回值从 `set[str]` 改为 `dict[occurrence_id, tuple[acceptance_id, ...]]`，**按
occurrence 聚合**：TG §12 允许两个槽位采用同一个共享目标，按义务聚合会让一份贡献替两个
occurrence 作答。事件 payload 新增 `contributing_acceptances`，记下哪张 acceptance
承担了哪个 occurrence。

### P1-7 reason 码与 author 锁

- `bad_command` / `bad_principal` → `BAD_COMMAND` / `BAD_PRINCIPAL`；新增
  `test_every_reason_code_is_upper_snake_case` 用正则锁住形状。
- `MethodSynthesizer.accept_response` 的 author **固定**为
  `SYNTHESIS_AUTHOR = RegistryAuthor.MODEL`，不再是带默认值的参数；真的持有人工/工具
  编写的定义的调用方传 keyword-only 的 `author_override` 并因此把这件事说出来。理由：
  `text` 是从模型来的，冒充别的作者等于把注册服务的写行权给一份模型编写的定义，而
  `MethodRegistration` 只允许模型作者停在 DRAFT 正是为了阻止这件事（§6.3、§7.3）。
  6 条新测试，含签名断言（`author` 不再是参数、`author_override` 是 keyword-only）。

### L2 验收所需、但仓库尚无的状态谓词（第二部分待办）

审阅要求列出来。本片的两个试点域覆盖的是**结构**谓词（工作树干净、测试可收集、
变更集大小、应用可达、凭据有效、实体基数、动作有收据）。L2 级验收还需要**业务状态**
谓词，仓库目前一个都没有：

1. `appworld.account-exists(app, account)` —— 账号/收件人是否存在。冻结的
   `PUBLIC_READ_APIS` 只有 `show_active_task` / `show_profile`，读不到任意账号。
2. `appworld.list-contains(list_ref, item_ref)` / `appworld.list-size(list_ref, n)`
   —— 清单内容与条目数。需要一个可枚举的清单读，且 CLOSED 化才能支撑「不在清单里」。
3. `appworld.amount-equals(transfer_ref, amount, currency)` —— 金额与币种。必须是
   **权威**读且带 watermark，否则不能用来支撑「金额正确」这类硬约束。
4. `code.diff-touches-only(changeset, paths)` —— 变更只落在授权路径内（配合 §24.1
   裁决 4 的产物身份）。
5. `code.declared-dependency-present(package)` —— 依赖声明存在（原计划提到，本片
   未做：种子库没有这个谓词签名，新增签名会动 `seed_methods/` 的内容哈希）。

每一条都要：谓词签名进 `seed_methods/<domain>/predicates.json`（会改内容哈希，
需与 `test_seed_methods.py` 的漂移检查一起做）+ 一个只读观察器 + CLOSED 域的话还要
一条可枚举、带 watermark 的权威查询。建议与「观察器注册进 PredicateRegistry」
（plan.md 接线清单第 5 条）同一片做。

### 修复后结果

```
源码
  orchestrator/_read_set.py                 570（新建，公共实现）
  orchestrator/plan_commits.py              +100 / −330（只切到公共实现，行为不变）
  orchestrator/resolution_commits.py       1520（P0-1/P0-2/P1-5/P1-6/P1-7 后）
  planning/htn/observers/code.py            827（P0-3 后）
  planning/htn/observers/appworld.py        612（P0-4 后）
  planning/htn/synthesis.py                 568（P1-7 后）
  scheduling/allocator.py                   +370（审阅「可原样合」，未再动）
  runtime/role_templates.py                 +54（审阅「可原样合」，未再动）

测试
  test_allocator_form_gate.py       744 行 /  71 条
  test_resolution_commits.py       2010 行 / 106 条（+26）
  test_predicate_observers.py      1389 行 / 134 条（+51）
  test_method_synthesis.py          785 行 /  54 条（+6）
  合计 4928 行 / 365 条；变异自证 26 条 test_mutant_*
```

验证（修复后重跑）：

```
pytest tests/orchestrator/full_target -q
  → 2174 passed, 1 skipped in 40.07s
pytest <本片四个测试文件> + test_plan_commits.py -q
  → 481 passed（其中 test_plan_commits.py 116 条全绿 = plan 路径行为未变的证据）
pytest tests/orchestrator/step02 step05 step06 step07 p34 p35 -q
  → 560 passed, 13 skipped（既有环境 skip，零新增失败）
ruff check src/agent_orchestrator tests/orchestrator/full_target → All checks passed!
ruff format --check <本片 12 个文件> → 全部 formatted
mypy src/agent_orchestrator → Found 17 errors in 4 files（与基线同数、同文件）
```
