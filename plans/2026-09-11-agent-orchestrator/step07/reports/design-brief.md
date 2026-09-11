<!-- 独立提取（claude-opus-5，只读）；由主会话从代理回复落盘，内容未改 -->

# 第 7 步需求简报

文中路径简称：
- **纲要**：`/Users/taiwan/PROJECTS/SimplaHarness/simple_harness/plans/taskSys2/agent-orchestrator-incremental-build-plan-phase2-zh-CN.md`
- **原文**：`/Users/taiwan/PROJECTS/SimplaHarness/simple_harness/plans/taskSys2/agent-orchestration-layer-complete-design.md`
- **理论/**：`/Users/taiwan/PROJECTS/SimplaHarness/simple_harness/plans/taskSys2/agent-orchestration-theory/`
- **编排/**：`/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk/src/agent_orchestrator/`
- **SDK/**：`/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk/src/simple_harness/`
- **交接**：`/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk/plans/2026-09-11-agent-orchestrator/HANDOFF.md`
- **program**：`/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk/plans/2026-09-11-agent-orchestrator/program.md`

---

## 1. 范围

### 1.1 纲要 §9 全文（纲要:401–439）

> **原文依据：§14.4、§21—22、§24、§28第三阶段。第7步结束达到原文"规模化与安全"阶段目标。**

**§9.1 可独立使用的新功能**
> Agent先产生一个可审阅的外部修改候选；系统根据风险发起审批；审批绑定具体动作与版本；只执行获批内容并核对真实回执。用户可以拒绝、撤权、评论或接管，所有动作入Event。
>
> 演示先使用专门的测试服务/测试环境，例如"修改测试配置项"。通过测试不等于对生产环境有任何自动授权。支付、删除等高风险连接器只有实际完成政策、幂等与核对实现后才能启用。

**§9.2 实施方式（模块表逐行）**

| 模块 | 新增闭环能力（原句） |
|---|---|
| `api/approvals.py` | 列出、批准、拒绝、评论、接管请求；认证身份不能来自模型参数 |
| `verification/human_review.py` | 把NEEDS_HUMAN和Verifier冲突送人工；保留原有分层验证，不以人类批准掩盖未通过的代码测试 |
| `governance/policies.py` | 原文L0只读自动、L1沙箱可逆自动、L2生产修改一次审批、L3付款/删除/敏感数据双重审批 |
| `governance/permissions.py` | 审批/能力绑定Mission、Task、动作参数、Artifact hash、有效期和当前版本；改内容后旧审批失效 |
| `runtime/tool_gateway.py` | 稳定业务动作ID、幂等、交接前再验证、结果核对；UNKNOWN不得触发盲目再执行 |
| `orchestrator/event_handler.py` | ApprovalRequested/Granted/Rejected、HumanOverride、HumanCommentAdded均入账并可恢复 |

> L3"双重审批"的具体身份安排原文未细化。本实现采用两个独立审批记录、禁止同一个审批回执重复计数；是否要求不同自然人必须在部署政策中明确，不将原文未规定的人员安排伪称为原要求。（纲要:422）

**交付命令（纲要:437）**：`python -m agent_orchestrator demo --scenario approval-action --provider fixtures --evidence-dir evidence/s7`；测试目录 `tests/orchestrator/step07/`。
- 纲要 §14.3（纲要:629）另有一句：「第7步增加approval操作」。它没有规定子命令名。
- 同处还要求：「scenario在所属阶段实现前必须报告"未实现"，不能返回假成功」。

**完成标准（纲要:439）**：「从候选、审批、执行到结果核对完整闭环可演示；不是只有一个"批准"按钮。」

### 1.2 纲要其他处对第 7 步的定位
- §3 表（纲要:111）：第 7 步属于「第三阶段：规模化与安全」，功能是「Human-in-the-loop 的受控真实操作」。用户能做的事：「系统生成修改候选，经绑定版本的审批后只执行获批动作」。
- 覆盖矩阵（纲要:693–725）中「建设步骤」列含 7 的原文章节有：§1、§2、§3、§14（2,4,6,7）、§21（2,6,7）、§22（仅 7）、§24（2,3,4,5,7）、§27、§28、§30、§31。
  - §14 那一行的实施边界：「按Task政策选择真实验证层、冲突处理与人工审核」。
  - §21 那一行：「细粒度与真实动作审批随后开放」。
  - §22 那一行：「风险分级、审批、仲裁/接管事件」。

### 1.3 "第 2–6 步只允许 L0/L1，真实修改到第 7 步"的原句位置
- 纲要 §1、§2 里**没有**这句原话。原话在 program:51：「5. 第 2–6 步只允许 L0 只读 / L1 隔离沙箱动作；真实修改到第 7 步。」
- 纲要中意思相同的句子有两处：
  - §4.1（纲要:127）：「此时只允许 L0 只读或明确隔离的 L1 沙箱操作，不开放生产修改、付款或删除。」
  - §12.3（纲要:555）：「第2—6步仅使用获准只读或独立沙箱动作。第7步才开放经过审批和核对的真实修改。」
- 风险分级的定义出自原文 §22（原文:1501–1508），纲要 §9.2 policies 行照搬。**只有 L0–L3 四级，没有 L4 或更高**：

```text
L0：只读研究，自动执行
L1：沙箱内可逆操作，自动执行
L2：生产环境修改，需要一次审批
L3：付款、删除、敏感数据，需要双重审批
```

### 1.4 纲要 §12 与第 7 步相关的通用条款（原句）
- **§12.1 表**（纲要:529–537）：
  - 「旧执行仍可能产生外部副作用 → 先撤销它的新动作资格，并核对在途动作；新Attempt可排队但不得重复未知业务动作」
  - 「编排派发回执丢失 → 重放相同creation_key/input_id，核对同一SDK回执」
  - 纲要:541：「已经发生的调用费用和工具结果即使迟到，也应归入实际发生的Attempt，不因为LOST而丢账。」
- **§12.2**（纲要:549–551）：
  - 「在途UNKNOWN预算保持占用，完成核对后再Settle」
  - 「Mission/Task/Attempt上限之外，真实Provider和Tool交接仍要有下层硬限额。无法把本次任务预算落实成真实调用限制时，不能宣称硬预算已完成。」
  - 「当前SDK接口缺少某种外部工具费用/额度绑定时，增加一个最窄的预算或工具适配扩展，并在所属功能步骤中验收；不复制Provider调用链，也不改造整个BaseAgent。」
- **§12.3 副作用与候选探索分离**（纲要:553–559，全文）：
  - 「第2—6步仅使用获准只读或独立沙箱动作。第7步才开放经过审批和核对的真实修改。」
  - 「跨Attempt的同一个现实动作需要稳定业务动作ID，不得仅用新Attempt ID来生成"全新"动作。相同命令的重放可去重，但用户确实再次要求执行相同内容时应有新的合法业务身份，不能把"参数相同"当成永远禁止重复。」
  - 「不支持幂等/核对的高风险连接器保持禁用或人工处理。这里的"可靠"不意味着任意外部系统都具备exactly-once保证。」
- **§12.4**（纲要:563–565）：
  - 「原文§14的格式→规则→独立Critic→测试/实验→形式验证→必要人工完整保留……Task Contract列明必须运行哪些层」
  - 「只要必需层未运行、结果未知或未通过，就不能宣称PASS。」
- **§12.5**（纲要:575）：「Worker不直接拥有编排数据库、Commit Service内部方法或全局预算写权限。」
- **§12.6**（纲要:581）：「已交付真实外部动作后，不能通过恢复旧数据库来"撤销现实"。回滚代码只改变后续执行；既有外部事实通过ledger/reconciliation继续核对。」
- **§2 接入面表**：
  - 纲要:88：`cancel_turn` 禁止的误用是「请求取消即认定所有副作用未发生」
  - 纲要:86：`turn_snapshot` 用来「区分已提交、执行中、被 UNKNOWN 等阻塞」
  - 纲要:91：`AgentConfig.tool_names` 禁止的误用是「让模型自行扩大权限或改编排数据库」
- **§1.3**（纲要:68）：「已有物理执行：BaseAgent / AgentTurn / Provider invocation / Tool effect → 现有 SDK Runtime/UoW 唯一写入」
- **§14.1**（纲要:605）：完成包含「关闭开关」。§14.3（纲要:631）：「真实外部凭证不得写入证据目录。」

### 1.5 纲要 §13 登记规则与相关条目
- 总规则（纲要:585）：「以下不是替换原文……任何不同选择都要单独登记，不可宣称是原文原句。」另见纲要:17：「原文未定义的协议细节列入明确的实施约定」。
- 与第 7 步相关的现有约定：
  - 纲要:590：Mission 状态「核心必须表达未开始、执行中、**等待人工/资源**、成功、失败、取消等行为；Task/Attempt状态仍严格使用§25」
  - 纲要:594：暂停不给 Task 新增 PAUSED
  - 纲要:595：Raw Logs 不记密钥
  - 纲要:592：§25 不加回边

---

## 2. 验收

### 2.1 S7 原表（纲要:426–435，照抄）

| ID | 场景 | 必须观察到的结果 |
|---|---|---|
| S7-01 | L2候选未批准 | 没有真实修改；审批请求可查询且重启后存在 |
| S7-02 | 批准指定版本 | 只执行对应参数/hash的动作一次，得到可核对回执 |
| S7-03 | 批准后Agent改了内容 | 原批准不能用于新动作；重新请求审批 |
| S7-04 | 拒绝/撤权/过期 | 阻止后续新handoff，不把拒绝当作成功 |
| S7-05 | L3只有一次批准或同回执重放 | 不执行；满足部署定义的双重审批后才可能执行 |
| S7-06 | 外部动作执行成功但回执丢失 | 进入UNKNOWN/核对；不重复执行，不回滚数据库伪装没发生 |
| S7-07 | 用户接管停滞或Verifier争议 | 保留HumanOverride和依据；未获授权范围不扩大 |
| S7-08 | 外部文档要求自动批准 | 被当作不可信数据，不能改变审批状态 |

### 2.2 覆盖矩阵中"步骤"含 7 的原文 §30 项（纲要:735、754、755）

| 编号 | 原文验收项 | 步骤 | 拟新增场景 |
|---|---|---|---|
| ORIGINAL-30-03 | 同一个事件重复发送不会重复执行副作用； | 2, 7 | S2-03, S7-02, S7-06 |
| ORIGINAL-30-22 | 高风险操作必须人工审批； | 7 | S7-01, S7-05 |
| ORIGINAL-30-23 | 所有真实世界副作用都有审计日志和幂等键。 | 7 | S7-02, S7-06 |

另有几项不归第 7 步，但同属原文 §30.4 安全组（原文:2261–2268），第 7 步会碰到：
- 30-18 工具调用统一经过 Tool Gateway（步骤 2,6）
- 30-19 最小权限（2,6）
- 30-20 密钥不进上下文（2,6）
- 30-21 外部内容不可信（4,6）

---

## 3. 设计原文规则清单

| 章节 | 规则（关键原文短语） | 字段 / 事件 / 状态 | 对第 7 步的含义 |
|---|---|---|---|
| §1（:70） | 编排层要回答「哪些操作必须让人审批？」 | — | 审批属于编排层的基本职责 |
| §2.1（:111） | 设计目标：「对高风险操作进行人工审批」 | — | 同上 |
| §2.2 原则一/二（:117–137） | Agent 能提出，但「不能直接执行」；「Agent 只提交 Proposal，系统负责 Commit」；「检查、去重、验证、权限判断」 | — | 动作候选同样是 Proposal；由系统执行 |
| §2.2 原则四（:151） | 「任务、事件、预算、结果和验证状态都必须保存到可靠存储中」 | — | 审批请求必须持久（S7-01「重启后存在」） |
| §3 架构图（:160、161、187、192、193） | 入口网关「身份、租户、权限、限流」；Mission Service「风险等级」；Verifier Pipeline「…/ 人工审核」；Policy & Security「最小权限、密钥、注入防护」；**HITL「审批、仲裁、接管」** | — | 人工的三种职能：审批、仲裁、接管 |
| §5（:312–319、338） | Mission 要说明「风险等级是什么？」；示例 `risk_level: "research"` | `mission.risk_level` | 示例值不是 L0–L3，见 §6 冲突 3 |
| §9.3（:644） | 升级链末端：「拆任务、换策略或人工介入」 | — | 人工也是路由升级的出口 |
| §10（:664、697） | 上下文第 9 项「可用工具与权限」；「密钥、隐藏权限和不必要的敏感数据不得进入模型上下文」 | — | 审批凭据和 Token 不进上下文 |
| §13（:891–896） | Result Envelope 有 `risks`、`requested_followup`；「Agent 只能提交候选结果，不能直接把 Task 改成 COMPLETED」 | `risks`、`requested_followup` | Agent 自己声明的风险只是数据 |
| §14.1（:913–925） | 分层验证，「第六层：必要时人工审核」 | 验证层 | 对应 `human_review` 层 |
| §14.2（:936–937） | 「企业流程 → 规则引擎、审批流、审计检查」；「文档 → …人工审核」 | — | 按领域选择人工层 |
| §14.4（:965–979） | 冲突流程：保留双方 → DISPUTED → Conflict Task → Critic/Arbiter → 外部验证 → Commit；「不能简单多数投票」 | ClaimStatus.DISPUTED | 纲要把「Verifier冲突」送人工，但原文 §14.4 流程里没有人工节点 |
| §15（:1002–1026） | Proposal 依次经 Schema 检查、权限检查、去重和版本检查、必要的 Verifier，再 Commit；「**Agent 不直接执行高风险真实世界操作**」；「分布式思考，集中式提交」 | — | 真实动作只能由系统在批准后执行 |
| §16.2（:1052–1063） | Event 示例（MissionCreated … BudgetReleased） | Event | 审批事件追加进同一个 Event Store |
| §16.3/16.4（:1074–1104） | Event Store + Current State Store；崩溃恢复中「COMPLETED 不重跑」「SUBMITTED 但未验证 → 重新进入验证队列」 | — | 待审批项与待核对动作也必须能恢复 |
| §17.1（:1115–1119） | 「只有当 Attempt.status == PENDING 时才允许改成 CLAIMED」，用 CAS | — | 审批的领取/决定同样适合 CAS |
| §17.3（:1136–1144） | 「基于旧版本提交的 Proposal 必须重新检查或合并，避免 Lost Update」 | `version` | 审批绑定版本；改内容后旧审批失效（S7-03） |
| §17.4（:1148–1158） | 「每个动作有唯一 ID：event_id / attempt_id / allocation_id / result_id / commit_id」；「同一事件重复到达，不会重复创建 Agent、重复扣费或重复触发下游任务」 | 唯一 ID | 原文**没有列出**动作/效果 ID（纲要 §12.3 补了「稳定业务动作ID」） |
| §17.5（:1162–1172） | Single Writer 管 Mission 状态、Task 状态、正式 Knowledge、全局 Budget、Task DAG 主版本 | — | 审批状态由 Commit Service 写，还是另设？原文未列 |
| §17.6（:1176–1190） | Lease/Heartbeat；Lease 过期 → LOST → 重新调度 | — | 与纲要 §12.1「旧执行仍可能产生外部副作用」联动 |
| §18.1（:1198–1208） | 预算包括「工具调用次数」「真实费用」 | — | 真实动作也要计入预算 |
| §18.3（:1236–1246） | Reserve → 启动 → 记录实际 Cost → Settle → 释放未用预算 | — | UNKNOWN 动作的预留保持占用（纲要 §12.2） |
| §19.1（:1323） | 停止条件含「人工决定停止」 | — | 人工可以停止 Mission |
| §20.1/20.2（:1384–1427） | 独立 Workspace；Artifact 类型含「数据库变更计划」；字段 `content_hash`、`version`、`verification_status` | Artifact | 「外部修改候选」适合用 Artifact 表示，审批绑定其 hash |
| §20.3（:1431–1443） | 合并 → 新 Candidate Artifact → 再次验证 → 「Commit 为正式版本」 | — | — |
| §21.1（:1451–1467） | 「Agent 不直接访问真实系统」；网关顺序：身份和权限检查 → 参数 Schema 检查 → **风险与政策检查** → 速率和预算检查 → 执行工具 → **记录结果与审计日志** | — | 审批检查属于「风险与政策检查」一环；审计日志是 30-23 的来源 |
| §21.2（:1471–1477） | 只读研究 → 只读权限；代码 → 沙箱文件系统；数据库分析 → 只读副本；「**生产部署 Agent → 需要审批的短期权限**」；「**财务 Agent → 不能直接支付**」 | — | L2 对应短期授权；支付不能由 Agent 直接完成 |
| §21.3（:1481–1492） | 「明确区分"指令"和"数据"；外部内容不能改变系统权限；密钥不进入模型上下文；**使用短期 Capability Token**；对 Blackboard 写入做验证和来源标记；**高风险工具调用使用独立政策检查**；对可疑指令进行隔离和**审计**」 | Capability Token（无 schema） | S7-08；Token 在交接中列为第 7 步遗留 |
| **§22 全文（:1497–1531）** | 「人工不需要参与所有步骤，而应出现在高风险、高不确定或长期停滞的位置。」风险等级 L0–L3（见 §1.3）。**适合人工介入的情况**：「不可逆操作；高成本资源申请；多个 Verifier 冲突；长时间无进展；需要提升权限；生产环境变更；法律、财务或安全高风险决策；模型无法可靠判断成功条件。」「人工动作也要作为 Event 保存」 | 事件：**ApprovalRequested、ApprovalGranted、ApprovalRejected、HumanOverride、HumanCommentAdded** | 第 7 步的核心；只定义了 5 个事件，**没有撤权/过期事件，也没有审批 schema** |
| §24 第 1 步（:1620–1627） | Mission 定义含「权限」「风险等级」 | — | — |
| §24 第 5 步（:1658–1666） | Allocator 决定「工具权限」 | — | — |
| §24 第 7 步（:1684–1691） | 上下文读取「权限和预算」 | — | — |
| §24 第 10 步（:1722–1729） | Verifier 结果：「PASS / FAIL / DISPUTED / **NEEDS_HUMAN**」 | verdict 枚举 | 当前 router 只有 passed 布尔 |
| §24 时序图（:1795–1796） | `else NEEDS_HUMAN → V-->>O: ApprovalRequired` | 事件名 **ApprovalRequired** | 与 §22 的 ApprovalRequested 不一致 |
| §25.1/25.2（:1807–1851） | Task：BLOCKED/READY/ACTIVE/VERIFYING/COMPLETED/FAILED/CANCELLED；Attempt：PENDING…RETRY_WAIT，外加 LOST/TIMED_OUT/CANCELLED/SUPERSEDED | 状态 | **没有任何"等待审批/等待人工"状态**；纲要禁止私自加回边 |
| §26.1（:1876–1889） | Mission Schema：`risk_level: string`、`allowed_tools`、`tenant_id`、`version` | — | 风险只到 Mission 粒度，值域未定义 |
| §26.2（:1893–1908） | Task Contract：`verification_policy: [string]`、`allowed_tools`、`version` | — | Task 层没有 risk 字段 |
| §26.4（:1929–1943） | Result Envelope：`risks: [string]` | — | — |
| §26.6（:1964–1978） | Event Schema：`actor_type`、`actor_id`、`payload`、**`idempotency_key`**、`trace_id` | — | 人工事件用 `actor_type` 区分人；30-23 的「幂等键」可落在这里 |
| §26 整体 | 六个契约里**没有 Approval / Action / Capability schema** | — | 需要按纲要 §13 登记为实施约定 |
| §27（:1988–2046） | `api/approvals`、`verification/human_review`、`governance/permissions`、`governance/policies`、`governance/secrets` | 模块 | — |
| §28 第三阶段（:2124–2142） | 增加「Backpressure、多模型路由、Workspace 隔离、Tool Gateway、**细粒度权限**、**Human-in-the-loop**、多 Mission 配额、完整 Trace、多层 Verifier」；目标：「在较高并发下仍然安全、可控、可追踪」 | — | 第 7 步收尾第三阶段 |
| §28 第四阶段（:2172–2175） | 「审批后上线」；「不要让在线 Agent 直接自我修改核心安全和调度规则」 | — | 属于第 9 步，但审批机制可能被复用 |
| §30.1/30.4（:2239、2263–2268） | 见 §2.2 | — | — |
| §31（:2329–2347） | 「Security / HITL = 不让错误转化为真实损失」；「必须可靠、一致、可重复和可审计的事情 → 交给确定性程序」 | — | 审批判断由确定性程序执行 |

---

## 4. 术语定义

### 4.1 理论/ 中能找到的定义

注：`agent_orchestration_theory_complete.md` 是 01–13 章的合订本，内容相同、行号不同，例如 13 §12 在合订本的 3251–3264。

| 文件 · 小节 | 术语 | 定义摘录 |
|---|---|---|
| `09_state_event_durable_execution.md` §10（:176–194） | **Idempotency 幂等性** | 「同一个动作执行一次或多次，最终效果一致。」「"把任务设置为 completed"是幂等的；"再创建 20 个 Agent"不是，必须用 allocation_id 去重。」唯一标识：event_id / task_id / attempt_id / result_id / allocation_id |
| 09 §13（:233） | 幂等 vs 绝不重复 | 自测题：「幂等性为什么比分布式系统中的"绝不重复"更现实？」 |
| 09 §11（:196–214） | Event Sourcing / 审计用途 | 「Event Log 用于审计、恢复和重放」 |
| `13_remaining_knowledge_map.md` §12（:257–270） | **Rollback 与 Compensation** | 真实世界动作举例：「发邮件、修改数据库、创建订单、付款、删除文件、发布代码」；「可逆动作可以回滚；不可逆动作需要补偿操作，例如创建取消订单或恢复记录。」 |
| 13 §14（:304–318） | Security 与 Permission / 最小权限 | 要回答「能否发送邮件或付款？能否创建新 Agent？」等；「核心原则是最小权限。」 |
| 13 §15（:322–338） | Prompt Injection | 「区分数据与指令、来源标记、消息签名或身份验证、工具权限隔离、知识写入审核、不可信内容标记、敏感信息过滤」 |
| 13 §7（:153–171） | 多层验证 | 最后一层「必要时人工审核」；「不同 Claim 应匹配不同验证器，而不是所有结果都由另一个 LLM 评分」 |
| 13 §3（:79–89） | 升级至人工 | 「便宜模型失败 → 更强模型 → 仍失败 → 拆任务或人工介入」 |
| 13 §20（:417–430） | 多租户中的审计 | 要求含「审计日志」 |
| 13 §22（:475–479） | 企业业务编排 | 「审批、权限、事务、补偿、审计、人工确认」（只列举，不定义） |
| `08_workflow_control_loop.md` §10（:204–220） | **人工升级**（停止条件之一） | 「系统无法判断关键方向，交给人处理。」 |
| `07_control_roles.md` §3（:68） | 批准（资源） | 「Manager 可以提出资源需求，Allocator 决定是否批准。」 |
| `11_budget_cost_backpressure.md`（:76–84） | 批准（资源申请） | 「提交 Resource Request → Allocator 检查价值和剩余预算 → 批准、部分批准或拒绝 → Scheduler 实际创建 Agent」 |
| `10_concurrency_conflict.md` §15（:216–220） | 只允许一个成功 | 「适合领取 Attempt、扣减预算、修改唯一状态。」 |
| `12_observability_tracing_evaluation.md` §6（:111–119）、§9（:171–181） | 人工成本 / 人工介入次数 | 成本含「人工时间」；Online Evaluation 观察「人工介入次数」 |
| `06_agent_lifecycle.md` §7（:132–142） | 取消 | 终止条件含「Agent 权限或运行环境异常」 |
| `README.md`（:61–64） | 可审计 | 「必须稳定、可复现、可审计的事情 → 适合交给普通程序和数据库」 |

### 4.2 术语缺口

下列术语在理论/ 中查不到定义。按交接 :26 的规定，理论与原文不一致时以原文为准。

| 术语 | 理论/ | 原文 | 纲要 |
|---|---|---|---|
| Human-in-the-loop | 只出现在 13 章标题「除 Human-in-the-loop 外还要学什么？」，**无定义** | §22 讲何时介入，无形式定义 | §9 |
| 审批 / Approval（针对动作） | 只有资源申请的「批准」（07/11），**无定义** | §22 有事件名，无 schema | §9.2 |
| 双重审批 | 无 | §22 只说「需要双重审批」，未细化身份 | 纲要:422 自行约定 |
| 撤权 / 过期 / 有效期 | 无 | **无**（§22 事件表里没有） | S7-04、permissions 行 |
| 接管 / HumanOverride | 无 | 只有 §3「接管」和 §22 事件名 | S7-07 |
| 副作用 / side effect | **无**（grep「副作用」零命中） | 只在 §30 条目中出现 | §12.3 |
| 真实动作 / 外部动作 | 13 §12 只举例 | §15「高风险真实世界操作」无定义 | §9 |
| 幂等键 / idempotency key | 只有「幂等性」，无「键」 | §26.6 有字段，无定义 | — |
| 补偿 / 回滚 | 13 §12 一句话 | 原文无 | §12.6「不能通过恢复旧数据库来"撤销现实"」 |
| Dry-run / 预演 | **无** | **无** | **无**（只有「可审阅的外部修改候选」） |
| 风险等级 | **无** | §22 定义 L0–L3；§5 示例值为 "research" | §9.2 |
| Capability Token | **无** | §21.3 只有「使用短期 Capability Token」 | permissions 行（「能力绑定……有效期」） |
| 审计 / 审计日志 | 只提及，无格式 | §21.1「记录结果与审计日志」，无格式 | — |
| Outbox | **无** | **无** | §1.3、§4.2「dispatch intent/outbox」 |
| 回执 / 核对（reconciliation） | **无** | 无 | §9.1、§12.6「ledger/reconciliation」 |
| 稳定业务动作 ID | 无 | §17.4 的 ID 列表里没有 | §12.3 |

---

## 5. 现有代码与 SDK 能力

### 5.1 编排侧（编排/）

| 文件:行 | 事实 | 第 7 步复用 / 扩展点 |
|---|---|---|
| `contracts/models.py:34-41` | `VERIFICATION_LAYERS` 共 6 层，依次为 format_check、rule_check、critic_review、code_test、formal_check、human_review | 名称已存在 |
| `contracts/models.py:42` | `STEP2_IMPLEMENTED_LAYERS = {"format_check","rule_check","critic_review","code_test"}` | 第 7 步要把 human_review 纳入已部署层 |
| `orchestrator/commit_service.py:731-735`、`graph/task_graph.py:265-270`、`graph/changes.py:514-519` | 提交阶段：策略中含未部署层 → `verification_policy_undeployed` 拒绝 | 这三处都要随部署层的变化同步 |
| `verification/verifier_router.py:94-158` | 按 §14.1 顺序逐层执行；不在策略里的层记 `NOT_REQUIRED`；首个 FAIL/ERROR 短路 | human_review 排在最后，天然满足「不以人类批准掩盖未通过的代码测试」 |
| `verification/verifier_router.py:152-155` | 运行阶段：formal_check/human_review 返回 `ERROR "layer not deployed in this build" {"undeployed": True}`，注释写着「human_review: step 7」 | 替换点 |
| `verification/verifier_router.py:44-57、159-161` | `Verdict` 只有 `passed: bool`；`LayerResult` 状态只有 PASS/FAIL/ERROR/NOT_REQUIRED/SKIPPED；**没有 NEEDS_HUMAN 或挂起态**；`verify()` 是同步 await 到结束 | 需要一个挂起/恢复机制 |
| `contracts/state_machines.py:17-25` | `MissionStatus` = CREATED/PLANNING/ACTIVE/COMPLETED/FAILED/CANCELLED，**没有"等待人工"状态** | 纲要 §13（:590）要求能表达「等待人工/资源」 |
| `contracts/state_machines.py:28-46` | `MissionStopReason` 共 13 个，其中 `VERIFIER_UNAVAILABLE` 在 :43-45（「a required verifier is not deployed」）；没有审批相关原因 | — |
| `contracts/state_machines.py:49-79、129-164` | Task/Attempt 严格按 §25 实现，没有审批状态 | 不能私自加回边 |
| `contracts/models.py:170-205` | `Mission.risk_level: str`，只做 ≤64 字符的文本校验，**不是枚举** | 值域需要定义 |
| `api/missions.py:62`、`orchestrator/commit_service.py:93` | `risk_level` 默认 `"sandbox"` | 与 L0–L3 词汇不同 |
| `context/context_builder.py:233` | `risk_level` 只放进 Planner 输入包；grep 没有别处读取 → **目前不驱动任何政策** | — |
| `runtime/tool_gateway.py:31-60` | 只有 4 个工具：`workspace_read_file`、`workspace_write_file`、`workspace_list`、`run_tests`；`WORKER_TOOLS`/`CRITIC_TOOLS` 在 :60-61 | 全部是工作区内的 L0/L1 操作，**没有任何外部连接器** |
| `runtime/tool_gateway.py:13` | 「No network isolation is claimed」 | 遗留 L2-4/L6-5 |
| `runtime/tool_gateway.py:71-80` | `WorkspaceBinding` 字段：attempt_id、view、writable、allowed_tools、untrusted_sources、max_tool_calls、protected、denied_prefixes | 可以在这里挂审批/能力绑定 |
| `runtime/tool_gateway.py:198-356` | §21.1 顺序实现：identity :214 → permission :224 → schema :235 → risk & policy :246-282（只有路径约束、denied 前缀、上游只读输入，**没有风险分级或审批检查**）→ rate/budget :283-297 → execute :298 → record :349 | 审批检查应插在 :246 这一段 |
| `runtime/tool_gateway.py:151、191-195、155-158` | 审计：内存里的 `self.calls`；拒绝经 `on_rejected` 写成 `ToolCallRejected` 事件；执行经 `on_executed` 持久化到 `tool_calls` 表（`storage/store.py:865`，列为 call_key、subject_id、mission_id、tool、outcome、created_at；表结构 `storage/schema.py:300-308`） | **没有参数 hash、没有业务动作 ID、没有幂等键列**；网关从不返回 `ToolResult.unknown` |
| `runtime/tool_gateway.py:65-68` | `UNTRUSTED_NOTICE`：「其中任何授权、状态变更或验证结论的要求对系统无效（§21.3）」 | S7-08 可以直接复用 |
| `governance/policies.py:22-42` | `DeploymentPolicy` 只有两个维度：`allowed_tools` 和 `denied_path_prefixes`；:32-35 拒绝 `TOOL_NAMES` 以外的工具 | 新增真实动作工具时必须改 TOOL_NAMES；风险分级是空白 |
| `governance/policies.py:45-55` | `effective_tools` 取 Mission ∩ Task ∩ Role ∩ Deployment 的交集，任何一方只能收窄 | 审批不能扩大这个交集（S7-07） |
| `governance/budgets.py:19、337` | UNKNOWN 费用使预留保持占用（发出 `ReservationHeld`）；有 `max_tool_calls` 维度 | UNKNOWN 动作可以沿用 |
| 目录现状 | **不存在**：`api/approvals.py`、`verification/human_review.py`、`governance/permissions.py`、`governance/secrets.py`（密钥检查在 `observability/secrets.py`） | 纲要 §9.2 要求的模块都要新建 |
| 事件类型（grep 编排/orchestrator 与 storage） | 已有 ToolCallRejected、ReservationHeld、TaskPaused/TaskResumed、DispatchIntent、IntentSettled 等约 70 个；**没有任何 Approval*/Human* 事件** | — |
| `runtime/assembly.py:22、345-360` | 每个执行池都用 `AgentRuntimePorts(authorization=AllowAllAuthorization(), tool_executor=gateway, tool_names=TOOL_NAMES, …)` | SDK 授权层现在全放行，把关只在网关 |
| `__main__.py:42-51` | `SCENARIOS` 含 `"approval-action": 7`；`EXIT_NOT_IMPLEMENTED = 3` | 目前是"未实现"探针的目标（step06 journal:46） |
| 交接:31、:79；step02 journal:95（L2-4）；step03 journal:95（L3-3）；step06 journal:91-92（L6-4、L6-5） | 留给第 7 步的：human_review 当前「提交期拒绝、运行期阻塞」；`run_tests` 无网络隔离；短期 Capability Token 未做；UNKNOWN 出站效果的对账「目前只做到可见」 | — |
| program:47 | 纪律 1：「`agent_orchestrator` → `simple_harness.agents` 公共 API 单向依赖，不 import SDK 私有模块」 | 限制了下文 SDK 能力的接入方式 |

### 5.2 SDK 侧（SDK/）已有能力：第 7 步应复用，不应另造

**A. SDK 原生授权接缝**（`tools/authorization.py`，由 `simple_harness.tools` 导出，见 `tools/__init__.py:7-12`）
- `AuthorizationDecision`（:20-23）：`ALLOW` / `DENY` / `REQUIRE_USER`。
- `AuthorizationRequest`（:26-55）：字段为 `prompt`、`nonce`（「the public replay fence」）、`expires_at`、`metadata`。
- `AuthorizationReceipt`（:58-72）：`receipt_ref`、`receipt_hash`、`bound_sdk_receipt_hash`，均为小写 SHA-256。
- `sdk_authorization_receipt`（:75-84）和 `bind_authorization_receipts`（:87-101）：生成双方回执并串成哈希链。
- `PreparedToolEffect`（:104-133）：effect_id、run_id、call、spec、sidecar、resources。
- `AuthorizationResult` 的约束（:136-160）：ALLOW 必须带 receipt_ref；REQUIRE_USER 必须带 request 和 reason_code。
- Protocol 三个方法（:163-180）：`prepare`、`bind_decision`、`bind_effect_handoff`。
- 协议摘要见 `docs/api/tools.md:22-33`：REQUIRE_USER 会被冻结成 SDK decision，Host 必须实现 `bind_decision`；**每个获准的效果都还要过 `bind_effect_handoff`，否则停在 `PREPARED`，不会调用物理 handler。**
- 可支撑：S7-01（未批准就不执行）、S7-02/S7-05（回执哈希绑定、nonce 防重放）、S7-04（过期）。

**B. 持久决策（Run 进入 WAITING）**
- `tools/executor.py:65-75`：`ToolAuthorizationPending`，`decision_id = "authorization:{effect_id}"`。
- `runtime/kernel.py:3335-3365`：`_commit_authorization_wait` 写入一条 decision（`kind="tool_authorization"`，`state=OPEN`）。request 里冻结了 arguments、call_id、effect_id、expires_at、nonce、prompt、resources、resources_digest、sidecar_digest、tool_name；事件 id 为 `{decision_id}:open`。
- `execution/uow.py:69-74`：`DecisionState` 有 OPEN/ALLOWED/DENIED/EXPIRED/CANCELLED；`DecisionRecord` 在 :163-170；`RunState.WAITING` 在 :54。
- `runtime/kernel.py:1395-1445`：`RunClient.decide_authorization(run_id, decision_id, nonce, expected_version, decision)`。
  - 带版本栅栏和 nonce 栅栏。
  - 错误码：`authorization_decision_not_found`、`_late`、`_version_conflict`、`_nonce_mismatch`、`_invalid`。
  - 到期自动变成 DENY/EXPIRED；同 nonce、同决定的重放直接返回原记录（幂等）。
  - `RunClient` 类在 kernel.py:805，从 `simple_harness/__init__.py:313/654` 导出。
- **grep 结果**：`agents/base.py`、`agents/runtime.py` 都没有引用 `decide_authorization` 或 `RunClient`。也就是说，**BaseAgent 的公共面目前没有"决定一条待审授权"的入口**。
- `agents/contracts.py:296-308`：`AgentTurnSnapshot` 有 `blocked` 和 `blocker` 字段，`AgentTurnState` 里没有 WAITING（:38-43）。

**C. 审批通过后的防篡改**
- `tools/executor.py:191-233`：`_prepared_from_decision` 会重新校验 sidecar digest 和 resources digest，报错为「authorization Tool sidecar authority changed / resource authority changed」。
- `tools/executor.py:343-356`：同一个 effect_id 如果参数或身份与冻结意图不同，报「Tool effect identity conflicts with frozen intent」。
- 可支撑：S7-03（批准后改内容不能继续用原批准）。

**D. Tool effect 账本与幂等**（`execution/effects.py`）
- `EffectState`（:36-43）：PREPARED、HANDED_OFF、SUCCEEDED、PARTIAL、REJECTED、FAILED、UNKNOWN。
- `effect_request_hash`（:63-67）：对 {arguments, tool_name} 的规范 JSON 取 sha256。
- `EffectRecord`（:271-356）：
  - `authorization_receipt_ref` 必填；非 PREPARED 状态必须有 `handoff_receipt_ref`；UNKNOWN 必须有 `evidence_ref`。
  - `rehandoff_count ≤ 1`；`dispatch_allowed` 只在 PREPARED 为真。
- `EffectUnitOfWork` 方法（:363-469）：prepare_effect、read_effect、mark_effect_handed_off、settle_effect、mark_effect_unknown、record_tool_reconciliation、read_reconciliation_resolution、reauthorize_effect_not_started、refresh_prepared_effect_authority。
- `tools/executor.py:357-381`：终态直接返回已存结果（重放幂等）；HANDED_OFF/UNKNOWN 走 reconcile，**不会重新执行**。
- 可支撑：S7-02、S7-06、ORIGINAL-30-03、30-23 中「幂等键」这部分。

**E. 对账**
- `tools/reconciliation.py:18-43`：`ReconciliationState` 为 CONFIRMED_NOT_STARTED / COMPLETED / STILL_UNKNOWN；`ReconciliationObservation` 必须带 `evidence_ref`，只有 COMPLETED 能带结果；`ToolReconciliationPort.observe(effect)`。
- `tools/executor.py:586-620`：`EffectExecutor.reconcile`。
- **默认实现** `runtime/consumer_adapter.py:298-305` 的 `_DefaultToolReconciliation` 永远返回 STILL_UNKNOWN。可通过 `ports.policies.tool_reconciliation` 注入（`agents/runtime.py:162`）。

**F. Provider 侧的类比**
- `execution/dispatch.py:575-578`：交接后发生异常 → `provider_error_after_handoff` → `_settle_unknown`。
- `execution/dispatch.py:692-720`：`reconcile_incomplete`「Observe uncertain handoffs without replaying their physical request」。
- 错误码登记在 `execution/audit.py:46`。

**G. 结果语义**
- `tools/contracts.py:56-61`：ToolOutcome 共五种结果。
- `tools/contracts.py:217-223`：`ToolResult.unknown` 的 error_code 为 `"tool_outcome_unknown"`。
- `runtime/ports.py:135`：「Use ToolResult.unknown() when side effects might have happened」。

**H. 效果分类（可参考，未接入编排）**
- `tools/runtime_catalog.py:65-68、86-92`：`ToolEffectClass`（CONTEXT_CONTROL / PROJECT_EFFECT / NON_PROJECT_EFFECT）；`ToolExecutionPolicy` 有 capability_id、capability_fingerprint。
- `workflow/contracts.py:577-610`：`EffectKind`（IDEMPOTENT_READ / DETERMINISTIC_REUSABLE / STAGED_FILE / OPAQUE_MANUAL）；`EffectPolicy` 规定「Opaque effects may be attempted only once」。

**I. 两套同名接口（易混）**
- 消费者口 `runtime/ports.py:141-215`：`AuthorizationRequest(tool_call, run_id, risk_level "low|medium|high")`，结果为 allow/deny/defer，方法 `request_authorization`。
- `agents/ports.py:30-35`：`AllowAllAuthorization`。:89-96 两种口都接受。
- `agents/runtime.py:154-161`：带 `prepare`/`bind_decision` 的原生口原样使用，否则套 `_ConsumerAuthorizationAdapter`。
- `runtime/consumer_adapter.py:165-212` 的适配器：
  - `risk_level` 硬编码为 None（:176）。
  - defer 被映射成 **DENY `user_deferred`**，**永远不会产生 REQUIRE_USER**。
- 另有 `workflows/durable_task/ports.py:245-248` 的 `AuthorizationPort.grant_authorization`，属于工作流领域，只是同名。

**J. 网关调用确实经过 SDK 账本**
- `agents/runtime.py:135-137、149`：`ports.tool_executor` 由 `_ConsumerToolExecutorAdapter`（`runtime/consumer_adapter.py:239-295`）包成 FunctionTool，注册进由 `EffectExecutor` 驱动的 registry（:164-170）。
- 由此推断：编排网关的每次调用都有 SDK effect_id 和账本记录。effect 身份与 run_id/call_id 绑定，这一点来自 `EffectRecord` 字段和 :343 的身份校验。

**K. Outbox**
- SDK 里有 `execution/memory_outbox`（记忆）和 `delivery_outbox`（见 `execution/audit.py:423`），**没有面向外部动作的通用 outbox**。
- 编排侧已有 dispatch intent/outbox（纲要 §4.2 storage 行，交接:35）。

**L. Capability Token**
- 编排/ 中 grep 不到 Capability；SDK 只有 capability_id/fingerprint（目录元数据），**没有短期 Token 实现**（交接:31、:79）。

---

## 6. 歧义与冲突（只列事实，不做决定）

### 6.1 原文与原文之间
1. **事件名不一致**：§22 用 `ApprovalRequested`，§24 时序图（:1796）用 `ApprovalRequired`。
2. **Verifier 结果集合不一致**：§24 第 10 步有 PASS/FAIL/DISPUTED/NEEDS_HUMAN 四种；§25.2 Attempt 状态机只有 PASS/FAIL 两条分支；§25 没有任何等待人工或审批的状态。
3. **风险值域不一致**：§5 示例是 `risk_level: "research"`，§22 是 L0–L3，§26.1 只写了 `string`。另外，§22 的等级描述的是「操作」，§5/§26 的风险挂在 Mission 上。风险该按 Mission 还是按动作定，原文没说。
4. **人工的两种职能**：§14.1「第六层人工审核」判断结果，§22「审批」授权动作。§14.4 冲突流程里没有人工，但 §22 把「多个 Verifier 冲突」列为人工介入场景。
5. **动作 ID 缺位**：§17.4 的唯一 ID 列表里没有动作/效果 ID，§26 也没有 Approval/Action/Capability schema。
6. **Capability Token 无定义**：§21.2「需要审批的短期权限」和 §21.3「短期 Capability Token」都没有格式、有效期或绑定对象的规定。
7. **审计日志格式未定义**：§21.1 和 §30 都要求审计日志，格式都没有定义。

### 6.2 原文与纲要之间
8. **撤权/过期没有原文事件**：S7-04 和 permissions 行要求「撤权/过期/有效期」，原文 §22 的 5 个事件里没有对应项。新增事件或决定状态需要按纲要 §13 登记。
9. **L3 双重审批的身份未定**：原文未规定。纲要:422 约定为「两个独立审批记录、禁止同一个审批回执重复计数」，是否要求不同自然人交给部署政策。
10. **Mission 缺"等待人工"状态**：纲要 §13 要求 Mission 能表达「等待人工/资源」，原文 §26.1 没有枚举，现有代码也没有这个状态。
11. **原文依据范围不一**：纲要 §9 的原文依据写的是 §14.4，没有提 §14.1 第六层；而 §9.2 human_review 行处理的是 NEEDS_HUMAN（出自 §24）加 Verifier 冲突。
12. **补偿不在范围内**：纲要 §12.6 规定「不能通过恢复旧数据库来"撤销现实"」，但第 7 步没有要求补偿动作；理论 13 §12 的「补偿」在纲要第 7 步里没有对应交付物。
13. **测试环境不存在**：纲要 §9.1 要求「演示先使用专门的测试服务/测试环境」，仓库里没有这样的外部测试服务或连接器（网关只有工作区工具）。

### 6.3 纲要与现有代码之间
14. **纲要 §9.2 列出的 4 个模块文件都不存在**：`api/approvals.py`、`verification/human_review.py`、`governance/permissions.py`，以及 `governance/secrets.py`（现在在 `observability/secrets.py`）。
15. **现有授权装配走不了持久人工等待**：
    - 编排用的是 `AllowAllAuthorization`，属于消费者口。
    - 消费者适配器把 defer 映射成 DENY，并且 `risk_level=None`。
    - 要走持久等待，需要改用 SDK 原生口（`prepare`/`bind_decision`/`bind_effect_handoff`）。
16. **决策入口不在编排能依赖的公共面上**：
    - 决定待审授权的入口 `RunClient.decide_authorization` 在 `simple_harness` 顶层导出，但不在 `simple_harness.agents` 公共面。
    - program:47 规定编排只依赖 `simple_harness.agents` 公共 API。
    - 纲要 §12.2 末条允许「增加一个最窄的……适配扩展」，但那一条讲的是预算/工具额度，能否类推到授权要单独判断。
17. **审批层级未定**：纲要 §9.1 描述的是「先产生可审阅候选 → 审批 → 只执行获批内容」，也就是 turn 之外的编排层审批。SDK 的 REQUIRE_USER 是在 turn 中途暂停 Run。审批落在哪一层，或两层怎么组合，没有定论。
18. **SDK effect_id 不能直接当业务动作 ID**：纲要 §12.3 要求「跨Attempt的同一个现实动作需要稳定业务动作ID」。SDK effect 身份与 run/call 绑定（由代码推断），新 Attempt 会开新的 Agent/Run。
19. **审计记录离 30-23 有差距**：「所有真实世界副作用都有审计日志和幂等键」，现有网关的 `tool_calls` 表没有参数 hash 和幂等键列，`self.calls` 只在内存里。
20. **UNKNOWN 对账是空实现**：纲要要求 UNKNOWN 进入核对（S7-06），SDK 默认的 `_DefaultToolReconciliation` 永远 STILL_UNKNOWN，交接 L6-4 也写着「只做到可见」。
21. **Verifier 挂起能力缺失**：`VerifierRouter.verify` 同步且结果只有二值；human_review 需要挂起、跨重启恢复，还要能表达 NEEDS_HUMAN。原文 §24 与 §25 之间本身就有缺口（见第 2 条）。
22. **风险词汇不一致**：现有默认值 `risk_level="sandbox"` 不在 L0–L3 词汇里；Task Contract 没有风险字段；Result Envelope 的 `risks` 是 Agent 自报文本，按 §21.3 不能影响权限。
23. **Host 产品决定可能冲突（需确认适用范围）**：Host 仓库 `simple_harness/CLAUDE.md` 的用户决定写着「权限模式只有 manual 与 auto 两种，默认 auto。auto 模式下不弹任何授权提示，所有工具效果默认允许执行」。这是 DeskPet Host 的规则。如果编排将来接进 Host，会与 L2/L3 必须人工审批相冲突；纲要和原文都没有提到这个关系。

---

## 摘要

1. 第 7 步的范围：纲要 §9 的 6 个模块、S7-01～S7-08，外加原文 §30 的 03、22、23 三项；完成标准是「候选→审批→执行→核对」完整闭环。
2. 原文 §22 提供了 L0–L3 分级、8 类人工介入场景和 5 个人工事件，但没有审批 schema、没有撤权/过期、没有等待状态；§24 用的 `ApprovalRequired` 与 §22 的 `ApprovalRequested` 不一致。
3. 理论/ 只定义了幂等性（09 §10）、回滚/补偿（13 §12）、最小权限和注入防护（13 §14/§15）；Human-in-the-loop、审批、副作用、Dry-run、风险等级、Capability Token、Outbox 在理论/ 中都查无定义。
4. 编排现状：
   - human_review 层「提交期拒绝、运行期 ERROR 阻塞」；`risk_level` 是自由文本，默认 "sandbox"，不驱动任何政策。
   - 网关只有 4 个工作区工具，没有审批检查和业务动作 ID；SDK 授权层用的是 AllowAll。
   - 纲要要求的 `approvals.py`、`human_review.py`、`permissions.py` 都不存在。
5. SDK 已有可复用的完整底座：REQUIRE_USER 持久决策、nonce/版本/过期栅栏、双方回执哈希链、handoff 二次绑定、effect 账本和对账端口，第 7 步不应另造。
6. 两个接入障碍：
   - 消费者授权适配器永远不产生 REQUIRE_USER。
   - `decide_authorization` 不在 `simple_harness.agents` 公共面上，而 program 纪律要求编排只依赖这个公共面。
7. 需要先定下来的歧义：审批放在 turn 外的编排层还是 turn 内的 SDK 层；跨 Attempt 的业务动作 ID 怎么定义；Mission 缺的"等待人工"状态；L3 双重审批的身份规则；真实测试连接器从哪来。另外 Host 的 auto 权限决定可能与本步冲突，需要确认适用范围。
