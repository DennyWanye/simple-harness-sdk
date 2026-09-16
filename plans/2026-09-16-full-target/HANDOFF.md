# FULL-TARGET-1.4 实施交接（P0–P2 先行）

**最后核查：2026-09-16 CST，第三批 c 提交（P2.3c 第一部分）。第三批 a e78a8cc、b 767316b。第一批 623d4c8、第二批 7b0a88f。计划来源：Host 仓库 `plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`（§23 执行顺序、§21.5 Grok 验收协议）。用户指令：先完成 P0、P1、P2 并做好测试，然后交用户验收。**

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
| P2.2b | InputManifest 纯解析（artifacts/input_bindings.py） | 已交付，审阅修复完成 | 111 | 需修后合并 → 已修（witness 用途/消费者/支持修订三闸门、epoch 未知拒绝、内嵌 BoundInput） |
| P1.2 | 存储层与第 16 号迁移（storage/htn_schema、htn_store、obligation_store；31 张 STRICT 表） | 已交付，审阅修复完成 | 111 | 需修后合并 → 已修（ACTIVE 部分唯一索引直测、SQL 相对累加与真并发测试、迁移 16 校验和钉入、零白名单静态守卫、input_manifests 拆表） |
| P1.3 | 替代任务沿 Obligation 继承失败计数与额度（orchestrator/obligation_commits.py，commit_service +10/−1） | 已交付，审阅修复完成 | 33 | 需修后合并 → 已修（事件键并入序号、单行写入、后继绑定清空派发态、零裸 SQL）；旧模式零回归由字节级事件对比测试钉住 |
| P2.1 | HTN 核心（planning/htn/{registry,grounding,refinement,compiler,validation}.py、seed_methods/ code+appworld+虚构域 widget） | 已交付，审阅修复完成 | 340 | 需修后合并 → 已修（HDDL 导出重写为全 ground 模型并 stub 联调 VERIFIED、取证不扣燃料、义务开户 ObligationOpening、27/27 变异捕获） |
| P2.1c | 纯 readiness 与 EligiblePrimitiveTask（graph/eligibility.py） | 已交付，审阅修复完成，已切契约第四/五轮 | 200 | 需修后合并 → 已修（replace/copy/pickle 旁路拒绝、witness 消费者校验、新鲜度复用契约、read-set 义务/授权通道） |
| P1.1 契约第四～六轮 | 操作三轴、审批与候选策略、demand、codec 补齐、read-set 通道、HUMAN 作者、步骤复用、Acceptance 引用、ObligationOpening | 已交付 | 303（五文件实测） | 每轮全目录测试全绿 |
| P2.3a | Commit 接线：PlanCommitsMixin.commit_plan_revision（ADR-13：legacy 门 → 幂等 → epoch → 整数闸门不 rebase → 语义 read-set 11 通道 → 结构/保留性/替代=方法实例 → 预算 → 在途 → 同事务写全表 + PlanRevisionCommitted）、orchestration_semantics_version 默认 legacy、parse_plan_proposal/parse_method_proposal、两个新标签块与 hierarchical Planner 模板、validate_graph_v2、脚本化 fixture | 已交付，审阅修复完成 | 116 + 66 | 需修后合并 → 已修（空 issued_by 拒绝、带 openings 的修订原先提交不了、写半场绕过快照不变式、golden 常量与 HEAD 字节摘要） |
| P2.3b | event_handler 装配 hierarchical_dispatch（Planner 回复 → 解析 → 编译 → commit_plan_revision；被拒分类与有界重编译；repair_hint 接 BlockError；compound phase 由 reducer 推进；list/ready/terminal 走投影；PlanIntegrityError 只停本 Mission）；versioning 新模式 materialise_v2 只按 InputManifest，legacy 三函数源码 hash 锁定 | 已交付，审阅修复完成 | 102 + 32 | 需修后合并 → 已修（GraphIntegrityError 逃逸到共享 run 循环、hierarchical Planner 收集路径零覆盖、事件归一化按字段名）；给 P2.3c 的四条阻碍见 P2.3b/journal §7 |
| P2.3c 第一部分 | allocator v2（form 门先于 status，只接 EligiblePrimitiveTask，旧函数 hash 不变）、ResolutionCommitsMixin（accept_review / commit_goal_resolution，AER §7 顺序，交付回执复读校验，根义务闭包读法已裁决采纳）、orchestrator/_read_set.py（11 通道公共检查，plan_commits 切换后 116 条不变）、code/appworld 谓词观察器（只读、超时、三态、解析失败一律 UNAVAILABLE）、MethodSynthesizer 系统侧、method_synthesizer 角色模板 | 已交付，审阅修复完成 | 365 | 需修后合并 → 已修（read-set 5/11 → 11/11、交付回执复读、观察器实参写文件、ValueError 当负观察） |
| P2.3c 第二部分 | 接线：CommitService 加 ResolutionCommitsMixin；hierarchical_dispatch 调 commit_goal_resolution；occurrence 桥到可派发工作 + allocate_v2；派发前 evaluate_readiness 与输出索引；hierarchical Planner 包与提示词；Manager 图变更闸门；observers 注册；Synthesizer 经 BaseAgent；回执两表迁移；L2 状态谓词；新 Mission 走 HTN 跑真实叶子 | 待做 | | |
| Grok 验收 | 题集冻结候选包已备（L1 12 / L2 12 / L3 4 / L4 3，隐藏评分器与参考解自证 21/21）；用户三项决定已批；等第二部分通过审阅后冻结并由独立子代理执行；分批与否待用户答复 | 待做 | | |

## 3. 约束

- 不改 orchestrator/、scheduling/、artifacts/、storage/ 直到对应接线片（P1.3、P2.3a/b/c）；热文件单代理。
- 契约变更请求统一记录在各片 journal §"契约变更请求"，由主 session 汇总后交 P1.1 契约持有者一次性修改，避免并发改 contracts/。
- 本机无 cmake/gengetopt，PANDA 走 stub；配置 `SH_PANDA_PARSER` 后真实用例自动启用。
- 原始收据、日志只留 `.local-test-evidence/`（ignored）；Git 只存文字结论。

## 4. 记录索引

- 程序：`program.md`；各片：`P1.1/`、`P2.2/`、`P1.1b/`、`P1.1c/`、`P2.1b/` 下 plan.md / journal.md。
- Host 侧：`plans/taskSys2/升级planV1/impl/program.md`、`v1.2/S0-记录`、`v1.4/v1.4-变更记录`、`v1.4/v1.4-整体任务报告`。
