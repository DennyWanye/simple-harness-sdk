# 验收标准：simple-harness-sdk 0.1.4 — 发布阻断收尾（生产化 P0）

> 状态：DRAFT（待用户确认）
> 仓库：`simple-harness-sdk`（代码改动 + 发布仅在此仓库；宿主 re-vendor 是 C1 slice，明确不包含）
> 来源：2026-08-19 SDK 生产化 program 中 Slice H1；评审文档"P0：Harness v0.1.4 发布前"8 项
> 前置：logger 二次异常已在 main 修复（commit 577ed87），本 slice 补回归测试 + 其余 7 项

## 范围

**包含**（single slice，集中在 consumer 适配层 + 发布/CI 卫生）：

- 消费者 delivery 的 no-op sink 移除 / fail-fast
- 工具调用的 ToolContext 真实传递（不再传空字典）
- `build_consumer_runtime` 的 demo/basic 边界标注
- 数据库生命周期关闭
- logger 二次异常回归测试（durable terminalization）
- CI 全量 pytest + 渐进 ruff/mypy
- 版本单一来源（消除硬编码旧版本）
- Memory Port 标 reserved（不宣称 Runtime 已使用）

版本 0.1.3 → 0.1.4，重打 wheel，conformance + release-gate 复跑。

**明确不包含**：
- 宿主 `simple_harness` re-vendor（C1 slice）
- Memory Port 的实际实现与接线（本 slice 只标 reserved）
- 全项目 strict mypy / 完整 ruff 规则集（渐进引入，后续扩大）
- byte-for-byte reproducible build（本 slice 只明确 BUILD_INFO 时间字段的边界）
- 非 macOS 平台的本地测试（CI 用 ubuntu + Python 3.11）

## 功能验收条款

| ID | 功能点 | 验收条件（可验证） | 优先级 |
|----|--------|-------------------|--------|
| H-AC-1 | delivery sink 显式注入 / 消除假投递 | `_DefaultDeliverySink`（no-op）移出生产命名空间（仅 `simple_harness.testing`）；`build_consumer_runtime` 不再隐式注册 no-op sink——未显式注入 sink 时，delivery 保持 PENDING、**不得**标记 DELIVERED。测试：sink 返回 → `complete_delivery`；sink 异常 → `release_delivery` 且可重投；未注入 → 不 DELIVERED | 必须 |
| H-AC-2 | ToolContext 真实传递 | `_ConsumerToolExecutorAdapter` 的工具 handler 把真实 context 传给 `ToolExecutorPort.execute(call, context)`，不再传 `{}`；context 至少含 `run_id` 与 `call_id`。测试：consumer 的 `execute` 收到的 context 非空且含 `run_id`/`call_id` | 必须 |
| H-AC-3 | facade 边界标注 | `build_consumer_runtime` 明确标注为 demo/basic facade（模块 docstring + CHANGELOG + docs），声明其不提供生产级 delivery/memory/真实计费；并说明生产消费者应自行组装 `RuntimePorts`。不改变现有 demo 行为 | 必须 |
| H-AC-4 | 数据库生命周期关闭 | `build_consumer_runtime` 打开的 `Database` 在 `Runtime.__aexit__` / `close()` 时被正确关闭。测试：退出 context 后连接关闭、文件可被再次打开、无连接泄漏 | 必须 |
| H-AC-5 | logger 二次异常回归 | driver 抛异常时：① logger 不抛二次异常 ② Run durable terminalize 为 FAILED ③ 对外 payload 不含 `private_cause` ④ 日志含 `run_id` 脱敏诊断。测试断言上述 4 点 | 必须 |
| H-AC-6 | CI 全量 + 渐进 lint | `ci.yml` 的 test job 运行全量 `pytest`（不止 `tests/artifact`）；引入 ruff（定义规则集后当前代码 0 error）与 scoped mypy（公共 API + consumer adapter + 本次改动文件）；`release.yml` 依赖全量测试 + release-candidate conformance 成功 | 必须 |
| H-AC-7 | 版本单一来源 | workflow/脚本中所有硬编码旧版本（`release-candidate-conformance.yml` 的 `"0.1.1"`）消除，改为从 `src/simple_harness/version.py` 单一来源读取。测试：grep workflow 无硬编码版本字面量 | 必须 |
| H-AC-8 | Memory Port 标 reserved | `ports.py` 的 Memory Port 明确标注为 reserved（未接线），文档声明 Runtime 尚未使用 Memory、不宣称 recall 可用 | 必须 |

## 非功能 / 边界

- **向后兼容**：0.1.3 消费者公开 API（`ConsumerRuntimePorts` 既有字段、`build_consumer_runtime`、3 个 Protocol）零改动可升级
- **错误态**：未注入 sink 时 fail-closed（不得静默产生假 DELIVERED）；model 非法值沿用 0.1.3 的 fail-closed
- **隐私**：`private_cause` 不得进入对外结果；日志不得打敏感值全量
- **reproducible build 边界**：`release.yml` 的 `BUILD_INFO.txt` 时间字段明确为"排除在 byte-for-byte 比较外"（或改为取自固定 commit timestamp），记录在本 slice，不强制 byte-for-byte
- **conformance 范围收窄（H-AC-6）**：本 slice 把"release-candidate conformance"收窄为 release.yml `test` job 内的 ubuntu 内联 conformance（provider+tool 套件）；多平台矩阵保留在独立 `release-candidate-conformance.yml`（手动候选验证），不进自动 release 门
- **兼容**：不引入新运行时依赖（ruff/mypy 仅 dev 依赖）

## 适用性声明（APPLICABILITY_DECLARATION）

- `input_sensitive=false`：库 API 硬化，验证走确定性 mock provider + conformance，非 LLM 语义功能。
- `llm_payload_driven=false`：无 LLM 输出驱动端侧状态机。
- `stateful_init=false`：无异步注册服务/登录态依赖，无冷启动场景。

## 测试义务矩阵（Test Obligation Matrix）

| obligation_id | type | ac_id | risk | min_decisive_test | required_reason |
|---------------|------|-------|------|-------------------|-----------------|
| TO-H1 | delivery | H-AC-1 | — | 未注入 sink 时断言 delivery 不标记 DELIVERED；注入后断言 complete/release 行为 | 直接证明假投递已消除 |
| TO-H2 | delivery | H-AC-2 | — | consumer 的 execute 收到非空 context 且含 run_id/call_id | 直接证明 ToolContext 打通 |
| TO-H3 | delivery | H-AC-3 | — | CHANGELOG/docstring 含 demo 边界声明 | 直接证明边界被记录 |
| TO-H4 | delivery | H-AC-4 | — | `__aexit__` 后 DB 连接关闭、文件可再次打开 | 直接证明无连接泄漏 |
| TO-H5 | delivery | H-AC-5 | — | driver 抛异常后 run=FAILED、payload 无 private_cause、无二次异常 | 直接证明 durable terminalization |
| TO-H6 | delivery | H-AC-6 | — | 断言 ci.yml 引用全量 pytest 命令 + ruff/mypy step | 直接证明 CI 升级生效 |
| TO-H7 | delivery | H-AC-7 | — | 断言 workflow 无 "0.1.1" 硬编码、版本取自 version.py | 直接证明版本单一来源 |
| TO-H8 | delivery | H-AC-8 | — | 断言 Memory Port 文档标注 reserved | 直接证明未接线被声明 |
| TO-R1 | change-risk | H-AC-1..3 | FAIL-5 向后兼容 | 既有 minimal-consumer 不改跑通（0.1.3 消费者代码升级后通过） | 防止硬化破坏消费者 API |
| TO-R2 | change-risk | H-AC-2 | FAIL-2 ToolContext 空字典 | 断言 `execute` 第二参不再为 `{}` 字面量 | 防止 context 仍被清空 |
| TO-R3 | change-risk | H-AC-5 | FAIL-4 二次异常 | caplog 断言 driver 异常路径无二次 error | 防止 logger 修复回归 |

## 完成的定义（DoD 摘要）

1. 8 条 H-AC 全部通过测试
2. 所有 delivery / change-risk obligation 有对应 PASS testcase
3. `simple-harness-sdk` git status 干净、CHANGELOG 更新、version 0.1.4
4. SDK 全量 pytest + 消费者 conformance（release-gate）PASS
5. gate finalize exit 0，receipt 入账
