# P2.3c 第一部分：allocator form 门 / 根 GoalResolution / 最小谓词观察器 / MethodSynthesizer 系统侧

FULL-TARGET-1.4 第 P2.3c 片，**只做第一部分**。第二部分（接进
`event_handler` / `hierarchical_dispatch`、跑真实叶子、Grok 验收）等 P2.3b
审阅定稿后由另一轮完成。

## 一、本片范围（做了什么）

| # | 交付 | 文件 | 规范依据 |
|---|------|------|----------|
| 0 | 语义 read-set 的**唯一**复查实现（11 通道），plan 与 accept 两条 Commit 路径共用 | `orchestrator/_read_set.py`（新建）+ `orchestrator/plan_commits.py`（只切到公共实现，行为不变） | ADR-13 clause 2；审阅 P0-1 |
| 1 | allocator 的 `form=compound` 门（新增入口，旧入口逐字节不动） | `scheduling/allocator.py`（+370 行） | §18.5 硬约束 2 / 4；§24.1 裁决 6 |
| 2 | `accept_review` / `commit_goal_resolution` 两个接受侧 Commit 入口 | `orchestrator/resolution_commits.py`（新建 1454 行） | AER §6–§7；§25.1 裁决 4；§6.3；§8.1 |
| 3 | 最小谓词观察器（code 域 5 个观察器 / 6 个谓词；appworld 域 5 个观察器 / 6 个谓词） | `planning/htn/observers/{__init__,code,appworld}.py`（新建 1356 行） | §7.3 种子库；§5.3–5.4；§6.6 C28 |
| 4 | MethodSynthesizer 系统侧（typed context + 准入回执，不调模型） | `planning/htn/synthesis.py`（新建 551 行） | §7.3 来源 4；§18.5 C8；§6.3 |
| 5 | MethodSynthesizer 角色模板（新增角色，旧模板原文不动） | `runtime/role_templates.py`（+54 行） | §18.5 C8 |
| 6 | 四个红测试文件，共 282 条 | `tests/orchestrator/full_target/test_{allocator_form_gate,resolution_commits,predicate_observers,method_synthesis}.py`（新建 3894 行） | — |

### 1. allocator：`form=compound` 门

- 新增 `frontier_v2(tasks, bindings, readiness)`、`evaluate_frontier_v2(...)`、
  `allocate_v2(...)`、`FrontierV2`、`FrontierRefusal`、`AllocationPlanV2`、
  `ALLOCATOR_V2_VERSION = "allocator-v2"`。
- **旧 `frontier()` / `allocate()` 逐字节不变**，由测试用 `inspect.getsource` 的
  SHA-256 锁定（`0ae7cd4c…` / `5940ab39…`）。§18.5 硬约束 2 要求旧 READY 入口保留，
  `EligiblePrimitiveTask` 是新模式的**附加**门而不是 `allocate()` 的唯一入参类型；
  旧 Mission 无语义绑定时照常派发。
- 检查次序是承重的：
  1. 无语义绑定 → `GRAPH_INTEGRITY`（§18.5：新模式缺绑定是损坏，不是 legacy fallback）；
  2. **form 门** → compound 无论 `Task.status` 是什么一律 `NEEDS_REFINEMENT`，
     且这一步在「看 status / 看 admission / 看 paused」之前；答案取自
     `graph.eligibility.legacy_ready_is_not_eligibility`，所以 allocator 与
     P2.3b 的 `hierarchical_dispatch.intercept_worker_dispatch` 不可能给出不同答案；
  3. 才看 admission：必须是 `admit_for_dispatch()` 造出来的记录（`gate_passed`），
     必须是**本** Task 的（不可转让），且 Task 不是 paused / 不是终态。
- 评分（`score_tasks` + `WEIGHTS`）、物理容量（`OPEN_ATTEMPT_STATES`、
  `concurrency_limit`、`candidates_per_task`、`waiting_attempt_ids`）、验证背压
  （`BackpressureState` 降并发 + 探索配额）全部复用旧实现；探索配额那段抽成
  `_pressure_keep()` 供 v2 用，旧函数体内联副本保持不动，另有一条测试断言两者一致。
- 给 `legacy_ready_is_not_eligibility` 补了此前缺失的正面测试（§18.5 第 2/4 条）。

### 2. `resolution_commits.py`：两个动作，不是一个

按 AER §7 伪代码逐步实现，两个入口共用同一组门：

认证（`_authorize`，未签名命令不归属于呈递者）→ 层次模式门（legacy Mission 在读
任何迁移 16 表之前被拒）→ 幂等回执 → 绑定核对（合同 / 输入清单 / 产物 / Review 身份，
**package 与 record 按 content hash 从库里复读**，requirements 同样按存储字节核对，
「要求不暗中放宽」）→ scope epoch 与 read-set（含 `support_set_revision`，AER §7
明确要求查支持集合版本而不只逐条证据）→ `purpose=ACCEPT` 的 `ValidityWitness`
当前有效（epoch 屏障 + 截止时间 + freshness，用契约自己的 `is_fresh_for`）→
在途/取消姿态 → `acceptance_rules.acceptable()` → 写入 → 事件 → 回执。

- `accept_review` 写 `Acceptance`，**不动义务生命周期**（§25.1 裁决 4：ACCEPT 与
  GoalResolution 是两个动作）。
- `commit_goal_resolution` 额外要求：resolution 的 verdict 是 ACCEPT、采用的
  method instance 当前 ADOPTED、子 resolution 都存在、resolution 不得与它绑定的
  Review 互相矛盾、**根要求全覆盖**（缺一条 → `ROOT_CRITERION_MISSING`，形成 0 次）、
  compound 的每个 REQUIRED 孩子在库里有有效 `Acceptance`（贡献集合**从库里读**，
  命令声称的贡献只做交叉核对，不符即 `COMPOUND_FACTS_CONTRADICT_STORE`）。
  成功后 `set_lifecycle(SATISFIED, resolution_ref=…)`；若该义务上挂着 admitted
  demand，先 `withdraw_demand`（TG 裁决 9：撤需求是结束共享，不是结束义务）。
- 「错误宣布完成 = 0」的守卫：Mission 根 Resolution 只由本方法形成，且当
  requirements 声明了 `delivery_contract_ref` 时，必须有一张 `DeliveryReceipt`
  达到要求阶段、且该回执引用的 `Acceptance` 当前有效；**从不读 `Mission.status`
  字符串**（测试用 `_check_delivery.__code__.co_names` 断言）。
- `IndependenceFacts` / `ExecutionPosture` 在命令上**没有默认值**：空值分别读作
  「没人生产过它、审阅者没有任何权限」和「没有在途、没有取消」，是最宽松的世界，
  给默认等于把「忘记查」变成「可接受」。

### 3. `planning/htn/observers/`

- `PredicateObserver` 协议：`observer_id` / `predicate_ids()` / `observe(...)`，只读。
- `Observation` 三态：`OBSERVED`（带 `ObservationRecord`）/ `OBSERVER_UNAVAILABLE`
  （**不带任何 record**，因为 record 有极性，把停机说成极性就是伪造 FALSE）。
- `observed()` 在构造处拒绝一切不可采纳的形状：observer 不在签名的
  `observer_ids` 里 → 拒；CLOSED 谓词的负观察缺
  `AUTHORITATIVE_WITH_SCOPE` / 覆盖范围 / watermark → 拒（§6.6 C28，无
  negation-as-failure）。`QueryCompleteness` 里能支撑否认的只有
  `AUTHORITATIVE_WITH_SCOPE`，本片把它命名为 `COMPLETE_COVERAGE`（任务书里的
  「QueryCompleteness=COMPLETE」对应这一枚举值，枚举没有更弱的 COMPLETE 成员）。
- `code.py`：5 个观察器覆盖种子 code 域全部 6 个谓词
  （repo-observer / workspace-observer / history-observer / changeset-observer /
  test-observer）。命令走**一条允许表**（`READ_ONLY_GIT` / `READ_ONLY_PYTHON`），
  在 spawn 之前拒绝任何修改型子命令；每条命令都有超时；超时 / 缺二进制 /
  结论不明都是 `OBSERVER_UNAVAILABLE`。`code.working-tree-clean` 是 CLOSED：
  `git status --porcelain` 确实穷举了工作树，空输出是完整查询、非空输出是权威负观察。
  `code.test-is-failing` 默认返回 UNAVAILABLE（只读收集只能回答「目标是否存在」，
  回答「是否失败」需要真跑测试，由 `allow_test_execution` 显式开启，跑时带
  `-p no:cacheprovider` 保证不写工作树）。
- `appworld.py`：5 个观察器覆盖种子 appworld 域全部 6 个谓词。客户端注入，
  结构上由 `evaluation.appworld.AppWorldEpisode` 满足（只用 `observe_public_api`）。
  **服务没起 / 传输失败 → UNAVAILABLE**，绝不伪造 FALSE；只有「主机真的答了、
  并且拒绝这个读」才是负观察。`appworld.action-confirmed` 是 CLOSED：没有
  收据台账可枚举时返回 UNAVAILABLE 而不是便宜的 FALSE。

### 4. `planning/htn/synthesis.py`

- `SynthesisRequest`：目标签名、要覆盖的父要求、已绑定参数、**本部署真实注册的
  Operator**（含 `unavailable_capabilities`）、已有方法为什么不适用（四个轴分开）、
  advisory-only 的近似方法、禁止字段清单。构造时用
  `authority_claims()` 断言不含任何 `SYSTEM_BOUND_FIELDS`（从 `planning.planner`
  导入同一份清单，避免两处漂移）；只查**结构键**，域参数里恰好叫 `scope` 的值不算
  声明（与 `planner._refuse_authority_claims` 同一条界线）。
- `accept_response()`：`parse_method_proposal` → `registry.admit` → 最多
  `TRIAL_ADMITTED`。模型自填 `registry_status` / `author` 由准入协议拒绝并**留痕**，
  本模块不做静默规范化；块格式错误抛 `ContractError`（§18.5 C8 的有界修复），
  不另开请求。回执若越过 `TRIAL_ADMITTED` 直接抛错。
- **没有 `promote`**：晋级需要 P8 的离线多实例评测，`registry.promote` 对任何目标
  都答 `PROMOTION_NOT_AVAILABLE`。
- 角色模板 `method_synthesizer` / `method-synthesizer-v1`：新增**角色**而不是
  planner 的新版本（§18.5：新 role 用途不得伪装 TaskCritic 进入错误 Task 预算），
  开销记在 `mission_planning` 账户；经 `register_template` 注册，旧模板原文不动。

### 7. 审阅修复（2026-09-16，「需修后合并」）

四处 P0 + 三处 P1 已全部修复，逐条的问题、修法与证据见 `journal.md` §6。摘要：

| 条目 | 修法 | 新增测试 |
|---|---|---|
| P0-1 read-set 只查 5/11 通道 | 抽 `orchestrator/_read_set.py`，两侧全覆盖；`plan_commits` 116 条全绿 | §3b 共 13 条（含反驳的 observation / 撤销的 authority / 重规划的义务各一条） |
| P0-2 交付回执不复读（变异 R9 存活） | 每张回执都复读并校验 mission / 入库 / 根义务闭包 / validity=CURRENT，任一不合格即 `DELIVERY_RECEIPT_INVALID` | §8b 共 6 条 + 改 1 条期望 |
| P0-3 允许表只看子命令（`--output=` / `--junitxml=` 实测可写文件） | 按完整 argv 校验 + `--` 分隔 + 拒绝以 `-` 开头的实参；收集改在只读临时副本；每观察器预算 | §7 / §7b 共 20 条（含一条真跑的「没有门就真会写出文件」反例） |
| P0-4 全部 `ValueError` 当拒绝（畸形 JSON / 身份不符变 FALSE） | `classify_read_error()` 三态分类，默认 UNAVAILABLE；台账不可解析 / 无 scope 也不得否认 | §7c 共 14 条 |
| P1-5 `_replayed` 全扫 | 事件 key 由命令 id 派生（`append_event` 的唯一键即幂等保证）+ `count_events` 定向查找 + 分页 | 2 条 |
| P1-6 `_accepted_occurrences` | 读子义务 lifecycle；按 occurrence 聚合；事件记 `contributing_acceptances` | 4 条 |
| P1-7 reason 码 / author 锁 | `BAD_COMMAND` / `BAD_PRINCIPAL`；`accept_response` 的 author 固定 MODEL，`author_override` 为 keyword-only | 8 条 |

## 二、明确不在本片

- 不改 `event_handler.py`、`hierarchical_dispatch.py`、`plan_commits.py`、
  `commit_service.py`、`contracts/`、`graph/`、`storage/`、`artifacts/`。
- `ResolutionCommitsMixin` **没有**加进 `CommitService` 的基类列表；测试用
  `class ResolutionService(ResolutionCommitsMixin, CommitService)` 组合验证，
  以免与在途的 P2.3b 审阅修复冲突。
- 观察器**没有**注册进 `PredicateRegistry`；MethodSynthesizer **没有**接
  `BaseAgent`；`allocate_v2` **没有**接派发。
- 不提交、不跑全量回归。

## 三、给第二部分的接线清单

1. `orchestrator/commit_service.py`：`CommitService` 基类列表加
   `ResolutionCommitsMixin`（放在 `PlanCommitsMixin` 之后）。
2. `storage/`：**两张表 + 迁移**（审阅确认留给第二部分）——
   `acceptance_commit_receipts` 与 `delivery_receipts`，唯一键
   `(mission_id, command_id)` + `intent_hash`。当前 `plan_commit_receipts` 的 CHECK 是
   `new_plan_revision > base_plan_revision`——plan 形状的表，acceptance 不产生 plan
   revision——所以本片改用 `CommandReceipt`，从本命令追加的事件投影出来（事件的
   `idempotency_key` 由**命令 id** 派生，`Store.append_event` 的唯一键路径本身就是
   「同命令两次 = 一次提交一张回执」的 durable 依据）。表落地后：
   `_committed_event` 的分页查找换成一次 keyed read；`DeliveryReceipt` 由命令参数
   改为库侧记录。
3. `orchestrator/hierarchical_dispatch.py`：必需孩子全部有有效 `Acceptance` 后
   调 `commit_goal_resolution`（`root_review_ready()` 已经算好了门；
   `terminal()` 已经在读 `adopted_goal_resolution`）。根 Mission 走
   `purpose=MISSION_FINAL` + `required_delivery_stage`。
4. `scheduling/` 接派发：`allocate_v2` 接进调度循环，
   `FrontierV2.needs_refinement` 回灌给 planner（P2.3b 的
   `advance_compound_phases` 是它的消费者）。
5. **L2 验收所需的业务状态谓词**（账号存在 / 清单内容 / 金额 / 变更路径 / 依赖声明），
   仓库目前一条都没有；清单与每条的落地条件见 `journal.md` §6 末节。建议与下一条
   同片做，因为都要动 `seed_methods/<domain>/predicates.json` 的内容哈希。
6. 观察器注册进 `PredicateRegistry` 驱动的取证管线：
   `code_observers(worktree)` / `appworld_observers(episode)` →
   按 `predicate_ids()` 建索引 → `observe()` 的 `ObservationRecord` 写
   `htn_store.insert_observation`；`OBSERVER_UNAVAILABLE` 走
   `ReadinessReason.OBSERVER_UNAVAILABLE` 而不是写一条假记录。
7. MethodSynthesizer 经现有 `BaseAgent` dispatch：
   `build_request()` → 渲染 `METHOD_SYNTHESIZER` 模板 → 模型 → `accept_response()`；
   费用记 `mission_planning`，不进任何 Task 预算。

## 四、偏差与契约变更请求

1. **`accept_review` / `commit_goal_resolution` 取两个参数**
   `(command, principal)`，与任务书写的 `accept_review(command)` 不同。理由：
   「认证」这一步的实质是命令自称的 `issued_by` 与**另外呈递**的 principal 对照
   （AER §7 step 1，`PlanCommitsMixin` 同构）；把 principal 塞进命令就没有可对照的
   第二方了。
2. **命令回执不落 `plan_commit_receipts`**（见接线清单第 2 条）。这是本片唯一的
   实质设计偏差，因为 `storage/` 本片只读。**契约变更请求（审阅已确认留第二部分）**：
   新增 `acceptance_commit_receipts` + `delivery_receipts` 两表与迁移，唯一键
   `(mission_id, command_id)` + `intent_hash`。
3. `CommitGoalResolutionCommand` 新增必填字段 `decided_at_ms`。理由：witness 的
   epoch 屏障 / 截止时间必须对着**做决定的当下**检查，用 `witness.as_of_ms` 会让
   截止时间检查变成空转。
4. `planning/htn/observers/code.py` 的 `SuiteObserver` 原名 `TestObserver`，
   改名以免被 pytest 当测试类收集。
5. `plan_commits.py` 的六个解析器（`_goal_state` … `_authority_state`）保留为**一行
   委托**的 seam：解析器本体搬进 `_read_set.py`，但那六个名字仍在 mixin 上，并通过
   checker 的 `resolvers=` 覆盖映射接回去——它的 mutation 测试逐个削弱单个通道时打的
   就是这些名字，所以 seam 保留是为了让那条测试仍然真的削弱实现，而不是把测试改成能过。
6. `_read_set.SemanticReadSetChecker` 有两个刻意的配置点：
   `allow_task_control_channels`（plan 侧关 = 抽取前的行为；accept 侧开，因为
   `eligibility.build_read_set` 会在 TASK 通道里放 `<task>#channel` 形式的 id）与
   `budget_grant_resolver`（P2 无库侧权威，claim 非 0 即 unresolved，fail closed）。
7. `runtime/role_templates.py` 在 HEAD 上本来就不满足 `ruff format`
   （`git show HEAD:… | ruff format --check` 为「would be reformatted」），
   本片只**追加**一段格式合规的新模板，没有对该文件跑 `ruff format`，
   以免把无关代码卷进 diff。
