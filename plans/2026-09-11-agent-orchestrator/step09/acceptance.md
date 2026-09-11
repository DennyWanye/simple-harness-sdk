# 第 9 步验收标准（MUST 8 条 = ORCH-BUILD §11.4 S9-01～08）

> 草稿（plan review 前）。实现名词（表名、命令参数）以 plan §3 / §6 为准；判定口径在评审后只许收紧、不许放宽。

| ID | 场景 | 必须观察到的结果（纲要原句 → 本步可判定形式） | 判定 |
|---|---|---|---|
| S9-01 | 生成一个新策略候选 | 可追溯到训练数据、代码、参数和评测版本 → `policy propose` 从一组历史证据（只读）生成候选（plan D9-2' 两层身份）：参数版本 id 由展开后的完整参数哈希决定；提议记录含训练集清单（每个 Mission 的 id、任务身份哈希、所在库的 sha256、创建时间、终态、`provider_kind`）、代码版本（两包版本 + 基线快照哈希 + 解释器版本）、参数相对基线的逐项差异、学习器 / 规则改进器名字与版本、逐规则统计；提议 id 由这些内容的规范哈希决定，同一输入重复提出得到同一提议 id，不同历史提出同一参数得到同一参数版本、不同提议；评测结果挂在提议上并记评测版本与报告哈希；旧库在提出前后 sha256 不变 | `test_policy_registry.py::test_s9_01_*` |
| S9-02 | 候选未通过质量 / 成本门槛 | 不上线，保留失败理由；旧策略继续运行 → 候选与当前生效版本在同一组留出 case、同一预算边界下评测；任一门槛（质量：成功数不低于基线且 oracle 误判不增；成本：tokens 不超过基线的允许倍数；风险：脚手架错误为 0、安全边界字段未变）不满足 → 候选记为未通过并逐条写明理由；`policy promote` 对它被拒；生效版本不变，之后提交的 Mission 仍绑定旧版本 | `test_policy_gates.py::test_s9_02_*`、`test_policy_registry.py::test_s9_02_*` |
| S9-03 | 合格候选但未批准 | 仅 shadow / 测试，不改变正式 Mission → 通过门槛但未批准的候选只在评测目录（新目录、新库）里运行；正式库中之后提交的 Mission 绑定的仍是生效版本；未批准就 `promote` 被拒；正式库里没有任何以该候选版本运行的 Mission | `test_policy_gates.py::test_s9_03_*` |
| S9-04 | 审批后上线 | 新 Mission 绑定新版本；在途 Mission 不被静默改策略 → 审批人（`Principal`，带 nonce 与决定回执）批准后 `promote`：生效版本切换并留事件；此后创建的 Mission 记录新版本 id，其调度 / 路由实际使用新参数（intent / 分配记录可见）；切换前已创建、仍在运行的 Mission 保持原版本——包括换一个新的 Orchestrator 实例（带新配置）恢复它之后 | `test_policy_binding.py::test_s9_04_*` |
| S9-05 | 新策略出现异常 | 快速回滚至已批准版本，既有事件和费用仍保留 → `policy rollback` 只能回到曾经批准并上线过的版本，一条命令完成、留事件与回执；回滚后新 Mission 绑定回滚目标版本；以被回滚版本运行过的 Mission 的事件、用量、归因总额在回滚前后逐字节 / 逐项相同（回放覆盖率 100%、0 不一致）；被回滚版本的记录保留、状态标为已回滚 | `test_policy_registry.py::test_s9_05_*`、`test_policy_binding.py::test_s9_05_*` |
| S9-06 | 在线 Agent 提出改安全阈值 | 不能直接写核心规则；按权限拒绝或进入人工审查 → 运行中的 Agent（Worker / Manager）经其输出夹带修改安全边界字段、预算上限、部署政策或核心调度规则（Worker 已接受产物落在 `policy/` 或部署配置文件名、Manager 改图提议里的配置类操作）：任何生效规则与版本库不变；记 `PolicySuggestionRefused`（列出被拒的键）；人工审查的出路是由人用 `policy propose --params FILE --as P` 提出（来源 `human:<P>`，仍须评测与批准）；Agent 身份不能构造审批 `Principal`，不能批准或晋级（plan D9-10'；残余风险登记） | `test_policy_guard.py::test_s9_06_*` |
| S9-07 | 训练数据不足 / 有明显污染 | 明确拒绝晋级，不输出虚假的有效提升结论 → 样本数低于下限时 `propose` 给出"缺少足够样本"且不产生可晋级候选；训练集与评测集泄漏（同一 Mission / 同一任务的 Attempt 同时出现在两边）、记录不可信（回放不一致或覆盖率 < 100%、归因对账失败、fixture 数据被当作真实数据）时拒绝并列出原因；报告里没有"提升"字样的结论 | `test_policy_learning.py::test_s9_07_*` |
| S9-08 | 高负载下策略频繁调整 | 并发和角色调整有边界，无无限扩张；安全 / 预算上限始终有效 → 候选只能改白名单参数且每项在允许范围内、每次晋级的变化幅度有上限、两次晋级之间有冷却；背压 RAISED 时不接受扩张并发的晋级；在高负载演练（多 Mission + 连续提出 / 晋级 / 回滚）中，运行中并发从不超过配置上限，Mission 与 Global 预算从不超支，安全边界字段始终不变 | `test_policy_guard.py::test_s9_08_*` |

附加门槛：`tests/orchestrator`（step02–09）全绿；SDK 全量红集 ⊆ 基线；安装 wheel 后跑 `tests/orchestrator` 与 `python -m agent_orchestrator demo --scenario policy-promotion --provider fixtures --evidence-dir evidence/s9`；CLI `policy propose / evaluate / approve / promote / rollback`；fixture 结果标注"机制验证"，不宣称真实质量提升；真实模型（deepseek-flash）小规模候选评测记录（标注为真实试验、带样本量，结论如实）；独立 review 处置；推送 origin main 且本地干净。
