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
