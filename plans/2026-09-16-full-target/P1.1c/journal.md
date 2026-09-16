# P1.1c 实施记录（理由维护纯规则：多组支持、带极性正规则最小不动点）

日期：2026-09-16｜基线：SDK main `873fd4a`（工作区含 P1.1/P1.1b/P2.2 未提交产物）｜执行：单个 Opus 子代理｜测试先行（写完实现后用变异注入反证红测试有效性，见 §5）

规范来源：
主计划 `simple_harness/plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`
§6.6（(t,f) 只用于单命题合并、C28 不做 negation-as-failure）、§11.2–11.5、§25.1 裁决 5/6/7；
附件 `annex/aer-1.0/design.zh-CN.md` §8.1–8.3、§9.1–9.3、§10.3、§10.4；
附件 `annex/aer-1.0/reference/protocol_rules.py` 的 `grounded_closure` / `truth_for`（作为红测试起点，未复制实现）；
附件 `acceptance-scenarios.json` 的 K 组 AER-K01…K12。

## 1. 交付清单

| 文件 | 行数 | 内容 |
|---|---:|---|
| `src/agent_orchestrator/knowledge/justifications.py`（新建） | 1252 | 见下 |
| `tests/orchestrator/full_target/test_justifications.py`（新建） | 1144 | 89 条红测试 |
| `src/agent_orchestrator/knowledge/__init__.py`（追加导出） | +35 | 再导出 `justifications` / `predicates` 两个子模块与 12 个高频名字 |

生产模块的类型与函数：

- **极性与原子**：`Polarity`（POSITIVE/NEGATIVE，`parse_polarity` 接受 `+`/`-`/bool/枚举名）、`Atom(key, polarity)`。不动点在「带极性的原子」上推进，正负两条闭包各自独立，最后才合成四态——这是「反证不被正支持数量冲淡」的结构性来源。
- **前提**：`PropositionPremise(atom)` 与 `EvidencePremise(ref: EvidenceRef)`，`parse_premise` 把任务书要求的 `PropositionKey | EvidenceRef` 规范化进这两个节点（也接受 `Atom` 与 JSON 形态）。
- **理由集合**：`JustificationSet(conclusion, polarity, premises, conditions, rule_version, receipt_ref, source_group)`；`signature` = 全字段规范 JSON 的 SHA-256；无前提直接拒绝（观察才是显式 anchor）。`RuleCondition(name, defeasible)` 表达 AER §9.2 的「显式可撤销假设」。
- **支持图**：`SupportGraph`（不可变；`__setattr__`/`__delattr__` 抛 `ContractError`；结论原子 → 多个 `JustificationSet`，组内 AND、组间 OR；同签名只收一次；入图时跑 `reject_executable`）。提供 `groups_for / justifications / signatures / conclusions / with_justifications / without_signature`。
- **锚选择**：`AnchorCandidate`（观察 + scope/来源群组/可读性/有效性/时间语义/监控间隔/可回溯/`predicted`/`inapplicable(+receipt)`）、`AnchorSelector(scope_id, purpose, as_of_ms, signatures)`、`Anchor`、`RejectedAnchor`、`AnchorRejection`（13 个理由码）、`AnchorSelection`。
- **闭包**：`grounded_closure(graph, anchors, *, node_budget=10_000, assumptions=None) -> ClosureResult`；`ClosureStatus`、`WitnessKind`、`SupportWitness`、`ClosureResult`（`support_for/truth_for/witnesses_for/independent_source_groups/satisfies_independence/rests_on_assumption/may_release/release_block_reason/require_complete`）。
- **五分类重求值**：`ConsumerUse`、`reevaluate_consumer(before, after) -> RecheckOutcome`（复用 P1.1 已有的 `RecheckOutcome` 枚举，不另起一套）。
- **血缘**：`LineageRecord`（`was_used` 只追加、`supports_for_use` 可改选，两者互不改写；`adopt_history_as_support` / `rewrite_history_from_supports` 只存在于「抛 `ContractError`」）。

## 2. 与规范的关键对应

| 规范条目 | 落点 |
|---|---|
| AER §9.2 步骤 1（按 scope/purpose/as_of 选合法可回溯 anchor） | `AnchorSelector._verdict` 的固定检查顺序：`PREDICTED → SUPERSEDED → SCOPE → 可读 → 有效 → 可回溯 → 谓词已注册 → as_of → 时效窗口 → 连续保证 → 负观察权威性` |
| AER §9.2 步骤 2（派生支持全部未证明，不沿用上次 TRUE） | `grounded_closure` 只接受 `Anchor`（`AnchorSelector.select` 的唯一产物）；传入 `SupportWitness` 或 `Atom` 抛 `ContractError`。函数本身无状态、无「上一轮结果」入参 |
| AER §9.2 步骤 3–4（worklist 推进、每个签名最多一次、超限 `EVALUATION_INCOMPLETE`） | `pending/fired/remaining` 轮次工作表；`admitted` 集合按签名去重；`nodes >= budget` 即置 incomplete 并中断 |
| AER §9.2 步骤 5（正负分别闭包并保存实际见证） | `ClosureResult.witnesses` 以 `Atom` 为键；`SupportWitness` 记 anchor_id 或规则签名、`rule_version`、实际用到的前提、深度、用到的可撤销假设 |
| §6.6 C28 / AER §9.2（不做 negation-as-failure） | 条件未被显式断言即不满足（`state.get(name) is not True`），不存在「未推出即为真/为假」的默认；封闭域缺失只走权威负观察一条路 |
| §6.6 C28（封闭域负观察三要件） | `_negative_verdict`：必须 `is_authoritative_negative`（覆盖=AUTHORITATIVE_WITH_SCOPE + 水位 + 覆盖范围）、`coverage_scope` 与本次 scope 一致；CLOSED 域再加「观察者须在 `PredicateSignature.observer_ids` 内」 |
| AER §9.1（组间 OR、记录实际见证路径） | 同一结论的多组理由各留一条 `SupportWitness`；`rule_version` 可区分是哪组firing |
| AER §9.1（来源群组，不按数量计票） | `ClosureResult.support_paths` 给出每条见证路径的来源集合；`satisfies_independence(required=n)` 判定「存在 n 条两两来源不相交的路径」，既不按见证条数也不按来源名字个数（见 §8 修复 3） |
| AER §9.1（expected_effects 不能作 anchor） | `AnchorCandidate.predicted=True` → `PREDICTED_NOT_OBSERVED` |
| AER §9.3（新支持不消除仍有效的反证） | 闭包不做任何「正支持覆盖负支持」的逻辑；`(t>0, f>0)` 恒为 CONFLICT，直到反证被撤回（`Validity.REVOKED`）或带回执声明不适用（`inapplicable_receipt`） |
| AER §9.3（`was_used` ≠ `supports_for_use`） | `LineageRecord` 两个隔间 + 两个只会抛错的跨界方法 |
| AER §10.3 / §11.5（五分类） | `reevaluate_consumer` 的判定顺序即语义（见 §3 偏差 3） |
| AER §10.4（时间语义） | `HISTORICAL_AS_OF` 的过期证据只在历史用途（`HISTORICAL_PURPOSES = {CONTEXT}`）下仍可作 anchor 且标 `historical=True`；`CURRENT_AT_USE` 一律查新鲜度；`CONTINUOUS` 在 MAINTAIN 用途下必须带 `monitor_interval_ms`；可读性检查在所有用途之前，历史分析不豁免权限 |
| §11.5（`EVALUATION_INCOMPLETE` 阻止放行，不解释为 UNKNOWN） | `release_block_reason` 在 `status != COMPLETE` 时返回 `EVALUATION_INCOMPLETE` 而非 `UNKNOWN_EVIDENCE`；`require_complete()` 抛 `ContractError` |
| §9.2（高风险门不采信未验证假设） | 凡由调用方断言满足的条件都给支持打标（与 `defeasible` 无关，见 §8 修复 1）；`may_release(..., high_risk=True)` 返回 False、理由 `UNVERIFIED_ASSUMPTION`；若同结论另有一条干净见证则不再标记 |

任务书点名的五个循环案例全部立测并通过：`A←B, B←A` 无 anchor 无支持；`A←(A AND E)` 不能自启动；`A←E, B←A, A←B` 可由 E 合法导出；删 E 后重算归零；E1/E2 同 `source_group` 不满足两个独立来源。

## 3. 与规范/任务书的偏差及理由

1. **`SupportCount` 按见证条数计，而非 0/1 封顶**。AER §8.3 的表只列 0/1，但 `SupportCount.merge` 是累加语义，且任务书要求「输出每个命题的 SupportCount」。实现取 `t = 该原子的见证条数`、`f = 反极性见证条数`，于是 AER-K07 的场景真实产出 `(3, 1)` 并由 `SupportCount.truth` 判为 CONFLICT——正好把「不被数量冲淡」测成了非平凡断言；若封顶为 (1,1) 该测试就退化成恒真。四态映射与 §8.3 完全一致（t>0 且 f>0 → CONFLICT）。
2. **`JustificationSet.conditions` 取 `RuleCondition(name, defeasible)` 而非裸字符串**，并给 `grounded_closure` 增加 `assumptions: Mapping[str, bool]` 关键字参数。任务书的函数签名是 `grounded_closure(graph, anchors, *, node_budget)`，这是在其上增加一个可选关键字，默认 `None` 时行为不变。理由：AER §9.2 明确允许「一个显式可撤销假设」作为声明缺席的第二条路径，同时要求「高风险门不采信未验证假设」；没有条件状态入口就无法把这条规则实现成可测的东西。未断言的条件一律不满足，因此这个入口不会成为 negation-as-failure 的后门。
3. **五分类的判定顺序是自定的**（规范只给了五个类别的语义，没给优先级）。实现取：
   `不可读 → UNAVAILABLE`（权限缺失永远不断言内容错误，优先于一切）
   → `要求本身变了 → INVALID`
   → `有效性非 CURRENT 时把真值降级为 UNKNOWN`（沿用 `EvidenceEntry.truth` 的既有语义，不翻转）
   → `FALSE/CONFLICT → INVALID`
   → `UNKNOWN → NEEDS_REVIEW`
   → TRUE 之下：`输入/引用变了 → NEEDS_REVIEW` → `字面引用被撤回且准则要求引用正确 → NEEDS_REVIEW` → `支持集合未变 → UNCHANGED` → `准则不允许改绑 → NEEDS_REVIEW` → 否则 `REBOUND_SUPPORT`。
   其中把 CONFLICT 归入 INVALID 而非 NEEDS_REVIEW，依据是 §10.3 的 INVALID 定义「关键前提被反证」——CONFLICT 的前提是存在一条真实反证。已在函数 docstring 写明。
4. **`AnchorCandidate` 带 `observer_id` 字段**。`ObservationRecord` 只有 `source_ref` 与 `observer_version`，没有可与 `PredicateSignature.observer_ids` 比对的观察者身份。为不改只读的 `contracts/`，把 `observer_id` 放在 P1.1c 自己的候选包装类型上。**这构成一条契约变更请求**，见 §6。
5. **`HISTORICAL_PURPOSES` 目前只含 `WitnessPurpose.CONTEXT`**。AER §10.4 只说「历史查询」可继续使用 `HISTORICAL_AS_OF` 证据，没有枚举哪些 purpose 算历史查询。选 CONTEXT 是因为七个用途里只有它是纯读取/编译上下文；`RECOVERY` 会触发真实修复动作，故不列入。这是一个模块常量，后续若规范细化直接改这一处。
6. **`release_block_reason` 返回字符串常量而不是枚举**。它是给上层门（P1.1b 的验收表达式、P3.5 的 witness）拼装 `reason_codes` 用的，`ValidityWitness.reason_codes` 本身就是字符串序列；先不引入第二套枚举，等 P3.5 接线时再看是否需要收敛。
7. **`node_budget` 计的是「被接纳的支持节点数（anchor + 派生见证）」**，规范只说「部署计算限制」。选这个计数口径是因为它同时给 anchor 播种与规则触发设上界，且与「每个签名最多加入一次」同尺度；默认 `DEFAULT_NODE_BUDGET = 10_000`（与附件 reference 的 `max_atoms` 同量级）。超限时返回的 `ClosureResult` 仍带已算出的部分支持（便于诊断），但 `may_release` 对任何命题一律为 False。

## 4. 未做的事项（任务书明确划出，留给后续）

- epoch 屏障、durable dirty 队列、`VALIDITY_RECHECK_PENDING`、落库 → P3.5 / P3.6。本模块零 store / 零 commit import（已 grep 验证）。
- 反向支持索引（AER §10.1 的 `Evidence → Justification → Claim → …` typed reverse-use）→ P3.6。
- AER-K08（异步传播窗口）、K09（过期事件延迟）、K12（隐藏证据与权限）→ 需要 epoch/时钟/权限投影，不在纯规则范围内。K10 只做了「anchor 选择」这一半，「START 前提被合法消耗后不倒推非法」属于方法实例侧（P1.1 的 `recheck_method_instance` 与 P3.5）。
- `SupportSetRead` / `SemanticReadSet` 只作为输入阅读，本片未产出 read-set；把闭包结果投影成 `support_sets`（含 `member_digest`）是 P3.5 的接线工作，`ClosureResult.admitted_signatures` 已经是那个 digest 的天然素材。

## 5. 验证结果

```
# 新测试
uv run --frozen --group dev --extra local-capacity pytest \
  tests/orchestrator/full_target/test_justifications.py -q
→ 89 passed

# 编排 full_target 范围（P1.1 + P1.1b + P2.2 + 本片）
uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target -q \
  --ignore=tests/orchestrator/full_target/test_projection_integrity.py
→ 415 passed, 1 skipped
  （`test_projection_integrity.py` 是并行的 P2.1 子代理在写的文件，
    import 的 `graph/projection_validation.py` 尚未落地，属于其在途状态，与本片无关）

# lint / format / 类型
uv run --frozen --group dev ruff check src/agent_orchestrator/knowledge \
  tests/orchestrator/full_target/test_justifications.py            → All checks passed
uv run --frozen --group dev ruff format --check <同上>              → 4 files already formatted
uv run --frozen --group dev mypy src/agent_orchestrator             → justifications.py / knowledge/ 零错误
  （余下 17 个错误全部是既有的可选依赖缺失：evaluation/are_benchmark.py 的 pydantic、
    are.simulation.types，runtime/deepseek_tokens.py 的 deepseek_recipe）

# 隔离门槛
grep -iE "import .*(storage|commit|scheduling|artifacts)" knowledge/justifications.py → 零命中
git status --short → 只新增 knowledge/justifications.py、tests/.../test_justifications.py、
                     plans/2026-09-16-full-target/P1.1c/，并追加 knowledge/__init__.py 的导出；
                     未触碰 contracts/、verification/、graph/
```

**红测试有效性验证（变异注入）**：由于实现与测试在同一片内完成，用 7 次定向变异反证测试不是恒真的，每次改完即还原：

| 变异 | 失败数 |
|---|---:|
| M1 未断言的条件视为成立（引入 negation-as-failure） | 1 |
| M2 忽略 anchor 播种阶段的 node_budget | 3 |
| M3 接受非权威负观察作 f=1 anchor | 1 |
| M4 `EVALUATION_INCOMPLETE` 按 UNKNOWN 上报 | 3 |
| M5 前提缺失时自动播种该原子（循环自证） | 9 |
| M6 来源独立性按见证条数而非群组数计 | 2 |
| M7 `adopt_history_as_support` 不再抛错 | 1 |

七条核心不变量各自都有测试兜住，无恒真测试。

**性能边界（实测，本机）**：闭包是「轮次重扫 pending 规则表」而不是反向索引驱动，深链场景是最坏情况——单链 N 个规则实测 N=500 → 0.02s、N=2000 → 0.27s、N=9000 → 5.00s（约 O(N²) 的规则扫描）。宽而浅的图不受影响。**`node_budget` 是节点数上限、不是时间上限**：9000 节点在默认预算 10000 之内，所以它不会触发 `EVALUATION_INCOMPLETE`，只是慢。真正的反向支持索引（AER §10.1）在 P3.5/P3.6 落地；在那之前，深链场景的部署应把 `node_budget` 按自己的时间预算调小。

## 6. 契约变更请求（`contracts/` 只读，留给另一审阅）

1. **`ObservationRecord` 建议增加 `observer_id: str | None`**。理由：`PredicateSignature.observer_ids` 声明了「谁有资格观察这个谓词」，而 CLOSED 域的负观察要求「完整覆盖的权威查询」，二者要对上必须有观察者身份。现有字段里 `source_ref` 是来源材料引用（同一来源可由不同观察者读取）、`observer_version` 是版本号，都不能承担身份比对。本片暂把该字段放在 `knowledge.justifications.AnchorCandidate` 上；若契约补齐，`AnchorSelector._negative_verdict` 应改读 `observation.observer_id`，`AnchorCandidate.observer_id` 随之删除。
2. **（弱）`ObservationRecord` 的「不适用/被撤回」回执无处安放**。AER §9.3 要求「新的观察/审阅明确反证不适用或被撤回，四态才离开 CONFLICT」。本片用 `AnchorCandidate.inapplicable + inapplicable_receipt`（无回执则构造即拒）表达，属选择器侧的策略输入；若未来希望这件事可持久化、可回放，建议在证据侧增加 `superseded_by: TypedRef | None`。不阻塞本片。

## 7. 交接提示

- 入口：`from agent_orchestrator.knowledge.justifications import AnchorSelector, SupportGraph, grounded_closure`；`agent_orchestrator.knowledge` 也再导出了这几个高频名字。
- 想让一条结论获得支持，唯一路径是「先用 `AnchorSelector.select` 拿到 `Anchor`，再喂给 `grounded_closure`」。`Anchor` 的构造器要求一个模块私有 token（`_SELECTOR_TOKEN`），手搓 `Anchor(...)` 直接抛 `ContractError`——但这是**防手滑的 API 约定，不是安全边界**：同进程内任何代码都能读到那个 token。跨进程/重启后 anchor 的真实性由 P3.5/P3.6 的存储兜底（从持久化观察重新选择，而不是信任传入对象）。`AnchorRejection` 的 13 条理由就是全部准入规则。
- 同理，`LineageRecord` 的 `was_used` 是**约定上的**只追加：`with_recorded_use` 只追加、`with_supports` 不碰历史、两个跨界方法只会抛错；但公共构造器 `LineageRecord(rec.artifact_ref, (), ...)` 仍能造出一条历史被清空的新记录。真正的 append-only 要由 P3.6 的存储层（只允许追加的血缘表 + 校验）保证，本片只保证「在正常 API 路径上不会互相改写」。
- 闭包每次从零重算是刻意的，不要加「增量复用上次结果」的捷径——那正是 AER-K06 要防的循环保活。需要增量时应在 P3.6 的反向索引层做「哪些 scope 需要重算」，而不是在不动点里保留状态。
- `release_block_reason` 是唯一该被门用的读法；`truth_for` 只是数据，`EVALUATION_INCOMPLETE` 时它返回的部分真值不得用于放行。

## 8. 审阅修复（2026-09-16，独立审阅结论「需修后合并」）

审阅意见 3 条必改 + 2 条建议，全部落地。测试从 89 条增至 101 条。

### 修复 1（中高）assumptions 后门：凡断言满足的条件一律打标

**问题**：`_premises_satisfied` 原本只在 `condition.defeasible=True` 时记 taint。于是 `defeasible=False` 的条件配 `assumptions={"x": True}` 实测得到 TRUE、`rests_on_assumption=False`、`may_release(high_risk=True)=True`——高风险门采信了一条未验证假设，`defeasible=False` 反而成了「把假设洗成已验证事实」的路径。

**修复**：`used.append(condition.name)` 无条件执行。`defeasible` 的语义收窄为「这条规则**允许不允许**依赖断言」，与「得到的支持算不算假设支持」解耦；taint 跟踪的是支持**怎么拿到的**，不是规则**怎么声明的**。`RuleCondition` 与 `_premises_satisfied` 的 docstring 均已写明这一区分。

**顺带（fail-closed）**：`may_release` 与 `release_block_reason` 的 `high_risk` 去掉默认值改为必填关键字。没说清自己是哪种门的调用方，不该默默拿到宽松的那一种。

**新增测试**：`test_a_non_defeasible_condition_met_by_assertion_is_still_assumed_support`、`test_a_high_risk_gate_refuses_support_resting_on_any_assertion`、`test_the_witness_names_every_assertion_it_leaned_on`、`test_a_release_gate_must_say_which_kind_of_gate_it_is`。变异回退（只有 defeasible 才打标）→ 3 条失败。

### 修复 2（中）`AnchorSelector.signatures` 改必填

**问题**：`signatures=None` 时 `_signature_for` 恒返回 None，CLOSED 域的 observer 校验被整体跳过，`observer_id=None` 的权威负观察被接纳。

**修复**：`signatures` 改为必填关键字，非 Mapping 抛 `ContractError` 并说明理由；谓词解析不到即 `UNREGISTERED_PREDICATE` 拒绝（原来只在 `signatures is not None` 时才拒）。`_negative_verdict` 的 `signature` 参数随之改为非可选——此处不再需要 `None` 分支，留着反而是不可达死代码（第一版修复留了这个分支，变异测试测不出差别，已删除）。另加 `AnchorSelector.registered()` 便于上层预检。

**新增测试**：`test_a_selector_without_a_registry_binding_is_refused`、`test_an_unbound_selector_cannot_admit_a_denial_by_default`、`test_a_denial_without_a_named_observer_is_refused_in_a_closed_domain`。变异（谓词解析不到时伪造一个签名继续）→ 2 条失败。

### 修复 3（中）独立性按见证路径而非来源名字个数

**问题**：`satisfies_independence` 原本比较「来源群组并集的大小」。于是 `C←(A AND B)`、A 来自 lab-a、B 来自 field-b 时，`required=2` 被判为满足——把**一条合取路径**算成了两个独立来源。AER §9.1 要的是「两条独立充分支持」。

**修复**：
- `ClosureResult` 新增 `path_groups` 字段与 `support_paths(key, polarity)` 读法：每条见证一个来源集合。
- `satisfies_independence(required=n)` 改判「存在 n 条两两来源不相交的路径」（`_max_disjoint_paths`，精确回溯 + `MAX_INDEPENDENCE_PATHS = 64` 硬上限，避免门被拖进无界 set packing）。
- 派生见证的路径来源取**各前提的来源并集**（而不是挑某一条子路径）。这是刻意的过近似：过近似让两条路径看起来更纠缠，独立性判定只会偏向拒绝、不会偏向放行。
- `independent_source_groups` 保留原语义（全部涉及来源的并集），docstring 明确它是展示用、不是政策判定。

**新增测试**：`test_one_conjunctive_path_is_one_reason_even_across_two_sources`（钉死审阅给的反例）、`test_two_source_disjoint_paths_do_satisfy_the_policy`、`test_two_paths_that_share_a_source_are_not_independent`。变异回退（按来源名字个数计）→ 1 条失败。原有的 K03 三条（10 份转述同一来源、两个真独立来源、派生继承来源）在新语义下仍全绿。

### 修复 4（建议）`Anchor` 加 selector-only token

`Anchor` 增加 `token: object`（`repr=False, compare=False`），构造时必须等于模块私有的 `_SELECTOR_TOKEN`，否则抛 `ContractError`；只有 `AnchorSelector.select` 传它。类 docstring 与 journal §7 同时写明：**这是防手滑不是安全边界**，同进程能读到 token，anchor 跨重启的真实性由 P3.5/P3.6 从持久化观察重新选择来保证。`LineageRecord` 的 append-only 同样改述为 API 约定，存储层兜底。

**新增测试**：`test_an_anchor_cannot_be_assembled_by_hand`、`test_a_forged_anchor_cannot_be_smuggled_past_the_closure`。变异（不校验 token）→ 1 条失败。

### 修复 5（建议）性能边界记入 §5

实测写入 §5：单链 N=500/2000/9000 → 0.02s/0.27s/5.00s；`node_budget` 是节点上限不是时间上限；反向索引留 P3.5。

### 保留项

`AnchorCandidate.observer_id` 按协调者指示保留。**迁移说明**：待 P1.1 持有者给 `ObservationRecord` 补上 `observer_id` 后，`AnchorSelector._negative_verdict` 改读 `observation.observer_id`，删除 `AnchorCandidate.observer_id` 字段与其在 `__post_init__` 的校验，测试 helper `candidate(..., observer_id=...)` 改为传进 `observation(...)`。契约变更请求原文见 §6。

### 修复后门槛

```
uv run --frozen --group dev --extra local-capacity pytest \
  tests/orchestrator/full_target/test_justifications.py -q          → 101 passed
uv run --frozen --group dev ruff check src/agent_orchestrator/knowledge \
  tests/orchestrator/full_target/test_justifications.py             → All checks passed
uv run --frozen --group dev ruff format --check <同上>               → 4 files already formatted
uv run --frozen --group dev mypy src/agent_orchestrator              → justifications.py 零错误
                                                                       （余 17 个仍是既有可选依赖缺失）
未提交；未跑全量回归；未改 contracts/、verification/、graph/。
```
