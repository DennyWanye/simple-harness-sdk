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

## 2. 执行记录

| 切片 | 提交 | 内容 | 测试 |
|---|---|---|---|
| A | `211500d` | Replay：`observability/replay.py` 纯折叠（按 seq 排序、按事件 id 去重）、投影表与推导规则（含非候选结果、审批→动作、审核→结果回到 RUNNING、冲突任务失败→UNRESOLVED）、结构性缺口、与只读副本快照逐字段比较、失败时间线；`Store.open_readonly` / `iter_events` / `has_table`（快照按表是否存在）；证据与指标改为分页读事件；补事件 `ActionSuperseded`、`ActionCancelled` | `test_replay.py` 12：四个演示 + 失败 Mission + 审批被拒 + 取消开放动作 + 人工审核挂起 / 通过，覆盖率 100% 且 0 不一致；重复投递；崩溃前缀；只读目录、不写、不外调、不导入 runtime；S8-05 删事件 / 缺字段 |
| B | （本次） | 贡献归因 `observability/traces.py`：最终产物 = 集成树（`merge_accepted`），产出者 Task / Attempt / Agent / 角色 / 模型 / profile / prompt 版本与通过的验证层；依赖闭包内的 Task 在路径内（被覆盖的上游产物标注）；知识路径来自 lineage，被驳倒的 Claim 所在 Attempt 列为探索；探索消耗逐条带原因；用量逐行归类（Attempt、其 Critic、planner / manager / judge、未归类桶必须为空、unknown 行单列），工具调用、动作预留、人工时间单列，未定价金额 null；记录缺失列为断点、不补边。策略快照 `governance/policies.py`：`SNAPSHOT_FIELDS` 逐字段归类（未归类即报错）、`VERSION_SOURCES` 版本常量带来源、全部角色模板版本、profiles / routing / 连接器 / provider 身份（不含密钥）、`snapshot_diff`；编排器 `policy_snapshot()`；证据 `attribution.json`、`policy_snapshot.json`（开始快照存 baseline，收尾比对漂移明细） | `test_attribution.py` 6（S8-01 静态 DAG / 知识与冲突 / 改图探索 / 动作与人 / 失败 Mission，S8-05 断点）、`test_policy_snapshot.py` 6（S8-07：全字段归类、同配置同哈希、差异带来源、版本常量改变、未归类字段拒绝、演示证据无漂移） |

## 3. 真实模型

## 4. 回归与 wheel

## 5. 遗留

- 第 6 步移交：DeepSeek 价目注入（L2-6、L6-2）；合并重复候选、角色配比调度、多样性配额（L6-3）；新思路数、剪枝率、重复率、误报率、污染率等指标（L6-8）——本步在评测指标里实现可由记录导出的部分，其余给 null 与原因。
- 运行时切换 Prompt / Allocator / Retrieval 版本（P1-9）、Allocator 消融（P1-11）、新 Verifier 重判旧产物（P2-1）：第 9 步。
