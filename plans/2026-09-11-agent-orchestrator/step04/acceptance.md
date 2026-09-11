# 第 4 步验收标准（MUST 8 条 = ORCH-BUILD §6.3 S4-01～08）

| ID | 场景 | 必须观察到的结果 | 判定 | ORIGINAL-30 |
|---|---|---|---|---|
| S4-01 | A 的 Claim 经机器验证 VERIFIED 后 B 继续 | B 的 Attempt 上下文含该知识的 id 与 version（intent config `knowledge` 冻结、包文本可见）；B 的结果 `used_knowledge` 引用它；知识记录 `used_by` 含 B 的 Task；事件 `KnowledgeUsed`；血缘可从 B 回溯到 A 的 Attempt/Agent | `test_knowledge_sharing_closure.py::test_s4_01` | 30-08 |
| S4-02 | A 只有自信但无机器验证 | 该 Claim 至多 SUPPORTED（不进 `knowledge` 表）；B 的 Worker 包 `verified_knowledge` 不含它、包文本不把它列为事实；B 若在 `used_knowledge` 引用该 claim id → `rule_check` FAIL | `test_retrieval_context.py::test_s4_02_*`、闭环 `test_s4_02` | 30-07、30-11 |
| S4-03 | 两个相反 Claim | 双方保留且 DISPUTED（VERIFIED 知识保持 VERIFIED 并记 `disputed_by`）；`conflicts` 表有记录、Conflict Task 入图（`TaskCommitted source=conflict`）；Arbiter 只给意见（无 pytest 证据）→ `rule_check` FAIL 重试；带外部验证的仲裁 Claim VERIFIED → `ConflictResolved`，争议 Claim 记 `resolved_by`；事件里没有票数、解决依据是 `code_test` | `test_conflicts.py`、闭环 `test_s4_03` | 30-10 |
| S4-04 | 知识被 SUPERSEDED | 新 VERIFIED Claim `supersedes=K1` → K1 Claim SUPERSEDED、知识 `superseded_by=K2`；之后的 Attempt 包 `verified_knowledge` 无 K1，`superseded_knowledge` 列出 K1→K2；结果引用 K1 → `rule_check` FAIL 且消息给出 K2；早先 Attempt 的 intent 仍冻结 K1 v1；SUPPORTED 的新 Claim 不能取代 | `test_claims_knowledge.py::test_s4_04_*` | 30-09 |
| S4-05 | Synthesizer 合并产生新错误 | 综合任务依赖全部叶子（含冲突任务）；综合产物的 `code_test` FAIL → 结果 FAIL、Task 不 COMPLETED、Mission 不判定；修正后再验收通过才 COMPLETED；`used_knowledge` 为空或含非 VERIFIED → `rule_check` FAIL | `test_synthesis.py`、闭环 `test_s4_05` | — |
| S4-06 | 同时运行两个 Mission | 两个 Mission 的全部 intent `knowledge` 只含各自 Mission 的 id；`list_knowledge(mission)` 严格按 Mission；引用他 Mission 知识 id → FAIL；每个 Attempt 是新的 BaseAgent（agent_id 不复用） | `test_retrieval_context.py::test_s4_06` | — |
| S4-07 | 检索/摘要失败或索引未就绪 | `block`：不建 Attempt、事件 `RetrievalUnavailable`、Task 仍 READY；连续超限 → Task FAILED `retrieval_unavailable`；`degrade`：建 Attempt，包内 `knowledge_retrieval.status="unavailable"`，文本不写"没有证据/没有知识" | `test_retrieval_context.py::test_s4_07_*` | — |
| S4-08 | 原始外部内容含改变权限的指令 | 读取该文件的工具返回带 `trust=untrusted_external` 标记；Worker 随后请求未授权工具被网关拒绝；Claim `status=VERIFIED` 的信封被拒；仅引用外部文档的 Claim 不 VERIFIED/SUPPORTED（`grade=unsupported`）；上下文包不内联外部内容 | `test_retrieval_context.py::test_s4_08_*`、闭环 `test_s4_08` | 30-21、30-08 |

附加门槛：`tests/orchestrator`（step02+03+04）全绿；SDK 全量红集 ⊆ 基线；schema v1 库可升级到 v2 且旧场景回归；安装 wheel 后跑 `tests/orchestrator` 与 `demo --scenario knowledge-sharing`；真实模型演示记录（至少"知识被复用 + 综合再验收"）；独立 review 处置；L3-2 决定性测试（同 Task 两候选同路径版本不同）。
