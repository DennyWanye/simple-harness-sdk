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
（回填）

## 4. 证据
（回填）

## 5. 遗留
（回填）

## 6. 终态
（回填）
