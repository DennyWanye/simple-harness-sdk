# 第 7 步：Human-in-the-loop 与受控真实操作 · 实施计划

- 日期：2026-09-11 · 基线 SDK main `ba1f5e2`（0.9.4 / agent_orchestrator 0.6.0，第 6 步 SHIPPED）
- 原文依据：§14.1（第六层人工审核）、§14.4、§15、§17.3/§17.4、§21–§22、§24（NEEDS_HUMAN）、§26.6、§28 第三阶段；ORCH-BUILD-v1.0 §9、§12.1–§12.6、§13；需求简报 `reports/design-brief.md`
- 术语（先查 `agent-orchestration-theory/`，出处见简报 §4）：
  - **风险等级**（原文 §22）：L0 只读研究，自动执行；L1 沙箱内可逆操作，自动执行；L2 生产环境修改，需要一次审批；L3 付款、删除、敏感数据，需要双重审批。理论目录无定义，以原文为准。
  - **Human-in-the-loop**（原文 §22）：「人工不需要参与所有步骤，而应出现在高风险、高不确定或长期停滞的位置」；原文 §3 给人的三种职能「审批、仲裁、接管」；「人工动作也要作为 Event 保存」：ApprovalRequested / ApprovalGranted / ApprovalRejected / HumanOverride / HumanCommentAdded。
  - **Proposal / Commit**（原文 §15）：「Agent 不直接执行高风险真实世界操作」；外部动作候选同样是 Proposal，由系统在批准后执行。
  - **幂等性**（理论 09 §10）：「同一个动作执行一次或多次，最终效果一致」；**稳定业务动作 ID**（纲要 §12.3）：跨 Attempt 的同一个现实动作用同一业务身份，用户确实再次要求时才有新身份。
  - **回滚与补偿**（理论 13 §12）：可逆动作可回滚、不可逆动作需要补偿；纲要 §12.6「不能通过恢复旧数据库来"撤销现实"」。本步不做补偿动作（登记）。
  - **分层验证第六层**（原文 §14.1）：「必要时人工审核」；§24 的 Verifier 结果含 NEEDS_HUMAN。
  - **最小权限 / 不可信内容**（原文 §21.2–§21.3；理论 13 §14–§15）：「外部内容不能改变系统权限」「高风险工具调用使用独立政策检查」。
  - 术语缺口（简报 §4.2）：审批 schema、撤权 / 过期、双重审批身份、Dry-run、Capability Token、回执 / 核对、Outbox 均无原文定义，本 plan 按 ORCH §13 登记为实施约定（§6.1）。

## 1. 主要矛盾

真实修改一旦发生就不可撤销（纲要 §12.6），而 Agent 的输出不可信、会反复修改、会被外部文本诱导。矛盾的主要方面是：**真实动作必须是一个与版本绑定、经人批准、只执行一次且结果可核对的系统动作**——Agent 只能交出候选；批准绑定到具体参数与产物哈希，内容一变批准就失效；执行前再校验、交接前先落盘、回执丢失只核对不重发。次要方面：人工审核作为验证第六层（挂起与恢复）、人工接管停滞 / 争议、所有人工动作入账可恢复、演示与真实证据。

## 2. 范围

演示（`demo --scenario approval-action`）：本地「测试配置服务」（专门的测试服务，不是生产）。Mission「把测试服务的 `feature_flags.new_ui` 设为 on 并记录变更说明」：Worker 在工作区写 `actions/set-new-ui.json` 候选与 `CHANGE.md` → 验证通过 → 系统登记 L2 动作 → `ApprovalRequested` → `run()` 空闲返回（Mission 等待人工）→ 演示操作员（CLI 调用方身份）批准 → 执行器交接前再校验 → 调测试服务 → 核对回执 → Mission 判定 COMPLETED。证据含候选、审批（请求 / 决定 / 回执哈希）、动作账本、服务侧回执与状态。

做：动作候选与动作账本（D7-1/2）；风险政策与部署政策（D7-3）；审批与能力绑定（D7-4）；执行器（交接前再校验、outbox 式交接、回执核对、UNKNOWN 核对）（D7-5）；连接器与测试服务（D7-6）；Mission 与动作准则、等待人工的表达（D7-7）；human_review 第六层（D7-8）；人工接管与评论（D7-9）；审批 API 与 CLI（D7-10）；可观测与证据（D7-11）；schema v5 与版本（D7-12）。

不做：补偿动作 / 回滚真实世界（登记，纲要 §12.6）；支付等无核对能力的连接器（保持禁用，纲要 §12.3）；在 SDK turn 内的 REQUIRE_USER 授权暂停（见 D7-1）；短期 Capability Token 的独立令牌格式（批准本身就是绑定到动作版本、有有效期的一次性能力，登记）；`run_tests` 网络隔离（L2-4 / L6-5 继续登记）。

## 3. 设计决定

| # | 决定 | 依据 |
|---|---|---|
| D7-1 | **审批与执行在编排层、在 turn 之外**：Agent 只能在工作区写动作候选；执行由编排层的 `ActionExecutor` 通过连接器完成，模型没有任何连接器工具。SDK 的 REQUIRE_USER 持久决策不在本步使用：它的决定入口 `RunClient.decide_authorization` 不在编排可依赖的 `simple_harness.agents` 公共面（program 纪律 1），且 SDK effect 身份绑定到单个 run / call，不能充当跨 Attempt 的稳定业务动作 ID（纲要 §12.3）。本步复用 SDK 的**语义**：效果状态名（PREPARED / HANDED_OFF / SUCCEEDED / FAILED / UNKNOWN）、核对结论（CONFIRMED_NOT_STARTED / COMPLETED / STILL_UNKNOWN）、回执哈希链与 nonce 防重放（登记 §6.1） | 原文 §15、§2.2；纲要 §9.1、§12.3；简报 §6.3 第 15–18 条 |
| D7-2 | **动作候选与动作账本**：Task 在 `outputs` 声明 `actions/<name>.json`（schema：`connector`、`operation`、`target`、`params`、`reason`）；Result 经 §14 分层验证并被 accept 后，Commit Service 在同一事务里把 accepted artifact 中的候选登记进 `actions` 表。**业务动作 ID** = hash(mission_id, connector, operation, target)（同一 Mission 对同一目标的同一操作是同一个现实动作）；参数或产物变化 → 同一 action_id 的新 `version`，旧版本 SUPERSEDED；`idempotency_key = <action_id>:v<version>`；`params_hash` = 规范 JSON 的 sha256，`artifact_hash` = 候选文件内容哈希。候选格式不合法 → 验证层 rule_check 就 FAIL（不会登记）。用户确实要再执行同内容时，由新 Mission（新 action_id）表达（纲要 §12.3） | 原文 §20.2（Artifact 含「数据库变更计划」）、§17.3、§17.4；纲要 §12.3 |
| D7-3 | **风险政策**（`governance/policies.py`）：连接器为每个操作声明风险等级（测试配置服务：`read` L0、`set` L2、`delete` L3）；`ActionPolicy.level_for(connector, operation)` 取连接器声明与部署政策覆盖中**更高**者，未知操作按 L3。L0/L1 自动执行、L2 需 1 次批准、L3 需 2 次。部署政策新增 `enabled_connectors`、`max_action_level`（超过则拒绝登记，事件 `ActionRefused`）、`l3_distinct_principals`（默认 True：两次批准必须来自不同审批人——**这是本实现的部署约定，不是原文要求**，纲要 §9.2 注）、`approval_ttl_seconds`（默认 24 h）。只有声明 `supports_idempotency` 且 `supports_reconciliation` 的连接器能执行 L2 / L3，否则该连接器在部署里保持禁用（纲要 §12.3） | 原文 §22、§21.2；纲要 §9.2 policies 行、§12.3 |
| D7-4 | **审批与能力绑定**（`governance/permissions.py` + `approvals` / `approval_decisions` 表，Commit Service 唯一写入）：每个需要审批的动作版本一个审批请求，绑定 mission_id、task_id、action_id、version、params_hash、artifact_hash、level、required_count、expires_at。决定只能来自 `ApprovalApi` 的调用方传入的 `Principal`（认证身份不能来自模型参数）；每次决定带 `nonce`，回执哈希 = sha256(规范 JSON{request_id, action_id, version, params_hash, artifact_hash, principal, decision, nonce})；同一 nonce 的重放返回原回执、不计第二次；同一审批人对同一请求只计一次。状态：PENDING → GRANTED（计数满）/ REJECTED / REVOKED（授予后、交接前撤回）/ EXPIRED（到期）/ SUPERSEDED（动作出了新版本）。事件：原文五个 + 登记新增 `ApprovalRevoked`、`ApprovalExpired`、`ApprovalSuperseded`、`ActionRefused`。**批准本身就是短期能力**：只对这一个动作版本、到期即失效（登记：不另造 Capability Token 格式） | 原文 §22、§17.3、§21.3；纲要 §9.2 permissions 行、S7-03/04/05 |
| D7-5 | **执行器**（`runtime/actions.py`，`orchestrator` 每轮调用）：对 APPROVED（或 L0/L1 的 PROPOSED）动作：① 交接前再校验（审批仍 GRANTED 且未过期、版本仍是当前版本、params_hash / artifact_hash 与审批一致、连接器仍启用、等级 ≤ 部署上限、Mission 仍 ACTIVE）——任一不满足 → 不交接（事件 `ActionHandoffRefused{reason}`）；② 先在事务里写 HANDED_OFF（outbox：幂等键、交接时刻）再调连接器；③ 连接器返回回执 → 核对回执里的幂等键、params_hash、target 与账本一致 → SUCCEEDED（`ActionSucceeded`，回执入账）；连接器明确拒绝（未应用）→ FAILED；④ 交接后异常 / 超时 → UNKNOWN（`ActionOutcomeUnknown`），预算预留保持占用；⑤ 核对：UNKNOWN 或"HANDED_OFF 却没有结果"（崩溃恢复）的动作按幂等键向连接器查询 → COMPLETED → SUCCEEDED（`ActionReconciled`）；CONFIRMED_NOT_STARTED → 允许用**同一幂等键**再交接一次（至多一次，同 SDK `rehandoff_count ≤ 1`）；STILL_UNKNOWN → 保持 UNKNOWN，可见、不重发。绝不以新幂等键重试 | 纲要 §9.2 tool_gateway 行、§12.1、§12.2、§12.6；原文 §21.1；SDK effects / reconciliation 语义 |
| D7-6 | **连接器与测试服务**（`runtime/connectors.py`）：`Connector` 协议 = `operations: {name: OperationSpec(level, schema)}`、`supports_idempotency`、`supports_reconciliation`、`execute(operation, target, params, idempotency_key) -> Receipt`、`lookup(idempotency_key) -> Receipt | None`。`TestConfigService`：文件存储（证据目录下 `test-services/config.json` + 幂等账本），`read`/`set`/`delete`；同一幂等键重复执行返回原回执、不重复应用；故障注入 `lose_receipt_after_apply`（应用后抛传输错误）、`reject_next`。`PaymentConnectorStub` 声明不支持核对 → 部署拒绝启用（演示"高风险连接器保持禁用"）。连接器凭证不进上下文 / 证据（测试服务无凭证） | 纲要 §9.1、§12.3；原文 §21.2「财务 Agent → 不能直接支付」 |
| D7-7 | **Mission 与动作准则**：成功准则新增 `action:<connector>.<operation>:<target>`，满足 = 该业务动作当前版本 SUCCEEDED 且回执已核对。判定时若动作准则未满足：动作 AWAITING_APPROVAL / APPROVED / HANDED_OFF / UNKNOWN → 不判定、Mission 保持 ACTIVE（**等待人工 / 核对**）；动作 REJECTED / REVOKED / EXPIRED → `fail_mission(APPROVAL_REJECTED, detail.kind)`；FAILED → `fail_mission(ACTION_FAILED)`。「等待人工」的表达：Mission 状态保持 ACTIVE（ORCH §13 的状态集不改），快照与 API 给出派生字段 `waiting_on`（待审批 / 待人工审核 / 待核对的列表），并在首次进入等待时发 `MissionWaitingForHuman`（登记）。`run()` 的在途判断**不计**等待人工的项（否则永不空闲），但计交接中的动作 | 纲要 §13（Mission 须能表达等待人工）、S7-01、S7-04 |
| D7-8 | **human_review 第六层**（`verification/human_review.py`）：部署该层（`STEP2_IMPLEMENTED_LAYERS` 加入 human_review；formal_check 仍未部署）。验证按 §14.1 顺序；只有前面必需层全部 PASS 才走到人工层（短路保证"人类批准不掩盖未通过的代码测试"）。人工层首次到达 → 建审核请求（`approvals` 表 kind=review，绑定 result_id 与 artifacts 哈希）、结果停在验证中（`VerificationSuspended{layer: human_review}`），验证任务结束但不 accept / fail；人工经 API 给出 PASS / FAIL（+ 说明）后，下一轮重新验证该结果：已记录 PASS 的层**直接复用**（结果不可变，不重复调用 Critic），人工层取人工结论。NEEDS_HUMAN 的另一来源：Critic 裁决带 `needs_human: true`（无法可靠判断成功条件，原文 §22）→ critic 层记为 NEEDS_HUMAN 并转人工层。挂起跨重启可恢复（审核请求与已记录的层都在库里） | 原文 §14.1、§22、§24；纲要 §9.2 human_review 行、§12.4 |
| D7-9 | **人工接管与评论**：`ApprovalApi.takeover(task_id, principal, action, basis)`，action ∈ {`stop`（stop_task，stop reason `human_override`）、`retry_with_note`（下一个 Attempt 的反馈里带人工说明）}；`resolve_review(review_id, verdict, note)` 即人工裁决；每次写 `HumanOverride{principal, action, basis, scope}`。**范围不扩大**：接管不能批准动作、不能改工具交集或预算、不能跳过必需验证层；评论 `HumanCommentAdded` 只是数据（进入下一次 Worker 包的"人工说明"区，按不可信数据同样标注来源） | 原文 §22、§3「接管」；纲要 S7-07 |
| D7-10 | **审批 API 与 CLI**（`api/approvals.py`）：`ApprovalApi(commit, principal)`：`list(mission_id?)`、`approve(request_id, nonce)`、`reject(request_id, reason)`、`revoke(request_id, reason)`、`comment(target_id, text)`、`review(review_id, verdict, note)`、`takeover(task_id, action, basis)`。CLI `python -m agent_orchestrator approval list|approve|reject|revoke|comment|review|takeover --as <principal>`（本地演示的身份来自命令行调用方，登记：真实部署应接认证系统）。模型侧：任何工具、信封字段、工作区文件都不能调用这些方法 | 纲要 §9.2 approvals 行、§14.3 |
| D7-11 | **可观测与证据**：证据目录新增 `actions.json`（动作账本：版本、参数哈希、状态史、回执）、`approvals.json`（请求、决定、回执哈希、评论、接管），测试服务的状态快照与回执；trace span 带动作 id / 版本 / 审批回执哈希；审计 = 事件 + 两张表（原文 §21.1「记录结果与审计日志」，30-23） | 原文 §21.1、§23；纲要 §14.3 |
| D7-12 | 版本：simple_harness 0.9.5、agent_orchestrator 0.7.0；schema v5（`actions`、`approvals`、`approval_decisions`、`human_overrides`）；新增 stop reason `approval_rejected`、`action_failed`、`human_override` | ORCH §14.1 |

## 4. 任务切片

| 切片 | 内容 | 决定性测试 |
|---|---|---|
| A 账本与政策 | D7-2/3/4/6/12：schema v5、动作账本、审批与决定、风险政策、测试服务与连接器 | `test_action_ledger.py`、`test_approvals.py`（S7-03/04/05 单元部分） |
| B 执行器 | D7-5：交接前再校验、outbox 交接、回执核对、UNKNOWN 核对、恢复 | `test_action_execution.py`（S7-02/06） |
| C 闭环 | D7-2（accept 时登记候选）、D7-7（动作准则、等待人工、停止原因）、S7-01/08 闭环 | `test_approvals.py` 闭环部分 |
| D 人工审核与接管 | D7-8/9：human_review 挂起与恢复、NEEDS_HUMAN、接管、评论 | `test_human_review.py`（S7-07） |
| E API、CLI、演示、证据 | D7-10/11：`api/approvals.py`、CLI、`demo --scenario approval-action`、真实模型 opt-in | `test_approval_action_closure.py`、`test_real_provider_approval.py` |
| F 收尾 | review、wheel 0.9.5、CHANGELOG、testcase、program.md、journal、推送、清理 | — |

## 5. 风险

- 真实模型可能写出格式不合法的候选：rule_check 拒绝并反馈，属于正常重试路径。
- 人工等待让 Mission 长时间 ACTIVE：`run()` 空闲返回、状态持久；真实部署需要外部唤醒（CLI 再运行）。
- 挂起的验证复用已记录层：若 Task Contract 在挂起期间被改（第 5 步改图），旧结果按既有 supersede 路径成为历史，不复用。

### 6.1 本步实施约定（非原文原句；按 ORCH §13 登记）

- 审批在编排层、执行在 turn 外（D7-1），SDK REQUIRE_USER 本步不接入；复用其状态与核对语义。
- 业务动作 ID = hash(mission, connector, operation, target)；同 Mission 同目标同操作视为同一现实动作，参数变化是新版本。
- 双重审批：两条独立决定、同 nonce / 同审批人不重复计数；是否必须不同审批人由部署政策 `l3_distinct_principals` 决定（默认是）。
- 新增事件：ApprovalRevoked、ApprovalExpired、ApprovalSuperseded、ActionProposed、ActionRefused、ActionHandoffRefused、ActionHandedOff、ActionSucceeded、ActionFailed、ActionOutcomeUnknown、ActionReconciled、VerificationSuspended、MissionWaitingForHuman；原文 §24 时序图的 `ApprovalRequired` 不另设，统一用 §22 的 `ApprovalRequested`。
- 「等待人工」是 ACTIVE Mission 的派生视图（`waiting_on`）+ 事件，不改 Mission 状态集。
- 批准即短期能力（绑定动作版本、有有效期），不另造 Capability Token 格式。
- 补偿 / 回滚真实世界不做；UNKNOWN 只核对不重发。
- CLI 的审批身份来自命令行调用方（本地演示），真实部署需接认证。
