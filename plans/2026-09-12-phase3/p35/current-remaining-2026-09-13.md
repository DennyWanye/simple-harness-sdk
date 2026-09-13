**Last updated: 2026-09-13 16:48 CST — P34 Manager/selection integration corrected, actual gate still OPEN.** Real v7 on6c17d4d failed both arms with no_progress (FIRST327.513s/441139tokens/52calls; COMPARE337.840s/422739tokens/49calls), exposing ambiguous graph wire examples and missing empty-COMPARE fragment recovery. New default manager-v4 preserves all historical prompt hashes; a bounded empty round now gets one funded independent F review, never resets A/budget/deadline, and only verified F plus a normal graph commit can retarget C. Completed compare predecessors remain allocator facts; C candidates and new synthesis retain exact original-round fragment inputs. Round/deadline validation is atomic with F creation; legacy receipts cannot gain a new selection binding. New8 runtime controls include two library cold gaps and a commit-time expiry. Adjacent1292PASS/4 old-version assertion FAIL/5 optional SKIP142.78s; corrected affected52PASS/noSKIP10.12s, including pinned tokenizer and legacy red-to-green control; ruff/mypy117PASS. Sol/high independent findings fixed, runtime/usage/rework recorded. Source-native Critic document/cold v27 also PASS on6c17d4d (7calls/1050tokens/0reserve/0rehandoff, artifact/hash and durable counts unchanged); this predates new selection code. Clean committed recheck, new fixed real v8 and cumulative/latest-native gates pending; P34/P35-A04 and whole Phase3 remain OPEN. Details: plans/2026-09-12-phase3/p34/manager-selection-fragment-fix.md. No packaging/P36/push.

**Last updated: 2026-09-13 16:01 CST - same-Task Critic growth and typed admission fix.** Critic can transfer only its request deficit from the original protected Task hold, keeping frozen identity/price, sibling first-Critic floors and UNKNOWN usage intact. Nonretryable SDK admission now atomically rejects/stops the Task without schema retry or Worker redo. New decisive old-source7FAIL/7PASS; corrected patch63PASS6.72s, adjacent1074PASS60.52s, tokenizer11PASS0.39s and cold-library2PASS1.58s; mypy117/ruffPASS. Independent Sol/high review no concrete P0/P1/P2. Cold tests preserve actual request identities and200000 Critic usage through both failed-intent/error-layer gaps, no new handoff. Fixture and parent integration rework retained in plans/2026-09-12-phase3/p35/critic-hold-admission-fix.md. Clean committed real v7 pair and current cumulative/source-native gates remain pending; P34/P35-A04 not yet reclosed. No packaging/P36/push.

**最后更新：2026-09-13 15:36 CST — 真实对照暴露后继缺陷，P35-A04重新打开。** 新profile docs480-s240-v3在干净3164954真实deepseek-flash/API准入双臂执行：FIRST276.552秒/563256tokens/63调用，COMPARE174.378秒/281851tokens/34调用；均budget_exhausted，runner451.71秒。所有97实际响应model=deepseek-flash，用量已知，97准入，未见物理Provider错误；不声称交付/收益。证据SDK .local-test-evidence/2026-09-13/p34-real-search-value-7c36782f54774c54983a571549c85148/pair-summary.json SHA25694626876cfc1f679fec87bf290b208f375b0506ec36c068bad82e8a17708613b。FIRST的S原hold尚有122683，Critic1已用24614、原预留40960、下一请求22999需增长6653，却因system增长路径排除Critic而回退普通账户余额0拒绝；public_output=null，无已观察schema错误。不可重试admission被包装为ContractError，又运行Critic2及新Worker，后者额外65399tokens仍失败。Astra独立诊断确认hold增长与错误分类两处缺口，待先红后绿修复。COMPARE则B完成、A原240K扣120K综合/40960首Critic后，Worker58811+下一请求23710超可用79040，仅差3481；为合法原额度拒绝。另登记audit320-docs480-s240-v4，每Mission总2M不变；A320K/B480K/S240K/C400K，旧两合同及全部oracle/material不变，12纯配置PASS0.21秒/runner0.76，新真实组未运行。P34继续OPEN，P35-A04因新缺陷重新打开，不用上一全量绿覆盖新发现。

# P3.5 当前验收索引

最后更新：2026-09-13。**历史f25a4de范围曾8/8完成；当前A04与共享后继回归重新打开，不能沿用8/8结论。** 不含打包、安装器或 P3.6。旧 A03/A07 UI 缺口及尾部增长待修状态已被下列后继证据替代。

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
