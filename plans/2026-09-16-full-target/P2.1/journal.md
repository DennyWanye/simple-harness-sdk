# P2.1 实施记录（HTN 核心纯函数：注册准入 / 参数绑定 / 递归细化 / 编译增量 / 结构与覆盖验证 / 种子方法库）

日期：2026-09-16｜基线：SDK main `623d4c8`（工作区含 P1.2、P2.1c、P2.2b 等并行子代理未提交产物）｜执行：单个 Opus 子代理｜实际耗时：约 2 小时 15 分（含规范阅读约 35 分钟、变异自证约 10 分钟、编排范围回归约 10 分钟）

规范来源：
主计划 `simple_harness/plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`
§5（完整目标架构）、§6.1（Obligation）、§6.2（form 两轴）、§6.3（MethodContract）、§6.4（MethodInstance、递归燃料按 obligation、goal_id = TaskRef）、§6.5（四类关系）、§6.6（前提表达式、资源读写、三条钉死规则、三阶段）、§7.1（什么才算实现）、§7.2（推进策略、ADaPT 安全约束）、§7.3（方法来源与六步准入、状态机、试点域与种子方法库）、§7.4（PANDA/HDDL 边界）、§8.1（AND–OR 满足关系）、§8.3（共享目标去重）、§18.3（核心函数合同、`PlanProposal` vs `ProposedPlanDelta` 命名）、§18.5（C8 脚本化 `method_proposal`）、§23（P2.1 行）、§24.1（裁决 1/2/3/9/10）；
附件 `simpleharness-taskgraph-design.zh-CN.md` §4.1（细化/满足）、§4.2（ORDER 释放条件）、§4.3（DATA 端口）、§6（复合边界编译）、§12（共享子任务）；
附件 `simpleharness-taskgraph-implementation-design.zh-CN.md` §3.2（AND–OR 需要方法节点）、§3.3（方法递归与执行循环）、§5.1（compound 不能与孩子互相等待）、§5.2（编译十步）、§5.3（算法选择）、§6（两种 frontier）、§9.1（四种去重）、§9.2（成果复用与 Demand）、§9.3（退役 membership）、§9.4（HTN 见证保持 occurrence）。

## 1. 实现清单

### 生产代码（6 个新文件 + `planning/htn/__init__.py` 只加导出）

| 文件 | 行数 | 主要类型与函数 |
|---|---:|---|
| `planning/htn/registry.py` | 1899 | `SchemaField`/`ObjectSchema`/`SchemaCatalog`；`TaskTypeSpec`/`TaskTypeCatalog`（compound 目标与 primitive Operator 同一张表，`form` 区分；端口、能力、副作用、资源、`reuse_policy`、`observes`、`preconditions`）；`AdmissionStepId`(6)、`StepOutcome`(4)、`AdmissionVerdict`(3)、`RejectionCode`(14)、`AdmissionProblem`、`AdmissionStepRecord`、`AdmissionReceipt`、`AdmissionPolicy`、`MethodProposal`（含 `from_json` 解 §18.5 C8 标签块）、`MethodCandidate`、`MethodSuggestion`、`MethodRegistry`（`admit`/`promote`/`suspend`/`reinstate`/`retire`/`note_trial_use`/`candidates_for`/`suggest_for`）；三个准入检查子函数 + `_first_cycle`；`implied_orderings`、`method_is_recursive`、`statement_similarity` |
| `planning/htn/grounding.py` | 847 | `GroundingError`；`derive_id`/`instance_identity`/`slot_identity`/`SlotIdentity`；`SharingSignature`、`ShareVerdict`(4)、`ShareDecision`、`may_share`、`SharedGoalEntry`、`SharedGoalIndex`（`lookup` 精确、`suggest` 只出建议）、`ShareSuggestion`；`SlotPlan`、`plan_slots`、`requiredness_of`、`resolve_arguments`；`ground_method`（§18.3）、`child_task_bindings`、`task_binding_for`、`data_flows` |
| `planning/htn/refinement.py` | 1076 | `AttemptPolicy`(2)、`RefinementOutcome`(9)、`FrontierItem`、`planning_frontier`；`LeafDecision`、`leaf_decision`、`bounded_attempt_admissible`；`UnknownProposition`、`unknown_predicates`、`EvidenceOccurrenceRequest`、`evidence_requests`；`CandidateAssessment`、`assess_candidates`（受 `max_candidates` 约束）；`RejectedCandidate`、`MethodProposalRequest`；`RefinementDecision`、`RefinementReport`、`refine`；`_ancestor_repeat`、`_open_children` |
| `planning/htn/compiler.py` | 1039 | `BudgetRequirement`、`RefinementCompilation`、`CompilationRefused`（带 `ProjectionReport`）；`compile_refinement`（§18.3 签名）与 `compile_refinement_bundle`（十步全量产物）；`_compile_order`、`_compile_data`、`_compile_coverage`、`_merge`、`_orphaned_occurrences`、`_is_open_frontier_gap`、`build_read_set`、`unbound_required_ports`、`RootNetwork` |
| `planning/htn/validation.py` | 810 | `DeltaProblemKind`(11)、`PreconditionClass`(5)、`DeltaProblem`、`PreconditionVerdict`、`DeltaReport`、`merge_delta`、`validate_delta`（可约化性 / 端口可绑定 / 前提可满足性四分类 / 根覆盖 / 规模）；`HDDL_FRAGMENT`、`UnsupportedFeature`、`HddlExport`、`unsupported_features`、`to_hddl` |
| `planning/htn/seed_methods/loader.py` | 372 | `SEED_ROOT`、`DOMAIN_FILES`、`seed_content_hash`、`fill_content_hashes`、`SeedDomain`、`predicate_signature_from_json`、`load_domain_path`、`available_domains`（扫描目录，不列举域名）、`load_domain`、`install_domain`、`admit_domain`、`install_library`、`describe` |
| `planning/htn/seed_methods/__init__.py` | 38 | 公开导出 |
| `planning/htn/__init__.py` | 136 | **只加导出**（原文件 3 行只有 docstring），补上五个模块的公开面与一段管线说明 |

种子数据（纯 JSON，3162 行）：`seed_methods/code/{schemas,predicates,task_types,methods}.json`、`seed_methods/appworld/{同上}.json`。

`contracts/`、`graph/`、`knowledge/`、`verification/`、`orchestrator/`、`scheduling/`、`artifacts/`、`storage/`、`runtime/`、`event_handler` 零改动；另两个并行子代理的写入范围（`artifacts/input_bindings.py`、`storage/`）零触碰。

### 红测试（5 个新文件 + 1 个夹具模块 + 12 个夹具 JSON）

`tests/orchestrator/full_target/fixtures/htn/htn_world.py`（542 行）：`Env`（schemas/predicates/task types/capabilities/registry/policy 一体）、`seed_env`、`add_domain`、`task_binding`、`root_network`、`ledger_for`、`method`/`step`/`atom`/`param`/`const`/`out` 构造器、`load_proposal`。
`fixtures/htn/proposals/*.json`（11 个脚本化 `method_proposal` 块，模拟 LLM 输出）；`fixtures/htn/domains/widget/*.json`（第三个虚构域，4 个文件）。

| 文件 | 用例数 | 覆盖 |
|---|---:|---|
| `test_htn_grounding_partial_order.py` | 69 | 身份（一槽一步、`(instance, slot)` 派生、注入、重复接地字节相同、两组参数两实例/占位互斥/各自绑定/digest 不同、`refines_parent` 承父义务、`independent_authorized` 开新义务、含分隔符的 slot key 不碰撞、方法 hash 被钉、SELECT 阶段见证）；类型检查（错类型、缺参数、多余绑定不入实例、原子目标、签名不符、四值三种拒绝、能力不可用、常量 TRUE 不开门、步骤类型未注册、不改输入）；部分序（DATA 边数 = 声明数、绑定声明端口、端口 schema、不凭空加 ORDER、两种线性化都合法、`P.exit→Q.entry`、默认 `accepted`、DATA 隐含序进入准入环检、投影无环、编译确定、不改入参网络、占位/表单/requiredness/覆盖/read-set/预算/十步/子绑定/父采用）；端口与环拒绝（未声明输出口、schema 不匹配、读自身输出、ORDER 环、DATA 环、环路文案、异源 draft、方法 hash 不符、未准入方法、只读副作用、无索引不共享、契约拒绝 NEW_WORK 借目标、目标类型/参数 schema 未注册） |
| `test_htn_and_or_shared_goal.py` | 33 | OR（选中可用替代、两条都被评估、被反证的报 `PRECONDITION_FALSE`、落选方法零 occurrence、根不被阻塞、两条都可用时选择确定、一个 occurrence 只一个采用实例、全不可用出 `MethodProposalRequest`、候选数受 `max_candidates` 截断）；AND（同方法槽全必需、无声明序则无 ORDER/DATA）；共享（C 只一个 occurrence、两个消费者、第二消费者是引用而非新建、共享后仍无环、取消一条方法 C 保留且只剩一个父）；共享拒绝（scope 不同、参数不同、类型未开 reuse、副作用不共享、契约拒绝"有副作用又声明 reuse 但无 effect_identity"、三个签名字段各自单独拦截、完全一致可共享、索引未命中给理由、词面相似只出建议、建议绝不变成 child binding、无 Acceptance 退化为 `SHARE_ACTIVE`、有 Acceptance 为 `REUSE_ACCEPTED`、网络没有该 occurrence 则拒绝、共享槽不产生第二份语义绑定、Acceptance 进 read-set 且 occurrence 进 `referenced_occurrences`） |
| `test_htn_novel_method_admission.py` | 64 | 空库（出 `MethodProposalRequest`、带目标类型、可 JSON 化、不花燃料）；happy path（到 `TRIAL_ADMITTED`、状态链三段、试用限当前 Mission、注册行作者是 registry service 而回执记提交者、注册指向回执、回执内容寻址、前三步 PASSED、第 4/5 步 DEFERRED 而非 PASSED、可检索、人工提交同路径、同字节幂等）；拒绝（9 个夹具 × 各自 `RejectionCode`、缺 Operator 进 `missing_operators` 且文案说"cannot execute"、缺能力进 `missing_capabilities`、覆盖缺口点名准则、被拒不可检索、被拒仍留 REJECTED 注册行、被拒不留可用定义、失败步之后记 NOT_REACHED、模型自填状态在任何检查之前被拒且 `steps == ()`、文案回引所声明状态、契约层也拒、作者与提交声明不符、零步骤、为原子类型写方法、步骤 form 不符、准则连到不存在步骤、策略步数上限）；生命周期（EVALUATED/ADMITTED 均 `PROMOTION_NOT_AVAILABLE`、文案点名 P8、promote 不改注册、SUSPENDED 不被检索但留定义、SUSPENDED 出现在建议里并带状态、reinstate 可回、非 SUSPENDED 不可 reinstate、RETIRED 不被检索、试用对别的 Mission 不可见但出建议、试用计数按 Mission、候选带计数、未注册方法不可 suspend）；检索（异版本只出建议、建议绝不是候选、建议全部 advisory、相似度对称有界、递归方法被识别且仍准入、无界递归文案、同 hash 两份定义被拒） |
| `test_compound_gate_no_pseudo_cycle.py` | 61 | 门（compound 孩子编译出 entry/exit、门不计费、primitive 单节点、未展开 compound 仍有 span 且 origin 写 `unexpanded`、父 entry 开全部孩子、每个把门孩子 exit 关父、编译后是 DAG、**同一批事实的朴素并集喂给既有 `check_dependencies` 抛 `DependencyError` 而编译后有拓扑序**、细化关系无环、展开后 span 消失并由真实孩子关门、两层仍无环、compound 的 DATA 边从 exit 出、compound 孩子无 operator/无资源、预算按 form 分列）；拒绝（根覆盖缺口、点名缺哪条、必需输入端口无生产者、父不在网络、父是 primitive、一个 occurrence 两个采用实例、退役后可换、退役进 delta、规模上限报 `BOUND_REACHED`、拒绝携带结构化报告）；验证（`validate_delta` 放行、缺输入时报 `NOT_CHECKED` 而不是放行、可约化性未检查时说明、无方法可refine 的 compound 报 `NOT_REDUCIBLE`、注册方法后不再报、前提 FALSE/UNKNOWN/CONFLICT 三分类、`unbound_required_ports`、编译不改入参网络）；纯度（5 模块 × 4 个被禁 import、5 模块无域名分支） |
| `test_seed_methods.py` | 70 | 库（两域就位、每域四文件、≥3 方法、含递归方法、含 OR 对、≥5 被观察谓词、观察器只读且不写资源、每条谓词都有观察器、每域有可共享只读子目标、每个种子方法到 `TRIAL_ADMITTED`、没有任何方法是 ADMITTED、派生 hash 稳定、摘要确定、非域目录被拒）；分解（code 分解并编译通过验证、appworld 同、API 不支持时切 search 替代、两域走完全相同的调用序列）；**第三个虚构域**（只靠数据注册、能分解、编译通过、自己选替代、不干扰前两域）；递归（选中递归方法、首展耗 1 燃料、开出同目标类型 compound 孩子、燃料足时展第二层、子义务花的是父 obligation 的燃料、耗尽报 `BOUND_REACHED`、报告点名 obligation 与已展开、序列化 status 为 `BOUND_REACHED`、已展开结构不被回收、文案不含 UNSOLVABLE、同参数重展在扣燃料前被拒、重复祖先报 `NO_STATE_CHANGE` 且不扣燃料、文案点名祖先、基例方法收敛）；取证（UNKNOWN 前提出取证 occurrence、只读、点名谓词、高风险叶子不派发、前提解决后成 LEAF、无观察器时明说而不猜）；尝试策略（只读可先试、可逆本地写可先试、不可逆外部写必须先规划、compound 永不是叶子、无 Operator 不可执行、缺能力挡住叶子）；HDDL（种子增量可导出、occurrence 映射完整、声明 fragment、不加声明外的序、导出确定、析取前提 / 集合端口 / 跟随版本策略三种 `UnsupportedFeature`）；通用性（loader 与 seed `__init__` 源码里不出现域名字面量） |

合计 **297 条**（69 + 33 + 64 + 61 + 70），全绿。

红→绿过程：先只写 `test_htn_grounding_partial_order.py`，`pytest` 报 `ModuleNotFoundError: agent_orchestrator.planning.htn.registry`（红）；五个模块落地后逐文件转绿，期间修掉四个真实缺陷（见 §3）。

## 2. 与规范的关键对应

- **§18.3 函数合同**：`assess_method`（P1.1 已有）、`ground_method(task, method, bindings, assessment)`、`compile_refinement(draft, current)` 三个签名的位置参数逐字保留；部署事实（catalog / schemas / registry / sharing / budget）作为关键字参数传入，使得同一份回执可以复现同一次编译。`compile_refinement` 返回 `ProposedPlanDelta`；需要同时拿到新建语义绑定、预算需求与两份结构报告的调用方走 `compile_refinement_bundle`，它是前者的唯一实现。
- **§18.3 命名规则**：`PlanProposal`（模型侧、未检查）与 `ProposedPlanDelta`（编译器输出、可交 Commit）在本片全程不混用；`MethodProposal` 是方法注册侧的提案，与前两者是三个不同类型。
- **§7.3 六步准入**：`AdmissionStepId` 逐条落成枚举，回执按序记录每一步的 `PASSED / FAILED / DEFERRED / NOT_REACHED`。第 4 步（独立规划审阅，真实模型调用）与第 5 步（HDDL/PANDA）在 P2.1 记 **DEFERRED 而非 PASSED** —— 回执不能宣称没人跑过的检查。失败即停，其后的步骤记 `NOT_REACHED`。
- **§7.3 状态机**：`DRAFT → STRUCTURALLY_VALID → TRIAL_ADMITTED` 由 `admit()` 写入，`transitions` 字段把这条链本身作为事实记录。`promote()` 对任何超出 `TRIAL_ADMITTED` 的目标一律 `PROMOTION_NOT_AVAILABLE` 并在文案里点名 P8；`SUSPENDED` 不被 `candidates_for` 命中但保留定义与历史实例，`reinstate` / `retire` 是 registry service 的写入口，本片没有任何自动触发它们的路径（"何时算反例"是 P8 的淘汰边）。
- **§6.3 / §7.3：模型不能自填状态**：`MethodProposal.declared_status` 存在的唯一目的就是被拒绝。`author is MODEL and declared_status is not DRAFT` 在**任何检查之前**返回 REJECTED（回执 `steps == ()`），并且晋级后的注册行一律由 registry service 署名 `SYSTEM` —— 契约 `MethodRegistration` 本身就拒绝"模型作者 + 超过 DRAFT 的状态"，这两处是同一条规则的两道闸。
- **§24.1 裁决 2 / TG §6 / 实现稿 §5.1（复合边界）**：编译器不自己造门 —— 它产出的网络交给 `TaskNetworkSnapshot.execution_projection()` 生成 entry/exit，然后**回头校验投影确实有门**。`test_the_naive_union_of_the_same_network_is_a_cycle` 用同一批编译产物构造朴素并集喂给既有 `graph/dependency_checker.check_dependencies`（抛 `DependencyError`），再断言同一网络的投影有完整拓扑序。
- **§24.1 裁决 1 / TG §4.2（ORDER）**：`_compile_order` 一律发 `release_condition=accepted`，投影把它编译成 `before.exit → after.entry`；方法里没声明的两个步骤之间绝不补边。`test_no_order_edge_is_invented_between_independent_steps` 与"链式线性化"变异（见 §4）一起把这条钉死。
- **§24.1 裁决 3 / TG §4.3（DATA）**：DATA 的唯一来源是步骤参数里的 `OutputValue(step, port)` —— `grounding.data_flows` 是这个读法的单一实现，准入期的环检查、编译期的 `DataRequirement` 生成与 HDDL 导出全部调它，三者不可能对"这个方法的数据流是什么"有不同意见。生产端口与消费端口必须由各自任务类型声明，且 `schema_ref` 精确相等（TG §4.3："完全匹配或注册的显式兼容声明"，本片只实现前者并显式拒绝后者之外的情况）；单值端口重复绑定在编译期就被拒。
- **§24.1 裁决 9 / §8.3 / TG §12（四种去重）**：`SharingSignature` 把目标合同、类型化参数、输入版本、授权 scope、语义 scope、assurance、freshness、领域语义、副作用种类与副作用身份九项全列出来，任何一项不同即 `SIGNATURE_DIFFERS`；类型没声明 `reuse_policy` 即 `REUSE_NOT_PERMITTED`；有副作用又没有显式 `effect_identity` 即 `SIDE_EFFECT_NOT_SHAREABLE`（"两次同参数的发送就是两次发送"）。词面相似只走 `SharedGoalIndex.suggest`，返回的 `ShareSuggestion` 恒带 `advisory_only=True`，本包没有任何一条代码路径把它变成 `ChildBinding`。
- **§8.3 / TG §9.3（退役只删自身采用关系）**：退役一个方法实例时，只有**仅由它开出**的 occurrence 上的 ORDER/DATA 边随之退役；仍被其他在采用方法绑定的 occurrence（共享子目标）不受影响。`_orphaned_occurrences` 是这条规则的实现，`test_cancelling_one_consumer_keeps_the_shared_goal` 与"保留退役方法的边"变异一起钉死。
- **§6.1（义务不因拆任务重置）**：`slot_identity` 按 `obligation_relation` 决定子义务 —— `refines_parent` 的槽**沿用父 `obligation_id`**，只有 `independent_authorized` 才派生新义务。这既是"拆任务不自动创建新的重试额度"的直接实现，也让递归深度被父义务的燃料真正约束住（否则每层都是新义务，燃料永远花不完）。occurrence 与 task 仍逐槽派生，TG §12 的"出现位置保留独立身份"不受影响。
- **§6.4 v1.2（递归燃料）**：`refine` 每次实例化调一次 `ledger.consume_fuel`；`REPEATED_EXPANSION`（同义务同方法同参数）与 `NO_STATE_CHANGE`（祖先已用同方法、同参数、同世界快照展开过）**都在扣燃料之前**判定并返回，所以一个不终止的方法不会靠烧光预算来抵达它的界。耗尽返回 `BOUND_REACHED` + `BoundReachedReport`（已展开列表 + 未决子义务），文案与序列化里都不出现 UNSOLVABLE，已建结构一个节点不回收。
- **§6.6 rule 2（授权闸门）**：`ground_method` 在方法有前提时要求 `report.authorization.allowed`；常量 TRUE、空表达式、混入 STALE/MISSING 的 TRUE 一律不放行。`assess_candidates` 把"APPLICABLE 但闸门不开"单独记为不可选并给出理由，`refine` 对这种局面返回 `NOT_AUTHORIZED` 而不是 `NO_APPLICABLE_METHOD`。
- **§6.6 rule 3 / 前提三阶段**：接地时冻结 `PreconditionWitnessRecord(phase=SELECT, truth=TRUE)`；`validate_delta` 拿方法定义重算并四分类（`SATISFIED / REFUTED / NEEDS_EVIDENCE / CONFLICTED`），见证对应的条件在该方法版本里已不存在时报 `PRECONDITION_WITNESS_STALE`。
- **§7.2 / ADaPT**：`AttemptPolicy` 只对 `low_risk` 的类型给 `BOUNDED_ATTEMPT_FIRST` —— 只读、或类型自己声明可逆的本地写。外部状态写、外部事件写一律 `PLAN_BEFORE_ATTEMPT`，与代价估计无关。
- **ADR-07（UNKNOWN 不是 FALSE）**：叶子有未解前提时 `refine` 返回 `NEEDS_EVIDENCE` 并产出 `EvidenceOccurrenceRequest` —— 一个由 `TaskTypeCatalog.observers_for` 选出的**只读**观察器 occurrence（含完整 `OccurrenceSpec` 与 `TaskSemanticBindingV1`），而不是把高风险叶子派出去试。没有注册观察器时明说"这条前提仍是 UNKNOWN，那不是 FALSE"，不猜。
- **实现稿 §5.2 编译十步**：`compile_refinement_bundle` 按序执行并把每一步记进 `RefinementCompilation.steps`（`test_the_compilation_records_all_ten_steps` 断言恰好十条）。第 10 步的内容是"不在这里做"——本模块不提交、不建 Agent、不碰生产工具。
- **实现稿 §3.3 / §5.2 步骤 8**：环检测只证明无环，可约化性、端口可绑定、前提可满足性、根覆盖由 `validate_delta` 分别报告，每类一个 `DeltaProblemKind`。缺少检查所需输入（registry / methods / snapshot / predicates）时返回 `NOT_CHECKED` **而不是放行** —— 否则"没检查"与"检查通过"无法区分。
- **§7.4 / §8.3（HDDL 最小接口）**：`to_hddl` 只导出 `strips-typed-partial-order` 片段；析取前提、结构化对象、数值、集合端口、跟随授权版本的 DATA 策略、非 `accepted` 的释放条件各自被 `unsupported_features` 点名后整体返回 `UnsupportedFeature`，不做近似、不偷偷线性化。复用被导成显式的 `m_reuse_*`"已有结果可用"方法，`occurrence_map` 保留每个出现位置到导出标签的映射（§8.3："不得把缩短后的执行列表伪装成原任务网络的有效计划"）。
- **§7.1 / §7.3 通用性门槛**：整包无 `if domain == ...`（测试逐模块 grep 五个源文件 + loader + seed `__init__`）。`available_domains()` 靠扫描目录发现域而不是列举名字，所以"第三个域只靠数据即可"是可检验的事实：`tests/.../fixtures/htn/domains/widget` 是一个凭空造出来的域，只有四个 JSON 文件，走完全相同的 `load_domain_path → install_domain → admit_domain → refine → compile` 调用链。
- **种子方法库（§7.3 v1.2）**：`code` 与 `appworld` 各 4 个 MethodContract（各含 1 个递归方法 + 1 对 OR 替代 + 1 个可共享只读子目标）、各 6 条谓词且每条都有只读观察器（code 5 个观察器类型，appworld 5 个）。它们由人工编写并走同一注册协议，全部停在 `TRIAL_ADMITTED`；测试断言注册表里不存在任何 `ADMITTED` 的方法。

## 3. 红→绿过程中发现的真实缺陷（均已修）

1. **编译器把"别的根还没细化"当成致命错误**。`validate_execution_projection` 的根覆盖检查是面向整网的终局问题；增量编译时另一个尚未细化的根必然缺覆盖，照单全收会让增量规划根本无法推进。修复：`_is_open_frontier_gap` 只放行"该根尚无采用方法"的覆盖缺口，本增量自身的覆盖仍由第 7 步针对它所细化的义务严格把关。
2. **退役方法时把它的边留了下来**。退役实例后其孩子 occurrence 不再被投影，遗留的 ORDER/DATA 边指向未投影节点 → `MISSING_EDGE`。修复：`_orphaned_occurrences` 随 membership 一起退役这些边，并显式保护仍被其他在采用方法绑定的共享 occurrence。
3. **异源 draft 能编译进别的目标**。原先只校验 occurrence / task / obligation 三元组，两个同类型目标的 draft 可以互换。修复：`_check_parameters_agree` —— draft 的参数可以比任务多（规划者的选择），但不得与任务自身的 `typed_parameters` 矛盾。
4. **`find`/`admit` 对"未注册的原子步骤类型"报 `UNKNOWN_TASK_TYPE`**，让读者去找一个目标。对 primitive 步骤而言任务类型**就是** Operator，修复为 `UNKNOWN_OPERATOR` 并写入 `missing_operators`，文案点明"这个部署无法执行该方法"。

## 4. 变异自证（14 条，全部被捕获）

在隔离运行中逐条注入、跑本片 297 条、随即还原：

| # | 变异 | 失败条数 |
|---:|---|---:|
| 1 | registry：允许模型自填 registry status | 5 |
| 2 | registry：`promote()` 直接批准 | 2 |
| 3 | registry：跳过根覆盖检查 | 3 |
| 4 | registry：接受无守卫的递归方法 | 2 |
| 5 | registry：不再报告缺失能力 | 2 |
| 6 | grounding：接地一个不适用的方法 | 3 |
| 7 | grounding：允许共享有副作用的目标 | 1 |
| 8 | grounding：每个子槽都开新义务（不承父债） | 11 |
| 9 | compiler：把槽线性化成一条链 | 6 |
| 10 | compiler：接受端口 schema 不匹配 | 1 |
| 11 | compiler：退役方法时保留其边 | 2 |
| 12 | refinement：先扣燃料再查无状态变化递归 | 1 |
| 13 | refinement：任何叶子都可以"先试一次" | 1 |
| 14 | refinement：前提未解也派发叶子 | 4 |

还原后基线 297 全绿。

## 5. 门槛核对

| 门槛 | 结果 |
|---|---|
| 本片测试 | 297 passed（`test_htn_grounding_partial_order` 69、`test_htn_and_or_shared_goal` 33、`test_htn_novel_method_admission` 64、`test_compound_gate_no_pseudo_cycle` 61、`test_seed_methods` 70） |
| `tests/orchestrator/full_target` | 1404 passed / 1 skipped（PANDA 真实解析器缺席） |
| 编排范围回归 `tests/orchestrator`（排除 gap_phase1） | 3370 passed / 21 skipped / **1 failed = 已知 p33 Python 3.14 历史 AST hash 断言**（HANDOFF 已记，与本片无关） |
| `ruff check` | All checks passed（新源文件 + 新测试 + 夹具模块） |
| `ruff format --check` | 新建目录 11 files already formatted |
| `mypy src/agent_orchestrator/planning` | Success: no issues found in 16 source files |
| 零 store/commit import | `test_the_planning_modules_import_no_persistence_layer`：5 模块 × {storage, sqlite3, commit_service, scheduling} |
| 不调模型 | 本片无任何 provider/runtime import；`method_proposal` 全部来自 `fixtures/htn/proposals/*.json` |
| 无域名分支 | `grep -rn '== "code"\|== "appworld"' src/agent_orchestrator/planning/htn/` 无命中；测试另有逐模块断言 |
| `git status` 范围 | 新增 `planning/htn/{registry,grounding,refinement,compiler,validation}.py`、`planning/htn/seed_methods/`、`tests/.../fixtures/htn/`、5 个测试文件；修改仅 `planning/htn/__init__.py`（只加导出）。工作区其余改动属并行子代理（P1.2 storage、P2.1c eligibility、P2.2b input_bindings、P1.1 契约第三轮等） |

## 6. 契约变更请求（交 P1.1 契约持有者统一处理，本片未改 contracts/）

1. **`RegistryAuthor` 缺 `HUMAN`**。§7.3 的四种方法来源里"人工/工具作者提供"是第一种，但枚举只有 `SYSTEM` / `MODEL`。本片把人工提交记为 `SYSTEM` 并在 `admit_domain` 的 docstring 里写明这是契约缺口而不是"其实不是人写的"。建议加 `HUMAN`（并入 `MODEL_SUBMITTABLE_STATUS` 之外的作者集合）。
2. **`MethodStep` 缺 `reuse_policy`（或等价的 `allow_reuse`）**。§8.3 与 TG §12 都说"只有方法明确允许 `reuse`"才可共享，但方法契约里没有承载这句话的字段。本片把它表达在 `TaskTypeSpec.reuse_policy`（任务类型声明自己的成果是否可被复用），语义上更窄也更安全，但与规范原文的落点不同。建议在 `MethodStep` 上加一个可选字段，让"这个槽允许复用"成为方法作者的显式声明。
3. **`ChildBinding` 缺 `acceptance_ref`**。TG 裁决 9 / 实现稿 §5.2 步骤 4 要求"对允许 reuse 的槽绑定**精确** Acceptance"。契约里 `reuse_policy=REUSE_ACCEPTED` 没有地方放那个 Acceptance id。本片要求调用方通过 `reuse_acceptances` 参数提供，并把它记进 `SemanticReadSet.acceptance_revisions`；建议契约加字段，使这条约束由类型保证而不是由编译器参数保证。
4. **`ProjectionProblem` 的 `ROOT_COVERAGE_GAP` 没有结构化主语**。`compiler._is_open_frontier_gap` 目前要从 `detail` 文本里把根 occurrence 认出来（`graph/` 属只读范围，未改）。建议给这一类问题填上 `nodes=(str(root),)`，届时本片可以删掉那段文本匹配。
5. **`PredicateSignature` 没有 codec**。种子库需要从 JSON 读谓词声明，本片在 `seed_methods/loader.predicate_signature_from_json` 里自带了一个解码器，形状与 `PredicateSignature.to_json` 对称。建议把 `from_json` 收进 `knowledge/predicates.py`，本片改为直接调用。

## 7. 偏差与自觉的取舍

- **`compile_refinement` 的额外产物**：`ProposedPlanDelta` 按契约不携带 `TaskSemanticBindingV1`（TG §3.2 让它自己一张记录），但一次细化确实会新建若干语义绑定。为同时守住 §18.3 的签名与这个事实，`compile_refinement` 保持 `-> ProposedPlanDelta`，`compile_refinement_bundle` 返回 delta + 新绑定 + 预算需求 + 两份报告，前者是后者的薄封装。
- **种子引用的 content hash 是派生的**：`seed_content_hash(id, version)`。方法自身的 `method_ref()` 仍是对自己字节的真实哈希；派生只用于四个文件之间的**前向引用**，否则需要人工在四个文件里抄六十四位十六进制，那是一种会静默失效的夹具。loader 的 docstring 明写这是夹具约定，不是"这些 id 是内容寻址的"。
- **`TaskTypeSpec` 合并了 compound 目标与 primitive Operator**：§18.4 的表里 `method_contracts` 与 Operator 注册是两件事。合成一张按 `form` 区分的表，是因为"步骤的任务类型必须已注册"这条检查对两者完全同构，分表会让通用性门槛多出一处按形态分叉的代码。
- **`TaskTypeSpec.preconditions`（新增数据字段）**：叶子"有没有未解决的必需前提"必须可判定，而 `TaskSemanticBindingV1.precondition_refs` 只存摘要、无法重算。把结构化前提放在任务类型上是数据驱动且领域中立的做法，与方法的 `applicable_when` 同一套 AST 与同一个求值器。
- **`refine` 不是纯函数**：它会在账本上扣燃料。这是刻意的——燃料是本片唯一的状态变更，且只在真的产出一次展开时发生；所有拒绝路径（重复、无状态变化、未授权、无方法）都在扣之前返回。
- **`suspend` / `reinstate` / `retire` 属 P8 的淘汰边**：本片提供状态转移入口（否则"SUSPENDED 不被检索"无法验收），但没有任何自动触发它们的判断逻辑——"什么算反例"由 P8 决定。
- **未做**：独立规划审阅（§7.3 步骤 4，需真实模型，P2.3）、HDDL 求解/验证回路（§7.4，P2.2 的 `backends/panda.py`）、`achieve_outcome` 通用能力的接线（§6.4，P2.3c）、`EVALUATED → ADMITTED`（P8）。这些在回执与 docstring 里都是显式的 DEFERRED / `PROMOTION_NOT_AVAILABLE`，不是沉默跳过。

---

# 审阅修复（第一轮）

日期：2026-09-16｜触发：独立审阅「需修后合并（准入、递归、编译、共享主体正确；HDDL 导出有一处严重缺陷）」｜实际耗时：约 1 小时 20 分（含契约第五、六轮切换与重跑）

## 0. 契约第五、六轮切换（本片改为使用新字段，contracts/ 仍只读）

| 契约新增 | 本片切换 |
|---|---|
| `RegistryAuthor.HUMAN` | `seed_methods.loader.admit_domain` 的默认作者改为 `HUMAN`；准入协议对 `HUMAN` 与 `SYSTEM` 同等（§7.3 的规则针对*模型*自我晋级），晋级后的注册行仍由 registry service 署名 `SYSTEM`。原 CR#1 关闭 |
| `MethodStep.reuse_policy` | 新增 `registry.effective_reuse_policy(step, spec)`：步骤自己声明的优先，`None` 表示"取任务类型的默认"；`plan_slots` 改用它。准入新增一条：步骤不得为"有副作用且无 `effect_identity`"的类型声明复用。原 CR#2 关闭 |
| `ChildBinding.acceptance_ref`（仅 `REUSE_ACCEPTED` 可带） | `SharedGoalEntry.acceptance_ref` / `SlotPlan.acceptance_ref` / `reuse_acceptances` 全部改为 `TypedRef`；`ground_method` 把它写进 `ChildBinding.acceptance_ref`，`SHARE_ACTIVE` 与 `NEW_WORK` 一律为 `None`（I01：在途工作还没有验收可指）。原 CR#3 关闭 |
| `PredicateSignature.from_json` | 删除 `loader.predicate_signature_from_json`（35 行）改调契约。原 CR#5 关闭 |
| `ObligationOpening` / `BudgetInheritance` / `ProposedPlanDelta.obligation_openings` / `require_commit_ready(registered_obligations=…)` / `ObligationLedger.open_from` | 见下 §1 第 4 条。原 CR#6 关闭 |

## 1. 逐条对应审阅意见

### 必改 1（严重）：HDDL 导出对真解析器不成立 —— 已重写

旧导出的四处缺陷全部属实：抽象任务 `t_available` 没有任何 method 把它分解到 `a_*`（所有 `:action` 不可达）；`(:htn :subtasks …)` 把父与全部子 occurrence 并列为顶层任务（子任务被要求执行两遍）；method 的 `:parameters (?o - occurrence)` 是自由变量；`t_<signature>` 声明后无人引用。

重写后的模型是**全 ground** 的：

- 每个 occurrence 是 domain 的 `:constants`（不是 problem 的 `:objects`），于是 method 的 `:task` 可以直接写常量，`:parameters ()`，没有自由变量；
- 只声明一个抽象任务 `t_available`，删掉所有 `t_<signature>`；
- 每个 occurrence 恰好一条 method：primitive → `m_do_<label>` 分解到 `(a_<sig> <label>)`；已细化的 compound → `m_<instance>` 分解到孩子的 `t_available`，`:ordering` 只来自声明过的 ORDER ∪ DATA（§7.4：部分序不被偷偷线性化，无声明则整段省略 `:ordering`）；复用 → `m_reuse_<label>`，`:precondition (accepted_result <label>)`、`:subtasks ()`，即 §8.3 / TG §12 要求的显式"已有结果可用"方法；
- 初始任务网络只放本增量所细化的目标（`_export_roots`），其余由分解产生；
- 从根按采用的细化关系遍历（`_collect_export_nodes`），遇到**无人细化的 compound** 直接返回 `UnsupportedFeature("unexpanded-compound-occurrence")` —— 给它一条空 method 等于宣称"什么都不做就达成了目标"；无方法实例的 delta 返回 `no-decomposition-to-export`。

未使用 `export_unverified` 兜底：本轮已完成联调，导出可被消费。补的 13 条测试：

| 测试 | 钉住 |
|---|---|
| `test_every_abstract_task_has_at_least_one_method` | 声明的抽象任务 ⊆ 被分解的抽象任务 |
| `test_every_declared_task_is_actually_used` | 无引用的任务声明 |
| `test_every_action_is_reachable_from_the_initial_network` | 从 problem 的根按 method 遍历，可达动作集 == 声明动作集 |
| `test_the_initial_network_contains_only_the_refined_goal` | 顶层任务只有根 occurrence |
| `test_no_method_carries_a_free_variable` | 每条 method `:parameters ()` 且 `:task` 无 `?` |
| `test_every_occurrence_is_a_declared_constant` | occurrence_map 的每个 label 都在 `:constants` |
| `test_one_method_per_exported_occurrence` | method 的 `:task` 集合 == 导出 occurrence 集合 |
| `test_an_unexpanded_compound_is_outside_the_fragment` | 未细化 compound → `UnsupportedFeature` |
| `test_a_delta_with_no_decomposition_exports_nothing` | 空 delta → `no-decomposition-to-export` |
| `test_the_export_passes_the_panda_adapter_s_fragment_check` | 走 `backends.panda.check_fragment` |
| `test_a_plan_written_against_the_export_parses_into_a_witness` | **按导出模型手写 IPC 计划并用 `parse_plan` 解析**——只有动作名/方法名/分解结构自洽才写得出来 |
| `test_the_witness_keeps_one_primitive_per_primitive_occurrence` | 见证里每个 primitive occurrence 都在（§8.3） |
| `test_the_stub_parser_verifies_the_exported_model` | **stub pandaPIparser 联调**：`PandaToolchain.verify_plan` 返回 `VERIFIED` 且带见证 |
| `test_the_adapter_reports_solver_unavailable_rather_than_a_pass` | 无二进制时 `SOLVER_UNAVAILABLE`，不冒充 PASS（§7.4 / C7） |

### 应改 2：取证不消耗燃料 —— 已补测试

审阅指出变异"取证也扣燃料"在 297 条下逃逸。补三条：`test_gathering_evidence_costs_no_recursion_fuel`（UNKNOWN 前提走取证路径后剩余燃料不变）、`test_a_leaf_that_is_ready_costs_no_fuel_either`、`test_a_compound_with_no_applicable_method_costs_no_fuel`。本轮变异表第 22 条用真正的 `consume_fuel` 注入，被捕获。

### 应改 3：`_check_single_port_bindings` 不可达 —— 已删除并改正 journal

确认不可达：`data_flows` 以步骤参数名作 `input_port`，dict 键唯一，同一方法内不可能出现重复。**选择删除**，并在原处留下注释说明真正管这件事的两层：`ProposedPlanDelta.__post_init__` 管一个 delta 内的重复绑定，`validate_execution_projection` 的 `SINGLE_PORT_OVERBOUND` 管跨 delta 往同一端口再绑一次。两层各补一条测试（`test_the_contract_refuses_two_bindings_into_one_input_port`、`test_a_second_delta_binding_a_filled_port_is_reported_by_the_projection`）。上文 §2「单值端口重复绑定在编译期就被拒」的表述据此改正为：**由契约与投影校验两层负责，编译器不再重复一遍**。

### 应改 4：新义务无人开户 —— 按契约第六轮实现

- `compiler._new_obligations` 为每个**派生出新义务**的槽生成 `ObligationOpening`，放进 `delta.obligation_openings`，同时留在 `RefinementCompilation.new_obligations`。`refines_parent` 的槽沿用父义务、不产生 opening——这正是"拆任务不铸造重试额度"与"递归燃料能约束深度"的同一条规则。
- `INDEPENDENT_AUTHORIZED` 必须由调用方通过 `slot_authorizations` 提供 `authorization_ref`，否则编译期按槽名拒绝（"planning alone does not create responsibility"）。默认 `INHERIT_PARENT_FUEL_SHARE`，`fuel_share = DEFAULT_CHILD_FUEL_SHARE = 1`；调用方给 `slot_grants` 才转 `SEPARATE_GRANT`。**细化槽给 grant 直接拒绝**（在 `continue` 之前检查，否则那条分支没有 opening 可挂）。
- `compiler.apply_obligation_openings(ledger, delta)` 逐条按父账户 `open_from`；`_check_obligations_accounted` 在编译期校验"每个子义务要么是父的、要么被本增量开出"。
- `refine` 新增 `RefinementOutcome.OBLIGATION_NOT_OPENED`：遇到没开户的义务时按结果返回并指出修法，而不是在账本里抛 `ContractError`。
- 补 17 条测试：细化槽不开义务 / 独立槽开一条 / opening 上 delta / 记录授权 / 继承份额不凭空造预算 / 无授权被拒（断言编译器自己的措辞，不是契约兜底的） / 指定 grant 转 SEPARATE_GRANT / 细化槽不得要 grant / 契约拒绝"细化 + 独立拨款" / 契约拒绝"无授权的独立义务" / 开户把燃料从父账划走 / **新义务子目标可继续 refine** / 未开户时 refine 明说 / 独立槽不把父的门 / 重复开户被拒 / `require_commit_ready` 拒绝未开户 occurrence / 带 openings 时放行。

### 可选项 —— 三条全做

- `_compile_coverage` 只算一次（原先编译一次算两遍），并顺手删掉它未使用的 `draft` 参数；
- `MethodRegistry` 加 `_by_goal_type` / `_by_goal_type_id` 两个索引，`candidates_for` 不再扫描全部定义；
- `retrievable` 内对 `mission_id` 做 `mission_ref()` 归一，并注明理由（拿未校验的字符串去比对已校验的 trial scope，空白或控制字符会静默不匹配）。

### mypy 三条

`compiler.py` 180/221 缺 `TypedRef` import、912 把 `TypedRef` 传给 `ReadItem.id`（该字段是 `str`）——改为 `plan.acceptance_ref.id` / `.revision` / `.content_hash`，`mypy src/agent_orchestrator` 回到 **17 条基线**（全部是 evaluation/runtime 的 `import-not-found`）。

## 2. 修复后门槛

| 门槛 | 结果 |
|---|---|
| 本片测试 | **340 passed**（69 + 40 + 64 + 80 + 87；修复前 297） |
| `tests/orchestrator/full_target` | 1493 passed / 1 skipped |
| 编排范围 `tests/orchestrator`（排除 gap_phase1） | 3459 passed / 21 skipped / **1 failed = 已知 p33 Python 3.14 历史 AST hash 断言** |
| `ruff check` / `ruff format --check` | 全通过 |
| `mypy src/agent_orchestrator` | 17 errors（基线，全在 evaluation/runtime 的缺失第三方 stub） |
| 变异自证 | **27 条全部被捕获**（修复前第一轮 14 条；本轮新增 13 条，含 HDDL 三条、义务开户三条、步骤复用两条、取证燃料一条） |

变异表新增部分：步骤声明不可共享副作用的复用 / 忽略步骤自己的复用策略 / 丢掉复用槽的 Acceptance / 不发 obligation opening / 细化槽可要 grant / 独立义务无授权也开 / draft 与目标参数不符也放行 / 取证扣燃料 / refine 未开户义务 / 导出把全部 occurrence 塞进初始网络 / 导出不给 primitive 发 method / 导出未细化 compound / 跳过前提复检。

第一轮曾有 5 条逃逸（步骤复用两条、Acceptance 一条、取证燃料一条、独立义务授权一条），逐条补测试后全部转为捕获；其中"独立义务无授权"最初是等价变异（契约兜底给出同样的措辞），把测试改为断言**编译器自己的**措辞后成为真变异。

## 3. 剩余契约观察（新增 1 条，交 P1.1）

**CR#7：`TypedRefKind` 没有 `authority` 成员。** `ObligationOpening.authorization_ref` 是 `TypedRef`，语义上指向"授权这件新责任的决定"。当前最接近的是 `review`（一次被记录的人工/系统决定），测试夹具即用它。建议加 `AUTHORITY = "authority"`，否则授权引用与审阅记录在类型上无法区分。

## 4. 修复轮的偏差

- **`refines_parent` 槽仍然不产生 `ObligationOpening`**。协调者写的是"对 independent_authorized / refines_parent 派生的新义务生成 opening"——在本片的身份派生里，`refines_parent` 槽**不派生**新义务（它就是父义务），所以没有 opening 可发。这是 §6.1 的直接后果，也是递归燃料能约束深度的前提；如果将来要让某个 `refines_parent` 的 compound 子目标单独记账，那是一次显式的政策变更，需要新的输入而不是默认行为。`ObligationOpening` 允许 `relation=REFINES_PARENT`（且强制 `INHERIT_PARENT_FUEL_SHARE`），本片的构造器保留了这条路径，只是当前没有槽会走到它。
- **`to_hddl` 的动作是占位语义**：`:precondition ()`、`:effect (done ?o)`。本片没有把方法的 `expected_effects` 编译成 STRIPS 效果——那需要把谓词条件映射到 HDDL 谓词，是一件独立的、有它自己的不支持子集要判定的工作。当前导出的层次结构（任务、方法、分解、部分序、复用）是真实的，状态语义是占位的，docstring 与本条都写明了这一点，P2.2 的见证校验消费的正是前者。
