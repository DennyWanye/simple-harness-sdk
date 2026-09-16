# P2.3a 实施日志

- 计划包：FULL-TARGET-1.4 §4 ADR-13、§7.4、§9.4、§14、§18.2、§18.5、§23；附件 TG §7.1–7.4
- 基线：main HEAD `7b0a88f`
- 耗时：约 1 小时 15 分（核对在途成果 30 分钟、补齐缺口 20 分钟、变异自证与门槛 15 分钟、回归 10 分钟）
- 未提交、未推送。

## 1. 接手时的现状核对结论

前一位实施者在会话中断前已经把主体做完了。逐文件通读 `git diff` + 新文件后的判断：

| 在途产物 | 核对结论 |
| --- | --- |
| `orchestrator/plan_commits.py`（1213 行） | **合规，未改逻辑**。九道闸门顺序、legacy 门的位置（读 `missions` 行之后、构造 `HtnStore` 之前）、无 `allow_rebase`、事务内不调模型/solver/工具、全部经 `HtnStore` / `ObligationStore` 无裸 SQL —— 与 ADR-13 / §9.4 / TG §7.4 一致。仅做了 7 处 E501 换行与一次 `ruff format`。 |
| `tests/orchestrator/full_target/test_plan_commits.py`（1384 行 / 74 条） | **合规但缺一项**：交付项 5（`validate_graph_v2`）在测试里零覆盖。已补 12 条（见 §5）。 |
| `graph/task_graph.py`（+234） | 合规。`validate_graph` 一个字节未动，v2 门是全新函数。**但 per-node 检查是近逐字的第二份拷贝**：`_node_checks` 当时只有 v2 调用，v1 保留自己的循环——审阅第 11 项已修，现在是一个函数体、两个调用方（见 §11）。 |
| `orchestrator/commit_service.py`（+38 / −2） | 合规，逐行说明见 §3。 |
| `planning/planner.py`（+98） | 合规。旧 `parse_task_proposal` / `parse_task_graph_proposal` 原文不动；新增 `SYSTEM_BOUND_FIELDS` + 两个 strict parser。 |
| `runtime/output_blocks.py`（+28） | 合规。只新增 `REPAIR_HINTS` / `repair_hint`，`extract_block` / `BlockError` / `outside_text` 原文不动。 |
| `runtime/role_templates.py`（+56） | 合规。两个新 tag 常量 + `planner-hierarchical-v1`（新版本号，`register_template` 追加），`planner-v3` / `planner-v4` 原文未动。 |
| `tests/orchestrator/fixtures_provider.py` | **缺**。交付项 6 未做，已补（见 §5）。 |
| `test_planner_typed_proposal.py` | **缺**。交付项 7 的第二个文件未做，已补（见 §5）。 |

结论：本片是「补三个缺口 + 立门槛 + 写记录」，没有推翻任何在途设计。

## 2. 改动点逐文件

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `src/agent_orchestrator/orchestrator/plan_commits.py` | 新建 1220 行 | 第 9 个 Commit Service mixin。本次仅格式化（7 处 E501 换行 + `ruff format`），逻辑零改动。 |
| `src/agent_orchestrator/orchestrator/commit_service.py` | 改 +38 / −2 | 见 §3 |
| `src/agent_orchestrator/planning/planner.py` | 改 +96 / −2 | `SYSTEM_BOUND_FIELDS`、`_refuse_authority_claims`、`parse_plan_proposal(text, *, mission_id)`、`parse_method_proposal(text)` |
| `src/agent_orchestrator/runtime/role_templates.py` | 改 +56 | `METHOD_PROPOSAL_TAG`、`PLAN_REVISION_PROPOSAL_TAG`、`PLANNER_HIERARCHICAL(_VERSION)` + `register_template` |
| `src/agent_orchestrator/runtime/output_blocks.py` | 改 +27 / −1 | `REPAIR_HINTS`、`repair_hint(error, tag)`（有界修复，不另开请求） |
| `src/agent_orchestrator/graph/task_graph.py` | 改 +234 | `_node_checks`、`_or_smuggling`、`validate_graph_v2` |
| `src/agent_orchestrator/testing/fixtures.py` | **本片新增 +73 / −1** | `plan_revision_proposal_step(...)` / `method_proposal_step(...)`，两个 `__all__` 条目，`Mapping` import |
| `tests/orchestrator/fixtures_provider.py` | **本片新增 +2** | 两个新 step 的再导出 |
| `tests/orchestrator/full_target/test_plan_commits.py` | 新建 1572 行 / 86 条 | 本片 +12 条（v2 门）+ 1 条断言加严 + 格式化 |
| `tests/orchestrator/full_target/test_planner_typed_proposal.py` | **本片新建 533 行 / 60 条** | — |

`git diff --numstat`：7 个已跟踪文件 +526 / −6，外加 3 个新文件 3325 行（`plan_commits.py` 1220、`test_plan_commits.py` 1572、`test_planner_typed_proposal.py` 533）。

### fixtures 的两个新 step 为什么**不做校验**

`plan_revision_proposal_step` 只拼 JSON，不验证。一个会拒绝坏块的 fixture 没法用来测「解析器拒绝坏块」，所以形状归调用方，`extras=` / `drop=` 专门用来脚本化**必须被拒**的块（自填权限字段、缺字段、坏 schema 版本）。它永远不写 `mission_id`——归属由发出请求的一侧决定，块里写了就该被拒。`method_proposal_step` 同理**不丢弃** `registry_status`：丢掉就没人拒它了，注册服务必须看见这个声明才能记下拒绝（§7.3）。旧 script 协议未变：step 仍是「字符串 / `(tool, args)` / 可调用」三种。

## 3. `commit_service.py` diff 逐行说明

| 位置 | 行数 | 说明 |
| --- | --- | --- |
| import 区 | +1 | `from ..contracts.htn import TaskSemanticBindingV1`（仅用于类型标注） |
| import 区 | +8 | 从 `.plan_commits` 引入 `PlanCommitsMixin` 与四个语义常量 + 两个函数 |
| `MissionSpec` 字段 | +3 | `orchestration_semantics_version: str = LEGACY_SEMANTICS`——**服务端默认 legacy**（§18.5 兼容硬约束 1），默认值写死在代码里，不从环境读 |
| `MissionSpec.to_json` | +4 | 只有**非 legacy** 才写进 spec JSON。与上面 `domain` / `runtime_profile_id` 同一写法，目的是默认值不改 `spec_hash`：否则 Host 升级后重发同一个请求会撞 `MissionConflict` |
| `CommitService` 基类列表 | +2 / −2 | 末尾加 `PlanCommitsMixin`，注释补一句 P2.3a |
| `create_mission` | +5 | 写 Mission 之前 `normalise_semantics(...)`，未知版本转成 `CommitRejected`——**在任何写入之前**拒绝；`full-target-v1` 与 `hierarchical` 归一到一个值，防止计划文档与代码漂成两种模式 |
| `create_mission` 的 `final_report` | +6 | 同样只在非 legacy 时写 `orchestration_semantics_version` 键，legacy Mission 的存储 JSON 字节与 P2.3a 之前完全一致 |
| `commit_task_graph` 签名 | +1 | 可选 `semantic_bindings: Mapping[str, TaskSemanticBindingV1] \| None = None` |
| `commit_task_graph` 转调 | +1 | 原样传给 `_commit_task_graph` |
| `_commit_task_graph` 签名 | +1 | 同上 |
| `_commit_task_graph` 主体 | +7 | `if semantics_of(mission) == HIERARCHICAL_SEMANTICS:` → `self._require_semantic_bindings(...)`，缺绑定抛 `MISSING_SEMANTIC_BINDING`，整张图不落库。**legacy Mission 永不进这个分支**，旧路径一条语句都不多执行 |

即：legacy 侧真正多出的只有一次 `semantics_of(mission)` 字典查表（读 `final_report`，不查库），其余全部在新分支内。

## 4. read-set 校验顺序

`commit_plan_revision` 的闸门顺序不是装饰性的，先便宜后昂贵、先共有后专属：

1. **类型 / 身份**（`_authorize`）：`issued_by` 与出示者必须一致，scope 必须是出示者持有的。放在回执查询**之前**，否则伪造他人 `command_id` 就能读回别人的回执。
2. **legacy 门**：`semantics_of(mission)` != hierarchical → `SEMANTICS_NOT_HIERARCHICAL`。此时只读过 `missions` 一行，没构造 `HtnStore` / `ObligationStore`，31 张 migration-16 表零读零写，无事件。
3. **同 command_id 幂等**（`_replayed_receipt`）：`intent_hash` 相同 → 返回原回执；不同 → `COMMAND_PAYLOAD_CONFLICT`。`intent_hash` 覆盖 delta + task_bindings + `base_graph_version` + scope + issued_by + running_work_policy + superseded_occurrences + structure_budget；`source` 是元数据，故意不在里面。
4. **Mission 可写**：终态 → `MISSION_NOT_WRITABLE`。放在回执查询**之后**，所以 Mission 结束后重放仍能拿到原回执。
5. **manager epoch**（`_check_manager_epoch`）：read-set 里的 epoch 与出示者持有的 epoch 都必须等于当前 scope epoch，否则 `MANAGER_EPOCH_STALE`。
6. **整数闸门**（`_check_integer_gate`）：`base_graph_version` != `mission.final_report["graph_version"]` → `GRAPH_VERSION_STALE`。**这里没有 `allow_rebase`**（ADR-13 / C19）：legacy `_commit_graph_change` 可以在「改动不重叠」时重放，层次提案不行——过期的可能是它**读到的一个事实**，任何基于 task id 的重叠判断都看不见。
7. **语义 read-set 逐项**（`_check_read_set`），顺序与报错分两类：
   - `requirements_revision`（整体）
   - `goal_revisions` → `task_semantics.(contract_revision, contract_hash)`
   - `method_revisions` → 注册表定义的 `(method_version, content_hash)`；SUSPENDED/RETIRED/REJECTED 返回 `status:` 而不是 hash 不匹配
   - `observation_revisions` → 记录不可变，故「过期 = 被取代」：同 `proposition_key` 有更新记录即 `superseded_by:`；找不到 observation 时退回 `ValidityWitness`（其语义版本就是 scope epoch）
   - `acceptance_revisions` → 非 CURRENT 返回 `validity:`
   - `obligation_revisions` → `account.shape_changes` 为语义版本 + duty/lifecycle/resolution_ref 的内容哈希
   - `authority_revisions` → approval 记录的 `version` + 内容哈希
   - `support_sets` → `(member_revision, member_digest)`（C29：只看成员不看集合摘要，加一条反证据也察觉不到，AER I02）
   - `scope_epochs` → `validity_epoch` 必须等于当前 scope epoch（C29 的 epoch 屏障：作废证据靠抬 epoch，从不改动被读过的那条记录）
   - `absences` → 三个谓词（`no_adopted_method_instance` / `no_obligation` / `no_order_constraint_into`）「什么都没有」由假变真即过期（TG §11.2，A→B / B→A 合并成环那一类）
   - 两类结果分开报：**不可复核**（`READ_SET_UNRESOLVED`）先抛，**过期**（`READ_SET_STALE`）后抛，并且一次列出所有过期项。「我判断不了」和「它变了」要的是不同的返工。
8. **active plan revision**（`_check_plan_revision`）：`delta.base_plan_revision` 必须等于当前 active；提交方编译出的 network 必须正好是 `base + 1`（否则就是拿另一个修订的证书来蒙）。
9. **结构再验证**（`_check_structure`）：在**当前事务**里对「快照 + delta」整体跑 `validate_execution_projection` + `validate_refinement_acyclic`，不是只看增量——两个各加一条边的提案合起来会成环，只看增量两个都能过。同时把 network 钉死到 delta（mission 一致、delta 的 occurrence 都在 network 里、delta 引用的 occurrence 不悬空、`delta.assert_consistent_with(bindings)`）。
10. **预算**（`_check_budget`）：`max_live_tasks` / `max_expanded_nodes` → `BOUND_REACHED`；声明成本与 delta 实际不符 → `BUDGET_REQUIREMENT_MISMATCH`；`obligation_openings` 在一份**临时账本**上跑 `apply_obligation_openings`，父额度不够 → `BUDGET_INSUFFICIENT`。旧 token ledger 不查、不动。
11. **在途工作**（`_revoke_running_work`）：被替代 occurrence 的 `TaskSemanticBindingV1` 写新一版（`contract_revision + 1`、`dispatch_generation + 1`），并 `mark_dirty(reason="dispatch_generation_revoked")` 登记核对。抬 generation 就是收回执行资格——已发出的 dispatch 从此对不上号；它**不**取消 Attempt、**不**批准新版本。`retain_if_bindings_unchanged` / `explicit_per_subject_in_commit` 两种策略下，有被换掉却没被列出的 occurrence → `RUNNING_WORK_NOT_RECONCILED`。

写半场（`_write`）在同一事务内：duty openings（`open_from`，必须先于引用它的 membership）→ task_semantics → `plan_revision`（`PREPARED`）→ method_instances（新 ADOPTED / 旧 RETIRED）→ memberships → order / data 表 → `plan_read_sets` → `activate_plan_revision`（**PREPARED→ACTIVE 同一事务原子切换**，§9.4 不允许一半旧计划一半新计划同时可派发）→ 事件 → 待派发意图（只登记 `pending_dispatch`，派不派是 P2.3c 的事）→ commit receipt。

## 5. 本片补齐的三个缺口

### 5.1 `testing/fixtures.py` + `fixtures_provider.py`（交付项 6）

`tests/orchestrator/fixtures_provider.py` 是 `agent_orchestrator.testing.fixtures` 的测试侧别名，所以两个新 step 实现在 shipped 模块里、在别名里再导出。见 §2 末的设计说明。

### 5.2 `test_planner_typed_proposal.py`（交付项 7，60 条）

- 合法块：解析成 `PlanProposal`、四种 operation 全通、trigger_ref 合法引用、块外散文与 ```json 围栏容忍（8 条）
- 权限不可自填：14 个 `SYSTEM_BOUND_FIELDS` 各一条参数化 + read_set 条目内 + operation 内 + 一次报全部 + `bindings` 里同名参数是**值不是声明** + trigger_ref 深处 `produced_by:"tool"` 被拒 + 字段表覆盖面（20 条）
- 块不可读：缺块 / 空输出 / 两个块 / 坏 JSON / 非对象 / `repair_hint` 带上 tag（6 条）
- 契约自身底线：空 read_set、空 operations、缺字段、错 schema 版本、未知 op、未知 running_work_policy（6 条）
- `method_proposal`：合法解析、默认 DRAFT、`registry_status` 作为**声明保留**、缺块、坏 JSON、缺定义、四个 tag 互不相同（7 条）
- 旧协议不动：`<task_graph_proposal>` 解析如旧、旧 codec 不受新权限检查管辖、两个 parser 互不接受对方的块（4 条）
- 模板注册：`planner-hierarchical-v1` 注册且是**追加**、`planner-v3` / `planner-v4` 指令原文哈希不变、新模板点名两个块与被绑定字段、点名四种 operation、`tool_names == ()`（6 条）
- fixture 自身：两个 step 各产出恰好一个可解析的块、永不自写 `mission_id`（3 条）

### 5.3 `test_plan_commits.py` 的 v2 门一节（交付项 5，+12 条）

在途测试文件对 `validate_graph_v2` 零覆盖，补：预算内准入、`max_nodes` / `max_depth` / `max_fan_out` 三个维度各报 `bound_reached` 且消息里带维度名与**预算版本号**、`dependencies` 有边而 typed 无关系（`or_smuggled`，OR 偷编码）、typed 有关系而 `dependencies` 丢了（另一个方向，投影会先于生产者派发）、typed 提到图里没有的节点、环仍报 `cycle` 且点名环路、per-node 契约检查与 Mission 预算检查仍在，以及**两条对照**：同一张图 legacy `validate_graph` 准入 / v2 拒绝——如果哪天 v1 门开始读新预算，这两条会失败。

另把 `test_v2_still_refuses_a_cycle_and_names_it_as_one` 从 `reason != "or_smuggled"` 加严成 `reason == "cycle"` 且点名 `A -> B -> A`，把 read_set / operation 两条权限测试从「消息里有字段名」加严成「消息里有 `the system binds`」——`fields_of` 本来就会以「未知字段」拒掉它们，不加严的话测的是别人。

## 6. 事件类型

只**新增**一种：`PlanRevisionCommitted`（§18.5 兼容硬约束 3 允许新增、禁止改写既有事件的字节）。payload 走 `_emit` 的既有 canonical 通道，字段：`command_id`、`delta_id`、`proposal_id`、`base_plan_revision`、`plan_revision`、`base_graph_version`、`scope_id`、`intent_hash`、`read_set_hash`、`running_work_policy`、`adopted_method_instances`、`retired_method_instances`、`added_occurrences`、`opened_obligations`、`order_constraints`、`data_requirements`、`revoked_dispatch_generations`、`pending_dispatch`、`source`。

未修改任何既有事件类型。`test_a_legacy_task_graph_commit_is_byte_for_byte_what_it_was` 把同一个旧模式提交在两个独立库上各跑一遍，逐字节比较 `events.payload_json` 全表相等，并断言每条 payload 里既无 `orchestration_semantics_version` 也无 `plan_revision`。

## 7. 测试数与结果

| 项 | 结果 |
| --- | --- |
| `test_plan_commits.py` | **86 passed**（要求 ≥40；含 8 条变异自证，要求 ≥6） |
| `test_planner_typed_proposal.py` | **60 passed**（要求 ≥20） |
| `tests/orchestrator/full_target`（全目录） | **1639 passed / 1 skipped**（skip 是 `test_panda_backend.py` 需要真实 pandaPIparser） |
| 旧模式回归 `step02 step03 step05 step06 step07 p33 p34 p35 p36 test_critic_test_evidence_order.py` | **1548 passed / 17 skipped / 1 failed** —— 唯一失败是已知可忽略的 `p33::test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged`（`KnowledgeIndex.check` 的 AST 哈希基线，与本片文件无交集）。零新增失败。 |
| `ruff check`（10 个相关文件） | All checks passed（修掉在途成果里的 16 条 E501 / UP030 / UP032） |
| `ruff format --check` | `plan_commits.py`、两个测试文件、`planner.py`、`output_blocks.py`、`fixtures.py`、`fixtures_provider.py`、`task_graph.py` 全 clean；`commit_service.py` 与 `role_templates.py` 在 **HEAD 版本就已经不是 format-clean**（`git show HEAD:<file> \| ruff format --check -` 同样报 reformat），本片未引入新格式债，也没有去整体重排这两个长期文件 |
| `mypy src/agent_orchestrator` | **17 errors**，与基线同样 17 条（16 条第三方 `import-not-found` + `agentdojo_runner.py` 1 条 `misc`），零新增 |

### 变异自证

在途文件自带 8 条（跳过语义门、自动 rebase 过期基线、read-set 放过不可复核项、两步激活、幂等键忽略 payload、撤销不动 generation、只验 delta 不验整网、预算从不拒绝）。本片额外跑了三轮**外部**变异确认新测试非空转：

1. `SYSTEM_BOUND_FIELDS = frozenset()` → 4 failed
2. `_refuse_authority_claims` 置为 no-op → **17 failed**（14 条参数化 + 3 条）
3. `task_graph._or_smuggling` 恒返回空 → **4 failed**（三条跨表不变量 + 一条 legacy 对照）

## 8. 偏差

1. **`testing/fixtures.py` 被改动**。交付项 6 写的是 `tests/orchestrator/fixtures_provider.py`，但该文件在本仓库只是 shipped 模块的再导出别名（22 行）。为了让 `agent_orchestrator.testing.fixtures` 与测试侧共用同一份脚本，两个 step 实现在 shipped 模块里，别名里再导出。该文件不在禁改清单内，且 §18.2 就是把它列在 fixtures_provider 同一落点。
2. **`validate_graph_v2` 的测试放进 `test_plan_commits.py`**，没有新建第三个测试文件——交付项 7 只点名两个测试文件，v2 门是交付项 5 的「上述每一条」之一，故并入。
3. **在途成果里 16 条 lint 违规由本片修掉**（7 条在 `plan_commits.py`、5 条在 `test_plan_commits.py`、4 条在本片新文件），并对三个 P2.3a 自有文件跑了 `ruff format`。`commit_service.py` / `role_templates.py` 未整体格式化（基线本就不 clean，重排会淹没 diff）。
4. **`plan_commits.py` 逻辑零改动**。核对后认为它已满足交付项 1 的每一条；不重写是接手指令的明确要求，也是本片没有再动它的原因。

## 9. 契约变更请求

**无**。`contracts/` 只读，本片所需的每一样东西都已在 `contracts/htn.py` 里：`PlanProposal`、`ProposedPlanDelta`、`SemanticReadSet`（含 obligation / authority 通道、`SupportSetRead`、`ScopeEpochRead`、`AbsenceRead`）、`ObligationOpening`、`TaskSemanticBindingV1`、`GraphStructureBudget`、`RunningWorkPolicy`、`ReadItemKind`、`parse_plan_operation` 的四种 operation。`MethodProposal.from_json` 已经支持可选 `registry_status`，无需改动。

## 10. 交接给 P2.3b / P2.3c

- `PlanRevisionCommitted` 的 `pending_dispatch` 只是**登记**。谁能真的跑（readiness / inputs / budget / form）由 P2.3c 的 allocator 回答；P2.3b 负责 `event_handler.py` 读这个事件与 `artifacts/versioning.py` 接线。
- `read_item_for(mission_id, kind, subject_id)` 是给提案方用的：提案侧与校验侧必须对「一个 subject 的语义版本是什么」有同一个答案，写两遍就会开始不一致，所以两侧共用同一组 resolver。
- §18.5 四条兼容硬约束在本片的状态：**语义版本默认 legacy** 有单独测试（`test_the_server_side_default_is_legacy` / `test_the_default_does_not_change_the_mission_spec_hash`）；**旧事件字节不变** 有逐字节对比测试；**allocator 保留旧 READY 入口** 与 **不重定义旧 READY** 靠「本片不改 `scheduling/`」保证，没有本片新增的测试去守它们——这两条的正面测试归 P2.3c，接手时请补。

---

## 11. 审阅修复（独立审阅：需修后合并）

审阅结论：闸门顺序、零回归（审阅做了真实 HEAD 字节对比，一致）、事务边界均合规；11 个变异里捕获 6 个，**5 条存活全是测试缺口**。以下 12 项全部完成，逐条对应。

- 耗时（本轮）：约 1 小时 05 分
- 本轮后规模：`plan_commits.py` 1381 行、`test_plan_commits.py` 2183 行 / **116 条**、`test_planner_typed_proposal.py` 599 行 / **66 条**；已跟踪文件 +543 / −43

### 必改

**1 整数闸门补 `base < current`（测试缺口）** —— 原有两条都是 `base=7 > current=1`，`!=` 改成 `>` 的变异存活。新增 `_set_graph_version` 辅助（按 legacy 图变更的方式移动 `final_report["graph_version"]`）与 4 条测试：behind 被拒、behind 不自动 rebase、两个方向都在消息里点名两个版本号、**base 等于被移动后的版本可以通过**（否则闸门可能退化成「只接受 1」）。变异 `!= → >`：3 红。

**2 `activate_plan_revision` 必须同事务（测试缺口）** —— §9.4 的 PREPARED→ACTIVE 原子切换原先无测试守。新增 2 条：把 `HtnStore.activate_plan_revision` 换成抛异常，断言 `list_plan_revisions() == ()`、`active_plan_revision() is None`、memberships 空、无事件、`plan_revisions` 表计数 0（两步激活会留下一行 PREPARED，这是唯一能区分的状态）。

**3 `SYSTEM_BOUND_FIELDS` 字面 golden 集合（测试缺口）** —— 原参数化遍历集合自身，删一个字段就连带删掉它的测试，恒真。新增 `GOLDEN_BOUND_FIELDS` 字面 14 项 + `== SYSTEM_BOUND_FIELDS` + `len == 14`，并把原来只列 9 项的覆盖面断言改为对 golden 断言。同时 **`role_templates.py` 的层次 Planner 正文补齐到 14 个受绑字段**（原列 10 项，缺 `principal_id`/`scope_id`/`budget_grant_revision`/`opened_by`/`authored_by`——被解析器拒绝却没写进提示词是个陷阱：模型从没被告知，整份提案却因此作废），并加一条测试断言 14 个字段全部出现在正文里。

**4 planner-v3/v4 字面 hex 摘要（测试缺口）** —— 原测试两侧都从活对象算 `content_hash_of`，恒真，任何改词都能通过。改为 `FROZEN_PLANNER_PROMPTS` 字面字典（`planner-v3 = 353b0dd0…87e9`、`planner-v4 = 13537f0a…9aad`，sha256 over instructions），参数化断言；另加一条「守卫的守卫」：摘要必须是本模块不推导的 64 位 hex 字面量。

**5 `_authorize`：空 `issued_by` 一律拒绝（真实缺陷）** —— 原 `if command.issued_by and ...` 让空值跳过比对，任意同 scope principal 都能提交非自己撰写的提案，回执会记上一个从未发出该命令的 manager。改为空/纯空白直接 `PRINCIPAL_MISMATCH`。3 条测试（空串、任意 principal、纯空白）；变异回原实现：3 红。

**6 `_or_smuggling` 的表述与实质**
- 审阅正确：集合相等不是 OR 门。`C` 依赖 `A`、`B` 且 typed 同写 `C:(A,B)` 就是准入，因为图层只有 AND 一种读法。
- 函数改名 `_or_smuggling` → **`_representation_drift`**，拒绝理由 `or_smuggled` → **`representation_drift`**，docstring 与 `validate_graph_v2` 的 docstring 改写成「表示漂移检测」，并明确写下它**不是** OR 门、两个相等的集合对语义一无所知。
- 真正的 OR 约束补在 delta 层：`_check_alternatives_are_method_instances`。调查发现 `TaskNetworkSnapshot.__post_init__` **已经**拒绝「一个 occurrence 两个 adopted 实例」（消息 `alternatives are OR, not AND`），所以提案方根本编译不出那种证书；但**写半场有真实漏洞**：`_write` 会把 `delta.method_instances` 全部以 ADOPTED 写库，而 `_check_structure` 从不校验 delta 的实例是否在 network 里——于是一个证书从未包含的实例能绕过快照不变式落成 adopted 行，两个这样的行叠在一个 occurrence 上就是在数据库里拼出来的未决 OR。新检查关掉两半：delta 采纳的实例必须是 network 持有且 adopted 的；且不得与库里已 adopted 于同一 occurrence 的实例并存（除非 delta 同时 retire 它）。
- 5 条测试：快照自身拒绝（在不变式所在层测）、delta 夹带证书之外的实例被拒、库里已有 adopted 时第二个被拒、**未 adopted 的备选允许存在**、以及正例 `test_several_dependencies_at_the_graph_layer_are_read_as_and`（明说图层读 AND，防止后人把漂移检测再读成 OR 门）。变异置空：2 红。

### 应改

**7 `delta.assert_consistent_with` 补测试** —— 变异跳过它原先存活。新增 1 条：把 delta 的 occurrence 的 `form` 从 PRIMITIVE 改成 COMPOUND（occurrence 携带的是绑定的**副本**，绑定才是权威），断言 `STRUCTURE_INVALID` 且零落库。

**8 `require_commit_ready` 提前为闸门（真实缺陷）** —— 原先它在写半场、**openings 持久化之后**才调用，此时刚开的 duty 与本来就存在的 duty 无法区分，于是 `opened & registered` 非空 → 抛裸 `ContractError`「would re-open duties that already exist」。结论：**任何带 `obligation_openings` 的计划修订都提交不了**，而且没有测试发现，因为没有一条测试试过成功提交带 opening 的 delta。修复两半：新增闸门 `_check_commit_ready`，对**开启之前**的 duty 集合做校验，`ContractError` → 具名 `PlanCommitRejected("DELTA_NOT_COMMIT_READY")`；`_write` 里把 `registered` 的读取挪到 openings 之前。3 条测试，其中 `test_a_fundable_opening_is_committed_and_lands_in_the_ledger` 就是原先不可能通过的正例。

**9 `_check_structure` 的保留性校验（真实缺陷）** —— 新增 `_check_preserves_plan`：供入 network 若丢失上一修订持有的 occurrence，而 delta 既没有把它列为 superseded、也没有 retire 开启它的方法实例 → `PLAN_NOT_PRESERVED`。这是结构校验器看不见的一类错：更小的 network 投影完全合法，而被丢掉的 occurrence 背后那条 duty 就没人干了，且没有任何记录说是谁决定的。5 条测试（静默丢弃被拒、消息点名被丢的 occurrence、列为 superseded 后过得了这道门、第一次提交无可保留、**没被 supersede 的 occurrence 保持其 dispatch generation 与 contract revision**）。变异置空：2 红。

**10 `MISSING_SEMANTIC_BINDING` 落成持久拒绝事实** —— `commit_task_graph` 的 `except` 扩到 `(GraphRejected, PlanCommitRejected)`，两者都写 `TaskGraphRejected` 事件；但层次拒绝**保留自己的类型与 reason 名**再抛出，不包成 `CommitRejected` 字符串——`MISSING_SEMANTIC_BINDING` 是提案方要据以行动的机器名。3 条测试（事件落库且 reason 正确、类型与 reason 未被洗掉、legacy 分支仍然答 `CommitRejected`）。

**11 v1/v2 约 50 行近逐字重复** —— `_node_checks` 上移到 `validate_graph` 之前，**v1 真正调用它**（v1 原有的 per-node 循环整段删除，−36 行）。检查项、顺序与消息全是 v1 的原文，靠现有 v1 测试与 legacy 事件字节对比守住。新增 2 条跨门对照：同一个 per-node 缺陷（空 rationale、越权工具）两个门给出**完全相同的 `(reason, detail)`**。journal §1 的「两侧共用」表述已更正为「一个函数体，两个调用方」。
  - 一处未合并的差异（原样保留，未扩大范围）：v1 在循环之后还检查 `final_report["synthesis"]` 模板预算，v2 没有。这是 v2 从一开始就有的差异，不在本轮修复范围；已在 §12 记为给 P2.3c 的注意点。

**12 固化 HEAD 字节对比** —— 新增字面常量 `LEGACY_GRAPH_EVENT_DIGEST = "e6f14c85…4a34b"`：legacy Mission + `commit_task_graph` 的全部 `events.payload_json` 规范化后的 sha256。原来的「两库两遍逐字节比较」只证明这条路径**确定**；这条证明它还是 P2.3a 之前那条路径（§18.5 硬约束 3）。另加一条「守卫的守卫」：常量必须是本模块不推导的 64 位 hex。

### 本轮变异自证

| 变异 | 结果 |
| --- | --- |
| `_authorize` 回到「空值跳过比对」 | 3 红 |
| `_check_preserves_plan` 置空 | 2 红 |
| `_check_alternatives_are_method_instances` 置空 | 2 红 |
| `_check_commit_ready` 置空 | 2 红 |
| 整数闸门 `!=` → `>` | 3 红 |

（`SYSTEM_BOUND_FIELDS` 清空 → 4 红、`_refuse_authority_claims` 置空 → 17 红、`_representation_drift` 恒空 → 4 红，与首轮相同。）

### 本轮门槛结果

| 项 | 结果 |
| --- | --- |
| `test_plan_commits.py` | **116 passed**（原 86，+30） |
| `test_planner_typed_proposal.py` | **66 passed**（原 60，+6） |
| `tests/orchestrator/full_target` | **1675 passed / 1 skipped**，`--ignore` 了 P2.3b 正在写的 `test_hierarchical_event_flow.py`（见下） |
| 旧模式套件（step02/03/05/06/07 + p33/p34/p35/p36 + test_critic_test_evidence_order.py） | **1548 passed / 17 skipped / 1 failed**，仍只是已知可忽略的 p33 AST 哈希基线。零新增失败 |
| `ruff check`（10 个相关文件） | All checks passed |
| `ruff format --check` | `plan_commits.py` 与两个测试文件、`planner.py`、`task_graph.py`、`output_blocks.py`、`fixtures.py`、`fixtures_provider.py` 全 CLEAN；`commit_service.py` / `role_templates.py` 在 HEAD 版本就已非 clean，未整体重排 |
| `mypy src/agent_orchestrator` | **17 errors = 基线 17**，零新增 |

### 共享工作树状态

本轮期间 P2.3b 正在同一工作树改 `orchestrator/event_handler.py`、`artifacts/versioning.py`、`artifacts/workspace.py`，并新建 `orchestrator/hierarchical_dispatch.py`、`tests/orchestrator/full_target/test_hierarchical_event_flow.py`。**我一行都没碰这些文件**（已核对我的 7 个热文件 diff 行数与本轮开始时一致，无外来改动）。他们的新测试文件当前有一处自身的收集错误（`'slow' not found in markers configuration option`），属于 P2.3b 在途状态，因此我的 full_target 运行对它用了 `--ignore`；那是他们的文件，不该由我修。

## 12. 给 P2.3b / P2.3c 的注意

- **层次模式下 `commit_plan_revision` 不推进 `graph_version`。** 串行化点是 **plan revision**（`_check_plan_revision` 的 active 修订号），整数闸门只是「每个 Mission 都认的那道便宜的门」。不要据整数闸门做层次模式的并发控制：两个 Manager 同时提案时，第二个是被 `PLAN_REVISION_STALE` / `READ_SET_STALE` 挡住的，`base_graph_version` 从头到尾都还是同一个值。
- **`repair_hint` 目前零调用方。** `runtime/output_blocks.py` 里的有界修复提示已经写好并有测试，但没有任何生产代码调用它——真正的「有界修复」要等 `event_handler.py` 把 `BlockError` 接到同一个 Attempt 的重试上（P2.3b）。在那之前它只是一个可用的函数，不是一条已经生效的路径。
- **`validate_graph_v2` 不检查 `final_report["synthesis"]` 的模板预算**，v1 检查。层次模式如果也要 synthesis Task，这道检查需要在 P2.3c 一并补上（见 §11 第 11 项）。
