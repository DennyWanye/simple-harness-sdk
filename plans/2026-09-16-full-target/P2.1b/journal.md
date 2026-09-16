# P2.1b 实施记录（TaskNetwork 不可变快照与执行投影验证，纯函数）

日期：2026-09-16｜基线：SDK main `873fd4a`（工作区含 P1.1 / P2.2 / P1.1b 未提交产物）｜执行：单个 Opus 子代理｜实际耗时：约 1 小时（含规范阅读约 25 分钟）

规范来源：
主计划 `simple_harness/plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`
§6.5（四类关系的唯一含义）、§6.6（资源读写冲突）、§18.5、§24.1 裁决 2/10/11、§24.2（文件名定案）；
附件 `taskGraph/simpleharness-taskgraph-design.zh-CN.md` §3（身份与 Task 组成）、§4（关系模型）、§5（哪张图必须无环）、§6（复合边界编译）、§14.2（拓扑与增量索引）、§14.3（损坏处理）；
附件 `taskGraph/simpleharness-taskgraph-implementation-design.zh-CN.md` §3.1（关系表：哪些进入执行环检查）、§3.2（AND–OR 需要方法节点）、§3.3、§5.1–5.3（entry/exit 门、编译步骤 8、算法选择）。

## 1. 实现清单

### 生产代码（2 个新文件，未改动任何既有文件）

| 文件 | 行数 | 主要类型与函数 |
|---|---:|---|
| `src/agent_orchestrator/graph/task_network.py` | 909 | `ProjectionStructureBudget` + `DEFAULT_PROJECTION_BUDGET`；`EndpointKind`(6)、`NetworkEndpoint`、`TypedEdge`；`ResourceClaim`、`SetPortOrder`；`ProjectionNodeKind`(3)、`ProjectionEdgeKind`(5)、`ProjectionNode`、`ProjectionEdge`、`ExecutionProjection`；`flip_to_dependency_map` / `flip_from_dependency_map`；`MethodAlternative`、`RefinementView`、`SupportView`、`SupervisionView`；`TaskNetworkSnapshot`（构造时引用完整性校验 + 四个视图查询）；`GATING_REQUIREDNESS` |
| `src/agent_orchestrator/graph/projection_validation.py` | 758 | `ProblemKind`(11)、`ProjectionProblem`、`ProjectionReport`、`GraphIntegrityError`（含 `diagnose()`）；`kahn_order`（heap-based）、`find_cycle`（反向走）、`require_topological_order`；`validate_execution_projection`、`validate_refinement_acyclic`；9 个分类检查子函数 |

`graph/` 下既有四个文件（`task_graph.py`、`changes.py`、`dependency_checker.py`、`deduplicator.py`）与 `graph/__init__.py` 零改动；`contracts/` 只读；`verification/`、`knowledge/`（另两个并行子代理的写入范围）零触碰。

### 红测试（1 个新文件，87 个测试函数 / 98 条参数化用例）

`tests/orchestrator/full_target/test_projection_integrity.py`（1547 行），分十节：

| 节 | 条数 | 覆盖 |
|---|---:|---|
| 1 编译：门而不是伪循环 | 17 | compound → entry/exit 门；primitive 单节点且 entry==exit；门 `billable=False`；compound 没有"整体节点"；孩子只连父门；父 entry 开孩子 / 必需孩子 exit 关父；组合 Review 等兄弟不等父；**朴素并集成环而编译后不成环**（对照既有 `check_dependencies`）；未展开 compound 的 span 边；可选孩子被开启但不把门；ORDER `P before Q` → `P.exit → Q.entry`（复合/原子两例）；DATA 方向 producer→consumer；真 2 环与 3 环检出且路径是真实走法；节点 id 对含冒号的 occurrence id 仍单射 |
| 2 视图 | 7 | 方法内 AND、方法间 OR；未采用方法不进投影；`parent_of`；`relations(kind)` 分类型；support 按集合分组；SUPPORT/supervision/funding/supersedes 不进执行投影；supervision 双向映射 |
| 3 方向与显式翻转 | 3 | `to_dependency_map` 确实翻转；`flip_*` 往返保边集；旧 `check_dependencies` 能直接吃翻转结果且顺序一致 |
| 4 置换/乱序不变 + graphlib 交叉 | 16 | 全量重命名后形状同构、环判断不变、环路径长度不变；输入乱序（5 个种子）后节点/边/报告逐字节相同；graphlib 对无环/有环的判定与本实现一致；本实现给出的拓扑序对 graphlib 的依赖图同样合法（3 个种子） |
| 5 构造期引用完整性 | 12 | ORDER/DATA 悬空端点、child binding 悬空、occurrence 无语义绑定、adopted 不在 active membership、adopted 不存在、coverage 悬空、compound 任务两个 occurrence、occurrence 与绑定的 obligation 不一致、typed edge 悬空、root 悬空、资源声明悬空 |
| 6 每类问题各一反例 | 19 | 悬空投影端点、缺失细化闭合边、ORDER 指向未投影 occurrence、一个 occurrence 占两个采用槽、单值端口双绑定、集合端口无序 / 顺序不全 / 顺序完整放行、必需输入端口未绑定 / 可选端口不报、输出端口未声明、必需义务无 occurrence、必需 occurrence 不属任何方法、仅属替代方法不算孤立、根覆盖缺口指名缺哪条 / 全覆盖不报、写写冲突、读写冲突、读读不冲突、已排序不冲突、三类问题分别报告 |
| 7 规模上限 | 6 | max_nodes / max_edges / max_depth / max_fan_out 各一条 `BOUND_REACHED`；**超限时拓扑序仍覆盖全部节点（不截断）**；报告带 `budget_version`；预算本身拒绝 0 值 |
| 8 细化无环 | 4 | 正常无环；occurrence 成为自己祖先；两 occurrence 互为祖先（路径 ≥3）；同一 method 定义用在两个 occurrence 上不算递归环 |
| 9 GraphIntegrityError | 4 | DAG 上返回顺序；环上抛错并带 remaining + 具体环（`cycle ⊆ remaining`）；`diagnose()` **不含无环前缀节点、不输出拓扑顺序**、含剩余数量；`diagnose()` 指名环成员 |
| 10 纯函数与隔离 | 3 | 两个新模块源码零 `storage/scheduling/artifacts/sqlite` import；投影是快照的纯函数（两次调用相等）；`ExecutionProjection` 冻结 |

红→绿过程：先只写测试文件，`pytest` 报 `ModuleNotFoundError`（红）；实现后 96 绿 2 红——两条都是 `find_cycle` 的真实缺陷（见 §3.4），修好转全绿。

**变异测试（确认测试真的咬人）**：
- 把 `exit_[child] → exit_[parent]` 改成 `exit_[child] → entry_[parent]`（即回到"子等父/父等子"的错误接线）→ 19 条失败。
- 注释掉 `_check_ports` 与 `_check_resource_conflicts` → 8 条失败。
两次变异后均已还原，还原后 98 全绿。

## 2. 与规范的关键对应

- **§24.1 裁决 2 / TG §6 / 实现稿 §5.1（复合边界编译）**：`execution_projection()` 为每个 compound occurrence 生成 `entry`/`exit` 两个逻辑门；父 entry → 每个孩子 entry（`REFINEMENT_OPEN`），每个**把门的**孩子 exit → 父 exit（`REFINEMENT_CLOSE`）。组合 Review 是一个普通孩子，它靠方法内的 ORDER 等兄弟，父 exit 再等它——所以"父等子"与"子被父开启"两条箭头方向相反却不成环。测试 `test_parent_child_pseudo_cycle_is_a_cycle_only_in_the_naive_union` 直接把朴素并集喂给既有 `check_dependencies`（抛 `cycle`），再断言编译后的投影无环。
- **门不是收费 Agent**：`ProjectionNode.billable` 对两种门恒为 `False`，门不携带 `operator_ref`，模块里没有任何构造 Attempt 的代码路径。`test_gates_are_not_billable_work_and_primitives_are` 钉住。
- **外部 ORDER**：`P before Q` 一律编译为 `exit_of(P) → entry_of(Q)`；原子任务的 entry 与 exit 是同一个节点，所以对 primitive 退化成直连。`release_condition` 原样带进边的 `origin`，不在投影里被解释成别的东西。
- **§24.1 裁决 10 / TG §5（哪张图查环）**：只有执行投影做环检查。`validate_refinement_acyclic()` 独立检查"已实例化细化关系"，并在 docstring 里明确写出方法定义图可递归、证据图查无锚 SCC、资源 wait-for 另做死锁分析这三条不在本模块。测试 `test_the_same_method_definition_at_two_occurrences_is_not_recursion_in_this_check` 用同一个 `method_ref` 连做两级展开，断言无问题。
- **§6.5 / 实现稿 §3.1（四类关系唯一含义）**：`RelationKind` 的九个值里，只有 `ORDER`、`DATA` 与编译出来的门边进入执行投影；`SUPPORT`/`ASSUMPTION`/`supervision`/`funding`/`supersedes` 只出现在 `support_view()` / `supervision_view()` / `relations()`。`test_support_and_supervision_edges_never_enter_the_execution_projection` 断言加上这四种边后投影的 `edges` 逐字节不变。
- **实现稿 §3.1 末句（方向）**：所有公开边是 `producer/predecessor → consumer/successor`。与旧 `key → dependencies[]` 的转换只走 `flip_to_dependency_map` / `flip_from_dependency_map` 两个具名函数，`ExecutionProjection.to_dependency_map()` 是它们的唯一调用者，没有任何地方靠"换个读法"来改方向。
- **TG §14.2（算法）**：`kahn_order` 是 heap-based Kahn，`O(E + V log V)`，tie-break 用节点 `ordinal`（由排序后的不可变 node id 赋值）。docstring 明确写"这是一个确定性的线性化选择，不代表业务顺序"。`graphlib.TopologicalSorter` 只在测试里做交叉核对，生产路径不用它（它 `prepare` 后不能再加节点，且会凭空补进未声明的前置节点）。
- **TG §14.2（分别报告）**：环、悬空、缺边、重复 slot、孤立必需义务、端口、根覆盖、资源冲突、规模各有独立 `ProblemKind`，`ProjectionReport.problems` 是问题列表而不是布尔；`ok` 只是便捷属性。
- **TG §14.3（损坏处理）**：`require_topological_order()` 在拓扑不完整时抛 `GraphIntegrityError`，携带 `remaining` 与一条具体环；`diagnose()` 只描述剩余节点与环，**不输出"凑齐的拓扑顺序"**，并在文案里点明"健康前缀是偏序、不是计划"。测试断言无环前缀节点 id 不出现在 `diagnose()` 文本里。
- **TG §4.3 / 裁决 3（端口）**：必需输入端口无绑定 → `UNBOUND_PORT`；单值端口多绑定 → `SINGLE_PORT_OVERBOUND`；集合端口有绑定但没有完整声明顺序 → `SET_PORT_UNORDERED`（"不能谁最后写谁赢"）。`DataRequirement` 引用未声明的输入/输出端口同样报 `UNBOUND_PORT`。
- **§6.6（资源冲突）**：同对象写写 / 读写，且两个 occurrence 在投影里互不可达 → `RESOURCE_CONFLICT`，一对只报一次（对称冲突不是两条有向依赖）。读读不报。已有 ORDER 排序则不报。模块不会自作主张补一条边——选锁还是选顺序是业务决定。
- **裁决 10 的"规模超限"**：`ProjectionStructureBudget` 是冻结且带 `budget_version` 的四项上限；超限返回 `BOUND_REACHED` 原因（文案里带具体维度名与实际值），投影本身一个节点都不丢，拓扑序仍覆盖全部节点。

## 3. 与规范/任务书的偏差及理由

### 3.1 预算类型命名：`ProjectionStructureBudget`，不是 `StructureBudget`

任务书写"新模式上限由版本化 `StructureBudget` 提供，见 `contracts/htn.py`"。实际 `contracts/htn.py:360` 的 `StructureBudget` 是 **P1.1 为条件 AST 解析写的可变节点计数器**（`__slots__ = ("remaining",)`，`spend()` 递减并在耗尽时抛 `ContractError`），既不版本化、也不表达节点/边/深度/扇出这类图规模维度，语义与本片需要的完全不同。

处理：本片在 `graph/task_network.py` 定义 `ProjectionStructureBudget`（冻结、带 `budget_version`、四项上限），**没有**在同一代码库里再造一个叫 `StructureBudget` 的类（两个同名不同义的类是给后来人埋雷）。签名相应变为 `validate_execution_projection(projection, budget: ProjectionStructureBudget)`。类的 docstring 与本记录都写明了它与 `contracts.htn.StructureBudget` 的区别。

→ 见 §4 契约变更请求 CR-1。

### 3.2 `contracts/` 缺三类字段，本片用旁置类型顶上

`contracts/` 只读，缺的字段按任务书写成契约变更请求（§4），生产代码里用 `graph/task_network.py` 内的旁置类型表达，不改契约：

1. **集合端口的顺序**：`PortSpec` 有 `cardinality=SET` 但没有顺序声明字段 → 新增 `SetPortOrder(consumer_occurrence, input_port, ordered_requirement_ids)` 挂在快照上（CR-2）。
2. **资源读写集合**：§6.6 要求 PrimitiveOperator 声明资源读写集合，`TaskSemanticBindingV1` 没有该字段 → 新增 `ResourceClaim(occurrence_id, resource_id, write)` 挂在快照上（CR-3）。
3. **SUPPORT/ASSUMPTION/supervision/funding/supersedes 五种关系**：契约里只有 refinement（方法实例）、ORDER、DATA 三种的专用类型 → 新增通用 `TypedEdge(relation, source: NetworkEndpoint, target: NetworkEndpoint, label)`，端点带判别式 `EndpointKind`，不做成 `kind:str + config:Any`（TG §14.1 明确禁止退化成这个形状）（CR-4）。

### 3.3 三条"比规范更收紧"的构造期规则（均有测试钉住）

1. **compound 任务只能有一个 occurrence**。`MethodInstanceDraft.goal_id` 是 `TaskRef`，而细化的父是一个 occurrence；若一个 compound 任务有两个 occurrence，`instance → 父 occurrence` 的映射就有歧义。primitive 任务不受此限（复用/共享成果要靠多 occurrence 表达）。
2. **occurrence 的 `obligation_id`、`form` 必须与它引用的 `TaskSemanticBindingV1` 一致**。两处都带这两个字段，不一致就是两份事实。
3. **`binding.adopted_method_instance_id` 必须落在 `adopted_instance_ids`（active membership）里**。否则"采用了哪个方法"会有两个说法。

### 3.4 `find_cycle` 走反向而不是正向（一个真实缺陷的记录）

第一版沿用 `dependency_checker._cycle_path` 的正向走法，结果两条测试红：Kahn 剩下的节点不只是环成员，还包括环下游被堵住的节点；从 `min(remaining)` 正向走可能走进下游的死胡同，返回一条根本不闭合的"路径"。改成沿**前驱**反向走：Kahn 留下的每个节点必定还有一个未安置的前驱，反向走永不死路，撞到重复即截出真正的环再反转成正向。既有 `dependency_checker.py` 里的同一弱点不在本片改动范围（它每次只处理"全部在环上"的小图，实际未暴露），记在这里供 P2.3b 接手时判断。

### 3.5 不做的事

- **不重复 `ProposedPlanDelta.__post_init__` 已做的检查**：契约层已拒绝"同一 (consumer, input_port) 被绑两次"和"端点不在 occurrences ∪ referenced_occurrences"。本片在**快照**层面重做这些检查，是因为快照可以由多个 delta 合并而成，不能假定每条边都来自一个已通过契约校验的 delta。
- **不实现 eligibility / frontier**（P2.1c）、**不实现编译器本身**（P2.1，本片只消费编译结果）、**不实现 `BoundInput` 解析与 InputManifest**（P2.2b/P2.3b）、**不落库**（P1.2）。
- **不碰 `graph/__init__.py`**：它目前只有版权头，加导出会与 P2.1/P3.2 的后续文件抢同一处改动。使用方按 `from agent_orchestrator.graph.task_network import ...` 直接导入。

## 4. 契约变更请求（`contracts/` 只读，留给 P1.2 或契约维护者）

| # | 位置 | 请求 | 理由 | 本片的临时处理 |
|---|---|---|---|---|
| CR-1 | `contracts/htn.py` | 新增一个**版本化、冻结**的图规模预算类型（建议名 `GraphStructureBudget`，字段 `budget_version / max_nodes / max_edges / max_depth / max_fan_out`），并**不要**复用现有 `StructureBudget` 这个名字 | 现有 `StructureBudget` 是条件 AST 的可变节点计数器，语义与图规模上限无关；同名不同义会被误用 | `graph/task_network.py` 的 `ProjectionStructureBudget`，契约补齐后整体迁入并在此处留别名 |
| CR-2 | `contracts/htn.py` `PortSpec` | 集合端口需要顺序声明（`ordering_policy` 或在 `DataRequirement` 上加 `set_position`） | TG §4.3"集合端口必须定义类型和顺序，不能谁最后写谁赢"，当前只有 `cardinality` | `SetPortOrder`（快照旁置） |
| CR-3 | `contracts/htn.py` `TaskSemanticBindingV1` | primitive 需要声明资源读写集合（建议 `resource_reads` / `resource_writes`，值取注册 Operator 上界，不由模型自由填） | §6.6"PrimitiveOperator 还需声明资源读写集合" | `ResourceClaim`（快照旁置） |
| CR-4 | `contracts/htn.py` | SUPPORT / ASSUMPTION / supervision / funding / supersedes 五种关系缺专用契约类型 | §6.5 要求四类关系各有唯一含义，但契约只覆盖 refinement/ORDER/DATA | `TypedEdge` + `NetworkEndpoint`（判别式端点，不是 `kind:str + config:Any`） |
| CR-5 | `contracts/htn.py` `OccurrenceSpec` / `TaskSemanticBindingV1` | 两处都带 `obligation_id` 与 `form`，建议明确哪一处是权威 | 本片按"必须一致，否则是损坏"处理；若将来允许不同，需要写明规则 | 构造期一致性校验 |

## 5. 验证结果

```
# P2.1b 红测试
uv run --frozen --group dev --extra local-capacity pytest \
  tests/orchestrator/full_target/test_projection_integrity.py -q
→ 98 passed（87 个测试函数，其中 3 个参数化）

# full_target 目录整体（含 P1.1 / P2.2 / P1.1b 三个并行片的测试）
uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target -q
→ 513 passed, 1 skipped（skip 是 test_panda_backend 未配置真实 pandaPIparser）

# lint / format / 类型
uv run --frozen --group dev ruff check src/agent_orchestrator/graph \
  tests/orchestrator/full_target/test_projection_integrity.py
→ All checks passed!
uv run --frozen --group dev ruff format --check <同上三个文件>
→ already formatted
uv run --frozen --group dev mypy src/agent_orchestrator
→ task_network.py / projection_validation.py 零错误
  （余下 28 个错误全部来自既有 evaluation/ 的可选依赖缺失与其他片的文件，与本片无关）

# 隔离门槛（零 DB import）
grep -nE "^(from|import) .*(storage|orchestrator\.|scheduling|artifacts)" <两个新文件>
→ 零命中（并由 test_new_graph_modules_import_no_persistence_layer 在测试里持续守住）

# 写入范围
git status --short
→ 新增 graph/task_network.py、graph/projection_validation.py、
  tests/orchestrator/full_target/test_projection_integrity.py、plans/.../P2.1b/journal.md
  graph/ 下既有四个文件与 graph/__init__.py 零改动；contracts/ 零改动；
  verification/、knowledge/ 零改动（另两个并行子代理的范围）
未提交、未跑全量回归（按任务书要求）
```

## 6. 交接提示

- **方向**：公开边一律 `producer/predecessor → consumer/successor`。要给旧 `dependency_checker` 用，调 `projection.to_dependency_map()`；要把旧字典读回来，调 `flip_from_dependency_map()`。不要在别处自己写一个 `for k, deps in d.items()` 的隐式翻转。
- **门的身份**：节点 id 形如 `f"{len(occ)}:{occ}:entry|exit|node"`，长度前缀是为了让 occurrence id 里本来就允许出现冒号时仍然单射；要给人看请用 `ProjectionNode.occurrence_id` 而不是解析 id。
- **谁做判断**：`TaskNetworkSnapshot.__post_init__` 只做引用完整性（悬空端点、身份冲突、membership 一致），任何关于"这个计划好不好"的判断都在 `projection_validation` 里，且必须是**可分别查询的一类问题**。新增检查时请加新的 `ProblemKind`，不要往已有 detail 里塞第二种含义。
- **执行路径 vs 诊断路径**：`require_topological_order()` 是执行/物化路径，坏图抛 `GraphIntegrityError` 并停止该 scope 派发；`validate_execution_projection()` 是诊断路径，坏图返回报告（`topological_order=None`）继续展示。两者不要互换。
- **P2.1（编译器）接手时**：编译步骤 8 的检查项就是 `validate_execution_projection` 的返回值；把 `ProposedPlanDelta` 转成 `TaskNetworkSnapshot` 后调用即可，`delta.referenced_occurrences` 对应快照里"存在但本次不新建"的 occurrence。
- **P2.1c（eligibility）接手时**：三层 frontier 应当消费 `ExecutionProjection` 的 `predecessors()`，而不是重新遍历 `order_constraints`；门节点永远不进 `EligiblePrimitiveTask`（`billable=False` 即判据）。

---

# 审阅修复（2026-09-16，独立审阅"需修后合并"→ 已全部处理）

审阅结论：5/5 变异捕获、门编译主体正确，3 条阻断 + 5 条应改 + 2 条测试改法。期间契约第三轮落地，协调者指示"直接切换，不再需要 CONTRACT_PENDING 兜底"。下表逐条对应。

## 1. 阻断项

### B1 已展开 compound 若无任何 gating 孩子，exit 门无前驱（阻断，已修）

**问题**：兜底条件写的是"无孩子"（`if not children`）而不是"无 gating 孩子"。三个孩子全 `OPTIONAL_AUTHORIZED` 时，`exit` 门没有任何前驱，可以排在 `entry` 之前；穿过该 compound 的外部 ORDER（`o-a → o-c → o-d`）因此不再复合，而验证器报 0 问题。

**修法**：
- `task_network.execution_projection()` 改判 `gating = [c for c in children if c.requiredness in GATING_REQUIREDNESS]`，`if not gating` 时补 `entry → exit` 的 `COMPOUND_SPAN` 边，`origin` 区分 `unexpanded` 与 `no_gating_children` 两种成因。
- 新增独立 `ProblemKind.NO_GATING_CHILDREN`（在 `_check_refinement_wiring` 里判定），文案点明"该方法产出的任何东西都不必被接受，exit 就开了；编译器要么标一个必需槽，要么显式声明这个边界在 entry 处即闭合"。span 保证图诚实，报告保证计划不会不声不响地通过。
- 三条新测试：全可选时 span 存在、全可选时 ORDER 链仍连通（用可达性断言 `entry(o-c) ⇝ exit(o-c)` 与 `exit(o-a) ⇝ entry(o-d)`，不靠 tie-break 序号）、混合（一必需两可选）时既无 span 也无 `NO_GATING_CHILDREN`。

变异验证：把 `if not gating` 改回 `if not children` → 2 条失败。

### B2 改接契约第三轮，删除全部旁置重复（阻断，已修）

| 旁置物 | 去向 |
|---|---|
| `ProjectionStructureBudget` / `ResolvedBounds` / `resolve_bounds` / `ProblemKind.CONTRACT_PENDING` | 全删。`validate_execution_projection(projection, budget: GraphStructureBudget)` 直接吃契约 9 维预算（`max_nodes`/`max_edges`/`max_fan_out`/`max_depth` 都在）。`DEFAULT_PROJECTION_BUDGET` 现在是一个 `GraphStructureBudget` 实例 |
| `SetPortOrder` + `snapshot.set_port_orders` | 全删。改用 `PortSpec.ordering` / `order_key` 与契约的 `undeclared_set_ports()`；新增 `_check_set_port_ordering`，对**每个**已投影 occurrence 的输入与输出集合端口检查（不只在"已经有两个绑定"时才查——按协调者指示，集合端口必须声明顺序这条由网络准入层强制） |
| `ResourceClaim`（裸字符串 `resource_id`） | 全删。改读 `TaskSemanticBindingV1.resource_reads/resource_writes`（元素是 `ResourceRef(namespace, object_id)`），冲突判定改调契约的 `resource_conflicts()`。身份是 `(namespace, object_id)`，新增反例测试"同名不同 namespace 是两个对象，不冲突" |
| 本地 `EndpointKind` / `NetworkEndpoint` / `TypedEdge` | 全删，改用 `contracts.htn` 版本 |

**一处必须记录的连带修正**：契约 `TypedEdge` 的 `TYPED_EDGE_ENDPOINTS` 规定 SUPPORT/ASSUMPTION 的方向是 **evidence → subject**（与"前置/生产者 → 后继/消费者"一致），而本片初版写反了（occurrence → evidence）。已改：`support_view().by_subject` 现按 `edge.target` 归组，并在 dataclass 上写明理由；测试新增 `support_edge()` 构造器把方向固定下来。

**另一处**：契约 `TypedEdge` 明确拒绝承载 refinement/ORDER/DATA（"它们有自己的契约类型"）。因此 `snapshot.relations(kind)` 不能再合成 `TypedEdge`，改为返回新的只读查询行 `RelationRow(relation, source, target, label)`（端点仍用契约 `NetworkEndpoint`）。`RelationRow` 是**读法**不是可存事实，docstring 写明可存形态是 `ChildBinding`/`OrderConstraint`/`DataRequirement`/契约 `TypedEdge`。

### B3 放宽"compound 只能一个 occurrence"，改按 `(goal_id, goal_occurrence_id)` 区分（阻断，已修）

契约已补 `MethodInstanceDraft.goal_occurrence_id` + `effective_goal_occurrence_id` + `ChildBinding.goal_occurrence_id` + `assert_method_instances_match_occurrences()`（原 CR-6 已由 P1.1 落地）。本片相应改造：

- 删除"compound 任务只能有一个 occurrence"的构造期拒绝。
- 构造期改调 `assert_method_instances_match_occurrences()`，再补两条本网络特有的检查：细化的目标 occurrence 必须在本快照内（delta 可以引用外部节点，闭合快照不行）；**同一 occurrence 不得有两个已采用的方法实例**（OR 不是 AND）。
- 采用关系改为**按 occurrence 解析**：`_adopted_by_occurrence`、`adopted_instance_for(occ)`、`adopted_children(occ)`。原先按 task 解析在共享场景下会张冠李戴。
- `RefinementView.parent_of` → **`parents_of`（多值）**，并新增 `ancestors_of()`。共享子目标被两个采用槽绑定时，两个持有者都要留下——单值映射必然丢掉其中一个责任，正是附件警告的那种抹除。`MethodAlternative` 增加 `goal_occurrence_id`。
- `DUPLICATE_SLOT` 语义收窄：只在**同一个方法实例**用同一 occurrence 填了自己的两个槽时触发；两个**不同**的已采用方法绑定同一 occurrence 是合法共享，不报。
- 新增 5 条共享复合子目标的正向测试（`shared_goal_snapshot()`：`o-left`/`o-right` 两个采用槽共享 `o-shared`，其中一个 `reuse_policy=SHARE_ACTIVE`）：两个父都在 `parents_of` 里、零问题、两个消费者的门都正确接上、共享目标只展开一次（两个节点而不是每个消费者一份）、细化无环。
- 另加 3 条构造期反例：细化到别的任务的 occurrence、细化到 primitive occurrence、细化到本网络之外的 occurrence。

## 2. 应改项

| # | 改法 |
|---|---|
| A4 | `_check_resource_conflicts` 先按 `ResourceRef.key` 分桶，只比同资源声明，成本从 `O(C²)` 降到 `O(Σ|bucket|²)`；每个资源最多列 `MAX_CONFLICTS_PER_RESOURCE = 32` 条，超出时补一条 `BOUND_REACHED`（文案："…{found} 对未排序，列出 {listed} 条；其余是真实存在且未列出的，不是不存在"）。两条测试：9 个写者 → 36 对、列 32 条 + 1 条截断标记；3 个写者 → 列满 3 条且无标记。 |
| A5a | `ORPHAN_REQUIRED_OBLIGATION` 拆成 `ORPHAN_OBLIGATION`（义务在采用计划里没有 occurrence）与 `UNREACHED_REQUIRED_OCCURRENCE`（occurrence 不属于任何方法实例）。两条测试各自断言"另一类不出现"。 |
| A5b | 有环时不再整体跳过 depth 与资源检查。`kahn_order` 改为总是返回 `(placed, remaining)`（`placed` 是健康前缀），验证器对 `_induced(successors, placed)` 这个无环子图继续查 depth 与资源排序，并补一条 `PARTIAL_CHECK` 说明"只查了无环部分（n/m 个节点），环内与环下游的 k 个节点未查"。`placed` 只在验证器内部使用：`report.topological_order` 在有环时仍是 `None`，`require_topological_order` 仍只用 `remaining` 抛错，`diagnose()` 仍不含任何顺序。 |
| A6 | 资源声明只在 primitive 上生效——这条**由契约强制**（`TaskSemanticBindingV1.__post_init__` 拒绝 compound 携带 `resource_reads/resource_writes/side_effect_kind`）。祖先—后代不判互斥的守卫仍然写在 `_check_resource_conflicts` 里（用 `RefinementView.ancestors_of`）作为纵深防御；由于契约的限制它当前不可达，因此**没有为它写投影级测试**，而是直接测了 `ancestors_of` 本身。这是一处有意识的取舍，记在这里以免后来人以为漏测。 |
| A7 | `validate_refinement_acyclic` 改为**按配置分别检查**：一个目标 **occurrence** 取一个方法实例（不是一个 task——TG §12 允许同一共享目标位于多个 occurrence，它们互相不是备选）。基线配置对每个 occurrence 取已采用实例、没有则取首个候选；其余配置每次只换一个 occurrence 的选择。覆盖了采用计划与每一个单点替代，不必枚举笛卡尔积。问题文案点名是哪个配置成环。新增测试：成环配置被点名、两个互斥备选不会被并成一张图而凭空造环。 |
| A8a | `kahn_order` 对邻接表里出现的未知目标键抛 `ContractError`（文案："拓扑排序不会凭空补出没给它的节点"），不再是 `KeyError`。新增测试。 |
| A8b | `ExecutionProjection` 在 `__post_init__` 里建 `_by_node_id` / `_entry_of` / `_exit_of` 三个索引（`compare=False`），`node()` / `entry_node_id()` / `exit_node_id()` 从线性扫描变成 O(1)。`dataclasses.replace` 会重算索引，注入缺陷的测试写法不受影响。 |

## 3. 测试改法

- **"朴素并集"不再手写字面量**：新增测试助手 `naive_union_edges(snapshot)`，从**同一个快照**导出"父等子 + 子等父 + ORDER"的并集 `key → dependencies` 图，喂给既有 `check_dependencies` 拿到 `cycle`；随后断言同一快照的编译投影无环。对照的是一份网络的两种读法，不再是一份可能与编译器漂移的手写反例。
- **"映射回原环"真的回映**：`_rename` 现在返回 `occ_map`，测试把原环路径的节点 id 经 `projection.node(...).occurrence_id` 还原成 occurrence，再用 `occ_map` 映到改名后的命名空间，与改名图给出的环逐集合比较（`remaining` 集合同样比较）。原先只比长度，等于没比。

## 4. 本轮之后的状态

- 生产代码：`graph/task_network.py` 852 行、`graph/projection_validation.py` 911 行（原 909 / 758）。
- 测试：`test_projection_integrity.py` 2040 行，**106 个测试函数 / 117 条用例**（原 87 / 98）。
- `ProblemKind` 现有 14 个值：`cycle`、`dangling_endpoint`、`missing_edge`、`no_gating_children`、`duplicate_slot`、`unbound_port`、`single_port_overbound`、`set_port_unordered`、`orphan_obligation`、`unreached_required_occurrence`、`root_coverage_gap`、`resource_conflict`、`bound_reached`、`partial_check`。
- 原 §4 的 5 条契约变更请求**全部由契约第三轮满足并已切换**：CR-1 → `GraphStructureBudget`（9 维）；CR-2 → `PortSpec.ordering/order_key` + `set_order_declared` + `undeclared_set_ports()`；CR-3 → `ResourceRef` + `resource_reads/resource_writes/side_effect_kind` + `resource_conflicts()`；CR-4 → 契约 `EndpointKind`/`NetworkEndpoint`/`TypedEdge` + `TYPED_EDGE_ENDPOINTS`；CR-6（B3）→ `goal_occurrence_id` + `assert_method_instances_match_occurrences()`。**本片不再持有任何旁置契约类型。** CR-5（`OccurrenceSpec` 与 `TaskSemanticBindingV1` 都带 `obligation_id`/`form`，哪一处是权威）仍未定，本片继续按"必须一致，否则是损坏"处理。

## 5. 验证结果（审阅修复后）

```
uv run --frozen --group dev --extra local-capacity pytest \
  tests/orchestrator/full_target/test_projection_integrity.py -q
→ 117 passed

uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target -q \
  --ignore=tests/orchestrator/full_target/test_input_manifest_resolution.py
→ 630 passed, 1 skipped
  （被 --ignore 的是 P2.2b 尚未落地的 artifacts/input_bindings.py，收集期 NameError；
    按协调者指示跳过，与本片无关）

uv run --frozen --group dev ruff check src/agent_orchestrator/graph \
  tests/orchestrator/full_target/test_projection_integrity.py      → All checks passed!
uv run --frozen --group dev ruff format --check <三个文件>            → already formatted
uv run --frozen --group dev mypy src/agent_orchestrator             → 两个新文件零错误
```

**变异验证（确认每条修复都有测试兜住）**，逐个注入后跑本片测试、随即还原：

| 注入的退化 | 失败条数 |
|---|---:|
| `if not gating:` 改回 `if not children:`（B1 的门兜底） | 2 |
| 去掉 `NO_GATING_CHILDREN` 判定 | 1 |
| 去掉每资源 32 条上限 | 1 |
| `_configurations` 改成"所有实例并成一张图"（A7） | 1 |
| 有环时不对无环子图继续检查（A5b） | 6 |

全部还原后 117 全绿。
