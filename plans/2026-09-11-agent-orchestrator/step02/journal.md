# 第 2 步 · 执行记录

## 1. 关键裁决

独立 review（子代理 claude-opus-5，只读；原文见 `reports/plan-review-round1.md`）对 step02 plan 提出 27 条发现 + 6 条应显式化的决定。处置如下（修订落在 `plan.md` §7）：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | P0 | D6 续租规则把 provider/tool UNKNOWN 的 blocked 误判成失联 → 与 S2-08 矛盾；存活集合漏 result_pending | 采纳：D6'（阻塞=存活；LOST 三条件） |
| 2 | P0 | D7 的 Attempt FAILED 不存在于 §25.2 | 采纳：D7'（RETRY_WAIT + failure.reason） |
| 3 | P0 | `usage_refs` 只含末次 provider 请求 → 费用少算 | 采纳：D10'（新增 SDK 只读门面 `list_provider_invocations`，逐条导入；`read_provider_budget.has_unknown_charge` 作 UNKNOWN 判据的一部分） |
| 4 | P0 | intent 只冻 hash，重放会撞 `config_hash`/`AgentInputConflict` | 采纳：D5'（冻结完整 config 与 message JSON） |
| 5 | P0 | recover 漏 `recover_pending_turns()` | 采纳：D14' |
| 6 | P1 | 硬预算未下沉 | 采纳：D10'（runtime 统一 hard_cap + AgentConfig.limits） |
| 7 | P1 | estimator pricing_key 必须为 "consumer" | 采纳：D10' |
| 8 | P1 | ports.model 必须等于 response.model | 采纳：D10'（CLI 核对，fail-fast） |
| 9 | P1 | tool_schemas 缺失；per-Agent 收窄靠 config | 采纳：D13' |
| 10 | P1 | run_tests 子进程参数不可行 | 采纳：D13' |
| 11 | P1 | 只读副本与 pytest 写缓存冲突 | 采纳：D12' |
| 12 | P1 | Critic 的工作区映射缺失 | 采纳：D13'（run_id → (attempt, view, mode)） |
| 13 | P1 | Task 从 VERIFYING 停止/取消无路径 | 采纳：D15' |
| 14 | P1 | Critic 无预算归属 | 采纳：D22 |
| 15 | P1 | Mission 级成功判定被收窄成 Task PASS | 采纳：D21 |
| 16 | P1 | 版本关系未定义 | 采纳：D19 |
| 17 | P1 | 事件词表未固定 | 采纳：D20 |
| 18 | P1 | api 校验与 tenant 来源 | 采纳：D25 |
| 19 | P1 | fixture 多 Agent 不确定；缺 UNKNOWN 制造器 | 采纳：D26 |
| 20 | P1 | 跨库崩溃点未列 | 采纳：D14'（6 个钩子） |
| 21 | P1 | S2-03 幂等点少 4 个 | 采纳：写入 acceptance S2-03 |
| 22 | P1 | 打包收尾不完整；版本撞名 | 采纳：D24（0.9.0） |
| 23 | P1 | Planner/Critic 重试身份缺序号 | 采纳：D22 |
| 24 | P1 | result_id 双键规则未落地 | 采纳：D7' |
| 25 | P2 | create() 不校验 tool_names | 采纳：D13'（编排层断言） |
| 26 | P2 | 两套租约关系 | 采纳：D6' |
| 27 | P2 | Critic 排在 code_test 前烧钱 | 保持 §14.1 顺序（忠于原文），加必需层 FAIL 短路（D23） |

reviewer 要求显式化的 6 个决定：硬预算落地（D10'）、费用权威口径（D10'）、intent 冻结内容（D5'）、Mission 级判定（D21）、版本模型（D19）、合规收尾与版本号（D24）；第 7 项 result id 规则（D7'）。

## 2. 执行记录

| 切片 | 提交 | 内容 | 测试 |
|---|---|---|---|
| A 合同与存储 | `370198a` | §26 六合同 + §25 状态机（Task/Attempt/Claim + Mission 状态集）、`Store`（STRICT/WAL、CAS、幂等事件、故障注入点）、预算账本（三级账户、Reserve/Settle、usage 逐条导入、unpriced 不写零）、Commit Service（幂等创建、带回执的 Task 提案、Attempt+intent 原子创建、派发进度、结果接收/拒绝、验证层记录、接受/失败、停止、取消、Mission 级判定） | `test_contracts.py` 7、`test_store_and_budgets.py` 3、`test_commit_service.py` 5 |
| B+C 执行链与验证 | `650fc9e` | 隔离工作区 + 验收副本、工具网关（4 工具、run_id→(attempt,view,mode)、子进程 pytest 超时/进程组/env 白名单）、角色模板（planner/worker/critic，带 prompt_version）、Context Builder（§10 的 1/2/6/8/9/10/11）、BaseAgent 桥（冻结 config/message 重放、存活快照、费用事实）、验证四层 + 路由（§14.1 顺序、必需层短路、NOT_REQUIRED 不算 PASS）、编排循环（recover→派发→采集→验证→决策→判定）、fixture provider | `test_workspace_and_gateway.py` 3、`test_single_task_closure.py` 4 |
| D 恢复与幂等 | `c70c4d6` | 6 个跨库崩溃点（按 intent kind 定向）、恢复矩阵、CLI（mission/attempt/artifact/demo）与证据目录、包内 fixtures、真实模型 opt-in 测试、SDK 唯一新增只读门面 `list_provider_invocations` | `test_recovery_matrix.py` 8、`test_cli_demo.py` 3 |

实现中的裁决（补充 §1）：
- Critic 在 §14.1 顺序里先于 code_test 运行，因此 Critic 看不到本次验证的测试输出，只能读验收副本代码；Critic 包里 `test_output` 为 null 并写明。保持原文层序。
- Attempt 的 LOST 判定本步只实现"turn 不存在/agent 打不开"一条（D6' 三条件中的第 1、2 条）；"无 blocker 且 ordinal 长期无推进"的停滞判定留到第 3 步 S3-07（多执行者场景）一并做，见 §5 遗留。
- 业务租约到期后允许新 owner 接管（`renew_lease`/`claim_intent`），未到期的活租约不可抢占。

## 3. 独立 review（代码）

独立子代理（claude-opus-5，只读 + 4 个复现脚本）对 step 2 累计 diff 的 review，原文 `reports/code-review-round1.md`。处置（全部有决定性测试 `tests/orchestrator/step02/test_review_round1.py`，提交 `review round 1`）：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | P0 | Worker 可改写自己的验收测试（验收副本原样复制 Worker 树），得到假的 VERIFIED 交付 | 受保护种子文件：`pytest:` 目标与 `tests/` 下的种子文件在验收副本里从 Mission 种子重新落盘；`rule_check` 对被改写的受保护文件直接 FAIL；修复 Attempt 的工作区也强制恢复受保护文件。测试：作弊 Worker → rule_check FAIL、副本含真实测试、修复后通过 |
| 2 | P1 | 重启时另一 owner 的活租约让 `renew_lease` 抛异常打死循环 | `_observe_liveness` 捕获 `CommitRejected` 视为"还不是我的"，租约到期后接管；`claim_intent` 对 AGENT_CREATED 也检查活租约 |
| 3 | P1 | 重启后在途 turn 的工具网关绑定丢失 | `recover()` 先重绑 AGENT_CREATED/SUBMITTED 的 attempt/critic intent，再 `recover_pending_turns()` |
| 4 | P1 | D6' 停滞判定未实现，`stall_seconds` 是死配置 | Attempt 记 `progress_marker/progress_at`（续租时按 `provider_turn_ordinal_to` 更新）；存活、无 blocker、`stall_seconds` 内无推进 → `TIMED_OUT`（事件 `AttemptTimedOut`）+ 协作式 `cancel_turn` + 新 Attempt |
| 5 | P1 | `cancel_mission` 留下活 intent/预留/待验证结果；循环不按 Mission 过滤 | cancel 关闭全部未完成 intent（FAILED）、结算/释放预留、待验证结果标 REJECTED、发 `TaskCancelled/AttemptCancelled`；`_cycle` 与 `_has_inflight` 只看非终态 Mission |
| 6 | P1 | priced 模式下 UNKNOWN 费用被当 unpriced 写零并释放预留；无 `model_echo_mismatch` | `UsageFact.unknown` 与 `imported_usage.unknown` 区分"部署无价格表"与"SDK 无法计价"；`settle()` 有 unknown 即拒绝（预留保持占用）；所有结算路径改走 `_settle_if_known`；provider 回显模型 ≠ 配置模型 → Attempt/Planner 以 `model_echo_mismatch` 停止。测试：priced 模式真实结算金额>0；回显不一致 → 预留 RESERVED、unknown=1、Mission FAILED |
| 7 | P2 | LOST 前未导入 usage | LOST/TIMED_OUT 前导入 usage，有 unknown 不结算 |
| 8 | P2 | `_settle_intent` 绕过 Commit Service；`mid_commit` 未接线 | `CommitService.settle_intent` + 事件 `IntentSettled`；`record_result` 事务中触发 `mid_commit`；恢复矩阵加一条 |
| 9 | P2 | Critic 看不到测试输出 | 保持 §14.1 层序（Critic 先于测试）；Mission 级判定时的 Critic 能拿到已完成的 code_test 输出；记入 §5 |
| 10 | P2 | `format_check` 恒 PASS | 保留（格式已由采集器严格校验，该层是审计记录）；记入 §5 |
| 11 | P2 | `run_tests` 参数注入 | 路径解析后必须存在，以 `--` 分隔传入 |
| 12 | P2 | 阻塞时每次轮询都发心跳 | 只在租约剩余不足一半时续租 |
| 13 | P2 | `usage_refs` 未交叉核对 | 未做（费用权威已是 SDK 账本逐条导入）；记入 §5 |
| 14 | P2 | `artifact.version` 恒 1 | 未做；记入 §5（第 3 步产物版本传递时一并做） |
| 15 | P2 | `--provider env` 无价格表时静默 unpriced | CLI 拒绝，除非显式 `--unpriced`；真实测试仍显式 unpriced（记录） |
| 16 | P2 | AGENT_CREATED 可被活 owner 抢占 | 已修（同 #2） |
| 17 | P2 | cancel 不结算、事件粒度 | 已修（同 #5） |
| 18 | P2 | 死代码/文档陈旧 | 符号链接守卫改为在解析前检查；删 `ALIVE_STATES`；plan D2 的 `leases` 表以列内联实现（记录） |

## 4. 证据

| 项 | 结果 |
|---|---|
| fixtures 决定性测试 | `tests/orchestrator` 45 passed（合同 7、存储/预算 3、Commit Service 5、工作区/网关 3、闭环 5、恢复矩阵 9、CLI 3、review 回归 7）+ 1 skipped（真实模型 opt-in） |
| SDK 全量回归 | 73 红 ⊆ 基线，0 新红（`baseline-known-failures.txt`；`test_public_api` 快照升 0.9.0，旧快照存 `public-api-0.8.0.json`；`test_build_contents` 接受第二个包） |
| mypy | `src/agent_orchestrator` 41 文件 0 错（已加入 `[tool.mypy] files`） |
| 真实模型（DeepSeek `deepseek-v4-pro`，unpriced 记账） | run1 FAILED `mission_criteria_unmet`（Planner 只选 `code_test`，自由文本准则无裁判 → 促成 D21 修订）；run2 COMPLETED `verification_passed`（判定时自跑 Critic）；run3（review 修复后）COMPLETED，1 个 Attempt，16 116 tokens，planner/attempt/critic 三笔预留全部 SETTLED。报告 `reports/real-single-task-run{1,2,3}.txt` |
| 发布物 | `simple_harness_sdk-0.9.0-py3-none-any.whl`，源提交 `37a6717`，`SOURCE_DATE_EPOCH=1789059427`，sha256 **`e8c945e344f641a2b129014b07b3ed4850f0b4707fff9f930841f574ff3f2625`**；干净 venv（uv, py3.12，wheel + pytest + tiktoken）从归档源根跑 `tests/orchestrator + tests/agents + contracts + schema v10/迁移`：**283 passed, 4 skipped**；安装后 `python -m agent_orchestrator demo --scenario single-task --provider fixtures` → COMPLETED，证据目录 7 类文件齐全 |
| 独立 review | plan review 27 条（§1）、代码 review 18 条（§3）全部处置或登记 |

## 5. 遗留

| # | 事项 | 归属 |
|---|---|---|
| L2-1 | Critic 在 §14.1 层序里先于 code_test，验证阶段看不到本次测试输出（Mission 判定阶段能看到） | 第 3/4 步评估是否给 Critic 加"可读取 Task 级测试输出"的独立通道 |
| L2-2 | `format_check` 层恒 PASS（格式校验在采集器完成） | 第 3 步把采集器的解析结果作为该层记录内容 |
| L2-3 | `usage_refs` 不做交叉核对；`artifact.version` 恒 1 | 第 3 步（产物版本传递）一并做 |
| L2-4 | `run_tests` 子进程无网络隔离；只有 env 白名单 + cwd + 超时 | 第 6/7 步沙箱 |
| L2-5 | LOST 只覆盖"turn 不存在/agent 打不开"；多执行者抢占场景在第 3 步 S3-07 | 第 3 步 |
| L2-6 | 真实模型运行仍以 unpriced 记账（DeepSeek 价格表未注入）；priced 路径由 fixture 测试覆盖 | 第 6 步接价格表 |
| L2-7 | 每 Attempt token 预留不是硬上限（硬上限靠 SDK per-turn limits 与 runtime hard_cap） | 第 6 步 |

## 6. 终态

**VERDICT: SHIPPED**（2026-09-11，SDK main `37a6717` + 本文档提交；版本 0.9.0）

| 验收 | 结果 |
|---|---|
| S2-01 正常闭环 | PASS（fixtures + CLI demo + 真实 DeepSeek run2/run3） |
| S2-02 有错代码→修复 | PASS |
| S2-03 八类重放不重复 | PASS |
| S2-04 Agent 创建后崩溃（planner/worker 各一，含 submit 后） | PASS |
| S2-05 结果提交后崩溃（含 turn 提交后、验证层通过后、事务中途） | PASS |
| S2-06 预算/次数耗尽可解释停止 | PASS |
| S2-07 无效/伪造/幽灵产物拒绝 | PASS |
| S2-08 UNKNOWN 保持阻塞、预留不释放、不写零（含 priced 模式 unknown） | PASS |
| 遗留 | §5 L2-1～L2-7 |
