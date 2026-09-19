# H1-S 实施记录

基线：`0d89307`；工作树：`h1-s-protocol-switch`。

先提交红测试：`37af171 test(h1-s): specify durable planning protocol switch`；主审补充约束后的红测试：`bac9176 test(h1-s): tighten protocol validation and rollback coverage`。

实现范围严格限于白名单：

- `MissionSpec` 增加默认关闭的 `planning_protocol_version`，默认规格 JSON 不增加字段，并支持对称读取与未知值拒绝。
- 新协议创建在 Mission 事务内写入 `mission_planning_protocols` 绑定；绑定摘要是协议名、包版本 4、提示词版本 v8 三项规范 JSON 的 SHA-256。
- 新增只读 `planning_protocol_for_mission`，缺行表示旧协议；重放校验绑定不可切换。
- 协议常量复用 `contracts.planning_decisions`；策略摘要不随协议开关变化；事务测试在绑定已写入后注入失败并确认整体回滚。
- 复核补充：同一冻结策略下实际创建 legacy/new 两个 Mission，比较持久化 policy binding 与 policy version 的 `params_hash`/`params`，均不含协议字段。

验证命令：

```text
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_protocol_switch.py tests/orchestrator/step02/test_commit_service.py -q -p no:cacheprovider
..............                                                           [100%]
14 passed in 0.49s

uv run --offline ruff check src/agent_orchestrator/orchestrator/commit_service.py src/agent_orchestrator/orchestrator/planning_protocol_binding.py tests/orchestrator/full_target/test_planning_protocol_switch.py
All checks passed!
```
