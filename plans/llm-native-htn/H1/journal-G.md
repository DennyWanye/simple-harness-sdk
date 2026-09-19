# H1-G 实施日志：已准入规划决定适配层

## 1. 范围与约束

本片基于提交 `5804dc3` 的输入形状，使用 `planning/decision_admission.py` 中现有的
`AdmittedPlanningDecision`，未修改准入类型。新增文件仅为：

- `src/agent_orchestrator/planning/decision_adapter.py`
- `tests/orchestrator/full_target/test_planning_decision_adapter.py`
- `plans/llm-native-htn/H1/journal-G.md`

适配器是纯值函数，不访问存储层、不执行 SQL、不发事件、不调用 grounding、compiler 或
commit。`AdapterContext` 保存请求绑定的任务上下文；`AdmissionContext` 也可直接作为输入，
由适配器读取 `binding.mission_id`、`binding.base_plan_revision` 和请求保存的可见读集。

## 2. 测试先行

先新增测试并运行，收集阶段按预期失败：

```text
E   ModuleNotFoundError: No module named 'agent_orchestrator.planning.decision_adapter'
```

红测试先提交为：

```text
[h1-g-decision-adapter ec71477] test(h1-g): add red tests for planning decision adapter
```

之后新增实现；等价性测试中的旧协议文本由独立的 `<plan_revision_proposal>` JSON 构造，
再调用现有 `parse_plan_proposal`，与适配结果做规范 JSON 字节比较。

## 3. 映射结论

| 新决定 | 适配结果 |
|---|---|
| `REFINE` | 一个现网 `RefineOperation` |
| `REPAIR/REPLACE_METHOD` | `RetireMethodOperation` 后接 `RefineOperation`，系统策略为 `REQUEST_STOP_THEN_RECONCILE` |
| `REPAIR/PROPOSE_SUCCESSOR` | 一个现网 `ProposeSuccessorOperation` |
| `BIND_EXISTING_GOAL` | 一个现网 `BindSharedGoalOperation`，`REUSE_ACCEPTED` 与 `SHARE_ACTIVE` 由 `resolution_id` 的有无保持现网形状 |
| `DECLARE_BLOCKED` | `DurableOnly`，保留 blockers 与 resumable 条件，交由现网停滞/合成逻辑消费 |
| `WAIT` / `NO_CHANGE` | `DurableOnly`，不生成 `PlanProposal` |

`proposal_id`、`mission_id`、`expected_plan_revision`、`read_set`、`trigger_refs` 和运行中工作
策略不从模型决定读取。方法引用使用准入阶段解析后的 `MethodRef`；模型 bindings 里的同名
键只保留为领域参数值，不会覆盖提案系统字段。未启用的决定类型在适配边界抛出
`ContractError`。

## 4. 验证记录

定向测试与变更文件静态检查的命令尾行原样如下：

```text
.........                                                                [100%]
9 passed in 0.29s
All checks passed!
```

全量目标目录命令的测试尾行原样如下：

```text
SKIPPED [1] tests/orchestrator/full_target/test_panda_backend.py:595: no real pandaPIparser configured via SH_PANDA_PARSER
SKIPPED [1] tests/orchestrator/full_target/test_real_provider_hierarchical_smoke.py: needs --run-real-provider

3619 passed, 2 skipped in 138.69s (0:02:18)
```

执行过的验证命令：

```text
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_adapter.py -q -p no:cacheprovider
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
PYTHONPATH=src uv run --offline ruff check src/agent_orchestrator/planning/decision_adapter.py tests/orchestrator/full_target/test_planning_decision_adapter.py
```

全仓 `ruff check src tests` 还会命中既有白名单外文件；本片未修改这些文件。变更文件的
ruff 检查通过。

## 5. 收尾

实现提交使用题目要求的提交说明：

```text
feat(h1-g): adapter from admitted planning decisions to the existing proposal chain
```

提交后再次检查工作树；本片不删除任何文件，不改 `contracts/`、`planner.py`、
`event_handler.py`、`hierarchical_dispatch.py` 或存储层。

## 6. 变基后的接手与 2026-09-19 15:20 追加裁定

接手时工作树干净，上一轮本片提交为：

```text
4d3fb90 test(h1-g): add red tests for planning decision adapter
1eb7b66 feat(h1-g): adapter from admitted planning decisions to the existing proposal chain
```

新主干同时带入合并提交 `400e3c4 merge(h1-t): demote bind-existing-goal and propose-successor to decode-only`。
追加裁定明确：本阶段 `BIND_EXISTING_GOAL` 与 `REPAIR/PROPOSE_SUCCESSOR` 只解码、持久化，
准入层应拒绝；适配层即使异常收到它们，也必须报编程错误，不能静默生成现网编译器无法消费的提案。

本轮变更如下：

1. 删除适配器对 `BindSharedGoalOperation` 与 `ProposeSuccessorOperation` 的执行映射和相关的旧正例等价性测试。
2. 新增两条回归测试：若这两种决定绕过准入到达适配器，均抛 `ContractError`。
3. `DurableOnly` 新增并保留输入决定的 `canonical_hash`，覆盖 `WAIT`、`NO_CHANGE`、`DECLARE_BLOCKED`。
4. WAIT 测试逐项断言 `wait_for` 与决定载荷相等，避免清空等待引用的变异静默通过。

本轮仍保留并验证的执行等价类型为 `REFINE`、`REPAIR/REPLACE_METHOD`；声明受阻、等待、
不改继续只产生 durable-only，不生成计划提案。上一轮的共享绑定/提后继等价性测试不再适用，
原因是追加裁定已将它们从本阶段执行集合移除，而不是改变现网编译器。

## 7. 本轮验证

测试先行的新增回归测试初次运行确实失败，尾行如下：

```text
4 failed, 7 passed in 0.32s
```

实现后的定向测试与 ruff 尾行如下：

```text
........                                                                 [100%]
8 passed in 0.25s
All checks passed!
```

全量目标目录尾行原样如下：

```text
SKIPPED [1] tests/orchestrator/full_target/test_panda_backend.py:595: no real pandaPIparser configured via SH_PANDA_PARSER
SKIPPED [3] tests/orchestrator/full_target/test_planning_decision_admission.py:1248: codec-level refusal is covered by the codec test
SKIPPED [1] tests/orchestrator/full_target/test_real_provider_hierarchical_smoke.py: needs --run-real-provider

3651 passed, 5 skipped in 131.52s (0:02:11)
```

## 8. 第二轮核验缺口收口与变异清单

第二轮核验指出：若把 `DECLARE_BLOCKED` 的 durable 结果伪装成 `NO_CHANGE`，原测试仍
可能通过。本轮新增断言，要求 durable 结果的 `decision_type`、`reason`、`wait_for`、
`blockers`、`resumable_if` 分别与决定载荷及对应类型的空集合逐项相等；因此声明受阻会
保留受阻项、受阻原因和恢复条件，不会退化成不改计划。

本轮先提交测试强化：

```text
[h1-g-decision-adapter de6d1a0] test(h1-g): distinguish blocked and no-change durable signals
```

已执行并杀死的临时变异（均未提交，随后恢复原实现）：

| 变异 | 结果 |
|---|---|
| `DECLARE_BLOCKED` 的 `decision_type` 改为 `NO_CHANGE` | `1 failed, 7 passed in 0.30s` |
| `WAIT` 的 `wait_for` 改为空元组 | `1 failed, 7 passed in 0.25s` |
| `DECLARE_BLOCKED` 的 `blockers` 改为空元组 | `1 failed, 7 passed in 0.25s` |

`DurableOnly.canonical_hash`、WAIT 的等待引用、NO_CHANGE 的空字段、BLOCKED 的阻塞项与
恢复条件均有定向断言。恢复后定向套件为 8 passed，变更文件 ruff 为 `All checks passed!`。
