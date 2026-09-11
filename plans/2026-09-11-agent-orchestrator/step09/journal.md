# 第 9 步 · 执行记录

## 1. 关键裁决

独立 plan review（claude-opus-5，只读；原文要点见 `reports/plan-review-round1.md`）3 P0 / 11 P1 / 10 P2，结论"不需推倒重来，修订后可进入实现"；处置写在 plan §6（D9-x'）。评审结论出来之前只做了只读摸底，没有写实现。

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| P0-1 | P0 | 内置策略"空参数"随配置 / 常量浮动，锁定无效 | 版本一律存展开后的完整参数；种子在正式库 `__aenter__` 物化并记配置哈希；配置白名单项与 ACTIVE 不同 → `PolicyConfigDrift`，生效仍是 ACTIVE（D9-1'、D9-3'） |
| P0-2 | P0 | 版本身份定义矛盾，状态机缺边 | 两层：参数版本（展开参数哈希）+ 提议（参数版本 + 训练集 + 代码 + 学习器 + 来源）；评测 / 批准挂在提议上；完整迁移表（D9-2'） |
| P0-3 | P0 | 泄漏按 spec 哈希判定永不触发 | 任务身份哈希（去掉 tenant / 幂等键）+ 派生 case 来源 Mission id；时间规则只对有来源时间的 case；测试构造"同任务不同幂等键"（D9-6'） |
| P1-1 | P1 | 决策点没列全，缺恢复与多 Mission 测试 | 逐项列出读取点与"部署级立即生效"清单；解释器版本漂移记事件；参数化"配置 A 创建、配置 B 恢复"与同进程两版本测试（D9-4'） |
| P1-2 | P1 | v5 旧库迁移规则缺失 | 迁移绑定 `legacy`（沿用部署配置、永不 ACTIVE）；Replay 新字段对旧库不计分母；补迁移测试（D9-3'） |
| P1-3 | P1 | fixture 评测可直接用于正式晋级 | `evidence_kind`；正式库有过真实运行时须 `accept_fixture_evidence` 显式确认并写进事件（D9-7'、D9-8'） |
| P1-4 | P1 | 门槛口径与第 8 步不一致 | 样本按每 case 每方；成本"明显更差才拒"；脚手架错误 → INSUFFICIENT；PASSED = 非劣（D9-7'） |
| P1-5 | P1 | fixture / 真实判定无依据、会误标 | 绑定行记 `provider_kind`（按 provider 类判定，可显式传入）；旧库 `unknown`（D9-3'） |
| P1-6 | P1 | `policy_pin` 识别正式库不可靠 | 库角色标记 evaluation / production；pin 只接受评测库，普通实例拒开评测库（D9-3'） |
| P1-7 | P1 | 部署级事件幂等键吞重复事件 | key 带激活序号 / 评测 id / 回执哈希；冷却从任意激活算；版本库事件折叠投影与表比较（D9-2'、D9-8'） |
| P1-8 | P1 | 批准未绑定评测基线 / 代码版本 | 晋级要求评测基线 = 当前 ACTIVE、包版本 = 当前代码、批准绑定最新评测（D9-8'） |
| P1-9 | P1 | `policy/` 通道不必要且扩大攻击面 | 撤掉；夹带修改记 `PolicySuggestionRefused`；人工出路 `policy propose --params --as`；登记残余风险（D9-10'） |
| P1-10 | P1 | 快照纳入绑定版本会让第 8 步测试变红 | 快照只记部署级 ACTIVE（种子先于开始快照）；Mission 绑定写进各自证据（D9-3'） |
| P1-11 | P1 | 角色 / Prompt 候选版本与第 8 步移交项未处置 | 白名单加 `prompt_versions`；全零权重即 Allocator 消融；provider 身份补全；其余登记；"学习型候选"登记为偏离（D9-1'、D9-12'） |
| P2-1 | P2 | R1 因果基础弱 | 只用有竞争的分配（v6 记 eligible / slots）；Attempt 级探索；按 Mission 计样本；`uncertainty` 不参与；写明启发式（D9-5'） |
| P2-2 | P2 | R2 可能不生效 | `by_role` 覆盖时判"不适用"；区分 no_change 与 insufficient（D9-5'） |
| P2-3 | P2 | 可信度检查代价 | 每库复制一次批量回放；只取终态 Mission（D9-5'） |
| P2-4 | P2 | fixture 门槛评测可行性 | 显式并发、脚本配足（D9-7'） |
| P2-5 | P2 | 回滚语义 | 目标规则写死、回执列在途 Mission、限定回滚时刻前的事件、按版本健康报告 + 人工回滚（D9-8'） |
| P2-6 | P2 | 路由目标 profile 缺失会拖垮实例 | `PolicyRouteUnavailable` + 回落部署规则（D9-4'） |
| P2-7 | P2 | S9-08 演练要可观测 | gate + 小 `max_running_attempts` + 每周期峰值 / 账本 / 部署政策哈希 + 注入时钟 + 路由幅度（D9-8'、D9-9'） |
| P2-8 | P2 | 部署级事件不进按 Mission 的证据 | 演示另写 `policy_events.jsonl`、`registry.json` 并做密钥扫描（D9-11'） |
| P2-9 | P2 | 既有测试与接口 | step02 测试改写保留语义；查第 5 步精确断言；`public-api.json`（D9-11'） |
| P2-10 | P2 | 切片与可砍范围 | C 只做训练侧；泄漏移到 D；`policy list/show` 最简；沿用 pytest 插件扫描（§6.1） |

### 实现前摸底（只读，plan review 期间）

要改为"读 Mission 绑定策略"的决策点（`src/agent_orchestrator/orchestrator/event_handler.py`，基线 `c9d4879`）：`manager_after_failures` 1758；`max_manager_rounds` 2021 / 2027 / 2074；`no_progress_limit` 2073 / 2168 / 2174 / 2250；`allocate(...)` 2627–2637（`max_concurrency` / `candidates_per_task` / `aging_window_seconds` / `reduced_concurrency_ratio` / `exploration_slots`）；候选预算分摊 2857；`create_attempt(candidates_per_task=…, max_open_attempts=max_concurrency)` 2907–2909；模型路由在 202 行只构造一个 `ModelRouter`（按版本需要多个）。打分权重：`scheduling/allocator.py:163` 直接读模块常量 `WEIGHTS`，`TaskScore.version` 固定为 `allocator-v1`。其他可复用：`governance/permissions.py` 的 `Principal`（只许 human）、`decision_receipt_hash`；`human_commits.py` 的 `_decision` / `_book_decision` 模式（但 `insert_decision` 写的是 `approval_decisions`，外键到 `approvals`，策略审批要新表）；schema 迁移按 `;` 切分 DDL、应用前备份；`Store.snapshot` 按表是否存在取数（新表照此处理）；step02 `test_cli_demo.py:76-89` 断言 `policy-promotion` 未实现，第 9 步实现后改写。

## 2. 执行记录

| 切片 | 提交 | 内容 | 测试 |
|---|---|---|---|
| A | `d5b740d` | 版本库：`governance/promotion.py`（白名单与范围、`resolve_params` 展开、内容寻址版本 id、提议 id、幅度 / 扩张检查、解释器与代码版本、任务身份哈希、版本库事件折叠投影与一致性检查）；schema v6 六张表与 v5 迁移时的 `policy-legacy` 绑定；Store 读写；`orchestrator/policy_commits.py`（种子、提议、拒绝提议、评测记录、人工决定、晋级、回滚、绑定、配置漂移）；`create_mission` 同一事务绑定版本并在 `MissionCreated` 记 `policy_version_id`；角色模板版本登记 | `test_policy_registry.py` 12（白名单 / 内容寻址、任务身份、S9-01 提议身份、S9-02 未通过不可批准晋级、状态表与重评作废批准、基线 / 代码 / fixture 证据条件、S9-08 幅度 / 冷却 / 背压、S9-05 回滚目标、重复激活事件与折叠投影、仅人工决定与 nonce、v5 迁移 legacy）；`tests/orchestrator` 全套 333 passed |
| B | `12fdb13` | 绑定与按版本运行：库角色标记（`scheduler_state.library_role`：production / evaluation；钉版只进评测库，普通实例拒开评测库）；正式库 `__aenter__` 物化种子版本（记配置哈希与来源）、配置白名单项与 ACTIVE 不同时记 `PolicyConfigDrift`（ACTIVE 仍生效）；`submit_mission` 带 `provider_kind`（按 provider 类判定，可显式传入）与评测钉版；`policy_for` / `_router_for`（每个版本一个 ModelRouter，缺失 profile 记 `PolicyRouteUnavailable` 并回落）；全部决策点改读绑定版本（分配权重 / 候选数 / 探索配额 / 并发上限 / aging、候选预算分摊、提交守卫、Manager 三个阈值含恢复路径、Attempt 与 Planner / Manager / Critic 路由和模板版本）；intent 与 `AllocationDecided` 记 `policy_version_id` / `weights_hash` / `eligible` / `slots`；恢复时解释器版本不同记 `PolicyInterpreterDrift`；Replay 可选正式字段 `mission.policy_version_id` | `test_policy_binding.py` 6（S9-04 新 Mission 按生效版本运行、在途 Mission 换配置恢复仍用绑定版本且记漂移、结构测试"无决策点读白名单配置"、S9-05 回滚不动既有事件与费用且回放 100%、库角色隔离、解释器漂移）；`tests/orchestrator` 全套 339 passed |
| C | `1aacc86` | 规则改进器 `governance/learning.py`（`rules-v1`，报告标"规则改进（启发式规则，非训练模型）"）：历史库各复制一次只读打开；只取终态 Mission，`provider_kind` 未知（旧库）的排除并列出；每个训练 Mission 回放覆盖率 100% / 0 不一致 / 0 缺口且归因对账，否则整份拒绝并点名；fixtures 与 real 混用拒绝；样本不足、适用规则都缺样本 → insufficient，样本够但规则不触发 → no_change，均不登记、记 `PolicyProposalRefused`；R1 只用有竞争的分配（`eligible > slots`）、按 Mission 计样本、`uncertainty` 不参与、最多动两项各一档；R2 默认档位首次失败率 Wilson 下界 ≥ 0.5 且 ≥ min_group 个 Mission → 该任务种类路由到升级目标（`by_role` 覆盖时不适用）；信誉统计（角色 × prompt 版本 × profile）；提议来源含训练集（Mission、任务身份、库 sha256、时间、终态、provider 种类）、代码与解释器版本、逐规则统计 | `test_policy_learning.py` 7（S9-01 候选可追溯且历史库不变、同输入同提议；S9-07 样本不足不登记、fixtures + real 混用拒绝、删事件的不可信记录点名拒绝；no_change；适用规则缺样本 → insufficient；R1 单元） |
| D | `f44e228` | 门槛评测 `governance/gates.py`：生效版本与候选各钉进一个策略（`Strategy.policy_pin`），每次运行在自己的新评测库里（库角色 evaluation、Mission 以 sandbox 绑定）；评测 case 可带多个 runtime profile 与路由（`EvaluationCase.profiles` / `routing`），派生 case 记来源时间；门槛与第 8 步同口径：每个 case 每一方的样本下限（真实 3、fixture 1）、任一方脚手架错误 → INSUFFICIENT（不判 FAILED）、逐 case 成功率非劣、oracle 误判不增、候选 tokens 最小值高于基线最大值才判成本 FAILED、等待人工不增、白名单外快照差异 → FAILED；PASSED 写"非劣：样本内未见退化"；评测前按任务身份哈希（去掉租户与幂等键）、派生来源 Mission、来源时间做防泄漏检查，泄漏直接拒绝、不运行；结论经 Commit 记入提议（报告哈希、基线版本、代码版本、证据种类），另写 `gate.json` | `test_policy_gates.py` 4（S9-03 合格候选只在评测库运行、正式库无候选绑定、未批准不能晋级；S9-02 路由到必败档位的候选 FAILED 且不可批准；样本不足与脚手架错误都是 INSUFFICIENT；S9-07 同任务 / 派生自训练 Mission / 来源早于窗口的泄漏在运行前拒绝）；`tests/orchestrator` 全套 350 passed |
| E | `a3427b2` | 防护与稳定性：结果接受事务内，Worker 已接受产物落在 `policy/` 或名为部署配置文件的 → `PolicySuggestionRefused`（Mission 时间线，列出键与核心规则键），不进版本库、不改任何规则；Manager 提议解析前，封闭词表之外且点名策略项的操作 → `PolicySuggestionRefused`，随后照旧被封闭词表拒绝；`api/policies.py` 的 `PolicyApi`（身份只来自人类 `Principal`；人工参数须在白名单与范围内，来源 `human:<id>`、标"人工参数，非规则改进"；批准 / 拒绝 / 晋级 / 回滚；按版本健康报告）；部署政策新增 `policy_cooldown_seconds`（默认 600）；分配记录写入当时的背压状态 | `test_policy_guard.py` 5（S9-06 Manager 夹带配置操作被拒且版本库不变、Worker 写 `policy/` 文件被拒、只有人能提出且只能在白名单内；S9-08 冷却 / 背压限制晋级而回滚即时、任何版本不含安全与预算项；高负载实跑：背压升起、RAISED 下并发 ≤ 1、所有分配 ≤ 部署上限、运行峰值有界、预算不超支）；`tests/orchestrator` 全套 355 passed |

## 3. 真实模型

## 4. 回归与 wheel

## 5. 遗留

## 6. 结论
