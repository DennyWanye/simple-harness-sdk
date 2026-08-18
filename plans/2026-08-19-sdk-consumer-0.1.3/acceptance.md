# 验收标准：simple-harness-sdk 0.1.3 — 消费者层两个设计缺陷修复

> 状态：DRAFT（待用户确认）
> 仓库：`simple-harness-sdk`（代码改动 + 发布仅在此仓库；宿主 re-vendor 是独立后续，明确不包含）
> 来源：2026-08-19 SDK 易用性优化 program 中 Slice 2/3 执行暴露的两个真实缺陷（已在 quickstart/CHANGELOG 文档化为待修项）

## 范围

**包含**（single slice，改动集中在消费者适配层）：

- **D1 — ProviderTarget model 硬编码**：`runtime/consumer_adapter.py:_ConsumerProviderAdapter` 硬编码
  `ProviderTarget(model="consumer-model")`。kernel 仅在 `response.model == target.model` 时信任 usage
  （`execution/dispatch.py:378`），否则记 `BudgetCharge.unknown()` → 拒绝后续轮。真实消费者返回自己的
  model 名时，任何报 usage 的两轮以上 run 必挂。
- **D2 — 占位 tool spec 拒绝参数**：`_ConsumerToolExecutorAdapter.build_registry()` 生成
  `input_schema={"type":"object","properties":{},"additionalProperties":False}`，空 properties + 禁额外
  字段 = 拒绝一切 arguments。消费者工具实际无法带参调用。
- 版本 0.1.2 → 0.1.3，重打 wheel，conformance + release-gate 复跑。

**明确不包含**：
- 宿主 `simple_harness` re-vendor 0.1.3（独立后续 program）
- kernel 的 usage-trust 机制改动（保持 `response.model == target.model` 不变，只修消费者侧 target 来源）
- 消费者 SDK 之外的新功能

## 功能验收条款

| ID | 功能点 | 验收条件（可验证） | 优先级 |
|----|--------|-------------------|--------|
| C-AC-1 | 消费者声明 model | `ConsumerRuntimePorts` 新增 `model: str` 字段（默认 `"consumer-model"` 向后兼容）；`_ConsumerProviderAdapter` 的 `target.model` 取自该字段 | 必须 |
| C-AC-2 | usage 信任打通 | 消费者声明 `model="gpt-4o"` 且其 `ProviderPort` 以非 None 回显相同 `ProviderResponse.model` 时，两轮 run 不被 unknown charge 拒绝（`react_cost_exceeded` 不再出现）；**契约要求 provider 回显声明的 model** | 必须 |
| C-AC-3 | 工具参数 schema | 消费者可为每个工具提供**闭合** input_schema（`ConsumerRuntimePorts` 新增 schema 入口）；带参数的 `ToolCall` 能通过校验并到达 `ToolExecutorPort.execute` | 必须 |
| C-AC-4 | 无 schema 工具无参可用 | 未提供 schema 的工具（仅 `tool_names`）仍能无参调用；默认 schema 保持 fail-closed 空 properties（这是安全默认，不要求"默认接受任意参数"） | 必须 |
| C-AC-5 | 向后兼容 | 0.1.2 的公开消费者 API（`ConsumerRuntimePorts` 既有字段、`build_consumer_runtime`、3 个 Protocol）不破坏；既有 minimal-consumer 不改动也能跑通 | 必须 |
| C-AC-6 | 版本与发布 | `version.py` = 0.1.3；wheel 重新构建；SDK 自身全量 pytest + 消费者 conformance（`verify_release_gate.sh`，含 provider/tool suite）均 PASS | 必须 |

## 非功能 / 边界

- **向后兼容**：`model` 与 schema 入口都有默认值，0.1.2 消费者代码零改动可升级
- **错误态**：`model` 为非法值（空串等）时 fail-closed（拒绝构造或拒绝 usage），不得静默放宽
- **文档**：`docs/api/ports.md` / `docs/api/runtime.md` / quickstart 同步 model 与 tool schema 用法
- **测试**：新增消费者 adapter 层单测覆盖 C-AC-1..4；既有 SDK 全量测试无回归

## Assurance contract 摘要

- **Profile**：standard
- **受保护资产**：ASSET-1 消费者公开 API 稳定性（0.1.2→0.1.3 不破坏）；ASSET-2 usage 信任链（不得因修复而放宽对 usage 的验证）
- **可信假设**：TRUST-1 消费者按文档声明 model 与 schema；TRUST-2 本机可构建 wheel + 建干净 venv
- **范围内失败**：FAIL-1 model 声明不生效（target.model 仍硬编码）；FAIL-2 usage 信任被放宽（去掉 `response.model==target.model` 检查）；FAIL-3 schema 入口不生效（带参调用仍被拒）；FAIL-4 向后兼容破坏
- **明确范围外**：OOS-1 宿主 re-vendor；OOS-2 kernel usage 机制重构；OOS-3 非 macOS 平台
- **最大可接受影响**：消费者集成失败需人工排查；不得造成多扣费/少扣费

## 测试场景矩阵

`input_sensitive=false`（库 API 改动，验证走确定性 mock provider + conformance，非 LLM 语义功能），不设输入语义矩阵。
`stateful_init=false`（无异步注册服务/登录态依赖），无冷启动场景。

## 测试义务矩阵

| obligation_id | type | ac_id | risk | min_decisive_test | required_reason |
|---------------|------|-------|------|-------------------|-----------------|
| TO-C1 | delivery | C-AC-1 | — | 断言 adapter.target.model == 声明值 | 证明 model 字段生效 |
| TO-C2 | delivery | C-AC-2 | — | mock provider 报 usage，两轮 run 不 refute | 证明 usage 信任打通 |
| TO-C3 | delivery | C-AC-3 | — | 带参数 ToolCall 到达 execute | 证明 schema 生效 |
| TO-C4 | delivery | C-AC-4 | — | 无 schema 工具无参调用成功 | 证明默认宽松 |
| TO-C5 | delivery | C-AC-5 | — | 既有 minimal-consumer 不改跑通 | 证明向后兼容 |
| TO-C6 | delivery | C-AC-6 | — | conformance + release-gate PASS | 证明发布完整 |
| TO-R1 | change-risk | — | FAIL-2 | 断言 usage-trust 条件仍含 `response.model==target.model` | 防止修复放宽信任链 |

## 完成的定义（DoD 摘要）

1. 6 条 C-AC 全部通过测试
2. 所有 delivery/change-risk obligation 有对应 PASS testcase
3. `simple-harness-sdk` git status 干净、CHANGELOG 更新、version 0.1.3
4. SDK 全量 pytest + 消费者 conformance（release-gate）PASS
5. gate finalize exit 0，receipt 入账
