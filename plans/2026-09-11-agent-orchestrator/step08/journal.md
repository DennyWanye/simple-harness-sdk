# 第 8 步 · 执行记录

## 1. 关键裁决

独立 plan review（claude-opus-5，只读；原文要点见 `reports/plan-review-round1.md`）4 P0 / 13 P1 / 8 P2，处置写在 plan §6（D8-x'）；评审结论出来之前只做了只读探查（`reports/event-coverage-probe.md`），没有写实现。

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| P0-1 | P0 | S8-02"在已覆盖字段上一致"可空洞通过；动作 CANCELLED 无事件，其他动作状态只能反推 | 本版本库覆盖率必须 = 100%；字段集逐项列出、排除项登记；补 `ActionCancelled`；反推规则写进投影表（D8-2'） |
| P0-2 | P0 | 评测 / 派生 case 得不到新 Mission id，已有目录会静默复用旧 Mission | 幂等键 `eval:<plan>:<strategy>:<case>:<trial>`；运行目录必须新建；派生改写 key 并记录原值（D8-6'、D8-8'） |
| P0-3 | P0 | Critic 消融记 `NOT_REQUIRED(ablated)` 违反 §12.4 | 消融 = 有效政策显式变更（Router 入口去掉 critic_review，`detail.ablated=true`），报告标"消融政策下的 PASS"（D8-7'） |
| P0-4 | P0 | 策略配置覆盖、派生 case 能绕过安全边界 | 覆盖改白名单；评测只允许测试连接器；带动作的派生 case 只在测试连接器下运行；各配拒绝测试（D8-7'） |
| P1-1 | P1 | `mode=ro` 仍会建 `-shm` / `-wal`；`Store.open` 会迁移、备份 | 复制到临时目录 + `Store.open_readonly`；"不写"证明含 wal、执行库、只读目录（D8-1'） |
| P1-2 | P1 | seq 全库自增无法定位缺失；`list_events` 截断 | 结构性不变量 + 与库事件集比对；分页读取（D8-1'、D8-2'） |
| P1-3 | P1 | 外部调用证明不全；崩溃用例偏弱 | 计数含 pytest / Critic / 执行库哈希；导入图测试；崩溃时刻复制库做前缀投影（D8-1'、D8-3'） |
| P1-4 | P1 | 费用主体划分漏 Task Critic 等，三项之和是恒等式 | 逐行分类 + 未归类桶为空；unknown 单列；工具调用、动作预留、人工时间单列（D8-4'） |
| P1-5 | P1 | 成功路径界定不可判定 | 多 Task、冲突、候选、改图、人工、失败 Mission 六条规则逐条写明并配用例（D8-4'） |
| P1-6 | P1 | fixtures 多策略多试验会耗尽脚本而挂起 | 每次试验新 provider；墙钟超时 → `harness_error`；fixture 结果标"机制验证"（D8-6'） |
| P1-7 | P1 | "差 >1 即可下结论"站不住 | Fisher 精确检验 + Wilson 区间；耗时 / 成本区间不重叠才写差异（D8-6'、§6.1） |
| P1-8 | P1 | 快照不全、会随路径 / pid 漂移 | 全字段枚举 + 排除清单 + 归类测试；补全版本与 provider 身份；开始与收尾各算一次（D8-5'） |
| P1-9 | P1 | 没有切换 Prompt / Allocator / Retrieval 版本的机制 | 登记：运行时版本注册表是第 9 步；本步比较模型 / profile、消融、白名单配置；版本常量变化由快照差异列出（D8-5'） |
| P1-10 | P1 | Critic 消融下自由文本准则必然失败；与 needs_human / human_review 交互 | critic 消融只配 `pytest:` / `file:` 准则；等待人工单列终态类别；连带影响写全（D8-7'、D8-6'） |
| P1-11 | P1 | `dynamic_graph` ≠ 原文"动态调度" | 改名 `graph_changes` 并登记；本步不做 Allocator 消融（D8-7'） |
| P1-12 | P1 | "验证误判"不应直接 null | 可选隐藏 oracle，算"PASS 但 oracle FAIL"比例（D8-6'） |
| P1-13 | P1 | 派生 spec 保真性 | 重算 spec 哈希与旧库 `MissionCreated.spec_hash` 比对；只支持本版本演示证据（D8-8'） |
| P2-1 | P2 | 新 Verifier 重判旧产物的变体 | 登记，属第 9 步 |
| P2-2 | P2 | 污染率 / 剪枝率 / 故障恢复 / 新思路数定义 | 按评审修正（§6.1） |
| P2-3 | P2 | 人工时间是成本 | 归因单列人工等待时间（D8-4'） |
| P2-4 | P2 | 证据文件来源的 payload 可能被脱敏 | 优先库，文件来源在报告里标注（D8-1'） |
| P2-5 | P2 | step02 未实现检查与文档串 | 改用 `policy-promotion`（D8-9'） |
| P2-6 | P2 | 价目注入与 L6-1 | 有 `SH_PRICE_*` 时注入，否则 null 并登记；L6-1 登记（D8-5'） |
| P2-7 | P2 | 切片顺序 | Replay 先行（§6 切片调整） |
| P2-8 | P2 | 可砍范围 | 派生只支持本版本演示证据；归因并入 `replay --attribution`；库内去重不另测（D8-8'、D8-9'） |

### 代码评审（第 1 轮）

独立代码评审（claude-opus-5，只读，范围 `a24e18a..49106bf`；原文要点见 `reports/code-review-round1.md`）1 P0 / 5 P1 / 11 P2，结论"还不能收尾"。处置如下，每项配决定性测试（修复切片 F）：

| # | 级别 | 发现 | 处置 | 测试 |
|---|---|---|---|---|
| P0-1 | P0 | "评测只许测试连接器"没有强制：无 action 准则的 case 可带支付连接器；运行时实际拿到的服务没查 | 修：`_refuse_services` 在 `validate`（每个带连接器的 case，不论有无 action 准则）与 `_run_once`（每次运行实际拿到的服务，建 Orchestrator 之前）各查一次，名字必须是 `test_config` 且为 `TestConfigService`；不合规抛 `EvaluationRefused`，整份评测拒绝，不记 harness_error | `test_evaluation.py::test_review_p0_1_*`（无 action 准则的支付连接器被拒；工厂第二次调用才塞支付连接器 → 运行被拒，库 0 个，无报告） |
| P1-1 | P1 | 冲突落败方的已接受 Attempt 仍在成功路径内 | 修订 plan（D8-4''，依据理论 12 §16"不能只把功劳给最终提交者"）：Attempt 自己的已接受产物在集成树 / 依赖闭包内就留在路径内，标 `claim_refuted=true` 并列入 `knowledge_path.refuted_on_path`；只经被驳倒 Claim 挂上路径的算探索（原因 `claim_refuted`） | `test_attribution.py::test_review_p1_1_*` |
| P1-2 | P1 | 删掉 `VerificationPassed` / `MissionCompleted` 等结果事件后无缺口，旧值被算作已决定 | 修：结构性不变量补齐——Mission 已判定却无终态事件；终态 Mission 仍有开放的 Task / Attempt / Result；已完成 Mission 的动作无结果、冲突仍 OPEN；已完成 Task 的已接受结果不是 DONE/PASS；有 `VerificationPassed` 无 `TaskCompleted`。违反即写缺口，并把对应字段置为未决定（`not_covered`，不再以旧值计入覆盖率）；`replay_mission` 对库中不存在的 Mission、文件中没有该 Mission 事件时报错 | `test_replay.py::test_review_p1_2_*`（参数化删除 `VerificationPassed` / `MissionCompleted` / `ActionSucceeded` / `ConflictResolved`，只用文件与带库两种方式；另测"通过而未完成"） |
| P1-3 | P1 | Critic 消融可让有效政策为空，零层验证 PASS | 修：消融后有效集合为空时，被消融的层记 `ERROR`（`detail.no_layer_left=true`）并短路，结果不能 PASS | `test_ablation.py::test_review_p1_3_*`（Task 政策只有 `critic_review` + 消融 critic → ERROR、Task 无已接受结果、Mission 未完成、Critic 调用 0） |
| P1-4 | P1 | `reconciled` 是恒等式 | 修：`reconciled` = 未归类 0 行 ∧ 路径内 + 探索 + 服务 = 总量 ∧ 预算账本（已结算预留的 `settled_tokens`）逐主体与用量一致；报告新增 `cost.ledger` | `test_attribution.py::test_review_p1_4_*`（插一行未知主体用量 → False；改一条结算量 → False 且列出该主体） |
| P1-5 | P1 | 比较未按 case 成对；脚手架错误不对称被忽略；fixture 下耗时 / tokens 仍可能写差异 | 修：`per_case`（逐 case 成功数、Fisher p、哪一方更高），方向不一致写"证据不足：各 case 的成功率方向不一致"；两策略脚手架错误数不同时成功率、耗时、tokens 都写"证据不足"；fixture 三项都写"不适用"；样本不足时耗时 / tokens 也写"证据不足" | `test_evaluation.py::test_review_p1_5_*`（辛普森反例、方向一致、脚手架错误不对称）、`test_time_and_token_differences_need_enough_samples_too` |
| P2-1 | P2 | ablations 字符串、计划配置类型错误到运行时才每次 harness_error | 修：`validate` 先为每个策略构造配置；ablations 为字符串、`global_budget` / `price_table` 类型错误直接拒绝；CLI 把计划里的 `global_budget` dict 转成 `Budget` | `test_review_p2_1_*` |
| P2-2 | P2 | approval-action 开始快照在运行后才算；multi-mission 无开始快照 | 修：两个演示都在运行前取开始快照写进 baseline | `test_policy_snapshot.py`（演示证据无漂移） |
| P2-3 | P2 | provider 身份对 `OpenAICompatibleProvider` 只记类名；fixtures 缺脚本摘要 | 登记（§5） | — |
| P2-4 | P2 | 快照差异来源粒度粗；评测拿各策略最后一次运行的快照比较 | 部分修：评测比较同一 case 的两策略快照（`policy_differences_case`）；来源细化登记（§5） | S8-03 断言 `policy_differences_case` |
| P2-5 | P2 | CLI 错误处理 | 修：`replay` 无库无文件、文件不存在、Mission 不存在、`--attribution` 无库 → 退出 2；有缺口或覆盖率 < 1 → 退出 1；`evaluate` 计划文件不存在、未知 case → 退出 2；有脚手架错误 → 退出 1 | `test_replay_evaluate_cli_errors.py` |
| P2-6 | P2 | 断点使 Attempt 被归为"未被最终产物使用"；断点重复 | 修：断点去重；断点 Task 的 Attempt 探索原因为 `record_missing` | S8-05 断点测试 |
| P2-7 | P2 | 回放 100% 覆盖的场景不够宽 | 部分修：新增四类删事件用例；更宽的场景登记（§5） | 同 P1-2 |
| P2-8 | P2 | "不外调"测试的 monkeypatch 探针形同虚设 | 修：删掉该探针，由导入图测试 + 库 / 执行库 / 测试服务哈希证明 | `test_s8_02_replay_never_writes_*` |
| P2-9 | P2 | 评测报告没写消融连带影响 | 修：`ABLATION_EFFECTS`；报告 `strategies[].ablation_effects`，中文报告"消融连带影响"一节 | S8-03 断言 |
| P2-10 | P2 | 派生 case 未校验证据版本；未按场景指定 provider | 部分修：证据 `baseline.agent_orchestrator` 必须等于本版本；按场景名指定 provider 登记（§5） | S8-06 断言"own version" |
| P2-11 | P2 | harness_error 记录不带 Mission id | 修：带 `idempotency_key` 与由它导出的 `mission_id` | `test_a_failure_and_a_broken_harness_are_told_apart` |

### 代码复核（第 2 轮，针对切片 F）

独立复核（claude-opus-5，只读；原文要点 `reports/code-review-round2.md`）在 step03–08 全部测试的 178 个 Mission 上做了回放 / 对账 / 零层 PASS 扫描：1 P1 / 6 P2，结论"范围小，修完跑 step08 与回归即可收尾"。全部修复（切片 G）：

| # | 级别 | 发现 | 处置 | 测试 |
|---|---|---|---|---|
| P1-A | P1 | `ResultRejected`（Attempt → RETRY_WAIT）未投影，合法库被报"缺记录"、覆盖率 < 1 | 修：`ResultRejected` 且原因不是 `superseded` → Attempt RETRY_WAIT（Task 保持 ACTIVE）；迟到结果（`superseded`）只作历史；从"无正式状态事件"清单删除 | `test_replay.py::test_re_review_a_rejected_result_then_a_retry_replays_completely`（覆盖率 1.0、0 缺口、0 不一致） |
| P2-1 | P2 | 测试服务可用子类；可跨运行共用状态文件 | 修：必须 `type(s) is TestConfigService` 且状态文件在本次运行目录（校验时为探针目录）下 | `test_evaluation.py::test_re_review_the_test_service_is_exactly_that_class_under_the_run_directory` |
| P2-2 | P2 | 被仲裁取代的 VERIFIED 知识的来源 Attempt 未标 `claim_refuted`；P1-1 探索分支无测试 | 修：经 `resolves` 边进入 lineage 且状态 SUPERSEDED 的知识，其来源 Attempt 计入被驳倒集合 | `test_attribution.py::test_re_review_knowledge_an_arbitration_superseded_is_refuted_too`（探索分支：`on_success_path=false`、原因 `claim_refuted`） |
| P2-3 | P2 | 未结算用量不影响 `reconciled`；账本并非完全独立来源 | 修：终态 Mission 还要求 `unsettled_usage_tokens == 0`；代码注释写明账本是结算时对同一用量表求和，能发现结算后导入或被改的用量，不是独立计量 | `test_re_review_a_finished_mission_with_usage_left_unsettled_is_not_reconciled` |
| P2-4 | P2 | Critic 消融后 Task 级自由文本准则无人判定未写明 | 修：写进 `ABLATION_EFFECTS["critic"]`（报告"消融连带影响"） | S8-03 断言连带影响非空 |
| P2-5 | P2 | 投影到 `AttemptStarted` 才把 Task 设为 ACTIVE，库在 `AttemptCreated` 同事务设置 | 修：`AttemptCreated` 时 READY 的 Task → ACTIVE | `test_re_review_a_created_attempt_makes_its_ready_task_active` |
| P2-6 | P2 | `cmd_evaluate` 未写退出码 | 修：docstring 写明 0 / 1（有脚手架错误）/ 2（坏计划或被拒） | — |

## 2. 执行记录

| 切片 | 提交 | 内容 | 测试 |
|---|---|---|---|
| A | `211500d` | Replay：`observability/replay.py` 纯折叠（按 seq 排序、按事件 id 去重）、投影表与推导规则（含非候选结果、审批→动作、审核→结果回到 RUNNING、冲突任务失败→UNRESOLVED）、结构性缺口、与只读副本快照逐字段比较、失败时间线；`Store.open_readonly` / `iter_events` / `has_table`（快照按表是否存在）；证据与指标改为分页读事件；补事件 `ActionSuperseded`、`ActionCancelled` | `test_replay.py` 12：四个演示 + 失败 Mission + 审批被拒 + 取消开放动作 + 人工审核挂起 / 通过，覆盖率 100% 且 0 不一致；重复投递；崩溃前缀；只读目录、不写、不外调、不导入 runtime；S8-05 删事件 / 缺字段 |
| B | `e52b9d4` | 贡献归因 `observability/traces.py`：最终产物 = 集成树（`merge_accepted`），产出者 Task / Attempt / Agent / 角色 / 模型 / profile / prompt 版本与通过的验证层；依赖闭包内的 Task 在路径内（被覆盖的上游产物标注）；知识路径来自 lineage，被驳倒的 Claim 所在 Attempt 列为探索；探索消耗逐条带原因；用量逐行归类（Attempt、其 Critic、planner / manager / judge、未归类桶必须为空、unknown 行单列），工具调用、动作预留、人工时间单列，未定价金额 null；记录缺失列为断点、不补边。策略快照 `governance/policies.py`：`SNAPSHOT_FIELDS` 逐字段归类（未归类即报错）、`VERSION_SOURCES` 版本常量带来源、全部角色模板版本、profiles / routing / 连接器 / provider 身份（不含密钥）、`snapshot_diff`；编排器 `policy_snapshot()`；证据 `attribution.json`、`policy_snapshot.json`（开始快照存 baseline，收尾比对漂移明细） | `test_attribution.py` 6（S8-01 静态 DAG / 知识与冲突 / 改图探索 / 动作与人 / 失败 Mission，S8-05 断点）、`test_policy_snapshot.py` 6（S8-07：全字段归类、同配置同哈希、差异带来源、版本常量改变、未归类字段拒绝、演示证据无漂移） |
| C | `2df01fd` | 消融：`OrchestratorConfig.ablations` 封闭词表 `critic` / `blackboard` / `graph_changes`（去重排序；`blackboard` → `knowledge_sharing=False`，`graph_changes` → `dynamic_graph=False`）；安全边界（权限、网关、幂等、审批、部署政策、密钥检查、format / rule / code_test、human_review、预算）与词表外名字一律拒绝；Router 入口把消融的层从有效必需集合去掉，层记 `NOT_REQUIRED` 且 `detail.ablated=true`、`required_by_policy=true`；Critic 消融时 Mission judge 不运行，自由文本准则判定 `source=ablated`、理由"judge ablated in this run"（不会被当作 Verifier 冲突）；快照登记 `ablations` | `test_ablation.py` 17（安全边界与词表外拒绝 ×14、映射与快照差异、Critic 层显式移除且 Critic 调用 0、judge ablated、Blackboard 消融后检索 disabled 且 KnowledgeUsed=0） |
| D | `408fb38` | Evaluation `observability/evaluation.py`：`EvaluationCase`（每次试验新 provider、可带隐藏 oracle 与测试连接器）、`Strategy`（覆盖只许白名单）、`EvaluationPlan`（计划级配置白名单、Critic 消融 × 自由文本准则拒绝、带动作的 case 只许测试服务）；每次运行幂等键 `eval:<plan>:<strategy>:<case>:<trial>`、独立新目录与库（目录非空拒绝）、墙钟超时与异常 → `harness_error`（不计入分母）、等待人工单列；逐次记录（耗时、tokens / 金额或未定价、验证通过率、知识复用、重复率、剪枝率、污染率、恢复、失败原因与失败层、消融政策下的 PASS、oracle）；按策略汇总（Wilson 区间）与比较（Fisher 精确检验、区间不重叠、fixture 写"不适用"）及快照差异；`evaluation.json` + 中文 `evaluation.md`；`case_from_evidence`（spec 哈希须与旧库 `MissionCreated.spec_hash` 一致，改写幂等键，记录 derived_from 与旧库摘要） | `test_evaluation.py` 11（S8-03 双策略 × 2 次、oracle 误判、failure 与 harness_error 分开、策略覆盖拒绝 ×5、自由文本 / 真实连接器 / 计划配置拒绝、统计口径、S8-06 派生重跑与篡改拒绝） |
| E | `49106bf` | CLI `replay --evidence-dir DIR MISSION_ID [--events] [--failures] [--attribution] [--out]`（只读，与库不一致退出 1）、`evaluate --plan plan.json --evidence-dir NEW_DIR [--provider fixtures|env]`（内置 case 目录：parse-kv（带 oracle）、parse-kv-strict（严格 oracle）、parse-kv-bad、textkit；env 只有 parse-kv，统一注入 flash 模型名与真实运行参数）；`demo --scenario evaluate-policies`（完整政策 vs 去掉 Critic × 2 次试验，另写 `samples.json`：一次成功运行的归因、一次失败运行的回放）；计划级配置允许 `model`；step02 未实现检查改用 `policy-promotion`；真实 flash 评测 opt-in | `test_evaluate_policies_closure.py` 2（演示 16 次运行、oracle 误判 2/4、失败原因、归因与回放样例；CLI evaluate / replay / 拒绝坏计划）、`test_real_provider_evaluation.py`（opt-in） |
| F | `23fcde5` | 代码评审第 1 轮修复（处置表见 §1）：评测每个 case、每次运行都只许测试服务（`EvaluationRefused`）；消融使有效政策为空 → ERROR；回放结构性不变量补齐、违反即字段未决定；归因 `claim_refuted` / `refuted_on_path`（plan D8-4''）与预算账本对账；逐 case 成对比较与脚手架错误不对称降级；计划预构造配置；演示开始快照在运行前；CLI 错误退出码；harness_error 带 Mission id；消融连带影响写进报告；派生 case 校验证据版本；版本号 0.9.6 / 0.8.0 | step08 共 68 passed / 1 skipped（新增 `test_review_*` 11 条与 `test_replay_evaluate_cli_errors.py`） |
| G | `a094f46` | 代码复核第 2 轮修复（处置表见 §1 第 2 轮）：`ResultRejected` 投影、`AttemptCreated` 使 READY 的 Task ACTIVE、测试服务必须是本类且状态文件在本次运行目录下、被仲裁取代知识的来源 Attempt 计入被驳倒、终态 Mission 有未结算用量不算对账、消融连带影响补"自由文本准则无人判定"、`evaluate` 退出码写明；真实评测运行 2 记录 | step08 共 73 passed / 1 skipped（新增 `test_re_review_*` 5 条） |

## 3. 真实模型

- 评测运行 1（`49106bf`，deepseek-flash，`reports/real-evaluation-run1.md`）：`parse-kv` × 完整政策 / 去掉 Critic × 2 次真实试验，4 次运行全部 success，隐藏 oracle 全部通过（误判 0/4）；完整政策约 51 s / 5.1 万 tokens，去掉 Critic 约 25 s / 3.8 万 tokens；成功率比较 Fisher p = 1.0，结论"证据不足：样本量 2 / 2 低于 3"；证据 135 个文件，真实密钥逐字节命中 0、`sk-` 模式命中 0。
- 评测运行 2（`23fcde5`，评审修复之后，`reports/real-evaluation-run2.md`）：4 次运行全部 success，oracle 误判 0/4，脚手架错误 0；完整政策 16–28 s / 2.4–2.6 万 tokens，去掉 Critic 23–29 s / 2.3–3.2 万 tokens；逐 case 成对 2/2 vs 2/2（p = 1.0），成功率、耗时、tokens 三项都是"证据不足"；同一策略两次评测间差一倍，印证小样本不能下结论。证据 126 个文件，真实密钥命中 0，`\bsk-` 命中 0（无边界模式在库内的 453 处匹配全是 `task-` 标识符）。
- 自查发现：耗时 / tokens 的比较在样本不足时仍写"有差异"。已改为与成功率同一口径（fixture → 不适用；样本不足 → 证据不足；够样本且区间不重叠才写有差异），配测试 `test_time_and_token_differences_need_enough_samples_too`。

## 4. 回归与 wheel

- SDK 全量回归（`49106bf`，`regress-s8/run.sh`）：58 failed / 2343 passed / 12 skipped / 15 errors，红集 73 条 = 基线，**0 新红**。
- SDK 全量回归（修复切片 F `23fcde5`）：58 failed / 2356 passed / 12 skipped / 15 errors，红集 73 条 = 基线，**0 新红**。
- SDK 全量回归（复核修复切片 G `a094f46`，wheel 源）：58 failed / 2361 passed / 12 skipped / 15 errors，红集 73 条 = 基线，**0 新红**。
- wheel：自 `a094f46`（`SOURCE_DATE_EPOCH=1789126927`）可复现构建 `simple_harness_sdk-0.9.6-py3-none-any.whl`，sha256 `a1bc14331fed3bbbdf2201f2a31b1a3ab547c3d55725e3a81596c0feef2e2e6f`；干净 venv（Python 3.12）安装后在解出的源码树跑 `tests/orchestrator tests/agents tests/unit/contracts` 与迁移测试：584 passed / 1 failed / 10 skipped，唯一失败是基线已知的 `test_execution_v3_to_v4_migration::test_completed_null_continuation_resolves_unique_pair_and_preserves_facts`；`demo --scenario multi-mission` / `approval-action` / `evaluate-policies`（fixtures）与一次 `replay --attribution` 退出码都是 0；版本 0.9.6 / 0.8.0（脚本 scratchpad `wheel-0.9.6/build-and-verify.sh`）。
- 版本：simple_harness 0.9.6 / agent_orchestrator 0.8.0（`tests/unit/contracts/public-api.json` 同步）。

## 5. 遗留

- 第 6 步移交：DeepSeek 价目注入（L2-6、L6-2）；合并重复候选、角色配比调度、多样性配额（L6-3）；新思路数、剪枝率、重复率、误报率、污染率等指标（L6-8）——本步在评测指标里实现可由记录导出的部分，其余给 null 与原因。
- 运行时切换 Prompt / Allocator / Retrieval 版本（P1-9）、Allocator 消融（P1-11）、新 Verifier 重判旧产物（P2-1）：第 9 步。
- 代码评审登记：provider 身份补 `OpenAICompatibleProvider` 的端点主机与目标模型、fixtures 脚本摘要（CR P2-3）；`snapshot_diff` 对 profiles / routing / connectors / provider 给到字段级来源（CR P2-4）；回放覆盖场景扩到候选被取代、`KnowledgeSuperseded`、冲突 DEFERRED / UNRESOLVED、仲裁 / 接管、审批撤销 / 过期、动作 FAILED / UNKNOWN / Reconciled（CR P2-7）；派生 case 按场景名指定 provider 工厂（CR P2-10）。第 9 步"候选版本注册"时一并处理。

## 6. 结论

**SHIPPED**：simple_harness 0.9.6 / agent_orchestrator 0.8.0，wheel 源提交 `a094f46`（sha256 `a1bc14331fed3bbbdf2201f2a31b1a3ab547c3d55725e3a81596c0feef2e2e6f`）。

| 验收 | 结果 | 证据 |
|---|---|---|
| S8-01 最终产物依赖链与费用归属 | PASS | `test_attribution.py`（静态 DAG、知识与冲突、改图探索、动作与人、失败 Mission；`claim_refuted`、账本对账、未结算用量） |
| S8-02 回放得到相同正式状态 | PASS | `test_replay.py`（四个演示 + 失败 + 审批被拒 + 取消开放动作 + 人工审核两个时刻 + 被拒后重试，覆盖率 100%、0 不一致；重复投递；崩溃前缀；只读、不写、不导入 runtime） |
| S8-03 两策略同 case 同预算比较，诚实统计 | PASS | `test_evaluation.py`、`test_evaluate_policies_closure.py`；真实 flash 运行 1、2（小样本写"证据不足"） |
| S8-04 消融 Critic / Blackboard，安全边界不可关 | PASS | `test_ablation.py`（含零层验证判 ERROR）、`test_evaluation.py`（策略越权与非测试服务被拒） |
| S8-05 Trace 不完整：覆盖率 < 100%、缺口逐项列出、不回填 | PASS | `test_replay.py::test_s8_05_*`、`test_review_p1_2_*`；`test_attribution.py::test_s8_05_*` |
| S8-06 新模型重跑旧任务是新 Evaluation，旧库不变 | PASS | `test_evaluation.py::test_s8_06_*`（含篡改 charter 与旧版本证据被拒） |
| S8-07 版本差异与配置来源 | PASS | `test_policy_snapshot.py` |

门：step08 73 passed / 1 skipped；SDK 全量回归红集 73 = 基线、0 新红（`a094f46`）；wheel 干净 venv 584 passed / 1 failed（基线已知）/ 10 skipped，三个演示与 `replay --attribution` 退出码 0；代码评审两轮全部处置（§1）；真实 deepseek-flash 评测两次（§3）。
