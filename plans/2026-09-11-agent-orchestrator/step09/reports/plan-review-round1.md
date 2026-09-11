# 第 9 步 plan 独立评审 · 第 1 轮（原文要点归档）

- 评审者：独立子代理（claude-opus-5，只读），2026-09-11；对象 `step09/plan.md`、`acceptance.md`；对照纲要 §11 / §12、原文 §28 第四阶段 / §29.2 / §29.3 / §23.4、理论 05 / 12 / 13、源码
- 结论：**不需推倒重来**；按 3 条 P0 与 P1 修订 D9-1 / 3 / 4 / 6 / 7 / 8 / 10、补 plan §6 与 journal §1 后，可按测试先行进入切片 A。问题集中在版本身份、在途锁定、防泄漏判定三处——恰好让 S9-01 / S9-04 / S9-07 出现"测试能过、保证不成立"。处置见 `journal.md` §1、plan §6

## P0

- **P0-1** 内置策略"空参数 = 代码常量 + 部署配置"是浮动的：版本内容不变而实际取值随配置 / 常量变化；未晋级前所有 Mission 都绑定它，换 `candidates_per_task=2` 的配置恢复即静默换策略而 `policy_version_id` 不变；门槛评测的 active 一方也在评测自己的配置下展开。建议版本行一律存展开后的完整参数；内置策略在正式库首次打开时物化为 `seed`（写明来源常量与配置哈希）；配置里白名单项与 ACTIVE 不一致时记 `PolicyConfigDrift`。
- **P0-2** plan（版本 id = 参数哈希）与 acceptance（id = 训练集 + 代码 + 参数 + 学习器 + 评测计划的哈希）矛盾；同参数不同来源、撞 ACTIVE / seed、REJECTED 等状态后再提、重评等没有状态边。建议拆两层：`policy_versions` 以参数哈希为身份，另设提议记录（多条提议可指向同一参数版本），评测、批准挂在提议上，批准绑定提议 id + 评测报告哈希 + 评测时基线版本；给出完整迁移表。
- **P0-3** 泄漏按 spec 哈希判定永不触发：`spec_hash` 含 `tenant_id` / `idempotency_key`，评测与派生都改写它们。建议定义去掉这两项的"任务身份哈希"；派生 case 用 `derived_from.mission_id` 比对训练集；时间规则只对有来源时间的 case；测试构造"同任务不同幂等键"的真实泄漏场景。

## P1

1. 决策点没列全：候选 token 份额（`event_handler.py:2857`）、提交层守卫（`:2907-2909` → `commit_service.py:2155-2170`）、Manager 阈值多处（含恢复路径 `_collect_manager` `:2250`）、服务路由 `_route_service`（`:406-416`）、`aging_window_seconds`（权重输入尺度）、prompt / verifier / retrieval 版本常量。建议逐项列出白名单参数的全部读取点与"部署级、对在途立即生效"清单；版本行记解释器版本，恢复时不一致记事件；参数化测试"配置 A 创建、配置 B 恢复"与同进程两个不同版本的 Mission。
2. v5 旧库迁移：在途 Mission 无绑定行会按 D9-4 报错永远不能恢复；v5 历史因新字段覆盖率 < 1 被误判不可信。建议迁移时绑定到 `legacy`，回放对旧库按旧字段集算覆盖率；补测试。
3. fixture 评测 PASSED 可直接用于正式晋级，与纲要 §11.3 冲突。建议评测记 `evidence_kind`；正式库有过真实运行时拒绝仅凭 fixture 证据晋级，或要求显式 `--accept-fixture-evidence` 并写进事件。
4. 门槛口径与第 8 步不一致：tokens × 1.10 在真实波动（同策略可差一倍）下约一半概率误判；"样本 < 3"未说明按 case 还是合并；一边脚手架错误就判 FAILED。建议成本"明显更差才拒"、样本按每 case 每方、脚手架错误 → INSUFFICIENT、PASSED 含义写"非劣"。
5. fixture / 真实判定依据不存在：库里不记 provider 种类，单 provider 默认 profile 的 `provider_kind` 取默认 `"fixtures"`，真实运行会被误标。建议 v6 起把 `provider_kind` 写进绑定行；旧库标 `unknown`。
6. `policy_pin` 以"库里已有 active 来源的 Mission"识别正式库不可靠（首个 Mission 之前可混入）。建议库创建时写角色标记（evaluation / production），pin 只接受评测库，正常实例不以 active 身份打开评测库。
7. 部署级事件幂等键会吞掉重复发生的事件（回滚到 A 再回滚、重评）。建议活动日志单调序号入 key；评测 key = 提议 + 报告哈希；冷却从最近任意激活算；为版本库写事件折叠投影与表逐项比较。
8. 批准未与评测基线 / 代码版本绑定：C 对照 A 获批，期间 B 上线，C 随后晋级从未与 B 比较。建议晋级要求评测基线 = 当前 ACTIVE、评测时包版本 = 当前代码。
9. `policy/` 产物通道不必要且扩大攻击面（Mission 事务写部署级登记表、可刷候选、无训练来源）。建议不开通道：夹带改安全阈值 / 预算 / 部署政策的内容或改图配置操作 → 记 `PolicySuggestionRefused`（Mission 时间线、replay 已知事件）；人工出路 `policy propose --params FILE --as P`（来源 `human:<P>`）；登记残余风险（`run_tests` 无文件系统隔离、`--as` 自报）。
10. 快照纳入"绑定版本"会让第 8 步测试变红（开始快照在 seed 之前）。建议快照只记部署级 ACTIVE（seed 在 `__aenter__` 物化），Mission 绑定写进该 Mission 证据。
11. 纲要 §11.1 / §11.2 的"角色 / Prompt 候选版本注册"与第 8 步移交项未处置。建议白名单加 `prompt_versions`（只能选已登记模板版本，fixture 登记第二个版本证明机制）；全零权重即 Allocator 消融；其余逐条登记；"有足够数据时实现学习型候选"登记为偏离。

## P2（要点）

1. R1 因果基础弱：`AllocationDecided` 不记被推迟的 Task；`uncertainty` 与重试混淆；探索是 Attempt 级；`min_group` 应按 Mission 计。建议只统计有竞争的分配（v6 起记可分配数 / 空位），报告写明启发式。
2. R2：`by_role` 优先于 `by_task_kind`，部署配了 `by_role[worker]` 时 R2 无效应检查；区分"有样本无改进"与"样本不足"。
3. 可信度检查代价：同一历史库只复制一次、批量回放；只取终态 Mission。
4. fixture 门槛评测可行性：评测默认并发 1、脚本可能耗尽；演示配足脚本、显式并发。
5. 回滚语义：异常未定义 → 按版本健康报告 + 人工回滚；回执列出仍绑定坏版本的在途 Mission；"逐字节不变"限定到回滚时刻前的事件；目标规则写死（最近 RETIRED，跳过 ROLLED_BACK）。
6. 路由目标 profile 缺失时构造 `ModelRouter` 会抛错，应按 S6-08 记事件并回落，不拖垮实例。
7. S9-08 演练要可观测：gate 占用 Attempt、小 `max_running_attempts` 保持 RAISED、每周期读峰值与账本、晋级前后比对部署政策哈希、冷却用注入时钟、路由变更幅度限定。
8. 证据导出：哨兵时间线的策略事件不进按 Mission 的 `events.jsonl`，演示另写 `policy_events.jsonl` 并做密钥扫描。
9. 既有测试：step02"未实现"测试改写后保留语义；`TaskScore` 加字段查第 5 步精确断言；`public-api.json`；`DeploymentPolicy` 加字段改变快照哈希（同配置同哈希仍成立）。
10. 切片：C 只做训练侧检查，防泄漏移到 D；可砍 `policy/` 通道、路由只做 `by_task_kind` 与 `escalate_after_failures`、`policy list/show` 最简；沿用 pytest 插件扫描绑定一致性。

## 已核实没有问题（摘要）

`allocate` 按 Mission 调用、权重注入方向正确；冲突优先 / 饥饿 / 背压闸门在公式之外，学习器只改权重不越过保底；另建 `policy_decisions` 而不复用 `approvals` 正确，`Principal` 强制 human 可沿用；`events.mission_id` 无外键，哨兵时间线可行，`replay_mission` 对不存在的 Mission 报错；迁移增量加 DDL 并自动备份，既有迁移测试按 `SCHEMA_VERSION` 泛化；第 8 步评测平台可直接复用；intent 已冻结分配分项与 profile；术语处理妥当；不可晋级清单与背压联动符合纲要；§25 状态机未被改动。
