**12:48更新：** A03原生总阈值4、减速10K、低水位2恢复20K及七任务人审交付已通过（v24）；A07原生12页/8次实际轮转/约束引用/冷读已通过（v23）。旧行的UI待验为历史状态。当前仍待system Worker原保护额度按请求增长修复、Critic schema重试反馈与最终回归。详见HANDOFF与ARCHITECTURE最新记录。

**11:48更新：** Manager历史Attempt准入与迟到账户两处实际缺陷已修，28+35项局部回归通过（重叠不相加）；全量关联仍等最终运行。原生COMPARE交付/冷读已PASS，UI文案77PASS/native待。A03完整阈值、A07原生轮转仍因UI操作接口noWindowsAvailable待完成。

# P3.5 当前验收索引（2026-09-13）
状态：功能验收进行中；不打包、不进入 P3.6。源码检查、受控原生 UI、真实模型与故障恢复分开计数。这里替代先前未通过审阅的草稿。

| 原验收项 | 当前证据 | 剩余动作 |
|---|---|---|
| A01 物理槽位不抬高 | p35/test_multi_profile_load.py；Host source-ui-load-v18 两个同时实际 Worker、第三排队；取消后零调用 | 最终全量关联；保留实际调用区间与峰值 |
| A02 排队不算失联 | p35/test_queued_planner_cancel.py、test_queue_mission_deadline.py；原生 v18/B2 取消和恢复 | 最终全量关联，不重开已通过原生取消 |
| A03 验证背压闭环 | test_verifier_pressure_load.py、test_pressure_priority_drain.py；原生 pressure-v19 两个 RUNNING、一个 PENDING 可见，20秒自动排空 | 原生补齐达到压力阈值后的 RAISED/CLEARED 与减速关联；v19不代表pending上限4饱和 |
| A04 预算不能双花 | test_tail_and_priced_budget.py、test_first_request_guard_integration.py、test_late_accounting_runtime.py；826c0e1 保持typed Attempt耗尽 | 最终全量关联。真实对照v1没有逐请求准入，不能当配置正确的生产证明；已补测试配置，7项纯检查通过，尚未重跑付费组 |
| A05 交叉故障恢复 | test_process_kill_recovery.py、test_action_cold_backup.py；实际SIGKILL、丢回执UNKNOWN、按原receipt核对 | 最终全量关联；UNKNOWN与成功终态的边界保留 |
| A06 控制面及时生效 | 原生v18 UI取消第三任务，槽释放后无实际调用；B2恢复后取消仍保留；SDK拒绝非法handoff与late accounting控制 | 最终全量关联 |
| A07 长Context | test_context_cold_recovery.py 已证SIGKILL后冻结请求不重新检索；新Host native_context 12页实际读取/轮转/原约束/未决状态/完整journal/冷读 2PASS 4.96s | 同一97,200字节文件的原生UI导入、交付和冷读 |
| A08 多库备份恢复 | test_offline_backup.py 正式3DB/WAL/CAS与拒绝矩阵；test_action_cold_backup.py 外部事实核对；Host B2官方独立2DB/CAS恢复成功/取消/待人，UI复核后正式交付、14 Provider记录不变 | 最终全量关联与现有manifest索引，非重新实现API |

证据根：SDK `.local-test-evidence/2026-09-12/p33-g/`；Host `.local-test-evidence/2026-09-13/p33-g/`。当前未关闭的是 A03 原生阈值、A07 原生轮转及累计全量关联；表中其它行已有实现和决定性局部证据，不声称整个阶段已完成。真实模型比较是否证明价值另归 P3.4，不把失败次数少误当优势。
