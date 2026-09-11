# 第 8 步 plan 独立评审 · 第 1 轮（原文要点归档）

- 评审者：独立子代理（claude-opus-5，只读），2026-09-11；基线 main `5c15f2b`
- 依据：纲要 §10、§12；原文 §23、§28 第四阶段；理论 12、13 §17；并实查 `_emit` payload、Store、ids、预算账本、Verifier Router、judge、fixtures；在 scratchpad 做了 SQLite WAL 只读实验
- 结论：**计划不能直接进入实现**；4 P0 / 13 P1 / 8 P2，集中在 D8-2、D8-6、D8-7、D8-8 与 §6.1，改文字即可
- 处置：plan §6（D8-x'）与 `journal.md` §1

## P0

- **P0-1** S8-02 以"已覆盖字段一致"判定却无覆盖率下限，覆盖越少越易"一致"，不可判定；实查：`_cancel_open_actions` 置 CANCELLED 不发事件，动作的 APPROVED / REJECTED / REVOKED / EXPIRED 只能从审批事件反推。建议：本版本库覆盖率 = 100%，字段集逐项列出或明确排除，补 `ActionCancelled`，反推规则写进投影表，Result 按 `verification_state` + `verdict` 两列定义。
- **P0-2** `mission_id = hash(tenant_id, idempotency_key)`，同 key 同 spec 幂等返回旧 Mission；D8-8 从 `baseline.json` 原样读 spec（含 tenant 与 key）重跑得到同一 id；同 case 跨策略 / 试验不改 key 也同 id；目录已存在会静默复用旧事实。建议：`eval:<plan>:<strategy>:<case>:<trial>`；目录必须新建；派生改写 key；测试断言 id 两两不同、每库一个 Mission。
- **P0-3** 消融后把在政策里的 critic_review 记 NOT_REQUIRED 等于必需层未运行判 PASS（纲要 §12.4），也混淆现有状态语义；`trace.py:63` 按状态字符串过滤。建议：消融 = 有效政策显式变更，状态仍 NOT_REQUIRED、`detail.ablated=true`，PASS 标"消融政策下的 PASS"，快照列差异。
- **P0-4** Strategy 配置覆盖无白名单，可改部署政策、预算、`hard_cap_micros`、背压、`l3_distinct_principals`；从 approval-action 派生的 case 会用新幂等键再执行一次真实动作。建议：白名单；评测强制 `enabled_connectors=()` 或只允许测试连接器；带动作的派生 case 只在测试连接器下运行；三条拒绝测试。

## P1

- **P1-1** `Store.open` 会建目录、设 WAL、迁移并写备份；实验：WAL 库 `mode=ro` 读主文件哈希不变但会新建 `-shm` / `-wal`，只读目录直接报错，未 checkpoint 时主文件哈希不反映最新状态；v5 前旧库 `snapshot()` 查不存在的表。建议 `Store.open_readonly`、复制到临时目录、证明口径含 wal 与执行库并测只读目录。
- **P1-2** seq 是全库 AUTOINCREMENT、多 Mission 交错，单 Mission 本就不连续；删事件后折叠得到错误状态而非 not_covered；`list_events` 默认 `limit=10_000`，证据与指标同样截断。建议结构性不变量检测缺口、与库事件集比对、分页读完。
- **P1-3** 外部调用计数应含 pytest / 子进程、Critic 与执行库哈希；加导入图测试；更强的崩溃用例：崩溃时刻复制库，前缀投影 = 副本快照。
- **P1-4** 实际主体：Task Critic `{attempt}:critic:{n}`（最多 2 次）、`{mission}:judge:{n}`、`:planner:`、`:manager:`、`action:<key>` 预留（tokens 0、tool_calls 1、不进 imported_usage）、`unknown` 列；arbiter / synthesizer / conflict 是 Attempt。Task Critic 被漏且归属自相矛盾；各类是 imported_usage 的划分时"和 = 总量"是恒等式。建议逐行分类、未归类桶为空、unknown 单列、工具调用取 `settled_tool_calls`、动作预留单列、复用 `metrics._service_role`。
- **P1-5** 成功路径需逐条写死：多 Task 以 `merge_accepted` 输出为最终产物、被覆盖的上游产物、冲突落败方 Claim 的 Attempt、被取代候选、改图中被取代 / 取消的 Task、人工 override、失败 Mission；`lineage.py:78-90` 会把落败方 Attempt 纳入 attempts。
- **P1-6** 脚本耗尽即 AssertionError → UNKNOWN → 阻塞到 stall（默认 180 s）；纲要 §14.2 / §11.3 fixture 须标注。建议每次运行墙钟超时、`harness_error` 类别、每次试验新 provider、fixture 结果标"机制验证"。
- **P1-7** "样本 ≥3 且成功数差 >1"下 3/3 对 1/3 Fisher 双侧 p≈0.4，与"差异在波动内写证据不足"矛盾。建议 Fisher 精确检验或 Wilson 区间、按 case 成对、耗时 / 成本区间不重叠才写差异。
- **P1-8** 快照漏 explorer / exploiter / simplifier / connector / failure_analyst 模板版本、`SUMMARY_VERSION`、schema 与包版本、源提交、routing / profiles / connectors、多个配置字段与 `extra`、provider 身份；`evidence_root` 与带 pid 的 `owner_id` 会让哈希漂移。建议全字段枚举 + 排除清单 + 归类测试，开始与收尾各算一次。
- **P1-9** 版本号是模块常量，配置里没有选择项，比较 Prompt / Allocator 只能靠 monkeypatch。建议加变体注册表，或登记本步只比较模型 / profile、消融与配置项。
- **P1-10** 自由文本准则只由 Critic 判，Critic 消融下必然失败（与"不预设"冲突）；needs_human 与第 ② 类仲裁随之消失；human_review 的 case 会挂起；`knowledge_sharing=False` 同时关掉冲突任务与综合知识。建议消融 case 只用 `pytest:` / `file:`、等待人工作为终态类别、blackboard 断言写成可数。
- **P1-11** `dynamic_graph=False` 关的是 Manager 改图，不是原文"动态调度"或理论"动态 Allocator"。
- **P1-12** 纲要评测报告列有"验证误判"；fixtures 已有"错但能过"的脚本。建议隐藏 oracle。
- **P1-13** `baseline.json` 经脱敏；MissionCreated 带 `spec_hash`。建议重算比对，只允许本版本演示证据并指定 provider 工厂。

## P2

1. 新 Verifier 重判旧产物的变体未提供，建议登记。
2. 污染率（只有 Claim 会 DISPUTED；SUPERSEDED 可能是正常修订）、剪枝率（排除级联停止与 not_needed_paused，计入被取代候选）、故障恢复、新思路数的定义需修正。
3. 人工时间（`human_wait_seconds`）建议在归因单列。
4. 从 `events.jsonl` 回放时 payload 可能被脱敏，应标注来源、优先读库。
5. step02 `test_cli_demo.py:81` 与 `__main__` 文档串、集合需改为 policy-promotion。
6. 价目注入（L2-6、L6-2）未处置；L6-1 需登记。
7. 切片顺序：Replay 先行；D 依赖 A、C 并先解决运行身份与超时。
8. 可砍：派生只支持本版本演示证据；`attribute` 并入 `replay --attribution`；库内去重由 UNIQUE 保证只测文件来源。
