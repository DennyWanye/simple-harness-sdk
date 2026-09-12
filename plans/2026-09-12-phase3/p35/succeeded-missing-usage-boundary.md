# P35 open gate：SUCCEEDED 但 usage 缺失的迟到账务

状态：**OPEN / 已只读定位，2 个新边界控制待主 runner；未改生产 API**。2026-09-13。

本文件只界定当前缺口与最小后续补充。FIRST、Mission pool core 保持 freeze；schema 15 由 Kepler 聚合。N1 v6 可按主线程既定范围继续，不把此处调查误称为 P34/P35 完成或要求 N1 等待全部后续。原始测试证据保持 ignored；本文无原始日志、数据库或凭据。

## 1. 已核实的当前边界

真实 Provider 可以成功返回正文，但响应不带 usage。SDK 于是保留 `ProviderInvocationState.SUCCEEDED`，`usage_json.usage=None`；Agent turn 可正常 committed。**调用结果已知与账务已知是两件事**。priced 下 SDK budget charge 可能仍是 `ESTIMATED_UPPER_BOUND`，不能把该上界当成实际账单。

当前调用链与阻点：

| 入口 | 实际边界 |
|---|---|
| `simple_harness/execution/sqlite/uow.py::list_incomplete_provider_invocations` | 只扫描 CLAIMED/HANDED_OFF/UNKNOWN，SUCCEEDED 缺 usage 不会进入观察队列 |
| `simple_harness/execution/dispatch.py::ProviderInvocationCoordinator.reconcile_incomplete` | 只对上述扫描到的 UNKNOWN 调用配置的 `ProviderReconciliationPort.observe`；有真实迟到 usage 的外部观察者也不会收到 SUCCEEDED |
| `uow.py::record_provider_reconciliation` | 入口及事务内 CAS 都要求 UNKNOWN；不能直接把真实 SUCCEEDED 送入旧接口补账 |
| `uow.py::settle_provider_invocation` | 更新条件要求旧行 HANDED_OFF；普通 settlement 也不是终态 usage 更新接口 |
| `agent_orchestrator/runtime/provider_budget_guard.py` | 对 SUCCEEDED/FAILED 缺真实 usage 保留 UNKNOWN grant / 原 reservation，下一次需要 prior-output 的请求拒绝，不猜零 |
| `runtime/agent_worker.py::AgentBridge.usage_facts` | 缺真实 usage 不导入 append-only `imported_usage`，避免先导入零后永远丢失实际费用 |

所以当前有正确的保守保护，**没有 SUCCEEDED 缺 usage 的公开迟到账务补充通路**。改 owner、例行 reconcile、重开两库或 Mission cancel 都不能令缺失事实变成零；也不能为套用旧 API 把真实 SUCCEEDED 手改为 UNKNOWN，或重发 Provider 请求来取另一份账单。

主报告的 `priced-cold-v1` 1 PASS / 0.40s 是真实 UNKNOWN 丢回执→两库 close/reopen→原回复核对→原价 200 结算一次。它不是本接缝：这里原回复已经成功落库，只有 usage 遗漏。两种状态不得替代计数。两库重开也不是 OS kill。

## 2. 原计划适用性：保留为 P35 open gate

原事实源：Host `plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md` §7.3、§7.4、§11 的 P3.5-A04/A05/A06。

- **A04「未知支出不释放」**：当前保守 hold 与不导入假零符合这项安全要求；不能因暂时无法补账就释放原额度。测试通过只能证明守住此边界。
- **A05「恢复身份与 receipt 一致；已完成任务不重跑；UNKNOWN 先核对」**：已有结果不重跑已保持；账务未知需要独立核对，不能误称调用执行 UNKNOWN。若权威迟到账务可得，现接口无法接入它，这部分恢复未闭环。
- **A06「在途事实可结算」及 §7.3「取消后的旧回执可以作为已发生事实结算」**：当原调用的权威 usage 后来确实可得时，需能绑定原身份补账一次。当前缺通路，因此 P35 最终验收仍开放；不扩大成“所有 Provider 必须凭空给出丢失账单”的保证。

判定：这是原 P35 恢复/账务范围内的必要补充条件，不是需要扩大 Mission cap 的理由。实现仍由主线程分配；本次不修改 SDK API。没有权威 usage/price 证据时继续明确 UNKNOWN 是合法终态边界；有匹配证据却不能接收时，不能宣称完整 priced recovery 已完成。

## 3. 最小后续建议（未实施，不是现有 API）

建议将**终态调用的账务补充**与现有“未知执行是否发生”的 reconciliation 分开，复用原 invocation/turn/subject，不创建新 Provider 调用或另一个费用账户：

1. 增加窄的 pending-accounting 读取/观察端口，只选择真实已终结 SUCCEEDED，以及适用的 FAILED 缺 usage/可信价事实；不更改调用执行 state。外部 observation 必须绑定 invocation、handoff ordinal、run/request ID、request fingerprint、target digest、原 estimator digest 和权威 evidence ref。SUCCEEDED 不接受 `CONFIRMED_NOT_STARTED`，不接收替换正文或新结果。
2. 在 SDK 自身事务内补充不可变 accounting receipt，原 response/receipt/hash 不变；同 identity + 同证据重复无副作用，冲突 usage/price/evidence 内容拒绝。用原冻结价计算已知 micros，不能用当前新 profile 价格重算旧 call；缺可信价时 money 继续未知。原行版本/身份检查与 receipt 写入同事务。网络观察不放 DB 锁内。
3. 对外只提供原调用的有效 usage/charge 事实；guard 和 AgentBridge 复用它，将原 UNKNOWN grant 按真实值转 SETTLED/OVERRUN，并沿原 subject 将事实按原 invocation usage key 导入一次。跨 SDK/编排两事务域用幂等重读衔接，不声称跨库原子。大于原上界必须保留实际 overrun，不能截断到 grant 或偷偷抬原价表/总 cap。
4. Mission/Task 已取消仍允许原费用事实落账，但绝不接受新的业务结果或恢复执行。未知未解决时不自动 unpause；实际已知后是否恢复尚存合法工作，由现有 owner/合同/截止门重查，不借补账端口新建 Attempt。

最少决定性后续 oracle：真实 SUCCEEDED 缺 usage→权威原 usage 到达→原调用/响应不变且原价只结算一次；重复、错 request/ordinal/price、冲突 evidence 拒绝；cancel 后可补账但不接受结果；SDK 写完而编排 import 前冷重开可重读一次；实际 overrun 持久化后拒绝新准入。FAILED 有实际 usage 的既有可恢复 empty/length retry 控制必须继续通过。

## 4. 本次新增控制及验证状态

文件：`tests/orchestrator/p35/test_succeeded_missing_usage_boundary.py`，共 2 个测试。使用真实 SDK dispatcher、Provider response、priced guard 与两库，不手写 SUCCEEDED/UNKNOWN 行，不塞假 PASS。

1. Provider 实际产生回复和 150 tokens/原价 200 micros；transport 省略 usage，真实 SDK 写 SUCCEEDED。即使配置观察者持有匹配原回复，reconcile 不扫描它；现有两个公开写入口均明确拒绝。原响应、version、grant、原 hold 保持，无 imported 假零。
2. 两个实际 SQLite 连接关闭后重开，使用新 runtime owner；既有 SUCCEEDED 仍不重发、不被 UNKNOWN 执行核对替换；public Mission cancel 后原未知账务继续 held。此项没有伪称获得新的执行 lease epoch，已完成 turn 也不需要重激活来证明账务缺口。

**尚未运行 pytest。** 这两个是当前缺口的 characterization controls；将来 PASS 仅证明缺口描述及安全边界真实存在，不能标“迟到账务实现通过”。主 runner 命令：

```sh
.venv/bin/python -m pytest -q tests/orchestrator/p35/test_succeeded_missing_usage_boundary.py
```

生产实现后，应将“仍拒绝补账”的 characterization 断言更新为新端口的实际成功/拒绝 oracle，并保留当前无假零、不重发、身份和取消边界；不能让旧缺口测试永久要求缺口存在。
