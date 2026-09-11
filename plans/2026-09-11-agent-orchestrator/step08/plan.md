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

## 6. 评审后修订

（plan review 后填写）

### 6.1 本步实施约定（非原文原句；按 ORCH §13 登记）

- Replay 与 Evaluation 的分界：Replay 只重建既有事实、不执行；任何"用新规则 / 新模型再跑一次"都是 Evaluation（新目录、新库、新费用）。
- 正式状态的字段集与投影表见 D8-2；覆盖率 = 已覆盖字段数 / 应覆盖字段数，按对象类型分别报告。
- 重复率 = 同一 Mission 内内容哈希相同的已提交 artifact 占全部已提交 artifact 的比例；剪枝率 = 被取代或取消的 Task 占全部 Task 的比例；污染率 = 被 DISPUTED 或 SUPERSEDED 的 Verified Knowledge 占全部 Verified Knowledge 的比例；验证误报率需要真值，本步记 null 并注明。
- 策略比较口径：成功率差异以样本量报告；样本量低于 `min_samples`（默认 3）或两策略成功数之差不大于 1 时写"证据不足"。
