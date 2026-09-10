# Slice 1 自我批评（SELF_CRITICISM）

- 真拦住问题的环节：**独立 primary 挑战**（发现"子 Agent 永不终态会切断 child_terminal_receipts 通道"这条结构性矛盾，避免了 T8 做完才断链）和 **closure 复核**（子 binding 外键顺序、围栏事务不可嵌套、失败载体会杀 Agent）；**真实 provider 一枪**（暴露了 mock 完全测不到的两件事：端点拒绝带 `.` 的函数名、持久 Context 里 assistant 没有 tool_calls）。
- 空转的门：机器账本（未启用，无损失）；plan 初稿的行号锚点核对——closure 复核已核过一次，T2 第 0 步再核只是重复劳动。
- 被仪式拖慢的地方：把 21 条裁决逐条写落点表花了一轮子代理；下次可以只写"裁决 → 任务"两列。
- 下次改进：真实 provider 探针应在 T6.5 里程碑后就打一枪（而不是等到 T10），端点差异会更早暴露；mock provider 应校验请求形状（assistant/tool 消息配对），让 wire 类缺陷在 mock 层就红。
