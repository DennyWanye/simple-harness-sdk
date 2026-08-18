<!-- plan-status: finalized -->

# Plan：simple-harness-sdk 0.1.3 — 消费者层两个设计缺陷修复

> 唯一真相：`plans/2026-08-19-sdk-consumer-0.1.3/acceptance.md` + `assurance-contract.json`（用户已批准 2026-08-19）
> 仓库：`simple-harness-sdk`（代码 + 发布仅此仓库）
> 档位：M（单垂直切面——消费者适配层，≤3 个源文件 + 测试/文档；无 UI、无状态机、无 provider 新增）

## 主要矛盾

消费者适配层是 SDK 对外的门面，但它把两个"消费者必须能自主决定"的维度**硬编码死**：
① LLM model 名钉死 `"consumer-model"`，导致真实消费者报 usage 时永远 `unknown charge` → 两轮 run 必被拒；
② 工具 input schema 钉死"空 properties + 禁额外字段"，导致任何带参工具调用必被拒。
两个缺陷同根：**adapter 层把本应由消费者声明的运行时事实写成了常量**。修法一致——把这两个维度变成
`ConsumerRuntimePorts` 的可声明字段（带向后兼容默认值），adapter 从字段取，不再硬编码。

## 关联验收标准

C-AC-1..6，每个 Task 标注覆盖。

## 文件影响清单

| 文件 | 职责 | 本次改动 |
|------|------|----------|
| `src/simple_harness/runtime/consumer_adapter.py` | 消费者适配层（唯一核心改动） | 加 2 个字段 + 修 2 处硬编码 |
| `src/simple_harness/runtime/ports.py` | 消费者 Protocol | 补 ProviderPort 的 model 回显契约（docstring + 示例修正 content= 签名） |
| `src/simple_harness/version.py` | 版本 | 0.1.2 → 0.1.3 |
| `tests/unit/runtime/test_consumer_adapter.py` | **新建**：覆盖 C-AC-1..5（含 mismatch 负向） | 新增 |
| `docs/api/ports.md` / `docs/api/runtime.md` / `docs/quickstart.md` | 文档 | 补 model 回显契约 + tool_schemas 用法 + 零成本 trusted usage 说明 |
| `scripts/verify_release_gate.sh` | 发布门脚本 | **0.1.2 → 0.1.3**（wheel 路径与 SHA 匹配串，challenger F2） |
| `CHANGELOG.md` | 变更记录 | 0.1.3 条目（含零成本 trusted usage 语义，challenger F6） |

## 已核实的关键事实（2026-08-19）

- `consumer_adapter.py:138-144`：`_ConsumerProviderAdapter.__init__` 硬编码
  `ProviderTarget(model="consumer-model", ...)`。
- `consumer_adapter.py:169-201`：`_ConsumerToolExecutorAdapter.build_registry()` 生成
  `input_schema={"type":"object","properties":{},"additionalProperties":False}`。
- `execution/dispatch.py:378`：`_response_charge` 仅在 `response.model == self._provider.target.model`
  时信任 usage，否则 `BudgetCharge.unknown()`；`budget.py:139` `refuse_on_unknown` 触发拒绝
  （`react_cost_exceeded`）。
- `consumer_adapter.py:56-90`：`ConsumerRuntimePorts` 现有字段 provider/tool_executor/authorization/
  database_path/tool_names/max_turns/max_tool_calls/owner_id，均带默认或无默认。
- `consumer_adapter.py:270-304`：`build_consumer_runtime` 现构造 `_ConsumerToolExecutorAdapter(ports.tool_executor, ports.tool_names)`
  与 `_ConsumerProviderAdapter(ports.provider)`——两处需注入新字段。
- `_ConsumerProviderAdapter` 的 `FrozenPriceEstimator("consumer-v1", "consumer", 0, 0)`（零价）不在本次范围：
  本次只让 usage **被信任**（不再 unknown 拒绝），价格模型留作后续。

## 任务清单（按依赖排序）

### Task 1 — 加 `model` 字段并修 ProviderTarget  [C-AC-1, C-AC-2]
- 改动：`consumer_adapter.py`
  - `ConsumerRuntimePorts` **末尾**（`owner_id` 之后，challenger F5 位置参数兼容约束）加 `model: str = "consumer-model"`
  - `_ConsumerProviderAdapter.__init__(self, consumer_port: ProviderPort, model: str)`，`target` 里 `model=model`
  - `build_consumer_runtime` 里 `_ConsumerProviderAdapter(ports.provider, ports.model)`
- 验证：构造 `ConsumerRuntimePorts(model="gpt-4o", ...)` → adapter.target.model=="gpt-4o"
- 依赖：无

### Task 2 — 加 `tool_schemas` 并保持 fail-closed 默认  [C-AC-3, C-AC-4]
- 改动：`consumer_adapter.py`
  - `ConsumerRuntimePorts` **末尾**加 `tool_schemas: Mapping[str, dict] = field(default_factory=dict)`
  - `_ConsumerToolExecutorAdapter.__init__(..., tool_schemas: Mapping[str, dict])`
  - `build_registry()`：`input_schema = self._tool_schemas.get(name)` 若存在；否则**保持现状**
    `{"type":"object","properties":{},"additionalProperties":False}`（fail-closed 默认，无 schema 工具只能无参调用）
  - `build_consumer_runtime` 里 `_ConsumerToolExecutorAdapter(ports.tool_executor, ports.tool_names, ports.tool_schemas)`
- 依据（challenger F1）：`schema.py:209-213` 禁止 `additionalProperties != False`，故不得用宽松默认——带参工具必须由消费者提供闭合 schema
- 验证：带闭合 schema 的工具带参调用通过；无 schema 工具无参调用通过；无 schema 工具带参调用仍被拒（fail-closed）
- 依赖：无（与 Task 1 并行）

### Task 3 — 版本 0.1.3 + CHANGELOG  [C-AC-6]
- 改动：`version.py`（0.1.2→0.1.3）、`CHANGELOG.md`（0.1.3 条目：两个缺陷修复 + 向后兼容说明 +
  **零成本 trusted usage 语义**——匹配 model 的 usage 现以 `FrozenPriceEstimator(...,0,0)` 零成本入账，challenger F6）
- 验证：`python -c "import simple_harness.version; assert __version__=='0.1.3'"`（源码树内）
- 依赖：无

### Task 4 — 测试  [C-AC-1..5]
- 改动：`tests/unit/runtime/test_consumer_adapter.py`（新建）
  - `test_model_field_reaches_provider_target`：adapter.target.model == 声明值（C-AC-1）
  - `test_usage_trusted_when_model_matches`：mock provider 返回 `ProviderResponse(model="gpt-4o", usage=...)`，
    两轮 run 不因 `react_cost_exceeded` 拒绝（C-AC-2）
  - `test_model_mismatch_still_unknown_and_refuses`：**负向**（challenger F4）——provider 回显不同 model 时，
    usage 记 unknown、run 被拒；证明 `response.model == target.model` 信任链未被放宽（FAIL-2/TO-R1）
  - `test_tool_with_schema_accepts_arguments`：闭合 schema 工具带参调用到达 executor（C-AC-3）
  - `test_tool_without_schema_noarg_only`：无 schema 工具无参成功、带参被拒（C-AC-4，fail-closed）
  - `test_legacy_ports_without_new_fields_still_build`：不加 model/tool_schemas 也能 build（C-AC-5）
- 验证：新测试文件全绿；SDK 全量 pytest 无回归（基线 16 failed / 1183 passed，不新增）
- 依赖：Task 1、2

### Task 5 — 文档  [C-AC-5, DoD]
- 改动：`docs/api/ports.md`（ConsumerRuntimePorts 字段表补 model/tool_schemas）、
  `docs/api/runtime.md`、`docs/quickstart.md`（示例演示声明 model + 闭合 tool schema）；
  `ports.py` 的 ProviderPort docstring 修正（**model 回显契约** + 过时 `content=` 签名改为当前签名，challenger F3）
- 验证：文档字段名与代码一致（grep 核对）；ProviderPort docstring 示例可跑
- 依赖：Task 1、2

### Task 6 — 发布验证  [C-AC-6, DoD]
- 改动：重建 wheel（`uv build` 或 hatch）；**`scripts/verify_release_gate.sh` 的 0.1.2→0.1.3**（challenger F2）；
  跑该脚本（消费者 conformance provider/tool 两 suite）
- 验证：SDK 全量 pytest 无回归；release-gate RESULT: PASS；`dist/SHA256SUMS` 更新为 0.1.3 wheel 的 SHA
- 依赖：Task 3、4、5

## Assurance / 信任与失败边界

- Profile=standard；contract 见 `assurance-contract.json`。
- 关键不变量（FAIL-2）：`execution/dispatch.py:378` 的 `response.model == target.model` **不得改动**——本次只让
  `target.model` 来源从硬编码变为消费者声明，信任链本身不动。
- 回滚：纯新增字段 + 默认值，回滚 = revert 提交；无数据迁移。

## 执行顺序

Task 1/2/3 并行 → Task 4 → Task 5 → Task 6。本仓库内提交（conventional commits，Co-Authored-By 规范）。
