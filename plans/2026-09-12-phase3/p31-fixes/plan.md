# P3.1 遗留修复 · 计划（第 1 版）

- 版本目标：simple_harness 0.9.11 / agent_orchestrator 0.9.4
- 来源：Host 原生验收（2026-09-12）发现的问题，登记在 Host `plans/2026-09-11-orchestrator-host-integration/journal.md` §4.4 与 §5；独立评审裁决 C 里的 B 部分
- 验收：同目录 `acceptance.md`

## 1. 问题

### F-ORCH-1　Task 预算可以低于一轮模型调用的最低开销

事实（原生验收，真实 deepseek-flash）：
- Mission 的预算全为 null，所以 Planner 输入里 `budget_for_tasks.max_tokens` 也是 null。
- Planner 给两个 Task 写了 1200 和 800 tokens。
- `task_graph.validate_graph` 只检查上限（`fits_within`，以及各 Task 之和），`graph/changes.validate_change` 也一样，整个 SDK 没有下限的概念。
- Worker 的预留是 `min(attempt_reserve_tokens, task.max_tokens // candidates)`，这只是记账，不限制单次调用的实际花费：这一次按 8192 输出加上输入，实际结算 22003 tokens。
- 验证政策含 critic_review 时，Critic 还要从同一个 Task 账户预留 `critic_reserve_tokens`（6000）。
- 结论：一个 800 tokens、又带 critic_review 的 Task，由配置就能判定必然失败（`budget_exhausted`），不需要去估计模型会怎么表现。

理论依据：理论第 11 章说，资源由系统分配，不由 Agent 自定，子预算来自父预算。Planner 写出的数字只是提案，Commit Service 应当拒绝一个在结构上不可行的提案，而不是替它编一个数。

### F-ORCH-3　产物的 `verification_status` 永远是 UNVERIFIED

`contracts.models.Artifact.verification_status` 的默认值是 `"UNVERIFIED"`。整个 SDK 源码里没有任何地方更新过它，所以已正式交付的产物也显示为 UNVERIFIED。

### F-ORCH-2　Mission 结束后，个别 Attempt 停在 RETRY_WAIT

按 `contracts/state_machines.py` 的 docstring，这是有意的设计：`RETRY_WAIT` 就是失败 Attempt 的终态，即使之后不再重试也一样，原文 §25.2 没有另设 Attempt 的 FAILED 状态。所以这不是缺陷。本切片只在文档里写明，并在 Host 的 ARCHITECTURE 中说明，不改代码。

## 2. 改动

### 2.1 F-ORCH-1：Task 预算下限（D1）

- `runtime/assembly.OrchestratorConfig` 新增字段 `min_task_tokens: int | None = None`：
  - None 表示按部署推导：基本下限等于 `default_max_output_tokens`，即单轮最少可能产出的 token 数；
  - 0 表示关闭下限；
  - 正整数表示显式指定。
- 新增 `graph/task_graph.TaskBudgetFloor(base: int, critic: int)`，方法 `floor_for(verification_policy) = base + (critic if "critic_review" in policy else 0)`；base 为 0 时整体关闭，不再加 critic。
- `Orchestrator` 在构造 CommitService 时传入 `task_floor=TaskBudgetFloor(base, config.critic_reserve_tokens)`，写法与现有的 `deployed_layers` 相同。
- 两处关口：
  - `validate_graph`：在 `normalise_budgets` 之后，对每个 Task 生效的 `max_tokens`（Planner 显式写的，或按池分到的份额）检查；如果不为 None 且低于 `floor_for(policy)`，抛 `GraphRejected("budget", …)`，原因里写出 `task_budget_below_floor`、所需下限和组成；
  - `validate_change`：对每个新增 Task 的最终预算，含缺省份额，做同样的检查，抛 `GraphChangeRejected("budget", …)`。
- 被拒后的反馈沿用现有通道：Planner 通过 `planning_rejected`，Manager 通过变更被拒的反馈。Planner 输入的 `budget_for_tasks` 新增 `min_task_tokens` 和 `min_task_tokens_with_critic_review` 两个字段。角色模板保持不变，理由见 §4。
- 系统任务不受下限约束：合成任务和冲突任务的预算来自系统模板或预留，不属于 Planner / Manager 的提案。
- Mission 预算有上限时，如果 N 个 Task 各自的份额都低于下限，这张图本身就不可行，照样拒绝；如果 Mission 池连一个 Task 的下限都容不下，Planner 重试到上限后 Mission 失败，停止原因如实记录。

### 2.2 F-ORCH-3：接受结果时回写产物验证状态（D2）

`CommitService.accept_result` 在同一个事务里，把被接受结果的每个产物的 `verification_status` 改为 `"VERIFIED"`，通过 `store.upsert_artifact` 写入。

- 没被接受的结果（FAIL、被取代的候选）不改，保持 UNVERIFIED，这就是如实的状态。
- 回放模块 `observability/replay.py` 不折叠产物，所以回放覆盖率不受影响；全量回归里含第 8 步的回放与对账扫描，用它兜底确认。

### 2.3 版本

`version.py` 两个包分别升到 0.9.11 / 0.9.4；`tests/unit/contracts/public-api.json` 的版本号跟着改；另写 CHANGELOG。

## 3. 测试（先写，先红）

新文件 `tests/orchestrator/host_support/test_p31_fixes.py`，覆盖以下几组：

| 组 | 测试 |
|---|---|
| D1 关口 | ① 一张图里有 800 tokens 且政策含 critic_review 的 Task → 被拒，原因含 `task_budget_below_floor`；② 同样 800、政策不含 critic_review，下限 base=4096 → 被拒；③ base+critic 正好等于下限 → 通过；④ `min_task_tokens=0` → 不拒；⑤ Mission 预算有上限、Task 不写预算、按池分到的份额低于下限 → 被拒 |
| D1 变更 | Manager 的 `add_task` 预算低于下限 → `GraphChangeRejected("budget")`，原因含 `task_budget_below_floor`；缺省份额低于下限时同样被拒 |
| D1 反馈 | 用 scripted Planner：第一版图给 800，被拒；第二版给足，Mission 完成。两次 Planner 输入都带 `min_task_tokens`，第二次输入的 `planning_rejected` 里有下限的原因 |
| D1 系统任务 | 合成模板预算低于下限，仍然照常追加，不被下限拒绝 |
| D2 | notes Mission 完成后，被接受产物的 `verification_status == "VERIFIED"`；另一次运行里，第一个 Attempt 被判 FAIL、第二个 PASS，被拒那次的产物保持 UNVERIFIED |

另外，原有测试里凡是写了明确小预算的，都逐一核对过：step05 Manager 的 `add_task` 给 5000，高于 4096，而且那些 Task 的政策里没有 critic_review；其余几处是 Mission 或全局预算，或本来就是测拒绝的。全量回归由看门狗兜底。

## 4. 取舍

- **下限只拒绝，不替提案抬高**：自动抬高等于替模型编一个数，违背"模型只是提案方，由 Commit Service 裁决"。
- **不改角色模板**：改模板就要升提示词版本（planner-v5），并连带登记到策略库的 `prompt_versions`，范围扩大很多。在输入包里给出 `min_task_tokens`，再加上被拒的原因反馈，已经能让 Planner 知道下限在哪里。改模板留作 P3.4 时考虑。
- **下限用 `default_max_output_tokens` 推导**：它是这个部署里一轮调用最少可能产出的量，是保守的下界。实际输入 token 由 Context 大小决定，无法事先确定，所以不计入；实际超支由预算账本如实记录。
- **超支没有单独发事件**：评审建议"结算远超预留时记录可观测事件"，这涉及回放需要识别的事件类型，放到 P3.5（预算不能双花 / 真实负载）一并处理，登记为遗留。

## 5. 交付

1. 测试先红，然后实现并转绿；
2. ruff 与 mypy 干净；
3. 代码评审；
4. 全量回归，红集 ⊆ 73；
5. 构建 wheel 0.9.11，在干净环境验证；
6. Host 改钉 0.9.11，跑 `tests/orchestration`，并在 Host ARCHITECTURE 中写明下限与 F-ORCH-2 的语义；
7. 提交并推送。
