# 第 7 步 · 执行记录

## 1. 关键裁决

独立 review（claude-opus-5，只读；原文见 `reports/plan-review-round1.md`）共 4 P0 / 12 P1 / 11 P2，处置写在 plan §6（D7-x'）。

**过程问题（评审指出，属实）**：切片 A 的代码在评审还没结束时就开始写了，违反 HANDOFF §2 第 3 步"先评审再实现"。
- 处置：这部分代码没有提交；评审结论出来后，先修订 plan，再按修订后的 plan 调整已写的代码，调整完再提交。
- 以后：评审结论出来之前，只写测试草稿，不写实现。

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| P0-1 | P0 | 已交接或已执行的版本也会被新版本 SUPERSEDED；SUCCEEDED 之后用新幂等键再执行一次 | 补动作与审批两张状态机表，列出禁止的边；最新版本 SUCCEEDED 时内容不同 → REFUSED `action_already_executed`；FAILED / 被拒之后可以出新版本，但要重新审批；准则满足 = 最新非 REFUSED 版本 SUCCEEDED（D7-2'） |
| P0-2 | P0 | 执行时点早于 Mission 判定，S7-03 测不出 | 交接门槛 = 所有 live Task COMPLETED 且非动作准则已满足，执行是判定的最后一步；S7-03 按"v1 被取代、连接器零调用"写测试（D7-5'） |
| P0-3 | P0 | `_decide` 每轮 `_judge` 并返回 True，`run()` 空转、重跑 pytest / Critic；挂起的结果被重复拾取 | 判定分两段，非动作准则按树哈希只判一次并入账；只剩等待人工时返回 False；结果新增 SUSPENDED；`needs_critic` 排除 `action:`（D7-7'、D7-8'） |
| P0-4 | P0 | 动作不在预算里，S7-06 的"预留保持占用"观察不到 | `begin_handoff` 同一事务预留 `action:<key>`（Mission 账户，tool_calls=1，cost 取上限或 null）；UNKNOWN 发 ReservationHeld 并保持占用；`max_action_handoffs_per_mission`（D7-5'） |
| P1-1 | P1 | 候选校验依赖 rule_check；accept 登记报错会让验证任务崩溃 | 有候选就强制做 `action_candidate` 检查；accept 从原字节重新校验，不通过走 `fail_result`；登记与 accept 同一事务（D7-2''） |
| P1-2 | P1 | 动作没有最小权限范围 | 以 Mission 的 `action:` 准则作为允许的动作范围，范围外 → FAIL；提交时检查（D7-3'） |
| P1-3 | P1 | 同 Mission 两个 Task 做同一操作会被合并；再次要求要靠新 Mission | 选方案 (b)：`OperationSpec.kind`，本步只允许 `state` 类；补"新 Mission 同内容再应用一次"测试（D7-2'） |
| P1-4 | P1 | 交接不原子、没有 owner、再交接不重校、没有人工出口、终态 Mission 不核对 | `begin_handoff` 一个事务 + CAS + owner / 租约；再交接走完整校验；STILL_UNKNOWN → 等待人工核对并由 HumanOverride 裁决；核对覆盖所有 Mission；`_cascade_stop` 不碰在途动作；lookup 必须权威，服务按键加锁（D7-5'） |
| P1-5 | P1 | 在途计数与核对节奏没定义 | 在途只算本进程持有的 HANDED_OFF；UNKNOWN 不算在途；连接器调用放到线程里并设超时；run 开始时核对一次，之后按间隔核对（D7-5'） |
| P1-6 | P1 | 挂起期间租约、复用层、替代、needs_human 规则都没写；Verifier 冲突没有覆盖 | 恢复时续租、只复用 verifier_version 一致的层、Critic 裁决从库里取；替代时请求 CANCELLED；needs_human 不短路、强制走人工层、最多升级一次；**Verifier 冲突不降级**：Host 文档 §9.2 明确要求，定义两种情形并建 arbitration 请求（D7-8'） |
| P1-7 | P1 | 接管边界 | retry_with_note 只用于非终态 Task，受 max_attempts 约束，次数用完拒绝；stop 会让整个 Mission FAILED；不能复活（D7-9'） |
| P1-8 | P1 | D7-4 与 D7-3 在计数上矛盾 | 按 `l3_distinct_principals` 分两种计数规则；统一写"不同 principal_id"；政策值冻结进请求（D7-4'） |
| P1-9 | P1 | 没有 CANCELLED；终态 Mission 上的请求仍能批准 | 新增 CANCELLED；批准要求 Mission 是 ACTIVE（D7-4'） |
| P1-10 | P1 | 等待人工与运行时间上限的关系 | 不计入：运行时间扣除请求区间的并集，由 approvals 表推导；登记（D7-7'） |
| P1-11 | P1 | 缺少"不回滚数据库伪装没发生"的证明 | S7-06 增加 (a)(b)(c) 三条测试（acceptance 已改） |
| P1-12 | P1 | 默认开着连接器 | `enabled_connectors` 默认 `()`（D7-3'） |
| P2-1 | P2 | `outputs` 一词两义 | 固定使用 `actions/` 前缀，登记 |
| P2-2 | P2 | L2 定级等约定 | 登记（D7-3'） |
| P2-3 | P2 | risk_level / Host auto 模式 | risk_level 不作为上限；auto 模式不适用；登记（D7-3'） |
| P2-4 | P2 | 遗留 L3-3 / L6-4 未处置 | 见 §3 遗留：本步的核对只覆盖编排层连接器动作；SDK 工具效果的 UNKNOWN 对账不在公共面，继续登记 |
| P2-5 | P2 | review 也要发 ApprovalRequested / Granted / Rejected，actor 字段 | 采纳（D7-4'） |
| P2-6 | P2 | 命名不一致、nonce 规则 | 统一为 `review`；nonce 自动生成，也可显式传入（D7-10'） |
| P2-7 | P2 | 审计链 | 决定回执 → 交接 → 服务回执（D7-4'） |
| P2-8 | P2 | 人工文本做密钥检查；reason 标注不可信 | 采纳（D7-10'） |
| P2-9 | P2 | S7-08 补用例 | 采纳（acceptance 已改） |
| P2-10 | P2 | 提交 Mission 时检查动作准则 | 采纳（D7-3'） |
| P2-11 | P2 | 可砍 | PaymentConnectorStub 只保留单元测试；砍掉 `MissionWaitingForHuman` 事件，保留 `waiting_on` |

### 代码评审（第 1 轮，claude-opus-5，只读；原文见 `reports/code-review-round1.md`）

结论：无 P0、3 P1、10 P2。修复都配有决定性测试，修完后 `tests/orchestrator` 248 passed / 6 skipped。

| # | 发现 | 处置 | 测试 |
|---|---|---|---|
| P1-1 | L3 同一审批人第二次批准没被拒，还写进授权回执链 | 写入之前抛错；此后每条记下的批准都计数，`decision_receipts` = 真实授权 | `test_action_ledger.py` L3 测试与 `test_every_recorded_grant_counts_*`；`test_approvals.py::test_s7_05_*`（第二次抛错、回执 2 条且审批人不同） |
| P1-2 | judge 没跑成被当成 Verifier 冲突送仲裁 | 判定项标 `source=unavailable`，冲突判断只看真正独立运行的 judge | `test_human_review.py::test_a_judge_that_could_not_run_*` |
| P1-3 | 冲突仲裁①编排接入点、新 Mission 再应用一次没有测试 | 补两条编排级测试 | `test_human_review.py::test_s7_07_a_conflict_task_out_of_attempts_*`（keep / unresolved）、`test_approvals.py::test_the_same_change_asked_by_a_new_mission_*` |
| P2-1 | 多个动作准则会部分执行 | 全部动作都可交接或已 SUCCEEDED 才开始交接 | `test_nothing_runs_until_every_action_*` |
| P2-2 | PENDING 请求可以撤回 | 只有 GRANTED 可撤回，PENDING 走拒绝 | `test_every_recorded_grant_counts_*` |
| P2-3 | 决定时发现过期，过期写入被回滚 | 先单独提交过期，再做决定 | `test_an_expiry_found_by_a_decision_*` |
| P2-4 | 超时后在途标记提前清掉 | 线程真正结束才移出在途 | 超时测试加断言 |
| P2-5 | 交接前没重算参数哈希 | 重算，不符 → `params_hash_mismatch` | `test_altered_stored_parameters_*` |
| P2-6 | 崩溃在租约期内重启时无人核对、`waiting_on` 不列 HANDED_OFF | 空闲返回前再核对一次；`waiting_on` 列出 `handoff` | 崩溃测试加断言 |
| P2-7 | 仲裁②条件比 plan 宽 | 逐条比较：每个判过该准则的 Task Critic 都判满足才算冲突 | 仲裁②测试（原有两条） |
| P2-8 | CLI 漏接 `CommitRejected` / `StoreError` | 捕获并以错误信息返回 | — |
| P2-9 | 连接器调用在本轮内 await，最长 `connector_timeout_seconds` | 登记（遗留）：调用有超时兜底，线程执行不阻塞事件循环，但同轮其他 Mission 要等 | — |
| P2-10 | S7-01 断言偏弱 | 新增含 `pytest:` 与自由文本准则的用例，数 pytest 与 Critic 调用次数（等待期间各 1 次） | `test_s7_01_waiting_never_reruns_*` |

## 2. 执行记录

| 切片 | 提交 | 内容 | 测试 |
|---|---|---|---|
| A | `c0cbd12` | schema v5；动作账本（D7-2' 版本规则：在途 / 已执行不可取代）；审批与决定（nonce、回执哈希、按部署计数、CANCELLED、Mission 须 ACTIVE）；风险政策（默认关闭、event 类拒绝）；范围检查；测试服务（幂等账本、按键加锁、故障注入） | `test_action_ledger.py` 13、`test_schema_v5.py` 2 |
| B | `57cdf12` | `begin_handoff`（再校验 + CAS + 预留 + 决定回执，同一事务）、`record_action_outcome`（回执核对，不符 → UNKNOWN）、`record_reconciliation`（COMPLETED / CONFIRMED_NOT_STARTED / STILL_UNKNOWN，租约过期视为崩溃）、`override_action_outcome`（人工带证据裁决）；执行器 `runtime/actions.py`（线程 + 超时，只经 Commit Service 写库） | `test_action_execution.py` 12：S7-02；S7-06 回执丢失 / 崩溃在调用前 / 崩溃在应用后 / 恢复旧库 / 取消 Mission / 超时；再交接用完 → FAILED；人工出口 |
| C | `259eae7` | 闭环：验证时有 `actions/` 就强制 rule_check 检查候选（schema / 部署政策 / Mission 范围 / 声明的 outputs）；accept 事务从已存字节重验并登记，不通过走 `fail_result`；判定分两段（非动作准则按树键只判一次并入账 `MissionCriteriaJudged`，再看动作：可交接则交接、被拒 / 撤回 / 过期 → `approval_rejected`、FAILED → `action_failed`、只剩等待 → 无进展，`run()` 空闲返回）；`waiting_on` 派生视图与快照；请求 `closed_at` 与人工等待时长（运行时间上限扣除）；Mission 结束取消开放动作；提交时检查动作准则 | `test_approvals.py` 10（S7-01 含重启、L0、S7-03、S7-04 ×3、S7-05、S7-08、范围 / 未声明、提交拒绝）、`test_waiting_view.py` 2 |
| D | `38deb9d` | 第六层 human_review 部署：人工层在 §14.1 顺序最后，未答复时结果 SUSPENDED（不在拾取范围）并建 `review` 请求，Attempt / Task 不动；人工答复后回到拾取，只复用同版本已 PASS 的层（Critic 不再问、测试不再跑），第六层取人工结论；`needs_human`：Critic 契约新增字段，有 blocker 一律 FAIL，不短路（code_test 照跑，任一 FAIL 就不问人），政策没写 human_review 也强制走人工层，每个 Task 只升级一次；Verifier 冲突仲裁两种（冲突任务次数用完 → 选边 / unresolved；judge Critic 与 Task Critic 分歧 → met / unmet），裁决写 HumanOverride + 依据；接管 stop / retry_with_note（不加次数、不复活、带说明进下一次反馈）；评论 HumanCommentAdded 作为数据进下一次 Worker 反馈；人工文本做密钥检查；审批入口按请求类型分流 | `test_human_review.py` 13（S7-07：政策审核含重启、needs_human 强制与 Critic 只问一次、不替失败测试兜底、人工 FAIL 反馈与只升级一次、Mission 结束关闭审核、判定分歧仲裁 ×2、冲突选边 / 未解决、接管 stop / retry、不复活、评论） |
| E | `5abe6ff` | `api/approvals.py`（调用方身份在构造时给定，入口做密钥检查，候选 reason 标注"来自模型，不可信"）；CLI `approval list|approve|reject|revoke|comment|review|arbitrate|takeover|resolve --as`；`demo --scenario approval-action`（一条命令走完候选 → 审批 → 交接 → 回执核对；`--pause-for-approval` 停在等待人工，退出码 4，用同一 `--idempotency-key` 再跑继续）；证据新增 `actions.json`、`approvals.json`（含决定与等待时长），`trace.json` 的 actions 链、`metrics.json` 的 human / actions；真实模型 opt-in 测试 | `test_approval_action_closure.py` 3（demo 全链与证据无密钥、CLI 在两次运行之间批准、CLI 拒绝与不能复活）、`test_real_provider_approval.py` 1（opt-in）；step02 `test_later_step_scenarios_are_not_implemented` 改用第 8 步场景 |
| F | （本次） | 代码评审处置（上表）；版本 0.9.5 / 0.7.0；CHANGELOG、testcase、真实运行报告 | step07 合计 66 条（含 opt-in 1 条） |

## 3. 真实模型

- 运行 1（`5abe6ff`，deepseek-flash，`reports/real-approval-run1.md`）：真实 Planner（1223 tokens）拆出一个 Task，outputs 正确声明 `CHANGE.md` 与 `actions/set-new-ui.json`；真实 Worker（11702 tokens）按 `docs/ACTION_FORMAT.md` 写出只含五个字段的候选；验证与 accept 登记为 L2 动作，`run()` 空闲返回；演示操作员批准后在判定最后一步交接一次，测试服务 `applied_count=1`，回执核对一致 → Mission COMPLETED；11.5 s。证据 29 个文件，真实密钥逐字节命中 0、`sk-` 模式命中 0。

## 4. 回归与 wheel

- SDK 全量回归（`5abe6ff` 工作树，脚本 scratchpad `regress-s7/run.sh`）：58 failed / 2279 passed / 11 skipped / 15 errors，红集 73 条 = 基线，**0 新红**。
- 版本：simple_harness 0.9.5 / agent_orchestrator 0.7.0（`tests/unit/contracts/public-api.json` 同步）。

## 5. 遗留

- L3-3 / L6-4（SDK 出站 UNKNOWN 对账）：本步的动作核对只覆盖编排层连接器动作；SDK 工具效果的 UNKNOWN 不在编排可依赖的 `simple_harness.agents` 公共面，继续登记，不在本步处理。
- L2-4 / L6-5（`run_tests` 网络隔离）：继续登记。
- 补偿动作 / 回滚真实世界：不做（纲要 §12.6）。
- 代码评审 P2-9：连接器调用在判定循环内 await（有超时），同一轮其他 Mission 要等它结束；多 Mission 高并发时再改为独立任务。
- 交接前没有复核候选 artifact 的字节（只重算参数哈希）；artifact 由内容寻址存放、accept 时已从字节重验。
