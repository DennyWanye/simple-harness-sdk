# Plan：simple-harness-sdk 0.1.4 发布阻断收尾（H1）

## 主要矛盾

决定成败的核心问题：**在不动摇 0.1.3 消费者公开 API（向后兼容）的前提下，把 consumer facade 从"能跑"推到"不假投递、不丢上下文、不泄漏连接、发布可审计"。**
所有改动集中在 `runtime/consumer_adapter.py`、`execution/delivery.py`、`execution/sqlite/*`、`execution/uow.py`、`runtime/kernel.py`（回归测试）与 `.github/workflows/*`、`scripts/verify_release_gate.sh`。风险最高的是 Task 1（delivery 语义）、Task 4（DB 生命周期所有权）与 Task 6（release 管线）——这三处一旦做错会破坏信任链、泄漏连接或使发布门失效。

## 关联验收标准

覆盖 H-AC-1（Task 1）、H-AC-2（Task 2）、H-AC-3（Task 3）、H-AC-4（Task 4）、H-AC-5（Task 5）、H-AC-6（Task 6）、H-AC-7（Task 7）、H-AC-8（Task 8）。

## 文件影响清单

| 文件 | 职责 | 本次改动 |
|------|------|----------|
| `src/simple_harness/runtime/consumer_adapter.py` | consumer → kernel 桥接 | Task 1/2/3/4：删 `_DefaultDeliverySink`、加 `delivery_sinks` 参数、传真实 ToolContext、标 demo 边界 |
| `src/simple_harness/execution/delivery.py` | delivery 状态机 | Task 1：允许空 sinks（无 sink → 永不 claim，保持 PENDING） |
| `src/simple_harness/execution/sqlite/uow.py` | SQLite uow | Task 4：加幂等 `close()` |
| `src/simple_harness/runtime/kernel.py` | Runtime + RuntimeUnitOfWork 协议（kernel.py:469） | Task 4：协议加 `close()`；`close()` 在 NEW 态与正常态都关 uow |
| `src/simple_harness/runtime/ports.py` | 公开 Port 协议 | Task 8：Memory Port 标 reserved |
| `src/simple_harness/testing/` | 测试命名空间 | Task 1：新增 `NoopDeliverySink` 测试夹具 |
| `.github/workflows/ci.yml` / `release.yml` / `release-candidate-conformance.yml` | CI/发布 | Task 6/7：全量 pytest、ruff/mypy、release 依赖、消硬编码版本 |
| `scripts/verify_release_gate.sh` | 发布 gate 脚本 | Task 7：消 8 处 `0.1.3` 硬编码 |
| `CHANGELOG.md` / `docs/api/` | 文档 | Task 3/8 + 收尾 |
| `src/simple_harness/version.py` | 版本单一来源 | Task 7/收尾：`0.1.4` |

## Complexity inventory

| 复杂度表面 | 本次是否新增 | 理由 / 绑定 |
|-----------|:---:|------------|
| 新依赖 | 否 | ruff/mypy 仅 dev 依赖 |
| 新公共 API | 是 | `build_consumer_runtime(delivery_sinks=...)` 新参数（H-AC-1）；`RuntimeUnitOfWork.close()` 可选 hook（H-AC-4） |
| 新持久化状态 | 否 | — |
| 新配置项 | 否 | — |
| 新抽象层 | 否 | 复用现有 DeliverySink / Database.close / ToolContext 字段 |
| 新后台任务 | 否 | — |
| 可复用已有实现 | `DeliveryDispatcher` / `Database.close` / `ToolContext` | Task 1/2/4 复用 |
| 标准库能力 | `workflow_call` reusable workflow | Task 6 解决 `needs` 无法引用手动 workflow |

## Assurance / 信任与失败边界

- Profile：standard（见 assurance-contract.json）。
- 入口链：consumer 调 `build_consumer_runtime` → adapter 桥接 → kernel Runtime。
- trust boundary：consumer 提供的 `ToolExecutorPort` / `ProviderPort` / `AuthorizationPort` 是外部代码；SDK 必须 fail-closed 地对待其返回值。
- 范围内失败：FAIL-1 假 DELIVERED / FAIL-2 ToolContext 空 / FAIL-3 连接泄漏 / FAIL-4 二次异常回归 / FAIL-5 向后兼容破坏 / FAIL-6 硬编码版本。
- 停止追踪点：不做 Memory Port 实际接线（OOS-3）；不做全项目 strict mypy（OOS-2）；不做 byte-for-byte reproducible（OOS-4）。

---

## 任务清单（按依赖排序）

### Task 1 — 消除假投递（delivery sink 显式注入）  [H-AC-1]
- 改动文件：`execution/delivery.py`、`runtime/consumer_adapter.py`、`testing/`、新增测试
- 现状：`_DefaultDeliverySink.deliver`（consumer_adapter.py:249-253）为 `pass`；`build_consumer_runtime` 行 333 `DeliveryDispatcher(uow, {"consumer": _DefaultDeliverySink()})`；`DeliveryDispatcher.__init__` 对空 sinks 抛 ValueError。
- 修改方式：
  1. `execution/delivery.py`：把 `if not self._sinks or any(...)` 的"空 sinks 抛错"改为仅拒绝"空 key"；空 sinks 合法化，文档注明"无 sink → `run_once` 永不 claim，delivery 保持 PENDING"。
  2. `consumer_adapter.py`：删除 `_DefaultDeliverySink` 类；`build_consumer_runtime` 新增参数 `delivery_sinks: Mapping[str, DeliverySink] | None = None`；`delivery=DeliveryDispatcher(uow, dict(delivery_sinks or {}))`。
  3. `testing/`：新增 `NoopDeliverySink`（`async def deliver(...): return None`），docstring 声明"仅测试用，不得用于生产投递；注意 sink 返回 None 且不抛异常会被记 DELIVERED"。
- 验证：新测试断言 ① 注入 sink 返回 → `complete_delivery` 记录 DELIVERED；② sink 抛异常 → `release_delivery` **置 PENDING** 且可被再次 claim（注：`release_delivery` 的 settle 目标态是 PENDING，非 RELEASED）；③ 空 sinks → `run_once()` 返回 False、无 delivery 被标记 DELIVERED。
- 依赖：无

### Task 2 — ToolContext 真实传递  [H-AC-2]
- 改动文件：`runtime/consumer_adapter.py`、新增测试
- 现状：handler 行 199-203 `call = ToolCall(context.call_id, tool_name, arguments); return await self._port.execute(call, {})`——`context` 是 SDK `ToolContext`（含 `run_id`/`request_id`/`call_id`/`cancellation`/`metadata`），但 `execute` 第二参传 `{}`。
- 修改方式：把 `{}` 改为真实字典：
  ```python
  tool_ctx = {
      "run_id": str(context.run_id),
      "request_id": str(context.request_id),
      "call_id": str(context.call_id) if context.call_id else None,
  }
  return await self._port.execute(call, tool_ctx)
  ```
- 验证：consumer 的 `execute` 断言收到的 `context` 是 dict 且 `context["run_id"]` 非空、`context["call_id"] == str(call.call_id)`（`CallId` 是 frozen dataclass，需 `str()` 后再比较）。
- 依赖：无

### Task 3 — facade 边界标注  [H-AC-3]
- 改动文件：`runtime/consumer_adapter.py`、`CHANGELOG.md`、`docs/api/runtime.md`（或 `docs/consumers/`）
- 现状：`build_consumer_runtime` docstring 宣称 "main entry point for external consumers"，未声明 demo 局限。
- 修改方式：模块 docstring + `build_consumer_runtime` docstring 增补：本 facade 为 demo/basic，不提供生产级 delivery/memory/真实计费（`FrozenPriceEstimator(0,0)`、reconciliation 写死 STILL_UNKNOWN）；生产消费者应自行组装 `RuntimePorts`。不改任何行为。
- 验证：CHANGELOG/docstring 含"demo/basic""自行组装 RuntimePorts"声明（TO-H3）。
- 依赖：无

### Task 4 — 数据库生命周期关闭  [H-AC-4]
- 改动文件：`execution/sqlite/uow.py`、`runtime/kernel.py`（RuntimeUnitOfWork 协议 + Runtime.close）、新增测试
- 现状：`build_consumer_runtime` 行 294 `Database.open(...)`；`SqliteExecutionUnitOfWork` 无 `close()`；`RuntimeUnitOfWork` 协议定义在 `runtime/kernel.py:469`（非 execution/uow.py）；`Runtime.close()`（kernel.py:944）不关 DB；NEW 态（未 start 即 close）提前 return（行 947-949）泄漏"构建后未启动"的 DB。
- 修改方式（challenger 建议：DB 归 uow 所有、Runtime 通过声明式协议方法关闭，不用 getattr duck-type）：
  1. `sqlite/uow.py`：`SqliteExecutionUnitOfWork` 加幂等 `close()` → `self._database.close()`（已关则跳过）。
  2. `runtime/kernel.py` `RuntimeUnitOfWork` 协议（行 469）加 `close()` 方法，用**默认体** `def close(self) -> None: return None`（可选、不破坏 repo 外实现；`...` 体会让 close 变 required）；`SqliteExecutionUnitOfWork` 覆写为真实关闭（幂等）。已核实 `SqliteExecutionUnitOfWork` 是传给 `Runtime`/`build_runtime` 的唯一 uow（全部 22 处调用点），无需给 mock 补 no-op close。
  3. `kernel.py` `Runtime.close()`：直接调用 `self._uow.close()`（不 getattr）；**重构 NEW 态提前 return**，使 NEW 态也关闭 uow；关闭顺序：`_stop_background_tasks()`（停止 `_wake_drain_task`/`_delivery_pump_task`）→ release fence/lease → `self._uow.close()` → 置 CLOSED。
  4. 共享 uow 安全理由：`ChildSignalRuntime`（kernel.py:798）是被 `_drain_child_signals_once`（kernel.py:1073）被动 drain 的对象，**非后台任务**；close 安全的前提是 `_wake_drain_task` 已在上一步停止、不再触发 child-signal reconcile 触碰 uow。
- 验证：① `async with build_consumer_runtime(...)` 退出后 `Database.open(same_path)` 可再次打开；② 新增"构建后未 start 即 close"用例，断言 DB 已关闭（覆盖 NEW 态回归）；③ close 两次幂等不抛。
- 依赖：Task 1

### Task 5 — logger 二次异常回归测试  [H-AC-5]
- 改动文件：`tests/`（integration/runtime 或 unit），不动生产代码（bug 已在 main 修复）
- 现状：kernel.py:1454-1455 已用 `extra={"run_id": str(run_id)}`（commit 577ed87）。
- 修改方式：新增回归测试：构造一个必抛异常的 driver，断言 ① 无二次 TypeError（caplog 无 `_log() got an unexpected keyword argument`）② `read_run` 后 state == FAILED ③ 对外 payload 不含 `private_cause` ④ 日志含 `run_id` 且不含敏感值。
- 验证：测试绿。
- 依赖：无

### Task 6 — CI 全量 + 渐进 lint + release 依赖  [H-AC-6]
- 改动文件：`.github/workflows/ci.yml`、`.github/workflows/release.yml`、`pyproject.toml`
- 现状：ci.yml test job 仅 `pytest tests/artifact`；release.yml 不跑全量 pytest；pyproject 无 ruff/mypy 配置。
- 修改方式：
  1. **先本地跑全量 `pytest -q` 建立基线，并拍板 fixture 门机制**：`simple_harness_conformance` 等 marker 若能在 repo 内 host（`examples/minimal-consumer/conformance_host.py`）下通过 → CI 全量 pytest 提供该 host（PYTHONPATH 指向 repo 内 host），不 deselect；若某 marker 依赖 repo 外 fixture（relay/model）→ 该 marker 在 CI 中显式 deselect，改由 `test` job 的 conformance 步骤覆盖，并把 H-AC-6 的"全量 pytest"收窄为"全量减外部 fixture-gated"（回写 acceptance 备注并经用户确认——scope 细化，不静默）。
  2. `ci.yml` test job 改 `pytest -q`（全量，按上一步 host/deselect 决定）+ `ruff check` + scoped `mypy`（目标限定 `consumer_adapter.py`/`delivery.py`/`ports.py` 及本次改动文件）。
  3. `pyproject.toml` 加 `[tool.ruff]`/`[tool.mypy]` 配置 + dev 依赖。
  4. **release 依赖（challenger：`workflow_call` 输入只能是 scalar，无法传 wheel 文件）**：选**内联**方案——`release.yml` 新增 `test` job：checkout repo（取 conformance host 源码）+ `download-artifact`（同 run 的 `release-distributions` 制品，非 workflow_call 传文件）→ 安装 wheel → 跑全量 pytest + conformance（PYTHONPATH 指向 repo 内 host）。`publish` 的 `needs` 加 `test`。多平台 conformance 矩阵保留在独立 `release-candidate-conformance.yml`（手动候选验证）；release 门在 ubuntu 上跑内联 conformance（**把 H-AC-6 的"release-candidate conformance"收窄为 ubuntu-only 内联 conformance 属 scope 细化，与步骤 1 的 fixture-deselect 一样需经用户确认，不静默**）。
- 验证：CI 绿（全量 pytest + ruff 0 error + scoped mypy 0 error）；release publish 依赖链正确（含 conformance）。
- 依赖：Task 1/2/4

### Task 7 — 版本单一来源  [H-AC-7]
- 改动文件：`.github/workflows/release-candidate-conformance.yml`、`scripts/verify_release_gate.sh`
- 现状：release-candidate-conformance.yml 行 75 `assert __version__ == "0.1.1"`；`scripts/verify_release_gate.sh` **8 处**硬编码 `0.1.3`（行 21 的 wheel 文件名、行 56/65/67/89 等）。H-AC-7 的"workflow/脚本"范围，计划原只列了 workflow，漏了脚本。
- 修改方式：
  1. release-candidate-conformance.yml：版本断言改为从 `src/simple_harness/version.py` 单一来源读取（runpy 注入 EXPECTED_VERSION）。
  2. `verify_release_gate.sh`：wheel 文件名 glob 化 / 版本从 version.py 读取，消除 8 处 `0.1.3` 字面量。
- 验证：grep workflow + scripts/ 无 `"0.1.x"` 硬编码版本字面量；候选制品版本断言与 version.py 一致；verify_release_gate.sh 在 0.1.4 bump 后能找到正确 wheel。
- 依赖：无

### Task 8 — Memory Port 标 reserved  [H-AC-8]
- 改动文件：`runtime/ports.py`、`CHANGELOG.md`
- 现状：`MemoryQueryPort`（行 218）/`MemoryWritePort`（行 265）已声明但 Runtime 未接线。
- 修改方式：两个 Protocol 的 docstring 顶部加 "reserved — declared but not yet wired into Runtime; do not assume recall/working-memory is active" 声明；CHANGELOG 记录。
- 验证：docstring 含 reserved 声明（TO-H8）。
- 依赖：无

### 收尾 — 版本 bump + 全量回归 + conformance
- `version.py` → `0.1.4`；CHANGELOG 记录 8 项；`uv build` 重打 wheel；全量 pytest + `verify_release_gate.sh`（provider/tool conformance）PASS。

## Challenge findings 闭环（round 1 → 修订）

| finding | 严重度 | 处置 |
|---|---|---|
| release-cannot-needs-manual-conformance | P0 | Task 6 改为 `workflow_call`/内联 conformance，`publish` 依赖同文件 `test` job |
| release-gate-script-hardcodes-0.1.3 | P1 | Task 7 补 `scripts/verify_release_gate.sh` |
| runtime-close-new-state-skips-db-close | P1 | Task 4 改所有权边界 + 覆盖 NEW 态用例 |
| ci-full-pytest-flip-unde-risked | P1 | Task 6 加"先本地全量 pytest 基线"步骤 |
| task2-verification-compares-str-to-callid | P2 | Task 2 验证改 `str(call.call_id)` |
| task1-release-state-notation-wrong | P2 | Task 1 验证改"置 PENDING" |
| delivery-sink-none-return-footgun | P2 | Task 1 的 NoopDeliverySink docstring 注明（范围外，不改协议） |

## Challenge findings 闭环（round 2 → 修订）

| finding | 严重度 | 处置 |
|---|---|---|
| workflow-call-cannot-carry-wheel-artifact | P1 | Task 6 改内联 conformance（`download-artifact` 传 wheel，不走 workflow_call scalar 输入） |
| runtime-uow-close-still-duck-typed-and-file-mapping-wrong | P2 | Task 4 改声明式协议方法（`self._uow.close()`，不 getattr）；协议文件映射改 `runtime/kernel.py:469`；ChildSignalRuntime 更正为被动 drain 对象 |
| full-pytest-fixture-gate-mechanism-unspecified | P2 | Task 6 步骤 1 拍板 fixture 门机制（repo 内 host 优先；外部 fixture 则 deselect + 回写 acceptance 经用户确认） |

## Challenge findings 闭环（round 3 → 收敛）

- round 2 的 3 条 findings 全部 resolved，**无新增 P0/P1** → 收敛。
- 2 条 P2 残留已修：① Task 4 的 `close()` 用默认体 `def close(self) -> None: return None`（可选、不破坏 repo 外实现），并删掉"mock 补 no-op close"的错误指引（challenger 已证 SqliteExecutionUnitOfWork 是唯一 uow）；② Task 6 步骤 4 的 ubuntu-only 收窄加"经用户确认"标注（与步骤 1 一致）。

## 出口

- 8 条 H-AC 全部有任务覆盖且代码级可执行；round 1/2/3 findings 已闭环（1 P0 + 4 P1 + 7 P2），无 open in-scope P0/P1 → **收敛，进入 phase-3 执行**。
