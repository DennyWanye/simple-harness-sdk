<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
last-updated: 2026-08-21
-->

# Simple Harness SDK — 架构基线（v0.2.0 conversation Memory）

> 本文件记录当前生产边界；0.1.4 的缺陷段落仅保留为历史对照，不代表当前实现。

## 0.2.0 conversation Memory 当前事实（2026-08-21）

- `ConversationTurnInput` / `ConversationContinuationInput` / `ConversationTurnOutput`
  保留完整 `Message`/`ContentBlock`，同时只允许显式 canonical `memory_text` 投影；纯非文本
  turn 使用 `None` 并在本地结算，附件 body、reasoning 与 tool payload 不会自动进入 Memory。
- `ConversationMemoryQueryPort.recall_bounded()` 与 `ConversationMemorySinkPort.apply()` 是稳定
  async 边界，均有显式 `close()`；旧 reserved `MemoryQueryPort` / `MemoryWritePort` 仍可导入。
- execution SQLite 只接受唯一、self-contained 的 `0003_fresh.sql` descriptor；loader 不读取或
  拼接 legacy DDL。新库包含不可变 user/session 绑定、`context_preparation_staging` 与
  `memory_outbox`；旧 v1/v2 history（包括 StartSnapshot v4 数据）稳定 fail-closed 且不迁移，
  每次打开都开启并 read-back FK，POSIX DB 文件强制 0600 且拒绝 symlink/非普通文件。
- root start、普通 user continuation、root terminal、continuation terminal 四条命令把对应
  Memory intent 与 execution 事实放在同一 SQLite 事务；replay 同时比较 canonical intent hash。
  非文本 intent 直接进入 `skipped_non_text`，不会调用 sink。
- context preparation 先持久 claim identity/input hash 与有界 lease。`sdk_prepared` 只有 owner
  发 deterministic query；`consumer_prepared` 也遵循单 winner。private snapshot 把 recall
  结果作为 USER/untrusted data，并在 start/continuation 原事务消费后清空 private bytes、保留
  lineage/hash；ReAct 恢复只读冻结 snapshot，不二次 recall。
- `MemoryDispatcher` 用 claim token/expiry、transient backoff、permanent dead-letter 与幂等 sink
  恢复；apply 成功后 ack 前崩溃会以同 source event 重放。Runtime 在 recovery/drain 后启动 pump，
  close 时 bounded drain，并关闭 projection pump、query、sink 与 execution DB。关闭路径把
  `asyncio.wait` 输入物化，并只 cancel/gather 未完成任务，兼容 Python 3.11–3.13。
- `build_production_runtime(ProductionRuntimeConfig)` 是严格生产组合根：Provider、Tool、Auth、
  Delivery、reconcilers、conversation Ports、context staging builder 都必须显式提供；同时保留
  0.1.5 的 tool catalog、Provider budget resolver/projection pump、run binding 与 structured-message
  services。`build_consumer_runtime` 仍是独立 demo/basic facade。
- StartSnapshot v5 新增 conversation envelope、preparation mode、stage identity/hash 与 private
  snapshot；v1–v4 仍可读。Memory enabled 缺 envelope/stage/mode 时 kernel fail-closed；disabled
  generic run 不创建 Memory intent。ReAct 完成结果经 typed `conversation_output`，通用 payload
  只保留非敏感诊断。
- CI 单次构建 authoritative wheel/sdist 并生成 canonical `BUILD_INFO.txt`/`SHA256SUMS`，
  Python 3.11–3.13 测同一 wheel。release 仅 manual dispatch 下载、校验并上传 program publisher
  已创建 release 的原 bytes，不响应 tag、也不重新 build。
- 2026-08-21 验证：H1 19 passed；H2 15 passed；Python 3.11/3.12/3.13 full pytest
  各 1264 passed / 2 skipped；
  release-owned mypy 11 files 无错误；full Ruff 476，相对 frozen 484 baseline 减少 8；
  REUSE 338/338 compliant；H-WHEEL artifact suite 21 passed。

## 0.1.5 Context authority 当前事实（2026-08-21）

- `Message.content` 是 `str | tuple[ContentBlock, ...]`；canonical JSON、StartSnapshot、
  ReAct 恢复和 OpenAI serializer 保留结构，不允许通过 `str(list)` 降级。
- `ProviderBindingResolver.resolve(run_id)` 一次性绑定每个 Run 的 Provider、可选 frozen
  estimator 与预算策略；其 fingerprint 随 StartSnapshot 持久化并在恢复时校验。
- SQLite schema v2 持久化不可变、内容寻址的 tool catalog generation。Runtime 按
  generation + fingerprint 精确恢复；0.1.5 不执行 GC，因此 WAITING/restart 引用不会丢失。
- Provider terminal settlement 与 `provider_projection_outbox` receipt 在同一事务提交；
  reader 使用单调 `sequence` cursor，支持重启后幂等投影。
- `deskpet_public_progress` 是可选公开进度元数据；缺失、空白或类型错误时只剥离该字段，
  不阻断业务工具参数。8 MiB/block 与 16 MiB/Run 上限由产品 ingress 前置执行。

## 1. Consumer Adapter 层（`runtime/consumer_adapter.py`，历史边界）

外部消费者 → SDK kernel 的桥接层。核心入口 `build_consumer_runtime(ports: ConsumerRuntimePorts) -> Runtime`。

**历史链路**（0.1.4 审计时）：

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

- `Database.open()` / `close()` 显式拥有单连接；`SqliteExecutionUnitOfWork.close()` 转交关闭。
- generic `build_runtime` 只关闭调用方注册的 hook，不擅自拥有外部资源。
- `build_consumer_runtime` 注册 UoW close hook；`build_production_runtime` 还注册 projection/query
  async hooks，并由 Memory dispatcher 关闭 sink，最后关闭 UoW/Database。
- `Runtime.close()` 先 bounded drain delivery/Memory，再停后台任务并释放 leases/fences，最后按
  组合根声明的 ownership 关闭资源；重复 close 幂等。

## 5. CI / Release（当前）

- `ci.yml`：单次 build + canonical provenance，Python 3.11/3.12/3.13 对同一 Actions
  artifact 跑 full pytest；release-owned paths 独立 ruff/mypy，另跑 source provenance。
- `release.yml`：仅 manual dispatch，按 candidate commit / Actions run / artifact / wheel SHA /
  version 校验冻结制品，再上传原 bytes；无 tag trigger、无 `uv build`、不创建第二套制品。
- `release-candidate-conformance.yml` 按 candidate commit 与 artifact SHA 在 macOS ARM64、
  Windows x64、Linux ARM64 上消费 exact wheel，不拥有发布权限。

## 6. Memory Port（`runtime/ports.py`）

- `MemoryQueryPort` / `MemoryWritePort` 保留为 0.1.4 reserved compatibility imports。
- 0.2.0 的生产链路使用 `ConversationMemoryQueryPort` / `ConversationMemorySinkPort`；public
  DTO 和 stable status/error enum 由 `runtime/conversation_memory.py` 统一定义。
