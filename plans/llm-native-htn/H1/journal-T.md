# H1-T 实施日志：绑定已有目标与提出后继任务降为只解码

## 1. 裁定与范围

依据 2026-09-19 15:20 追加裁定：`BIND_EXISTING_GOAL` 与
`REPAIR/PROPOSE_SUCCESSOR` 在本阶段只解码、往返、持久化，不执行；准入层在
“类型是否启用”阶段返回 `DECISION_NOT_ENABLED_IN_PHASE`。裁定之前的包绑定、修订号、
主题与引用检查顺序保持不变。

本片只改白名单内的合同启用状态、既有测试、提示词 v8、提示词冻结指纹、三个受影响的
`.expect.json`，以及本日志。格式文件与 `valid/` 样例未改。

## 2. 实施结果

- `H1_DECISION_ENABLEMENT` 只把 `REPAIR/PROPOSE_SUCCESSOR` 与
  `BIND_EXISTING_GOAL` 改为 `decodable=True, admissible=False, executable=False`。
- 准入实现原有第 6 阶段直接读取 `enabled_decision_types`，无需改动生产逻辑；新增/更新用例
  钉住两类决定在该阶段拒绝，并钉住该阶段之前的错误仍优先返回。
- 请求包继续从 `H1_DECISION_ENABLEMENT` 的 `executable` 项派生；测试逐项断言剩余 8 个
  启用表条目状态不变，并断言请求包不包含两类决定。
- 提示词 v8 删除 `BIND_EXISTING_GOAL` 的类型与载荷诱导；v8 文本不含
  `BIND_EXISTING_GOAL`、`PROPOSE_SUCCESSOR`、`goal_ref`、`resolution_ref`。其余提示词
  文本保持不变。
- 新冻结指纹：`planner-hierarchical-v8` 的 UTF-8 `instructions` sha256 为
  `90c8b9f0551b98e299f1c11f90930f2b77b46e83397caaaa0f99617381c93e1c`，长度为 `2459`。
  `plans/llm-native-htn/H1/prompt-v8.md` 的全文块与代码逐字一致。
- 新增编解码往返测试：两类决定的三个合法样例仍可解码，序列化后再次解码保持一致。

## 3. 受裁定影响的反例期望

以下三个 `.expect.json` 只改 `expected_code`；决定本体、格式文件与合法样例均未改。

| 文件 | 原期望 | 新期望 | 原因 |
|---|---|---|---|
| `invalid/obligation-not-open.expect.json` | `OBLIGATION_NOT_OPEN` | `DECISION_NOT_ENABLED_IN_PHASE` | `REPAIR/PROPOSE_SUCCESSOR` 在类型启用阶段已被拒绝，不能到达载荷阶段 |
| `invalid/refinement-cycle.expect.json` | `REFINEMENT_CYCLE` | `DECISION_NOT_ENABLED_IN_PHASE` | 同上；类型启用检查先于结构载荷检查 |
| `invalid/reuse-not-allowed.expect.json` | `REUSE_NOT_ALLOWED` | `DECISION_NOT_ENABLED_IN_PHASE` | `BIND_EXISTING_GOAL` 在类型启用阶段已被拒绝，不能到达复用检查 |

三类下游拒绝码仍由准入单元测试在显式启用对应行的测试上下文中覆盖；它们不再由 H1-F
夹具声明为默认阶段的首个拒绝码。

## 4. 测试记录

测试先行的首轮结果（实现尚未修改）：

```text
6 failed, 416 passed in 2.13s
```

相关定向测试：

```text
422 passed, 3 skipped in 0.89s
```

Schema 与准入定向回归：

```text
284 passed, 3 skipped in 0.38s
```

全 `tests/orchestrator/full_target`：

```text
3643 passed, 5 skipped in 128.81s (0:02:08)
```

旧模式回归（`step02`、`step05`、`step06`、`step07`、`p34`、`p35`）：

```text
560 passed, 13 skipped in 212.30s (0:03:32)
```

`ruff`：

```text
All checks passed!
```

## 5. 自我批评

本片没有 UI 或真实 provider 入口；手工验收门不适用。全量回归中首次发现的是夹具覆盖元测试
与新裁定不一致，已按事实修正测试而未掩盖失败；其余流程检查未发现空转门。
