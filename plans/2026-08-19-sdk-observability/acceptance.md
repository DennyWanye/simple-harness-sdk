# 验收标准：双 SDK 日志补齐（可观测性）

> 状态：DRAFT（待用户确认）
> 涉及仓库：`simple-harness-sdk`（主战场，几乎裸奔）、`simple-harness-memory-sdk`（有地基需补漏）
> 目标：通过收集日志判断 SDK 当前有什么问题，方便后续优化与 bug 查找

## 范围

**包含**：
- **Slice 1 — harness-sdk 关键路径日志**（当前仅 2 个文件 6 处异常日志，核心状态机全黑）：
  - Run 生命周期、Provider 调用（含 usage 信任/unknown charge）、工具调用+授权、恢复对账、预算
  - 用 stdlib `logging`（库的最佳实践：零依赖、宿主可用标准 logging 机制接管），一致的 event 命名 + 结构化字段
- **Slice 2 — memory-sdk 补漏**（已有 structlog，补核心检索路径）：
  - recall 检索（query、命中数、RRF 各路贡献、命中来源）、embed（维度、hash fallback）、world model 基础生命周期
- **Slice 3 — 约定 + 防退化**：
  - event 命名约定（`<模块>.<动作>`）+ "关键路径必有日志"的测试，防止未来又退化成裸奔

**明确不包含**：
- 宿主侧如何消费/聚合 SDK 日志（simple_harness 后端的日志桥接是独立后续）
- 日志采集/传输/存储基础设施（ELK 等）
- 日志脱敏框架的全面实现（本次只保证"参数脱敏"不泄露敏感值，不做通用脱敏库）

## 功能验收条款

| ID | 功能点 | 验收条件（可验证） | 优先级 |
|----|--------|-------------------|--------|
| L-AC-1 | harness Run 生命周期日志 | 用 stdlib logging，一次完整 run 走完（start→complete 或 start→fail）时，日志含 `run.start` / `run.complete`（或 `run.fail`），且带 `run_id`、`session_id`、`profile`、失败时含终止原因 | 必须 |
| L-AC-2 | harness Provider 调用日志 | 每次 provider 调用打 `provider.invoked`（model、token 用量、耗时）；当 `response.model != target.model` 时打 `provider.usage_untrusted`（声明 model vs 实际 model）；当 charge 为 unknown 时打 `provider.charge_unknown` | 必须 |
| L-AC-3 | harness 工具调用+授权日志 | 工具调用打 `tool.invoked`（name、参数脱敏）；授权通过打 `tool.authorized`、拒绝打 `tool.denied`（reason）；effect 落账打 `tool.effect_settled` | 必须 |
| L-AC-4 | harness 恢复对账+预算日志 | 重启恢复打 `reconcile.recovered`（恢复条数）；unknown settle 打 `reconcile.unknown_settled`；预算拒绝打 `budget.refused_on_unknown` / 超限打 `budget.exceeded` | 必须 |
| L-AC-5 | memory recall/embed 补漏 | `memory.recall` 打 query（截断 80）、命中数、RRF 各路贡献、命中来源（vec/fts/facts/entity）；embedder 选择与 hash 回退由既有 `memory.embedder_selected`（含 dim）与 `memory.embedder_fallback_to_hash` 观测（本 program 补齐 bge 分支的 dim 字段并加测试锁） | 必须 |
| L-AC-6 | 约定 + 防退化 | event 命名遵循 `<模块>.<动作>`；有一个测试断言"L-AC-1..5 的关键路径在源码里有对应日志点"，防止未来删日志而测试不知 | 必须 |
| L-AC-7 | 不改变行为 | 两个 SDK 全量测试回归 0 新增失败；日志是可观测性纯新增，不改变任何公开 API/协议/持久化状态/依赖 | 必须 |

## 非功能 / 边界

- **库日志最佳实践**：harness-sdk 用 stdlib `logging.getLogger(__name__)`，不强制配置 handler/level（宿主用标准 logging 机制接管）；memory-sdk 保持现有 structlog 体系
- **参数脱敏**：工具参数、prompt 内容等敏感值不打全量，用脱敏/长度截断
- **性能**：日志调用不得引入明显开销（不在热路径做昂贵格式化；用惰性格式化）
- **兼容**：不引入新运行时依赖

## Assurance contract 摘要

- **Profile**：standard
- **受保护资产**：ASSET-1 两个 SDK 的既有行为（日志是纯新增观测，不得改变行为）；ASSET-2 日志不泄露敏感值
- **可信假设**：TRUST-1 宿主用标准 logging/structlog 机制能接管日志；TRUST-2 日志字段命名约定被遵守
- **范围内失败**：FAIL-1 日志改变了行为（回归新增红）；FAIL-2 日志泄露敏感值（全量打 prompt/key）；FAIL-3 关键路径仍无日志（防退化测试失效）；FAIL-4 日志点加错位置（打了但不在真正触发的路径上）
- **明确范围外**：OOS-1 宿主日志桥接；OOS-2 日志采集/传输/存储；OOS-3 通用脱敏框架
- **最大可接受影响**：日志信息不足需人工补日志；不得导致行为变化或敏感值泄露

## 测试场景矩阵

`input_sensitive=false`（纯可观测性改动，不产生/消费 LLM 语义结果）、`stateful_init=false`、`llm_payload_driven=false`——不设输入语义矩阵、无冷启动场景、无 LLM 变异清单。

## 测试义务矩阵

| obligation_id | type | ac_id | risk | min_decisive_test | required_reason |
|---------------|------|-------|------|-------------------|-----------------|
| TO-L1 | delivery | L-AC-1 | — | caplog 捕获一次 run 完成，断言 run.start/run.complete + run_id 字段 | 直接证明 run 生命周期日志存在 |
| TO-L2 | delivery | L-AC-2 | — | mock provider 返回 model 不匹配，断言 provider.usage_untrusted 出现 | 直接证明 usage 信任观测（对着 0.1.3 修的 bug 面） |
| TO-L3 | delivery | L-AC-3 | — | 触发一次工具调用，断言 tool.invoked + authorized/denied | 直接证明工具日志存在 |
| TO-L4 | delivery | L-AC-4 | — | 触发 budget refuse，断言 budget.refused_on_unknown | 直接证明预算日志存在 |
| TO-L5 | delivery | L-AC-5 | — | 触发 recall，断言 memory.recall + 命中数字段 | 直接证明 recall 日志存在 |
| TO-L6 | delivery | L-AC-6 | — | 跑防退化测试，断言关键路径源码含日志点 | 证明约定被机器守住 |
| TO-R1 | change-risk | L-AC-7 | FAIL-1 | 两 SDK 全量 pytest 0 新增红 | 证明日志纯新增不改行为 |
| TO-R2 | change-risk | L-AC-7 | FAIL-2 | 日志断言不含 prompt/api_key 明文 | 证明不泄露敏感值 |

## 完成的定义（DoD 摘要）

1. 7 条 L-AC 全过
2. 所有 delivery/change-risk obligation 有对应 PASS testcase
3. 两个 SDK 仓库各自 git 干净、各自提交
4. harness-sdk CHANGELOG 记录日志补齐
5. gate finalize exit 0（两仓库各一个 run-dir 或单一 run-dir + 两仓库提交）
