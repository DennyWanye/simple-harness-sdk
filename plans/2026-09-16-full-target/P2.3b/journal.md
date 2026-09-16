# P2.3b 施工日志

- 基线：`simple-harness-sdk` main HEAD `e78a8cc`（P2.3a 已提交定稿）
- 交付方式：测试先行；记录用中文，代码/注释用英文

## 1. 文件与行数

| 文件 | 变更 | 行数 |
|---|---|---|
| `src/agent_orchestrator/orchestrator/hierarchical_dispatch.py` | 新建 | 1376 |
| `src/agent_orchestrator/orchestrator/event_handler.py` | 改 | +206 / −4 |
| `src/agent_orchestrator/artifacts/versioning.py` | 改 | +201 / −1 |
| `src/agent_orchestrator/artifacts/workspace.py` | 改 | +34 / −0 |
| `tests/orchestrator/full_target/test_hierarchical_event_flow.py` | 新建 | 1992（102 条） |
| `tests/orchestrator/full_target/test_versioning_v2_manifest.py` | 新建 | 925（32 条） |
| `plans/2026-09-16-full-target/P2.3b/{plan,journal}.md` | 新建 | — |

`git diff --stat` 只含以上允许文件；未提交、未 push、未 stash/checkout。

## 2. event_handler.py 的 diff 逐段说明

> **审阅后更正**：修复前是 11 段，修复后是 **13 段**（新增 `_plan_integrity_stop` 与 `_next_attempt` 输入处的 `except GraphIntegrityError`）。完整的 13 段表见文末"审阅修复"节，以那一张为准；下表保留首轮的段落说明。

| # | 位置 | 内容 | 对 legacy 的影响 |
|---|---|---|---|
| 1 | import 区 `..graph.projection_validation` | 新增 `GraphIntegrityError` | 无 |
| 2 | import 区 `..runtime.output_blocks` | 行内追加 `repair_hint`（唯一一行 legacy 文本改动，只加了一个名字） | 无 |
| 3 | import 区 `..runtime.role_templates` | 新增 `PLAN_REVISION_PROPOSAL_TAG` | 无 |
| 4 | import 区 `.hierarchical_dispatch` / `.plan_commits` | 新增 `HierarchicalDispatch`、`is_hierarchical`、`PlanPrincipal` | 无 |
| 5 | `__init__` | `self._hierarchical: HierarchicalDispatch \| None = None` | 默认 `None`，legacy 部署行为不变 |
| 6 | `store`/`commit` 属性后 | 新增 `hierarchical` 属性、`install_hierarchical()`、`_new_mode(mission)`；**唯一**一处判定语义版本 | 纯新增 |
| 7 | `_collect_plan` 回声校验后 | `new_mode = self._new_mode(mission)`；非 None 时走 `_collect_plan_hierarchical` 并 return | legacy 走原 `parse_task_graph_proposal` 路径，一行未改 |
| 8 | `_collect_plan` 之后 | 新方法 `_collect_plan_hierarchical`：导入用量、settle intent、失败走既有 `_planning_rejected`（reason `proposal_unreadable` / `plan_commit_refused`），`BlockError` 时把 `repair_hint` 放进持久化拒绝明细 | 纯新增 |
| 9 | `_collect_attempt` 尾部 | 结果落库后调 `advance_compound_phases`（新模式） | legacy 分支 `new_mode is None`，不执行 |
| 10 | `_decide` 终态判断 | `if live and all(... COMPLETED)` 改写为 `settled = new_mode.root_review_ready(...) if new_mode else bool(live) and all(...)`；条件表达式的 legacy 半边逐字节保留，只换了缩进 | 语义等价（`bool(live) and all(...)` ≡ `live and all(...)` 作为布尔判据） |
| 11 | `_next_attempt` 开头 | `intercept_worker_dispatch` 拦截 compound，记 `NEEDS_REFINEMENT` 并 `return False` | legacy `new_mode is None`，直接落到原第一条语句 |
| 12 | `_next_attempt` 输入计算 | `merge_accepted(...)` 包进条件表达式；新模式改调 `new_mode.attempt_inputs(...)` | legacy 调用文本逐字节保留，只换缩进；`except ArtifactConflict` 路径不变 |

event_handler.py 的删除行只有 4 行，全部是上面第 2、10、12 段被重排的原行（`git diff | grep ^-` 已核对）。

零回归的最强证明不是字节比较而是"根本没进去"：`test_the_legacy_path_never_enters_the_assembly_at_all` 装一个所有入口都 `raise AssertionError` 的 `HierarchicalDispatch` 子类，跑完整 legacy Mission 到 `COMPLETED`。

## 3. 装配逻辑的几个决定

1. **一轮只装配一条 `RefineOperation`**。多条操作合并后的完整再验证是 §24.1 裁决 8 / P3.1 的范围；本片显式 `ContractError`，不默默取第一条。
2. **重编译的判据是"换快照能不能修"**，不是"错误看起来严重不严重"。`RECOMPILABLE_REFUSALS` 只含四个 staleness 闸门、两个结构闸门（`STRUCTURE_INVALID`、`PLAN_NOT_PRESERVED`）与两个预算闸门。身份类（`PRINCIPAL_MISMATCH`、`SCOPE_NOT_AUTHORIZED`）、模式门（`SEMANTICS_NOT_HIERARCHICAL`）、损坏（`MISSING_SEMANTIC_BINDING`）、命令本身的缺陷（`DELTA_NOT_COMMIT_READY`、`OR_NOT_RESOLVED`）一律一次即停。有一条测试反向锁定：集合里的每个名字都必须是 `plan_commits` 真的会抛的。
3. **不用整数闸门做并发控制**。P2.3a 定稿后 layer 模式的串行化点是 plan revision，`commit_plan_revision` 不推进 `graph_version`。`build_command` 只是把当前值原样带上（ADR-13 要求），有测试锁定"提交一次修订后 `graph_version` 不动"。
4. **`mission_admits_work` 取"未终止"而不是"已 ACTIVE"**。选择闸门问的是"这个 Mission 的计划还能不能被行动"，而 hierarchical Mission 在第一次细化时还在 CREATED/PLANNING；能不能真的派发是 TG §8.1 第三层（派发事务）的事，本视图不做那个决定。
5. **无 plan revision 时读 seed 网络**。`seed_network()` 用 `RootNetwork`（编译器自己的形状）由唯一一条 `task_semantics` 构造；0 条或 >1 条都是 `GraphIntegrityError`，不猜根。
6. **未冻结的 manifest 返回空输入而不是抛异常**。生产者没完成留下的是 symbolic binding，这是 DATA 闸门的 `WAITING_DATA`，不是物化失败。
7. **`Acceptance` 定义 occurrence 的 ACCEPTED**，不抄 `Task.status`。COMPLETED 而无 Acceptance 记为 `SETTLED_OTHER`（TG 裁决 1：不知道怎么结束的不结算任何东西）。
8. **前提见证按 condition digest 取**，链路是 `MethodInstanceDraft.precondition_witnesses[].witness_ref` → `validity_witnesses` 行。直接拿 `ValidityWitness` 猜 digest 会让一个见证去许可它没算过的前提。
9. **`repair_hint` 接线在 `BlockError` 路径**：提示进入持久化的 `PlanningRejected` 明细，由既有 `_planning_rejections` 带给下一次提案；不另开请求（§18.5 C8）。

## 4. versioning.py / workspace.py

- legacy `topological` / `merge_accepted` / `collect_upstream_inputs` 函数体**逐字节未变**，测试用三者源码串接的 sha256 锁定（`fe215c0d…8fea4`）。versioning.py 唯一的删除行是 `from typing import Any`（改成 `from typing import TYPE_CHECKING, Any`），在三个函数之外。
- 新增：`resolve_input_manifest`（只看本 occurrence 声明的 `DataRequirement`，执行路径先 `require_topological_order`，`check_topology=False` 留给诊断路径）、`manifest_upstream_inputs`（按 `MaterialisationEntry` 产出 `UpstreamInput`，同路异 hash → `ArtifactConflict` 且消息里点名候选 artifact）、`materialise_v2`（字节仍走 `read_verified`，放置走 workspace 新入口）。
- `Workspace.materialise_manifest(entries, blobs)`：只写清单条目；缺字节 → `WorkspaceError`（不造空文件），只读副本 → `WorkspaceError`。旧入口（`materialise_inputs`、`create`、`verification_copy`…）未动。
- 诊断路径未搬：测试断言 `observability/traces.py` 与 `observability/evaluation.py` 仍调 `merge_accepted`、且不含 `materialise_v2`（§24.1 裁决 11）。

## 5. 测试

`test_hierarchical_event_flow.py`：83 条。
- 一轮计划：提案→编译→提交、落库、事件、`compiled_from_proposal_id`、seed 网络、读回网络、无标签块 / 权限字段 / 多操作 / legacy Mission / 缺 PlanningWorld 的拒绝。
- 有界重编译：`READ_SET_STALE` 重编译一次后成功（并断言第二次用的是**新读**的快照）、成功时不记拒绝事件、用尽上限记 `PLAN_COMMIT_REFUSED`（`attempts=2`、`rebased=false`）、上限可配、拒绝后什么都没写、不可重编译原因一次即停、不碰 `commit_graph_change`、第二次同样回复被编译器结构性拒绝而不是产生第二个修订。
- compound 门：拦截、事件、`READY` 不影响、primitive 不被拦、不建 Attempt、无绑定→`GraphIntegrityError`。
- 投影读法：ready/running/terminal/根评审在把 `Task.status` 写成 `READY`/`COMPLETED` 前后完全一致；listing 来自投影不是 tasks 表；孤儿 membership → `GraphIntegrityError`；根评审要全部 gating 孩子有 CURRENT Acceptance（带完整 review package/record 链）。
- phase reducer：planning_ready → waiting_children → composition_review；resolved → resolution_committed；primitive 被拒；只记事件不建 Attempt；legacy 状态完全不参与。
- manifest 输入：无 DATA 声明→空；生产者未完成→空且 `WAITING_DATA`；未知 task→`GraphIntegrityError`。
- legacy 零回归：`_ExplodingDispatch` 跑通完整 legacy Mission；开关两次跑事件字节逐条比较（只归一化环境量：wall clock、evidence root、`execution_id`/`run_hash`/`knowledge_id`/`context_version`/`waiting_age`，并整类排除 `HeartbeatReceived` 这个活性采样）；legacy 跑不产生任何新事件类型；`_new_mode(mission)` 只有 4 个调用点；`merge_accepted(` 在 event_handler 里仍只有 2 处。
- 变异自证：6 个变异 × 2 条（真实实现下见证断言通过 / 变异下见证断言失败）= 12 条。变异体为：放大重试上限、把终止 Mission 也算可重编译、关掉 compound form 门、用 COMPLETED 行数决定根评审、组合评审忽略孩子、悄悄丢掉无绑定的 membership。

`test_versioning_v2_manifest.py`：32 条。只按 manifest 物化；必需端口无 requirement 时报 `UNBOUND_REQUIRED_PORT` 而不是从祖先补；ORDER-only 前置不进（并用同一世界跑 legacy `collect_upstream_inputs` 证明旧语义**确实**会带进来）；同路异 hash 显式冲突且点名候选、不按拓扑先后挑；同 hash 不算冲突；两个 namespace 同相对名在 `preserve_source_namespace` 下分开；未冻结清单不能物化；环图在 resolve / 输入集 / 物化三条路径都抛 `GraphIntegrityError` 且诊断读法仍可用；legacy 三函数源码 hash 与行为（链式覆盖、独立分支冲突、坏库仍给序）不变；诊断模块仍走 legacy。

## 6. 门槛结果

| 项 | 结果 |
|---|---|
| `test_hierarchical_event_flow.py` + `test_versioning_v2_manifest.py` | 134 passed |
| `tests/orchestrator/full_target`（全目录） | 2091 passed, 1 skipped（PANDA 无真解析器；含并发进行的 P2.3c 新测试） |
| `step02`–`step08` + `test_critic_test_evidence_order.py`（审阅后重跑，一批） | 345 passed, 7 skipped（真 provider） |
| `p32 p33 p34 p35 p36`（审阅后重跑，一批） | 1467 passed, 12 skipped, **1 failed**（同一条已知项） |
| `ruff check src/ tests/orchestrator/full_target` | All checks passed |
| `ruff format --check`（四个改动文件 + 两个测试文件） | **审阅后更正**：首轮两个新测试文件各有约 4 处会被重排（审阅指出），现已用仓库锁定的 ruff 格式化，两文件 `--check` clean；`hierarchical_dispatch.py` / `versioning.py` / `workspace.py` 亦 clean。`event_handler.py` 在 HEAD 就不是 format-clean（已用 `git show HEAD:… \| ruff format --check` 核对），我新增的段落在 `format --diff` 里零命中，未动其格式基线 |
| `mypy src/agent_orchestrator` | 17 errors（= 基线，未增） |

唯一失败：`p33/test_p33_source_dependencies.py::test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged`，锁的是 `memory/verified_knowledge.py::KnowledgeIndex.check` 的 AST hash。该文件本片未改（`git diff --stat` 无此文件），属交接说明里"p33 已知 1 个可忽略"。

另注：`tests/orchestrator/gap_phase1` 在本机整目录 collection error（`'asyncio' not found in markers`，缺 asyncio marker 注册/插件），与本片无关，亦不在门槛清单内。

## 7. 偏差与遗留

1. **一轮一条 refine**（见 §3.1）。多操作合并的再验证留给 P3.1。
2. **Manager 图变更仍走 legacy `commit_graph_change`**。新模式 Manager 提案改走 `plan_commits` 属 P3.1；本片只接了 Planner 一条入口。
3. **`HierarchicalDispatch._recorded_outputs` 目前返回空**。`AcceptedOutputsIndex` 的 outputs 需要"某个 Acceptance 的哪个输出端口对应哪个 artifact"这条索引，写入方是 P2.3c 的根验收/结果落库。现在的后果是可见且正确的：声明了 DATA 端口的消费者停在 `WAITING_DATA`，而不是悄悄退回全祖先汇入。这是本片最需要 P2.3c 接上的钩子。
4. **`install_hierarchical()` 需要部署提供 `PlanningWorld`**（task types / schemas / method registry / predicates）。没有就显式 `ContractError`，不回落 legacy planner。真实部署怎么装配这个世界（种子方法库 loader）不在本片范围。
5. **compound 的 phase 只落事件，不写 `Task.status`**。TG §7 说粗粒度状态是展示，`COMPOUND_DISPLAY_STATUS` 给出映射，但把它写回 tasks 表需要碰 `state_machine.next_task`，不在允许清单内。

## 8. 契约变更请求

无。`contracts/`、`graph/`、`planning/`、`storage/`、`commit_service.py`、`plan_commits.py`、`scheduling/`、`observability/` 均未改。

---

# 审阅修复（独立审阅：需修后合并）

审阅结论"装配链、零回归、v2 物化均合规，11/11 变异捕获"照旧；以下是两处严重问题与五处应改项的处理。修复后 `event_handler.py` 的 hunk 数从 11 变为 **13**（本节末列出全表，正文 §2 的段数表以此为准）。

## R1（严重）GraphIntegrityError 逃逸到共享 run 循环

**问题**：`GraphIntegrityError` 是 `RuntimeError`，而 `_cycle` 只宽恕 `StoreBusy` / `CommitRejected` / `IllegalTransition`。一个 Mission 的语义绑定损坏会从 `root_review_ready` / `advance_compound_phases` / `intercept_worker_dispatch` / `attempt_inputs` 抛出，终结整个 `run()`，连带其他 Mission。

**修法**：

1. **专门的异常与专门的消息**。新增 `PlanIntegrityError(GraphIntegrityError)`，带 `code` / `mission_id` / `subjects`，并覆盖 `__str__` 与 `diagnose()`。两个构造器：`missing_bindings(mission_id, task_ids)`（code `semantic_binding_missing`）与 `root_not_identified(mission_id, task_ids)`。基类那句 "N node(s) could not be ordered; cycle: unknown" 描述的是错的缺陷——这里没有任何东西无序，是**含义缺失**，让运维去找环会把人送错方向。`hierarchical_dispatch.py` 里三处裸 `GraphIntegrityError((...), ())` 全部换掉（`grep` 归零）。
2. **读与执行分开**。`network()` 拆成 `_read_network()`（返回 `(snapshot, PlanIntegrityError | None)`，把无绑定的 member 排除在快照之外并把原因带回来，同时过滤掉指向它的 order/data 边）+ 一层薄壳 `network()`（有错就抛）。`seed_network()` 同样拆成 `_read_seed_network()`。
3. **携带机制真的走到**。`read(mission_id, *, tolerate_integrity=False)`：默认是执行答案（抛）；`True` 是解释答案——同一个错挂在 `ActivePlanView.integrity_error` 上，于是**每个** occurrence 报 `GRAPH_INTEGRITY`、两条 frontier 都空、`root_review_ready` 返回 False。这正是 §24.1 裁决 11 要的"损坏停掉该 scope 的全部派发，诊断照旧可看"，`plan_view(..., integrity=...)` 新增入参承接它。
4. **四个分派点转成该 Mission 的 stop**。新增 `Orchestrator._plan_integrity_stop(mission, error)`：先由 `HierarchicalDispatch.record_integrity_failure()` 落 `PlanIntegrityFailed` 事件（payload 含 `code` / `subjects` / `cycle` / `diagnose`，**不含**拓扑序——损坏投影的健康前缀是偏序不是计划），再按状态 `fail_planning`（PLANNING）或 `fail_mission`（ACTIVE，级联在途工作），然后 `_release_mission`。Planner 分支的 `except` 拆成两条：`GraphIntegrityError` → integrity stop（损坏不是坏提案，再问 Planner 也长不出一条语义绑定，不烧 attempts）；`ContractError` → 原来的 `proposal_unreadable`。
5. **端到端测试**：`test_one_damaged_mission_does_not_take_another_down_with_it` —— 同一个 `run()` 里一个 hierarchical Mission 绑定损坏、一个 legacy 单任务 Mission 正常跑完，断言后者 COMPLETED、前者 FAILED 且只有前者有 `PlanIntegrityFailed`。（两个 Mission 共用 provider，谁先被驱动是调度事实，所以 planner 脚本改成按 package 内容选回复的 callable，而不是位置脚本。）另加 4 条：`_decide` 守卫、`_next_attempt` 守卫、默认 `read` 抛、`tolerate_integrity` 携带（+ 每个 occurrence GRAPH_INTEGRITY、两条 frontier 空、消息不提 cycle、`cycle == ()`）。

## R2（严重）`_collect_plan_hierarchical` 零行为覆盖

原来唯一的"测试"是源码字符串断言。现在用真 `Orchestrator` + 脚本化 planner 驱动该分支（`_orchestrator` / `_seed_hierarchical` / `_drive` 三个辅助）：

| 测试 | 覆盖 |
|---|---|
| `test_the_handler_commits_a_scripted_hierarchical_planner_reply` | 整条 parse→compile→commit 经 event_handler 走通；`PlanPrincipal`（含 `epoch` 读取）真的被构造 |
| `test_the_handler_advances_the_compound_phase_after_committing` | 提交后 `advance_compound_phases` 被调用，且事件顺序在 `PlanRevisionCommitted` 之后 |
| `test_the_handler_settles_the_planner_intent_after_a_commit` | settle 路径：该 Mission 没有残留 open intent |
| `test_a_malformed_block_reaches_the_planning_rejection_with_its_repair_hint` | 坏块 → 持久化 `PlanningRejected.detail.repair_hint` + `block_defect == "invalid_json"` |
| `test_a_malformed_block_rides_the_existing_attempt_ladder` | 有界修复：正好两次（`max_planning_attempts`），不另开请求，然后 fail |
| `test_a_refused_plan_commit_reaches_the_planning_rejection` | 被拒提案 → `PlanCommitRefused` 事件 + `PlanningRejected(reason="plan_commit_refused")` |
| `test_a_turn_that_did_not_commit_is_a_planning_rejection` | COMMITTED 闸门：直接喂一个 `state=FAILED` 的 turn，断言 `"planner turn failed"` 进入拒绝明细 |
| `test_a_damaged_plan_stops_that_mission_through_the_planner_branch` | 损坏 → `PlanIntegrityFailed(code="root_not_identified")` + Mission FAILED + **没有** `PlanningRejected` |

## R3 ruff format

两个新测试文件已用仓库锁定的 ruff 格式化（`ruff format --check` 两个文件均 clean）。`event_handler.py` 不动其 format 基线（HEAD 本就不 clean），我新增段落在 `format --diff` 里零命中。

## R4 事件归一化改成按字段名

原来的 `abs(value) >= 1e9` 数值规则会顺手遮住 `receipt.effective_limits.max_rss_bytes` 这类确定性配置值——字节上限是决定，遮住它等于那处没在比较。现在：

- 先用探针跑两遍同一个 Mission，**枚举**真正有差异的字段路径（共 11 条），据此写死名单：`{execution_id, run_hash, knowledge_id, claim_id, context_version, waiting_age}`；
- 字符串/列表里的同一批标识（`final_report.knowledge[]`、`lineage.knowledge[].id`、`lineage.edges[].produced`）用一条 `observation:<64hex>` 正则归一；
- 数值规则、`ctx-` 与 32-hex 正则全部删除；
- `HeartbeatReceived` 仍整类排除（整个事件是活性采样，`liveness.progress` 会随采样时刻变）；
- 比较键从 `event.type` 改为 `type|task_id|attempt_id`：同类型、不同 Task 的两条事件是两个事实。

顺带修掉一个首轮没暴露的**间歇失败**（3 次连跑里挂 1 次）：`AllocationDecided.score` 是 `waiting_age` 的加权函数（权重 0.10）并四舍五入到 4 位，两次运行的微秒级差异偶尔会跨过第四位小数。做法是先把探针改成跑 6 对并统计每个字段路径的差异次数（`waiting_age` 2/6、其余 6/6），据此把 `score` 一并列名并写明理由；`tier` **不**宽恕——它是 1 秒的阈值，离这些亚毫秒读数很远，真要翻转就该让比较失败并被人看一眼。改后 `legacy_mission_produces` 单测连跑 10 次、两个测试文件整体连跑 5 次全绿。

## R5 `_ExplodingDispatch` 覆盖三条路径

参数化成三例（复用 step02/step03/step05 fixture）：单任务（含修复与 Critic）、并行 DAG（含产物下游流动与 `_next_attempt` 输入路径）、动态 DAG（Worker 提案经 Manager 进图）。三例都在装了"所有入口即炸"的 dispatch 下跑到 COMPLETED。`_ExplodingDispatch` 同时补上 `record_integrity_failure` / `admit_method_proposal` / `compile_proposal` / `build_command`。

## R6 两个 src 内无调用方的入口

`admit_method_proposal` 与 `materialise_v2` 的 docstring 都加了 **"Not called from `src` yet — P2.3c wires it"** 段，并说明为什么留在这里（前者：MethodSynthesizer 是独立 intent 与独立预算账户，开 intent 属 allocator 那片；后者：P2.3b 决定的是输入**集合**，把物理写入搬过来等于改验证副本与保护文件的来源）。同时列入下方 §7 补充。

## R7 两处小改

- `read()` 里 `occurrence_outcomes` / `witnesses` / `accepted_outputs` 各算一次并传下去（原来每个 occurrence 重算一遍，一次读计划变成 O(n²) 次 store 往返）；`input_result` / `accepted_outputs` 新增可选入参承接。
- `_is_refine` 改 `isinstance(RefineOperation)`：`ProposeSuccessorOperation` 也带版本化引用和 goal 形状的 id，结构探测会把它当成 refine 去编译错的东西。

## 修复后的 event_handler.py 13 段（正文 §2 表以此为准）

| # | 位置 | 内容 |
|---|---|---|
| 1 | import `..graph.projection_validation` | `GraphIntegrityError` |
| 2 | import `..runtime.output_blocks` | 行内追加 `repair_hint` |
| 3 | import `..runtime.role_templates` | `PLAN_REVISION_PROPOSAL_TAG` |
| 4 | import `.hierarchical_dispatch` / `.plan_commits` | `HierarchicalDispatch`、`is_hierarchical`、`PlanPrincipal` |
| 5 | `__init__` | `self._hierarchical = None` |
| 6 | `store`/`commit` 属性后 | `hierarchical` 属性、`install_hierarchical()`、`_new_mode()`、**`_plan_integrity_stop()`** |
| 7 | `_collect_plan` 回声校验后 | 新模式分派 + return |
| 8 | `_collect_plan` 之后 | 新方法 `_collect_plan_hierarchical`（含 integrity / ContractError 两条 except） |
| 9 | `_collect_attempt` 尾部 | `advance_compound_phases`，包 integrity 守卫 |
| 10 | `_decide` 终态判断 | `settled` 条件表达式 + integrity 守卫 |
| 11 | `_next_attempt` 开头 | compound 拦截 + integrity 守卫 |
| 12 | `_next_attempt` 输入计算 | `attempt_inputs` 分支 |
| 13 | `_next_attempt` 输入异常 | 新增 `except GraphIntegrityError` 在 `except ArtifactConflict` 之前 |

删除行仍只有 4 行（同正文 §2），`git diff | grep ^-` 已核对。

## §7 遗留补充（给 P2.3c 的阻碍）

正文 §7 的五条之外，审阅另发现四条，都要 P2.3c 处理，本片无法在允许的文件范围内解决：

- **(a) 新模式目前不能端到端派发。** `commit_plan_revision` 不创建 legacy `Task` 行，`list_tasks` 因此为空，legacy allocator 看不到任何任务，`_decide` 在 `if not tasks` 处提前返回。P2.3c 必须把 occurrence 桥成可派发工作，或把 allocator 的输入换成 `ready_occurrences()`。顺带的现象：hierarchical Mission 提交修订后停在 PLANNING（没有 `commit_task_graph` 把它推到 ACTIVE），本片两条守卫测试用 `_force_active` 显式转换而不是假装它会自己发生。
- **(b) `_next_attempt` 只接了 form 门，没接 `evaluate_readiness`。** 一个声明了 DATA 端口的 primitive 目前不会被 readiness 拦住；只是因为 `_recorded_outputs` 为空、manifest 未冻结、`attempt_inputs` 返回空，它会**无输入静默执行**。P2.3c 必须同时补上输出索引（R6/§7.3）与 readiness 闸门，只补一个都会留下更差的状态。
- **(c) Planner 提示词与包仍是 legacy。** `role_for_task` / 模板选择与 `is_hierarchical` 无关，`_create_planner_intent` 造的还是 legacy 包（没有方法库、任务类型、当前 `plan_revision`）。真实模型下每轮都会 `proposal_unreadable`；本片的测试用脚本化 provider，因此不依赖提示词内容，但这是上真模型前的必修项。
- **(d) `commit_graph_change` 在 hierarchical Mission 上没有闸门。** 可以绕过类型化计划直接改 DAG。加这个闸门要动 `commit_service.py` / `plan_commits.py`，都在本片禁改清单内。
