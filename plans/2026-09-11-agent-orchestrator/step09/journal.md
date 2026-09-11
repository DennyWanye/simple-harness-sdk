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

## 3. 真实模型

## 4. 回归与 wheel

## 5. 遗留

## 6. 结论
