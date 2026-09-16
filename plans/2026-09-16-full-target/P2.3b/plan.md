# P2.3b · HTN 接线第二步：event_handler 装配新模式 + versioning 走 InputManifest

- 计划包：FULL-TARGET-1.4 §7.2、§14、§18.2（versioning/workspace 行）、§18.5（四条硬约束）、§23 P2.3b 行、§24.1 裁决 3/4/11；附件 TG §7、§8.1–8.3、§10.1–10.3；实现稿 §6
- 基线：`simple-harness-sdk` main HEAD `7b0a88f`，工作树含未提交的 P2.3a 成果（只读输入）
- 热文件单代理：`orchestrator/event_handler.py`、`orchestrator/hierarchical_dispatch.py`（新建）、`artifacts/versioning.py`、`artifacts/workspace.py`

## 目标

P2.3a 交付了新模式**唯一的写入入口** `commit_plan_revision`。P2.3b 把它接到真实的编排循环上：

1. Planner 的 typed 回复变成一次计划修订（parse → compile → commit），被拒时在新快照上重编译一次、有上限、不自动 rebase；
2. 新模式的 list / ready / terminal / 并发统计 / 根评审全部改成读 `TaskNetworkSnapshot` + `evaluate_readiness`（两条 frontier），`TaskStatus.READY` 不再是判据；
3. compound 的推进由类型化 phase reducer 驱动，不创建假的 Worker Attempt，且在派发前被 `legacy_ready_is_not_eligibility` 拦截；
4. 产物物化改成只按 `InputManifest`，ORDER-only 祖先不进；拓扑不完整在执行/物化路径抛 `GraphIntegrityError`。

算法不堆进 `event_handler.py`：新增代码全部在 `orchestrator/hierarchical_dispatch.py`，`event_handler.py` 只做按语义版本的分派。

## 交付项

1. `orchestrator/hierarchical_dispatch.py`（新建）
   - `HierarchicalDispatch`：`network()` / `plan_view()` / `task_views()` / `readiness()` / `planning_frontier()` / `execution_frontier()` / `running_occurrences()` / `root_review_ready()` / `terminal()`；
   - `apply_planner_reply()`：`parse_plan_proposal` →（`RefineOperation`）→ `assess_method` / `ground_method` / `compile_refinement_bundle` → `CommitPlanCommand` → `commit_plan_revision`；被拒且原因可重编译 → 读新快照重编译，总次数上限 `compile_attempts`（默认 2）；用尽或不可重编译 → 记 `PlanCommitRefused` 事件并停止该轮；
   - `admit_method_proposal()`：`parse_method_proposal` → `MethodRegistry.admit`；
   - `intercept_worker_dispatch()`：`legacy_ready_is_not_eligibility(form=compound)` → 记 `HierarchicalDispatchIntercepted`（reason `NEEDS_REFINEMENT`）；
   - `CompoundPhase` + `next_compound_phase()` 纯 reducer 与 `advance_compound_phases()`（只记事件，不建 Attempt）；
   - `attempt_inputs()`：新模式一次派发的输入 = `InputManifest` 的物化条目。
   - 缺语义绑定 → `GraphIntegrityError`（不是 legacy fallback）。
2. `orchestrator/event_handler.py`：五处按语义版本分派（Planner 回复、派发前拦截、输入物化、终态/根评审、结果回调的 phase 推进）；`self._hierarchical` 默认 `None`，legacy 行为与事件字节不变。
3. `artifacts/versioning.py`：新增 `resolve_input_manifest()` / `manifest_upstream_inputs()` / `materialise_v2()`；legacy `merge_accepted` / `collect_upstream_inputs` / `topological` 函数体逐字节不变（测试用源码 hash 锁定）。
4. `artifacts/workspace.py`：`Workspace.materialise_manifest()` —— 只物化受控清单条目；旧入口不变。

## 红测试

- `tests/orchestrator/full_target/test_hierarchical_event_flow.py`（≥30 条，含 ≥5 条变异自证）
- `tests/orchestrator/full_target/test_versioning_v2_manifest.py`（≥20 条）

## 边界（本片不碰）

- `commit_service.py`、`plan_commits.py`、`scheduling/allocator.py`（P2.3c）、`contracts/`、`graph/`、`planning/`、`storage/`、`observability/traces.py`、`observability/evaluation.py`
- 不提交、不推送、不 `git stash` / `checkout`

## 已知取舍

- 一轮只装配一条 `RefineOperation`。多条操作合并成环的完整再验证是 §24.1 裁决 8 / P3.1 的范围，本片显式拒绝而不是默默取第一条。
- Manager 的图变更仍走 legacy `commit_graph_change`；新模式的 Manager 提案改走 `plan_commits` 属 P3.1。
