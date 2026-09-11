# Agent 编排框架 · 交接说明（第 2–9 步全部交付）

- 日期：2026-09-11
- 仓库：`simple-harness-sdk`，分支 `main`；第 9 步代码与 wheel 源提交 `88e5582`，之后只有文档提交（已推送 origin/main，本地干净，只有主工作树）
- 版本：simple_harness **0.9.7** / agent_orchestrator **0.9.0**（同一个 wheel）
- 纲要：Host 仓库 `simple_harness/plans/taskSys2/agent-orchestrator-incremental-build-plan-phase2-zh-CN.md`（ORCH-BUILD-v1.0，第 2–9 步）；原文 `agent-orchestration-layer-complete-design.md`；术语 `agent-orchestration-theory/`
- 本仓库的总表：`plans/2026-09-11-agent-orchestrator/program.md`；每步 `stepNN/{plan,acceptance,journal}.md` 与 `reports/`；测试归档 `testcase/agent-orchestrator-stepNN/`

## 1. 现在做到哪里

ORCH-BUILD-v1.0 的第 2–9 步全部 SHIPPED——纲要 §11 写的"第 9 步结束覆盖原文完整建设目标"已达到（真实效果是否提高由评测报告决定，见各步 journal §3）。

| 步 | 功能 | 状态 | wheel 源提交 |
|---|---|---|---|
| 2 | 单 Task Mission 可靠验收闭环 | SHIPPED 0.9.0 | `bd308dd` |
| 3 | Planner 拆解并并行执行静态 DAG | SHIPPED 0.9.1 | `d919ba5` |
| 4 | 团队共享知识、冲突仲裁、综合 | SHIPPED 0.9.2 | `57e6368` |
| 5 | 按 Worker 返回动态修改 Task DAG | SHIPPED 0.9.3 | `399d0e7` |
| 6 | 多 Mission、多模型、背压、隔离 | SHIPPED 0.9.4 | `a2ce656` |
| 7 | Human-in-the-loop 与受控真实操作 | SHIPPED 0.9.5 | `418a6d7` |
| 8 | 归因、Replay、策略对照评测 | SHIPPED 0.9.6 | `a094f46` |
| 9 | 从历史学习并受控晋级 | SHIPPED 0.9.7 | `88e5582`（sha256 `291c824deeaf70c5c535413f3b2cb405c615e8654c216739eb1e1940ea4c06d2`） |

第 9 步最终证据：step09 全部通过；SDK 全量回归红集 73 = 基线、0 新红；wheel 在干净 venv（Python 3.12）安装后 626 passed / 1 failed（基线已知的 `test_execution_v3_to_v4_migration::test_completed_null_continuation_*`）/ 11 skipped，multi-mission、approval-action、evaluate-policies、policy-promotion 四个演示与 `replay --attribution`、`policy status` 退出码都是 0；真实 deepseek-flash 门槛评测如实给出 INSUFFICIENT（每一方样本 2 < 3）。细节见 `step09/journal.md` §1–§6。

## 2. 之后可以做什么（不在 ORCH-BUILD-v1.0 范围内）

1. **Host 产品接线**：把 `agent_orchestrator` 接进 Host（`simple_harness` 桌面工作台）——Mission 创建、审批、策略状态的 UI；这是第一阶段 BaseAgent 之后原定的"Host 产品接线"方向。
2. **各步遗留**（`stepNN/journal.md` §5 汇总；要点见 §8）：学习型候选（需要足够真实数据）、价目注入、`run_tests` 网络 / 文件系统隔离、真实认证替代 CLI `--as`、补偿动作。
3. 新工作照前几步的编排：建 plan / acceptance / journal → 独立 plan review（opus，只读）→ 评审结论前只写测试草稿 → 测试先行、按切片实现并各自提交 → 独立代码 review（沿用 pytest 插件在全部测试的每个 Mission 上做回放 / 绑定 / 对账扫描）→ 全量回归 → wheel → 真实模型证据（只用 deepseek-flash）→ SHIPPED → 推送 → 清理。

## 3. 不能破坏的不变量

- 编排库 `orchestrator.db` 只有 Commit Service 写（动作 / 审批在 `action_commits.py`、`human_commits.py`，策略版本库在 `policy_commits.py` 三个 mixin）；SDK 执行库只有 SDK 写；Replay / 归因 / 评测 / 规则改进器只读（库复制到临时目录后 `Store.open_readonly`）。
- §25 状态机不加回边；执行中的任务要改合同只能 supersede；已完成任务只被引用、永不重跑；接管不能复活终态 Task / Mission。
- 预算两层不混；unpriced 金额记 null 不写 0；UNKNOWN 费用与 UNKNOWN 动作都让预留保持占用。
- 真实动作：模型只写候选；连接器只由执行器调用；已交接 / 已执行版本永不 SUPERSEDED；UNKNOWN 只核对不重发；部署默认不启用连接器。
- 人工身份只来自 API 调用方的 `Principal`；人工 PASS 不能掩盖失败的测试。
- 消融只关封闭词表里的层，安全边界永远不能关；有效政策为空判 ERROR。
- 评测不覆盖旧 Mission 事实（新幂等键、新目录、新库）；小样本比较写"证据不足"。
- **策略（第 9 步）**：策略只含白名单参数、一律展开存储、内容寻址；安全边界 / 预算 / 部署政策 / 消融 / 超时 / 层开关永远不能进策略。每个 Mission 在创建事务里绑定一个版本并按它运行到底——晋级、回滚、换配置都不改在途 Mission；新增会读"可调参数"的决策点时，必须从 `Orchestrator.policy_for(mission_id)` 取，不能读 `self._config` 或模块常量（结构测试 `test_no_decision_point_reads_*` 会拦）。晋级只对 APPROVED、批准绑定最新评测、基线 = ACTIVE、代码版本一致、fixture 证据须显式接受、有幅度 / 冷却 / 背压限制；回滚只回 RETIRED 版本、即时。在线 Agent 不能改策略或核心规则（夹带即 `PolicySuggestionRefused`）。评测库（`library_role=evaluation`）只接受钉版，正式库拒绝钉版。
- `Store.transaction()` 不能跨 await；密钥不进模型上下文、不进证据。

## 4. 已知陷阱

- 崩溃点落在 provider / 工具调用中途 = SDK UNKNOWN 并阻塞；重启类测试用 `after_agent_created` / `after_turn_committed`。
- fixtures 脚本耗尽 → UNKNOWN → 挂起：每次试验新建 provider、脚本按次数配足；多个 Attempt 并发时用 `TaskRoutedProvider(per_attempt=…)` 或让 Mission 依次运行，否则脚本步骤交错。
- 事件按 `(type, key)` 幂等去重：数事件证明不了没有重复发生；会重复发生的事件，key 里要带唯一的东西（激活序号、评测 id、回执哈希、时间）。
- 回放的投影规则要与每个改正式状态的写入一一对应（第 8 步漏过 `ResultRejected` / `AttemptCreated`，第 9 步漏过 legacy 绑定的覆盖率口径）；新增事件同步改 `observability/replay.py` 并跑回放扫描。
- `sk-[A-Za-z0-9_-]{20,}` 会命中库里的 `task-…`；扫描与脱敏用 `\bsk-`，并用真实密钥逐字节比对（只打印次数）。
- 后台跑全量回归时，不要往 `tests/` 放依赖未实现代码的新测试文件（收集阶段导入失败会中断整轮），也不要改源码与版本号（子进程测试读磁盘上的源码，契约测试读版本号）。
- ruff format 会重排刚写的代码，补丁锚点可能失配，补丁脚本要容错并报告未命中；zsh 下变量不按空格拆词（用数组），`echo ===X` 要加引号；非仓库目录处理中文用 SDK venv 的 python 并设 `LC_ALL=en_US.UTF-8`；macOS 没有 `timeout`，用 python 子进程加超时。
- DeepSeek 官方端点模型 id 是 `deepseek-flash`；真实测试给 `default_max_output_tokens=8192`、`max_output_tokens_ceiling=32768`；同一 case 同一策略在两次真实评测间耗时 / tokens 可差一倍；真实模型偶尔连续交空结果触发 no_progress。

## 5. 真实模型测试（opt-in）

用户指示：**真实模型一律 deepseek-flash，不用 deepseek-v4-pro**。凭证在 Host 仓库 `.local-test-evidence/2026-09-07/credentials/deepseek.env`（不要打印、不要提交）。一次性脚本里 `source` 该文件，导出 `SH_BASEURL` / `SH_APIKEY` / `SH_MODEL=deepseek-flash`，运行输出在进程内脱敏；归档前用真实密钥逐字节扫描证据目录（只打印命中次数）。第 9 步：`--run-real-provider tests/orchestrator/step09/test_real_provider_policy.py`。

## 6. 重建并验证 wheel

在会话临时目录里从已提交 HEAD 可复现构建（工作树必须干净）：

```bash
git archive --format=tar HEAD | tar -x -C "$BUILD_DIR/src" && cd "$BUILD_DIR/src" && SOURCE_DATE_EPOCH=$(git -C "$REPO" log -1 --format=%ct) uv build --wheel --out-dir "$BUILD_DIR/dist"
```

然后 `uv venv --python 3.12` 建干净环境、装 wheel 与 pytest、tiktoken，在解出的源码树里跑 `tests/orchestrator tests/agents tests/unit/contracts` 与迁移测试，再跑 `demo --scenario multi-mission`、`approval-action`、`evaluate-policies`、`policy-promotion`（fixtures）、一次 `replay --attribution` 与 `policy status`。唯一允许的失败是基线已知的 `test_execution_v3_to_v4_migration::test_completed_null_continuation_*`。

## 7. 回归口径

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests --continue-on-collection-errors --ignore=tests/integration/runtime/test_context_use_admission.py --ignore=tests/integration/runtime/test_context_use_durable.py --ignore=tests/integration/runtime/test_context_use_public_memory.py
```

失败与错误的集合必须是 `plans/2026-09-10-base-agent-phase1/baseline-known-failures.txt`（73 条）的子集。

## 8. 遗留（按归属）

- 第 9 步（`step09/journal.md` §5）：学习型候选未做（偏离，如实登记）；Worker 策略文件检测的覆盖面；CLI nonce 自动生成与人工提议不带部署信息；R2 样本独立性；角色配比未做成可晋级参数；Retrieval 版本运行时切换、新 Verifier 重判旧产物、`snapshot_diff` 字段级来源、回放更宽场景、派生 case 的 provider 工厂。
- 第 7 步：`run_tests` 网络 / 文件系统隔离（L2-4、L6-5）；SDK 工具出站 UNKNOWN 的对账（L3-3、L6-4）；补偿动作 / 回滚真实世界；独立的短期 Capability Token；连接器调用在判定循环内 await；CLI `--as` 是自报身份。
- 价目：DeepSeek 价目未注入（L2-6、L6-2），真实金额为 null。
- 只用 flash：两个执行池同模型名、路由类候选的"换模型"只由 fixtures 证明（L6-1）。
- 其余登记项：各步 `journal.md` 的遗留节。
