# FULL-TARGET-1.4 实施交接（P0–P2 先行）

**最后核查：2026-09-16 CST，第三批 d 提交 d26a7b1（P2.3c 第二部分、b、c；四项设计裁决与 b/c 独立审阅进行中）。第三批 a e78a8cc、b 767316b、c 28fcc0d。第一批 623d4c8、第二批 7b0a88f。计划来源：Host 仓库 `plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`（§23 执行顺序、§21.5 Grok 验收协议）。用户指令：先完成 P0、P1、P2 并做好测试，然后交用户验收。**

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
| P2.3c 第二部分 | 已接：CommitService 加 ResolutionCommitsMixin（MRO 无私名冲突）；occurrence → Task 桥 + 预算守恒等式（committed+granted ≤ pool，未细化 compound 预留份额）；PLANNING→ACTIVE 正式规则；`_decide` 走 allocate_v2（readiness → admit_for_dispatch），`_next_attempt` 复检 admission；`_recorded_outputs` 从迁移 17 `acceptance_outputs` 读回；迁移 17 三表（接受侧回执改 keyed read、DeliveryReceipt 改库侧记录 + `record_delivery_receipt` 入口）；hierarchical Planner 包与提示词（按模式同时选）；Manager 图变更闸门（新事件 + 指路）；观察器取证管线 `planning/htn/observation_pipeline.py`；根 Resolution 触发 + Mission COMPLETED 只由它触发 | 已交付，审阅修复完成（由第二部分 c 修完） | 91（test_htn_end_to_end，含 8 变异） | **未做**：叶子验收链路（result → verifier_router → Critic → accept_review → 写 acceptance_outputs）、观察器与 Synthesizer 的部署装配、部署侧 PlanningWorld 装配、L2 状态谓词、真实模型冒烟（端点本轮 5xx / 模型不在该 base_url）。详见 P2.3c/journal.md「第二部分」§10 §13 |
| P2.3c 第二部分 b | 部署侧 `PlanningWorld` 装配（`planning/htn/world.py`：种子域 → 目录/schema/注册表/谓词，能力表按真注册的 Operator + 已部署层推导，证据快照每次读 htn_store，观察器索引按部署顺序单读者，`publish_methods` 把已批准方法写进库；`htn_world.seed_env` 委托真装配）；叶子验收链路（`orchestrator/leaf_acceptance.py`：verdict → 四个 AER 锚 → `accept_review`；`AcceptReviewCommand.outputs` 按 `check_declared` 落 `acceptance_outputs`；`event_handler._accept_hierarchical_leaf` 接线）；DATA 见证四件套（`input_witnesses` / `input_witness_index` / `issue_input_witnesses` / `resolution_policy_for`，修掉「解析器按 acceptance id 查、dispatch 按条件摘要给」的空集，`read()`/`admissions()` 两条闸门路径同步换索引）；取证轮（`planning/htn/evidence_round.py` 只读执行、OBSERVER_UNAVAILABLE 零落库）；MethodSynthesizer 经 BaseAgent dispatch（typed context、mission_planning、author 锁 MODEL）；提示词 `planner-hierarchical-v2` + 模式选提示词（pin 只在层次版本间生效）；包 `planner-package-hierarchical-v2`（step 字段修正 + `refine_method_ref`） | 已交付，审阅修复完成（由第二部分 c 复核） | 60（37 wiring + 22 end-to-end + 1 real_provider），4 条变异 | 真实冒烟（DeepSeek `deepseek-flash`，4 轮）：**`proposal_unreadable` 已消除**，Mission 诚实失败于 `READ_SET_UNRESOLVED`（模型引用了包从未给过的观察 id）。**未做**：Planner 包的 `facts` 段、L2 五条业务状态谓词与种子库哈希、compound COMPOSITION 账户、取证轮进 `_cycle`、`<method_proposal>` 回收分支。详见 P2.3c/journal.md「第二部分 b」§5 §8 |
| P2.3c 第二部分 c | 按独立审阅报告逐条修复（P0：F1 派发闸门 fail-closed + `HierarchicalAssemblyMissing`；F7 跨修订预算守恒两轮端到端。P1：F2 输出索引单写入点静态守卫、F3 `list_acceptance_outputs` 按 Acceptance 有效性过滤、F4 根判据取自 ReviewRecord（未覆盖 → UNKNOWN）、F5 `eligible_root_receipts` 三条件、F6 `judge_mission` 模式闸门 + 根 Resolution 快乐路径真跑、F8 `NetworkView` 带 accepted/witnesses/licences/starts、F9 四条改行为断言、F10 走完整 MRO、F11 admission 认类型不认 flag、F12 `intent_hash` 不再哈希 `decided_at_ms`、F15/F16/F18）；冒烟推进中另修六个真缺陷：第二轮细化的 `goal_occurrence_id`、`obligation_coverage` 重推导（`coverage_from_slots`）、`build_read_set` 的 FACT 通道、取证轮进 `_cycle` 并按已看过的命题封顶、**START 前置条件许可链（`issue_start_witnesses` + `plan_view.scope_epochs`，此前带前置条件的叶子永远不可派发）**、空转不再无声（`HierarchicalMissionStalled`，只记录不判决） | 已交付，审阅修复完成 | 47（full_target 第 17、18 节） | 真实冒烟（`deepseek-flash`，9 轮，超出 3 轮预算 6 轮，已如实记账）：计划提交 → 叶子真派发 → Worker 真改代码跑测试 → 六层验证过 → Acceptance 落库；最终停在 `HierarchicalMissionStalled`（`accepted_outputs_for` 的产物↔端口配对规则拒绝按位置配，模型交两个 artifact 而方法只声明一个端口）。三条新契约请求见 P2.3c/journal.md「第二部分 c」§7 第 10–12 条 |
| Grok 验收 | 题集冻结候选包已备（L1 12 / L2 12 / L3 4 / L4 3，隐藏评分器与参考解自证 21/21）；用户三项决定已批；等第二部分通过审阅后冻结并由独立子代理执行；分批与否待用户答复 | 待做 | | |

## 3. 约束

- 不改 orchestrator/、scheduling/、artifacts/、storage/ 直到对应接线片（P1.3、P2.3a/b/c）；热文件单代理。
- 契约变更请求统一记录在各片 journal §"契约变更请求"，由主 session 汇总后交 P1.1 契约持有者一次性修改，避免并发改 contracts/。
- 本机无 cmake/gengetopt，PANDA 走 stub；配置 `SH_PANDA_PARSER` 后真实用例自动启用。
- 原始收据、日志只留 `.local-test-evidence/`（ignored）；Git 只存文字结论。

## 4. 记录索引

- 程序：`program.md`；各片：`P1.1/`、`P2.2/`、`P1.1b/`、`P1.1c/`、`P2.1b/` 下 plan.md / journal.md。
- Host 侧：`plans/taskSys2/升级planV1/impl/program.md`、`v1.2/S0-记录`、`v1.4/v1.4-变更记录`、`v1.4/v1.4-整体任务报告`。
