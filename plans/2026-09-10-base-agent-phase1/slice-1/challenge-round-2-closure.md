# Slice 1 · 计划挑战第 2 轮（closure 复核）与裁决

- 日期：2026-09-10
- 复核者：独立 Opus 子代理（只读，按 `plan-closure-challenger.md`），对象为 plan/acceptance v2
- 结论：第 1 轮 21 条中 **19 条 closed、2 条 partially-closed**（#11 围栏落地写法、#18 失败载体）；修订者自报的 5 个新问题经代码核实**全部成立**；新发现 **5 条必改**（1 P0 + 4 P1）与 6 条 P2。
- 复核者判定"v2 暂不可执行，5 条改完即可放行"。

## 必改 5 条（已裁决，落点见 `plan.md` 附 E）

| id | 级别 | 一句话 | 裁决 |
|---|---|---|---|
| child-binding-fk-precedes-run-row | P0 | 子 binding 行带 `REFERENCES runs(run_id)` 外键，却要求在子 runs 行存在之前写入 | E1：binding 行改到 launch 成功后写；预围栏事务只写模式行 + 委派行（`reserved`） |
| base-agent-fence-cannot-reuse-self-transaction-facade | P1 | 围栏 facade 自开事务且 `namespace` 是外键，不能嵌入外层事务 | E2：helper 内联 `_bind_namespace` + 模式行 + `context_use_requirements.bind` |
| base-agent-failed-driver-result-terminalizes-run | P1 | `DriverResult(FAILED)` 会让 `_drive` 终态化 Run，Agent 永久死亡 | E3：意外 continuation → WAITING 无 outcome（kernel 自动 ack）；binding 缺失属内核完整性故障，保留 FAILED |
| delegation-row-precedes-launch-without-resume | P1 | 幂等闸"命中即跳过"会永久毒化崩溃窗口里的 delegation_id | E4：按 `reserved/launched/settled` 分流续做，只有 settled 才直接返回 |
| child-start-snapshot-preflight-fields-unspecified | P1 | 手工拼的子快照会在 `_drive` 预检被终态化 | E5：用 `bind_start_snapshot` 与 `_start_run` 同源填充 |
| queued-turn-lost-across-restart | P1 | WAITING 常驻的 Run 不在 `list_recoverable_*` 里，submit 后崩溃的 queued Turn 无人唤醒 | E6：`AgentRuntime.recover_pending_turns()` 启动时唤醒 |

## P2 六条

锚点漂移（E7）、STILL_UNKNOWN 的 evidence_ref（E8）、委派等待 vs 租约 TTL（E9，默认 20 s）、孤儿 context-use 行（E10，记遗留）、ordinary 回归用独立 react Runtime（E11）、recover 分支不跳过 resolved 事件（E12）。

## 放行

plan/acceptance 升为 **v2.1**（附 E 覆盖前文对应条款）。挑战轮次：primary 1 轮 + closure 1 轮，无新增 in-scope P0/P1 未处置 → 进入 phase-3 执行。
