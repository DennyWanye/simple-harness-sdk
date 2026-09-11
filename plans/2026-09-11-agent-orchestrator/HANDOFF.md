# Agent 编排框架 · 交接说明（截至第 8 步交付）

- 日期：2026-09-11
- 仓库：`simple-harness-sdk`，分支 `main`；第 8 步代码与 wheel 源提交 `a094f46`，之后只有文档提交（已推送 origin/main，本地干净，只有主工作树）
- 版本：simple_harness **0.9.6** / agent_orchestrator **0.8.0**（同一个 wheel）
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
| 7 | Human-in-the-loop 与受控真实操作 | SHIPPED 0.9.5 | `418a6d7` |
| 8 | 归因、Replay、策略对照评测 | SHIPPED 0.9.6 | `a094f46`（sha256 `a1bc14331fed3bbbdf2201f2a31b1a3ab547c3d55725e3a81596c0feef2e2e6f`） |
| 9 | 从历史学习并受控晋级 | **下一步** | — |

第 8 步最终证据：step08 73 passed / 1 skipped（skip 的是真实模型 opt-in）；SDK 全量回归红集 73 条 = 基线、0 新红；wheel 在干净 venv 安装后 584 passed / 1 failed（基线已知红）/ 10 skipped，multi-mission、approval-action、evaluate-policies 三个演示与一次 `replay --attribution` 退出码 0；真实 deepseek-flash 评测运行 1、2 都完成（`parse-kv` × 完整政策 / 去掉 Critic × 2 次，4/4 成功、oracle 误判 0/4，比较结论"证据不足"）。细节见 `step08/journal.md` §1–§4、§6。

## 2. 下一步（第 9 步）怎么开始

1. 读纲要 §11（第 9 步：S9-01…S9-08；拟交付 `demo --scenario policy-promotion` 与 `policy propose/evaluate/approve/promote/rollback`，测试目录 `tests/orchestrator/step09/`）与原文 §28 第四阶段、§29.3、§23。`agent-orchestration-theory/` 里没有 shadow / 晋级的专门定义（只有 13 §12 的 Rollback 与 Compensation，那是针对动作的），这些术语以原文为准并在 plan 里注明；Allocator、Model Router、Offline Evaluation、A/B、Ablation 等仍先查理论定义。
2. 纲要要点：执行顺序"收集 Trace → 离线训练或规则改进 → 新策略版本 → Offline Evaluation → A/B → 审批后上线"；历史不足时返回"候选未达到晋级条件 / 缺少足够样本"是正常结果；fixture 只证明机制，不能宣称质量提升；不能把手工调权重冒充已训练的 Model Router；学习器只排序合法 Frontier，不改预算与权限资格；在线 Agent 不能直接改核心调度或安全规则；训练 / 评测集按时间与任务隔离（同一任务的相邻 Attempt 不能一个进训练一个进测试）。
3. 建 `plans/2026-09-11-agent-orchestrator/step09/{plan,acceptance,journal}.md`，照前几步格式；独立 plan review（opus，只读）→ plan §6 与 journal §1 逐条处置；**评审结论出来前只写测试草稿，不写实现**。
4. 测试先行、按切片实现并各自提交；每个切片跑 `tests/orchestrator` 相关目录（带超时的 python 子进程）。
5. 独立代码 review → 修复并配决定性测试（第 8 步做了两轮：第 2 轮用 pytest 插件在全部测试的每个 Mission 上做回放 / 对账扫描，发现了第 1 轮漏掉的投影漏项，值得沿用）→ 全量回归 → 重建 wheel → 真实模型证据 → journal → program.md 标 SHIPPED → HANDOFF → 推送 origin main → `git status` 干净、`git worktree list` 只有主工作树。
6. 第 8 步已为第 9 步留好的东西：
   - `observability/evaluation.py`：`EvaluationPlan`（cases × strategies × trials、每次运行新 Mission / 新目录 / 新库、策略覆盖白名单 `STRATEGY_OVERRIDES`、测试服务只许本类且在运行目录下）、`run_plan` 报告（Wilson、逐 case 成对 Fisher、脚手架错误不对称降级、oracle 误判率、消融连带影响）、`case_from_evidence`（只接受本版本演示证据、spec 哈希校验）——第 9 步的"候选评测"直接复用，门槛（质量 / 成本 / 风险）加在报告之上。
   - `governance/policies.py`：`policy_snapshot`（全配置字段归类 + 版本常量来源 + provider 身份，无密钥）与 `snapshot_diff`——第 9 步的策略版本库以快照哈希为版本身份，"新 Mission 绑定新版本、在途 Mission 不变"可用 baseline 里的开始快照核对。
   - `observability/traces.py`：归因（成功路径、探索消耗、按角色 / 模型 / prompt 版本的费用、`cost.ledger` 对账）——第 9 步"角色和 Prompt 可靠性 / 贡献统计"的数据源。
   - `observability/replay.py`：回放（纯折叠、只读副本、结构性不变量）——回滚后"既有事件和费用仍保留"可用回放核对。
   - 消融 `OrchestratorConfig.ablations`（critic / blackboard / graph_changes）与安全边界清单 `SAFETY_BOUNDARIES`——第 9 步 S9-06"在线 Agent 提出改安全阈值"的拒绝可以沿用这张清单。

## 3. 不能破坏的不变量

- 编排库 `orchestrator.db` 只有 Commit Service 写（动作与审批在 `orchestrator/action_commits.py`、`orchestrator/human_commits.py` 两个 mixin）；SDK 执行库只有 SDK 写；Replay / 归因 / 评测只读（库复制到临时目录后 `Store.open_readonly`）。
- §25 状态机不加回边；执行中的任务要改合同只能 supersede 成新实体；已完成任务只被引用、永不重跑；接管不能复活终态 Task / Mission。
- 预算两层不混；unpriced 金额记 null 不写 0；UNKNOWN 费用与 UNKNOWN 动作都让预留保持占用（`ReservationHeld`）。
- 真实动作：模型只写候选；连接器只由执行器调用；已交接 / 已执行的版本永不 SUPERSEDED；UNKNOWN 只核对不重发；部署默认不启用任何连接器；L3 同一人第二次批准直接拒。
- 人工身份只来自 API 调用方的 `Principal`（CLI `--as`）；人工 PASS 不能掩盖失败的测试。
- 消融只能关闭封闭词表里的层，安全边界（权限、网关、幂等、审批、部署政策、密钥检查、format / rule / code_test、human_review、预算）永远不能关；消融使 Task 有效政策为空时判 ERROR，不会零层 PASS。
- 评测不覆盖旧 Mission 事实：每次运行新幂等键 `eval:<plan>:<strategy>:<case>:<trial>`、新目录、新库；小样本比较一律写"证据不足"。
- `Store.transaction()` 不能跨 await；密钥不进模型上下文、不进证据。

## 4. 已知陷阱

- 崩溃点落在 provider 调用或工具调用中途，SDK 记为 UNKNOWN 出站效果并保持阻塞；重启类测试只用 `after_agent_created` / `after_turn_committed`。
- fixtures 脚本耗尽会变成 UNKNOWN 出站调用 → turn 阻塞到 stall；评测里每次试验必须新建 provider，脚本按次数配足；验证 ERROR 后若还能重试就需要下一份 Worker 脚本（测试里把 Task 的 `max_attempts` 设成 1 可避免挂起）。
- 事件按 `(type, key)` 幂等去重：数事件次数证明不了"没有重复发生"，要数真实调用（pytest、Critic、连接器）。
- 回放的投影规则要和 Commit Service 的每个状态写入一一对应：第 8 步复核就漏了 `ResultRejected`（Attempt → RETRY_WAIT）与 `AttemptCreated`（Task → ACTIVE）。新增会改正式状态的事件时，同步改 `observability/replay.py` 并跑回放扫描。
- `sk-[A-Za-z0-9_-]{20,}` 会命中库里的 `task-…` 标识符；密钥扫描与脱敏用 `\bsk-`，并用真实密钥逐字节比对（只打印次数）。
- `final_state.json` 里 Task 的主键字段是 `id`；测试模块文件名在各 step 目录间必须唯一；单独跑某一步的测试时，别的 step 目录不在 `sys.path` 里。
- ruff format 会重排刚写的代码，补丁锚点可能失配；zsh 下变量不按空格拆词（多个文件用数组 `S=(a b)` 再 `$S`），`echo ===X` 会被当成等号展开，要加引号；`grep --include=*.py` 会被通配符展开打断。
- 非仓库目录下用系统 `python3 -` 处理中文会报编码错，用 SDK venv 的 python 并设 `LC_ALL=en_US.UTF-8`；macOS 没有 `timeout`，用 python 子进程加超时。
- DeepSeek 官方端点模型 id 是 `deepseek-flash`；真实测试给 `default_max_output_tokens=8192`、`max_output_tokens_ceiling=32768`；同一 case 同一策略在两次真实评测间耗时 / tokens 可差一倍。

## 5. 真实模型测试（opt-in）

用户指示：**真实模型一律 deepseek-flash，不用 deepseek-v4-pro**。凭证在 Host 仓库 `.local-test-evidence/2026-09-07/credentials/deepseek.env`（不要打印、不要提交）。做法：一次性脚本里 `source` 该文件，导出 `SH_BASEURL` / `SH_APIKEY` / `SH_MODEL=deepseek-flash`，运行输出在进程内把密钥值替换为 `<redacted>` 并把 `\bsk-…` 替换掉；归档前用真实密钥逐字节扫描证据目录（只打印命中次数）。第 8 步的真实测试：`--run-real-provider tests/orchestrator/step08/test_real_provider_evaluation.py`。

## 6. 重建并验证 wheel

在会话临时目录里从已提交 HEAD 可复现构建（工作树必须干净）：

```bash
git archive --format=tar HEAD | tar -x -C "$BUILD_DIR/src" && cd "$BUILD_DIR/src" && SOURCE_DATE_EPOCH=$(git -C "$REPO" log -1 --format=%ct) uv build --wheel --out-dir "$BUILD_DIR/dist"
```

然后 `uv venv --python 3.12` 建干净环境、装 wheel 与 pytest、tiktoken，在解出的源码树里跑 `tests/orchestrator tests/agents tests/unit/contracts` 与迁移测试，再跑 `demo --scenario multi-mission`、`approval-action`、`evaluate-policies`（fixtures）与一次 `replay --attribution`。唯一允许的失败是基线已知的 `test_execution_v3_to_v4_migration::test_completed_null_continuation_*`。

## 7. 回归口径

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests --continue-on-collection-errors --ignore=tests/integration/runtime/test_context_use_admission.py --ignore=tests/integration/runtime/test_context_use_durable.py --ignore=tests/integration/runtime/test_context_use_public_memory.py
```

失败与错误的集合必须是 `plans/2026-09-10-base-agent-phase1/baseline-known-failures.txt`（73 条）的子集。

## 8. 遗留（按归属）

- 第 8 步登记、未做（`step08/journal.md` §5）：provider 身份补 `OpenAICompatibleProvider` 的端点主机与目标模型、fixtures 脚本摘要；`snapshot_diff` 对 profiles / routing / connectors / provider 给字段级来源；回放覆盖场景扩到候选被取代、`KnowledgeSuperseded`、冲突 DEFERRED / UNRESOLVED、仲裁 / 接管、审批撤销 / 过期、动作 FAILED / UNKNOWN / Reconciled；派生 case 按场景名指定 provider 工厂；运行时切换 Prompt / Allocator / Retrieval 版本、Allocator 消融、新 Verifier 重判旧产物——都在第 9 步"候选版本注册"时一并处理。
- 第 7 步登记、未做：`run_tests` 网络隔离（L2-4、L6-5）；SDK 工具出站 UNKNOWN 的对账（L3-3、L6-4）；补偿动作 / 回滚真实世界（纲要 §12.6）；独立的短期 Capability Token；连接器调用在判定循环内 await（代码评审 P2-9）；CLI `--as` 是自报身份。
- 价目：DeepSeek 价目未注入（L2-6、L6-2），真实评测金额为 null（未定价），不写 0。
- 只用 flash 带来的限制：两个执行池同模型名，"切换到另一个模型名"只由 fixtures 证明（L6-1）；第 9 步的模型路由候选同样只能用 fixtures 证明"换模型"。
- 其余登记项：各步 `journal.md` 的遗留节。
