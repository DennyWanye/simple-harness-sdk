# FULL-TARGET-1.4 实施交接（P0–P2 先行）

**最后核查：2026-09-17 CST，P2.3d（Grok 验收暴露的分层闭环缺陷修复）已完成，分支 `p2.3d-fix`，基于 main `e53395c`（= 0.12.0 + 发布记录），**未推送、未合并 main**。`tests/orchestrator/full_target` **2689 PASS + 2 skip**（基线 2648，净增 41）；旧模式回归 1 failed · 1966 passed · 20 skipped（与本机基线逐项一致，唯一的红是已知可忽略的 `p33::test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged`）；`ruff check src/agent_orchestrator tests/orchestrator` 本片改过的文件全清（仓库遗留 4 条在 p33/p34/p35）；`mypy src/agent_orchestrator` 17（基线未增）。上一段：P2.3c 第三部分 c（HEAD `e53395c`）。计划来源：Host 仓库 `plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`（§23 执行顺序、§21.5 Grok 验收协议）；本片依据：`impl/Grok验收-H臂故障诊断-2026-09-17.zh-CN.md`。**H 臂必须在本片之后全量重跑；F 臂因配对口径也要在同一提交上重跑。**

## 1. 接手结论

- 计划已定稿到 v1.4；P0（计划定稿 + 绿基线）完成；P1/P2 按 §23 的小片顺序实施中。
- 每片：Opus 子代理测试先行实施 → 独立 Opus 审阅（含隔离副本变异测试）→ 修复 → 主 session 编排范围回归 → 提交推送 main → 更新本文与 ARCHITECTURE。
- 只有 P2.3c 调真实模型（grok-4.6 medium，独立子代理按 §21.5 验收）；其余片不调模型。
- 基线：SDK 873fd4a（运行代码 = 61a85eb7）；编排范围 `tests/orchestrator` 2430 PASS + 2 已知 FAIL（p35 负载抖动、p33 Python 3.14 历史 AST hash 断言）；gap_phase1 需 `--with 'pydantic>=2' --with pytest-asyncio`。
- **库 schema：`SCHEMA_VERSION = 18`**（第二部分 d 裁决 1 的 migration 18 只加列 `validity_witnesses.subject_digest` 并换唯一索引，16/17 的 checksum 逐字节未动；就地升级留 `<db>.pre-schema-18.backup`）。
- 第二部分 d 之后的计数：`tests/orchestrator/full_target` **2435 PASS + 2 skip**（开工基线 2376/2）；旧模式回归 **1 failed · 1854 passed · 20 skipped（与基线逐项一致）**（唯一的红是已知可忽略的 `p33::test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged`）；`mypy src/agent_orchestrator` 17 errors in 4 files，与基线一致。

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
| P2.3c 第二部分 d | **四项设计裁决**：①许可的「对象」进唯一键（migration 18 `subject_digest` + `knowledge/validity.py::witness_subject` + `insert_validity_witness(subject=)`，契约字节未动）；②空转有界结束（`_stall_fingerprint` + `_confirm_and_stop_stalled`，恰好再跑一个周期后 `fail_mission(NO_DISPATCHABLE_WORK)`，本片唯一被授权的契约改动）；③demand 准入有了生产路径（`apply_obligation_openings` 按 REFINES_PARENT / INDEPENDENT_AUTHORIZED 判准入、`admit/withdraw_obligation_demand` 两个新事件、静态守卫锁死 `admit_demand(` 的四个合法出现点）；④端口↔产物由 Worker 声明（`PortClaim` + `parse_port_claims` + `worker-hierarchical-v1` 只加一行 `outputs`，删掉 `_port_for` 子串配对，新拒绝理由 `OUTPUT_PORT_UNCLAIMED`）。**b/c 独立审阅处置**：P0-1/P0-2 已修（P0-1 在真 `build_planning_world` + 出厂 code 域上确认 READY_CANDIDATE）；P1-3/4/5/6/7/8 全修；P2-9/11/12/13/14/15/16/19 全修，P2-10/17/18/20/21 记录并交第三部分 | 已交付（未提交），待独立审阅 | full_target 2435 + 2 skip（净增 59），11 条变异自证全部 KILLED | 真实冒烟（`deepseek-flash`，3 轮，用满未超）：`mission-a15cd6763b11c82a`，四个叶子全部派发/验证 PASS/落 Acceptance，下游拿到 `inputs=1`（声明式端口 DATA 链路端到端通了），`OUTPUT_PORT_UNCLAIMED` **0 次**，结算 128 396 token；停在 **FAILED / `no_dispatchable_work`**，唯一卡点 `ROOT_REVIEW_PACKAGE_MISSING`——根 MISSION_FINAL ReviewPackage 的裁剪（部署侧评审协调器）是第三部分范围，如实记录未就地修。详见 P2.3c/journal.md「第二部分 d」§6 §7 §11 |
| P2.3c 第三部分 a | **根 MISSION_FINAL 评审裁剪协调器**（新文件 `orchestrator/root_review.py` 1075 行：`root_review_ready` → 发布 `RequirementsRevision` → 裁 `ReviewPackage`(MISSION_FINAL) → 签 ACCEPT `ValidityWitness` → `_ask_root_reviewer` 走 service intent（`role=root_reviewer`、账户 `ReviewAccount.MISSION`、按包幂等） → `_collect_root_review` 用 `parse_critic_verdict` 落 `ReviewRecord`（**系统永不自填 PASS**，AER I05） → 既有根 Resolution 触发 → COMPLETED；**重裁有界**：`stale_reasons` 三通道（requirements / contributions / scope epoch），旧包 `HierarchicalRootReviewSuperseded` 留记录作废，每修订上限 `max_root_review_cuts`（默认 3），超限 `HierarchicalRootReviewCutBudgetSpent` + idle-stall；评审人判 FAIL → `HierarchicalRootReviewRejected` 进 §9.1 决策表，不重试；读不懂的回复只重问一次并附解析错误 `MAX_ROOT_REVIEW_ASKS=2`，被读懂过的回复永不重问）；**L2 业务态谓词 6 条**（`appworld.account-exists` / `list-contains` / `list-size` / `amount-equals`、`code.diff-touches-only` / `declared-dependency-present`，各一个只读观察器，TRUE/FALSE/UNAVAILABLE 三态 + 「解析失败永不变 FALSE」）；**第三轮审阅处置**（P0-A 冻结提示词 sha256 常量、P1-A 确认轮后可继续且有界、P1-B/P1-C 补承重接线与空测、P2-1 见证同键异结论、P2-4 demand 序号、P2-5 golden 时长归一、P2-3/P2-7/P2-9）；**Host runner 四条缺口**（G1 `MethodApplicabilityAssessed`、G2 `shared_goal_index` 接进 `_compile` + code 域共享只读子目标方法对、G4 revision 操作数、G5 `H` 臂）；P2-17 三条退出路径都留 stall 记录 | 已交付（未提交），待独立审阅 | full_target **2585 + 2 skip**（净增 150），本段新增 8 条变异自证全部 KILLED；旧模式回归 1 failed / 1854 passed / 20 skipped（与基线逐项一致）；mypy 17（基线） | 真实冒烟（`deepseek-flash`，**4 轮**，超出上限 1 轮已记账）：`mission-7ebe2d5cafa71266` **status = COMPLETED / verification_passed**——根评审裁剪 1 次、作废 0、拒绝 0，评审记录 4×TASK_CONTENT ACCEPT + 1×**MISSION_FINAL ACCEPT**，plan revision 1、Attempt 4、结算 172 864 token。三轮逼出的真缺陷均已修并补单测：①`AgentLimits(max_tool_calls_per_turn=0)` 让 Mission 死在自己的评审路上（synthesizer 同拼法一并修）；②读不懂的回复没有第二次机会；③`request` 读了 accept 路径根本不写的 `artifact_refs`，四条贡献全成「无证据」。**连带契约**：`OrchestratorConfig.max_root_review_cuts` 已登记进 `SNAPSHOT_FIELDS`（策略快照摘要因此改变）；`methods.json`/`task_types.json` 纯追加 → runner FREEZE-candidate 需重生成。详见 P2.3c/journal.md「第三部分 a」§3 §4 §5 §7 §8 |
| P2.3c 第三部分 c | **第四轮独立审阅处置 + runner 缺口 G7/G8**（Grok 验收开跑前最后一批代码改动）。**P0**：①G8 —— `appworld` 观察器把「宿主冻结策略在发请求前拒读」从**否定观察**改成 OBSERVER_UNAVAILABLE（新 `read_is_permitted` + `_Read.policy_refused` 互斥不变式，`Availability`/`Credential`/`Account` 同形一并修；此前 `account-exists('venmo'/'spotify'/'amazon')` 恒 FALSE，测试还把缺陷钉成了规格）；②`code.diff-touches-only` 空枚举（`-- <不存在路径>`、`HEAD..HEAD`）不再是 TRUE + COMPLETE_COVERAGE，改答 UNAVAILABLE；③补一条走 `event_handler._collect_root_review` 的端到端测试（真回复「FAIL + blocker + 全部 met:true」→ `record.verdict is REJECTED`、根 Resolution `committed is False`），并在协调器层加对称校验 `refuse_self_contradicting_accept`（PASS 却有准则 met=false 一律拒；FAIL + 全 met 合法）。**G7**：`HistoryObserver` 改走 G4 的 `revisions=("refs/bisect/bad",)` 只读路径（`REVISION_OPERANDS` 加 `rev-parse`），stub 测试改精确 argv 匹配 + 真跑 git 的回归。**P1**：P1-1 `run()` carry-on 真走 `run()` 的测试；P1-2 根评审人提示词改 `goal_statement`/`accepted_outputs`/`review`/`evidence` 并对零证据贡献显式标 `evidence.kind="none"` + 理由，测试从「至少一个」改成「每一个」；P1-3 golden 时长归一化按 `(event_type, field)` 九对定点 + `_redact` 边界单测；P1-4 ①`declares_package` 不再命中 TOML 键名/注释/URL、②`scope_id` 变活配置并接到 `build_planning_world`（CLOSED 否认端到端落进 `AnchorSelector`）、③`list-contains`（OPEN）不再发权威否认；P1-5 G2 两条空测改真行为 + `may_share` 接线层测试；P1-6 臂名闸门改读 `ARM_NAMES`（H 单独指名 `run_h_arm` 拒绝，见 journal §3 偏差 1）；P1-7 参数化负向解析测试，另修真缺陷 `verdict` 不可哈希时抛 `TypeError` 打挂循环。**P2**：P2-3/4/5/6/8/10 各补测试或说明（`CONTRIBUTIONS_MOVED` 独立触发、live package 规则 2/3 各自独立、I07 执行轴、READY 加说明、哨兵补 7 个新入口 + 6 个新事件名并修掉一条永远不会失败的断言、G1 幂等键按 plan_revision）；P2-1/2 口径写进本文发布说明段 | 已交付（未提交） | full_target **2648 + 2 skip**（净增 63）；旧模式回归 1 failed / 1854 passed / 20 skipped（零新增失败）；mypy 17（基线）；21 条变异 **20 KILLED**（唯一非 KILLED 的哨兵加宽不可独立自证，理由见 journal §3 第 2 条） | 本片即第四轮审阅的处置，处置表逐条见 P2.3c/journal.md「第三部分 c」§1，偏差见 §3，未修项见 §4 |
| P2.3d | **Grok 验收暴露的分层闭环缺陷修复**（H 臂 40 局 COMPLETED=0 的四类失败，全部确定性 SDK 缺陷）。**D3** 端口集合定义扩为「被 `DataRequirement` 消费 ∪ 被 `composition.criterion_links` 引用」，生产侧四处（诊断说三处，`leaf_acceptance._outputs` 是漏掉的第四处）统一走新的 `accepted_outputs.output_ports_in_revision`；**D4(a)** `_request_management` 加模式分支短路，记 `ManagementNotApplicableUnderHierarchical`，不再烧 manager/no_progress 额度；**D1** 从 `worker-appworld-v3` 派生 `worker-appworld-hierarchical-v1`（含 `appworld_execute` 与 AppWorld 文本），`HIERARCHICAL_WORKER_VERSIONS` 改为域模块注册后冻结，兜底改 `hierarchical_worker_for_domain(domain_for(mission))`，新增 `APPWORLD_PROFILE_V4`；**D5-A** 根验收 REJECT 且有 blocker finding 时把 findings 作为 `PlanningRejected{root_review_rejected}` 反馈重开一轮 Planner（`max_root_review_repairs`，默认 1/plan_revision）；**D2c** 理由码拆 `proposal_unreadable` / `proposal_not_grounded`；**D5-B** 规划触发扩为「存在未细化 compound 占位」（每 plan_revision 一轮，用 `adopted_instance_for is None` 判、不用 `NEEDS_REFINEMENT`），并让事后规划轮被拒时不再杀掉已有计划的 Mission；**D2b** `goals_needing_method` 加「证据已饱和」判定（同命题同观察器 ≥N 次仍 UNKNOWN，N=`evidence_saturation_rounds` 默认 2），I18 不动 | 已交付（分支 `p2.3d-fix`，未推送） | full_target **2689 + 2 skip**（净增 41，五个新文件共 24 条 + 既有文件补强）；旧模式 1 failed / 1966 passed / 20 skipped（零新增失败）；mypy 17（基线） | 真实模型冒烟**未能执行**：本机端点对默认模型返回 5xx、对 `deepseek-flash` 直接 `provider_request_rejected`（该 base_url 不提供该模型），DeepSeek 专用 key 在该 base_url 上 `provider_authentication_failed`。冒烟文件的收口断言已补强（终结步 `AcceptanceCommitted.accepted_outputs` 不得为空），端点恢复后跑一次即可。详见 P2.3c/journal.md「第四部分（P2.3d）」 |
| Grok 验收 | 题集冻结候选包已备（L1 12 / L2 12 / L3 4 / L4 3，隐藏评分器与参考解自证 21/21）；用户三项决定已批；等第二部分通过审阅后冻结并由独立子代理执行；分批与否待用户答复 | 待做 | | |

## 2b. 发布说明（Grok 验收开跑前必须一起读）

1. **层次 Mission 收尾会花两次 mission 账户的模型调用**：根评审人
   （`_ask_root_reviewer`，账户 `ReviewAccount.MISSION`）**加上**既有的 Mission Judge
   （`_judge`，根 Resolution 成立后触发）。§13 v1.4 的映射表原文只写了一次
   （「MISSION_FINAL → Mission 账户（现有 Mission Judge）」），本片之后是两次。
   顺序上保守（多一道闸，不会造成错误完成），但 **§21.5 的预算守恒按两次算**。
2. **策略快照 digest 已从 `85d78ea5…` 变为 `7cf60224…`**，差异恰好一条
   `config.max_root_review_cuts: null → 3`（第三部分 a 把它登记进 `SNAPSHOT_FIELDS`）。
   全仓没有任何钉死 digest 的常量，`storage/offline_backup.py` 不调 `policy_snapshot`，
   `observability/evaluation.py` 只抄进溯源字段不比对——**不阻塞发布**，但
   依赖旧 digest 做外部对照的脚本要重取基线。
3. **`shared_reuse` 的前提**：共享构造必须挂到 `code.fix-failing-test`，否则恒 0；
   `code.assess-regression` 目前只有一条方法、没有 OR 分支。
4. **G7 已修**：`code.regression-commit-known` 现在能在真 git 工作区上为 TRUE，
   runner 的 M3 `shared_reuse` 不必再记 BLOCKED（runner 侧无须改动）。
5. **G8 已修但不等于放宽**：`app-reachable` / `credentials-valid` / `account-exists`
   对非 `supervisor` 应用从「假的 FALSE」变成诚实的 UNKNOWN。**没有**放宽
   `PUBLIC_READ_APIS`，所以非 supervisor 应用仍然观察不到；runner 把 `app` 钉成
   `"supervisor"` 的做法**仍然有效且仍然需要**。
6. **`ReceiptObserver` / `code` 域观察器的 `coverage_scope` 仍是描述性字符串**，
   与锚点层要求的「等于决策 scope」对不上（本片只修了两条新 CLOSED appworld 谓词）。
   域级遗留问题，记在 P2.3c/journal.md「第三部分 c」§4。

7. **P2.3d 的三条口径变化**（Grok 验收重跑前必须一起读）：
   - `resolve_domain("appworld-v1").version` 由 `"3"` 变为 `"4"`（新建 Mission 冻结
     `APPWORLD_PROFILE_V4`，多一个 `worker_hierarchical` 键；已冻结的快照逐字节不动）；
   - `OrchestratorConfig.to_json()` 多一个 `max_root_review_repairs` 键——**策略/配置快照
     digest 因此再次改变**，依赖旧 digest 做外部对照的脚本要重取基线；
   - `HIERARCHICAL_WORKER_VERSIONS` 不再是单元素集合（现含
     `worker-hierarchical-v1` 与 `worker-appworld-hierarchical-v1`）。
8. **runner 侧还欠两条**（本片范围外，见诊断 D2a / S1 与 journal「第四部分」§6）：
   - `run_h_arm.py` 的 L3 根目标不要指向绿色可见套件，且 `OrchestratorConfig` 要显式传
     `max_planning_attempts=3`（SDK 出厂默认仍是 2，理由见 journal D2c 段）；
   - `_accepted_files()` 名实不符、`pair.py` 只看 `official_utility` 而没有
     `mission_status == COMPLETED` 的不变量。
9. **FREEZE 必须重生成**：本片动了 `accepted_outputs.py`、`hierarchical_dispatch.py`、
   `role_templates.py`、`domains.py` 等被钉住的上游文件，`FREEZE.json` 必失配。

## 3. 约束

- 不改 orchestrator/、scheduling/、artifacts/、storage/ 直到对应接线片（P1.3、P2.3a/b/c）；热文件单代理。
- 契约变更请求统一记录在各片 journal §"契约变更请求"，由主 session 汇总后交 P1.1 契约持有者一次性修改，避免并发改 contracts/。
- 本机无 cmake/gengetopt，PANDA 走 stub；配置 `SH_PANDA_PARSER` 后真实用例自动启用。
- 原始收据、日志只留 `.local-test-evidence/`（ignored）；Git 只存文字结论。

## 4. 记录索引

- 程序：`program.md`；各片：`P1.1/`、`P2.2/`、`P1.1b/`、`P1.1c/`、`P2.1b/` 下 plan.md / journal.md。
- Host 侧：`plans/taskSys2/升级planV1/impl/program.md`、`v1.2/S0-记录`、`v1.4/v1.4-变更记录`、`v1.4/v1.4-整体任务报告`。
