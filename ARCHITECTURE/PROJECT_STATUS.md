<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Simple Harness SDK 项目状态

本页只记录当前 SDK source candidate 的生产事实和仍开放的跨仓门禁。完整协议边界见
[ARCHITECTURE.md](./ARCHITECTURE.md)，目录入口见 [index.md](./index.md)。

## Human Memory Program

| 能力 | 当前状态 | 证据与边界 |
|---|---|---|
| S1 cognitive/evidence strict wire | Harness candidate 已完成 | schema-v2 cognitive wire、EvidenceItemAuthority v3、RecallDecision v3；Memory SDK 与 Host 产品能力仍需各自验收。 |
| S1 Host action authority | Harness candidate 已完成 | `MemoryActionAuthority` schema v2 与 mutation schema v4 已冻结；whole-plan commitment 防跨 operation 重放，COMMITTED result/receipt fail-closed 检查 protected refs，CONTEST 不能表达 destructive terminal lifecycle。 |
| S3 Procedure/Prospective Host authority | Harness candidate 已完成 | 两套 schema v1 ref-only authority 已冻结：Procedure terminal/applicability 与 Prospective scheduler/event/ack signal 都必须 Host resolve，exact scope/revision/receipt/transition binding，半开有效期和 replay identity。Memory consumer 尚待跨仓实现。 |
| Memory repository action-authority consumer | 未完成，跨仓门禁 | Memory 必须单次 resolve 并复验 exact binding，在 mutation transaction 内原子唯一消费 replay identity；同 receipt 的幂等重放是唯一例外。 |
| TaskScope、动态 Context、数字孪生体 | 未完成 | 不属于当前 Harness SDK candidate 的已交付能力。 |

## 最近里程碑

- **2026-08-31 — Harness S3 lifecycle-authority candidate**：新增 Procedure observation 与 Prospective signal
  两套 Host-owned strict protocol；阻断调用方自证 terminal success、clock due 或 event occurrence，冻结 exact
  scope/revision/trigger/receipt/transition/Run-operation commitment、strict current wire、半开有效期和 replay identity。
  Memory repository 的原子 consumer、scheduler outbox 与状态机仍是下一跨仓门禁。
- **2026-08-31 — Harness S1 action-authority candidate**：新增 Host-owned exact
  action authority、无循环 whole-plan/operation-intent commitment、canonical operation index、ref-only mutation wire、严格旧 wire
  拒绝，以及 `COMMITTED` / `NEEDS_USER_CONFIRMATION` / `REJECTED` typed result。CONTEST
  不获得覆盖或删除权；Memory consumer 的原子 replay fence 与 CONTEST target-state exact-unchanged
  验证仍是下一跨仓验收门禁。

<!-- last-updated: 2026-08-31 -->
