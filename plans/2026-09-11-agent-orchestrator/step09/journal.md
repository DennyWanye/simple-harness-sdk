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

### 代码评审（第 1 轮）

独立代码评审（claude-opus-5，只读；范围 `a8ce3b6..c519a1c`；原文要点见 `reports/code-review-round1.md`）：0 P0 / 2 P1 / 11 P2。评审者用 pytest 插件在 `tests/orchestrator` 全部测试的每个 Orchestrator 关闭前扫描了 205 个库、231 个 Mission：回放 100% / 0 不一致 / 0 缺口、绑定行与 `MissionCreated.policy_version_id` 一致均 0 例违反；2 例归因未对账（step02 重复投递、step06 模型回显不符）在第 8 步代码上同样，是预留被按设计扣住（`ReservationHeld`）后"未结算用量"如实判未对账，不是缺陷。处置（修复切片 G）：

| # | 级别 | 发现 | 处置 | 测试 |
|---|---|---|---|---|
| P1-1 | P1 | 升级后的旧库回放覆盖率掉到 0.909，`replay` 退出 1 | 修：`policy-legacy` 绑定不输出 `policy_version_id`（它不是这些 Mission 的事件能决定的正式状态） | `test_policy_binding.py::test_review_p1_1_*`（把已完成 Mission 改成迁移后的样子，回放覆盖率 1.0） |
| P1-2 | P1 | S9-08 演练没有"高负载下频繁调整"，断言偏空 | 修：重写演练——Attempt 在途时于两次运行周期之间交替晋级 / 回滚、RAISED 下扩张被拒、中途新建 Mission 绑定当时的版本；每周期断言每个 Mission 的开放 Attempt ≤ min(绑定 `mission_concurrency`, 部署上限)、各 Mission 与 Global 账户不超支、部署配置快照哈希不变 | `test_policy_guard.py::test_s9_08_frequent_changes_under_load_*` |
| P2-1 | P2 | Manager 越界操作漏报 | 修：封闭词表外的操作一律记录（含操作名），合法操作夹带策略 / 核心键也记录 | `test_s9_06_a_manager_*`（加 `disable_code_test` 与夹带 `max_concurrency`） |
| P2-2 | P2 | Worker 检测只认根目录 `policy/` 与三个文件名 | 登记（§5）：嵌套路径 / 大小写变体漏报、用户项目的同名文件误报，均只影响审计记录 | — |
| P2-3 | P2 | 评测库版本库一致性必然报错 | 修：一致性检查跳过钉版的 sandbox 版本 | `test_policy_gates.py::test_review_p2_3_p2_8_*` |
| P2-4 | P2 | CLI 只读命令写库（新建空库、迁移 v5 库） | 修：list / show / status 复制后只读打开；库不存在退出 2 | `test_policy_promotion_closure.py::test_review_p2_4_*` |
| P2-5 | P2 | `--cooldown` 可绕过部署冷却；nonce 自动生成；人工提议不知道部署并发与 profiles | 部分修：去掉 `--cooldown`，冷却只取部署政策；nonce 自动生成与 CLI 提议不带部署信息登记（§5） | — |
| P2-6 | P2 | provider 种类只读声明字段、只看默认档位 | 修：按每个 profile 的 provider 类判定，全部是 fixture 才记 fixtures | `test_review_p2_6_*` |
| P2-7 | P2 | `PolicyRouteUnavailable` 每进程只记第一个 Mission；`PolicyConfigDrift` 回摆不再记 | 修：路由缺失按 Mission 各记一次；配置漂移每次打开都记 | 既有测试 |
| P2-8 | P2 | 对已晋级 / 已拒绝提议先跑完评测才被拒 | 修：运行前拒绝 | `test_review_p2_3_p2_8_*` |
| P2-9 | P2 | Commit 层 `propose_policy` 不校验参数 | 修：单一写入者检查参数结构（恰好是白名单项），范围与模板仍由 API / 学习器检查 | `test_policy_registry.py::test_review_p2_9_*` |
| P2-10 | P2 | 缺 prompt 版本运行时切换、两侧任务身份一致的测试 | 修：补测试（登记第二个 Worker 模板，绑定版本的 Attempt 用它；学习器侧与门槛侧哈希一致） | `test_review_p2_10_*` ×2 |
| P2-11 | P2 | R2 的 Wilson 区间按首次 Attempt 计、DAG 型 Mission 样本不独立 | 登记（§5），启发式，报告已写明 | — |

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
| F | `c519a1c` | CLI 与演示：`policy propose`（`--history` 规则改进 / `--params` 人工参数）/ `evaluate`（fixtures 留出 case 或真实 parse-kv）/ `approve` / `reject` / `promote`（冷却默认取部署政策、`--accept-fixture-evidence`）/ `rollback` / `list` / `show` / `status`（含版本库一致性），退出码 0 / 1（规则或门槛拒绝）/ 2（用法）；`demo --scenario policy-promotion`（history 六个 Mission → 正式库：样本不足拒绝、规则改进器候选（R2 路由 code→large）、人工候选路由到 flaky 被门槛拒绝、学习候选门槛通过且未批准不能晋级、晋级前创建的 Mission 仍按种子运行、晋级后按候选路由、回滚后既有事件与用量不变；`summary.json` / `registry.json` / `policy_events.jsonl` / 每个正式 Mission 的证据）；夹具 `policy_demo_profiles` / `policy_demo_routing` / `policy_spec`；step02"未实现"测试改为"全部场景已实现、未知场景退出 2"；真实门槛 opt-in；版本 0.9.7 / 0.9.0 | `test_policy_promotion_closure.py`（演示闭环 S9-01…07 + CLI 拒绝路径）；`tests/orchestrator` 全套 356 passed / 8 skipped |
| G | `88e5582` | 代码评审第 1 轮修复（处置表见 §1）：legacy 绑定不计入回放覆盖率；S9-08 演练改为高负载下周期间交替晋级 / 回滚并逐周期核对边界；Manager 越界操作一律记录、合法操作夹带策略键也记录；评测库一致性跳过 sandbox 版本；CLI 只读命令复制后只读打开、库不存在退出 2、去掉 `--cooldown`；provider 种类按每个 profile 的 provider 类判定；路由缺失按 Mission 各记、配置漂移每次打开都记；已晋级 / 已拒绝提议评测前拒绝；单一写入者检查参数结构 | step09 新增 `test_review_*` 7 条与重写的 S9-08 演练；SDK 全量回归红集 = 基线 |

## 3. 真实模型

- 门槛评测运行 1（`c519a1c`，deepseek-flash，`reports/real-policy-gate-run1.md`）：人工候选 `no_progress_limit=3` 对生效种子版本（2），留出 case `parse-kv` 各 2 次真实运行；active 1/2（一次因模型连续交空结果、达到 `no_progress_limit=2` 以 no_progress 停止）、candidate 2/2；门槛结论 **INSUFFICIENT**（每一方样本 2 < 3），CLI 退出 1，提议不可批准晋级；证据 149 个文件，真实密钥命中 0，`\bsk-` 命中 0。只用 flash 做不了"换模型"的真实对比（L6-1），候选选的是人工参数。

## 4. 回归与 wheel

- 每个切片提交前都跑 `tests/orchestrator` 全套：A 333、B 339、D 350、E 355、F 356 passed（真实模型 opt-in 跳过）。
- SDK 全量回归（代码评审修复之后，wheel 源）：58 failed / 2403 passed / 13 skipped / 15 errors，红集 73 条 = 基线，**0 新红**。
- 版本：simple_harness 0.9.7 / agent_orchestrator 0.9.0（`tests/unit/contracts/public-api.json` 同步）；编排库 schema v6。
- wheel：自 `88e5582`（`SOURCE_DATE_EPOCH=1789133695`）可复现构建 `simple_harness_sdk-0.9.7-py3-none-any.whl`，sha256 `291c824deeaf70c5c535413f3b2cb405c615e8654c216739eb1e1940ea4c06d2`；wheel 在干净 venv（Python 3.12）安装后 626 passed / 1 failed（基线已知的 `test_execution_v3_to_v4_migration::test_completed_null_continuation_*`）/ 11 skipped，multi-mission、approval-action、evaluate-policies、policy-promotion 四个演示与 `replay --attribution`、`policy status` 退出码都是 0；版本 0.9.7 / 0.9.0（脚本 scratchpad `wheel-0.9.7/build-and-verify.sh`）。

## 5. 遗留

- **偏离（如实登记）**：纲要 §11.2"有足够数据时实现学习型候选"本步未做——历史数据量不足以训练，候选生成只有规则改进（`rules-v1`，启发式）；报告与记录一律写"规则改进（启发式规则，非训练模型）"。
- 代码评审登记：Worker 策略文件检测只认根目录 `policy/` 与三个固定文件名，嵌套路径 / 大小写变体漏报、用户项目同名文件误报（CR P2-2，只影响审计记录）；CLI 不传 `--nonce` 时自动生成、`policy propose --params` 不知道部署的并发上限与 profiles（CR P2-5 余项）；R2 的 Wilson 区间按首次 Attempt 计、DAG 型 Mission 的样本不独立（CR P2-11，启发式）。
- 残余风险（plan D9-10'）：`run_tests` 是普通子进程、无文件系统隔离（第 7 步遗留 L2-4 / L6-5），CLI `--as` 为自报身份——"Agent 不能批准 / 晋级"只在 API 层成立。
- 角色配比（原文 §29.2 `ROLE_MIX_START`）未做成可晋级参数（角色按任务种类选取）；Retrieval 版本运行时切换、新 Verifier 重判旧产物、`snapshot_diff` 字段级来源、回放覆盖更宽场景、派生 case 按场景指定 provider 工厂——第 8 步移交项中本步未做的部分，继续登记。
- 只用 flash：路由类候选的"换模型"只由 fixtures 证明（L6-1）；真实门槛评测用的是人工参数候选。价目未注入，真实金额为 null（L2-6、L6-2）。

## 6. 结论

**SHIPPED**：simple_harness 0.9.7 / agent_orchestrator 0.9.0，wheel 源提交 `88e5582`（sha256 `291c824deeaf70c5c535413f3b2cb405c615e8654c216739eb1e1940ea4c06d2`）。

| 验收 | 结果 | 证据 |
|---|---|---|
| S9-01 候选可追溯到训练数据、代码、参数和评测版本 | PASS | `test_policy_learning.py::test_s9_01_*`（训练集 Mission / 任务身份 / 库 sha256 / provider 种类、代码与解释器版本、逐规则统计；同输入同提议；历史库不变）、`test_policy_registry.py::test_s9_01_*`、演示 `summary.json.learned` |
| S9-02 未通过门槛不上线、保留理由、旧策略继续 | PASS | `test_policy_gates.py::test_s9_02_*`、`test_policy_registry.py::test_s9_02_*`、演示 `flaky` |
| S9-03 合格未批准只在评测库运行 | PASS | `test_policy_gates.py::test_s9_03_*`（评测库角色 evaluation、sandbox 绑定、正式库无候选绑定、未批准晋级被拒） |
| S9-04 审批后上线、新 Mission 绑定新版本、在途不被静默改策略 | PASS | `test_policy_binding.py::test_s9_04_*`、结构测试、演示 `missions.before/after` |
| S9-05 快速回滚、既有事件与费用保留 | PASS | `test_policy_registry.py::test_s9_05_*`、`test_policy_binding.py::test_s9_05_*`（回放 100%）、演示 `rollback` |
| S9-06 在线 Agent 改安全阈值被拒、人工审查出路 | PASS | `test_policy_guard.py::test_s9_06_*` |
| S9-07 数据不足 / 污染 / 泄漏拒绝晋级、无虚假提升结论 | PASS | `test_policy_learning.py::test_s9_07_*`、`test_policy_gates.py::test_s9_07_*`、真实 flash 门槛 INSUFFICIENT（§3） |
| S9-08 高负载下频繁调整有边界、安全 / 预算上限始终有效 | PASS | `test_policy_guard.py::test_s9_08_*`（周期间交替晋级 / 回滚、RAISED 扩张被拒、每周期开放 Attempt / 账户 / 部署配置哈希核对）、`test_policy_registry.py::test_s9_08_*` |

门：step09 全部通过（真实模型 opt-in 跳过）；SDK 全量回归红集 ⊆ 基线（§4）；wheel 在干净 venv（Python 3.12）安装后 626 passed / 1 failed（基线已知的 `test_execution_v3_to_v4_migration::test_completed_null_continuation_*`）/ 11 skipped，multi-mission、approval-action、evaluate-policies、policy-promotion 四个演示与 `replay --attribution`、`policy status` 退出码都是 0；plan 评审与代码评审全部处置（§1）；真实 deepseek-flash 门槛评测（§3）。ORCH-BUILD-v1.0 第 2–9 步至此全部交付。
