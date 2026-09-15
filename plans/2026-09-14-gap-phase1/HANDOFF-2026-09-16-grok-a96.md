# 指针（2026-09-16）：Grok-4.6 单模型 A96 基线已完成，SDK 未改

Host 侧交接与结论（本机 taiwan）：`simple_harness/plans/taskSys2/HANDOFF-2026-09-16-grok-a96.md`、`testPhase1-a96-grok46-2026-09-16.md`。

- 评测统一 grok-4.6 medium（用户决定，A 轮 Qwen / B 轮 Flash 取消）。
- N4 `a96-grok46-256k-v2`：12 题 × S/R/D/F × 2 = 96 局，SDK `61a85eb`（含 f7432dc），官方 93/96，未知用量 1。
- N7 配对：D/F 未多解任何题，成本 3.4–4.0× S，错误宣布完成 2 例均在编排臂，知识复用 0。题单与 runner 已冻结，作为 HTN 上线后配对对照的前半。
- Grok 接线在 runner 级：usage 归一（completion+reasoning）、回显 `grok-4.6-build` 映射、`reasoning_effort`；`OpenAICompatibleProvider`/`MeteredProvider` 未改。若将来把 Grok 作为正式 provider，需在 SDK 侧处理 usage 的 total≠input+output 与回显名。
- 原始收据只在 Host 本机 ignored 目录。
