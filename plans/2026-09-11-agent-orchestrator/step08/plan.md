# 第 8 步：贡献归因、Replay 与策略对照评测 · 实施计划

- 日期：2026-09-11 · 基线 SDK main `8b1cf3e`（0.9.5 / agent_orchestrator 0.7.0，第 7 步 SHIPPED）
- 原文依据：§23（23.1 Logs/Metrics/Trace、23.2 监控指标、23.3 Lineage 与贡献归因、23.4 Evaluation 方法）、§28 第四阶段；ORCH-BUILD-v1.0 §10（第 8 步）、§12 共用实施规则
- 术语（先查 `agent-orchestration-theory/`；与原文冲突以原文为准）：
  - **Replay / 重放**（理论 12 §12；原文 §23.4「用历史 Event 和 Trace 重放失败过程」）：用保存的 Event 与 Trace 复现过程。纲要 §10.2 进一步规定：**Replay 是重建既有事实，不是"用新模型重新执行旧任务"；缺失数据必须标注；回放不调用真实外部工具、不重复收费**。理论 12 §12 里"新的检索或验证规则下重放历史失败案例"在本实现中属于 Evaluation（见下），不属于 Replay。
  - **Evaluation**（理论 12 §6、§8；原文 §23.4）：衡量"这套系统到底好不好"。多维：结果质量、成本（token、费用、工具调用、人工时间）、效率（重复工作、无价值任务占比）、稳定性（重复运行成功率是否稳定）、可扩展性。**Offline Evaluation** = 固定任务集重复测试；**A/B Test** = 相似任务交给两种策略，比较质量、成本、延迟、稳定性；纲要 §10.2：新策略重跑任务叫 Evaluation，涉及 LLM 随机性用多次独立试验并报告分布 / 样本量，不以单个成功样例宣称胜出；真实模型试验与确定性协议回归分开。
  - **Ablation / 消融**（理论 12 §11；原文 §23.4「移除 Critic、Blackboard、动态调度等组件，观察变化」）：分别去掉一个组件观察效果变化，判断真实贡献。纲要 §10.2：**消融不得关闭必要的权限 / 幂等安全边界后去执行真实副作用**；S8-04「不预设一定变差」。
  - **Lineage / 知识血缘**（理论 12 §5）与 **Credit Assignment / 贡献归因**（理论 12 §16、理论 13 §17；原文 §23.3「需要记录哪些知识位于最终成功路径上」）：不能只把功劳给最终提交者；沿知识依赖图追踪哪些角色、模型和策略真正进入成功路径。纲要 §10.2 `observability/traces.py`：产物 → 知识 → 验证 → Attempt → Agent 的贡献路径，并**记录未进入最终路径的探索消耗**。
  - **版本化**（理论 12 §13「没有版本信息，实验往往无法复现」；原文 §23.1 每个对象的 model/prompt/retrieval/allocator/verifier version）。
  - **Goodhart**（理论 12 §15）：评测报告不以"创建任务数 / 知识条目数 / Agent 数 / 消息数"为目标指标；以是否验证、是否复用、是否解锁、最终成功率为主（纲要 §10 完成标准）。
  - 术语缺口（原文与理论均无定义，本 plan 按 ORCH §13 登记为实施约定，§6.1）：「正式状态」的精确字段集；「覆盖率」的计算方法；「重复率」「剪枝率」「污染率」「误报率」的算法；策略差异的判定口径。

## 1. 主要矛盾

编排系统已经能跑、能记录，但还**不能用历史解释一次任务的成败，也不能凭证据比较两种策略**。矛盾的主要方面是：**解释与比较必须建立在不可伪造的记录上**——归因只沿真实记录走、缺了就报缺口；回放只重建既有事实、绝不再触发外部动作或收费；比较必须在相同任务集与相同预算边界下、带样本量与失败样例，新的运行永远是新的 Evaluation、不覆盖旧 Mission 事实。次要方面：版本冻结与差异报告、消融开关的安全边界、CLI 与演示、真实模型试验与确定性回归分开。

## 2. 范围

演示（`demo --scenario evaluate-policies --provider fixtures --evidence-dir evidence/s8`）：固定任务集（fixtures 上的若干既有场景）× 两种策略（A/B）× 一个消融（去掉 Critic）× 每组 N 次试验，每次运行在自己的目录与库里；产出评测报告（JSON + Markdown：成功率、完成耗时、成本、重复率、知识复用、验证结果、故障恢复、失败原因、失败样例、版本差异），对其中一次已完成 Mission 给出贡献归因，对一次失败给出回放。

做：Replay（D8-1/2/3）；贡献归因（D8-4）；策略快照与版本差异（D8-5）；Evaluation 套件、策略、试验、报告（D8-6）；消融开关与安全边界（D8-7）；旧任务新模型重跑 = 新 Evaluation（D8-8）；CLI `replay` / `evaluate` 与演示（D8-9）；证据（D8-10）；版本（D8-11）。

不做：学习型优化与策略晋级（第 9 步）；在线 A/B（真实用户流量分流；本步只做离线 A/B）；可扩展性维度（"10 → 100 个 Agent"需要大规模真实运行，登记）；原文 §23.2 中需要人工标注才能判定的指标（例如验证误报率的真值），给 null 与原因。

## 3. 设计决定

| # | 决定 | 依据 |
|---|---|---|
| D8-1 | **Replay 是纯折叠**（`observability/replay.py`）：输入是一个 Mission 的事件流（从库以 sqlite 只读连接 `mode=ro` 读取 `events` 表，或从证据目录的 `events.jsonl` 读取），按 `seq` 排序、按 `event_id` / `idempotency_key` 去重，逐条应用到一个内存中的投影状态。Replay 模块**不导入**任何 provider、连接器、预算账本或 Commit Service 写路径；不写库（库文件字节在回放前后一致）；不产生外部调用（测试以探针计数证明）。未知事件类型只列出，不猜含义 | 纲要 §10.2 replay 行、S8-02；理论 12 §12 |
| D8-2 | **正式状态的投影表**：由事件可以重建的正式状态 = Mission 状态与 stop_reason；每个 Task 的终态与 accepted_result_id；每个 Attempt 的终态；每个 Result 的判定（PASS / FAIL / REJECTED / SUSPENDED）；Verified Knowledge 集合及其 SUPERSEDED；冲突状态；动作版本的状态与回执哈希；审批请求状态；人工 override。每个字段登记它由哪些事件决定（投影表写进代码与 §6.1）。事件不足以决定的字段标 `not_covered`，**不从库里的现值回填**；回放报告给出覆盖率与缺口清单。与库快照比较时只比较已覆盖字段，列出每个不一致 | 纲要 §10.2、S8-02、S8-05 |
| D8-3 | **回放关键失败**：对一个失败的 Task / Attempt 给出按时间排列的关键事件（创建、分配、提交、各验证层结论与摘要、失败原因、重试、Manager 决定、停止原因），并标出事件缺失的环节 | 纲要 §10.1「回放关键失败」 |
| D8-4 | **贡献归因**（`observability/traces.py`，建立在 `lineage.py` 上）：从 Mission 的最终产物出发——终态 Task 的已接受 artifact 及合并进集成树的上游已接受 artifact——走到产出它们的 Task / Attempt / Agent / 角色 / 模型 / prompt 版本；再并入 lineage 的知识路径（used_knowledge、resolves、confirmed_by）、使这些结果通过的验证层与 Critic、路径上的动作与人工决定。**成功路径之外**的 Attempt（失败、被取代、被取消、未被使用的探索）单列为"探索消耗"。费用逐项来自已导入的用量事实（`imported_usage`），路径内 + 路径外 + 服务调用（Planner / Manager / judge）= Mission 总用量，逐项可对账；未定价部署的金额记 null 并注明，不写 0 | 原文 §23.3；理论 12 §16、13 §17；纲要 §10.2 traces 行、S8-01 |
| D8-5 | **策略快照**（`governance/policies.py` 的 `PolicySnapshot`）：冻结一次运行的全部版本来源——各角色 prompt 版本（planner / worker / critic / arbiter / synthesizer / manager）、模型与 runtime profile、retrieval、context builder、allocator、model router、verifier、backpressure、deployment policy 版本，以及影响行为的配置项与消融列表；规范 JSON + sha256。`snapshot_diff(a, b)` 列出每个差异项的两边取值与来源（常量名 / 配置字段）。每次评测运行与每个证据目录都写入快照 | 纲要 §10.2 policies 行、S8-07；理论 12 §13 |
| D8-6 | **Evaluation**（`observability/evaluation.py`）：`EvaluationCase`（名字 + MissionSpec 构造 + provider 工厂 + 种类 fixtures / env）、`Strategy`（名字 + 配置覆盖 + 消融列表）、`EvaluationPlan`（任务集 × 策略 × 试验次数 + 统一预算边界）。每次运行（case, strategy, trial）在独立目录 `runs/<strategy>/<case>/<trial>/` 与独立库中执行，产生新的 Mission id 与新费用；各策略使用同一组 case 与同一预算上限（Mission 预算与 Global 预算），差异只来自策略。结果汇总：成功率（成功数 / 样本量）、完成耗时分布（min / median / max）、tokens 与金额（或 unpriced）、Attempt 数、验证通过率、知识复用、重复率、剪枝率、故障恢复（失败后重试成功的次数）、失败原因分布、失败样例（stop_reason、失败层、摘要）。比较结论必须带样本量；样本量不足（< 策略最小样本数）或差异在样本波动内时写"证据不足"，不宣称胜出 | 纲要 §10.2 evaluation 行、§10 完成标准、S8-03；理论 12 §6、§8、§10、§15 |
| D8-7 | **消融开关**：`OrchestratorConfig.ablations`，封闭词表 `critic`（Critic 层与 Mission judge Critic 不运行，记为 `NOT_REQUIRED(ablated)`，依赖 judge 的自由文本准则判为未满足并注明"judge ablated"）、`blackboard`（等价于 `knowledge_sharing=False`）、`dynamic_graph`（等价于 `dynamic_graph=False`）。**安全边界不可消融**：权限交集、网关检查顺序、幂等键、审批、部署政策、密钥检查、验证的 format_check / rule_check / code_test 不在词表内，请求消融它们直接拒绝。消融运行的证据与报告列出"关闭了什么"与"观察到的变化"，不预设方向 | 原文 §23.4；纲要 §10.2、S8-04 |
| D8-8 | **旧任务新模型重跑 = 新 Evaluation**：Evaluation case 可以由一个已存在 Mission 派生（从旧证据目录的 `baseline.json` 读取 MissionSpec，只读），在新目录新库中运行，报告写明 `derived_from`（旧 Mission id、旧库路径、旧快照哈希）与新的快照；旧库在运行前后的文件哈希与快照不变 | 纲要 §10.2、S8-06 |
| D8-9 | **CLI 与演示**：`replay --evidence-dir DIR MISSION_ID [--events FILE] [--failures]`（只读，输出回放报告）、`attribute --evidence-dir DIR MISSION_ID`（贡献归因报告）、`evaluate --plan plan.json --evidence-dir DIR`（运行评测计划）、`demo --scenario evaluate-policies`。`--provider env` 的评测按用户指示只用 deepseek-flash，标注为真实模型试验，与 fixtures 回归分开报告 | 纲要 §10 拟交付命令 |
| D8-10 | **证据**：`write_evidence` 新增 `attribution.json` 与 `policy_snapshot.json`；`replay` 写 `replay.json`；评测目录写 `evaluation.json`（逐次运行、汇总、比较、失败样例、快照与差异）与 `evaluation.md`（中文可读报告）；全部经脱敏写入 | 原文 §23；纲要 §14.3 |
| D8-11 | 版本：simple_harness 0.9.6、agent_orchestrator 0.8.0；编排库 schema 不变（v5）——Replay 只读、Evaluation 运行在各自新库里 | ORCH §14.1 |

## 4. 任务切片

| 切片 | 内容 | 决定性测试 |
|---|---|---|
| A 快照与归因 | D8-5 策略快照与差异；D8-4 贡献归因与费用对账；证据 `attribution.json`、`policy_snapshot.json` | `test_attribution.py`（S8-01）、`test_policy_snapshot.py`（S8-07） |
| B Replay | D8-1/2/3：只读事件源、去重、投影表、覆盖率与缺口、与库快照比较、失败时间线 | `test_replay.py`（S8-02、S8-05） |
| C 消融 | D8-7：`ablations` 词表、Critic 消融的路由与判定、安全边界拒绝 | `test_ablation.py`（S8-04） |
| D Evaluation | D8-6/8：case / strategy / plan、独立目录运行、统一预算边界、汇总与比较、样本量规则、派生 case | `test_evaluation.py`（S8-03、S8-06） |
| E CLI 与演示 | D8-9/10：`replay` / `attribute` / `evaluate` 子命令、`demo --scenario evaluate-policies`、报告 Markdown；真实 flash 评测 opt-in | `test_evaluate_policies_closure.py`、`test_real_provider_evaluation.py` |
| F 收尾 | review、wheel 0.9.6、CHANGELOG、testcase、program.md、journal、HANDOFF、推送、清理 | — |

## 5. 风险

- 事件的 payload 在前几步里并不总是带全状态：投影表只能覆盖一部分字段。处置：覆盖不足如实报告（S8-05 本来就要求），必要时在本步给关键事件补充 payload 字段（只增不改，旧库回放时对应字段标 not_covered）。
- fixtures 脚本对不同策略不一定都够用（例如多候选策略需要更多 Worker 脚本）：演示与测试只选脚本能覆盖的策略组合，另写登记；真实模型评测不受脚本限制但有费用与随机性，按 opt-in 与样本量规则处理。
- 评测运行耗时：每次运行一个完整 Orchestrator，fixtures 上秒级；真实 flash 每次数十秒，试验次数取小值并如实报告样本量。

## 6. 评审后修订（plan review 第 1 轮：4 P0 / 13 P1 / 8 P2，原文 `reports/plan-review-round1.md`；逐条处置见 `journal.md` §1）

下面的 D8-x' 与 §3 冲突时以本节为准。

**D8-1'　只读与"不写"的证明**（P1-1、P1-3、P2-4）
- Replay 与归因不直接打开被分析的库：先把 `orchestrator.db` 及其 `-wal` / `-shm`（存在时）复制到临时目录，再用 `Store.open_readonly`（URI `mode=ro`，不迁移、只校验版本不高于当前，`snapshot` 能感知表是否存在）打开副本。原库在构造上不会被写。
- "不写 / 不增加外部调用"的证明口径：原库主文件、`-wal`、同目录执行库（`execution*.db`）的 sha256 回放前后都不变；目录设为只读（chmod）时回放照样成功；provider 调用数、连接器调用数、pytest 子进程调用数、Critic 调用数都不变；另有导入图测试：在子进程里导入 `observability.replay` 后，`sys.modules` 里没有 `agent_orchestrator.runtime.*`、`simple_harness.providers`、`verification.deterministic_checks`。
- 事件来源优先是库；从 `events.jsonl` 回放时报告标注"来源：证据文件（已脱敏，payload 可能被替换）"。事件一律按 `after_seq` 分页读完（`Store.iter_events`），证据与指标也改为分页，不再受 `limit=10_000` 静默截断。

**D8-2'　正式状态的字段集与覆盖率**（P0-1、P1-2）
- 正式状态字段集（逐项）：Mission{status, stop_reason}；Task{status, accepted_result_id}；Attempt{status}；Result{verification_state, verdict}；Knowledge{status, superseded_by}；Conflict{state}；Action{state, receipt_hash}；Approval{state}；HumanOverride{存在与否}。**明确排除**（登记）：预算账户余额与预留（由账本另算，归因里对账）、dispatch intent、租约与心跳、分配分数、背压状态。
- 推导规则（写进代码的投影表并在 §6.1 列出）：例如 Attempt COMPLETED ← 同 attempt 的 `VerificationPassed`；Task ACTIVE ← `AttemptStarted`，VERIFYING ← `ResultSubmitted` / `VerificationSuspended`，回到 ACTIVE ← `VerificationFailed` / `TaskVerificationAbandoned`；动作 APPROVED / REJECTED / REVOKED / EXPIRED / SUPERSEDED ← 对应审批事件（`ApprovalRequested.action_key` 建立请求 → 动作映射）。
- 补事件（只增不改）：`_cancel_open_actions` 取消动作时发 `ActionCancelled`；凡推导不出的状态变化，本步补事件而不是降低要求。
- **本版本产生的库，字段集覆盖率必须 = 100%**，并与库快照逐字段一致；`not_covered` 只允许出现在旧版本事件或事件被删除的场景（S8-05）。
- 缺口检测用结构性不变量而不是 seq 连续性（seq 是全库自增，多 Mission 交错）：例如有 `VerificationPassed` 却没有对应 `TaskCompleted`；Mission 已终态而仍有非终态 Task；Attempt 有 `ResultSubmitted` 却没有 `AttemptCreated`；动作有 `ActionHandedOff` 却没有 `ActionProposed`。有库可用时，另把库里的事件集合与给定事件流比对，列出缺失的事件 id。

**D8-3'　崩溃记录**（P1-3）：用编排器的故障点让运行在中途崩溃，在崩溃时刻复制一份库；用该副本的事件投影，结果等于该副本的快照；恢复运行完成后再回放，等于最终快照。

**D8-4'　归因的主体分类与路径规则**（P1-4、P1-5、P2-3）
- 逐行给 `imported_usage` 分类，主体解析复用 `metrics._service_role` 并补齐：`<mission>:task-k:attempt-n` → Attempt；`<attempt>:critic:<n>` → 该 Attempt 的验证费用（跟随该 Attempt 进入路径内或路径外）；`<mission>:judge:<n>` → Mission 判定（服务）；`:planner:` / `:manager:` → 服务；`unknown=1` 的行单列；另设"未归类"桶，要求为空。工具调用取 `budget_reservations.settled_tool_calls`；动作预留（`action:<key>`，没有模型用量）单列。人工等待时间（`human_wait_seconds`）单列为人工成本。
- 成功路径规则（逐条，各配用例）：多 Task 以集成树（`merge_accepted`）的输出条目为最终产物，产出它们的已接受结果与其依赖闭包内的已接受结果在路径内；被后续 Task 覆盖的上游产物仍在依赖闭包内时算路径内并注明"被覆盖"；冲突中落败一方 Claim 的 Attempt 算"探索消耗（被驳倒）"，胜出一方与仲裁结果在路径内（**D8-4'' 修订，代码评审 P1-1**：落败一方的 Attempt 若它自己的已接受产物在集成树 / 依赖闭包内，就仍在路径内——产物确实由它产出，理论 12 §16"不能只把功劳给最终提交者"——并标 `claim_refuted=true`、列入 `knowledge_path.refuted_on_path`；只经被驳倒的 Claim 挂上路径的 Attempt 才算探索消耗，原因 `claim_refuted`）；同一 Task 被取代的候选 Attempt、失败重试的 Attempt 算探索消耗；动态改图中被取代 / 取消的 Task 算探索消耗；人工 override / 审批作为路径节点（token 为 0，另记人工时间）；失败的 Mission 没有成功路径，全部消耗列为未进入成功路径。

**D8-5'　策略快照全字段枚举**（P1-8、P1-9、P2-6）
- 用 `dataclasses.fields(OrchestratorConfig)` 枚举全部配置字段：每个字段要么进快照，要么进显式排除清单（`evidence_root`、`owner_id` 等随运行变化的项，理由写在代码里）；测试保证"新增配置字段必须被归类"。
- 另含：全部角色模板版本（含 explorer / exploiter / simplifier / connector / failure_analyst 等）、`SUMMARY_VERSION`、`CONTRACT_SCHEMA_VERSION`、编排库 `SCHEMA_VERSION`、两个包版本、runtime profiles 与 routing、连接器清单、provider 身份（fixtures：provider 类名与脚本摘要；env：base_url 主机名、模型名、价目 snapshot id，从不含密钥）。
- 快照在运行开始时写入 `baseline.json` 与 `policy_snapshot.json`，收尾时再算一次，比较有无漂移。
- 登记（P1-9）：本步**不**提供运行时切换 Prompt / Allocator / Retrieval 版本的注册表（那是第 9 步"候选版本注册 / 版本化优先级候选"）；本步能比较的策略维度是模型与 runtime profile、消融、白名单内的配置项。版本常量的变化（代码升级）由快照差异逐项列出并给出来源（模块与常量名）——S8-07 以此判定。
- 登记（P2-6）：真实评测默认 unpriced，金额为 null；有 `SH_PRICE_*` 时注入价目；只用 flash 做不了"不同模型名"的真实对比（L6-1），fixtures 用两个 profile 名证明机制。

**D8-6'　评测运行身份、隔离、统计口径**（P0-2、P1-6、P1-7、P1-12）
- 每次运行的幂等键为 `eval:<plan>:<strategy>:<case>:<trial>`（Mission id 随之不同）；运行目录必须不存在或为空，否则拒绝；每个运行库只含这一个 Mission；测试断言跨运行的 Mission id 两两不同。
- 每次试验新建 provider（工厂），脚本按策略配足；每次运行有墙钟超时，超时或脚手架错误记为 `harness_error`，不计入成功率分母但必须报告；"等待人工"（waiting_on 非空的 ACTIVE）是单独的终态类别。
- fixtures 评测结果强制标注"机制验证（fixture），不代表质量"，策略比较结论写"不适用（fixture）"。
- 统计口径：成功率用 Fisher 精确检验（双侧，p < 0.05）判定差异，同时给 Wilson 95% 区间；按 case 成对比较；耗时与 tokens 给 min / median / max，两策略区间不重叠才写"有差异"，否则"证据不足"。真实 flash 小样本预期为"证据不足"，如实写。
- 验证误判：`EvaluationCase` 可带隐藏 oracle（评测方的额外测试文件与 pytest 目标，Agent 看不到），评测执行器在运行结束后于最终集成树上跑 oracle；"验证 PASS 但 oracle FAIL"的比例即验证误判率；无 oracle 时为 null 并注明。

**D8-7'　消融 = 有效验证政策的显式变更；安全边界白名单**（P0-3、P0-4、P1-10、P1-11）
- `critic` 消融：在 Router 入口从有效 required 集合里去掉 `critic_review`；该层状态仍为 `NOT_REQUIRED`，`detail.ablated=true` 记录"原政策要求、被消融"；Mission judge 不运行。消融运行产生的 PASS 在报告里标注"消融政策下的 PASS"，不与完整政策下的 PASS 混算；快照把政策变更列为差异。连带影响写进"关闭了什么"：needs_human 升级与第 ② 类仲裁随之消失。
- 评测计划校验：`critic` 消融的 case 其 Mission 准则只能是 `pytest:` / `file:`（自由文本准则在消融下结果预先确定，违背"不预设方向"），否则拒绝该组合。
- `blackboard` 消融（= `knowledge_sharing=False`）：连带关闭冲突任务与综合所需知识；判定写成可数形式：`KnowledgeUsed = 0`，出现 `ConflictOpenDeferred(knowledge_sharing_disabled)`（有冲突时）。
- `graph_changes` 消融（= `dynamic_graph=False`；原 D8-7 的 `dynamic_graph` 改名）：登记"这是关闭 Manager 改图，不是原文 §23.4 的动态调度 / 理论 12 §11 的动态 Allocator"；本步不提供 Allocator 消融（登记）。
- Strategy 的配置覆盖改为**白名单**：`ablations`、runtime profile / 模型选择、`candidates_per_task`、`manager_after_failures`、`no_progress_limit`、`max_manager_rounds`、`max_concurrency`、`knowledge_sharing` 与 `dynamic_graph`（只能经消融词表）。预算、Global 预算、`hard_cap_micros`、背压硬上限、`deployment_policy`（权限、连接器、审批规则）、密钥与验证的确定性层一律拒绝覆盖。
- 评测运行强制部署只启用测试连接器（`enabled_connectors ⊆ {"test_config"}` 且实例是 `TestConfigService`，每次运行一个新的测试服务文件）；带 `action:` 准则的 case 只能在测试连接器下运行，否则拒绝。

**D8-8'　派生 case 的保真性**（P0-2、P1-13、P2-8）：只支持本版本演示产生的证据目录（按 `baseline.json` 的场景名指定 provider 工厂）；从 `baseline.json` 读取 spec 后重算 `spec_hash`，与旧库 `MissionCreated.spec_hash` 比对，不一致就拒绝派生；派生运行改写幂等键，报告记录原 tenant / 原 key / 原 Mission id / 原快照哈希；旧库在运行前后哈希不变。

**D8-9'　CLI**（P2-8、P2-5）：`replay --evidence-dir DIR MISSION_ID [--events FILE] [--failures] [--attribution]`（归因并入 replay，不另设 `attribute`）、`evaluate --plan plan.json --evidence-dir DIR`、`demo --scenario evaluate-policies`。step02 的"未实现"检查改用 `policy-promotion`，`__main__` 文档串同步。

**指标定义修正**（P2-2）：见 §6.1。

**登记**（P2-1）：理论 12 §12 的"新验证规则下重放"的一个变体——用新 Verifier 重新判定旧产物而不重跑 Agent——本步不做，属于第 9 步候选评测；本步凡"新规则 / 新模型"一律是 Evaluation。

**切片调整**（P2-7）
- A Replay：只读副本打开、分页读事件、`ActionCancelled`、投影表与推导规则、覆盖率、结构性缺口、与快照比较、失败时间线、崩溃前缀。
- B 归因与快照：主体分类、路径规则、费用与人工时间、策略快照与差异。
- C 消融：有效政策变更、白名单、安全边界拒绝。
- D Evaluation：运行身份与隔离、超时与 harness_error、统计口径、oracle、派生 case。
- E CLI、演示、真实 flash 评测（opt-in）。
- F 收尾。

### 6.1 本步实施约定（非原文原句；按 ORCH §13 登记）

- Replay 与 Evaluation 的分界：Replay 只重建既有事实、不执行；任何"用新规则 / 新模型再跑一次"都是 Evaluation（新目录、新库、新费用）。
- 正式状态的字段集与投影表见 D8-2；覆盖率 = 已覆盖字段数 / 应覆盖字段数，按对象类型分别报告。
- 重复率 = 同一 Mission 内内容哈希相同的已提交 artifact 占全部已提交 artifact 的比例。
- 剪枝率 = 被取代的 Task 与被取代的候选 Attempt 占全部 Task / Attempt 的比例；排除 Mission 停止的级联取消（`mission_stopped`）与 `not_needed_paused`。
- 污染率 = 经冲突仲裁被驳倒的 Claim 数 / 全部 Claim 数（只有 Claim 会被 DISPUTED；知识的正常 SUPERSEDED 不算污染）。
- 故障恢复 = 某 Attempt 失败后同一 Task 的后续 Attempt 被接受的次数（"重试成功"）；崩溃恢复另计（recover 报告里的数）。
- 验证误判率 = 验证 PASS 但隐藏 oracle FAIL 的运行比例；没有 oracle 的 case 为 null。新思路数：本步没有可靠的记录口径，为 null 并注明。
- 策略比较口径：成功率用 Fisher 精确检验（双侧 p < 0.05）并给 Wilson 95% 区间；耗时与 tokens 用 min / median / max，区间不重叠才写"有差异"；否则"证据不足"；fixtures 结果写"不适用（fixture）"。
