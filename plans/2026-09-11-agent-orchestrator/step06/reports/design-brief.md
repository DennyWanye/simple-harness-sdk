<!-- 独立提取（claude-opus-5，只读）；由主会话从代理回复落盘，内容未改 -->

# 第 6 步需求简报

来源文件（均在 `/Users/taiwan/PROJECTS/SimplaHarness/simple_harness/plans/taskSys2/`）：
- 纲要：`agent-orchestrator-incremental-build-plan-phase2-zh-CN.md`（§8 / §12 / §13 / §2 / §1.2 / 附录覆盖矩阵）
- 设计原文：`agent-orchestration-layer-complete-design.md`
- 术语目录：`agent-orchestration-theory/`

---

## 1. 范围（纲要 §8 原句 + 模块表逐行）

**标题**：`## 8. 第 6 步：多 Mission、多模型、背压和隔离运行`
**原文依据行（照抄）**：「原文依据：§8—10、§17—21、§23、§28第三阶段、§29。」

**§8.1 可独立使用的新功能（原句）**：
> 同时启动多个Mission，按任务类型选择真实模型、给不同Mission配额；Worker过快导致验证积压时自动减速；不同Attempt在独立Workspace工作；所有动作、成本和上下文版本能在完整Trace中关联。

> 本步扩大的是"受控制的并发"，不是仅把max_agents改大。第一版以原文§29的10—20个Worker、2个Verifier Worker作为可配置目标拓扑；若部署承载不足，应降低实际并发并报告测试结果，不将原文建议数量视为质量保证。

**§8.2 模块表（逐行照抄）**：

| 模块 | 新增闭环能力 |
|---|---|
| `scheduling/backpressure.py` | 运行、待处理、待验证、Task深度、单Task Attempt、单Agent子任务建议数上限；上下游高低水位，恢复有滞回 |
| `scheduling/allocator.py` | Verifier积压→减少Worker/增加验证资源/暂停低价值扩展；保持等待老化和探索额度 |
| `runtime/model_router.py` | profile→实际Provider/model/tokenizer/context policy/价格/能力映射；每次调用冻结实际目标并纳入Trace |
| `runtime/agent_worker.py` | 支持按路由选执行池；不能多个不兼容Runtime同时恢复同一个Agent；runtime_profile_id必须持久绑定 |
| `artifacts/workspace.py` | 独立可写目录/沙箱与只读依赖产物；合并通过新候选与再验收，不共享可写主目录 |
| `runtime/tool_gateway.py,governance/*` | Mission/Task/Role/部署政策的权限交集；工具参数、速率、资源和费用检查；外部文本不能提升权限 |
| `governance/budgets.py` | Global→Mission→Task→Attempt配额和所有真实调用费用关联；资金分配与最终计费不重复相加 |
| `verification/verifier_router.py` | 根据verification_policy运行多层验证；保留分层结果/版本；未部署的必需验证器导致阻塞而非假通过 |
| `observability/*` | 完整版本字段、队列/延迟/费用/复用/验证率、日志/指标/Trace，能从最终产物回溯来源 |

**§8.2 表后约束（原句）**：
> 当前基础类在Runtime上注入provider/model，并不能仅凭AgentConfig.model_profile_ref就证明物理路由正确。可以按模型配置组建不同Runtime池，但必须分开SDK存储/明确恢复分区，并持久绑定每个Attempt目标池；不能盲目让不同模型的Runtime打开同一execution.db恢复全部Agent。

**§8.3 原文角色配比与动态调整（原句）**：
> 注册原文的比例起点：Explorer20%、Exploiter40%、Critic20%、Synthesizer10%、Verifier10%。这些是可调整的起始资源建议，不要求每个时刻严格凑成该比例，更不能覆盖Task所需的强制Verifier和权限限制。

> 早期增加探索；中期增加深化和Connector；临近交付增加Critic/Synthesizer/Verifier；积压时减少Worker、增加验证资源。多层Manager是原文的大系统可选形态，本版本优先保持单Manager/合并Search Controller；只有规模评测显示必要时再配置组级管理，不把它设为前置。

**§8.4 交付物（原句）**：
> 拟交付命令：`python -m agent_orchestrator demo --scenario multi-mission --provider fixtures --evidence-dir evidence/s6`；另交付真实多模型小规模负载报告。测试目录 `tests/orchestrator/step06/`。
> **完成标准：多个任务能同时、安全、有上限地运行；背压和物理模型路由在可观察场景中实际生效。**

---

## 2. 验收 S6-01…S6-09（原表照抄）

| ID | 场景 | 必须观察到的结果 |
|---|---|---|
| S6-01 | 同时运行两个Mission | 均有进展，配额不互相挪用，跨Mission资料隔离 |
| S6-02 | 人为放慢Verifier | 验证队列有界；Worker并发下降；停止低优先扩展；恢复后逐渐放开 |
| S6-03 | 小模型失败后升级 | Trace中实际provider/model改变，保留先前Attempt与费用，而不是只改标签 |
| S6-04 | 两个Attempt写同名文件 | 位于不同Workspace；无覆盖；正式版本只经验证后提交 |
| S6-05 | 模型索要额外工具或读取其他工作目录 | 网关拒绝；Prompt和模型置信度不能授权 |
| S6-06 | 一个模型服务不可用 | 相应工作有界等待/明确降级；其他可执行Mission继续 |
| S6-07 | Token/工具/运行时间某一维耗尽 | 停止新分配；已发生费用和在途预留仍保留正确 |
| S6-08 | 重启多Runtime执行池 | 旧Attempt回到原目标配置，不能被另一模型静默接管 |
| S6-09 | 日志检查 | 无密钥；所有Result可追到prompt/model/retrieval/allocator/verifier版本 |

**纲要附录中映射到第 6 步的原文 §30 验收项**（编号照抄，"步骤"列含 6）：
- ORIGINAL-30-12「Mission、Task 和 Attempt 都有预算；」→ S6-07
- ORIGINAL-30-15「有并发和队列上限；」→ S6-02
- ORIGINAL-30-16「Verifier 积压时会触发 Backpressure；」→ S6-02
- ORIGINAL-30-18「工具调用统一经过 Tool Gateway；」→ S6-05
- ORIGINAL-30-19「Agent 只拥有完成任务所需的最小权限；」→ S6-05
- ORIGINAL-30-20「密钥不进入模型上下文；」→ S6-09
- ORIGINAL-30-21「外部内容被标记为不可信数据；」→ S4-08, S6-05
- ORIGINAL-30-24「每个 Mission、Task、Attempt 和 Result 有 Trace ID；」→ S6-09
- ORIGINAL-30-25「能查看每个任务的完整事件链；」→ S8-01, S8-05（步骤含 6）
- ORIGINAL-30-26「能统计成本、成功率、重复率和验证通过率；」→ S8-03（步骤含 6）
- ORIGINAL-30-29「Prompt、模型、Retrieval 和 Allocator 都有版本号。」→ S8-07, S9-04（步骤含 6）

---

## 3. 设计原文规则清单（按章节）

### §8 Frontier、Allocator 与 Scheduler

| # | 章节 | 规则（保留原文关键短语） | 涉及字段/事件/阈值 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.1 | §8.1 | 「Frontier 是当前满足依赖、尚未完成、值得继续处理的任务集合。」 | frontier 集合 | 背压要能"暂停低价值扩展"，必须先能界定 Frontier 与其价值序 |
| 3.2 | §8.2 | Allocator 决定：「处理哪些任务；每个任务给多少 Agent；使用哪些角色；使用什么模型；分配多少 Token、时间和工具预算。」 | task, agent_count, role, model, token/time/tool budget | Allocator 输出里必须含"使用什么模型"→ 与 model_router 的 profile 是同一条决策链 |
| 3.3 | §8.2 | 优先级公式：「Priority = 重要性 + 解锁价值 + 当前有希望程度 + 未探索程度 + 等待时间 − 预计成本 − 结果重复度 − 风险 − **下游积压惩罚**」 | waiting_age（等待老化）、下游积压惩罚 | "下游积压惩罚"是背压进入 Allocator 的原文接口；"等待时间"对应纲要要求的"保持等待老化" |
| 3.4 | §8.2 | 分配示例形如「Task D：Exploiter×6 / Critic×2 / Verifier×1 / Token Budget = 1.5M」 | 角色×数量 + token budget | 分配单元是"角色配额 + 预算"，不是单一 max_agents 数字 |
| 3.5 | §8.3 | Scheduler 负责：「哪些现在启动；哪些进入等待队列；在哪个 Worker 或 GPU 上运行；谁超时；谁需要重试；任务被别人完成后取消哪些冗余 Attempt。」 | 队列、执行位置、超时、重试、取消冗余 Attempt | 「在哪个 Worker 上运行」= 本步"按路由选执行池"的原文落点 |
| 3.6 | §8.3 | 「Allocator = 给多少；Scheduler = 什么时候、在哪里执行」 | — | 背压既要改 Allocator 的"给多少"，也要改 Scheduler 的"此刻跑多快"，两者不可混为一处 |

### §9 Role 与 Model Router

| # | 章节 | 规则 | 涉及字段/事件/阈值 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.7 | §9.1 | 「如果 20 个 Agent 拿到完全相同的 Prompt、上下文和策略，它们很可能产生高度相似的答案。」要制造「**搜索多样性**」 | 结果重复率 | 扩并发时必须同时扩多样性，否则只是放大重复 |
| 3.8 | §9.2 | 推荐角色 8 个：Explorer / Exploiter / Critic / Simplifier / Connector / Failure Analyst / Synthesizer / Verifier（职责表见原文） | role | §8.3 配比只列 5 类（Explorer/Exploiter/Critic/Synthesizer/Verifier），Connector、Simplifier、Failure Analyst 无百分比 |
| 3.9 | §9.2 | 「Role 不是职位名称装饰，而是一种 **搜索偏置**。」 | role template | role 必须影响 prompt/上下文可见性，不能只是标签 |
| 3.10 | §9.3 | 模型路由映射：「简单分类、格式转换 → 小模型；批量去重、摘要 → 快速长上下文模型；任务拆解、路线判断 → 强推理模型；代码、形式化证明 → 代码或领域专用模型；最终关键候选 → 最强模型；确定性验证 → 程序、测试、Lean」 | 任务类型 → model class | model_router 的映射表须按"任务类型"而非只按 role |
| 3.11 | §9.3 | 升级策略：「便宜模型先尝试 → 失败或低置信 → 换更强模型 → 仍失败 → 拆任务、换策略或人工介入」 | 置信度、失败信号、retry_of | S6-03 的直接依据；"仍失败→拆任务/人工"跨到第 5/7 步 |

### §10 Context Builder 与 Retrieval

| # | 章节 | 规则 | 涉及字段/事件/阈值 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.12 | §10 | 每次创建 Agent 组装 11 项：「1 Mission 根目标 2 当前 Task Contract 3 父任务和直接依赖 4 当前分支摘要 5 相关 Verified Knowledge 6 相关失败历史 7 有争议的 Claim（必须标记）8 Verifier 最近反馈 9 **可用工具与权限** 10 **Token、时间和调用预算** 11 结构化输出要求」 | 第 9、10 项 | 权限与预算是上下文的一部分 → 权限交集与配额结果必须体现在任务包里；上下文版本要入 Trace（retrieval_version） |
| 3.13 | §10.1 | 检索综合：「语义相关性 + Task DAG 距离 + 知识可信等级 + 知识新旧 + 分支相关性 + 历史复用价值 − 重复内容 − 已被取代内容」 | retrieval_version | 多 Mission 下检索范围必须按 Mission 隔离（S6-01"跨Mission资料隔离"） |
| 3.14 | §10.2 | 默认可见性：「Worker 默认只把 Verified Knowledge 当成事实；Explorer 可以看到低可信度的新想法，但必须明确标记；Critic 应看到候选结论、失败记录和反对证据；Verifier 应尽量独立，不应只看到原作者的自我解释；**密钥、隐藏权限和不必要的敏感数据不得进入模型上下文**」 | 可见性策略 | 最后一条是 S6-09"无密钥"的上游规则；"Verifier 应尽量独立"约束 verifier_router 的上下文装配 |

### §17 并发、冲突与幂等

| # | 章节 | 规则 | 涉及字段/事件/阈值 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.15 | §17.1 | 「只有当 Attempt.status == PENDING 时才允许改成 CLAIMED」，「使用 Compare-and-Swap 或数据库原子更新」 | Attempt.status, CAS | 多执行池并存时领取仍必须单点原子，S6-08 的"不能被另一模型静默接管" |
| 3.16 | §17.2 | 「多 Agent 同时探索同一个 Task 是允许的」但「一个 Attempt 只能有一个执行者」 | attempt_id ↔ 执行者 | 同 Task 多 Attempt → 必须多 Workspace（S6-04） |
| 3.17 | §17.3 | 「基于旧版本提交的 Proposal 必须重新检查或合并，避免 Lost Update」 | Graph Version | 高并发下版本冲突频率上升，本步需实测 |
| 3.18 | §17.4 | 唯一 ID：「event_id / attempt_id / allocation_id / result_id / commit_id」；「同一事件重复到达，不会重复创建 Agent、**重复扣费**或重复触发下游任务」 | 五个 ID、idempotency_key | 「重复扣费」直接约束 budgets 与 usage_ref 导入 |
| 3.19 | §17.5 | Single Writer 管辖：「Mission 状态 / Task 状态 / 正式 Knowledge / **全局 Budget** / Task DAG 主版本」；「Agent 只能提交 Proposal」 | 全局 Budget | 多 Mission 配额写入必须走同一 Commit Service，不能各 Mission 各写 |
| 3.20 | §17.6 | 「Agent 周期性发送 Heartbeat。心跳停止后：Lease 过期 → Attempt 标记 LOST → 任务重新调度」 | lease_owner, lease_expires_at | 与纲要 §12.1 的"SDK 执行租约 vs 业务租约"两层配合，S6-08 重启场景 |

### §18 Budget、Cost 与 Backpressure

| # | 章节 | 规则 | 涉及字段/事件/阈值 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.21 | §18.1 | 「预算可以包括：Token / Agent 数量 / 并发数量 / GPU 时间 / 总运行时间 / 工具调用次数 / 搜索次数 / 真实费用」 | 8 个维度 | S6-07"某一维耗尽"→ 配额必须是多维向量而不是单一 token 数 |
| 3.22 | §18.2 | 「Global Budget ├ Mission A Budget │ ├ Task A1 └ Mission B Budget」；「**子任务预算来自父任务，不得凭空放大**」；示例「Task A：10M；A1:4M A2:3M A3:2M 预留:1M」 | Global→Mission→Task→Attempt | 与模块表 budgets.py 完全对应；"预留"是显式的一档 |
| 3.23 | §18.3 | 「Reserve 最大预算 → 启动 Agent → 记录实际 Cost → Settle 结算 → 释放未使用预算」；「避免并发 Agent 同时超支」 | budget_reserved, settle | S6-07"已发生费用和在途预留仍保留正确" |
| 3.24 | §18.4 | 每次 Attempt 记录：「模型 / 输入 Token / 输出 Token / 工具调用 / 运行时间 / GPU 时间 / 结果状态 / 是否产生可复用知识 / 是否进入最终成功路径」 | cost 归因字段 | 最后两项与 §23.3 Lineage 打通；"模型"字段是 S6-03 的证据 |
| 3.25 | §18.5 | 「当下游处理不过来时，上游必须减速。」示例「Worker 每分钟提交 1000 个结果 / Verifier 每分钟只能处理 50 个」 | 吞吐比 | S6-02 的原型场景 |
| 3.26 | §18.5 | 系统应：「降低 Worker 并发；提高 Verifier 资源；暂停低优先级任务；禁止新任务继续分裂；合并重复候选；缩小每个 Attempt 预算。」 | 6 个降速动作 | 纲要 allocator 行只列了 3 个（减少Worker/增加验证资源/暂停低价值扩展），原文共 6 个 |
| 3.27 | §18.5 | 需要设置：「最大运行 Agent 数 / 最大等待任务数 / 最大待验证结果数 / 单 Task 最大 Attempt 数 / 最大 Task DAG 深度 / 单 Agent 最大子任务 Proposal 数」 | 6 个上限阈值 | 与 backpressure.py 行一一对应（运行/待处理/待验证/单Task Attempt/Task深度/单Agent子任务建议数） |
| 3.28 | §18.6 | 「系统不能只看调用次数，应看：是否出现新思路；是否产生可验证 Lemma；是否减少不确定性；是否解锁关键任务；是否找到明确失败原因；结果重复率是否下降。」 | 进展信号 6 项 | 并发扩大后判断"有进展"的判据，S6-01"均有进展"的定义来源 |

### §19 停止、停滞、死锁与目标漂移

| # | 章节 | 规则 | 涉及字段/事件/阈值 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.29 | §19.1 | 停止条件：「Verifier PASS；预算耗尽；连续多轮无新知识；结果重复率过高；新增 Agent 边际价值很低；所有高价值 Frontier 已处理；人工决定停止。」 | stop reason | 多 Mission 下每个 Mission 独立判停；"预算耗尽"对应 S6-07 |
| 3.30 | §19.2 | 停滞检测「例如连续 5 轮：没有新节点；没有新 Knowledge；Verifier 分数无提升；结果相似度超过 90%」；可采取「**换模型**；换角色；重新组装上下文；派 Failure Analyst；拆小任务；暂停或终止分支」 | 5 轮 / 90% 阈值 | 「换模型」把停滞检测与 model_router 升级策略接起来 |
| 3.31 | §19.3 | 「每次添加依赖边时必须做环检测」 | 环检测 | 第 3/5 步已有，本步在并发下仍须成立 |
| 3.32 | §19.4 | Starvation：「等待越久，优先级逐渐增加」 | waiting_age | 纲要 allocator 行"保持等待老化和探索额度"的原文出处 |
| 3.33 | §19.5 | Goal Drift：每个 Task 必须说明「它和根目标有什么关系？它会解锁什么？为什么值得继续？」；「无法解释的任务应降级、暂停或删除」 | parent_goal 说明 | 注意与纲要 §13"§25 无 PAUSED，不给 Task 私自新增 PAUSED 终态、不物理删除审计历史"的约定冲突 |

### §20 Workspace 与 Artifact

| # | 章节 | 规则 | 涉及字段/事件/阈值 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.34 | §20.1 | 「多个 Agent 不应直接共享同一个可写目录」；「Attempt 1 → Workspace A / Attempt 2 → Workspace B / Attempt 3 → Workspace C」；「这可以避免文件覆盖和环境污染」 | attempt_id → workspace | S6-04 的直接依据；粒度是 **per-Attempt**，不是 per-Task |
| 3.35 | §20.2 | Artifact 管理「代码 / 补丁 / 证明文件 / 数据集 / 实验结果 / 日志 / 图表 / 模型输出 / 数据库变更计划」；schema 字段：`id, mission_id, task_id, attempt_id, type, workspace, version, content_hash(sha256), produced_by, verification_status, storage_uri` | artifact schema 11 字段 | workspace 字段必须持久化到 artifact；content_hash 支撑"无覆盖"证明 |
| 3.36 | §20.3 | 合并过程：「多个独立 Artifact → 测试和验证 → 选择或 Synthesizer 合并 → 生成新的 Candidate Artifact → **再次验证** → Commit 为正式版本」 | candidate → verify → commit | 纲要"合并通过新候选与再验收，不共享可写主目录"即此；S6-04"正式版本只经验证后提交" |

### §21 工具、安全与权限

| # | 章节 | 规则 | 涉及字段/事件/阈值 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.37 | §21.1 | 「Agent 不直接访问真实系统。所有工具调用通过统一网关」，顺序为「身份和权限检查 → 参数 Schema 检查 → 风险与政策检查 → **速率和预算检查** → 执行工具 → 记录结果与审计日志」 | 6 段检查链 | 纲要"工具参数、速率、资源和费用检查"即第 2/3/4 段；**顺序是原文规定的** |
| 3.38 | §21.2 | 最小权限：「只读研究 Agent → 只读权限；代码 Agent → 沙箱文件系统；数据库分析 Agent → 只读副本；生产部署 Agent → 需要审批的短期权限；财务 Agent → 不能直接支付」 | role → 权限档 | role 是权限交集的一个输入维度（纲要写作 Mission/Task/Role/部署政策四者交集） |
| 3.39 | §21.3 | 「外部网页、邮件、文档和其他 Agent 消息都可能是不可信内容」，需要：「明确区分"指令"和"数据"；**外部内容不能改变系统权限**；密钥不进入模型上下文；使用短期 Capability Token；对 Blackboard 写入做验证和来源标记；高风险工具调用使用独立政策检查；对可疑指令进行隔离和审计」 | 7 条 | S6-05 的直接依据；「短期 Capability Token」在纲要模块表中未出现 |

### §23 Observability、Tracing 与 Evaluation

| # | 章节 | 规则 | 涉及字段/事件/阈值 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.40 | §23.1 | 「Logs = 一件件发生了什么；Metrics = 整体健康和效果如何；Trace = 某个任务从头到尾经历了什么」；每个对象应包含：`trace_id, mission_id, task_id, attempt_id, agent_id, model_version, prompt_version, retrieval_version, allocator_version, verifier_version` | **10 个字段** | S6-09"所有Result可追到prompt/model/retrieval/allocator/verifier版本"；注意原文还要求 agent_id 与 trace_id |
| 3.41 | §23.2 | 监控指标 6 类：系统健康「并发数、队列长度、超时率、工具错误率」；成本「Token、时间、工具费用、每成功任务成本」；搜索质量「新思路数、分支剪枝率、结果重复率」；知识质量「Claim 验证率、污染率、**复用率**」；验证质量「PASS 率、误报率、**积压量**」；最终效果「Mission 成功率、完成时间、稳定性」 | 22 个具体指标 | 纲要 observability 行"队列/延迟/费用/复用/验证率"是此表的子集；"积压量"是背压的观测量 |
| 3.42 | §23.3 | 「需要记录哪些知识位于最终成功路径上」 | lineage / 成功路径标记 | 与 §18.4「是否进入最终成功路径」同一字段；纲要"能从最终产物回溯来源" |
| 3.43 | §23.4 | Evaluation 四法：Offline Evaluation / A/B Test / Ablation / Replay；「不要只看"Agent 数量"和"消息数量"，要看是否真的提高：成功率；验证通过率；知识复用；成本效率；任务完成速度；系统稳定性」 | — | 第 6 步只需产出可供后续评测的 Trace；Replay/A-B 属第 8 步 |

### §28 第三阶段

| # | 章节 | 规则 | 涉及 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.44 | §28 第三阶段 | 增加：「Backpressure / 多模型路由 / Workspace 隔离 / Tool Gateway / 细粒度权限 / **Human-in-the-loop** / 多 Mission 配额 / 完整 Trace / 多层 Verifier」；目标：「在较高并发下仍然安全、可控、可追踪。」 | 9 项 | 9 项中有 8 项在第 6 步，**Human-in-the-loop 被纲要拆到第 7 步** |

### §29 第一版推荐配置

| # | 章节 | 规则 | 涉及 | 对第 6 步的含义 |
|---|---|---|---|---|
| 3.45 | §29.1 | 逻辑组件：「1 个 Orchestrator / 1 个 Planner-Manager / 1 个简单 Allocator / 1 个 Scheduler / 1 个 Context Builder / 1 个 Blackboard / 1 个 Synthesizer / **2 个 Verifier Worker** / **10～20 个 Agent Worker**」 | 目标拓扑 | §8.1 明确它是"可配置目标拓扑"，承载不足应降低并报告 |
| 3.46 | §29.2 | 角色比例起点「Explorer 20% / Exploiter 40% / Critic 20% / Synthesizer 10% / Verifier 10%」；动态调整「早期：Explorer 增加；中期：Exploiter 和 Connector 增加；接近完成：Critic、Synthesizer、Verifier 增加；**下游积压：Worker 减少，Verifier 增加**」 | 比例配置 + 4 条调整规则 | §8.3 逐字沿用；末条是背压与角色配比的耦合点 |
| 3.47 | §29.3 | 优先级规则：「priority = 0.30×mission_importance + 0.20×unlock_value + 0.15×progress_signal + 0.15×uncertainty + 0.10×waiting_age − 0.05×estimated_cost − 0.05×duplication_score」；「这只是起点。参数应通过 Evaluation 调整。」 | 7 项加权 | 该式**不含** §8.2 的"风险"和"下游积压惩罚"两项 |

### 本步会用到的其他原文条款（支撑性，非主章节）

| # | 章节 | 规则 | 对第 6 步的含义 |
|---|---|---|---|
| 3.48 | §14.1 | 分层验证六层：「Schema/格式检查 → 确定性规则检查 → 独立 Critic → 测试、模拟或实验 → 形式化验证 → 必要时人工审核」 | verifier_router 按 verification_policy 选层的层枚举 |
| 3.49 | §14.2 | 领域验证器表（代码/数学/SQL/网页/科学实验/企业流程/文档） | "未部署的必需验证器导致阻塞而非假通过"需要按领域声明可用性 |
| 3.50 | §26.3 | Attempt Schema：`id, task_id, role, model, prompt_version, context_version, budget_reserved, lease_owner, lease_expires_at, status, retry_of` | 原 schema 有 `model` 但**没有** runtime_profile_id / workspace 字段——本步要扩展 |
| 3.51 | §26.6 | Event Schema：`id, type, trace_id, mission_id, task_id, attempt_id, actor_type, actor_id, payload, idempotency_key, created_at, schema_version` | 背压/路由/配额事件都要落在这个信封里 |
| 3.52 | §26.4 | Result Envelope：`id, task_id, attempt_id, outcome, summary, claims, evidence, artifacts, proposed_tasks, used_knowledge, risks, cost` | `cost` 字段与 SDK usage_ref 的关系见纲要 §12.2（模型自填 cost 只作待核对信息） |

---

## 4. 术语定义（文件名 · 术语 · 定义摘录）

### `agent-orchestration-theory/11_budget_cost_backpressure.md`

- **Budget / Cost / Backpressure（一句话定义）**：「Budget：最多允许花多少资源。Cost：已经花了多少资源。Backpressure：下游处理不过来时，让上游减速。」
- **Budget 与 Backpressure 的区别（第 10 节）**：「Budget = 整个任务总共最多花多少；Backpressure = 当前这一刻最多能跑多快。总预算还有很多，不代表此刻可以无限并发。」
- **Budget 维度（第 2 节）**：「输入和输出 token / Agent 总数 / 并发 Agent 数 / GPU 时间 / 墙钟时间 / 工具调用次数 / 搜索次数 / 真实费用 / **验证次数**」（比设计原文 §18.1 多"验证次数"）。示例 YAML：`max_agents / max_tokens / max_duration_minutes / max_search_calls / max_retries`
- **分层预算（第 3 节）**：「Global Budget ├ Mission A Budget（├ Task A1 Budget └ Task A2 Budget）└ Mission B Budget……分层预算防止一个局部任务吃光整个项目资源。」
- **Budget Inheritance 预算继承（第 16 节）**：「父任务有 10M token：A1:4M A2:3M A3:2M 保留:1M。子任务预算来自父任务，而不是凭空增加。」
- **动态追加和回收预算（第 17 节）**：「当 A2 出现突破：从低价值任务回收 5M，追加给 A2。这类似投资组合再平衡。」
- **Agent 不能自我复制（第 4 节）**：「提交 Resource Request → Allocator 检查价值和剩余预算 → 批准、部分批准或拒绝 → Scheduler 实际创建 Agent」
- **Cost Accounting 成本归属（第 5 节）**：每个 Attempt 记录「task_id / attempt_id / model / input_tokens / output_tokens / tool_calls / duration / GPU time / money_cost / result_status」；「系统不仅要知道总成本，还要知道钱花在哪个方向、角色和模型上。」
- **Backpressure（第 9 节，餐厅类比）**：「前台每分钟接 100 单，厨房每分钟只能做 10 单……厨房必须要求前台减速，否则队列无限增长。」「1000 个 Worker 每分钟提交 1000 个结果，Verifier 每分钟只能处理 50 个。继续增加 Worker 只会制造积压。」
- **Concurrency Limit 与 Queue Limit（第 11 节）**：并发上限「最多同时运行 100 个 Agent」；队列上限「最多积压 1000 个等待任务」；「到达上限后，需要暂停低优先级任务或拒绝新任务。」
- **Admission Control 准入控制（第 12 节）**：「并不是 Agent 提出的每个子任务都必须进入系统。创建新任务前应说明：为什么需要它？它解锁什么？是否和现有任务重复？怎样判断完成？预计成本是多少？」
- **过载时的降速手段（第 13 节，8 项）**：「降低并发；暂停低优先级任务；暂停任务继续分裂；缩小单个 Agent 的预算；合并重复候选；优先处理高分结果；**增加 Synthesizer 或 Verifier**；提高知识压缩频率。」
- **瓶颈不一定是 Worker（第 14 节）**：「Worker 很快 / Synthesizer 跟不上 / Verifier 严重积压」→「减少 Worker / 增加 Synthesizer / 增加 Verifier」；「系统吞吐量由最慢环节决定。」
- **防止任务无限分裂（第 15 节）**：「最大图深度 / 单节点最大子任务数 / 单 Agent 最大 Proposal 数 / **未经 Manager 审核的节点不能继续分裂**」
- **Activity without Progress（第 6 节）**与**进展信号（第 7 节）**：「新的非重复路线；新的可验证 Lemma；明确的失败原因；Verifier 分数提升；关键依赖被解决；不确定性显著下降；产生可跨分支复用的知识。连续多轮无新进展，应降低预算或停止。」
- **Marginal Value 边际价值（第 8 节）**：「第 1 个 Agent：发现新路线……第 20 个 Agent：重复已有答案……任务再重要，也不代表无限增加 Agent 都有价值。」
- **本章小结**：「资源必须由系统分配，不能由 Agent 自我复制；成熟编排既要限制总成本，也要根据流水线瓶颈动态控制当前速度。」

> **注意**：全部 theory 目录中**没有出现"高低水位 / 滞回 / hysteresis"字样**（grep 无命中）。纲要模块表里的"上下游高低水位，恢复有滞回"是纲要新增的实施表述，最接近的原文依据是本文件第 11 节并发/队列上限与设计原文 §18.5。

### `agent-orchestration-theory/05_allocator_scheduler.md`

- **一句话定义**：「Allocator 决定"给谁多少资源"。Scheduler 决定"谁在什么时候、在哪里运行"。」
- **四个维度（第 2 节）**：Importance 重要性「任务完成后，对根目标能产生多大影响？」；Promise 有希望程度「目前是否已经出现可验证进展？」；Uncertainty 不确定性「这个方向是否尚未被充分探索？」；Cost 成本「需要多少 token、时间、GPU、工具调用和验证资源？」
- **Allocator 的完整资源方案（第 4 节）**：「task / agent_role / agent_count / model / context_budget / token_budget / time_limit / tool_limit / verification_budget」；示例 `allocation: explorers:4 exploiters:12 critics:3 verifiers:2 max_tokens:5000000`
- **Scheduler 的工作（第 5 节）**：「哪一批先启动；哪台机器执行；哪个任务进入等待队列；超时后是否重试；**Agent 失联后由谁接管**；依赖满足后何时解锁。」
- **Dynamic Scheduling（第 7 节）**：Blackboard 新增验证通过的 Lemma 后「A2、C7、F3 被解锁，优先级立即上升」；「Verifier 失败也可能触发：降低原路线优先级；创建修复任务；增加 Critic。」
- **Allocator 与 Manager 的区别（第 8 节）**：「Manager：从自己负责的任务出发，提出"这里需要更多资源"。Allocator：从全局预算出发，决定实际能分配多少。」
- **Diversity-aware Allocation（第 9 节）**：「多个 Agent 研究同一任务时，不应全部使用同一策略……对于已尝试很多次的低价值路线，可以保留少量"反常规"Agent，而不是彻底删除，以避免过早剪枝。」
- **Starvation 任务饥饿（第 10 节）**：「如果系统永远只运行高优先级任务，低优先级任务可能永远得不到资源。常见处理方法：保留固定探索预算；等待时间越长，优先级逐渐提高；为不同任务类别设置最低资源份额；定期检查长期未运行的节点。」（= 纲要"保持等待老化和探索额度"的定义原文）

### `agent-orchestration-theory/07_control_roles.md`

- **一句话定义**：「Planner 决定做什么。Manager 决定如何持续推进。Allocator 决定给多少资源。Scheduler 决定何时、在哪里运行。Orchestrator 保证整个系统协调运转。」
- **Allocator：分配蛋糕（第 3 节）**：处理「哪个任务得到多少 Agent？使用哪个模型？给多少 token 和时间？**各类角色比例是多少？**」；「Manager 可以提出资源需求，Allocator 决定是否批准。」（"角色配比"的定义出处）
- **Scheduler（第 4 节）**：「哪个任务现在启动；哪台机器运行；谁进入等待队列；依赖满足后何时解锁；超时后如何重试；**Agent 失联后如何重新分配**。」

### `agent-orchestration-theory/06_agent_lifecycle.md`

- **一句话定义**：「Agent 生命周期描述一个 Agent 从被创建、拿到任务、执行、提交，到重试、分裂、取消或完成的全过程。」
- **Context Assembly 工作包（第 2 节）**：「你是谁：Role；你要做什么：Task；为什么做：Parent Goal；团队已经知道什么：Relevant Knowledge；**你能使用什么：Tools and Permissions**；**你能花多少：Budget**；你要提交什么：Output Contract」；示例 YAML 含 `role / task / parent_goal / knowledge / tools / budget.max_tokens / output`

### `agent-orchestration-theory/10_concurrency_conflict.md`

- **一句话定义**：「允许 Agent 并行思考，但关键状态变化必须受控。」
- **Race Condition（第 2 节）**：「Agent 17 和 Agent 18 同时读取到 PENDING，然后都把它改为 RUNNING。最终结果取决于谁先写入，而不是系统规则。」
- **Atomic Operation（第 3 节）**：「检查任务是否空闲 + 正式领取任务，必须成为一次不可分割操作。」
- **CAS（第 4 节）**：「如果当前值仍然等于我之前看到的值，才允许修改。」
- **Version 与 Optimistic Concurrency Control（第 10 节）**：「Agent 1 基于 42 修改成功，版本变成 43。Agent 2 仍基于 42 提交，系统发现基础版本过期，要求重新读取并合并。这叫乐观并发控制：先并行工作，提交时再检查冲突。」
- **Lock 与 Lease（第 11 节）**：「Lock：某个 Agent 修改节点时暂时禁止其他 Agent 修改。Lease：锁有有效期，并依赖心跳续租，防止 Agent 崩溃后永久锁住任务。」
- **Stale State（第 12 节）**：「定期刷新上下文；订阅任务完成和知识更新事件；**提交时携带 graph_version、knowledge_version**；对基于旧状态的结果重新验证。」
- **Semantic Duplication（第 13 节）**：「"证明 Lemma X"与"验证 X 这个引理"文字不同，可能是同一任务……但不要过度自动合并，因为"证明 X"和"寻找 X 的反例"目标不同。」

### `agent-orchestration-theory/12_observability_tracing_evaluation.md`

- **一句话定义**：「Observability：系统现在发生了什么。Tracing：一个任务从头到尾经历了什么。Evaluation：这套系统到底好不好。」
- **Observability（第 1 节）**：数百 Agent 并发时至少要看见「运行中 Agent 数 / 等待任务数 / 失败和超时数量 / **待总结与待验证队列** / token 和费用消耗 / 最昂贵任务 / 长期无进展任务」；「它回答：系统当前健康吗？」
- **Metrics（第 3 节）**：「任务成功率 / 平均任务耗时 / 每个成功任务平均成本 / 验证通过率 / 结果重复率 / Agent 超时率 / 队列长度」；「Logs：Agent 17 超时；Metrics：过去一小时超时率 7%」
- **一个 Attempt Trace 应记录什么（第 13 节）**：「task_id / attempt_id / parent_task_id / agent_role / model_version / prompt_version / allocator_version / retrieval_version / 读取了哪些知识 / 完整工具调用 / 输入输出 token / 运行时间 / 提交结果 / Verifier 结果 / 创建的子任务 / 最终状态」；「**没有版本信息，实验往往无法复现。**」
- **监控面板的四块（第 14 节）**：系统健康「并发、队列、失败率、超时率」；成本「总成本、每任务成本、每角色成本、每模型成本」；研究进展「完成节点数、新知识数、已验证知识数、关键路径剩余任务」；质量（略）
- **Replay（第 12 节）**：「因为保存了 Event 和 Trace，可以在修改规则后重放历史失败案例：同一个任务、相同初始条件、新的检索或验证规则。」

### `agent-orchestration-theory/13_remaining_knowledge_map.md`

- **Model Routing 模型路由（第 3 节）**：「并非所有任务都应该使用最强、最贵模型。简单分类 → 小模型；任务拆解 → 强推理模型；代码修改 → 代码模型；摘要压缩 → 便宜快速模型；关键最终方案 → 最强模型；外部验证 → 测试或形式化系统。」升级策略：「便宜模型失败 ↓ 更强模型 ↓ 仍失败 ↓ 拆任务或人工介入」
- **Heterogeneous Agents 异构 Agent（第 4 节）**：「团队可以由不同模型、工具和能力构成：数学 Agent / 代码 Agent / 搜索 Agent / 视觉 Agent / 快速小模型 Agent / 形式化验证器。需要研究：能力发现、任务匹配、成本差异和跨模型通信。」（= "执行池/runtime profile"最接近的定义原文；theory 目录**没有** "执行池"或 "runtime profile" 字样）
- **Workspace 与 Artifact Management（第 13 节）**：Agent「还会产生：代码 / 文件 / 证明 / 数据集 / 实验结果 / 模型权重 / 数据库变更」；需要处理「**工作区隔离** / 文件版本 / 分支 / 测试 / 合并冲突 / 产物血缘 / 结果复现」；「对 Codex 类系统尤其重要。」
- **Security 与 Permission（第 14 节）**：必须明确「哪个 Agent 能使用什么工具？能读取哪些数据？能修改哪些文件？能否联网？能否执行 shell？能否发送邮件或付款？能否创建新 Agent？」；「**核心原则是最小权限。**」
- **Prompt Injection 与 Agent-to-Agent Attack（第 15 节）**：「一个被污染的 Agent 还可能把恶意内容写进 Blackboard，继续感染其他 Agent。需要：区分数据与指令 / 来源标记 / 消息签名或身份验证 / **工具权限隔离** / 知识写入审核 / 不可信内容标记 / 敏感信息过滤。」
- **Multi-tenancy 多租户（第 20 节）**：「多个用户和项目共用平台时，需要：**数据隔离 / 权限隔离 / 资源配额** / 公平调度 / 成本归属 / 审计日志。一个项目不能读取另一个项目的私有知识，也不能占光全部算力。」（= 多 Mission 配额与隔离最接近的定义原文，S6-01）
- **群体失效需要的对策（第 23 节）**：「多样性配额 / 独立验证 / 探索预算 / **分组隔离** / 反对意见机制 / 受控知识传播」（对应失效模式「羊群效应 / 错误知识快速扩散 / 热门任务吸走全部预算 / 不同分支间来回震荡 / 局部最优 / 信息瀑布」）
- **优先级老化（第 12 节相关）**：「需要循环依赖检测、停滞检测、最大重试和优先级老化。」（theory 目录中"老化"仅此一处 + 05 号文件第 10 节）

### 术语缺口（在 theory 目录中查无定义原文）

- **高低水位 / 滞回（hysteresis）**：无任何文件出现该词。
- **执行池 / runtime profile / runtime_profile_id**：无。最近似为 13 号文件第 4 节"异构 Agent"与 §29.1 的 Worker 拓扑。
- **权限交集**：无该组合词。最近似为 13 号第 14 节"最小权限"与设计原文 §21.2。
- **Verifier 路由 / verifier_router**：theory 无该词，定义只在设计原文 §14.1/§14.2 与 §27 模块树。
- **Verifier Worker**：theory 无定义，仅见设计原文 §27.1「若干 Agent Worker，一个或多个 Verifier Worker」与 §29.1「2 个 Verifier Worker」。

---

## 5. 与前几步的接口（原文/纲要指出的、本步要扩展的点）

| 已有能力（来自第 2—5 步 / 原文） | 本步扩展点 | 依据 |
|---|---|---|
| **Allocator**（第 3、5 步：依赖 Frontier 与受控调度、规则分配） | 加入"下游积压惩罚"，接受背压信号；在减速时仍保持等待老化与探索额度；按 §29.2 注册角色配比起点并按阶段动态调整 | 纲要附录「§8 → 步骤 3,5,6：先依赖Frontier与受控调度，再规则分配和背压」；设计 §8.2、§29.2 |
| **Commit Service / Single Writer**（第 2 步唯一逻辑写入） | 全局 Budget 仍由它唯一写入；多 Mission 配额分配、背压状态变更都必须经它落事件 | 设计 §17.5；纲要 §1.3「Orchestrator Commit Service 唯一逻辑写入」 |
| **SDK invocation ledger（费用事实）** | 每条 `usage_ref` 至多导入一次实际支出；本步把所有真实调用费用关联到 Global→Mission→Task→Attempt 配额；"资金分配与最终计费不重复相加" | 纲要 §12.2、§8.2 budgets 行；设计 §17.4「不会……重复扣费」 |
| **Workspace**（第 2—4 步：早期只读/隔离沙箱） | 升级为"独立可写目录/沙箱 + 只读依赖产物"；每 Attempt 一个；合并走新候选与再验收 | 纲要附录「§20 → 步骤 2,3,4,6：早期只读/隔离沙箱；后续强化Workspace和产物合并」；设计 §20.1/§20.3 |
| **Tool Gateway**（第 2 步已启用） | 细粒度权限：Mission/Task/Role/部署政策四者**交集**；参数、速率、资源和费用检查；外部文本不能提升权限 | 纲要附录「§21 → 步骤 2,6,7：工具与权限早期启用；细粒度与真实动作审批随后开放」；设计 §21.1/§21.2/§21.3 |
| **Verifier**（第 2、4 步：确定性 + 独立审阅） | verifier_router 按 `verification_policy` 运行多层；保留分层结果/版本；未部署的必需验证器→阻塞而非假通过 | 纲要附录「§14 → 步骤 2,4,6,7」；纲要 §12.4；设计 §14.1 |
| **Trace / Event**（第 2 步起基础 Trace） | 补齐完整版本字段（`model_version / prompt_version / retrieval_version / allocator_version / verifier_version` + `trace_id / agent_id`）与队列、延迟、费用、复用、验证率指标 | 纲要附录「§23 → 步骤 2,4,6,8,9：基础Trace早记录；完整运营视图……逐步完成」；设计 §23.1/§23.2 |
| **BaseAgent SDK 面**：`model_profile_ref` + `AgentRuntimePorts.provider/model` | 纲要 §2 原句写明其用途就是「第 6 步验证配置到物理模型的真实路由」，禁止误用为「只改 profile 字符串就宣称换模型成功」 | 纲要 §2 接入面表 |
| **BaseAgent SDK 面**：`artifact_refs / usage_refs` | 关联实际产物与真实费用账本；禁止「相信模型自己填写的 cost 或伪造的文件引用」 | 纲要 §2 |
| **并发/预算默认值**（第 2 步已要求显式绑定） | 纲要 §2 原句：「第 6 步才扩大规模，不等于第 2 步可以没有上限」——本步是把上限做成分层可配置，而非首次引入 | 纲要 §2 末段 |
| **Attempt Schema（§26.3）** | 原 schema 只有 `model`；本步要求 `runtime_profile_id` 持久绑定、workspace 归属；属于必须新增的结构化绑定 | 设计 §26.3 vs 纲要 §8.2 agent_worker 行；纲要 §13「实现细节可以新增结构化绑定表，但不能新增一套不同的顶层任务名词」 |
| **Lease / Heartbeat（第 3 步）** | 纲要 §12.1：业务租约与 SDK 执行租约分别保存；重启多执行池（S6-08）时"旧Attempt回到原目标配置" | 纲要 §12.1；设计 §17.6 |
| **execution.db / orchestrator.db 分离（§1.3）** | 本步新增约束：按模型配置组建不同 Runtime 池时"必须分开SDK存储/明确恢复分区"，不能让不同模型 Runtime 打开同一 `execution.db` | 纲要 §1.3 + §8.2 表后段 |

---

## 6. 我注意到的歧义/冲突

1. **"高低水位 + 滞回"无原文出处**。纲要 `backpressure.py` 行要求"上下游高低水位，恢复有滞回"，但设计原文 §18.5 只给了 6 个静态上限，theory 11 号文件只有"并发上限/队列上限 + 到达上限后暂停低优先级或拒绝新任务"，全库 grep 无"水位/滞回/hysteresis"。→ 双阈值与滞回宽度是纯实现新增，按纲要 §13 首句「任何不同选择都要单独登记，不可宣称是原文原句」需要显式登记。S6-02 的"恢复后逐渐放开"是其唯一验收锚点。

2. **两条优先级公式项数不一致**。§8.2 概念公式含 9 项（含"风险"与"下游积压惩罚"），§29.3 的第一版加权公式只有 7 项且**不含风险与下游积压惩罚**。背压要通过"下游积压惩罚"进入 Allocator，但推荐权重表里没有这一项的系数。→ 需要决定是给 §29.3 加项（偏离原文权重）还是把背压做成 §29.3 之外的独立闸门。

3. **§19.5 的"暂停或删除" vs 纲要 §13 的"不新增 PAUSED、不物理删除"**。设计 §19.5 说无法解释根目标关系的任务「应降级、暂停或删除」，§19.2 也说「暂停或终止分支」；纲要 §13 明确「在调度控制或 Mission/分支政策中暂停；保留事件，不给 Task 私自新增 PAUSED 终态，不物理删除审计历史」。→ 本步 allocator 的"暂停低价值扩展"必须实现为调度层抑制，不得改 Task 状态机。

4. **降速动作原文 6 项，纲要只列 3 项**。设计 §18.5 列「降低 Worker 并发 / 提高 Verifier 资源 / 暂停低优先级任务 / 禁止新任务继续分裂 / **合并重复候选** / **缩小每个 Attempt 预算**」，theory 11 号第 13 节更列 8 项（多"优先处理高分结果""提高知识压缩频率"）。纲要 allocator 行只写了前三类。→ "合并重复候选"和"缩小每个 Attempt 预算"归属哪个模块（allocator? budgets? deduplicator?）未定，且 S6-02 的验收结果列也未覆盖它们。

5. **角色配比覆盖了哪些角色不一致**。§9.2 定义 8 个角色，§29.2 只给 5 个角色的百分比（Explorer/Exploiter/Critic/Synthesizer/Verifier = 100%），而 §29.2 与 §8.3 的动态调整规则却说"中期：Exploiter 和 **Connector** 增加"——Connector 没有基线份额却要被"增加"。Simplifier、Failure Analyst 同样无份额。→ 配比注册表的角色全集与百分比归一化方式未定义。

6. **§28 第三阶段把 Human-in-the-loop 列在本阶段，纲要把它整体推到第 7 步**。纲要附录也确认「§22 → 步骤 7」。但 §14.1 的第六层"必要时人工审核"属于 verifier_router 要落实的分层之一，而第 6 步模块表要求"未部署的必需验证器导致阻塞而非假通过"。→ 若某 Task 的 `verification_policy` 含第六层，本步的正确行为是"阻塞等待第 7 步能力"还是"该策略在第 6 步不可选"，未写明。

7. **Trace 版本字段清单在三处不完全一致**。设计 §23.1 给 10 项（含 `trace_id / mission_id / task_id / attempt_id / agent_id` + 5 个 version）；theory 12 号第 13 节给的 Attempt Trace 清单**没有** `verifier_version` 但多了 `parent_task_id / agent_role / 读取了哪些知识 / 创建的子任务`；纲要 S6-09 只点名"prompt/model/retrieval/allocator/verifier 版本"。§26.3 Attempt Schema 又只有 `prompt_version / context_version`（而非 `retrieval_version`）。→ `context_version` 与 `retrieval_version` 是否同一字段需要裁定；纲要 §13 已有一条同类裁决（§13 result_id vs §26 result.id），本条尚未登记。

8. **`model` 字段 vs `runtime_profile_id` 的权威性**。§26.3 Attempt Schema 有 `model: string`；纲要要求"runtime_profile_id必须持久绑定"且"每次调用冻结实际目标并纳入Trace"。S6-03 要求"实际provider/model改变……而不是只改标签"，意味着需要同时存在"路由请求的 profile"与"实际冻结的 provider/model"两组值。→ 三者（`model`、`runtime_profile_id`、实际冻结目标）的关系与冲突检测规则未定义；升级换模型时是新建 Attempt（带 `retry_of`）还是原 Attempt 改 `model`，原文 §9.3 的升级链未说明，而 §25/纲要 §13 规定「RETRY_WAIT后……retry_of记录旧尝试，创建新id」——S6-03"保留先前Attempt与费用"倾向新建，但没有原文明文。

9. **Workspace 隔离粒度**。§20.1 明确是 per-Attempt（Attempt 1/2/3 → Workspace A/B/C），§20.2 artifact schema 也带 `workspace` 字段；但 §26.3 Attempt Schema 没有 workspace 字段。→ workspace 标识的存放位置（Attempt 表 / artifact 表 / 独立绑定表）未定，且"只读依赖产物"如何挂载（纲要用语，原文 §20 无对应描述）也无原文依据。

10. **"权限交集"的四个维度无原文枚举**。纲要写"Mission/Task/Role/部署政策的权限交集"；设计 §21.2 只按 Agent 类型（=Role）给权限档，§10 第 9 项只说"可用工具与权限"，theory 13 号第 14 节只给"最小权限"原则。Mission 级与"部署政策"级权限在原文中没有定义位置。→ 交集的计算顺序、冲突时谁更严（取交集应始终取最严，但"部署政策"能否放宽 Role 档未说明）需要登记。

11. **S6-06"一个模型服务不可用"在原文里没有对应条款**。§9.3 的升级链只覆盖"失败或低置信"，不覆盖"provider 不可达"；§19 的停滞/死锁也不含外部依赖不可用。"有界等待/明确降级"的边界值与降级目标（降到哪个 profile、是否算入停滞计数、是否消耗预算）全无原文。

12. **§29.1 的"10～20 个 Agent Worker、2 个 Verifier Worker"与 §18.5 的 Worker:Verifier = 1000:50 示例比例悬殊**。前者约 5:1～10:1，后者 20:1。§8.1 已声明前者只是"可配置目标拓扑"，但 S6-02 的减速触发阈值究竟以哪种比例为默认，未定。

13. **"探索额度"与"多样性配额"是否同一物**。纲要 allocator 行说"保持等待老化和探索额度"；theory 05 号第 10 节说"保留固定探索预算"（属反饥饿），13 号第 23 节说"多样性配额 / 探索预算 / 分组隔离"（属反群体失效）。两者动机不同但机制可能重叠。→ 是一个配额还是两个，未裁定。

---

## 10 行摘要

- 目标文件 `/private/tmp/claude-501/-Users-taiwan-PROJECTS-SimplaHarness/a708b80b-3df1-4c43-bad3-32fb941bf39d/scratchpad/step06-design-brief.md` **未写入**：本会话是强制只读模式，禁止创建任何文件（含 scratchpad 与 heredoc）。完整简报见上方正文，可直接落盘。
- 第 1 节：纲要 §8 原句 4 段 + 模块表 9 行逐行照抄 + §8.3 角色配比 2 段 + 交付命令。
- 第 2 节：S6-01…S6-09 共 9 条原表照抄，另附映射到第 6 步的 §30 原始验收项 11 条（ORIGINAL-30-12/15/16/18/19/20/21/24/25/26/29）。
- 第 3 节：设计原文规则共 **52 条**，全部注明章节号——§8:6、§9:5、§10:3、§17:6、§18:8、§19:5、§20:3、§21:3、§23:4、§28:1、§29:3，另加支撑条款 §14.1/§14.2/§26.3/§26.4/§26.6 共 5 条。
- 第 4 节：术语定义摘录自 6 个 theory 文件（11/05/07/06/10/12/13 号），共 **42 条定义原文**；并记录 5 个**查无定义**的术语缺口（高低水位与滞回、执行池/runtime profile、权限交集、Verifier 路由、Verifier Worker）。
- 第 5 节：与前几步的接口 **13 行**，覆盖 Allocator、Commit Service、Ledger、Workspace、Tool Gateway、Verifier、Trace、BaseAgent SDK 接入面、Attempt Schema、Lease、双库分离。
- 第 6 节：歧义/冲突 **13 条**，其中最需先裁定的是：高低水位与滞回无原文出处（须按纲要 §13 登记）、两条优先级公式项数不一致、`context_version` 与 `retrieval_version` 是否同名、模型升级是改 `model` 还是新建带 `retry_of` 的 Attempt。
- 全程只摘录整理，未做任何设计决定；所有表格与公式按原文逐字保留。
