# FULL-TARGET-1.4 实施程序（P0–P2 先行）

开始：2026-09-16 09:30 CST。依据：Host 仓库 `plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`（§23 执行顺序、§21.5 Grok 验收协议、§24/§25 附件）。用户 2026-09-16 指令：先把 P0、P1、P2 处理完并做好测试，然后交用户验收。

## 纪律

- 每片先写红测试；编排范围 `tests/orchestrator` 独立回归；旧模式零回归；完成即提交推送 main；更新 HANDOFF 与 ARCHITECTURE。
- 最多 3 个 Opus 子代理并行，写入范围不重叠；热文件（commit_service、event_handler、versioning、action_commits、connectors）单代理。
- 不调真实模型的片优先；只有 P2.3c 调 grok-4.6（独立子代理按 §21.5 验收）。
- 基线：SDK 873fd4a（运行代码 = 61a85eb7）；编排范围 2133 PASS / 47 SKIP / 2 已知 FAIL + gap_phase1 297 PASS / 38 SKIP（需 `--with pydantic --with pytest-asyncio`）。

## 顺序与状态

| 片 | 状态 | 记录 |
|---|---|---|
| P0 | 完成 | Host `升级planV1/v1.2/S0-记录`、`v1.4/v1.4-变更记录` |
| P1.1 | 进行中 | `P1.1/` |
| P2.2 | 进行中（与 P1.1 并行，写入不重叠） | `P2.2/` |
| P1.1b P1.1c P2.1 P2.1b P2.1c P2.2b P1.2 P1.3 P2.3a P2.3b P2.3c | 待做 | |
