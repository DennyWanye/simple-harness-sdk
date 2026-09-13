**最后更新：2026-09-13 14:22 CST — P3.3/P3.5 功能范围累计验收完成。** 干净SDK f25a4de完整编排1826PASS/0FAIL/9真实Provider默认SKIP，pytest626.58秒、runner627.15秒，g-current-orchestrator-full-v10；父唯一pytest进程组已退出无残留。P33原46行/47断言/100selector已关联，本轮均非跳过；current-ac-association-v4.json SHA256 bbf8088f0f5eb279a14e86b4f6f98f12460db9e4d001feb4305ebb8a830d90f4。原始回放1265DB/1400数据库Mission身份/7670观察仍raw OPEN：116finding全归因（50负向、66直接状态/历史夹具），944诊断逐项保留（917canonical target在完成的扫描根、14原快照与搬移副本双hash一致、9负向、4非Mission SQLite夹具）；没有整测试豁免或宣称raw unknown为空。检查点间未观察删除文件及完整execution回放仍属范围限制。后继原生34DB/46Mission/80观察独立PASS零差异/错误/额外调用，0.847秒，Host replay-all-native-v5.json SHA256133846607875ee93355545f949cd461a378b25070bf5174f4cc71230f5050997。snapshot-v26含最新ed42919生产代码，COMPARE原生新建交付及冷读通过，18调用/0rehandoff/40journal/18selection/94Mission事件不变，生命周期178.704/90.287秒；部署层仅预期PolicyConfigDrift，ACTIVE未变。P35原8项均已有决定性软件/原生证据并关联此全量，8/8完成；P33按已批准源码载体范围完成，未声称安装包验收。P34固定真实pair v5两臂仍budget_exhausted，完整交付/收益门OPEN；默认FIRST不变，B240K→480K/S120K→240K且总2M不变的新对照提议待用户选择，不改旧失败实验。整体Phase3未完成，不打包/P36/推送。

# Agent 编排 Phase3 · 执行纲要（SDK 侧）

- 日期：2026-09-12 起
- 当前覆盖规则（2026-09-13）：暂停打包、发布、P3.6和推送，先完成功能并以源码原生UI验收；后文历史wheel/推送步骤不在当前执行范围。
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
| 3 | P3.4 | 让动态任务图成为有效搜索：局部 GraphPatch、候选择优组合、失败片段复用、Context 策略 | SDK 为主 | `p34/` |
| 4 | P3.5 | 真实负载与长任务恢复：物理槽位、排队不算失联、验证背压、预算不双花、多库备份恢复 | SDK + Host | `p35/` |

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
| P3.3 | Approved source-native functional acceptance complete:46 rows/47 original assertions/100 selectors associated;1826PASS full suite; raw negative diagnostics retained and individually adjudicated;34DB/46Mission native replay PASS. Packaging deferred. | f25a4de; current-ac-audit-2026-09-13.md |
| P3.4 | Controlled FIRST/COMPARE native delivery and cold recovery PASS, including latest production v26. Fixed real deepseek-flash pair v5 still budget_exhausted in both arms; no successful pair or measured advantage. Revised sub-budget comparison proposal awaits user preference; simple FIRST remains default. | 4ffad9e real pair v5; ed42919 native v26 |
| P3.5 | 8/8 functional and approved source-native acceptance complete; original pressure/Context/kill/backup controls and subsequent budget/late-Verifier fixes associated with final green full suite. | f25a4de; p35/current-remaining-2026-09-13.md |
