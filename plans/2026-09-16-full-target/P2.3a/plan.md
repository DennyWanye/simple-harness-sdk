# P2.3a · HTN 接线第一步：PlanCommitsMixin + Planner typed 提案 + task_graph v2 门

- 计划包：FULL-TARGET-1.4 §4 ADR-13（含 v1.3/v1.4 补充）、§7.4、§9.4、§14、§18.2、§18.5、§23；附件 TG §7.1–7.4
- 基线：`simple-harness-sdk` main HEAD `7b0a88f`
- 性质：接手前一位实施者中断留下的在途工作树，补齐 + 验证 + 记录，不重写
- 热文件单代理：`orchestrator/`、`runtime/role_templates.py`、`runtime/output_blocks.py`、`planning/planner.py`、`graph/task_graph.py`

## 目标

层次模式（hierarchical）多出一条**唯一的写入入口**：`CommitService.commit_plan_revision`。它把「模型提出的语义操作」变成「一个原子的 PlanRevision」，或者带具名理由整个拒绝。旧模式（legacy）的行为、字节与入口一条都不动。

## 交付项

1. `orchestrator/plan_commits.py`（新建）：`PlanCommitsMixin.commit_plan_revision`，九道闸门 + 一个写半场，顺序固定（见 journal §4）。
2. `orchestrator/commit_service.py`：mixin 入基类；`MissionSpec.orchestration_semantics_version`（服务端默认 `legacy`）；hierarchical Mission 的 `commit_task_graph` 要求每个 Task 有语义绑定。
3. `planning/planner.py`：`parse_plan_proposal` / `parse_method_proposal`，`SYSTEM_BOUND_FIELDS` 在边界拒绝模型自填的权限字段；旧函数不动。
4. `runtime/role_templates.py` + `runtime/output_blocks.py`：`plan_revision_proposal` / `method_proposal` 两个标签块常量、`planner-hierarchical-v1` 模板（新版本号，经 `register_template`）、`repair_hint` 有界修复提示。
5. `graph/task_graph.py`：`validate_graph_v2`（用 `GraphStructureBudget`，禁止 `dependencies` 偷编码 OR）；legacy `validate_graph` 不动。
6. `testing/fixtures.py` + `tests/orchestrator/fixtures_provider.py`：脚本化两种新块，保持旧 script 协议。
7. 测试：`test_plan_commits.py` 116 条、`test_planner_typed_proposal.py` 66 条（含变异自证）。
8. 旧模式零回归：`step02/03/05/06/07 + p33/p34/p35/p36 + test_critic_test_evidence_order.py`，以及 `tests/orchestrator/full_target` 全目录。

## 边界（本片不碰）

- `orchestrator/event_handler.py`、`artifacts/versioning.py` → P2.3b
- `scheduling/`（allocator form 门、readiness 入口）→ P2.3c
- `contracts/` 只读；本片无契约变更请求
- 不提交、不推送、不 `git stash` / `checkout`（共享工作树）

## 审阅后追加

独立审阅判「需修后合并」，12 项修复全部完成（详见 journal §11）。其中三项是**真实缺陷**而非测试缺口：
`_authorize` 的空 `issued_by` 可被任意同 scope principal 冒用；`require_commit_ready` 在写半场读取
openings **之后**的 duty 集合，导致任何带 `obligation_openings` 的修订都提交不了；`_check_structure`
不校验保留性，一次修订可以静默丢掉上一修订的 occurrence。另外把 `_or_smuggling` 更正为
`_representation_drift`（表示漂移检测，不是 OR 门），真正的 OR 约束补在 delta 层
`_check_alternatives_are_method_instances`。
