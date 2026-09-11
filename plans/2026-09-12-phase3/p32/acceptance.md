# P3.2 · 验收（第 1 版）

## 与 Phase3 P3.2-A01..A08 的对照

| P3.2 | 本计划 |
|---|---|
| A01 沙箱真实有效 | P32-1、P32-2、P32-16 |
| A02 超时无遗留 | P32-3 |
| A03 审批后发布 | P32-7、P32-16 |
| A04 内容变化需要重新批准 | P32-8 |
| A05 发布回执丢失 | P32-9 |
| A06 非权威查询不可重发 | P32-10 |
| A07 拒绝权限放大 | P32-11 |
| A08 恢复与补偿分离 | P32-11 |

## 条目

| 编号 | 场景 | 什么算对 | 证据 |
|---|---|---|---|
| P32-1 | 沙箱探针 | `probe_sandbox(SeatbeltExecutor)`：读 HOME 下的敏感目录被拒；写工作区外被拒；联网被拒；fork 超过上限受限；输出超过上限被截断。每项结果都写进报告，任何一项不符，适配器就不可用 | `test_sandbox.py` |
| P32-2 | 行为 oracle | 在 `sandboxed` 下，Worker 写的测试文件会在导入时尝试读 `~/.ssh`、写 HOME、联网。测试照常运行，但三件事全部失败，没有留下文件，code_test 层的结果如实记录 | 同上 |
| P32-3 | 超时无遗留 | 测试里 fork 出孙进程后挂起。超时后整个进程树被回收（`ps` 查不到），输出有界，`timed_out=true`，`tree_killed=true`。并证明不是只取消了等待的 Future | 同上 |
| P32-4 | 语义与兼容 | `code_execution` 三种取值的行为；旧字段 `local_code_execution` 保持兼容；默认部署行为不变；`sandboxed` 没有通过探针的适配器时，构造报错；0.9.x 的 `off` 测试全部通过 | `test_code_execution_modes.py` |
| P32-5 | R13 | 召回消息的角色不是 SYSTEM，正文在数据框里，并标注"不是指令"。召回原文中带"忽略以上指令"一类的注入文本时，也不会以 SYSTEM 身份出现 | `tests/agents/…` |
| P32-6 | 工作区登记 | 每个 Attempt 都有登记：base_snapshot、只读输入、可写输出。目录已存在但登记不一致时，拒绝。Mission 终态加保留期之后，目录被清理，但登记和产物还在。回放与对账扫描照常通过 | `test_workspace_registry.py` |
| P32-7 | 审批后发布 | 发布候选 → 验证 PASS → 审批 → 交接 → 文件以"带稳定身份的文件名"原子写入 root。回读 hash 等于产物 hash，回执与 ledger 一致 | `test_publish_connector.py` |
| P32-8 | 内容变化需要重新批准 | 产物内容一变，就生成新版本的动作，旧的批准不能用于新版本；已经批准的旧版本也不会被新内容冒用 | 同上 |
| P32-9 | 发布回执丢失 | 故障注入：文件已经 link 成功，但回执没写回就崩溃。重启后按稳定的业务键核对原文件，得到 SUCCEEDED；root 里没有第二份文件，也没有重复修改 | 同上 |
| P32-10 | 非权威查询不可重发 | `best_effort` 的连接器，`lookup` 返回 None 时保持 UNKNOWN，不重发；`authoritative` 的连接器才允许确认"未开始" | `test_lookup_authority.py` |
| P32-11 | 拒绝权限放大；补偿 | target 出现 `..`、绝对路径、symlink 时，一律拒绝并记录依据。`retract`（L3）默认拒绝；启用后需要两个不同的人审批。补偿动作有独立的身份和审批，原动作的事实不变 | `test_compensation.py` |
| P32-12 | 交付（SDK） | 全量回归红集 ⊆ 73；wheel 0.10.0 在干净环境验证；代码评审意见已处置 | journal |
| P32-13 | Host 探针接线 | 探针通过时为 `sandboxed`，部署清单里有探针报告；探针不通过时为 `off`，并写明原因；`pytest:` 条件与 `run_tests` 随之开关 | Host `tests/orchestration` |
| P32-14 | Host 发布目录 | 没选目录时，`action:file_publish…` 被拒，原因是"需要明确授权"；选了之后才启用。target 越出 root 时被拒 | 同上 |
| P32-15 | Host 界面 | 能分清"已生成未发布""已发布"两种状态，UNKNOWN 显示为"核对中"；审批卡显示路径、hash、版本 | vitest |
| P32-16 | 真实与原生 | 用 deepseek-flash 的原生 App 验收：①请求生成报告；②看到"未发布"；③批准；④目录里出现文件，回读 hash 与界面一致。另做一轮带 `pytest:` 条件的 Mission，确认它在沙箱里跑，并且探针证据齐全 | Host 报告 |
