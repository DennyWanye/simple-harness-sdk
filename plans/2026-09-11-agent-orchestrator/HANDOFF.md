# Agent 编排框架 · 交接说明（截至第 7 步交付）

- 日期：2026-09-11
- 仓库：`simple-harness-sdk`，分支 `main`；第 7 步代码与 wheel 源提交 `418a6d7`，之后只有文档提交（已推送 origin/main，本地干净，只有主工作树）
- 版本：simple_harness **0.9.5** / agent_orchestrator **0.7.0**（同一个 wheel）
- 纲要：Host 仓库 `simple_harness/plans/taskSys2/agent-orchestrator-incremental-build-plan-phase2-zh-CN.md`（ORCH-BUILD-v1.0，第 2–9 步）；原文 `agent-orchestration-layer-complete-design.md`；术语 `agent-orchestration-theory/`
- 本仓库的总表：`plans/2026-09-11-agent-orchestrator/program.md`；每步 `stepNN/{plan,acceptance,journal}.md` 与 `reports/`；测试归档 `testcase/agent-orchestrator-stepNN/`

## 1. 现在做到哪里

| 步 | 功能 | 状态 | wheel 源提交 |
|---|---|---|---|
| 2 | 单 Task Mission 可靠验收闭环 | SHIPPED 0.9.0 | `bd308dd` |
| 3 | Planner 拆解并并行执行静态 DAG | SHIPPED 0.9.1 | `d919ba5` |
| 4 | 团队共享知识、冲突仲裁、综合 | SHIPPED 0.9.2 | `57e6368` |
| 5 | 按 Worker 返回动态修改 Task DAG | SHIPPED 0.9.3 | `399d0e7` |
| 6 | 多 Mission、多模型、背压、隔离 | SHIPPED 0.9.4 | `a2ce656` |
| 7 | Human-in-the-loop 与受控真实操作 | SHIPPED 0.9.5 | `418a6d7`（sha256 `a1c4061067545a34f2f3815b0fa1b84c4c8b9245a0b48bf3f9757326c14c6640`） |
| 8 | 归因、Replay、策略对照评测 | **下一步** | — |
| 9 | 从历史学习并受控晋级 | 待做 | — |

第 7 步最终证据：`tests/orchestrator` 248 passed / 6 skipped（skip 的是真实模型 opt-in 测试）；SDK 全量回归红集 73 条 = 基线、0 新红；wheel 在干净 venv 安装后 511 passed / 1 failed（基线已知红），两个演示退出码 0；真实 deepseek-flash 运行 1、2 都完成（候选 → L2 审批 → 交接一次 → 回执核对）。细节见 `step07/journal.md` §3、§4、§6。

## 2. 下一步（第 8 步）怎么开始

1. 读纲要 §10（第 8 步：S8-01…S8-07）与原文 §23、§28 第四阶段；先在 `agent-orchestration-theory/` 查 Replay、Evaluation、Ablation、Lineage / 贡献归因等术语定义，定义与原文不一致以原文为准并在 plan 里注明。要点：Replay 是用旧 Event 重建既有事实，不调用真实外部工具、不重复收费；新策略重跑旧任务叫 Evaluation，是新成本、不覆盖旧 Mission 事实；消融不能关闭权限 / 幂等等安全边界去执行真实副作用。
2. 建 `plans/2026-09-11-agent-orchestrator/step08/{plan,acceptance,journal}.md`，照前几步格式；验收按 S8 条目写成可判定形式。
3. 独立 plan review（子代理，model 用 opus，只读）→ 在 plan §6 与 journal §1 逐条处置。**评审结论出来前只写测试草稿，不写实现**（第 7 步违反过一次，已记入其 journal）。
4. 测试先行、按切片实现并各自提交；每个切片跑 `tests/orchestrator` 全套（用带超时的 python 子进程包起来）。
5. 独立代码 review → 修复并配决定性测试 → 全量回归 → 重建 wheel → 真实模型证据 → journal → program.md 标 SHIPPED → HANDOFF → 推送 origin main → `git status` 干净、`git worktree list` 只有主工作树。
6. 第 7 步已为第 8 步留好的东西：
   - 事件与账本：`actions` / `approvals` / `approval_decisions` / `human_overrides` 四张表与 `ActionProposed`、`ApprovalRequested/Granted/Rejected/Revoked/Expired/Superseded/Cancelled`、`ActionHandedOff/Succeeded/Failed/OutcomeUnknown/Reconciled`、`HumanOverride`、`HumanCommentAdded`、`VerificationSuspended`、`MissionCriteriaJudged` 事件——Replay 要能重建它们，且**绝不调用连接器**。
   - `trace.json` 已有 `actions` 链（决定回执 → 交接 → 服务回执），`metrics.json` 已有 `human` / `actions` 两节，可直接进归因与评测报告。
   - 版本冻结：Attempt 的 prompt / context / retrieval / allocator / router / verifier 版本已记在 trace（第 6 步），第 8 步在 `governance/policies.py` 做实验基线的冻结。
   - 真实价目仍未注入（unpriced），评测报告里的费用列按"未定价"如实写，不写 0。

## 3. 不能破坏的不变量

- 编排库 `orchestrator.db` 只有 Commit Service 写（含第 7 步的四张表；动作与审批写在 `orchestrator/action_commits.py`、`orchestrator/human_commits.py` 两个 mixin 里）；SDK 执行库只有 SDK 写。
- §25 状态机不加回边；执行中的任务要改合同只能 supersede 成新实体；已完成任务只被引用、永不重跑；接管不能复活终态 Task / Mission。
- 预算两层不混；unpriced 金额记 null 不写 0；UNKNOWN 费用与 UNKNOWN 动作都让预留保持占用（`ReservationHeld`）。
- 真实动作（第 7 步）：
  - 模型只写候选（`actions/<name>.json`，必须在 Task outputs 里声明、在 Mission 的 `action:` 准则范围内）；连接器只由 `runtime/actions.py` 的执行器调用，执行器不写库。
  - 交接只在 Mission 判定的最后一步，且同一 Mission 的所有动作都就绪才开始；`begin_handoff` 在一个事务里再校验绑定、重算参数哈希、预留预算、写 HANDED_OFF（owner + 租约 + 决定回执）。
  - 已交接 / 已执行的版本永不 SUPERSEDED；UNKNOWN 只经按幂等键核对或人工带证据裁决离开，重交接只用同一幂等键、至多一次。
  - 同一审批人对 L3 的第二次批准直接拒绝（部署约定 `l3_distinct_principals=True`）；记下的每条批准都计数。
  - 恢复旧库不能撤销现实：旧库重放同一幂等键，服务去重，账本凭原回执核对为 SUCCEEDED。
  - 部署默认不启用任何连接器（`DeploymentPolicy.enabled_connectors=()`）。
- 人工（第 7 步）：审批 / 审核 / 仲裁 / 接管的身份只来自 API 调用方的 `Principal`（CLI `--as`），不来自模型参数；人工文本做密钥检查；人工 PASS 不能掩盖失败的测试（needs_human 不短路）。
- `Store.transaction()` 不能跨 await；密钥不进模型上下文、不进证据。

## 4. 已知陷阱

- 崩溃点落在 provider 调用或工具调用中途，SDK 记为 UNKNOWN 出站效果并保持阻塞；重启类测试只用 `after_agent_created` / `after_turn_committed`。
- fixtures 脚本耗尽会变成 UNKNOWN 出站调用 → turn 阻塞到 stall；多 Mission 共用 scripted provider 时脚本按 Mission 数配足；Critic 格式错误会被重试 `MAX_CRITIC_ATTEMPTS=2` 次，要配够。
- 带 `action:` 准则的 Mission 要在 `Orchestrator(connectors=...)` 传入连接器，且部署 `enabled_connectors` 包含它，否则提交时就被拒。
- 事件按 `(type, key)` 幂等去重：数事件次数证明不了"没有重复发生"，要数真实调用（pytest、Critic、连接器）。
- 判定结果按集成树键缓存在 `MissionCriteriaJudged` 里；等待人工期间不会重跑 pytest / Critic。
- `final_state.json` 里 Task 的主键字段是 `id`，不是 `task_id`。
- ruff format 会重排刚写的代码，补丁锚点可能失配，补丁脚本要容错并报告未命中；测试模块文件名在各 step 目录间必须唯一；单独跑某一步的测试时，别的 step 目录不在 `sys.path` 里。
- DeepSeek 官方端点模型 id 是 `deepseek-flash`；真实测试给 `default_max_output_tokens=8192`、`max_output_tokens_ceiling=32768`。
- 非仓库目录下用系统 `python3 -` 处理中文会报编码错，改用 SDK venv 的 python 并设 `LC_ALL=en_US.UTF-8`；macOS 没有 `timeout`，用 python 子进程加超时；zsh 下 `grep --include=*.py` 会被通配符展开打断，改用目录参数。

## 5. 真实模型测试（opt-in）

用户指示：**真实模型一律 deepseek-flash，不用 deepseek-v4-pro**。凭证在 Host 仓库 `.local-test-evidence/2026-09-07/credentials/deepseek.env`（不要打印、不要提交）。做法：一次性脚本里 `source` 该文件，导出 `SH_BASEURL` / `SH_APIKEY` / `SH_MODEL=deepseek-flash`，运行输出在进程内把密钥值替换为 `<redacted>` 并把 `sk-…` 替换掉；归档前用真实密钥逐字节扫描证据目录（只打印命中次数）。第 7 步的真实测试：`--run-real-provider tests/orchestrator/step07/test_real_provider_approval.py`。

## 6. 重建并验证 wheel

在会话临时目录里从已提交 HEAD 可复现构建（工作树必须干净）：

```bash
git archive --format=tar HEAD | tar -x -C "$BUILD_DIR/src" && cd "$BUILD_DIR/src" && SOURCE_DATE_EPOCH=$(git -C "$REPO" log -1 --format=%ct) uv build --wheel --out-dir "$BUILD_DIR/dist"
```

然后 `uv venv --python 3.12` 建干净环境、装 wheel 与 pytest、tiktoken，在解出的源码树里跑 `tests/orchestrator tests/agents tests/unit/contracts` 与迁移测试，再跑 `demo --scenario multi-mission` 与 `demo --scenario approval-action`（fixtures）。唯一允许的失败是基线已知的 `test_execution_v3_to_v4_migration::test_completed_null_continuation_*`。

## 7. 回归口径

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests --continue-on-collection-errors --ignore=tests/integration/runtime/test_context_use_admission.py --ignore=tests/integration/runtime/test_context_use_durable.py --ignore=tests/integration/runtime/test_context_use_public_memory.py
```

失败与错误的集合必须是 `plans/2026-09-10-base-agent-phase1/baseline-known-failures.txt`（73 条）的子集。

## 8. 遗留（按归属）

- 第 7 步登记、未做：
  - `run_tests` 网络隔离（L2-4、L6-5）；SDK 工具出站 UNKNOWN 的对账（L3-3、L6-4，不在编排可依赖的公共面，目前只做到可见）。
  - 补偿动作 / 回滚真实世界（纲要 §12.6）；独立的短期 Capability Token 格式（批准本身即绑定版本、有有效期的一次性能力）。
  - 连接器调用在判定循环内 await（有超时），同一轮其他 Mission 要等（代码评审 P2-9）；交接前没有复核候选 artifact 字节（只重算参数哈希）。
  - CLI `--as` 是自报身份，真实部署要接认证。
- 第 8 步：DeepSeek 价目注入（L2-6、L6-2，目前 unpriced）；合并重复候选、角色配比调度、多样性配额（L6-3）；新思路数、剪枝率、重复率、误报率、污染率等指标（L6-8）。
- 只用 flash 带来的限制：两个执行池同模型名，"切换到另一个模型名"只由 fixtures 证明（L6-1）。
- 其余登记项：各步 `journal.md` 的遗留节。
