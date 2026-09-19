# H1-D 实施日志 — 分层规划请求包的新协议增量（planner-package-hierarchical-v5）

**片名：** H1-D（V2 §18/§19/§38/§39 + 裁定补遗 §1/§7）
**分支：** `h1-d-planner-package-v4`
**开工基线：** `b13e757b74aff9987eebf796ad1b01a3e27b4808`（main，与 H1-A1 收官 commit 相同）
**工作目录：** `/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk-h1d`（git worktree；`pwd` 的 SDK 相对路径 `simple-runtime-sdk-h1d`）

---

## 1. 范围与文件

### 新增

| 路径 | 说明 |
|---|---|
| `tests/orchestrator/full_target/test_planning_decision_package_v4.py` | 本片测试（46 条），先红后绿 |

### 修改（仅一个文件）

| 路径 | 改什么 |
|---|---|
| `src/agent_orchestrator/planning/htn/planner_package.py` | 新增 `planning_protocol` 显式开关、五个协议字段、`visible_refs` 纯函数收集器与三个哈希函数；老协议路径逐字节不变 |

### 明确未改（白名单外，属其他片）

- `runtime/role_templates.py`（整数包版本 3→4、提示词 v8 属 H1-E）；
- `contracts/`（`method_instance` 枚举成员属 H1-A envelope 片）；
- `event_handler.py` 与派发路径（接线属 H1-F/H）。

---

## 2. 交付内容

### 2.1 显式开关（§38、补遗 §7.1）

`hierarchical_planner_package(...)` 新增关键字参数：

```python
planning_protocol: str | None = None,     # None=旧协议；PLANNING_DECISION_V1=新协议
previous_feedback: Any = None,            # PlanningFeedbackV1 或 None
```

- `planning_protocol=None`（默认）：返回的 mapping 与改动前**逐字节相同**。
- `planning_protocol="planning-decision-v1"`：在旧包内容之上 `update` 五个字段；
  `output_contract` 换成 `<planning_decision>{json}</planning_decision>`；
  `package_version` 换成 `planner-package-hierarchical-v5`。
- 其它字符串：抛 `ContractError`，**不**静默回退到旧协议。

### 2.2 五个新字段（§38）

```text
planning_protocol     {protocol, enabled_decision_types}   # H1 可执行类型取自 H1_DECISION_ENABLEMENT
planning_subjects     [{subject_key, occurrence_id, task_id, obligation_id, contract_revision}]
visible_refs          [PlanningRefV1 四元组]
visible_refs_omitted  int                                  # §48 截断计数（本片附加的兄弟字段）
previous_feedback     PlanningFeedbackV1.to_json() | null
decision_limits       {MAX_PD_* 十一项，值取自 contracts.planning_decisions 常量}
```

`enabled_decision_types` = `H1_DECISION_ENABLEMENT` 中 `executable=True` 的键，排序后输出
（H1：REFINE / REPAIR 两个子类 / BIND_EXISTING_GOAL / DECLARE_BLOCKED / WAIT / NO_CHANGE；
三个仅解码类型 REQUEST_EVIDENCE / REQUEST_HUMAN / PROPOSE_METHOD 不出现）。

### 2.3 subject_key（§19）

`planning_subjects(network)`：每个 occurrence 一条。`subject_key = "subject-" + sha256(规范JSON
{occurrence_id, task_id, obligation_id, contract_revision})[:32]`。

- **唯一**：occ-tuple 不同 → key 不同；
- **稳定可重算**：key 由内容派生，不依赖计数器或字典序，两次构建一致；
- 只在本 PlannerRequest 内有效（§19 原话）。

### 2.4 visible_refs 收集器（§18、补遗 §1、冲突检查 §5.1）

`visible_refs_from_hierarchical_package(package) -> tuple[dict, ...]`：**纯函数**，只读现包。
来源与四元组取值：

| 来源（包内已有） | kind | id | semantic_revision | content_hash |
|---|---|---|---|---|
| `method_library[].refine_method_ref` | `method` | `id` | `version` | 原样 |
| `applicability[].method_ref` | `method` | `method_id` | `version` | 原样 |
| `rejected_refinements[].rejected_method_ref` | `method` | `method_id` | `version` | 原样 |
| 同上 + `parameters_digest` | `method_instance`※ | `rejected_method_instance_id` | `plan_revision` | `parameters_digest` |
| `plan.open_compound_goals[]` | `task` | `goal_id` | `contract_revision` | `contract_hash` 或规范JSON sha256 |
| 同上 | `obligation` | `obligation_id` | 1（无账本修订） | 规范JSON sha256 |
| `plan.committed_primitives[]` | `task` / `obligation` | 同上 | 同上 | 同上 |
| `facts[].read_set_entry`（现网 kind=fact） | `observation` | `id` | 原样 | 原样 |
| `accepted_results[].acceptance_ref`（H2 才填充） | `acceptance` | `id` | 原样 | 原样 |

※ `method_instance` 的枚举成员属 H1-A envelope 片，**不在本片白名单**。收集器用
`getattr(PlanningRefKind, "METHOD_INSTANCE", None)` 探测：成员存在时输出该四元组；不存在时
退回该 entry 同时给出的 `method` 引用（**仍是正确引用，只是更粗**，且绝不编造版本/哈希）。
成员到达 A2a 片后，本函数无需改动即自动输出更细的引用（已用等价的 FakeKind 注入验证）。

**关键改写：** 现包 `facts[].read_set_entry` 的 `kind` 是 `fact`（read-set 线上 kind），
在 `visible_refs` 里**一律改写为 `observation`**（V2 §17：没有 `fact` 这个 ref kind）。

**去重与排序：** 去重键 = 四元组 `(kind, id, semantic_revision, content_hash)`；
排序键 = `(kind, id, semantic_revision, content_hash)`，确定性。

**截断：** 上限 `MAX_VISIBLE_REFS = 128`，超限取排序前缀；`visible_refs_omitted(package)`
返回被丢弃条数（§48「必须输出 truncated/omitted_counts」）。

**不编造：** `id` 为空、`semantic_revision` 非正整数、或 `content_hash` 存在但非 64 位小写
hex → 该来源**跳过**（不修哈希、不造 id）。缺哈希时才按 §5.1 用「对象规范 JSON 的 sha256」
派生。

### 2.5 三个哈希函数（供 H1-F 请求绑定）

```python
visible_refs_digest(refs) -> str          # canonical_json(refs).sha256
subject_bindings_hash(subjects) -> str    # canonical_json(subjects).sha256
package_hash(package) -> str              # canonical_json(package).sha256
```

三者均为规范 JSON 的 sha256（复用 `contracts.semantic_base.content_hash_of` → 验证与 H1-B 的
`package_hash` 校验口径一致）：**对象 key 顺序无关**，**数组顺序参与**（§15）。

---

## 3. 红 → 绿

- **红：** 先写 `test_planning_decision_package_v4.py`。首跑收集即红：

```text
E   ImportError: cannot import name 'HIERARCHICAL_DECISION_PACKAGE_VERSION' from 'agent_orchestrator.planning.htn.planner_package'
=========================== short test summary info ============================
ERROR tests/orchestrator/full_target/test_planning_decision_package_v4.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.28s
```

- **黄金哈希先行：** 写测试前先在**未改动的模块**上，对两个确定性输入量出规范 JSON sha256，
  写进测试作为旧协议字节钉子：

```text
fixture world (build_world(key="p23c-pkg"))  a9aa2e7e715596ebbec79ac3f319530ab6675272b6808a8a6e3d8f1e59ba42fd
stub world (no registry/occurrence/observation)  801b8e3934fd5a77c347385a13d467157bc3e5f325d85faafc5c84877293e9d0
```

- **绿：** 实现后新测试文件实测尾行：

```text
46 passed in 0.46s
```

（分步：收集红 → 实现 → 44 passed → 补「旧节不动」「method_instance 边界」两条 → 46 passed。）

---

## 4. 测试覆盖（46 条）

| 组 | 覆盖点 | 条数 |
|---|---|---|
| A 旧协议字节不变 | 两个黄金 sha256 逐位相等；旧包不含任何决策字段；旧 output_contract/标签不变；未知协议名拒绝 | 4 |
| B 五字段齐全且类型正确 | 与旧包字段差集恰为六项；旧节全部保留；protocol+enabled_types；output_contract/label；decision_limits=§16 常量；previous_feedback null 与 to_json 往返；previous_feedback 走合同校验 | 8 |
| C planning_subjects | 唯一、稳定（两次构建相同）；五键齐全；覆盖 board 所有 occurrence；已提交计划仍唯一 | 4 |
| D visible_refs | 每种来源各一例（method_library/applicability/rejected_refinement/accepted_result/open_goal/committed_primitive/facts）；fact→observation 改写；method_instance 边界；非法来源跳过不编造；是合法 PlanningRefV1；去重；排序确定；128 截断取排序前缀；omitted 计数；内建包暴露收集器输出 | 18 |
| E 三个哈希函数 | 各自键序无关；package_hash=规范JSON sha256；数组序敏感；独立；对真实包各节自洽 | 8 |
| F 无系统权威字段泄漏 | 新顶层字段不与 §32 集合相交；visible_refs 只有四键；protocol 只有两键；新节不含 registry_status/authorization_ref/grant_ref | 4 |

> 合计 46 条测试函数。

### 4.1 变异清单（H1 纪律）

| 变异 | 结果 |
|---|---|
| M1 旧协议路径也给 `output_contract`/`package_version` 换成新值 | **KILLED**（A 组黄金 sha256 + `test_the_legacy_package_never_grows_a_decision_field`） |
| M2 `enabled_decision_types` 直接列九种（含仅解码三类） | **KILLED**（`test_the_decision_package_states_the_protocol_and_its_enabled_types`） |
| M3 `fact` 不改成 `observation`（原样保留 kind=fact） | **KILLED**（`test_a_fact_entry_is_written_as_an_observation`） |
| M4 排序键换成插入序 / 用 set 去重后不排序 | **KILLED**（`test_the_same_package_twice_yields_the_same_refs_in_the_same_order`） |
| M5 截断但 `visible_refs_omitted` 恒为 0 | **KILLED**（`test_the_omitted_count_is_reported_for_over_long_input`） |
| M6 `subject_key` 用计数器（enumerate 序号） | **KILLED**（`test_subject_keys_are_unique_and_stable_across_two_builds`） |
| M7 `package_hash` 用 `json.dumps` 默认（键序参与） | **KILLED**（`test_package_hash_ignores_object_key_order`） |
| M8 `previous_feedback` 不校验直接透传 | **KILLED**（`test_previous_feedback_is_validated_through_the_contract`） |
| M9 非法 `content_hash` 被「修好」成派生哈希 | **KILLED**（`test_a_malformed_source_ref_is_skipped_rather_than_invented`） |

---

## 5. 验收门（完成标准逐条）

| 完成标准 | 证据 | 结果 |
|---|---|---|
| 新测试文件全绿 | `46 passed in 0.46s` | ✅ |
| 名称含 planner_package 的既有测试全绿 | `tests/orchestrator/full_target -k planner_package`：`1 passed, 3059 deselected in 1.10s` | ✅ |
| full_target 全绿 | `3058 passed, 2 skipped in 129.15s (0:02:09)`（skip：SH_PANDA_PARSER、--run-real-provider） | ✅ |
| 旧协议字节不变 | 两个黄金 sha256 逐位相等（改动前量取并写进测试） | ✅ |
| ruff 无告警 | `ruff check <两文件>`：`All checks passed!` | ✅ |
| 工作树干净 | 见 §7 commit | ✅ |

### 5.1 全仓基线（不计入本片，如实记录）

`pytest tests/orchestrator`（排除 `tests/orchestrator/gap_phase1`）实测 `30 failed, 4995 passed,
22 skipped in 727.61s`。失败全部落在本片未触碰且不 import `planner_package` 的模块：

- `tests/orchestrator/p32/test_p32_sandbox.py`（15）等：`PermissionError: [Errno 1] Operation not permitted`（沙箱环境）；
- `tests/orchestrator/p33/test_g_workspace_paging.py`（5）、`test_g_large_read_context.py`（1）：缺 `tiktoken`；
- `tests/orchestrator/p33/test_p33_pytest_workspace_config.py`：宿主 pytest 配置；
- `tests/orchestrator/p33/test_p33_source_dependencies.py`：`KnowledgeIndex.check` AST 基线不符（Python 3.14）。

另 `tests/orchestrator/gap_phase1` 与部分 `tests/integration/runtime` 在采集期即报
`'asyncio' not found in markers` / `No module named 'simple_harness_memory'`——均为 baseline
既有环境/配置问题，与本片无关。

---

## 6. 边界与后续

- **未接线**：调用方（`event_handler._hierarchical_planner_package`）本片**不动**，因此现网路径
  仍只走旧协议；新协议需 H1-H 双分支接线后才真正被使用（本片只提供开关与包形状）。
- **整数配对版本**：`runtime/role_templates.py:715` 的 3→4 属 H1-E，本片不改（故本片测试不涉及
  整数版本配对）。
- **method_instance**：本片在枚举成员缺失时降级为 method 引用；A2a 片补上枚举成员后自动生效。
- **accepted_results**：现包尚无该节（H2 引入）；收集器已按 §48 预留读取逻辑，当前恒为空。

---

## 7. 提交

```text
feat(h1-d): planner package additions for planning-decision-v1 (subjects, visible refs, feedback, limits)
```

---

## 8. 第 1 轮处置（核验结论：修后可合）

**处置提交：** `b532b51`（fix(h1-d): use authoritative §5.1 digests in visible_refs (task/obligation) and strict ref bounds）

**核验报告：** `plans/llm-native-htn/H1/reviews/核验-H1-D-2026-09-19.md`（位于核验副本
`simple-runtime-sdk-h1d-verify-h1-d`，未跟踪）。P0 无；P1 一条；P2 三条。

### 8.1 P1-1（必修）：task / obligation 的 `content_hash` 来源不符 §5.1

**问题。** 旧包的 `open_compound_goals[]` 只带 `contract_revision`、不带哈希，收集器因此落进
「无现成哈希 → 派生」分支，对 `{kind,id,semantic_revision}` 求 sha256。该值与库内权威值不等：
task 输出 `fbc4fcfd…` 而 `task_semantics.content_hash` 为 `814531fc…`；obligation 输出
`66efa7b8…` 而 `content_hash_of(obligation_json)` 为 `ed0798d5…`。`visible_refs` 是模型要逐字节
照抄、H1-F 会重算比对的四元组，钉一个与对象无关的哈希等于让下游校验必然失败。

**修复。** 收集器**不再派生任何哈希**（删除 `_ref_hash`）。`_one_ref` 现在是「哈希必须由来源
提供」：无哈希 → 跳过；非整数/非正 revision → 跳过；哈希格式不合法 → 跳过。权威摘要改由一条**仅
新协议读取的旁路**带入：

- `hierarchical_planner_package` 新增 `authoritative_refs` 入参（默认 `()`，旧路径不传、也不产出该键）；
- 包内新增 `authoritative_refs` 兄弟字段：`_network_authorities(network)` 覆盖 board 上**每个 task
  binding**（`kind=task`、`id=task_id`、`semantic_revision=binding_revision`、
  `content_hash=binding.content_hash()`，即 `task_semantics.content_hash`），调用方再补 obligation
  的对象规范 JSON 摘要；
- 收集器按 `(kind,id)` 索引该表，`task`/`obligation` 只从表里取值和哈希；表里没有的 kind/id
  **不产出 ref**（宁缺毋滥）。

`authoritative_refs` 是 side table（模型不据此推理），随包进入决策包、旧包永不出现它。

**实测（核验员同款脚本）。**

```text
task emitted  : 814531fcfc18f110015323715cddbdc1be62e06403bd0d22958574d18018cc70
task authority: 814531fcfc18f110015323715cddbdc1be62e06403bd0d22958574d18018cc70
task MATCH    : True
obl emitted   : ed0798d5f897c6a47e665ffd04b611845476433ab1609acc377c0d7745ed6fbe
obl authority : ed0798d5f897c6a47e665ffd04b611845476433ab1609acc377c0d7745ed6fbe
obl MATCH     : True
no-supply has obligation ref: False
```

### 8.2 P2 三条

| 编号 | 问题 | 修复 |
|---|---|---|
| P2-1 | 非正 revision 被「抬为 1」、字符串 revision `"3"` 被 `int()` 接受 | `_one_ref` 要求 `isinstance(int)` 且 `>=1`，否则跳过（与 `index(minimum=1)` 口径一致） |
| P2-2 | `resolution_ref` 被标成 `acceptance` | `_accepted_ref` 按 §5.1 区分两种 kind，各自原样输出 |
| P2-3 | observation 缺哈希时被派生 | observation 只取 `read_set_entry` 自带的 `ReadItem.content_hash`，缺则跳过 |

P2-4（`method_instance` 缺枚举成员时降级为 method 引用）核验员已确认归属 H1-A，不构成本片问题。

### 8.3 测试先行与变异

先补测试到红（红尾：`12 failed, 43 passed in 0.59s`），再改实现到绿（`58 passed in 0.55s`）。
核验员上一轮判 **SURVIVED** 的 5 个变异（E1 派生材料加盐、E2 obligation 固定哈希、E3 非正 revision
抬为 1、E4 `resolution_ref` 发 `acceptance`、E5 字符串/浮点 revision 被接受）本轮逐一重跑，**全部
KILLED**：

```text
E1 -> 3 failed, 55 passed in 0.56s
E2 -> 1 failed, 57 passed in 0.55s
E3 -> 1 failed, 57 passed in 0.55s
E4 -> 1 failed, 57 passed in 0.55s
E5 -> 1 failed, 57 passed in 0.56s
```

变异均以 `/tmp` 副本注入并恢复（`diff -q` 校验恢复后与备份一致），未使用任何 git 写命令。

### 8.4 本轮验收门

| 完成标准 | 证据 | 结果 |
|---|---|---|
| 新测试文件全绿 | `58 passed in 0.55s` | ✅ |
| full_target 全绿 | `3070 passed, 2 skipped in 129.68s (0:02:09)` | ✅ |
| 相关六文件全绿 | `349 passed in 18.45s` | ✅ |
| 旧协议字节不变 | 黄金 `a9aa2e7e…` / `801b8e39…` 仍逐位相等 | ✅ |
| ruff 无告警 | `ruff check <两文件>`：`All checks passed!` | ✅ |

---

## 9. 第 2 轮处置（核验结论：修后可合）

**核验报告：** `plans/llm-native-htn/H1/reviews/核验-H1-D-2026-09-19.md`「复核 2」小节。P0 无；P1
一条（P1-2，本轮新引入的不一致）；P2 四条（P2-5…P2-8，其中 P2-4 沿用、非本片问题）。

**处置提交：** `7561fba`（fix(h1-d): count omitted refs against the same scoped input as visible_refs）、`5ec0d06`（fix(h1-d): sort the authority sidecar so its row order cannot move the package）。

### 9.1 P1-2（必修）：`visible_refs_omitted` 与 `visible_refs` 取自不同输入包

**问题。** 上一轮的修复让 `_decision_fields` 用**带旁路 `authoritative_refs` 的 `scoped` 包**算
`visible_refs`，却用**不带旁路的 `package`** 算 `visible_refs_omitted`。旁路会贡献 task/obligation
引用，于是截断发生时两个字段来自不同的输入集合，`visible_refs_omitted` **少报**被丢弃条数。

**复现（核验员公开构造器口径）。** 65 个 compound goal（每个贡献 task + obligation 两条 ref，共 130
条）+ 65 条 obligation 权威，收集总数 130、上限 128；修复前 `visible_refs_omitted` 报 `0`（真实应丢
`2`）：

```text
len(visible_refs)        : 128
visible_refs_omitted     : 0     ← 少报
omitted(returned)        : 2
INCONSISTENT             : True
```

**修复。** `_decision_fields` 里两个量都基于**同一个 `scoped`**：`refs = visible_refs_from_...(
scoped)`，`omitted = len(_sorted_unique_refs(scoped)) - len(refs)`。两者同源，计数即「本次
`visible_refs` 实际丢了多少」（§48「必须输出 truncated/omitted_counts」）。

**实测（同一构造器，修复后）。**

```text
len(visible_refs)        : 128
visible_refs_omitted     : 2
omitted(returned)        : 2
INCONSISTENT             : False
```

### 9.2 P2 四条

| 编号 | 问题 | 处置 |
|---|---|---|
| P2-5 | 权威表按 `(kind,id)` 取值的 kind 区分未钉死（N3 SURVIVED） | 补两条负例：同 id、不同 kind 的两条权威并存时不得串用；只有 task 行时不产出 obligation ref |
| P2-6 | 截断边界与旁路顺序无断言（N7 / N8 SURVIVED） | 补「>128 且旁路贡献 ref 时 `visible_refs_omitted == 收集总数 − 128`」与「旁路行序无关」两条断言 |
| P2-7 | `acceptance_ref` / `resolution_ref` 同时存在时优先级无断言（N9 SURVIVED） | 补两条：两者并存取 acceptance；无 acceptance 时取 resolution |
| P2-8 | 新增模型可见顶层字段 `authoritative_refs` 不在 §38「只加五项」清单内 | **见 §9.3：规格未覆盖点，按纪律记录并请计划作者裁定（本片不擅自改名或收敛）** |

### 9.3 P2-8 规格未覆盖点（**已裁定，2026-09-19 06:30**）

**裁定（计划作者，补遗文件末尾「追加裁定 2026-09-19 06:30」）：不追认 `authoritative_refs`。**
请求包在新协议下只加 V2 第 38 节的五项；任务/责任的权威哈希改由**收集器（构包函数）的可选入参**
在构包时传入，不写进包体、不渲染给模型。

**改法（本轮）。**

- **收集器签名**：`visible_refs_from_hierarchical_package(package, *, authoritative_refs=())` 与
  `visible_refs_omitted(package, *, authoritative_refs=())` 新增可选关键字入参；`_collect_refs` /
  `_sorted_unique_refs` / `_authority_index` 全程以参数传递，**不再从包体读任何 `authoritative_refs`**。
- **构建函数**：`hierarchical_planner_package(..., authoritative_refs=...)` 仍接受该可选入参（默认 `()`），
  由它算出 `visible_refs`；**返回值只含 §38 的五项新增字段**。原先落在包里的
  `authoritative_refs` 与 `visible_refs_omitted` 两个键**一并移除**：前者改当入参，后者由调用方用
  `visible_refs_omitted(pkg, authoritative_refs=...)` 现算（H1-F 仍可从同一输入重算）。
- **结果**：新协议包的顶层键集合 = 旧键集合 + **恰好五项**；`_seal` 渲染的模型文本里不再出现
  `authoritative_refs` / `visible_refs_omitted`（实测 `"## authoritative_refs" in sealed.text == False`）。
- **旧协议字节不变**：黄金 `a9aa2e7e…`（fixture 世界）与 `801b8e39…`（stub 世界）仍逐位相等。

> 上一轮 §9.3 的「待裁定线上字段」备案**随之关闭**；本片实际采用的是当日核验报告给出的方案 (b)
> （收集器可选参数）。`visible_refs_omitted` 一并离开包体，是因为裁定要求「只加第 38 节的五项」，
> 而 §38 列的五项不含它。

### 9.4 本轮验收门

| 完成标准 | 证据 | 结果 |
|---|---|---|
| 新测试文件全绿 | `66 passed in 0.55s` | ✅ |
| 相关四文件全绿 | `339 passed in 9.42s` | ✅ |
| full_target 全绿 | `3078 passed, 2 skipped in 126.43s (0:02:06)`（2 skipped 为既有条件跳过） | ✅ |
| 旧协议字节不变 | 黄金 `a9aa2e7e…` / `801b8e39…` 仍逐位相等 | ✅ |
| ruff 无告警 | `ruff check <两文件>`：`All checks passed!` | ✅ |
| sdk_gate.sh | 见 §9.5 | ✅ |

---

## 9.5 提交与闸门

```text
fix(h1-d): count omitted refs against the same scoped input as visible_refs
```

`sdk_gate.sh <workspace> b13e757 --tests tests/orchestrator/full_target/test_planning_decision_package_v4.py
--max-sentinel 26`：`ok=true`，8 项全 PASS。

### 9.6 本轮新变异（复核 2 的 N/F 系列）与补充修复

复核 2 报上一轮实现有 6 个变异 SURVIVED（N3 / N7 / N8 / N9 / F1 / F2）。本轮逐条重做，**全部
KILLED**：

```text
N3  权威表索引忽略 kind      -> 2 failed, 63 passed
N7  omitted 恒为 0（=F1）    -> 2 failed, 63 passed
N8  权威表行序反转（未排序） -> 1 failed, 65 passed
N9  resolution 优先于 acceptance -> 1 failed, 64 passed
F2  权威表只保留第一个 binding   -> 2 failed, 64 passed
```

其中 **N8 暴露了一个真实缺陷**（不只是测试缺口）：调用方传入的 `authoritative_refs` 行序原先直接
落进包，两个仅行序不同的调用会得到不同的 `visible_refs` 与 `authoritative_refs`，从而移动请求绑定
所哈希的整包。已在 `_decision_fields` 里按 §5.1 四元组 `sorted(..., key=_authority_sort_key)` 定序
（`_authority_sort_key` 容忍字段缺失，畸形行仍由下游 `_authority_index` 跳过，不由排序崩溃）。补测试
`test_the_built_sidecar_is_order_independent` 钉死。

变异均以 `/tmp` 副本注入并恢复（`diff -q` 校验恢复后与备份一致），未使用任何 git 写命令。

---

## 10. 第 3 轮处置（裁定落实 + 复核 3 意见，核验结论：修后可合）

**处置提交：** `2fe1062`（fix(h1-d): keep the decision package to §38's five fields (authorities as a collector argument)）。
**裁定来源：** `LLM-native-HTN计划V2-裁定补遗-2026-09-18.zh-CN.md` 末尾「追加裁定 2026-09-19 06:30」。
**核验来源：** `plans/llm-native-htn/H1/reviews/核验-H1-D-2026-09-19.md`「复核 3」小节。

### 10.1 裁定落实：请求包只加 §38 五项（关闭 §9.3 备案）

- `authoritative_refs` 不再写进包体：改为**收集器可选入参**
  `visible_refs_from_hierarchical_package(pkg, *, authoritative_refs=...)`（构包函数同名入参，默认 `()`）。
- `visible_refs_omitted` 同样离开包体（§38 五项不含它）：调用方用
  `visible_refs_omitted(pkg, *, authoritative_refs=...)` 现算，与 `visible_refs` **同源**。
- 实测：新协议包顶层键 = 旧键集合 + **恰好五项**（`planning_protocol` / `planning_subjects` /
  `visible_refs` / `previous_feedback` / `decision_limits`）；`_seal` 文本不含
  `authoritative_refs` / `visible_refs_omitted`；旧协议黄金哈希 `a9aa2e7e…` / `801b8e39…` 不变。

### 10.2 P1-3：`visible_refs_omitted` 只计唯一引用的口径

**问题（复核 3）。** 若把计数口径写成原始收集数（`len(_collect_refs(...)) − 128`），同一引用同时
出现在 `method_library` 与 `applicability`（生产路径确实如此）时会把重复算作「被截断丢弃」，从而
**多报**。该口径此前无测试钉死（变异 I SURVIVED）。

**修复/钉死。** 计数一律基于**去重后的唯一引用**（`_sorted_unique_refs`）。新增两条断言：

- 唯一引用 2 条、其中 1 条重复、总量远低于上限 → `visible_refs_omitted == 0`；
- 唯一引用 `128+7`、每条再重复一次（原始收集 `2×135`、唯一 `135`）→
  `visible_refs_omitted == 135 − 128 == 7`，且**不等于** `2×135 − 128 == 142`。

### 10.3 P2：排序键分量与畸形入参兜底

| 编号 | 问题 | 处置 |
|---|---|---|
| P2-9 | `_ref_sort_key` 的 hash / kind 分量可弱化而不被发现（D / E / N8b 存活） | 补两条：`(kind,id,revision)` 相同、仅 hash 不同时按 hash 升序；task/obligation 共享 id 时按 kind 先序 |
| P2-10 | 畸形入参行的兜底分支无断言（F 存活） | 补两条：收集器层 `["not-a-row", {"kind":...无 id}, {"id":...无 kind}, None, 真行]` 不崩且只出真 ref；**构建器层**（排序列真正执行处）同样不崩 |
| P2-8 | 见 §9.3 / §10.1，**已裁定并落实** | — |
| P2-4 | `method_instance` 缺枚举成员时降级，归属 H1-A | 不改 |

### 10.4 本轮变异（复核 3 的 I/D/E/F 与前三轮）

复核 3 报 4 个存活点，本轮逐条重做，**全部 KILLED**：

```text
I  计数改用原始收集数（非唯一）  -> 1 failed, 71 passed
D  排序键丢 hash 分量             -> 1 failed, 71 passed
E  排序键只留 hash（丢 kind/id）  -> 3 failed, 69 passed
F  畸形入参行直接 raise           -> 1 failed, 72 passed
```

变异均以 `/tmp` 副本注入并恢复（`diff -q` 校验恢复后与备份一致），未使用任何 git 写命令。

### 10.5 本轮验收门

| 完成标准 | 证据 | 结果 |
|---|---|---|
| 本片测试文件全绿 | `73 passed in 0.58s` | ✅ |
| 相关四文件全绿 | `346 passed in 9.27s` | ✅ |
| full_target 全绿 | `3085 passed, 2 skipped in 123.82s (0:02:03)` | ✅ |
| 旧协议字节不变（黄金测试未改） | 黄金 `a9aa2e7e…` / `801b8e39…` 逐位相等 | ✅ |
| 包顶层键 = 旧 + 恰好五项 | 见 §10.1 | ✅ |
| sealed 文本不含 `authoritative_refs` | 见 §10.1 | ✅ |
| ruff 无告警 | `ruff check <两文件>`：`All checks passed!` | ✅ |

---

## 11. 第 4 轮处置（核验结论：修后可合）

**处置提交：** `e8e635b`（fix(h1-d): pin production task digest and make authority sorting discriminating）。
**核验来源：** `plans/llm-native-htn/H1/reviews/核验-H1-D-2026-09-19.md`「复核 4」小节（同一核验员续做；
上一轮核验副本 HEAD `881471c`，本轮验 `0d9d7fb`）。P0 无；P1 一条（P1-4，含 P1-5 / P1-6 两条派生
测试缺口）；P2-8 已关闭。

### 11.1 P1-4：生产路径下 task 权威哈希来源被调用方入参掩盖

**问题。** 上一轮把权威摘要改成「调用方入参」后，测试里一律传 `authoritative_refs=task_authorities(
network)`；而 `_authority_index` 对同一 `(kind,id)` **后写覆盖**，调用方行盖过了构建器 `_network_
authorities` 自己生成的行。于是**生产调用方（`event_handler` 不传该入参）的 task 哈希取值没有任何
测试钉死**：把 `_network_authorities` 的哈希加盐（变异 E1）后，测试路径仍显示正确、73 条全过，而
生产包会暴露与库内权威值不一致的 task 哈希。

**修复（两层）。**

1. **补生产路径断言**（核验给出的第一方案）：新增
   `test_the_production_path_task_hash_is_the_bindings_own_digest`——不给任何调用方权威行，直接断言
   `package["visible_refs"]` 里每个 task 的 `content_hash == binding.content_hash() ==
   content_hash_of(binding.to_json())`，且**不等于**任何「由 ref 派生」的值。
2. **让构建器行不可被覆盖**（核验给出的第二方案，也是更稳的形态）：新增 `_merge_authorities`，
   构建器（网络 binding）行在前、调用方行**只补缺不覆盖**——同一 `(kind,id)` 已有构建器行时丢弃
   调用方行；调用方行先按 §5.1 四元组排序，保证「同一 `(kind,id)` 的重复行取最小键」与输入顺序无关。
   新增 `test_a_caller_row_cannot_override_the_builders_task_digest` 钉死。

### 11.2 P1-5：`_ref_sort_key` 的 kind 分量未真正钉死（T2 SURVIVED）

原用例取 task id `"z"`、obligation id `"a"`，id 序恰好与 kind 序一致，故丢掉 kind 分量也能通过。
改用**交叉 id**（task `"a"`、obligation `"z"`）：只有 kind 先于 id 才得到 `[("obligation","z"),
("task","a")]`。

### 11.3 P1-6：`_authority_sort_key` 并列分量与构建器排序无判别性断言（D/E/N8b/P5/N8 SURVIVED）

原「旁路行序无关」用例只用两行**键完全不同**的 obligation 行，任何弱化排序键的写法都看不出差别。
新增 `test_duplicate_authority_keys_are_order_independent`：对**同一 `(kind,id)`** 的重复调用方
行（同 revision / 异 hash，以及异 revision / 异 hash 两组）断言 fwd / rev 两序得**同一个**包，并
断言胜出者即**最小四元组**（revision 1、hash `f…`）。因调用方行会先排序，重复键的胜出者与输入顺序
无关。

### 11.4 本轮变异（复核 4 的存活项与学生项）

复核 4 报的存活点与本人追加的「覆盖回写」缺陷，本轮逐条重做，**全部 KILLED**：

```text
E1  构建器 task 哈希加盐（生产路径）  -> 4 failed, 72 passed
P5  构建器不排序调用方行              -> 1 failed, 75 passed
T2  _ref_sort_key 丢 kind 分量        -> 1 failed, 75 passed
D   _authority_sort_key 丢 hash 分量  -> 1 failed, 75 passed
F   畸形入参行直接 raise              -> 1 failed, 75 passed
O1  调用方行覆盖构建器行（原缺陷）    -> 2 failed, 74 passed
```

变异均以 `/tmp` 副本注入并恢复（`diff -q` 校验恢复后与备份一致），未使用任何 git 写命令。

### 11.5 裁定落实仍成立（复核确认）

新协议包顶层键 = 旧键集合 + **恰好五项**；`authoritative_refs` / `visible_refs_omitted` 均不在
包体、不在 sealed 文本；旧协议黄金 `a9aa2e7e…` / `801b8e39…` 逐位相等。

### 11.6 本轮验收门

| 完成标准 | 证据 | 结果 |
|---|---|---|
| 本片测试文件全绿 | `76 passed in 0.61s` | ✅ |
| 相关四文件全绿 | `349 passed in 9.19s` | ✅ |
| full_target 全绿 | `3088 passed, 2 skipped in 123.27s (0:02:03)` | ✅ |
| 旧协议字节不变（黄金测试未改） | 黄金 `a9aa2e7e…` / `801b8e39…` 逐位相等 | ✅ |
| 包顶层键 = 旧 + 恰好五项 | §11.5 | ✅ |
| ruff 无告警 | `ruff check <两文件>`：`All checks passed!` | ✅ |

---

## 12. 第 5 轮处置（核验结论：修后可合）

**处置提交：** `2967b43`（test(h1-d): discriminate merge kind, binding revision, and sort/dedup components）、`85129c7`（journal 记录）。
**核验来源：** `plans/llm-native-htn/H1/reviews/核验-H1-D-2026-09-19.md`「复核 5」小节（同一核验员续做；
上一轮核验副本 HEAD `0d9d7fb`，本轮验 `75be3df`）。P0 无；P1 两条（P1-7 / P1-8，均为「测试输入恰好
相等而使断言失去鉴别力」的缺口）。

### 12.1 P1-7：`_merge_authorities` 去重键的 kind 维度未被钉死（A2 SURVIVED）

**问题。** 把合并去重键从 `(kind, id)` 弱化成 `("", id)` 后 76 条全过。此时若一个 task 与一个
obligation **共用同一 id**，后者会被当成「已有键」丢弃——而冲突检查 §5.1 允许 task / obligation
共享 id（按 kind 分列）。既有 `test_the_authority_table_keys_on_kind_and_id_not_id_alone` 只覆盖
**查询侧**（`_authority_index`），未覆盖**合并侧**（`_merge_authorities`）。

**修复。** 新增 `test_a_caller_obligation_sharing_a_task_id_is_not_dropped`：令一个 task 与其
obligation 共用同一 id（`shared`），构建器提供 task 行（网络 binding）、调用方提供 obligation 行，
断言两条 ref 都在 `visible_refs` 里，且各取自己的哈希。变异 A2 → `1 failed, 81 passed`（KILLED）。

### 12.2 P1-8：`_network_authorities` 的 `semantic_revision` 取值未被钉死（A7 SURVIVED）

**问题。** 把 `_network_authorities` 的 `semantic_revision` 写死为 `1` 后 76 条全过——现有
`build_world` / `committed` 世界里每个 binding 的 `contract_revision` **恰好都是 1**，故断言
`semantic_revision == int(binding.contract_revision)` 没有鉴别力。真实系统会产出
`contract_revision > 1` 的 binding（`orchestrator/plan_commits.py:1507` 对 supersede 的 binding 做
`ContractRevision(int(rev) + 1)`）；写死 1 会让 ref 与 `task_semantics.binding_revision` 不一致，
破坏 §17 四元组的逐字节匹配。

**修复。** `_WideBinding` 增加 `revision` 入参，新增
`test_a_task_ref_revision_comes_from_the_binding_even_past_one`：构造 `contract_revision = 3` 的
binding，断言该 task 的 ref `semantic_revision == 3` 且 `!= 1`。变异 A7 → `1 failed, 81 passed`（KILLED）。

### 12.3 自查：其它「恰好相等 / 恰好有序 / 恰好唯一」的鉴别力缺口

按任务书要求对同类问题做了一次系统自查，用**变异扫描排序键 / 去重键各分量**找出仍存活的退化输入，
并各补一条有鉴别力的用例：

| 缺口 | 退化点 | 新增用例 | 变异结果 |
|---|---|---|---|
| 排序键 id 分量 | 同 kind 的 id 序恰好与 (revision, hash) 序一致 | `test_the_refs_are_sorted_by_id_ahead_of_revision_and_hash`（`m-a` v2 vs `m-z` v1） | `R_drop_id` → `2 failed`（KILLED） |
| 排序键 revision 分量 | 同 (kind, id) 的 revision 序恰好与 hash 序一致 | `test_the_refs_are_sorted_by_revision_ahead_of_hash`（v2/hash-a vs v1/hash-e） | `R_drop_rev` → `2 failed`（KILLED） |
| 去重键 revision 分量 | 现有用例中同一 (kind, id, hash) 的 revision 全相同 | `test_refs_differing_only_in_revision_are_not_collapsed`（acc-1 @ rev1 / rev2） | `D_dedup_drop_rev` → `1 failed`（KILLED） |
| 去重键 id 分量 | 现有用例中 (kind, revision, hash) 全不同的 id 从未重复 | `test_refs_differing_only_in_id_are_not_collapsed`（m-one / m-two 同 rev、同 hash） | `D_dedup_drop_id` → `1 failed`（KILLED） |

**判定为等价（非缺口）的退化：** `_authority_sort_key` 的 kind / id 分量——合并对同一 `(kind, id)`
分组取最小行，而组内 kind 与 id 是**常量**，故丢弃它们不改变任何分组的胜者（`A_drop_kind` /
`A_drop_id` 不可观测，已用脚本证明）。畸形行透传与否同属等价（`_authority_index` 一律跳过）。

### 12.4 本轮变异汇总（复核 5 的存活项 + 自查项）

```text
A2  _merge_authorities 去重键丢 kind        -> 1 failed, 81 passed
A7  _network_authorities revision 写死 1    -> 1 failed, 81 passed
R_drop_kind  排序键丢 kind                  -> 1 failed, 81 passed
R_drop_id    排序键丢 id                    -> 2 failed, 80 passed
R_drop_rev   排序键丢 revision              -> 2 failed, 80 passed
R_drop_hash  排序键丢 hash                  -> 1 failed, 81 passed
D_dedup_drop_id   去重键丢 id               -> 1 failed, 81 passed
D_dedup_drop_rev  去重键丢 revision         -> 1 failed, 81 passed
D_dedup_drop_hash 去重键丢 hash             -> 1 failed, 81 passed
M_drop_kind  合并键丢 kind                  -> 1 failed, 81 passed
M_drop_id    合并键丢 id                    -> 2 failed, 80 passed
```

变异均以 `/tmp` 副本注入并恢复（`diff -q` 校验恢复后与备份一致），未使用任何 git 写命令。

### 12.5 保持不变项（复核确认）

旧协议字节不变（黄金 `a9aa2e7e…` / `801b8e39…` 逐位相等）；新协议包顶层键 = 旧键 + **恰好五项**；
sealed 文本不含 `authoritative_refs` / `visible_refs_omitted`；§5.1 生产路径 task 哈希 =
`binding.content_hash()`。

### 12.6 本轮验收门

| 完成标准 | 证据 | 结果 |
|---|---|---|
| 本片测试文件全绿 | `82 passed in 0.62s` | ✅ |
| 相关四文件全绿 | `355 passed in 9.22s` | ✅ |
| full_target 全绿 | `3094 passed, 2 skipped in 123.00s (0:02:03)` | ✅ |
| 旧协议字节不变（黄金测试未改） | 黄金 `a9aa2e7e…` / `801b8e39…` 逐位相等 | ✅ |
| ruff 无告警 | `ruff check <两文件>`：`All checks passed!` | ✅ |

---

## 13. 第 6 轮处置（核验结论：修后可合）

**核验来源：** `plans/llm-native-htn/H1/reviews/核验-H1-D-2026-09-19.md`「复核 6」小节（同一核验员续做；
上一轮核验副本 HEAD `75be3df`，本轮验 `a5acea1`）。P0 无；P1 两条（P1-9 / P1-10，均为「测试输入退化
使断言失去鉴别力」的缺口，实现本身已正确）；P2-14 记录三条等价变异。

### 13.1 P1-9：`_network_authorities` 的 revision 来源未被判别性钉死（C4 SURVIVED）

**问题。** 把 `semantic_revision` 改成 `int(network.plan_revision) + int(binding.contract_revision)`
后 82 条全过——现有 fixture 世界的 `plan_revision` **恰好为 0**，该表达式与 binding 修订号数值相同。
生产世界 `plan_revision > 0`（提交后即非 0），此变异会把 ref 写成 `plan_revision + binding_revision`，
与 `task_semantics.binding_revision` 不一致，破坏 §17 四元组逐字节匹配。

**修复。** 新增 `test_a_task_ref_revision_is_the_binding_not_the_plan_revision`：构造
`plan_revision = 5`、`binding.contract_revision = 1`，断言 ref `semantic_revision == 1` 且
`!= plan_revision + contract_revision`。变异 C4 → `1 failed, 84 passed`（KILLED）。

### 13.2 P1-10：`_ref_sort_key` 的 revision 分量类型未被钉死（C7 SURVIVED）

**问题。** 把 `int(ref["semantic_revision"])` 改成 `str(...)` 后 82 条全过——现有用例只用到 revision
1 / 2（同一数量级），`str` 与 `int` 排序结果相同。两位数字 revision 下字典序会把 `"10"` 排在 `"2"`
前；`visible_refs` 的数组顺序参与 canonical hash（§15），故属行为差异。

**修复。** 新增 `test_the_refs_are_sorted_by_revision_numerically_not_as_strings`（同 `(kind,id)`
下 v10 与 v2，断言升序为 `[2, 10]`）。变异 C7 → `1 failed, 84 passed`（KILLED）。

### 13.3 自查：其它「恰好相等 / 恰好同数量级」的鉴别力缺口

按任务书要求再次系统自查（对本轮相关排序键 / 去重键 / 来源表达式做变异扫描），又发现并补上一个同类缺口：

- **`_authority_sort_key` 的 revision 分量**：合并对同一 `(kind,id)` 分组取最小行；若该分量用
  `str` 比较，rev 10 会被当成小于 rev 2（`"10" < "2"`），从而选出错误胜者。新增
  `test_caller_authority_revisions_sort_numerically_not_as_strings`（同一 obligation 键的 rev 10
  与 rev 2，断言胜出者 rev == 2）。变异 `A_rev_str` → `1 failed, 84 passed`（KILLED）。

以下退化经证为**等价**（非缺口）：`_authority_sort_key` 的 kind / id 分量在分组内为常量（丢弃不改变
胜者）；畸形行透传与否被 `_authority_index` 一律跳过；`_one_ref` 的 revision 下界/类型分支已有用例
（`one_ref_bool`、`one_ref_hash_optional` 均 KILLED）。

### 13.4 本轮变异汇总（复核 6 的存活项 + 自查项）

```text
C4  revision 取 plan_revision + contract_revision -> 1 failed, 84 passed
C7  _ref_sort_key revision 用 str()               -> 1 failed, 84 passed
A_rev_str  _authority_sort_key revision 用 str()  -> 1 failed, 84 passed
R_drop_kind / R_drop_id / R_drop_rev / R_drop_hash -> KILLED（1–3 failed）
R_rev_str                                           -> 1 failed, 84 passed
A_drop_hash                                         -> 1 failed, 84 passed
D_dedup_drop_id / _rev / _hash                      -> KILLED（1–2 failed）
M_drop_kind / M_drop_id                             -> KILLED（2 failed）
A7  revision 写死 1                                 -> 1 failed, 84 passed
```

共 14 个变异全部 KILLED。变异均以 `/tmp` 副本注入并恢复（`diff -q` 校验恢复后与备份一致），未使用
任何 git 写命令。

### 13.5 保持不变项（复核确认）

旧协议字节不变（黄金 `a9aa2e7e…` / `801b8e39…` 逐位相等）；新协议包顶层键 = 旧键 + **恰好五项**；
sealed 文本不含 `authoritative_refs` / `visible_refs_omitted`；§5.1 生产路径 task 哈希 =
`binding.content_hash()`；合并语义与调用方行序无关。

### 13.6 本轮验收门

| 完成标准 | 证据 | 结果 |
|---|---|---|
| 本片测试文件全绿 | `85 passed in 0.61s` | ✅ |
| 相关四文件全绿 | `358 passed in 9.18s` | ✅ |
| full_target 全绿 | `3097 passed, 2 skipped in 123.09s (0:02:03)` | ✅ |
| 旧协议字节不变（黄金测试未改） | 黄金 `a9aa2e7e…` / `801b8e39…` 逐位相等 | ✅ |
| ruff 无告警 | `ruff check <两文件>`：`All checks passed!` | ✅ |
