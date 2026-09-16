# FULL-TARGET-1.4 实施交接（P0–P2 先行）

**最后核查：2026-09-16 CST，第一批提交（P1.1、P1.1b、P1.1c、P2.1b、P2.2）。计划来源：Host 仓库 `plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`（§23 执行顺序、§21.5 Grok 验收协议）。用户指令：先完成 P0、P1、P2 并做好测试，然后交用户验收。**

## 1. 接手结论

- 计划已定稿到 v1.4；P0（计划定稿 + 绿基线）完成；P1/P2 按 §23 的小片顺序实施中。
- 每片：Opus 子代理测试先行实施 → 独立 Opus 审阅（含隔离副本变异测试）→ 修复 → 主 session 编排范围回归 → 提交推送 main → 更新本文与 ARCHITECTURE。
- 只有 P2.3c 调真实模型（grok-4.6 medium，独立子代理按 §21.5 验收）；其余片不调模型。
- 基线：SDK 873fd4a（运行代码 = 61a85eb7）；编排范围 `tests/orchestrator` 2430 PASS + 2 已知 FAIL（p35 负载抖动、p33 Python 3.14 历史 AST hash 断言）；gap_phase1 需 `--with 'pydantic>=2' --with pytest-asyncio`。

## 2. 小片状态

| 片 | 内容 | 状态 | 测试 | 审阅 |
|---|---|---|---|---|
| P1.1 | HTN 契约类型、四值谓词求值器（contracts/{semantic_base,evidence_state,htn,obligations,resolution}.py、knowledge/predicates.py、planning/htn/applicability.py）+ 契约第三轮 12 项加法 | 已交付，审阅修复完成 | 225 | 需修后合并 → 已修（账本原子写入、fixture 入仓、schema 边界测试） |
| P2.2 | PANDA/HDDL 后端适配（planning/htn/backends/panda.py） | 已交付，审阅修复完成 | 55 + 1 skip | 需修后合并 → 已修（完整 stdout 抽取、路径绝对化、进程组、11 项变异全捕获） |
| P1.1b | 验收纯规则（verification/acceptance_rules.py） | 已交付，审阅修复完成，已适配契约第三轮 | 122 | 需修后合并 → 已修（独立性/在途事实必填、表达式一致性、伪回执） |
| P1.1c | 理由最小不动点（knowledge/justifications.py） | 已交付，审阅修复完成 | 101 | 需修后合并 → 已修（假设支持一律 taint、注册表必填、独立路径计数） |
| P2.1b | TaskNetwork 快照与投影验证（graph/task_network.py、graph/projection_validation.py） | 已交付，审阅修复完成，已切契约第三轮 | 117 | 需修后合并 → 已修（无 gating 孩子的 exit 门、共享复合子目标、资源分桶） |
| P2.2b | InputManifest 纯解析（artifacts/input_bindings.py） | 审阅修复中（未入第一批提交） | 92 | 需修后合并（witness 用途/消费者校验） |
| P1.2 | 存储层与第 16 号迁移 | 实施中（未入第一批提交） | | |
| P2.1 P2.1c P2.2b P1.2 P1.3 P2.3a P2.3b P2.3c | | 待做 | | |

## 3. 约束

- 不改 orchestrator/、scheduling/、artifacts/、storage/ 直到对应接线片（P1.3、P2.3a/b/c）；热文件单代理。
- 契约变更请求统一记录在各片 journal §"契约变更请求"，由主 session 汇总后交 P1.1 契约持有者一次性修改，避免并发改 contracts/。
- 本机无 cmake/gengetopt，PANDA 走 stub；配置 `SH_PANDA_PARSER` 后真实用例自动启用。
- 原始收据、日志只留 `.local-test-evidence/`（ignored）；Git 只存文字结论。

## 4. 记录索引

- 程序：`program.md`；各片：`P1.1/`、`P2.2/`、`P1.1b/`、`P1.1c/`、`P2.1b/` 下 plan.md / journal.md。
- Host 侧：`plans/taskSys2/升级planV1/impl/program.md`、`v1.2/S0-记录`、`v1.4/v1.4-变更记录`、`v1.4/v1.4-整体任务报告`。
