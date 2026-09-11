# 第 7 步 plan 独立评审 · 第 1 轮（原文归档）

- 评审者：独立子代理（claude-opus-5，只读，没有改动任何文件），2026-09-11
- 输入：`step07/plan.md`、`acceptance.md`、`reports/design-brief.md`、设计原文、ORCH-BUILD-v1.0、现有代码
- 结论：4 P0 / 12 P1 / 11 P2，「计划现在不能直接进入（或继续）实现」
- 处置：见 `plan.md` §6 与 `journal.md` §1

---

## 过程问题

工作树里已经有未提交的第 7 步实现：`orchestrator/action_commits.py`、`governance/permissions.py`、`runtime/connectors.py`、`tests/orchestrator/step07/test_action_ledger.py`，另外 `schema.py`、`store.py`、`policies.py`、`state_machines.py`、`commit_service.py` 都有修改。评审期间 `policies.py` 还变过一次：第一次读是 58 行旧版，第二次已经有 `action_decision`。也就是说实现在评审完成前就开始了（HANDOFF §2 第 3 步规定先评审再实现）。下面的发现同时指向计划原文和这些已写的代码。

## P0（会让 S7 验收不成立，或与硬性规则冲突）

**P0-1 已经交接或已执行的动作版本没有定义，"新版本让旧版本 SUPERSEDED"不分状态**
- 发现：D7-2 写的是"参数或产物变化 → 同一 action_id 的新 version，旧版本 SUPERSEDED"，不看旧版本处在什么状态。计划没有动作状态机表，也没说旧版本处于 HANDED_OFF / UNKNOWN / SUCCEEDED / FAILED 时新候选怎么处理。已写代码的做法是：进行中则 REFUSED，开放态则 SUPERSEDED；最新版本已经 SUCCEEDED 或 FAILED 时，直接生成 v2（AWAITING_APPROVAL，幂等键 `action_id:v2`）并执行。结果是同一个业务动作用新幂等键执行第二次。另外 D7-7 的准则是"当前版本 SUCCEEDED"，已满足的准则会被 v2 拉回等待状态。
- 依据：plan.md:32、35（D7-5 写着"绝不以新幂等键重试"）；action_commits.py:117-173。
- 违反：纲要 §12.6（不能用账本状态改写已发生的现实）；§12.3（同一现实动作用稳定业务身份）；S7-02"只执行一次"。
- 处置建议：在 §6.1 补动作和审批两张状态机表，写明合法迁移，禁止 HANDED_OFF/UNKNOWN/SUCCEEDED → SUPERSEDED/REVOKED/EXPIRED；UNKNOWN 只能经核对或带证据的人工裁决离开。三条规则：最新版本 SUCCEEDED 时内容相同去重、内容不同 REFUSED（`action_already_executed`）；最新版本 FAILED 时允许新版本但必须重新审批并登记为新的合法尝试；准则满足改为"已有版本 SUCCEEDED 且之后没有开放版本"，或直接禁止执行后再出新版本。每条配决定性测试。

**P0-2 执行时点没有和 Mission 判定对齐，S7-03 在真实循环里无法稳定成立**
- 发现：候选在 Task accept 时就登记（D7-2），执行器每轮对 APPROVED 动作交接（D7-5）。已完成的 Task 永不重跑，所以"批准后 Agent 改了内容"只能来自后续 Task 或第 5 步的替代 Task；而循环里执行器会先把已批准的 v1 执行掉，于是 S7-03 要么测不出，要么落进 P0-1。多 Task 的 Mission 还可能在真实修改之后才因为其他准则、预算或冲突变成 FAILED。
- 依据：plan.md:32、35、37；event_handler.py:575-627、2363-2368；commit_service.py:3041-3042。
- 违反：纲要 §12.3"副作用与候选探索分离"；S7-03 无法判定。
- 处置建议：审批可以在 accept 时发起；交接门槛定为"所有 live Task 都 COMPLETED，且所有非 `action:` 准则已在集成树上判定满足"，动作是 Mission 判定的最后一步，只执行最终版本。S7-03 测试写成：v1 已批准 → 下游 Task 产出 v2 → v1 被 SUPERSEDED，连接器零调用。

**P0-3 按现有代码，"`run()` 空闲返回"做不到**
- ① `_decide` 在所有 Task 都 COMPLETED 后每轮调用 `_judge` 并返回 True（event_handler.py:2363-2368）。D7-7 说"不判定、保持 ACTIVE"，于是每轮都算有进展，`run()` 空转到 `max_cycles=10000`，每轮还重跑 pytest 和判定 Critic，消耗 Mission 预算。
- ② 挂起的人工审核结果如果还停在 `verification_state=RUNNING`，每轮都会被重新拾取、重新验证（:607-615）。
- 违反：S7-01（"`run()` 空闲返回"）；预算不应因为等待人工而白白消耗。
- 处置建议：判定分两段，处于等待时 `_decide` 返回 False，不跑 pytest 和 Critic，非动作准则的判定结果缓存或入账；`results.verification_state` 新增 `SUSPENDED`，不在拾取范围里，人工给出结论后改回 PENDING；`needs_critic` 排除 `action:` 前缀；加测试：S7-01 中 `run()` 在有限轮数内返回，并且 pytest / Critic 调用次数为 0。

**P0-4 动作不在预算里，S7-06"预算预留保持占用"无从观察**
- 计划没有说动作的预留挂在哪个账户、哪个维度，也没有交接次数的下层硬限额；Task 的预留在 accept 时已经结算（commit_service.py:2979-2980），执行发生在这之后，没有任何预留可以"保持"。
- 违反：S7-06 验收原文；纲要 §12.2；原文 §18.1。
- 处置建议：交接前在同一事务里预留（subject `action:<action_key>`，Mission 账户，`tool_calls=1`，`cost_micros` 取连接器声明的上限，没有定价时记 null 不写 0）；SUCCEEDED / FAILED 结算；UNKNOWN 发 `ReservationHeld` 并保持占用；部署政策增加 `max_action_handoffs_per_mission`；S7-06 测试断言预留状态。

## P1（设计缺口或不一致）

- **P1-1 候选校验依赖 rule_check 层，登记失败时 accept 的行为没定义**：rule_check 只在 Task 政策包含它时才运行；accept 事务里登记报错时 `_verify` 只捕获 CommitRejected / IllegalTransition，验证任务会崩溃、`run()` 被中断；已写代码把不合法候选记为 REFUSED 而 Task 照样 COMPLETED。建议：只要 Task 会产出候选就强制检查 schema 与政策；accept 事务从已接受产物的原字节重新校验，`artifact_hash` 用 artifact 的 `content_hash`；不通过走 `fail_result`；登记、`ApprovalRequested` 与 accept 同一事务。
- **P1-2 动作没有最小权限范围**：任何 Worker 写出的候选都能变成动作；L0/L1 不经人工直接执行；Planner 可以给 Task 声明任意 `actions/*.json`；四方交集只管工具不管动作。违反原文 §21.2、§21.3。建议：Mission 章程声明允许的动作范围（`allowed_actions` 或以 `action:` 准则为准），Task 合同声明可产出的候选，范围外 → 验证 FAIL；`target` 先规范化再哈希。
- **P1-3 业务动作 ID 与纲要 §12.3 的对照**：跨 Attempt 稳定满足；"再次要求 → 新身份"靠新 Mission 表达可以接受，要登记并补测试（新 Mission 同内容 → 新 action_id → 服务再应用一次）；同一 Mission 两个 Task 对同一目标做同一操作会被合并成版本互相 SUPERSEDED。建议 (a) 哈希加入 Task 血缘根和候选名，或 (b) `OperationSpec` 标注"状态设置型 / 事件发生型"，本步只允许状态设置型并登记。
- **P1-4 执行器的原子性和崩溃窗口**：① 交接前再校验与写 HANDED_OFF 必须是 Commit Service 同一事务、对动作版本和审批版本做 CAS；② 没有执行者归属，两个实例共用一个库时会把"调用在路上"误判为 CONFIRMED_NOT_STARTED 并发再交接，测试服务无锁可能应用两次——HANDED_OFF 带 owner 和租约，或连接器按键串行；③ CONFIRMED_NOT_STARTED 后再交接必须完整重跑交接前校验；④ 再交接用完或一直 STILL_UNKNOWN 时要有人工出口；⑤ 核对必须覆盖已终止的 Mission，`_cascade_stop` 不能改动 HANDED_OFF / UNKNOWN；⑥ 写明 `supports_reconciliation` 的前提是 lookup 权威（应用与写幂等账本原子完成）。
- **P1-5 在途计数与核对节奏**：连接器调用 inline 还是跨轮没说；建议在途只算本进程持有的 HANDED_OFF（连接器超时兜底），UNKNOWN 不算在途、每次 `run()` 或每若干轮带退避核对并出现在 `waiting_on`；连接器调用要有超时且不阻塞事件循环。
- **P1-6 human_review 的挂起与恢复**：① 挂起期间 Attempt 租约过期，accept / fail 都会 `_require_lease`，要写明恢复时的租约规则；② 复用 PASS 层只限 `verifier_version` 一致的层，`_critic_verdicts` 只在内存里；③ 第 5 步替代 VERIFYING 的 Task 时审核请求要同步关闭，`review()` 拒绝非 SUSPENDED 的结果；④ `CriticVerdict` 没有 `needs_human` 字段（契约变更）、有 blocker 一律 FAIL、NEEDS_HUMAN 不能短路掉 code_test、最终 `passed` 要重新定义、每个 Task 最多升级一次、政策没有 human_review 时是否强制走人工层要明确；⑤ 纲要要求把"Verifier 冲突"送人工，计划没有覆盖，建议增加 kind=arbitration 的人工仲裁，或登记未做并降低 S7-07 覆盖说明。
- **P1-7 接管的边界**：`retry_with_note` 只能用在非终态 Task，被关闭的 Attempt 计数、次数用完拒绝接管；`stop` 会让整个 Mission FAILED 要写明；已终止的 Task 或 Mission 不能被接管复活。
- **P1-8 D7-4 与 D7-3 自相矛盾**：D7-4 无条件写"同一审批人对同一请求只计一次"，`l3_distinct_principals=False` 时一个审批人永远满足不了 L3；术语要写"不同 principal_id"而不是"不同自然人"，CLI `--as` 是自报身份；政策值在创建请求时冻结要写明。
- **P1-9 审批请求没有"撤销 / 作废"状态**：Mission 终止、Task 被替代时挂着的请求无处可关；终态 Mission 上的请求照样能被 GRANT。建议新增 `CANCELLED`，在 `_cascade_stop` 和 supersede 时关闭，approve 要求 Mission ACTIVE。
- **P1-10 等待人工与运行时预算的关系没有裁定**：人工审核挂起时 `_runtime_exhausted` 会以 `budget_exhausted` 让 Mission 失败；要定等待时间算不算运行时间。
- **P1-11 S7-06"不回滚数据库伪装没发生"缺少可证明的测试**：建议 (a) HANDED_OFF 之后账本只追加、状态不回到 APPROVED；(b) 交接前备份 `orchestrator.db` → 执行 → 恢复备份 → 再跑：同一幂等键交接、服务去重、`applied_count` 仍为 1、凭去重回执核对为 SUCCEEDED；(c) UNKNOWN 期间 `cancel_mission`：动作仍 UNKNOWN、核对继续、不写 FAILED。
- **P1-12 默认值与关闭开关**：`DeploymentPolicy` 默认 `enabled_connectors=("test_config",)`、`max_action_level="L3"`，任何部署默认都开着一个连接器。建议默认 `()`，演示和测试显式启用。

## P2（登记、术语、覆盖、可砍）

1. `Task.outputs` 一词两义（原意是本 Task 可改写的上游路径）：用独立字段 `action_outputs`，或固定 `actions/` 前缀约定。
2. `test_config.set` 定为 L2 是约定；"未知操作按 L3""取声明与覆盖中较高者"也要登记。
3. 简报歧义 3 / 22（`Mission.risk_level` 是否作为动作等级上限）没裁定；歧义 23（Host auto 模式）要登记"不适用"。
4. 遗留 L3-3 / L6-4（SDK 出站 UNKNOWN 对账）完全没提，要逐条写明新归属。
5. §24 的 `ApprovalRequired` 实际对应 NEEDS_HUMAN：审核请求也应发 `ApprovalRequested{kind: review}`，人工结论发 `ApprovalGranted` / `ApprovalRejected{kind: review}`；人工事件用 `actor_type` / `actor_id`。
6. 命名不一致：`resolve_review` 与 `review`；acceptance 的 CLI 列表没有 `review`；CLI 的 nonce 自动生成还是传入没写。
7. 审计链：HANDED_OFF 记录要写入授权本次交接的决定回执哈希，并和连接器回执串起来。
8. 人工文本入口：`HumanComment` 在 API 入口做密钥检查；候选 `reason` 是模型写的文本，展示给审批人时标注"不可信"。
9. S7-08 补用例：候选里带 `approved` / `level` / `idempotency_key` 字段 → FAIL；不可信文档要求"降为 L0"→ 等级不变。
10. `validate_spec` 要检查 `action:` 准则引用的连接器已启用、操作已知。
11. 可砍：`PaymentConnectorStub` 只需一条"部署拒绝启用"单元测试；`MissionWaitingForHuman` 事件和 `waiting_on` 派生字段保留一个。

## 结论（评审原话要点）

计划现在不能直接进入（或继续）实现。先改 plan §3 / §6.1 并在 journal §1 逐条处置：P0-1～P0-4；P1-1、P1-2；P1-4、P1-5；P1-6、P1-7；P1-8、P1-9、P1-12；P1-11。其余 P1 和 P2 可以在实现中处理，但必须登记。
