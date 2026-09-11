# Agent 编排框架 · 交接说明（截至第 6 步交付）

- 日期：2026-09-11
- 仓库：`simple-harness-sdk`，分支 `main`，交付提交 `26ecf96`（已推送 origin/main，本地干净，只有主工作树）
- 版本：simple_harness **0.9.4** / agent_orchestrator **0.6.0**（同一个 wheel）
- 纲要：Host 仓库 `simple_harness/plans/taskSys2/agent-orchestrator-incremental-build-plan-phase2-zh-CN.md`（ORCH-BUILD-v1.0，第 2–9 步）；原文 `agent-orchestration-layer-complete-design.md`；术语 `agent-orchestration-theory/`
- 本仓库的总表：`plans/2026-09-11-agent-orchestrator/program.md`；每步 `stepNN/{plan,acceptance,journal}.md` 与 `reports/`；测试归档 `testcase/agent-orchestrator-stepNN/`

## 1. 现在做到哪里

| 步 | 功能 | 状态 | wheel 源提交 |
|---|---|---|---|
| 2 | 单 Task Mission 可靠验收闭环 | SHIPPED 0.9.0 | `bd308dd` |
| 3 | Planner 拆解并并行执行静态 DAG | SHIPPED 0.9.1 | `d919ba5` |
| 4 | 团队共享知识、冲突仲裁、综合 | SHIPPED 0.9.2 | `57e6368` |
| 5 | 按 Worker 返回动态修改 Task DAG | SHIPPED 0.9.3 | `399d0e7` |
| 6 | 多 Mission、多模型、背压、隔离 | SHIPPED 0.9.4 | `a2ce656`（sha256 `0f292029ca5aca3499db0ad780036fb23096a81776372e0b0ce43a02624803f2`） |
| 7 | Human-in-the-loop 与受控真实操作 | **下一步** | — |
| 8 | 归因、Replay、策略对照评测 | 待做 | — |
| 9 | 从历史学习并受控晋级 | 待做 | — |

第 6 步最终证据：`tests/orchestrator` 184 passed / 5 skipped（skip 的是真实模型 opt-in 测试）；SDK 全量回归红集 73 条 = 基线、0 新红；wheel 在干净 venv 安装后 447 passed / 1 failed（基线已知红）；真实 deepseek-flash 两执行池运行 1、2 都完成。细节见 `step06/journal.md` §4、§6。

## 2. 下一步（第 7 步）怎么开始

1. 读纲要 §9（第 7 步）与原文 §22；先在 `agent-orchestration-theory/` 查 Human-in-the-loop、审批、副作用、幂等键等术语定义，定义与原文不一致以原文为准并在 plan 里注明。
2. 建 `plans/2026-09-11-agent-orchestrator/step07/{plan,acceptance,journal}.md`，照前几步的格式：术语段、主要矛盾、范围、决定表、任务切片、风险；验收按纲要 §9 的 S7 条目逐条写成可判定形式。
3. 独立 plan review（子代理，model 用 opus，只读）→ 在 plan §6 与 journal §1 逐条处置。
4. 测试先行、按切片实现并各自提交；每个切片跑 `tests/orchestrator` 全套（用带超时的 python 子进程包起来，macOS 没有 `timeout`）。
5. 独立代码 review → 修复并配决定性测试 → 全量回归 → 重建 wheel → 真实模型证据 → journal §3–§6 → program.md 标 SHIPPED → 推送 origin main → 确认 `git status` 干净、`git worktree list` 只有主工作树。
6. 第 6 步已为第 7 步留好的接口：`human_review` 验证层目前"提交期拒绝、运行期阻塞"（`verification_policy_undeployed` / `verifier_unavailable`），第 7 步部署它；`run_tests` 仍无网络隔离（L2-4）；短期 Capability Token 未做。

## 3. 不能破坏的不变量

- 编排库 `orchestrator.db` 只有 Commit Service 写（包括 `scheduler_state`、`tool_calls`、各类事件）；SDK 执行库只有 SDK 写。两库之间没有原子事务，靠 dispatch intent + 稳定身份（creation_key / input_id）+ SDK 幂等 + 回执核对 + 结果去重。
- §25 状态机不加回边；执行中的任务要改合同只能 supersede 成新实体；已完成任务只被引用、永不重跑。
- 预算两层不混：额度层（Reserve / Settle，`governance/budgets.py`）与费用事实层（SDK invocation ledger）；每个 `usage_ref` 至多导入一次；unpriced 部署金额记 null，不写 0；UNKNOWN 费用让预留保持占用（`ReservationHeld`）。
- 路由结果冻结在 dispatch intent 里（`runtime_profile_id` / `model`），回显核对只和冻结值比；一个 Attempt 只在自己的执行池里恢复，另一个模型不能接管。
- `Store.transaction()` 不能跨 await；持有者校验会拒绝别的协程闯入未关闭的事务。
- 密钥不进模型上下文（包构建时按字段名和值模式检查，命中则该工作停止为 `context_rejected`），也不进证据（JSON 脱敏、带密钥的 artifact 不复制）。

## 4. 已知陷阱

- 崩溃点落在 provider 调用或工具调用中途，SDK 记为 UNKNOWN 出站效果并保持阻塞（S2-08 既定语义）。重启类测试只用"Agent 已创建未提交"（`after_agent_created`）或"turn 已完成未采集"（`after_turn_committed`）。
- fixtures 脚本耗尽也会变成 UNKNOWN 出站调用 → turn 阻塞到 stall。两个 Mission 共用一个 scripted provider 时，Planner 与每个 Attempt 的脚本都要按 Mission 数配足；新增 Manager 触发点时，旧测试要么配 `manager_steps`，要么调高 `manager_after_failures`。
- ruff format 会重排刚写的代码，后续补丁的精确锚点可能失配；补丁脚本要容错并报告未命中。
- 测试模块文件名在各 step 目录间必须唯一（例如 `graph_helpers.py` 在第 6 步用 `graph_helpers6.py`）。
- DeepSeek 官方端点的模型 id 是 `deepseek-flash`（请求 `deepseek-v4-flash` 会回显不一致而停止）；flash 推理吃输出上限，真实测试给 `default_max_output_tokens=8192`、`max_output_tokens_ceiling=32768`；`provider_protocol_error/tool_parse` 偶发，按重试路径处理。
- 在记忆目录等非仓库目录下用系统 `python3 -` 处理中文脚本会报编码错，改用 SDK venv 的 python 并声明 utf-8。

## 5. 真实模型测试（opt-in）

用户指示：**真实模型一律 deepseek-flash，不用 deepseek-v4-pro**。凭证在 Host 仓库 `.local-test-evidence/2026-09-07/credentials/deepseek.env`（不要打印、不要提交）。做法：在一次性脚本里 `source` 该文件，导出 `SH_BASEURL` / `SH_APIKEY` / `SH_MODEL=deepseek-flash`，运行后对输出做密钥脱敏，归档前确认报告里没有密钥样式的字符串。

```bash
SH_MODEL=deepseek-flash .venv/bin/python -m pytest -q -p no:cacheprovider --run-real-provider tests/orchestrator -k real_provider
```

## 6. 重建并验证 wheel

wheel 与验证目录都放在会话临时目录里，不在仓库中；需要时按下面的方式在干净工作区从已提交 HEAD 重建（可复现）：

```bash
git archive --format=tar HEAD | tar -x -C "$BUILD_DIR/src" && cd "$BUILD_DIR/src" && SOURCE_DATE_EPOCH=$(git -C "$REPO" log -1 --format=%ct) uv build --wheel --out-dir "$BUILD_DIR/dist"
```

然后 `uv venv --python 3.12` 建干净环境、`uv pip install` 装 wheel 与 pytest、tiktoken，在解出的源码树里跑 `tests/orchestrator tests/agents tests/unit/contracts` 与迁移测试，再跑 `python -m agent_orchestrator demo --scenario multi-mission --provider fixtures --evidence-dir <dir>`。唯一允许的失败是基线已知的 `test_execution_v3_to_v4_migration::test_completed_null_continuation_*`。

## 7. 回归口径

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests --continue-on-collection-errors --ignore=tests/integration/runtime/test_context_use_admission.py --ignore=tests/integration/runtime/test_context_use_durable.py --ignore=tests/integration/runtime/test_context_use_public_memory.py
```

失败与错误的集合必须是 `plans/2026-09-10-base-agent-phase1/baseline-known-failures.txt`（73 条）的子集。

## 8. 遗留（按归属）

- 第 7 步：`run_tests` 网络隔离（L2-4、L6-5）；短期 Capability Token；UNKNOWN 出站效果的对账（L3-3、L6-4，目前只做到可见）。
- 第 8 步：DeepSeek 价目注入（L2-6、L6-2，目前 unpriced）；合并重复候选、角色配比调度、多样性配额（L6-3）；新思路数、剪枝率、重复率、误报率、污染率等指标（L6-8）。
- 只用 flash 带来的限制：两个执行池同模型名，"切换到另一个模型名"只由 fixtures 证明（L6-1）。
- 其余登记项：各步 `journal.md` §5（L2-*、L3-*、L4-*、第 5 步遗留、L6-1～L6-9）。
