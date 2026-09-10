# 第一阶段 BaseAgent：切片纲要（BA-v1.0 → 5 个垂直切片）

- 日期：2026-09-10
- 依据：Host 仓库 `plans/taskSys2/base-agent-phase1-plan.zh-CN.md`（BA-v1.0，A0–A6 / BA01–BA40）
- 代码事实依据：本目录 `investigation.md`（main = `fd12e7dd`，0.7.10）
- 约束：用户的 plan-test 流程规定**每个交付单元 MUST AC ≤ 8、任务 ≤ 10**，因此 A0–A6 不能一次交付。

---

## 0. 主要矛盾与切片原则

**主要矛盾（一句话）：** 要让主 Agent 用一个受配额的委派工具造出子 Agent、并把子 Agent 的结果可靠取回用于回答，就必须让父子两个执行身份都永不终态；可 SDK 里唯一的父子结果通道 `child_terminal_receipts` 恰恰只在子 Run 终态时才产生（`kernel.py:3195-3241`），于是"不死"与"能取回结果"在现有内核里互相排斥。

> **2026-09-10 修订（Slice 1 第 1 轮挑战裁决 #12）**：初稿把矛盾写成"只缺一个不死的执行身份、委派底座已就绪"，后半句被代码证伪 —— 一旦执行身份不死，既有的结果回传通道同时失效。矛盾必须写成同一件事的两面。

- **矛盾的主要方面**：是**「单轮终态约束」与「父子结果回传依赖终态」这同一件事的两面**（`kernel.py:218-222`、`react.py:292`、`kernel.py:1267`、`kernel.py:3195-3241`），不是委派本身。原子父子创建、工具网关/授权/effect 账本、租约与围栏确实已存在且 driver 无关（`investigation.md` §11），但**结果回传通道必须重造**：改为让子 Agent 也永不终态，父侧直接条件等待子 Agent 的 `base_agent_turn_results_v1` 结果行（带超时），彻底不用 `child_terminal_receipts`。
- **两面同为核心**：S1 的资源同时投在「不死的执行身份」（结果载体 + `_drive` 分流 + stage/finalize + 同事务 ack）与「不依赖终态的结果通道」（`agent.delegate` 直读子结果行）两侧，少任何一样价值链都断。
- **切片原则**：Slice 1 只打通这一条最短价值链；凡是"让 Agent 更聪明/更省 token/更抗压"的能力（Context 有界装配、混合召回、并发公平、故障注入、发布物）一律后置。

### 0.1 与 BA-v1.0 §2 的一次有意越界（必须先认账）

BA-v1.0 §2 写明"本阶段不实现**自动子 Agent 委派**"，§15 写明"默认不向模型暴露自动 `create_many` 权限"。
用户给第一阶段定的验收目标恰恰是"主 Agent 委派子 Agent"。

**处置**：
- **不**向模型暴露 `create_many`（尊重 §15 的"防无限繁殖"意图）；
- 改为一个 **单层、有配额、不递归** 的 `agent.delegate` 工具：每个 AgentTurn 的委派次数有上限，子 Agent 的 `capability_snapshot.tools` 不含该工具（结构性禁递归，见 `investigation.md` §12），委派记录进 `base_agent_delegations_v1` 做幂等；
- 这几条验收项**不占用 BA01–BA40 编号**，另起 **DG01–DG04**，以免声称"BA-v1.0 已覆盖它"。

---

## 1. 切片总表

| 切片 | 目标（一句话） | MUST 的 BA/DG 项（≤8） | 次要/后续 | 依赖 | 主要交付物 |
|---|---|---|---|---|---|
| **S1 委派最短价值链** | 让主 Agent 在同一个持久执行身份上多轮工作，并通过受配额的 `agent.delegate` 造出**同样永不终态**的子 BaseAgent、直接从子结果行把结果可靠取回。 | DG01、DG02、DG03、BA07、BA30、BA01、BA06、BA38 | DG04、BA12 | 无（基线 `fd12e7dd`） | schema v10（4 张表）；`runtime/agent_turn.py` + `agents/` 包；`base-agent/v1` api namespace + 两个 kernel 公开入口（**不新增 start mode、不动 `start_snapshot.py` 与 `StartModeDriverRouter`**，改用 `driver_kind="base_agent"`）；AgentTurn 结果载体 + `_drive` 分流 + 同事务 ack + recover 优先 finalize；`AgentExecutionDriver`；`build_agent_runtime`；`agent.delegate`（直读子 `base_agent_turn_results_v1`）；内核级里程碑 spike + mock 端到端 + 真实 provider 演示 |
| **S2 生命周期与批量创建** | 把 BaseAgent 的持久生命周期补完：批量幂等创建、排队串行、取消/关闭、UNKNOWN 同 Turn 恢复。 | BA02、BA03、BA04、BA05、BA08、BA09、BA11、BA12 | BA10 | S1 | `base_agent_creation_batches_v1`；`create_many` 完整幂等/冲突/整批回滚；输入队列与 FIFO+轮转；`cancel_turn` / `close`；`(agent_id,turn_id)` 分键 checkpoint namespace |
| **S3 有界 Context 与 Journal** | 让每个模型步骤的完整请求都在真实 token 预算内，且原文先存后裁、可精确回读。 | BA13、BA14、BA15、BA16、BA17、BA18、BA19、BA22 | BA20、BA21 | S1（S2 可并行） | `agents/context/{composer,budget,protocol_groups,working_notes}.py`；`base_agent_session_journal_v1`、`base_agent_context_selections_v1`、`base_agent_session_summaries_v1`；TokenizerPort；增量 Journal 取代全量快照 |
| **S4 AgentSession 混合召回与隔离** | 在当前 Agent 自己的历史里做词面+向量混合召回，严格隔离、可见降级、不回灌。 | BA23、BA24、BA25、BA26、BA27、BA28、BA29、BA20 | — | S3 | `agents/memory/{session,journal,lexical,vectors,retrieval,index_jobs}.py`；`base_agent_session_vectors_v1`、`base_agent_index_jobs_v1`；EmbeddingPort；FTS5/中文分词；`session_history.search/read` 工具 |
| **S5 并发、恢复、迁移与发布** | 在多实例并发与故障注入下证明不重复执行、不越限、可迁移，并交出 exact-wheel 证据。 | BA31、BA32、BA33、BA34、BA35、BA36、BA37、BA39 | BA40 | S1–S4 | 公平限流与队列配额；故障注入矩阵；v9→v10 备份优先迁移 + 回执；exact-wheel conformance；发布说明与示例 |

**覆盖核对**：BA01–BA40 全部落位（S1:01/06/07/30/38；S2:02/03/04/05/08/09/10/11/12；S3:13–19/21/22；S4:20/23–29；S5:31–37/39/40），另加 S1 的 DG01–DG04。

---

## 2. 各切片展开

### S1 · 委派最短价值链

**目标**：主 Agent（BaseAgent 实例）收到一个复杂任务，通过 `agent.delegate` 创建一个**同样永不终态**的子 BaseAgent 去完成它，结果经"直读子结果行"回到主 Agent 并出现在主 Agent 的最终回答里；主 Agent 之后仍可接第二条输入；进程重启后旧结果可读且不重生成。

**MUST（8 条）**

| ID | 内容 | 矛盾地位 |
|---|---|---|
| DG01 | 主 Agent 调用 `agent.delegate` → 原子创建一个独立子 BaseAgent（独立 agent_id / 独立执行身份 / 独立 Context），且子 Run 从存在的第一刻起就被 `base-agent/v1` 围栏 | 决定性 |
| DG02 | 子 Agent 的结果**经其 `base_agent_turn_results_v1` 行**回到主 Agent 的本轮 Context，并被主 Agent 用于最终回答（nonce 口径可判定） | 决定性 |
| DG03 | 委派单层且受配额：子 Agent 工具集不含 delegate；每 AgentTurn 委派次数超限被拒；同 `delegation_id` 重发返回原子 Agent；**一次委派 = 子 Agent 一个 AgentTurn** | 决定性 |
| BA07 | 一次回答提交以后，同一个**主** Agent 仍可处理第二条输入（子 Agent 不要求两轮） | 决定性 |
| BA30 | **结果 stage 之后**任意点 kill 进程，恢复只提交已冻结结果，不重新调用模型、不重进 driver | 决定性 |
| BA01 | `create` / 最小 `create_many` 产出互不共享可变 Context 的独立实例；创建阶段不调用模型 | 必须 |
| BA06 | 旧 Run / conversation API 对 `base_agent_v1` 身份明确拒绝（覆盖 root 与 child），新入口对 legacy/unmanaged 对称拒绝；未注册 `base_agent` driver 时 `build_runtime` 拒绝而非落回 react；旧普通调用不受影响 | 必须 |
| BA38 | 不安装 `simple-harness-memory-sdk` 也能跑完整链路；spy 证明未调用任何用户记忆入口 | 必须 |

**次要/后续**：DG04（委派 effect UNKNOWN 时**凭子 Agent 结果行**结算、不盲重试）标为"本片实现但只做单条断言"；BA12（close）推 S2。

**明确移交 S5 的遗留**：`react_loop.py:717-732` 的最终 CAS 与本片 stage 事务之间的**残留冻结窗口**（该窗口内崩溃会重新调模型），归 **S5 的 BA31**（届时把 `RESULT_PENDING` 并进同一次 CAS）。

**任务与里程碑**：10 个任务，**价值验证里程碑 = T6.5**（内核级 spike，不依赖 `agents` 包与 delegate 工具），**完整价值链验收 = T9**。详见 `slice-1/plan.md`。
**端到端演示**：mock 确定性版（4 次模型调用）+ 真实 provider 版各一（后者 `--run-real-provider` 开关，默认 skip，**不改 pytest addopts**，报告分开）。
**第 1 轮挑战**：21 条已全部裁决（`slice-1/challenge-round-1.md`），落点逐条见 `slice-1/plan.md` 附 B。

---

### S2 · 生命周期与批量创建

**目标**：把"一个配置模板造 N 个实例、每个实例串行处理自己的输入队列、可取消可关闭、UNKNOWN 恢复同 Turn 同动作身份"补完。

**MUST（8 条）**：BA02（100 个 IDLE Agent 不发 100 次 Provider 请求）、BA03（同 batch_key 同内容返回原 IDs，不同内容冲突）、BA04（一项配置不合法整批不留部分成功）、BA05（`open` 只打开已存在 Agent，错误 owner 不泄露）、BA08（同 Agent 两条并发输入顺序处理）、BA09（`ask` 超时不取消底层 Turn）、BA11（授权/UNKNOWN 恢复保持同 Turn 同动作身份）、BA12（`close` 拒收新输入；`runtime.shutdown` 不关闭全部逻辑 Agent）。
**次要**：BA10（Turn 失败不清空 session/费用）。
**依赖**：S1。
**交付物**：`base_agent_creation_batches_v1`（owner_scope+batch_key UNIQUE、payload_hash、ID 序列、receipt）；批 fingerprint 规范化；输入队列与 ready 标记；`cancel_turn` / `close` 合同与 closing receipt；checkpoint namespace 迁到 `(agent_id, turn_id)` 分键（补 `investigation.md` D7）。

---

### S3 · 有界 Context 与 Journal

**目标**：把"每次 append 重写整份 messages"（`context.py:170-174`）换成增量 Journal + 有界装配，并保证真实 tokenizer 下请求永不超预算。

**MUST（8 条）**：BA13（真实 tokenizer 下完整请求在预算内）、BA14（工具 schema/角色说明/召回都计入）、BA15（完整工具协议组不被拆开）、BA16（必需内容超预算时调用前报 `context_required_content_too_large`）、BA17（长 Turn 内可轮换旧闭合组）、BA18（原文先保存、裁减后可精确回读）、BA19（追加体量随新记录近似线性）、BA22（换模型/tokenizer/模板不沿用旧计数）。
**次要**：BA20（召回片段不被再写成新原始历史）、BA21（摘要有来源、工作笔记不覆盖真实执行状态）。
**依赖**：S1（S2 可并行）。
**交付物**：`agents/context/*`；`base_agent_session_journal_v1`、`base_agent_context_selections_v1`、`base_agent_session_summaries_v1`；`TokenizerPort`；ProtocolGroup 选择器；大工具结果外置为产物 + 回读引用。

---

### S4 · AgentSession 混合召回与隔离

**目标**：Agent 只在自己的历史里检索，词面+向量融合，索引滞后/embedding 不可用有可见降级。

**MUST（8 条）**：BA23（A 的秘密不进 B 的搜索与模型输入）、BA24（中文语义改写 + 英文函数/路径查询各有实测用例）、BA25（FTS 不支持/embedding 不可用/索引滞后有明确降级）、BA26（维度或模型版本变更不混算旧向量）、BA27（允许零召回，不凑 top-k）、BA28（冻结请求重试保留原选择）、BA29（需要全集时分页，不用 top-k 代替）、BA20（召回不回灌）。
**依赖**：S3。
**交付物**：`agents/memory/*`；`base_agent_session_vectors_v1`、`base_agent_index_jobs_v1`；`EmbeddingPort`（真实 embedding，HashEmbedder 不算数）；FTS5 中文路径（字符 n-gram / 经测试分词，见 BA-v1.0 [W02]）；`session_history.search/read` 工具（参数里没有任意 `agent_id`）。

---

### S5 · 并发、恢复、迁移与发布

**目标**：证明多实例并发下不越限、故障点恢复不重复执行、v9→v10 迁移安全，并交出 exact-wheel 证据。

**MUST（8 条）**：BA31（finalize 任意事务点异常不形成"输入已确认但结果丢失"；**并接收 S1 移交的残留冻结窗口**——把 `RESULT_PENDING` 并进 `react_loop.py:717-732` 的同一次 CAS，使失败域从"stage 之后"扩到"最终响应冻结之后"）、BA32（输入到达与 IDLE 转换竞争不丢唤醒）、BA33（旧 lease 实例迟到不能新 handoff 或覆盖新提交）、BA34（不同 Turn 计数重启不撞请求 ID；同 Turn 恢复不重复预留费用）、BA35（多 Agent 不超模型/工具/索引并发与队列限制）、BA36（低成本 `session_history` 查询与摘要也有可观察成本或调用限制）、BA37（数据库副本迁移后旧 API/旧快照/旧 ledger 回归通过）、BA39（外部工具响应丢失不盲重试；取消不宣称撤销现实动作）。
**次要**：BA40（源码测试与 exact-wheel 测试分别留证）。
**依赖**：S1–S4。
**交付物**：FIFO+轮转公平调度与配额；故障注入矩阵（复用 `uow.py` 现成的 `_fault(...)` 钩子）；备份优先的 v9→v10 迁移 + 回执（照抄 `short_context_migration.py` 骨架）；exact-wheel conformance；发布说明。

---

## 3. 跨切片的固定纪律

1. **每片独立验收**，各自有 `acceptance.md`，MUST ≤ 8。
2. **回归口径固定**：
   `.venv/bin/python -m pytest -q -p no:cacheprovider --ignore=tests/integration/runtime/test_context_use_admission.py --ignore=tests/integration/runtime/test_context_use_durable.py --ignore=tests/integration/runtime/test_context_use_public_memory.py`
   新红 = 不在 `baseline-known-failures.txt` 里的 FAILED/ERROR，**一条即阻断**。
3. **schema 版本只分配一次 v10**（S1 用掉），S2–S5 若要新表，一律追加到 v10 的 DDL 里并在同一切片内重算 descriptor；不得出现两份互不兼容的 v10（BA-v1.0 §11.2）。
4. **旧路径零改动清单**见 `investigation.md` §16，每片自检。
5. **不 import `simple_harness_memory`**：Agent 层任何模块都不许（BA-v1.0 §9.4）。
6. **两类报告分开**：mock 确定性测试与真实模型/真实 embedding 测试的结论不互相冒充（BA40）。
7. **start snapshot 编解码零改动**：`runtime/start_snapshot.py` 与 `runtime/drivers/start_mode.py` 在 S1 是字节级冻结；BaseAgent 走 `driver_kind="base_agent"` + `start.input.base_agent_binding`，**不新增 start mode、不新增 snapshot schema 版本**。后续切片若要动，须单独立项并重新评估 v6/v7 兼容证据。
8. **分层单向**：`agents` → `runtime` 单向依赖；`runtime/kernel.py` 与 `runtime/agent_turn.py` 永不 import `simple_harness.agents`；跨层只搬纯 JSON。
9. **父子结果通道**：BaseAgent 之间的结果回传一律走 `base_agent_turn_results_v1` 行 + 条件等待，**不使用 `child_terminal_receipts` / `child_signals`**（它们依赖终态，而 BaseAgent 永不终态）。
