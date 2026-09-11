# 第 8 步验收标准（MUST 7 条 = ORCH-BUILD §10.3 S8-01～07）

| ID | 场景 | 必须观察到的结果（纲要原句 → 本步可判定形式） | 判定 |
|---|---|---|---|
| S8-01 | 指定一次已完成 Mission | 可查看最终产物依赖链及真实费用归属 → 归因报告列出最终产物（路径与内容哈希）→ 产出它的 Task / Attempt / Agent / 角色 / 模型 / prompt 版本 → 使它通过的验证层；多 Task 时包含合并进集成树的上游产物与知识路径；未进入成功路径的 Attempt 单列为探索消耗；路径内 + 路径外 + 服务调用的 tokens 之和等于该 Mission 已导入用量总和，金额在未定价部署下为 null 并注明 | `test_attribution.py::test_s8_01_*` |
| S8-02 | 回放重复事件 / 崩溃记录 | 得到相同正式状态；外部调用数不增加 → 对已完成 Mission 回放：投影出的正式状态在已覆盖字段上与库快照一致；把事件流重复投递一遍（同 event_id）结果不变；对一次崩溃后恢复完成的 Mission 回放同样一致；回放前后 provider 调用计数、连接器调用计数、库文件 sha256 都不变 | `test_replay.py::test_s8_02_*` |
| S8-03 | 比较两个策略 | 使用相同任务集 / 预算边界，输出两者指标和失败案例 → 评测计划里两个策略跑同一组 case、同一预算上限、各 N 次试验，每次运行 Mission id 不同且在独立库；报告对两个策略各给出成功率（带样本量）、耗时分布、tokens / 金额（或 unpriced）、验证通过率、知识复用、重复率、失败原因与失败样例；样本不足时比较结论写"证据不足" | `test_evaluation.py::test_s8_03_*` |
| S8-04 | 移除 Critic 或 Blackboard 做消融 | 报告关闭了什么、哪些变化可观察；不预设一定变差 → 消融 `critic`：Critic 层记为 `NOT_REQUIRED(ablated)`、没有 Critic 调用、依赖 judge 的准则注明 ablated；消融 `blackboard`：没有知识投影与检索；报告列出关闭项与观察到的指标差异（不论方向）；请求消融权限 / 幂等 / 审批 / 必需确定性验证层被拒绝 | `test_ablation.py::test_s8_04_*` |
| S8-05 | 历史 Trace 不完整 | 报告覆盖不足，不能伪造完整 Lineage → 删去部分事件或用缺字段的旧事件回放：覆盖率 < 100%，缺口逐项列出（缺哪个对象的哪个字段、哪些 seq 缺失），这些字段为 `not_covered` 而不是库里的现值；归因遇到缺失的记录时标注断点，不补边 | `test_replay.py::test_s8_05_*`、`test_attribution.py::test_s8_05_*` |
| S8-06 | 新模型重跑旧任务 | 标记为新 Evaluation 与新成本，不覆盖旧 Mission 事实 → 从旧证据目录派生 case、换 runtime profile（或模型名）重跑：新 Mission id、新目录与新库、新费用；报告写明 `derived_from`；旧库文件 sha256 与旧快照在运行前后相同 | `test_evaluation.py::test_s8_06_*` |
| S8-07 | 检索 / Prompt / 模型版本变化 | 报告明确列出版本差异，能够复现配置来源 → 两个策略快照（例如不同 prompt 版本、retrieval 版本、模型名、消融列表）之间的 `snapshot_diff` 逐项列出差异与来源；评测报告对每个策略给出快照哈希；同一配置两次快照哈希相同 | `test_policy_snapshot.py::test_s8_07_*` |

附加门槛：`tests/orchestrator`（step02–08）全绿；SDK 全量红集 ⊆ 基线；安装 wheel 后跑 `tests/orchestrator` 与 `python -m agent_orchestrator demo --scenario evaluate-policies --provider fixtures --evidence-dir evidence/s8`；CLI `replay` / `attribute` / `evaluate`；真实模型（deepseek-flash）小规模评测记录（标注为真实试验、带样本量）；独立 review 处置；推送 origin main 且本地干净。
