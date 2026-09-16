# P2.2b 实施记录（DataRequirement → BoundInput → InputManifest 纯解析）

日期：2026-09-16｜基线：SDK main `873fd4a`（工作区含 P1.1 / P1.1b / P1.1c / P2.1b / P2.2 未提交产物）｜执行：单个 Opus 子代理｜实际耗时：约 55 分钟（规范阅读约 20 分钟，实现与测试约 25 分钟，变异与门槛约 10 分钟）

规范来源：
主计划 `simple_harness/plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`
§18.2（`artifacts/versioning.py` 行：legacy `merge_accepted`/`collect_upstream_inputs` 逐字节不变）、§23（P2.2b 行：交付 `artifacts/input_bindings.py`、红测试 `test_input_manifest_resolution`、门槛「`versioning.py` 零 diff」）、§24.1 裁决 3 与 4、§24.2（文件名定案 `artifacts/input_bindings.py`；「覆盖语义的准确描述」）；
附件 `annex/simpleharness-taskgraph-design.zh-CN.md` §4.3（DATA 端口与版本）、§10.1–10.3（产物组装必须同时修改）；
附件 `annex/simpleharness-taskgraph-implementation-design.zh-CN.md` §4.3（`ResolvedInputBinding` 字段表、冻结 dispatch intent 必须解析为精确身份）。

## 1. 交付清单

新增两个文件，**未改动任何既有文件**：

| 文件 | 行数 | 内容 |
|---|---:|---|
| `src/agent_orchestrator/artifacts/input_bindings.py` | 1238 | 见下 |
| `tests/orchestrator/full_target/test_input_manifest_resolution.py` | 1503 | 92 条测试 |

`input_bindings.py` 的公开面：

- 词汇：`DisclosureState`(4)、`ResolutionProblemKind`(15)、`ResolutionProblem`、`ManifestNotFrozen`
- 身份：`ResourceIdentity`（namespace + 规范化 path，`is_escaping`、`collation_key(case_insensitive=)`）
- 输入：`AcceptedOutput`、`AcceptedOutputsIndex`（`at` / `is_complete` / `authorized_revision`）、`SchemaCompatibilityRule`、`SchemaCompatibilityRegistry`、`ExplicitPortOrder`、`ResolutionPolicy`
- 输出：`ResolvedInputBinding`、`SymbolicBinding`、`InputManifest`、`ResolutionResult`
- 函数：`resolve_declared_inputs`、`materialise_plan`（`TargetRules` / `MaterialisationEntry` / `MaterialisationPlan`）、`explain`（`Explanation`）

全部是 frozen dataclass 与纯函数。模块只 import `..contracts.{evidence_state,htn,models,semantic_base}` 与同目录 `.paths`；不 import `storage/`、`scheduling/`、`orchestrator/`、`graph/`，也不 import `.versioning` / `.workspace` / `.store`。测试里有两条源码扫描用例把这条钉死（`test_the_module_does_not_import_the_forbidden_layers`、`test_the_module_never_reads_the_filesystem`）。

## 2. 与规范的关键对应

- **DataRequirement 是约定，BoundInput 是解析结果（TG §4.3）**：`resolve_declared_inputs` 只读 `requirements` 与 `AcceptedOutputsIndex`，产出 `InputManifest`。`ResolvedInputBinding` 冻结的是实现稿 §4.3 列出的全部精确身份：`binding_id`、`producer_task_ref` + `output_port`、`producer_result_id`、`acceptance_id` + `support_revision`、`artifact_id` + `content_hash`、`consumer_task_ref` + `input_port`、`read_policy` / `freshness_policy` / `disclosure_scope`。测试 `test_binding_freezes_the_exact_identity_rather_than_latest` 逐字段断言，落实「不能把 latest 留给 Worker」。
- **单值端口唯一 binding**：两个候选（同一 producer 的两次 attempt，或两条 DataRequirement 指向同一 input port）都报 `AMBIGUOUS_SINGLE_PORT`，`manifest` 为 `None`。`test_ambiguous_single_port_never_prefers_the_later_producer` 特意给一个候选 `producer_ordinal=99`，断言两个候选都被列进 `problem.candidates`——代码里没有任何「拓扑更靠后者胜」的分支。
- **集合端口必须有序**：直接用 P1.1 本片开工后刚落地的 `PortSpec.ordering`（`PortOrdering.BY_PRODUCER_ORDINAL` / `BY_KEY` / `EXPLICIT`）与 `PortSpec.order_key`。未声明 `ordering` 的集合端口 → `SET_PORT_UNORDERED`；声明了但排不出全序（BY_KEY 缺键 / 键重复、BY_PRODUCER_ORDINAL 序号打平、EXPLICIT 漏项或含未知项）→ `SET_PORT_ORDER_INCOMPLETE`。没有任何兜底顺序，「谁最后写谁赢」在代码里不存在。
- **schema 精确匹配或注册兼容声明**：匹配条件是 `VersionedRef` 三元组（id + version + content_hash）完全相等，否则查 `SchemaCompatibilityRegistry` 的**有向**规则（`produced → required`），命中则把 `converter_ref` 记进 binding，否则 `SCHEMA_MISMATCH`。测试覆盖了「同 id 同 version 但 content_hash 变了 → 仍是 mismatch」「反向声明不生效」「无关 schema 不因为无法证伪就放行」三种不推含义的场景。另外 requirement 的 `schema_ref` 必须与端口声明一致，否则也是 `SCHEMA_MISMATCH`（约定不能和端口互相矛盾）。
- **PINNED vs FOLLOW_AUTHORIZED_REVISION**：`PINNED` 取 `policy.pinned_revisions[requirement_id]`，索引里出现更新的 `r2` 也不动；`FOLLOW_AUTHORIZED_REVISION` 取 `AcceptedOutputsIndex.authorized_revision(producer, port)`，并在「当前授权版本 ≠ 已固定版本」时把 `requires_reacceptance=True` 写进 binding（变化触发相关重新验收）。pin 指向的版本已不在已接受集合 → `REVISION_NOT_AVAILABLE`，不静默回退到别的版本。
- **撤权 / 删除 / 用途限制仍需当前检查**：`DisclosureState` 的三个非 `DISCLOSABLE` 值都进 `NOT_DISCLOSABLE`，detail 里带具体状态。顺序上披露检查在版本筛选**之后**，所以 `test_pinned_does_not_buy_a_way_past_revocation` 证明 PINNED 不是绕过许可的理由。
- **witness**：`decision` 非 `USABLE` → `WITNESS_NOT_USABLE`；`decision` 是 `USABLE` 但 `is_fresh_for(now_ms, current_scope_epoch)` 为假（scope epoch 被抬高，或 `not_after_ms` 已过）→ 同样 `WITNESS_NOT_USABLE`，detail 标 `stale`（落实 I19：缓存的 TRUE 不是令牌）；完全没有 witness → `WITNESS_MISSING`（`policy.require_witness=False` 可显式豁免，binding 的 `witness_id` 记 `None`）。
- **生产者未完成 → 保留符号绑定**：`AcceptedOutputsIndex.completed_producers` 把「没产出」和「没跑完」分开。未完成的 requirement 变成 `SymbolicBinding` 进 `manifest.pending`，同时报 `PENDING_PRODUCER`。`PENDING_PRODUCER` 是**唯一不阻断 manifest 生成**的问题种类：manifest 会返回，但 `is_frozen` 为假，`manifest_hash()` 与 `materialise_plan()` 都抛 `ManifestNotFrozen`——「冻结 dispatch intent 时必须解析为精确身份」在类型层面成立，而不是靠调用方自觉。
- **provisional 明确标记且只在获准范围内**：provisional 候选只能绑到 `policy.provisional_ports` 列出的端口，否则 `PROVISIONAL_NOT_AUTHORIZED`。绑上了也带 `provisional=True`，并且 `InputManifest.required_ports_satisfied(consumer)` 对「必需端口只有 provisional binding」返回 `False`——推测执行可以跑，但不能对外宣称已满足必需 DATA。
- **ORDER-only 前置绝不进 manifest（TG §10.1，T015/T066 纯部分）**：函数只看 `requirements`。三条用例分别证明：ORDER-only 前置有已接受产物时 manifest 为空；DATA + ORDER-only 混合时只有 DATA 那条进来；索引里有 6 个祖先产物而只有 1 条 requirement 时 manifest 只有 1 条（旧的「汇入所有祖先」语义在这条路径上不存在）。
- **资源身份 =（namespace, normalized-path）（TG §10.2）**：`ResourceIdentity` 复用既有 `artifacts/paths.py:normalise_workspace_path`（反斜杠→斜杠、去 `./` 与多余分隔符与前导斜杠、**保留 `..` 原样**），`..` 由 `materialise_plan` 报 `TARGET_PATH_INVALID`，与 workspace 解析器「看得见才能拒绝」的既有约定一致。`attempt-A/report.md` 与 `attempt-B/report.md` 是两个 `ResourceIdentity`，在集合端口上是两条独立 binding。
- **同路径异 hash 必须显式解决，不合并**：`materialise_plan` 按 `(namespace, path)`（可选 case-fold）分组，组内出现两个不同 `content_hash` → `TARGET_PATH_CONFLICT`，**该目标位置整个不进 `entries`**（`test_the_conflicting_plan_does_not_pick_a_winner` 断言冲突路径不出现在计划里）。同一 hash 落同一位置不是冲突，合成一条 entry 并列出两个 `binding_ids`。`TargetRules.preserve_source_namespace=True` 则让两个 attempt 各自落到 `attempt-A/report.md`、`attempt-B/report.md`，不必为了独立探索而互相禁止。
- **只出计划，不写文件**：`materialise_plan` 返回 `MaterialisationPlan`，全程没有任何文件系统调用；`test_materialise_plan_writes_nothing_to_disk` 用 `tmp_path` 当 namespace 跑一遍再断言目录为空。
- **`explain` 给结构化原因**：每个 problem 一条 `Explanation`（kind / summary / remedy / input_port / requirement_ids / candidates），remedy 表覆盖全部 15 种 kind。`test_explain_never_proposes_an_automatic_winner` 断言歧义的 remedy 文本里既不出现 `topolog` 也不出现 `last`——建议永远是「显式选择或加综合任务」，不是一个可以顺手套用的默认值。

## 3. 测试与变异自证

- **红→绿**：先只提交测试文件，`pytest` 报 `ModuleNotFoundError: No module named 'agent_orchestrator.artifacts.input_bindings'`（红）；实现后 92 条全绿，`0.07s`。
- **条数**：92 条（含 3 组 parametrize 展开），分 12 节：单值端口 7、集合端口 11、schema 6、源版本策略 7、披露 5、witness 7、pending 与未绑定 9、ORDER-only 与端口声明 6、provisional 4、manifest hash 6、物化计划 16、explain 与纯度 8。

**变异自证（8 个变异，全部被杀）**：

| # | 变异 | 失败用例数 |
|---|---|---:|
| M1 | 单值端口多候选时取第一个，不报歧义 | 4 |
| M2 | 集合端口无 ordering 声明时按输入顺序兜底 | 1 |
| M3 | schema 只比 `id`（等于自作主张推含义） | 4 |
| M4 | `PINNED` 也跟随当前授权版本 | 2 |
| M5 | 跳过撤权 / 删除 / 用途限制检查 | 5 |
| M6 | 同路径异 hash 直接取第一个（静默覆盖） | 5 |
| M7 | `InputManifest` 不做规范排序（hash 与输入顺序相关） | 1 |
| M8 | witness 只看 `decision`，不校验 scope epoch 与过期 | 2 |

变异后逐条还原，还原文件与备份 `diff` 为空，再跑一遍 92 全绿。

## 4. 门槛

| 门槛 | 结果 |
|---|---|
| 本片测试 | 92 passed（`uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target/test_input_manifest_resolution.py -q`） |
| `ruff check` | All checks passed（两个新文件） |
| `ruff format --check` | 2 files already formatted |
| `mypy src/agent_orchestrator` | 20 errors，**没有一条出自 `input_bindings.py`**；20 条分别是 `evaluation/*` 的可选依赖缺失（既有基线，文件未改动）与 `graph/projection_validation.py`（并行子代理在途文件） |
| `git diff --stat` | 唯一被改的已跟踪文件是 `contracts/__init__.py`（P1.1 持有者的改动）；`artifacts/versioning.py`、`artifacts/workspace.py` **零 diff**（`git diff --stat -- ...` 输出为空），`input_bindings.py` 为新增未跟踪文件 |

另跑 `tests/orchestrator/full_target`（排除并行子代理正在重写的 `test_projection_integrity.py`）：550 passed / 41 failed / 1 skipped，41 条失败**全部**集中在 `test_acceptance_rules.py`（P1.1c 在途），与本片无关；本片 92 条全在 passed 里。按分派要求未跑全量回归、未提交。

## 5. 偏差与理由

### 5.1 `materialization_target` 不做 `ResolvedInputBinding` 的字段

实现稿 §4.3 的字段表列了 `materialization_target`。本片把它拆成两处：binding 上记 `source_identity`（产物**来源**的 `(namespace, normalized-path)`），目标位置由 `materialise_plan(manifest, target_rules)` 算出并放进 `MaterialisationEntry.target`。

理由：目标位置依赖消费者工作区的 `TargetRules`（namespace、端口前缀、是否保留来源命名空间、平台大小写规则），而 manifest 必须在知道这些规则之前就能冻结并算 hash（`Acceptance.input_manifest_hash` 引用的是**输入身份**，不是落盘布局）。把目标写进 binding 会让同一组输入在两台不同平台上得到两个不同的 manifest hash，等于把物化细节混进验收身份。目标位置是确定性的纯函数产物，任何时候都能由 `(manifest, target_rules)` 重算。

→ 见 §6 CR-2。

### 5.2 `EXPLICIT` 顺序的清单放在 policy，不在契约里

`PortSpec` 本片开工后已由 P1.1 补上 `ordering` 与 `order_key`（`BY_PRODUCER_ORDINAL` / `BY_KEY` / `EXPLICIT`），三种顺序按分派要求直接用契约枚举，未再造旁置枚举。但 `EXPLICIT` 需要一份「哪个 requirement 排第几」的清单，`PortSpec` 目前没有这个字段。本片用旁置的 `ExplicitPortOrder(input_port, ordered_requirement_ids)` 放进 `ResolutionPolicy.explicit_orders` 顶上，**待契约落地后切换**。

注：P2.1b 在 `graph/task_network.py` 里有同义的 `SetPortOrder`。本片没有 import 它——`artifacts/` 依赖 `graph/` 会造出一条不该有的层间依赖，而且那是并行子代理的在途文件。两者应在契约补上字段后一起收敛掉。

→ 见 §6 CR-1。

### 5.3 `TaskSemanticBindingV1` 的 `resource_reads` / `resource_writes` 未用到

分派说明提到 P1.1 可能新增这两个字段。截至本片完成时它们尚未落地，但本片也不需要它们：「用途限制」是产物侧的属性，由 `AcceptedOutput.disclosure = PURPOSE_RESTRICTED` 表达并进 `NOT_DISCLOSABLE`。字段落地后本片无需改动；它们属于 §6.6 的资源冲突分析（P2.1b 的 `RESOURCE_CONFLICT`），不属于输入解析。

### 5.4 签名多了一个关键字参数 `consumer_occurrence`

分派给的签名是 `resolve_declared_inputs(consumer, requirements, accepted, *, witnesses, policy)`。`consumer` 是 `TaskSemanticBindingV1`（按 TaskRef 标识），而 `DataRequirement.consumer_occurrence` 是 occurrence id，两者之间没有映射的话，一条指向**别的消费者**的 requirement 会被静默吸收成本任务的输入。本片加了可选关键字 `consumer_occurrence: OccurrenceId | None = None`：给了就校验，不匹配报 `FOREIGN_REQUIREMENT`；不给则退回「调用方已筛选好」的宽松模式。默认值保证这是纯加法。

### 5.5 `PINNED` 在没有既有 pin 时不强行挑一个

`PINNED` 且 `policy.pinned_revisions` 里还没有这条 requirement 时（首次解析），本片不按「最新」自动定版，而是让所有已接受版本都留作候选——于是多版本并存会正常触发 `AMBIGUOUS_SINGLE_PORT`，由调用方显式定版。这是保守方向：自动挑一个正是裁决 3/4 要禁止的行为。P2.3b 接线时，定版动作应由 Commit 写进 `pinned_revisions` 后再解析。

## 6. 契约变更请求

- **CR-1（`PortSpec` 的 EXPLICIT 顺序清单）**：`PortSpec.ordering = EXPLICIT` 目前无处安放「顺序清单」。建议给 `PortSpec` 增加 `explicit_order: tuple[str, ...] = ()`（元素为 requirement_id），并在 `__post_init__` 里要求 `ordering is EXPLICIT` 时非空、其余情形为空（与既有 `order_key` 的约束写法一致）。落地后 `ExplicitPortOrder` 与 `ResolutionPolicy.explicit_orders`、以及 `graph/task_network.py` 的 `SetPortOrder` 可一并删除。
- **CR-2（`input_manifest_hash` 的口径）**：`Acceptance.input_manifest_hash` 应明确定义为「`InputManifest` 中全部 `ResolvedInputBinding` 的规范 JSON 的 SHA-256」，**不含物化目标路径**，理由见 §5.1。本片的 `InputManifest.manifest_hash()` 即按此实现，并对未冻结的 manifest 抛 `ManifestNotFrozen`。
- **CR-3（`DisclosureState` 的归属）**：撤权 / 删除 / 用途限制三态目前是本模块的本地枚举。若 P1.1 或 AER 侧已有等价的产物可披露性表达（`Availability` 只覆盖读取可得性，不覆盖「被撤权」与「用途限制」），应统一到契约层，避免两套词汇。

## 7. 与 P2.3b 的交接

P2.3b 接线时需要的三件事：

1. `event_handler` / 派发路径按新模式调用 `resolve_declared_inputs` → `materialise_plan`，物化改由 `artifacts/versioning.py` 的新分支按 `MaterialisationPlan` 执行；`collect_upstream_inputs` / `merge_accepted` / `ArtifactConflict` 保持逐字节不变，两种模式按版本分流（§18.2、§24.1 裁决 4：旧 exact-path 保护在新路径接通前不得删）。
2. 定版动作（写 `pinned_revisions`）与授权版本（写 `AcceptedOutputsIndex.authorized_revisions`）需要来源：前者应由 Commit 在冻结 dispatch intent 时落库，后者来自当前授权的产物版本。见 §5.5。
3. 拓扑不完整时抛 `GraphIntegrityError`（裁决 11）属于 P2.3b 的物化路径，本模块不抛也不捕；本模块只在 manifest 未冻结时抛 `ManifestNotFrozen`。

---

## 8. 审阅修复（2026-09-16，独立审阅「需修后合并」后）

审阅结论：5/5 变异被杀、规范核对基本全过，但指出 8 项缺口 + 1 项签名问题。以下逐条处理，全部完成。基线：SDK `623d4c8`（batch 1 已提交）。本轮耗时约 35 分钟。

### 8.1 必改

**F1（高）witness 只看 `decision`，不看这份 witness 是不是给「本消费者、本用途、本支持版本」的。**

`_check_witness` 改为四道独立闸门，各自一个 problem kind：

| 闸门 | 判据 | 新 kind |
|---|---|---|
| 用途 | `witness.purpose ∈ policy.witness_purposes`（默认 `{START}`） | `WITNESS_PURPOSE_MISMATCH` |
| 消费者 | `consumer_ref.kind is TASK` 且 `consumer_ref.id == str(consumer.task_id)` | `WITNESS_CONSUMER_MISMATCH` |
| 支持版本 | `witness.support_revision == candidate.support_revision` | `WITNESS_SUPPORT_MISMATCH` |
| 决定 + 新鲜度 | 原有 `USABLE` + `is_fresh_for` | `WITNESS_NOT_USABLE` |

依据 §11.5 / AER §8.1：witness 是「一次、一个用途、一个结论」的使用许可。`purpose=PLAN` 不授权开工；发给别的 task 的许可是**那个** task 的许可；对 support revision 1 取的 witness 对 revision 99 什么也没说（等于拿旧读数给新证据背书）。
`policy.witness_purposes` 允许调用方显式放宽（`test_the_accepted_purposes_can_be_widened_deliberately`）。
新增 7 条测试（含 4 条 purpose 参数化）。审阅点破的「『witness 不校验用途』变异打不中」现在由 M9/M10/M11 三个变异各杀 4 条覆盖。

**F2（中）`scope_epochs` 缺该 scope 时回落到 witness 自带 epoch，`scope_epochs={}` 一律判新鲜，I19 失效。**

改为：`witness.scope_id not in policy.scope_epochs` → `WITNESS_EPOCH_UNKNOWN`，拒绝。I19 是一次**比较**，没有可比对象不是通过。放宽需调用方显式 `ResolutionPolicy(allow_unknown_scope=True)`，这时才回落到 witness 自带 epoch。新增 2 条测试（拒绝 / 显式放宽）。

**F3（中）契约已有 `BoundInput` 却未使用，身份字段有两套。**

`ResolvedInputBinding` 现在内嵌 `bound: BoundInput`（契约类型，`__post_init__` 校验类型），并提供 `as_bound_input()`。七个身份字段（`requirement_id` / `producer_result_id` / `acceptance_id` / `artifact_id` / `content_hash` / `schema_ref` / `source_revision`）从 dataclass 字段降级为**只读 property**，一律转发到 `self.bound`——身份只有一个家，调用方 API 不变。`to_json()` 里改为嵌套 `"bound_input"` 子对象（manifest hash 口径随之变化，本片尚未接线，无兼容负担）。其余字段是「身份周围的路由与政策」：谁在哪个 output port 产出、喂给哪个 input port、来源在哪、读取/新鲜度/披露条款。变异 M16（让 `artifact_id` 访问器返回 `bound.artifact_id + "-drift"`）杀 2 条。
本地 `ExplicitPortOrder` 按审阅要求保留，journal §5.2 与 §6 CR-1 已注明「待契约 `PortSpec.explicit_order` 落地后删除」。

### 8.2 应改

- **F4** `FOLLOW_AUTHORIZED_REVISION` 且从无 pin 时改为 `requires_reacceptance=True`。理由：首次绑定就是这个消费者第一次拿到这份输入的这个版本，它**没有**被针对该输入验收过；把「从无到有」当作中性起点，等于让第一版输入免验收混进来，而第二版反而要验收，方向是反的。`PINNED` 首次绑定仍为 `False`（PINNED 的语义是「明确研究历史版本」，定版动作本身就是一次决定）。新增 2 条测试。
- **F5** `case_insensitive=True` 时目标路径规范化为**折叠后的小写拼法**（`members[0][0].path.casefold()`），不再取「先遍历到的那个拼法」。写死为小写并测试；另加一条把两个拼法对调的用例，证明结果与绑定顺序无关。变异 M14 杀 2 条。
- **F6** 删除死代码 `touched: set[str]`（只写不读）与 `was_pinned`（per-port 元组的第 5 位，构造后从未使用）；per-port 元组由 5 元降为 4 元。
- **F7** `ResolutionProblem` 增加 `input_ports: tuple[str, ...]`（`__post_init__` 里把单数 `input_port` 自动折进来，其余调用点零改动）。`TARGET_PATH_CONFLICT` 改填全部涉及端口、不再只报第一个；`Explanation` 同步透传。新增 1 条跨两个端口撞同一路径的用例。
- **F8** 两条源码文本扫描测试改为 **AST 级 import 检查**：`module_imports()` 走 `ast.walk` 收集 `Import` / `ImportFrom` 的模块名，断言 ⊆ 10 项白名单，并单独断言不含 storage / scheduling / graph / `.versioning` / `.workspace` / `.store`，以及首段不在 `{os, io, pathlib, shutil, sqlite3}`。文本扫描既会被 docstring 里出现的词误伤，也抓不到 `importlib.import_module("os")`；import 节点才是这条规则真正管的东西。
- **签名** `consumer_occurrence` 由 `OccurrenceId | None = None` 改为**必填关键字** `consumer_occurrence: OccurrenceId`。审阅意见正确：默认 `None` 时 `FOREIGN_REQUIREMENT` 形同虚设。docstring 写明理由（`consumer` 按 TaskRef 标识而 requirement 按 occurrence 标识，缺了它就没有可比对象）。新增 1 条 `TypeError` 用例；变异 M17 杀 1 条。

### 8.3 修复后门槛

| 项 | 修复前 | 修复后 |
|---|---|---|
| 本片测试 | 92 passed | **111 passed**（+19） |
| problem kind 数 | 15 | 19（+4 个 witness 类） |
| 变异自证 | 8/8 被杀 | **17/17 被杀**（新增 M9–M17 覆盖 F1×3、F2、F3、F4、F5、F7 与必填 `consumer_occurrence`） |
| `ruff check` / `format --check` | 通过 | 通过 |
| `mypy src/agent_orchestrator` | 20 errors（0 条本片） | 17 errors（0 条本片；减少的 3 条是并行子代理修好的 `graph/projection_validation.py`） |
| `artifacts/versioning.py` / `workspace.py` | 零 diff | **零 diff**（`git diff --stat` 与 `git status --porcelain` 均为空） |
| `contracts/` | 只读 | 只读（`git status --porcelain -- contracts/` 为空） |

变异逐条还原后与备份 `diff` 为空，还原后 111 全绿。仍未提交、未跑全量回归。

### 8.4 本轮新增的规范对应

- §11.5「一次、一个用途、一个结论」→ F1 的 purpose / consumer / support_revision 三道闸门。
- 不变量 I19「比较 scope_epoch 与 not_after_ms 后才能复用缓存的 TRUE」→ F2：**缺少可比对象不是通过**，放宽必须显式署名。
- TG §4.3「变化触发相关重新验收」→ F4：首次绑定也是一次输入变化。
- TG §10.2「对大小写和平台规则做真实适配」→ F5：折叠平台上的规范拼法写死为小写，而不是取决于遍历顺序。
