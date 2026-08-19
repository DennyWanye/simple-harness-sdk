<!-- plan-status: finalized -->

# Plan：双 SDK 日志补齐（可观测性）

> 唯一真相：`plans/2026-08-19-sdk-observability/acceptance.md` + `assurance-contract.json`（用户已批准）
> 涉及仓库：`simple-harness-sdk`（主战场）、`simple-harness-memory-sdk`（补漏）
> 路径：LEAN（单业务切面=可观测性，纯新增日志，不改行为）

## 主要矛盾

harness-sdk 的核心引擎几乎零日志。主要矛盾不是"日志写得美不美"，而是**关键路径有没有日志，且日志点锚在真实执行流上**——上一版 plan 用 grep 行号猜日志点位置，被 challenger 抓出多处错位（授权决策点、budget 决策点、provider 双收费路径、run 创建入口全部定位错）。本版每个日志点都锚定在**已读透的精确函数 + 判断条件**上，且用 caplog 行为测试（真触发路径断言 event 出现）而不是源码字符串匹配来锁住"日志点在正确位置"。

## 最佳实践调研（含本项目适配分析）

| 实践 | 来源 | 本项目适配 |
|------|------|-----------|
| **库用 stdlib logging，不用 structlog** | Python 库通用惯例 | **照用**。`logging.getLogger(__name__)` 零依赖；宿主 structlog 可经 structlog.stdlib 桥接。memory-sdk 已有 structlog，保持不动 |
| **event 命名 = `<模块>.<动作>` + 结构化字段** | 12-factor / observability | **照用**。stdlib：`logger.info("run.complete", extra={...})`；structlog：`logger.info("memory.recall", hits=...)`（kwargs，不是 extra=） |
| **caplog 行为测试锁位置，源码 AST 只锁"存在"** | docs-as-tests 同类 | **照用**。行为测试真触发路径断言 event 出现（防错位 FAIL-4）；AST 测试只防"删日志点"（防退化 FAIL-3）。两者分工，不混用 |
| **参数脱敏** | 日志安全惯例 | **改造用**。工具参数只打 keys；query 截断到 80 字符；不做通用脱敏框架 |

**放弃的备选**：统一 structlog（加依赖、与宿主冲突）；OpenTelemetry（超出需求）。

## 关联验收标准

L-AC-1..7，每 Task 标注覆盖。

## 文件影响清单

| 文件 | 职责 | 本次改动 |
|------|------|----------|
| `simple-harness-sdk/src/simple_harness/runtime/kernel.py` | Run 状态机 | run.start（_start_run）、run.complete/run.fail（_terminalize 入口）、reconcile.recovered（recover） |
| `simple-harness-sdk/src/simple_harness/execution/dispatch.py` | Provider 协调 | provider.invoked（invoke + reconcile_incomplete 两处）、provider.usage_untrusted、provider.charge_unknown、reconcile.unknown_settled |
| `simple-harness-sdk/src/simple_harness/tools/executor.py` | 工具执行 | tool.invoked、tool.authorized、tool.denied、tool.effect_settled |
| `simple-harness-sdk/src/simple_harness/execution/budget.py` | 预算 | budget.refused_on_unknown、budget.exceeded（authorize 的 raise 处） |
| `simple-harness-sdk/tests/unit/runtime/test_logging_observability.py` | **新建** | caplog 行为测试 + AST 存在性测试 + 脱敏断言 |
| `simple-harness-sdk/CHANGELOG.md` | 变更记录 | 日志补齐条目 |
| `simple-harness-memory-sdk/src/simple_harness_memory/features/retriever.py` | 检索 | memory.recall（query 截断、命中数、来源贡献，structlog kwargs） |
| `simple-harness-memory-sdk/CHANGELOG.md` | 变更记录 | recall 日志条目 |

## Complexity inventory

| 复杂度表面 | 本次是否新增 | 理由 / 绑定 |
|-----------|:---:|------------|
| 新依赖 | 否 | stdlib logging，零新依赖 |
| 新公共 API | 否 | 日志不是公开 API |
| 新持久化状态 | 否 | 日志是运行时输出 |
| 新配置项 | 否 | 不强制配置 logger |
| 新抽象层 | 否 | 不抽日志封装层 |
| 新后台任务 | 否 | — |
| 可复用已有实现 | 是 | memory-sdk 已有 structlog，只补点不改体系 |

## Assurance / 信任与失败边界

- Profile=standard；contract 见 `assurance-contract.json`。
- 入口链：日志点全部加在已验证的现有路径上，不新增路径。
- 敏感信息：工具参数只打 keys；query 截断 80 字符；不打 prompt/api_key 明文。
- 停止追踪点：宿主日志桥接（OOS-1）、采集/存储（OOS-2）。

## 任务清单（按依赖排序）

### Task 1 — harness-sdk Run 生命周期日志  [L-AC-1]
- 改动文件：`src/simple_harness/runtime/kernel.py`
- 现状（已读透）：
  - `_start_run`（1137）是 run 创建唯一入口：admission 拒绝在 1140 raise，创建在 1150 `create_with_start_snapshot` 后拿到 `created`。RunClient.start（539）只是薄包装，recover/spawn 子 run 不走它。
  - `_terminalize`（1569）是所有终态（COMPLETED/FAILED/CANCELLED）的入口，child 提前 return（1590）、continuation-ack return（1652）、root 主路径 `_terminal.commit` 都在**函数入口之后**才分叉——所以在入口处打日志能覆盖全部终态。workflow 取消走 `_terminalize_cancelled`（1521），不经过 `_terminalize`。
  - `recover`（860）是恢复入口，`list_recoverable_root_runs` + `list_recoverable_child_runs` 提供条数。
- 修改方式：
  - kernel.py 已有 `logger = logging.getLogger(__name__)`（66）
  - `_start_run` 在 `created` 拿到后（1150 之后）打 `logger.info("run.start", extra={"run_id": start.run_id.value, "session_id": start.execution_session_id.value, "profile": self._root_profile_key})`；admission 拒绝（1140）打 `logger.warning("run.admission_denied", extra={"run_id": ..., "reason": "admission_denied"})`
  - `_terminalize` 函数**入口第一行**打 `logger.info("run.complete" if state is RunState.COMPLETED else ("run.fail" if state is RunState.FAILED else "run.cancelled"), extra={"run_id": run.run_id, "state": state.value, "payload_keys": list(payload)[:10]})`
  - `recover` 统计条数后打 `logger.info("reconcile.recovered", extra={"roots": len(list_recoverable_root), "children": len(list_recoverable_child)})`
- 验证：caplog 跑一次 mock run 完成，断言 `run.start` + `run.complete` 出现且带 run_id
- 依赖：无

### Task 2 — harness-sdk Provider 调用日志  [L-AC-2]
- 改动文件：`src/simple_harness/execution/dispatch.py`
- 现状（已读透）：
  - `invoke`（242）是主收费路径，settle_succeeded 后返回 response。
  - `reconcile_incomplete`（409）是第二条收费路径（对账 COMPLETED），走 `_response_charge_for_record`（475）——**注意这里不比对 model**，所以 usage 信任判断只存在于 `_response_charge`（372）。
  - `_response_charge`（372）比对 `response.model == target.model`，不匹配返回 `BudgetCharge.unknown()`。
  - `_settle_unknown`（385）settle unknown 状态。
  - `response.usage` 可为 None（329-340 显式处理）。
- 修改方式：
  - 文件顶部加 `logger = logging.getLogger(__name__)`
  - `invoke` settle_succeeded 后打 `logger.info("provider.invoked", extra={"model": response.model, "input_tokens": (response.usage.input_tokens if response.usage else None), "output_tokens": (response.usage.output_tokens if response.usage else None), "total_tokens": (response.usage.total_tokens if response.usage else None)})`（None 守卫，B6）
  - `_response_charge`：model 不匹配且有 usage 时打 `logger.warning("provider.usage_untrusted", extra={"target_model": self._provider.target.model, "response_model": response.model})`；返回 unknown 时打 `logger.warning("provider.charge_unknown", extra={"model": response.model})`
  - `reconcile_incomplete` settle 后打 `logger.info("provider.invoked", extra={"reconcile": True, ...})`（第二条收费路径，B5）
  - `_settle_unknown` 打 `logger.warning("reconcile.unknown_settled", extra={"error_code": error_code})`
- 验证：mock provider 返回 model 不匹配，caplog 断言 `provider.usage_untrusted` 出现
- 依赖：无（与 Task 1 并行）

### Task 3 — harness-sdk 工具调用+授权+落账日志  [L-AC-3]
- 改动文件：`src/simple_harness/tools/executor.py`
- 现状（已读透）：
  - `execute`（203）有两个独立 deny 出口：**durable 决策**（293-316，`durable_decision.state` 非 ALLOWED 时 return rejected）与**即时授权**（324-342，`decision is not ALLOW` 时 return rejected）。
  - effect 落账在 `settle_effect`（445）。
- 修改方式：
  - 文件顶部加 `logger = logging.getLogger(__name__)`
  - effect 准备后（`_prepared` 返回 prepared）打 `logger.info("tool.invoked", extra={"tool": call.name, "args_keys": list(call.arguments)[:20]})`（只打 keys）
  - **两个 deny 出口都打 tool.denied**（B4 修复）：
    - durable 拒绝（308-316 的 `else` 分支）打 `logger.warning("tool.denied", extra={"tool": call.name, "reason": f"authorization_{durable_decision.state.value}", "path": "durable"})`
    - 即时拒绝（333 的 `decision is not ALLOW` 分支）打 `logger.warning("tool.denied", extra={"tool": call.name, "reason": authorization.reason_code or "authorization_denied", "path": "immediate"})`
  - ALLOW 通过（authorization_receipt_ref 确定后）打 `logger.info("tool.authorized", extra={"tool": call.name})`
  - `settle_effect`（445）后打 `logger.info("tool.effect_settled", extra={"tool": call.name, "effect_id": ...})`
- 验证：caplog 触发一次工具调用，断言 tool.invoked + tool.authorized + tool.effect_settled
- 依赖：无（与 Task 1/2 并行）

### Task 4 — harness-sdk 预算日志  [L-AC-4]
- 改动文件：`src/simple_harness/execution/budget.py`
- 现状（已读透）：决策在 `BudgetPolicy.authorize`（136）：`refuse_on_unknown and has_unknown_charge` → raise `BudgetUnknownError`（139）；hard_cap 超限 → raise `BudgetExceededError`（151）。字段是 `snapshot.committed_micros` / `snapshot.reserved_micros` / `self.hard_cap_micros`。
- 修改方式：
  - 文件顶部加 `logger = logging.getLogger(__name__)`
  - `raise BudgetUnknownError()`（139 处）前打 `logger.warning("budget.refused_on_unknown", extra={"committed_micros": snapshot.committed_micros, "reserved_micros": snapshot.reserved_micros, "hard_cap_micros": self.hard_cap_micros})`
  - `raise BudgetExceededError()`（151 处）前打 `logger.warning("budget.exceeded", extra={"committed_micros": snapshot.committed_micros, "reserved_micros": snapshot.reserved_micros, "reservation_micros": reservation_micros, "hard_cap_micros": self.hard_cap_micros})`
- 验证：caplog 触发 budget refuse，断言 budget.refused_on_unknown 且字段名正确
- 依赖：无

### Task 5 — memory-sdk recall 补漏  [L-AC-5]
- 改动文件：`src/simple_harness_memory/features/retriever.py`
- 现状（已读透）：`recall`（28）跑六路召回 + fuse + rerank，零日志。memory-sdk 用 structlog **kwargs**（不是 extra=）。
- 修改方式：
  - 文件顶部加 `logger = structlog.get_logger("simple_harness_memory.features.retriever")`
  - recall 末尾打 `logger.info("memory.recall", query=query[:80], hits=len(hits), vec=len(vec_items), fts=len(fts_items), facts=len(facts_items), entity=len(entity_items))`（structlog kwargs，query 截断 80 字符，B8/B10）
  - 空结果打 `logger.info("memory.recall_empty", query=query[:80])`
- 验证：触发 recall，断言 memory.recall + hits 字段
- 依赖：无（独立仓库，与 Task 1-4 并行）

### Task 6 — caplog 行为测试 + AST 存在性测试 + 脱敏断言（两仓各建测试文件）  [L-AC-6, L-AC-7]
- 改动文件（**拆两仓**，B12 修复）：
  - `simple-harness-sdk/tests/unit/runtime/test_logging_observability.py`（新建）：锁 harness 的 4 个关键函数 + 行为测试 + 脱敏断言
  - `simple-harness-memory-sdk/tests/unit/test_logging_observability.py`（新建）：锁 recall 的 AST 存在性 + query 截断断言
- 修改方式（分工明确）：
  - **行为测试（防错位 FAIL-4，harness-sdk 侧）**：caplog 真触发路径（跑 mock run / 调 tool / 触发 budget refuse），断言对应 event 出现——即 TO-L1..4 的实际测试，放 harness-sdk 测试文件
  - **AST 存在性测试（防删除 FAIL-3）**：
    - harness-sdk 侧：读 `_terminalize`/`_response_charge`/`execute`/`authorize` 源码，断言体内含 `logger.` 调用
    - memory-sdk 侧：读 `retriever.py` 的 `recall`，断言体内含 `logger.` 调用（不跨仓引用 harness 测试文件）
  - **脱敏断言（防泄露 FAIL-2，B10 修复）**：
    - harness-sdk 侧：断言源码日志调用不含 `api_key`/`prompt`/`secret` 字段名；工具参数只打 keys（行为测试断言 args_keys 不是 args 值）
    - memory-sdk 侧：**断言 recall 的 query 字段被截断**（源码含 `query[:80]` 或等价的截断表达式），防止未来把截断改回全量
- 验证：两仓 pytest 全绿；故意删一处日志点，AST 测试必 FAIL（负向自检后还原）；故意把 tool.denied 移到错误分支，行为测试必 FAIL（负向自检后还原）；把 query[:80] 改回 query，memory 侧截断断言必 FAIL（负向自检后还原）
- 依赖：Task 1-5

### Task 7 — CHANGELOG + 全量回归  [L-AC-7]
- 改动文件：两仓库 CHANGELOG
- 验证：两 SDK 全量 pytest 0 新增失败（harness-sdk 基线 16 failed/1191 passed；memory-sdk 基线 54 passed/2 skipped）
- 依赖：Task 1-6

## 回滚
纯新增日志点 + 一个测试文件，`git revert` 即回滚，无行为影响。
