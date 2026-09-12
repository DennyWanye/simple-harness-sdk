# P3.5 原八项 AC：证据与最小剩余工作

**07:40更新：下表为早期缺口审查历史，不是当前未实现清单。** A01多profile6PASS、A02Mission期限1PASS、A03容量1PASS、A05实际OSkill2PASS、A07Context冷恢复与新输入2PASS、A08正式offline helper20PASS及实际外部效果cold/backup1PASS已新增；对应精确时长和索引见当前ARCHITECTURE/ORCHESTRATOR.md。A03复合优先级、A06原生控制面及最终全量仍OPEN。

2026-09-13；只读代码/证据复核，未运行测试、进程、模型或 UI。仅新增本文，不改 core/tests。原 AC 来自 Host `plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md` §7、§11；不缩减原要求，不涉及 P3.6。

## 1. 当前判定

**P3.5 未整体完成。** `readiness-draft.md` 早期基线及旧增量中关于 priced/tail 尚未实现、SUCCEEDED 永久无 accounting 通路的描述，不能当作当前事实。现已有 token/priced guard、FIRST quota、Mission system hold/transfer、权威 accounting receipt/effective-fact 及局部验收。FIRST 首物理请求的完整输入+输出保证仍冻结；终态 Mission/service 的迟到账务自动导入接缝由 Kepler 修复中。

本次读取原始日志末尾核实：multiMission-v1 为 **1 PASS / 0.57s**；priced-v3 为 **1 PASS / 0.94s**。priced-v1/v2 的失败没有被删改；v3 通过不意味着 FIRST 首请求保障或全部 P35 通过。N1v7 使用 immutable SDK6866，不因当前工作树变化自动获得后续功能。

## 2. 原 AC 与剩余门槛

| 原 AC | 当前可引用的实现/证据 | 最小剩余实现或验证 |
|---|---|---|
| A01 物理槽位不抬高 | `test_multi_mission_load.py` 实际同 Orchestrator、3 Mission、3 logical Worker、2 model slot、2 Verifier slot，provider 边界 peak **恰为2**；单 profile。guard 在共享编排 Store 中计持久 held grants，assembly 不再隐式扩容。E1 | 补同部署 **两 profile** 共用总上限的实际 invoke 控制，排除每 pool 各拿2槽；核 profile 上限与全局上限同时生效。当前 profile 未提供独立更小 cap 配置，不把相同全局 cap 宣称为任意 profile 配额功能。部署范围限定本机同编排库，不能推广任意多进程/Host全部调用。 |
| A02 排队不算失联 | E1 的第三个真实 SDK 请求具有 slot-wait/nonbillable liveness、handoff0、usage空、仅一个 Attempt；`test_provider_budget_recovery.py::test_slot_queue_has_explicit_nonbillable_liveness_and_original_deadline` 已有 SDK turn deadline 控制 | 现有 deadline 测试推进的是 guard 的 SDK turn deadline，不能替代 **Mission 总期限**。补等槽跨越原 stall/续租观察窗口、随后真实 Mission deadline 到期的 Orchestrator 控制；同 agent/turn/attempt，无重复执行。 |
| A03 验证背压闭环 | `step06/test_backpressure.py` 已有真实 Router/调度的 RAISED→CLEARED、重试延迟；`test_backpressure_state.py` 有六维/滞回。E1 真正局部 Verifier 等待与 human wait 同时存在时 active model calls=0；slow Critic 在人审仍待定时推进。FIRST/system hold 与 priced synthesis oracle 已有 E2/E3/E5 | E1 **不是**队列洪峰/冲突优先级 oracle。当前 `max_pending_verifications` 用作观察高水位，Router活跃数由 verifier_workers 限制；仍需对 pending+在途潜在完成数确定界限并测试。复合验证：高水位→Worker减速→冲突/最终验证实际有额度并推进→低水位→排空。FIRST quota不保证首次完整请求，待冻结接口后补必要保证。 |
| A04 预算不能双花 | E3 真实并发末余额、priced retry/UNKNOWN/overrun/原价；E4 两 SQLite close/reopen、原价结算；E6 的50个通过控制含 accounting/legacy/cold（联合批另有2 fragment失败）；E1 等槽取消不计费、其余实际账务对齐 | Kepler 的终态/已收集 service late-accounting **自动**导入修复及实际 Orchestrator oracle，不能用测试手调 `import_original_once` 替代。原 authority/price、UNKNOWN held、原 subject 去重保持。第一请求保障另列，不扩大 Mission cap。 |
| A05 交叉故障恢复 | SDK `step02/test_recovery_matrix.py` 跨提交点 InjectedCrash；P33 critic lifecycle/recovery；E4 两库真实关闭重开；Host有真正 SIGKILL 测试，见§3 | 新 guard/原 reservation/receipt 需要真实冷进程杀死矩阵；已有 Host kill 测试没有启用本轮 fixture estimator，SDK close/reopen不是OS kill。至少实际 SDK完成未Orch收集、已handoff未获usage两切点，原 identity不重复、未知先核对。 |
| A06 控制面及时生效 | E1 public Commit cancel两次幂等、排队请求永不handoff、其余Mission继续完成；已有 actual SDK cancel/lease/collector 控制 | UI路径仍需主通过源码UI观察 cancel→确认、旧请求零新handoff、在途事实结算、unknown清晰显示。E1只有服务控制，不冒充UI，也不证明人为选择的时延 SLA。与 N1/N2/N6 原native门协调，不能直接拿它们名称替代P35压力控制。 |
| A07 长Context不丢关键要求 | E7 doc6/large pages/context组合97 PASS；`test_role_context_runtime.py` 原合同可见性；`tests/agents/test_context_journal.py` 原文回读、protocol group、当前输入、UNKNOWN frozen request不重新selection | 真实长回合超过窗口后仍满足最早合同/未决状态，读取实际后页材料产出完整 formal 内容，结合源码UI证据；Context字数测试或单次页面读取不替代该业务闭环。FIRST final-wire cap 暂停，不能标已实现。 |
| A08 多库备份恢复 | 有 SQLite 单库备份/校验原语及旧 action 库回滚去重控制；**未找到 orchestration 多库+artifact 协调 production helper**，见§4 | 需正式安全切点、全库/产物清单、隔离restore与否定控制；这是实现缺口，不能只新增 backup test 宣称完成。 |

E1 的每 Mission事实：slow 完成真实文件/验证/Critic；human 真实 policy review暂停，读取实际artifact后公开review完成；cancel真实SDK排队后取消，零physical call/零usage，无第二Attempt。三阶段快照由测试写入主runner日志，原始内容不复制进本文。

## 3. OS kill：已有与缺少的区别

Host已有：

- `backend/tests/orchestration/test_restart_recovery.py`：`_spawn` 真实子进程运行 `_child_service.py`，`_kill9` 使用 SIGKILL并断言负signal退出码。等待人审时杀死后同目录新owner恢复、已提交Worker不重跑；模型调用中杀死后显示UNKNOWN，合法takeover可继续。
- `backend/tests/orchestration/test_instance_lock.py`：实际子进程持有生产 `InstanceLock`，SIGKILL后内核释放锁；第二服务不能同目录写入。
- Child使用 fixture provider。`backend/deskpet/orchestration/runtime_profile.py::source_runtime_options` 仅 official DeepSeek snapshot生成counter，fixture snapshot=None不会注入provider_token_estimator。因此这些原控制不能证明P35 grant恢复。
- Host HA-8/15/16 原验收定义在 `plans/2026-09-11-orchestrator-host-integration/acceptance.md`。本次只确认代码及旧范围，没有新跑结果；Host整目录聚合统计不拆成未经记录的最新单case PASS。

最小新增验证可独立 test-only：复用真实 Orchestrator fault边界及child marker，在child抵达确定提交点后父进程SIGKILL，不调用close；记录SDK/Orch原agent/turn/invocation/receipt。另启动冷进程原root/newowner恢复。先做两个决定性切点：(1) SDK终态已落库、Orch尚未收集；(2) HANDED_OFF且回复/usage未知。第二点只有权威lookup返回后才能settle，不能把kill视为0费用。真实外部效果已发生但丢receipt的控制可复用原action connector业务键，另保留应用次数断言。精确进程及child全部清理，证据进ignored目录，不触N1进程。

SDK `step02/test_recovery_matrix.py` 使用 `InjectedCrash` 并退出 `async with`；它验证durable重入语义但执行了正常teardown。E4同理是两库close/reopen，不替代上述kill切点。`p33/test_g_replay_audit_plugin.py` 的child kill仅timeout清理，也不是恢复oracle。

## 4. A08：最小生产缺口与验证路线

已存在的可复用原语：SDK `storage/store.py::_migrate` 的单编排库SQLite backup；execution migration单库备份；Host `product_state/backup.py` 的单库backup、quick_check、hash sidecar、原子发布及offline restore。这些均未枚举整个orchestration部署，也没有关联各profile execution DB、CAS及未知动作。

最小正式方案（待主分工授权，不在本次实施）：

1. 提供**停写安全切点的本地维护 helper**。先停新准入并停止/收尾当前本地写者；活跃业务未到允许切点则拒绝备份。使用生产InstanceLock/实际进程状态保证源root不会继续写；UNKNOWN事实允许保留，但不把它结算或改为未执行。单纯依次对多个正在变化的DB调用backup并不构成一致切点。
2. 从真实部署profile/库身份枚举 `orchestrator.db`、全部 `execution*.db`、context identity sidecars及所有引用artifact/CAS；SQLite backup包含已提交WAL，不能直接copy主db冒充。清单记录角色/profile/schema/config/policy、源身份、hash、dispatch/receipt关联与UNKNOWN。禁止把凭据或运行环境secret一起递归打包。
3. 隔离target先完整验证，再原子发布restore；源root不变。拒绝缺库、错profile/schema、错context identity、错artifact hash、重复库身份及不完整manifest。artifact `storage_uri`含绝对路径，必须通过既有存储位置更新/验证接口定位到target并保持content hash，不能恢复后偷偷读取原root来假成功。
4. 恢复副本走生产Orchestrator recover；已完成任务不重派，未决审批保持，UNKNOWN先权威lookup。至少2profiles+编排库+真实artifact的正控，缺库/hash损坏/错profile反控；原库不可见时恢复仍能读自己的产物。外部副作用不会随库回滚撤销，必须沿原业务键核对且不得自动重发。

这不要求在线跨库原子事务或P3.6发布；不应为完成A08临时增加广泛备份架构。需要的只是原计划范围内可复用的正式维护入口、清单和隔离恢复闭环。

## 5. 已核对证据索引

以下均位于 SDK `.local-test-evidence/2026-09-12/p33-g/`；本次只读日志摘要及计算SHA-256，没有重跑。相邻同名JSON是主runner receipt。

| 索引 | 日志 | 实际批次结论 | SHA-256 |
|---|---|---|---|
| E1 | `p35-multi-mission-v1.log` | 1 PASS / 0.57s | `6cd27a0a0fdce6542f31346a9c34001be469e2e0dd006416efbf002e92a75aeb` |
| E2 | `p35-system-priced-v3.log` | 1 PASS / 0.94s | `f9c684a79977ff1a5e73ad07b06ebe5942fb5893e2e738d093a0772e789a3e91` |
| E3 | `p35-tail-price-v2.log` | 19 PASS / 1.00s | `ac75a7f88d4d202f0e7e1a5e53c50a93aa0e86b94c201e623314d35a82be2155` |
| E4 | `p35-priced-cold-v1.log` | 1 PASS / 0.40s | `59b9466559aeabf218a54ec041fd83cd357cf9bf1d80a17c47aab62ed0c205bf` |
| E5 | `p35-first-default-v1.log` | 6 PASS / 0.58s | `83ef11ace12d1ef8964708e04e58196db7ea23f86721f1686aa707958b594b11` |
| E6 | `p35-system-accounting-v1.log` | 50 PASS / **2 FAIL** / 33.44s；非全绿 | `713c73775d803947de7ea9917068db8b41f9028f292c5399f6bc4dfa60eb6e59` |
| E7 | `g-doc6-large-pages-v1.log` | 97 PASS / 5.88s | `9852554a667c2b063fdee167c6167386ad8d2a58b297cbd4152889614250ae3c` |
| E8 | `p35-cold-owner-v4.log` | 3 PASS / 0.44s | `3a75cdc9908b3a2bc2fc2c788afa5fe7624c777b6d68d869d817d96e1a51725e` |

建议顺序：主继续N1native及N2/N6原门；Kepler先收口迟到账务P1；随后恢复FIRST有限保证。独立补两profile/长排队/背压及真正kill控制；A08需先完成上述生产helper再测。最终按原8AC整合回归和源码UI收尾，保持失败原证据；任何一批小控制通过都不代替整阶段退出门槛。
