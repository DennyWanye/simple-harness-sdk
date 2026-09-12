# Agent 编排 Phase3 · 执行纲要（SDK 侧）

- 日期：2026-09-12 起
- 方向依据：用户的 Phase3 计划 P3-v1.0，位于 Host 仓库 `plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md`（提交 `e1f9e6cb`）。本纲要与它不一致时，以它为准。
- 用户指示（2026-09-12，原话）："先修复，然后开始P3.2 到 P3.5，文件提交"
- 长期规则：
  - 专门术语先查 `agent-orchestration-theory/` 里的定义；
  - 真实模型只用 deepseek-flash；
  - 记录与回复都用中文；
  - 技术取舍交给独立评审子代理裁决并记录；
  - 测试先行；
  - 回归红集必须 ⊆ 基线 73；
  - 每个切片单独提交并推送，同时更新 handoff；
  - 额度快用完时，先写 handoff，再提交推送。

## 1. 顺序与范围

| 顺序 | 版本 | 内容 | 仓库 | 计划目录 |
|---|---|---|---|---|
| 0 | P3.1 遗留修复 | F-ORCH-1：Task 预算下限；F-ORCH-3：产物验证状态回写；F-ORCH-2：只做语义说明 | SDK 为主，Host 重钉 | `p31-fixes/` |
| 1 | P3.2 | 隔离执行与真实受控交付：SandboxExecutorPort、测试运行改走沙箱、工作区登记、可回读回执的发布连接器 | SDK + Host 适配 | `p32/` |
| 2 | P3.3 | 非代码任务与证据闭环：EvidenceResolver、引用与段落定位、证据不足时的出口 | SDK 为主 | `p33/` |
| 3 | P3.4 | 让动态任务图成为有效搜索：局部 GraphPatch、候选择优组合、失败片段复用、Context 策略 | SDK 为主 | `p34/`（待建） |
| 4 | P3.5 | 真实负载与长任务恢复：物理槽位、排队不算失联、验证背压、预算不双花、多库备份恢复 | SDK + Host | `p35/`（待建） |

每个版本都按同一流程走：
1. 读 Phase3 计划的对应章节和它的 8 条验收（P3.x-A01..A08）；
2. 写本仓库的 plan、acceptance，交独立评审；
3. 测试先行，完成实现；
4. 做代码评审，跑全量回归，构建 wheel；
5. Host 重钉并补接线；
6. 需要界面的项做原生验收；
7. 最后交付。

P3.6 用户这次没有点名，不在本轮范围。

## 2. 起点

- SDK main 为 `29daa9c`（simple_harness 0.9.10 / agent_orchestrator 0.9.3），全量回归红集 73 条，等于基线。
- Host main 为 `e1f9e6cb`，已钉 0.9.10；P3.1 Host 直连路径已交付，记录在 Host `plans/2026-09-11-orchestrator-host-integration/`。
- P3.1 的遗留 F-ORCH-1 至 F-ORCH-7，见 Host 那份 journal 的 §5。

## 3. 进度

| 版本 | 状态 | 提交 |
|---|---|---|
| P3.1 遗留修复 | ✅ **SHIPPED**（2026-09-12）：代码评审第 1 轮已处置，只补了测试、改了文档；全量回归红集等于基线；wheel 0.9.11 已在干净环境验证；Host 已改钉 | `e182696`（计划）、`a84e2a4`（0.9.11 / 0.9.4）；Host `64930ad3` |
| P3.2 | ✅ **SHIPPED**（2026-09-12）：两轮计划评审 + 七个切片 + 一轮代码评审（5 P1 全修）+ 三轮 wheel 验证；SDK 0.10.0 已钉进 Host；真实 deepseek-flash 原生验收两场全过（发布四处哈希一致、沙箱 pytest 回执 tree_killed/residual 空）；遗留 F-P32-1..7 在 `p32/journal.md` §6 | `48e441a`（SDK 终态）、Host `04350956` |
| P3.3 | 计划第 3 版定稿；A 领域画像/角色/冻结/五入口已完成，干净源码编排全量 651 passed / 8 skipped。B–G 未完成，下一步来源登记与引用解析；尚未进行本次 Host/wheel/真实模型验收 | `fdc9c91`、`1eaa91f` |
| P3.4 | 未开始 | — |
| P3.5 | 未开始 | — |
