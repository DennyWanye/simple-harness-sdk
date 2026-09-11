# 第 9 步：从历史中学习，并受控晋级新策略 · 实施计划

- 日期：2026-09-11 · 基线 SDK main `c9d4879`（0.9.6 / agent_orchestrator 0.8.0，第 8 步 SHIPPED，wheel 源 `a094f46`）
- 原文依据：§28 第四阶段（学习型优先级模型、学习型 Model Router、角色和 Prompt 信誉、贡献归因、Replay、A/B Test、自动参数推荐、群体稳定性控制；"收集 Trace → 离线训练或规则改进 → 生成新策略版本 → Offline Evaluation → A/B Test → 审批后上线"；"不要让在线 Agent 直接自我修改核心安全和调度规则"）、§29.2 角色比例起点、§29.3 第一版优先级规则（"这只是起点。参数应通过 Evaluation 调整"）、§23.4；ORCH-BUILD-v1.0 §11（第 9 步）、§12 共用实施规则
- 术语（先查 `agent-orchestration-theory/`；与原文冲突以原文为准）：
  - **Offline Evaluation**（理论 12 §8）：固定任务集可重复测试，每次修改编排后重新运行、用于版本比较；**A/B Test**（理论 12 §10）：相似任务交给两种策略，比较质量、成本、延迟、稳定性；**Online Evaluation**（理论 12 §9）：真实运行中的成功率、费用、处理时间、人工介入次数——本步只做离线评测与离线 A/B，"上线后异常"由正式库中的运行指标触发回滚判断（S9-05）。
  - **Allocator / 优先级**（理论 05 §3：重要性 + 有希望程度 + 未探索价值 − 执行成本 − 重复风险，"不是必须使用固定数学公式"；原文 §29.3 七项权重是起点）：学习器 / 规则改进器只能提出**权重与参数的候选**，只给合法 Frontier 排序，不改变预算与权限资格（纲要 §11.2）。
  - **Model Routing / 模型路由**（理论 13 §3：按任务类型选模型 + 升级阶梯）：候选只能在允许模型集合内提出路由；模型可用性与预算仍由确定性程序检查（纲要 §11.2）。
  - **Reputation / 信誉**（理论 13 §18：Role、Prompt、模型、策略的成功率、真实错误发现率、误报率、成本、重复率、擅长任务类型；"要防止早期偶然成功造成长期偏见"）：本步用带样本量下限与置信下界的统计，不因"高信誉"免验证（纲要 §11.2）。
  - **Learning from Traces**（理论 13 §19："先可以更新规则，进一步再训练"）：资料不足时的正式路径是**规则改进**；本步的候选生成器是规则改进器，报告与记录如实标注"规则改进"，不冒充"已训练的模型"（纲要 §11.2 末句）。
  - **Rollback**（理论 13 §12 针对的是动作：可逆动作回滚、不可逆动作补偿）：本步的"回滚"是**策略版本**的回滚——生效指针回到曾经批准过的版本，不撤销、不改写任何既有事件与费用（S9-05）。
  - **Shadow**、**晋级（promotion）**、**生效版本**、**版本锁定**：原文与理论均无定义（纲要 §11.4 S9-03 用"shadow / 测试"），按 ORCH §13 登记为实施约定（§3 / §6.1）。
  - **Goodhart**（理论 12 §15）：门槛不以"任务数 / 知识条目数 / Agent 数"为目标，以成功率、验证误判、成本、稳定性为主。

## 1. 主要矛盾

系统已经能解释一次运行、能在相同任务集上比较两种策略（第 8 步），但**历史还不能变成受控的改进**：没有"候选策略"这一对象，也没有从候选到上线再到回滚的闭环，正式 Mission 用的策略就是启动时的配置。矛盾的主要方面是：**改进必须在受控通道里发生**——候选可追溯、先离线评测过门槛、经人批准才生效、新 Mission 绑定一个版本且在途 Mission 不被静默改变、异常时一步回滚且不改写历史；运行中的 Agent 无权改核心安全与调度规则；数据不足或有污染时如实拒绝。次要方面：规则改进器本身的质量（本步只要求它可追溯、可评测、结论诚实）、CLI 与演示、真实模型证据。

## 2. 范围

演示（`demo --scenario policy-promotion --provider fixtures --evidence-dir evidence/s9`）：先用 fixtures 跑出一批历史 Mission → 规则改进器从历史提出候选（可追溯）→ 离线评测（候选 vs 生效版本，留出 case）→ 一个过门槛的候选未批准时只在评测目录运行 → 批准并晋级 → 新 Mission 绑定新版本、在途 Mission 不变 → 注入异常后回滚 → 既有事件与费用不变；另演示一个不过门槛的候选与一次数据不足的拒绝。

做（设计见 §3，待代码摸底后补全）：策略版本库与生效指针（S9-01～05）；候选生成（规则改进器，S9-01、S9-07）；门槛评测（复用第 8 步 Evaluation，S9-02、S9-03）；审批与晋级、回滚（S9-04、S9-05）；Mission 版本绑定与恢复时锁定（S9-04）；运行中 Agent 的提议防护（S9-06）；参数边界、变化幅度、冷却与背压联动（S9-08）；CLI `policy propose / evaluate / approve / promote / rollback` 与演示；证据；版本（simple_harness 0.9.7 / agent_orchestrator 0.9.0，暂定）。

不做（登记）：训练型模型（数据量不足以训练，纲要 §11.3 允许返回"候选未达到晋级条件"）；在线流量分流 A/B；真实世界动作的补偿（第 7 步遗留）。

## 3. 设计决定

现状（代码摸底，2026-09-11）：没有策略版本库；Mission 不记录所用策略（`MissionCreated` 只有 goal / budget / spec_hash），重启后新决策一律用新配置与模块常量——**在途 Mission 会被静默换策略**，唯一的防线是证据层的快照漂移检测；Allocator 权重是模块常量 `WEIGHTS`，无注入点；`allocate` 在每个 Mission 的循环里调用（`event_handler.py:2627`），可以按 Mission 传参；审批 `decide_approval` 只接受 `kind == "action"`，`approvals.mission_id` 是 NOT NULL 外键；Agent 没有改配置的通道，Manager 的改图操作词表是封闭的。

| # | 决定 | 依据 |
|---|---|---|
| D9-1 | **策略参数与白名单**（`governance/promotion.py`：`PolicyParams`、`PROMOTABLE`、`NON_PROMOTABLE`）：策略是覆盖在部署配置之上的一层参数，只能含白名单项且每项在允许范围内——`allocator_weights`（§29.3 的七项，正项 ∈ [0, 0.5]、负项 ∈ [−0.2, 0]，缺省项取内置值）、`candidates_per_task` ∈ [1, 3]、`exploration_slots` ∈ [0, 2]、`mission_concurrency` ∈ [1, 部署 `max_concurrency`]（生效值取二者较小）、`manager_after_failures` ∈ [1, 5]、`no_progress_limit` ∈ [1, 5]、`max_manager_rounds` ∈ [1, 8]、`routing`（`escalate_after_failures` ∈ [1, 3]；`by_task_kind` 的目标必须是部署已有的 runtime profile）。**不可晋级**（显式清单，出现即拒绝）：`SAFETY_BOUNDARIES` 全部、`deployment_policy`、全部预算 / 预留 / `global_budget` / `hard_cap_micros` / `price_table`、`ablations`、超时 / 租约、`knowledge_sharing` / `dynamic_graph` 开关、角色模板正文。空参数 = 内置策略（代码常量 + 部署配置）。版本 id = `policy-` + 规范参数的 sha256 前 16 位（内容寻址：同参数同 id） | 原文 §28、§29.3；纲要 §11.2（学习器只排序合法 Frontier、不改预算与权限资格；只在允许模型集合内路由） |
| D9-2 | **策略版本库**（编排库 schema v6，四张表）：`policy_versions`（id、参数、来源、状态、评测摘要与报告哈希、理由）、`policy_decisions`（批准 / 拒绝：审批人、nonce 唯一、回执哈希、绑定的评测哈希）、`policy_activations`（上线 / 回滚 / 初始化的有序日志：前一版本、审批回执）、`mission_policies`（每个 Mission 绑定的版本与来源 `active` / `sandbox`）。状态：PROPOSED → PASSED / FAILED / INSUFFICIENT（评测）→ APPROVED / REJECTED（人）→ ACTIVE → RETIRED / ROLLED_BACK。**只有 Commit Service 的 `PolicyCommitsMixin` 写这些表**；部署级事件记在哨兵时间线 `deployment` 上：`PolicyCandidateProposed`、`PolicyCandidateRefused`、`PolicyEvaluated`、`PolicyApproved`、`PolicyRejected`、`PolicyPromoted`、`PolicyRolledBack`、`PolicySuggestionRefused` | 纲要 §11.2 policies 行；ORCH 单一写入者 |
| D9-3 | **Mission 绑定一个版本**：`create_mission` 在同一事务里读生效版本（首次使用时把内置策略登记为 ACTIVE，活动日志记 `seed`），写 `mission_policies` 并在 `MissionCreated` payload 加 `policy_version_id`；Replay 的正式字段集增加 `mission.policy_version_id`（第 8 步教训：改正式状态的事件必须同步投影），旧库无此表时该字段为 `not_covered`。评测沙箱：`Orchestrator(policy_pin=PolicyParams)` 只供评测使用，Mission 以来源 `sandbox` 绑定到沙箱库里的版本行；若库中已有来源 `active` 的 Mission（即正式库），`policy_pin` 直接拒绝 | S9-03、S9-04 |
| D9-4 | **按绑定版本运行**：Orchestrator 每个 Mission 从 `mission_policies` 取版本参数（按版本缓存），传给 `allocate(weights=…, candidates_per_task, exploration_slots, concurrency_limit=min(policy, 部署上限))`、Manager 触发阈值与路由覆盖（每个版本一个 `ModelRouter`）；`TaskScore` 记 `weights_hash`，intent 与 `AllocationDecided` 记 `policy_version_id`。重启换了配置的新实例恢复在途 Mission 时仍用绑定参数（部署上限另行生效，取较小值）；绑定的版本不在库里 → 显式报错，绝不静默换用新配置 | S9-04；摸底结论 7 |
| D9-5 | **规则改进器**（`governance/learning.py`，版本 `rules-v1`，报告如实标注"规则改进"，不称"训练"）：只读历史库（复制后只读打开）。每个训练 Mission 取：终态、归因（路径内 / 探索的 Task 及其分配分项，来自 `AllocationDecided`）、Attempt 的 profile 与失败原因。先查可信度与纯度，再按规则提候选：R1 权重——某一分项在探索 Task 上系统性高于路径内 Task（差值超过阈值且两组样本都达下限）则该项权重降一档（0.05），反之升一档，每次最多动两项；R2 路由——某任务种类在默认 profile 上首次失败率的 Wilson 下界 ≥ 0.5 且样本达下限，则提议该种类改用升级目标 profile。信誉统计（角色 × prompt 版本 × profile：样本数、成功率与 Wilson 下界、验证失败率、平均 tokens）写进来源，只作依据，不免验证。候选来源记录：训练集（Mission id、库路径与 sha256、创建时间、终态、provider 种类、spec 哈希）、代码版本（两包版本 + 基线快照哈希与版本常量来源）、学习器名与版本、逐规则统计、相对基线的参数差异 | 原文 §28；理论 13 §18、§19；纲要 §11.2、§11.3；S9-01 |
| D9-6 | **拒绝晋级的数据条件**（S9-07）：训练 Mission 数 < `min_missions`（默认 6）或所有规则的分组样本都不达 `min_group`（默认 5）→ 结果"缺少足够样本"，**不登记候选**，只记 `PolicyCandidateRefused` 与理由；训练记录不可信（回放覆盖率 < 1、有不一致或缺口、归因未对账）→ 拒绝并列出记录；fixture 与真实记录混在一起 → 拒绝；评测时训练集与评测 case 的 spec 哈希有交集（同一任务进了两边）或评测 case 早于训练窗口 → 拒绝（泄漏）。所有拒绝报告里没有"提升"结论 | 纲要 §11.2 evaluation 行（防泄漏）、§11.3 |
| D9-7 | **门槛评测**（复用第 8 步 Evaluation）：`evaluate_candidate` 在新目录里建评测计划，策略 `active`（钉住生效版本参数）与 `candidate`（钉住候选参数），同一组留出 case、同一预算、各 N 次。门槛（`GateSpec`，默认值写进代码并随报告输出）：样本 < 3 → INSUFFICIENT；质量——逐 case 候选成功数不低于生效版本、oracle 误判不增加；成本——候选 tokens 中位数 ≤ 生效版本 × 1.10；风险——两边脚手架错误为 0、等待人工不增加。结果（PASSED / FAILED / INSUFFICIENT + 逐条理由 + 报告哈希）经 Commit 记入版本库；fixture 评测标注"机制验证"，只证明流程，不证明质量 | 原文 §28（Offline Evaluation → A/B）；S9-02、S9-03 |
| D9-8 | **审批、晋级、回滚**（`PolicyCommitsMixin` + `api/policies.py` 的 `PolicyApi`，身份只来自人类 `Principal`，nonce + 回执哈希沿用第 7 步）：批准绑定"版本 id + 评测报告哈希"，只对 PASSED；拒绝 → REJECTED。晋级只对 APPROVED 且批准绑定的评测哈希仍是当前评测；受 D9-9 限制；同一事务里旧 ACTIVE → RETIRED、新版本 → ACTIVE、写活动日志与 `PolicyPromoted`。回滚：目标默认是上一次活动记录里的版本，必须曾经 ACTIVE 且未被 REJECTED；不受冷却与幅度限制（快速）；当前版本 → ROLLED_BACK；写 `PolicyRolledBack`。晋级 / 回滚都不碰任何已绑定的 Mission、事件与用量 | S9-04、S9-05 |
| D9-9 | **群体稳定性边界**（S9-08）：部署政策新增 `policy_cooldown_seconds`（默认 600）与 `policy_max_step`（权重每项每次 ≤ 0.10、整数项每次 ≤ 1）；两次晋级之间不足冷却时间 → 拒绝；背压 RAISED 时拒绝任何提高 `candidates_per_task` / `mission_concurrency` / `exploration_slots` 的晋级；并发生效值永远 ≤ 部署上限；预算与安全边界不在白名单里，任何版本都改不了。角色配比（§29.2 `ROLE_MIX_START`）本步不做成可晋级参数（角色按任务种类选取），登记 | 原文 §28"群体稳定性控制"、§29.2；纲要 §11.2 backpressure 行 |
| D9-10 | **在线 Agent 的提议防护**（S9-06）：Agent 能提出策略意见的唯一通道是在 Task outputs 里声明的 `policy/<name>.json` 产物（与第 7 步 `actions/` 候选同一模式）。accept 事务内检查：含不可晋级项（安全阈值、预算、部署政策、核心调度规则）→ `PolicySuggestionRefused`（理由：在线 Agent 不能改核心规则），任何生效规则不变；只含白名单项 → 登记为 PROPOSED 候选（来源 `agent:<attempt_id>`），必须经评测与人工批准才可能上线。`Principal` 只能是人，Agent 身份无法批准 / 晋级；Manager 改图提议中夹带配置操作仍被封闭词表拒绝 | 原文 §28 末句；纲要 §11.4 S9-06 |
| D9-11 | **CLI 与演示**：`policy propose --evidence-dir PROD --history DIR…`、`policy evaluate VERSION --evidence-dir PROD --eval-dir NEW [--provider fixtures|env] [--trials N]`、`policy approve|reject VERSION --as P --nonce N`、`policy promote VERSION --as P`、`policy rollback [--to VERSION] --as P`、`policy list|show`；退出码 0 成功 / 1 被门槛或规则拒绝 / 2 用法错误。`demo --scenario policy-promotion`（§2 流程）。真实 flash：对一个候选做小规模 `policy evaluate --provider env`，样本不足时如实给 INSUFFICIENT | 纲要 §11.4 拟交付命令 |
| D9-12 | 版本：simple_harness 0.9.7、agent_orchestrator 0.9.0；编排库 schema v6（迁移前备份，旧 DDL 不改）；`VERSION_SOURCES` 增加 `promotion` / `learning` 版本；策略快照增加绑定的策略版本 | ORCH §14.1 |

## 4. 任务切片

| 切片 | 内容 | 决定性测试 |
|---|---|---|
| A 版本库 | D9-1、D9-2：`PolicyParams` 校验、schema v6、`PolicyCommitsMixin`（登记 / 记录评测 / 批准 / 拒绝 / 晋级 / 回滚 / 冷却与幅度）、`PolicyApi` | `test_policy_registry.py`（S9-01 的可追溯与内容寻址、S9-02 / S9-05 的状态规则、非白名单拒绝） |
| B 绑定与运行 | D9-3、D9-4：创建即绑定、按版本分配 / 路由 / Manager 阈值、intent 与分配记录版本、恢复时锁定、Replay 新字段 | `test_policy_binding.py`（S9-04、S9-05 的"历史不变"） |
| C 规则改进器 | D9-5、D9-6：训练集读取与可信度检查、R1 / R2、信誉统计、样本不足 / 污染 / 泄漏拒绝 | `test_policy_learning.py`（S9-01、S9-07） |
| D 门槛评测 | D9-7：沙箱钉版、候选 vs 生效版本、门槛与记录、正式库不受影响 | `test_policy_gates.py`（S9-02、S9-03） |
| E 防护与稳定性 | D9-9、D9-10：Agent `policy/` 产物、冷却 / 幅度 / 背压、高负载演练 | `test_policy_guard.py`（S9-06、S9-08） |
| F CLI 与演示 | D9-11：`policy` 子命令、`demo --scenario policy-promotion`、改写 step02 的"未实现"检查；真实 flash opt-in | `test_policy_promotion_closure.py`、`test_real_provider_policy.py` |
| G 收尾 | review、wheel 0.9.7、CHANGELOG、testcase、program.md、journal、HANDOFF、推送、清理 | — |

## 5. 风险

- 按 Mission 取策略要改动分配、路由、Manager 触发三处热路径，可能影响第 3–8 步行为。处置：内置策略的参数与现有常量 / 配置逐项相同，第 2–8 步测试必须全绿（行为不变的证明）；快照哈希因 schema v6 变化，第 8 步的快照测试按"同配置同哈希"口径仍成立。
- fixtures 历史数据量小且"完美"，规则改进器在 fixtures 上可能提不出候选或只能证明机制。处置：演示用专门构造的历史（标注 fixture）触发 R1；报告标"机制验证"；真实数据只做样本不足的如实拒绝。
- 部署级事件没有 Mission：`events` 表无外键，用哨兵时间线；Replay 按 Mission 回放不受影响，另给版本库一个一致性检查（活动日志与 ACTIVE 状态一致）。

## 6. 评审后修订（plan review 第 1 轮：3 P0 / 11 P1 / 10 P2，原文要点 `reports/plan-review-round1.md`；逐条处置见 `journal.md` §1）

下面的 D9-x' 与 §3 冲突时以本节为准。

**D9-1'　参数一律展开；白名单补项**（P0-1、P1-11）
- 版本行存**展开后的完整参数**：白名单每一项都有显式值，登记时从当时的代码常量与部署配置解析（`resolve_params(config, partial)`），不留"缺省"；版本身份 = 展开后参数的规范哈希。
- 白名单增加 `aging_window_seconds` ∈ [60, 1800]（`waiting_age` 分项的尺度）与 `prompt_versions: {role: version}`——只能从 `runtime/role_templates.py` 已登记的模板版本里选（生产代码每个角色只登记一个版本；测试登记第二个 Worker 版本证明切换机制）。全零权重即"Allocator 消融"，作为合法候选写明。
- 版本行另记"解释器版本"：`ALLOCATOR_VERSION`、`ROUTER_VERSION`、`RETRIEVAL_VERSION`、`VERIFIER_VERSION`、`CONTEXT_BUILDER_VERSION` 与两包版本（D9-4'）。

**D9-2'　两层身份与完整状态表**（P0-2、P1-7、P1-8）
- 表（schema v6）：`policy_versions`（`version_id` = `policy-` + 展开参数哈希前 16 位；参数、解释器版本、来源类别 seed / legacy / learner / human、部署状态 ACTIVE / RETIRED / ROLLED_BACK / NEVER_ACTIVE）；`policy_proposals`（`proposal_id` = 规范哈希(参数版本 id + 训练集清单 + 代码版本 + 学习器名与版本 + 来源)；一个参数版本可有多条提议；提议状态见下）；`policy_evaluations`（`evaluation_id` = 提议 id + 报告哈希；基线版本 id、评测时两包版本、`evidence_kind` fixture / real、结论与逐条理由、报告哈希）；`policy_decisions`（批准 / 拒绝：绑定提议 id + 评测 id + 基线版本 id；审批人、nonce 唯一、回执哈希）；`policy_activations`（单调序号；版本、提议、动作 seed / promote / rollback、前一版本、回执、时间）；`mission_policies`（Mission → 版本、来源 active / sandbox / legacy、`provider_kind`、绑定时间）。
- 提议状态迁移（写进代码的表并有测试）：PROPOSED →评测→ PASSED / FAILED / INSUFFICIENT；这三者 →重评→ 三者之一（最新评测为准）；PASSED →批准→ APPROVED；PASSED →拒绝→ REJECTED（终态）；APPROVED →重评→ 新结论（原批准失效）；APPROVED →晋级→ PROMOTED（终态）。同参数的新提议另起一行（可指向 ROLLED_BACK 的版本，经新评测与批准后可再晋级；指向 ACTIVE 版本的提议晋级被拒"已生效"）。
- 部署状态由活动日志决定：最后一次激活的版本 ACTIVE，曾生效后被替换的 RETIRED，被回滚掉的 ROLLED_BACK。
- 事件（部署级，哨兵时间线 `deployment`，幂等 key 带唯一序号）：`PolicySeeded`（key = 激活序号）、`PolicyProposed`（提议 id）、`PolicyProposalRefused`（请求哈希）、`PolicyEvaluated`（评测 id）、`PolicyApproved` / `PolicyRejected`（回执哈希）、`PolicyPromoted` / `PolicyRolledBack`（激活序号）、`PolicyConfigDrift`（激活序号 + 配置哈希）。另写版本库的事件折叠投影 `registry_projection(events)`，与表逐项比较（第 8 步教训）。

**D9-3'　库角色、种子与绑定**（P0-1、P1-2、P1-5、P1-6、P1-10）
- 库角色标记 `scheduler_state.library_role`：评测器新建的库在打开前标 `evaluation`；普通 Orchestrator 首次打开时标 `production`。`policy_pin` 只接受 `evaluation` 库；普通实例拒绝打开 `evaluation` 库。
- 正式库 `__aenter__`：若无 ACTIVE，按当前配置与常量物化内置策略（来源 `seed`，记配置哈希与常量来源）并激活；若有 ACTIVE 而配置中白名单项的值与之不同 → 记 `PolicyConfigDrift`（列出差异，提示"改白名单项须走 propose / promote"），**生效的仍是 ACTIVE**。策略快照只记部署级 ACTIVE 版本 id（种子在开始快照之前物化，第 8 步漂移测试仍成立）；每个 Mission 的绑定写进它的证据。
- `create_mission` 同一事务写 `mission_policies`（`provider_kind` 来自默认 profile；Orchestrator 按 provider 类判定 fixtures / real / unknown，可显式传入）与 `MissionCreated.policy_version_id`；无 ACTIVE（CLI 直接用 Commit Service）时按默认配置物化种子。
- v5 → v6 迁移：新建表；已有 Mission 绑定到 `legacy` 行——参数为空、含义是"迁移前的 Mission，沿用部署配置"（它们在 v5 下本来就按当时配置运行），`provider_kind=unknown`；`legacy` 永不 ACTIVE。
- Replay：`mission.policy_version_id` 为可选正式字段——库快照有绑定时才计入分母（旧库不计），由 `MissionCreated` payload 决定；`PolicySuggestionRefused` 等新的 Mission 级事件登记为无正式效果。

**D9-4'　决策点与"部署级立即生效"清单**（P1-1、P2-6）
- 按 Mission 绑定取值的读取点（逐项改造并各有测试）：`allocate` 的权重（穿过 `score_tasks`）/ `candidates_per_task` / `exploration_slots` / `aging_window_seconds` / 并发（`min(mission_concurrency, max_concurrency)`）；候选 token 份额（`event_handler.py:2857`）；提交层守卫 `create_attempt(candidates_per_task, max_open_attempts)`（`:2907-2909`）；Manager 阈值 `manager_after_failures`（`:1758`）、`max_manager_rounds`（`:2021/2027/2074`）、`no_progress_limit`（`:2073/2168/2174/2250`，含恢复路径）；路由（Attempt 与 `_route_service` 的 Planner / Manager / Critic，每个版本一个 `ModelRouter`，覆盖层合并到部署规则上）；角色 prompt 版本（`prompt_versions`）。intent 与 `AllocationDecided` 记 `policy_version_id` 与 `weights_hash`。
- 部署级、按设计对在途 Mission 立即生效（不属策略）：安全边界、预算与预留、背压上限与收紧比例、超时 / 租约、`dynamic_graph` / `knowledge_sharing`、`max_concurrency` 上限、Verifier 并发、部署政策。
- 解释器版本（代码常量）变化时：恢复在途 Mission 若当前代码的解释器版本与绑定版本记录的不同 → 在该 Mission 时间线记 `PolicyInterpreterDrift`（列出差异）并写进证据；代码无法运行旧解释器，这是显式告知而不是静默。
- 覆盖层引用的 profile 在当前部署不存在 → 记 `PolicyRouteUnavailable`，该项回落到部署路由规则，不拖垮实例（S6-08 同一原则）。

**D9-5'　规则改进器收紧**（P2-1、P2-2、P2-3）
- R1 只统计"有竞争"的分配：v6 起分配记录 `eligible`（可分配 Task 数）与 `slots`（空位），R1 只用 `eligible > slots` 的记录；探索按 Attempt 计（归因里的 `on_success_path`）；`min_group` 按贡献样本的**不同 Mission 数**计；`uncertainty` 与重试次数混淆，不参与 R1 调整；报告写明"启发式规则改进"。
- R2：部署路由若对 worker 设了 `by_role`（优先级高于 `by_task_kind`），R2 结论为"不适用"；`task_kind` 是 Mission 级字段，写明。
- 结果分三类：`candidate`（登记提议）、`no_change`（样本够、规则没有触发，不登记）、`insufficient`（样本不够，不登记，记 `PolicyProposalRefused`）。
- 训练集读取：每个历史库只复制一次、批量回放；只取终态 Mission（非终态排除并列出）；v5 库的 Mission `provider_kind=unknown` → 不可信（列出），不当作"污染"而是"旧版本库 / 来源不明"。

**D9-6'　防泄漏按任务身份**（P0-3、P2-10）
- 任务身份哈希 = 去掉 `tenant_id`、`idempotency_key` 后的规范合同哈希（goal、criteria、seed、budget、task_kind、allowed_tools、synthesis 等）；提议记录训练集每个 Mission 的任务身份哈希。评测时：case 的任务身份与训练集有交集、或派生 case 的 `derived_from.mission_id` 在训练集 → 拒绝（泄漏）；时间规则只对有来源时间的派生 case 生效（早于训练窗口末端 → 拒绝），内置 case 报告标"无时间来源"。测试构造"同一任务、不同幂等键"。
- 切片 C 只做训练侧检查（可信度、纯度、样本），泄漏检查在切片 D（评测）。

**D9-7'　门槛与第 8 步同口径**（P1-3、P1-4、P2-4）
- 样本下限按**每个 case、每一方**：真实 ≥ 3；fixture ≥ 1（fixture 只证明机制）。任一方有脚手架错误 → INSUFFICIENT（写明理由），不判 FAILED。
- 质量：逐 case 候选成功率 ≥ 基线（非劣）、oracle 误判数不增加；成本：只有候选 tokens 最小值 > 基线最大值（明显更差）才 FAILED，区间重叠视为未见退化；风险：等待人工不增加；两方开始快照的差异只允许落在白名单字段上，否则 FAILED。PASSED 的含义写"非劣：样本内未见退化"，报告不许出现"提升"。
- 评测记录 `evidence_kind`：fixture / real。评测计划显式给出并发，fixture 脚本按候选参数配足。

**D9-8'　晋级 / 回滚的附加条件**（P1-3、P1-8、P2-5、P2-7）
- 晋级还要求：批准绑定的评测 id 是该提议的最新评测；评测基线版本 = 当前 ACTIVE；评测时两包版本 = 当前代码；`evidence_kind=real`，或正式库从未有过 `provider_kind≠fixtures` 的 Mission，或调用方显式 `accept_fixture_evidence=True`（写进 `PolicyPromoted`）。批准只需一位人类审批人（等同 L2），登记。
- 幅度：权重每项每次 ≤ 0.10、整数项每次 ≤ 1、`aging_window_seconds` 每次 ≤ ×2、路由每次最多改一个任务种类、`prompt_versions` 每次最多改一个角色；冷却从最近一次任意激活（含回滚）算起，用 Store 时钟（测试可注入）。
- 回滚：目标默认是最近一个 RETIRED 版本（跳过 ROLLED_BACK），或 `--to` 指定的 RETIRED 版本；不受冷却与幅度限制；回执列出仍绑定被回滚版本的在途 Mission（供人决定是否取消）；"既有事件与费用不变"限定为回滚时刻（seq）之前的事件。"异常"的判断：`policy status` 给出按版本的健康报告（Mission 数、完成 / 失败、失败率、等待人工），回滚由人执行。

**D9-10'　撤掉 `policy/` 通道，S9-06 按权限拒绝 + 人工出路**（P1-9）
- 不新开 Agent 写版本库的通道。Agent 夹带的修改——Worker 已接受产物落在 `policy/` 路径或名为部署配置的文件、Manager 改图提议里的配置类操作——一律记 `PolicySuggestionRefused`（Mission 时间线，列出被拒的键与"在线 Agent 不能改核心规则"），任何生效规则不变；Manager 的配置操作仍由封闭词表拒绝。
- 人工出路：`policy propose --params FILE --as P`，来源 `human:<P>`，报告写明"人工参数，非规则改进"，同样须评测与批准。Agent 身份不能构造 `Principal`。
- 残余风险登记：`run_tests` 是普通子进程、无文件系统隔离（第 7 步遗留 L2-4 / L6-5），`--as` 为自报身份——"Agent 不能批准"只在 API 层成立，不宣称更强。

**D9-9'　S9-08 演练可观测**（P2-7）：用 gate 让 Attempt 占位、小 `max_running_attempts` 让背压保持 RAISED；每周期由测试钩子读每个 Mission 的开放 Attempt 数与全局峰值、读账本核对预算；每次晋级 / 回滚前后比对部署政策与安全边界字段的哈希；冷却用注入时钟。

**D9-11'　证据与 CLI**（P2-8、P2-9）：演示另写 `policy_events.jsonl`（部署时间线）、`registry.json`（版本、提议、评测、决定、活动日志与折叠投影比较），均经脱敏与密钥扫描。step02"未实现"测试改写为：`SCENARIOS` 全部已实现、未知场景退出码 2。`TaskScore` 加字段前查第 5 步的精确断言；`public-api.json` 同步版本。

**D9-12'　第 8 步移交项与偏离登记**（P1-11）：做——`prompt_versions`（运行时切换 Prompt 版本的机制）、全零权重即 Allocator 消融、provider 身份补 `OpenAICompatibleProvider` 端点主机与目标模型。登记不做——`snapshot_diff` 字段级来源、回放覆盖更宽场景、派生 case 按场景指定 provider 工厂、Retrieval 版本运行时切换、新 Verifier 重判旧产物。**偏离**：纲要 §11.2"有足够数据时实现学习型候选"本步未做（历史数据量不足以训练，候选生成只有规则改进），如实登记。

### 6.1 切片（修订）

A 版本库（D9-1'、D9-2'：展开参数、两层身份、状态表、审批 / 晋级 / 回滚 / 冷却 / 幅度、事件与折叠投影、`PolicyApi`、schema v6 与 legacy 迁移）→ B 绑定与运行（D9-3'、D9-4'：库角色、种子、绑定、全部决策点、漂移事件、Replay 可选字段）→ C 规则改进器训练侧（D9-5'）→ D 门槛评测与防泄漏（D9-6'、D9-7'）→ E 防护与稳定性（D9-8' 幅度 / 冷却 / 背压、D9-9' 演练、D9-10'）→ F CLI 与演示（D9-11'）与真实 flash → G 收尾。
