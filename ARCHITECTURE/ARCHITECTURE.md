<!-- last-calibrated: 617fa9f -->

# Simple Harness SDK — 架构基线（v0.1.4 生产化）

> 本文件记录 H1 slice（v0.1.4 发布阻断收尾）涉及的模块生产事实。非全仓架构；其余模块见 `docs/`。
> 校准锚点：`617fa9f`（H1 完成：8 条 H-AC 全部实现并 finalize PASS，receipt `c5f546cd`，全量 pytest 1226 passed / 2 skipped）。
> 下列"已知缺陷"在 0.1.4 已全部修复，仅保留缺陷描述与修复方式作为历史对照。

## 1. Consumer Adapter 层（`runtime/consumer_adapter.py`）

外部消费者 → SDK kernel 的桥接层。核心入口 `build_consumer_runtime(ports: ConsumerRuntimePorts) -> Runtime`。

**链路**（`build_consumer_runtime`，行 256-362）：

1. `Database.open(ports.database_path)` 打开 SQLite（**行 294**，未见关闭 hook，见 §4）。
2. `SqliteExecutionUnitOfWork(database)` 建 uow。
3. `_ConsumerToolExecutorAdapter.build_registry()` 把消费者的 `ToolExecutorPort` 包成 SDK `ToolRegistry`。
4. `_ConsumerAuthorizationAdapter` / `_ConsumerProviderAdapter` 桥接 auth/provider。
5. 组装 `RuntimePorts`（行 320-333），其中 `delivery=DeliveryDispatcher(uow, {"consumer": _DefaultDeliverySink()})`。
6. `build_react_driver` + `build_runtime` 返回 Runtime。

**已知缺陷（H1 目标）**：
- `_DefaultDeliverySink.deliver`（行 249-253）是 `pass`，但 `DeliveryDispatcher` 无异常即 `complete_delivery` → **假 DELIVERED**。
- 工具 handler（行 199-203）`execute(call, {})` 传空字典，丢失 run/request/session 上下文。
- `build_consumer_runtime` 是 demo 级 facade：缺 memory、真实计费（`FrozenPriceEstimator(...,0,0)`）、reconciliation 写死 `STILL_UNKNOWN`。

## 2. Delivery Dispatcher（`execution/delivery.py`）

- `DeliverySink.deliver(payload, *, idempotency_key)` — 消费者的投递实现。
- `DeliveryDispatcher.run_once()`：`claim_delivery` → 找 sink → `sink.deliver()`；**无异常 → `complete_delivery`**；异常 → `release_delivery`（可重投）。
- `__init__` 对空 `sinks` 抛 `ValueError("delivery sinks must have non-empty unique keys")`。
- **缺陷根源**：只要 sink 存在且不抛异常，就标记 DELIVERED。no-op sink 满足"不抛异常"，于是假投递。

## 3. Driver Failure Terminalization（`runtime/kernel.py`）

- driver 边界异常在 `except Exception`（行 1438 起）：`logger.exception("sdk_run_driver_failed", extra={"run_id": ...})`（**行 1454-1455，已修复**，原为 bare `run_id=` 触发 `TypeError` 遮蔽原始异常并跳过 terminalization）。
- 随后 `read_run` → 若 state 非终态 → `_terminalize(state=FAILED, payload=failure.to_dict(), deliveries=())`，`private_cause` 进 `HarnessError` 但被 `to_dict()` 排除在对外 payload 外。

## 4. Database 生命周期（`execution/sqlite/database.py` + `kernel.py`）

- `Database` 有 `open()`（行 31）与 `close()`（行 134）。
- `build_consumer_runtime` 行 294 `Database.open(...)`；`build_runtime` 只把 `uow` 传给 `Runtime`。
- `Runtime.close()`（行 944）清理 deliveries/后台任务/fence/lease，但**不调用 `database.close()` / `uow.close()`** → 连接泄漏。
- `Runtime.__aexit__`（行 1137）仅 `await self.close()`。

## 5. CI / Release

- `ci.yml`：build + test；test job 仅 `pytest tests/artifact`（**不跑全量**），仅 Python 3.11，无 ruff/mypy。
- `release.yml`：build → verify(sha+版本) + test-import（**只 import 冒烟 + conformance `--version`，不跑全量 pytest**）→ publish。`BUILD_INFO.txt` 写实时时间戳。
- `release-candidate-conformance.yml` 行 75：硬编码 `assert __version__ == "0.1.1"`（**版本漂移**）。

## 6. Memory Port（`runtime/ports.py`）

- `MemoryQueryPort`（行 218，Protocol，只读 recall）与 `MemoryWritePort`（行 265）已声明，但 **Runtime 未接线**。
- H1 只标 reserved，不实现、不接线。
