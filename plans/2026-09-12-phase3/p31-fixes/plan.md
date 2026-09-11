# P3.1 遗留修复 · 计划（第 2 版，已处置计划评审第 1 轮，见 journal §1）

- 版本目标：simple_harness 0.9.11 / agent_orchestrator 0.9.4
- 来源：Host 原生验收（2026-09-12）发现的问题，记在 Host `plans/2026-09-11-orchestrator-host-integration/journal.md` §4.4 与 §5；以及独立评审裁决 C 里的 B 部分
- 验收：同目录 `acceptance.md`

## 1. 问题

### F-ORCH-1 Task 预算可以低于一轮模型调用的最低开销

事实（原生验收，真实 deepseek-flash）：
- Mission 的预算全为 null，所以 Planner 拿到的 `budget_for_tasks.max_tokens` 也是 null。
- Planner 给两个 Task 写了 1200 和 800 tokens。
- `task_graph.validate_graph` 和 `graph/changes.validate_change` 都只检查上限，SDK 里没有"下限"这个概念。
- Worker 的预留是 `min(attempt_reserve_tokens, task.max_tokens // k)`，其中 k 是 `candidates_per_task`。这个预留只是记账，不限制单次调用：这次实际结算了 22003 tokens。
- policy 含 critic_review 时，**每个候选的结果**都要从同一个 Task 账户再预留一份 `critic_reserve_tokens`。

所以，一个预算小于"单轮最少可能的产出 + Critic 预留"的 Task，连第一个 Attempt 加它的 Critic 都预留不起，从配置上就能判定会失败。

理论依据：第 11 章说，资源由系统分配，不由 Agent 自定，子预算来自父预算。Planner 写的数字只是提案；一个连第一步都走不了的提案，Commit Service 应当拒绝，而不是替它编一个数。

**说明**：下限只是**必要条件**。它保证第一个 Attempt 加它的 Critic 能预留成功，但保证不了后面的修复和重试。账户剩余额等于上限减预留再减已结算，真实一轮就可能结算好几万。之后预算不够，照旧由 `budget_exhausted` 如实停止。

### F-ORCH-3 产物的 `verification_status` 一直是 UNVERIFIED

`Artifact.verification_status` 默认为 `"UNVERIFIED"`，SDK 里没有任何地方更新它。`Store.upsert_artifact` 用的是 `INSERT … ON CONFLICT DO NOTHING`，所以对已有的行也改不了。

### F-ORCH-2 Mission 结束后个别 Attempt 停在 RETRY_WAIT

这是设计如此：原文 §25.2 与理论第 09 章里，RETRY_WAIT → PENDING 指的是另起一个新的 Attempt；代码的 `TERMINAL_ATTEMPT` 含 RETRY_WAIT。所以不改代码，只在文档里说明。另外建议 Host 读模型在 Task 已经终止时，把它显示为"失败（未再重试）"，记为遗留建议。

## 2. 改动

### 2.1 F-ORCH-1：Task 预算下限

- `graph/task_graph.TaskBudgetFloor(base: int, critic: int)`，方法 `floor_for(policy, candidates=1) = candidates × (base + (critic if "critic_review" in policy else 0))`。base 为 0 时表示关闭。
- 缺省一律**不检查**：`validate_graph(..., task_floor=None, candidates=1)`、`validate_change(..., task_floor=None, candidates=1)`、`CommitService(task_floor=None, candidates_for=None)`。只有 `Orchestrator` 会注入下限，其他地方直接构造 CommitService（源码与测试里共 23 处）行为不变。
- `OrchestratorConfig.min_task_tokens: int | None = None`：
  - None 表示 base 取 config 与所有 profile 的 `default_max_output_tokens` 中的最大值，也就是 Worker 可能路由到的 profile 中产出最多的那个；
  - 0 表示关闭；
  - 正整数表示显式指定 base。
  - critic 部分取 `critic_reserve_tokens`。
- `Orchestrator` 构造 CommitService 时传入 `task_floor` 和 `candidates_for=lambda mission_id: int(self.policy_for(mission)["candidates_per_task"])`，按 Mission 绑定的策略取 k。
- 两处检查：
  - `validate_graph`：在 `normalise_budgets` 之后，**只遍历 `proposal.tasks`**，检查每个 Task 生效的 `max_tokens`，即显式写的值或从池里分到的份额。低于 `floor_for(policy, k)` 时，抛 `GraphRejected("budget", "task_budget_below_floor: …")`，原因里写出所需的下限、base、critic、k。合成模板那段检查不动。
  - `validate_change`：对新增 Task 的最终预算（含缺省份额）做同样的检查，抛 `GraphChangeRejected("budget", …)`。
- 反馈给模型：
  - Planner 输入的 `budget_for_tasks` 增加 `min_task_tokens`（即 k×base）与 `min_task_tokens_with_critic_review`（即 k×(base+critic)）；
  - Manager 输入包同样增加这两个字段；
  - 被拒后，Planner 从 `planning_rejected` 读到原因，Manager 从 rejections 读到原因。
- 系统任务（合成、冲突）不受下限约束。
- Planner 一直提议低于下限的预算时，会用完重试次数，Mission 以 `planning_failed` 结束，失败原因里带 `task_budget_below_floor`。如果提案达到了下限、却超出预算池，被拒的原因就是"超出 Mission 预算"，这是如实的。规划开始前预检"池容不下一个下限"这件事登记为后续项，出自代码评审 P2-3。
- 措辞（代码评审 P2-2）：下限是**预留层面**的必要条件，前提是一轮结算的用量不超过它的预留。真实模型一轮会连输入一起结算，远超 base，所以预算正好等于下限的 Task，第一轮之后照样可能付不起 Critic。
- 真实模型：在 `REAL_KNOBS` 的调参下，含 critic 的下限会升到几万 tokens，真实 Planner 可能要多试一两次。本仓库的真实 opt-in 测试把 `max_planning_attempts` 调到 3。

### 2.2 F-ORCH-3：产物验证状态

- 新增 `Store.update_artifact_verification(artifact_id, status)`，用 UPDATE 改 json 列里的 `verification_status`，必须在事务内调用。
- `accept_result`：在同一个事务里，把被接受结果的产物（`stored.artifacts`，即 artifact id）改为 `"VERIFIED"`。
- `fail_result`：在同一个事务里，把该结果的产物改为 `"REJECTED"`。
- 被取代的候选保持 `UNVERIFIED`。
- 回放不折叠产物，不受影响；全量回归里含第 8 步的回放与对账扫描，作为兜底。

### 2.3 版本

两个包的 `version.py` 分别升到 0.9.11 / 0.9.4，同步 `public-api.json` 与 CHANGELOG。

## 3. 测试（先写，先红）

`tests/orchestrator/host_support/test_p31_fixes.py`：

| 组 | 测试 |
|---|---|
| 关口 | 7 组参数：正好等于下限的通过，差 1 的被拒，带或不带 critic 分别验证，base=0 时关闭；池份额低于下限被拒；k=2 时下限翻倍（10096 不够，20192 才够）；缺省参数时行为不变；合成模板不受约束 |
| 变更 | Manager 的 `add_task` 低于下限被拒，高于下限通过；缺省份额低于下限被拒 |
| 反馈 | 用脚本化的 Planner，而且**正好 2 步**（`max_planning_attempts=2`）：第一版给 800 被拒，第二版给 30000，Mission 完成；两次输入都带下限字段，第二次带着拒绝原因。Mission 预算为 null |
| 失败 | 预算池 5000（小于一个下限）→ Planner 两次都被拒 → `planning_failed`，失败原因里带 `task_budget_below_floor` |
| Manager 输入 | Manager 输入包里带下限字段（直接调用 context_builder 的 Manager 包构造） |
| 关闭 | `min_task_tokens=0`：800 通过，Mission 完成 |
| 产物 | 先 FAIL 后 PASS：用新连接从库里读，被接受的是 VERIFIED，被判 FAIL 的是 REJECTED；在 `after_accept_before_supersede` 注入崩溃后，库里仍是 UNVERIFIED |

## 4. 取舍

- **只拒绝，不替提案抬高**：自动抬高就等于替模型编一个数。
- **不改角色模板**：改模板就要升提示词版本，还要牵动策略库；在输入包里写明下限，再加上拒绝反馈，已经够用。P3.4 再考虑改模板。
- **超支事件**（结算远超预留时发一个事件）放到 P3.5 处理。
- **Host**：门口已经补了默认预算，原生路径走不到 null 预算，所以 F-ORCH-1 由 SDK 测试证明；Host 这边只需要重钉 SDK，再补文档说明。

## 5. 交付

测试先红后绿 → ruff 与 mypy → 代码评审 → 全量回归（红集 ⊆ 73）→ 构建 wheel 0.9.11 并在干净环境验证 → Host 改钉，跑 `tests/orchestration`，更新 ARCHITECTURE（下限、产物状态、RETRY_WAIT 的语义）→ 推送。
