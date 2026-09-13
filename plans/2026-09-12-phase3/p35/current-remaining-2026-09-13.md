# P3.5 当前验收索引

最后更新：2026-09-13。**功能与已批准源码原生 UI 范围：8/8 完成。** 不含打包、安装器或 P3.6。旧 A03/A07 UI 缺口及尾部增长待修状态已被下列后继证据替代。

共同回归：干净 SDK `f25a4de`，`g-current-orchestrator-full-v10`，**1826 PASS / 0 FAIL / 9 真实 Provider 默认 SKIP**；pytest 626.58 秒、runner 627.15 秒。下列 SDK 用例均在本次完整编排套件中。共享全量只计一次，不能逐行累加；真实模型收益属于 P3.4。

| 原验收项 | 已完成证据 | 已记录耗时与边界 |
|---|---|---|
| A01 物理槽位不抬高 | test_multi_profile_load.py；load-v18 两个实际 Worker、第三排队，多 profile 共享物理槽；最新全量通过 | 共享全量626.58秒；原生v18独立耗时未在本索引提取，不推算 |
| A02 排队不算失联 | test_queued_planner_cancel.py、test_queue_mission_deadline.py；load-v18/B2取消、恢复与零派发 | 与A06共享同一原生运行，不重复计时 |
| A03 验证背压闭环 | pressure-v24采样2 RUNNING+2 PENDING达总上限4；RAISED后Worker10K，降至低水位2后CLEARED、恢复20K；7 Mission UI人审完成 | 原生512.800秒；RAISED→CLEARED20.222秒。峰值来自同次持久采样，未声称峰值截屏或手动释放 |
| A04 预算不能双花 | typed/priced/UNKNOWN/late accounting；33b25b5按请求从原Task hold幂等增长，保护各活跃候选首Critic下限，初始分派/背压不变 | 增长控制65PASS/5.49秒；真实pair v5按原额度拒绝，不把失败说成模型收益 |
| A05 交叉故障恢复 | 实际SIGKILL成功回执/UNKNOWN恢复不重发；取证副本保留原hot journal/WAL给冷owner；ed42919合法败选Verifier迟返保留胜选收尾和真实错误 | 副本及原kill4PASS/9.79秒；迟返邻接31PASS/42.91秒；最新全量通过 |
| A06 控制面及时生效 | load-v18取消第三任务，槽释放后零调用；B2恢复保留取消；非法handoff/迟到账本控制 | 与A02共享原生证据，不重复计时 |
| A07 长Context | context-v23原生导入97,200字节，12页实际读取、8次持久轮转，原约束保留，报告/引用冷读一致；17调用不增；另有Context SIGKILL控制 | 原生299.810秒、冷读271.067秒；受控机制，不冒称真实模型长期记忆质量 |
| A08 多库备份恢复 | test_offline_backup.py正式3DB/WAL/CAS、损坏/锁/UNKNOWN/source控制；action冷备按原receipt核对；Host B2正式独立2DB/CAS恢复并由UI人审交付 | action cold v5 1PASS/8.35秒；B2 14 Provider记录不增。SDK三DB与原生两DB分别保留真实范围 |

最新通用原生复验：snapshot-v26（SDK ed42919/Host af8a490c）COMPARE新建、双候选实际code_test、独立综合、正式交付、同源冷启动全文读取通过。18调用、0rehandoff、40journal、18context selection、94Mission事件不变；部署层新增预期PolicyConfigDrift，ACTIVE冻结策略保留。原生178.704秒、冷读90.287秒。SDK f25a4de仅测试/文档后继，生产源码相同。

证据根：SDK `.local-test-evidence/2026-09-12/p33-g/`；Host `.local-test-evidence/2026-09-13/p33-g/`。原始证据不入Git。

- Host `source-ui-compare-v26/case-summary.json` SHA256 `780e52b243135f714bba7a86a00acaa3908cb28344c0846c65909894c52a04a3`。
- Host `replay-all-native-v5.json`：34DB/46Mission/80观察，0.847秒，PASS零差异/错误/额外调用；SHA256 `133846607875ee93355545f949cd461a378b25070bf5174f4cc71230f5050997`。
- 全量raw replay仍OPEN，负向fixture、存储诊断及观测范围见 [P3.3审计](../p33/reports/current-ac-audit-2026-09-13.md)；不把执行库盘点称作完整执行回放。

P3.5当前范围剩余任务 **0**。Phase3仍有P3.4真实模型固定子预算完整交付与收益判定门，未宣布整个Phase3完成。

原生退出补充：资源包装器所属进程组均正常退出，但后续进程盘点发现独立carrier PID71160（无backend/Vite监听）。已核对其完整路径属于source-ui-compare-v26，用SIGTERM退出并确认消失；不把进程组为空等同全部原生进程为空。额外窗口出现原因未判定，不冒称应用正常退出链已修复。Host证据 source-ui-compare-v26/detached-carrier-cleanup.json SHA256655eaf6269be6d8f2743cb1b136154b1ef66c40a652c0fd0a6b5d9fbd2b9b7a4。Mac防熄屏caffeinate仍保留。
