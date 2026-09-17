## 0.12.2 — P2.3e/P2.3f/P2.3g/P2.3h：Grok 验收重跑与探针暴露的规划循环、provider 阻塞、合成器字段对齐、根评审证据四组缺陷修复（2026-09-17）

**P2.3l：Grok 第 2 批 L4 的两个 P0（N5 UNKNOWN grant 锁死重交接 / N7 非根 compound 无 composition 决议）。** 分支 `p2.3l-provider-grant-composition`，基 4a446f3；版本号不动。诊断 `plans/taskSys2/升级planV1/impl/Grok验收-第2批L4诊断-2026-09-17.zh-CN.md`；`contracts/` 零改动，无新配置项、无新提示词、无新事件名。

- **N5（P0）Provider UNKNOWN grant 把后续规划锁死**：M3-r2/r3 Planner 交接后 `provider_error_after_handoff`（0 token）→ P2.3f 300 s 后重交接 → `recover()` 拿活 intent 对新 agent、旧 grant 对旧 agent，身份不符默认 `authority_rejected` → 规划全灭、`planning_failed`、5 万预留卡住。修法：重交接前与 give-up 时 `ProviderBudgetGuard.release_held_grants`（SDK invocation 仍 UNKNOWN）；`recover()` 对已改写的 intent RELEASE 旧 grant 而不 deny；`provider_outcome_unknown` 且从未到达模型 → `fail_planning(..., stop_reason=RUNTIME_UNAVAILABLE)`；预留收口，`remaining+reserved+settled==pool`。P2.3f 原 7 条测试未装 estimator，行为不变。
- **N7（P0）非根 compound 的 composition_review 没有决议路径**：M3-r0 assess 子叶验收后进入 `composition_review`，无 GoalResolution，revert `WAITING_ORDER` → `no_dispatchable_work`。选择「按 children's Acceptances 走 composition acceptance 规则形成子 GoalResolution」，不复用根评审员（§6.3 / `leaf_acceptance`：compound 从不被自己评审；子叶已过独立 Critic；系统不自填 PASS）。`occurrence_outcomes` 按 `goal_task_id` 认决议；共享义务的非根决议不 SATISFY 根 duty；根决议 contributions 只报根方法直接孩子。
- 测试：`test_provider_grant_rehandoff.py` 2（真 `run()`）+ `test_nested_compound_composition.py` 2（夹具 + 真 `Orchestrator.run()` 两层到 COMPLETED）；4 条变异全部 KILLED（M3 关 `resolve_ready` 在真 `run()` 上复验 `no_dispatchable_work`）。独立核验「修后可合」两条 P1 已修：P1-1 UNKNOWN 调用进 `imported_usage.unknown=1`、可覆盖导入、`settle_known` 只结算已知；P1-2 删任意 PASS fallback、子验收按 task_id、GoalResolution 认 CURRENT+epoch。详见 journal 第四部分 §2n「核验处置」。
- 核验后测试：上段 4 条 + P1-1 2 + P1-2 3；变异 P1-1 / P1-2（M5）KILLED。full_target **2857 passed / 2 skipped**（核验基线 2851/3，+5）；旧模式 step02/05/06/07/p34/p35 **560 passed / 13 skipped / 0 failed**；ruff 全清；`_new_mode` 19 处不变。

代码候选提交 444879a（分支 `p2.3e-run-exit` c110515/8b8466d/424c179 与 `p2.3h-root-review` c9adf5b/256a316，合并提交 c202dec，均已 ff 进 main）；发布身份以本条目所在的版本提交为准。四片各有独立 fable 核验记录在 `plans/2026-09-16-full-target/P2.3c/reviews/`（P2.3e/f 可合、P2.3g 可合、P2.3h 修后可合 → 256a316 已修）。**迁移**：`code.fix-by-patch` / `fix-by-revert` / `fix-by-assessed-revert` 升到 `method_version 2`，旧库 `@1` 行保留并存；Grok runner 的 FREEZE-candidate 必须按本提交重生成。

**P2.3e：`run()` 在两个规划类意图在途时提前退出。** 分支 `p2.3e-run-exit`，基于 `05cfbb83`（= 0.12.1）。Grok 验收重跑 H 臂 L3 C1 三局完全一致：runner 的 `first_evidence_round` 已把 `code.test-is-failing` 由同一观察器记了两次 FALSE，D2b 的证据饱和判定在**第一个规划周期**就成立，Planner 意图与 MethodSynthesizer 意图同周期创建、同为 `InputSubmitted`；约 28.5 s 后 `orchestrator.run()` 返回、`Orchestrator` 上下文关闭，两个 agent run 在刚开始 preflight 的那一刻被取消（`runtime_boundary_interrupted` / `provider_error_after_handoff`），Mission 停在 PLANNING，事件表只有 AgentCreated/InputSubmitted。

- **根因（编排循环，不是 runtime、不是代理）**：`_request_method_synthesis` 每个周期都跑；合成轮在途期间 `goals_needing_method` 仍返回该目标（`synthesis_round_recorded` 只在回复被收集后才为真），`create_service_intent` 按 subject 幂等地把**已存在**的意图原样返回，方法把这当成 `progressed=True`。有进展的周期不 sleep，进程内 bridge 的 await 也不真正挂起，于是 `run()` 以每周期约 2.85 ms 把 `max_cycles=10 000` 的预算烧完（≈28.5 s），runtime 的回合任务在此期间一直饿着（两个回合的 `runtime.preflight` 时间戳恰好是 `run()` 返回的那一瞬），最后走 `max_cycles` 出口返回——而 `_has_inflight()` 从未被问到。
- **修法**：`_request_method_synthesis` 在创建前先查 `get_intent_for_subject`，已有意图的目标直接跳过、不计进展（问一次是上限，等待不是进展；subject 拼法收拢到 `_synthesizer_subject`）；`run()` 每个有进展的周期后 `await asyncio.sleep(0)`，让连续进展的循环也把控制权交给 runtime 的回合任务。`_has_inflight` / profile 归属 / 合成等待逻辑 / runtime handoff 均无缺陷，未改。
- 测试（`tests/orchestrator/full_target/test_run_loop_inflight_planning.py`，2 条，先红后绿）：①提供者全部挂在闸门上、两意图同时在途时 `run(max_cycles=120)` 1 s 内不得返回、两回合必须已到达提供者，开闸后两意图各自结算、`PlanRevisionCommitted`、Mission 不停在 PLANNING（去掉合成守卫时仍红，变异 KILLED）；②纯 `run()` 端到端：planner:1 被拒、合成方法准入、planner:2 采用，三个意图各有 `IntentSettled`，有进展的周期 < 60（修前 400/400 烧尽且零结算）。
- 真实复现：见 `plans/2026-09-16-full-target/P2.3c/journal.md` 第四部分 §2g。修后同一开局两个意图各自结算（`PlanningRejected{proposal_not_grounded}` + `MethodSynthesisRoundRecorded{UNREADABLE}`），随后暴露下一条（P2.3f）。

**P2.3f：plan/critic/synthesizer 意图的 provider 阻塞不得挂满期限。** planner:2 交接 0.2 s 后传输失败；runtime 如实记 `provider_error_after_handoff` → invocation UNKNOWN → run `waiting{provider_outcome_unknown}`（设计如此：请求可能已到模型），而编排侧没有裁决者——`_observe_liveness` 对 plan 意图只看 `exists`、Critic runner 只看 `result is None`——意图 SUBMITTED 挂到 runner 1800 s 超时。

- 修法（只对分层 Mission）：同一 provider 阻塞持续 `min(stall_seconds, 300)` s → `CommitService.rehandoff_service_intent`：同 subject、同预留、同请求字节，`creation_key` 换成 `{subject}:rehandoff:{n}`（runtime 按 creation_key 幂等创建 agent，换 key 才是新执行器），状态回 CLAIMED 走普通 dispatch，记新事件 **`ServiceIntentRehandedOff`**（`previous_agent_id/previous_turn_id/reason/detail`）。上限 1 次（`MAX_SERVICE_REHANDOFFS`，从事件表读）。再次未知 → 按角色收口：planner `PlanningRejected{provider_outcome_unknown}` 由梯子决定（下一级或 `fail_planning`）；method_synthesizer `MethodSynthesisRoundRecorded{admitted:false, verdict:UNANSWERED}` + `_after_synthesis_round` → `method_synthesis_refused`；root_reviewer `HierarchicalRootReviewUnreadable`；critic 交回 `_run_critic` 既有「did not answer within the wait window」路径。`_run_critic` 的等待循环抽成 `_await_service_turn`。
- 预算：旧执行器的 invocation 在 runtime 账本保持 UNKNOWN、不导入；只有回答过的执行器进 `imported_usage`。
- 事件键：重交接后的 `AgentCreated`/`InputSubmitted` 幂等键改用带后缀的 creation_key（否则被第一次的键吞掉）；旧 intent 与 legacy 意图键不变。`store.update_intent` 多写 `creation_key` 列。**无新增配置项**（策略快照 digest 不变）。
- 测试：`test_service_intent_provider_blocker.py` 7 条（planner 重交接后成功 / 两次未知走梯子 / synthesizer UNANSWERED / critic 重交接后回答 / critic 两次交回 runner / 上限取小 / legacy 不动）；`NEW_EVENT_TYPES` 加 `ServiceIntentRehandedOff`，「一个判定点」哨兵 18。
- 测试计数：`tests/orchestrator/full_target` **2719 + 2 skip**（上一段 2710，P2.3e +2，P2.3f +7）；旧模式回归 1 failed / 1854 passed / 20 skipped（唯一红仍是已知的 p33 AST 断言，零新增失败）；ruff 全清；`mypy src/agent_orchestrator` 17（基线）。

**P2.3g：MethodSynthesizer 提示词与编解码器逐字段对齐 + 一次结构化重试；Planner 回复里的 `<method_proposal>`。** 真实 Grok 局 H-L3-C1：合成轮写出了结构完整但按请求包形状拼字段的方法（`id/version/goal_signature_ref/parameter_bindings/…`），codec 报 10 个缺失字段即 `UNREADABLE` 收口；Planner 第 2、3 轮按 `planner-hierarchical-v1` 原文「改为只输出一个 `<method_proposal>`」照做，被记 `block_missing`，梯子烧尽。

- 提示词：新注册 `method-synthesizer-v2`（codec 逐字段清单 + 一个可解析的最小完整示例 + 点名不接受的拼法；`method-synthesizer-v1` 字节不动、可 pin）；新注册 `planner-hierarchical-v4`（Planner 永不提方法；无方法可用就输出 operations 为空、rationale 以 `no_applicable_method: ` 开头的 `<plan_revision_proposal>`），同包版本 2，pin v3 仍生效，未 pin 默认 v4。
- 请求包：`SynthesisRequest.to_json()` 多 `goal_type_ref`（目录里的 `{id,version,content_hash}`）、`method_shape`（codec 必填字段清单）、`schema_feedback`；合成意图 `context_version` 因此变化。
- 结构化重试：`accept_response` 把 codec 读不懂的回复命名为 `SynthesisReplyUnreadable`；`_collect_synthesizer` 记新事件 **`MethodSynthesisReplyUnreadable`** 后同锚、序号 +1 再问一次并把 codec 原话作为 `schema_feedback` 附上（`MAX_SYNTHESIS_ASKS = 2`，第二次调用同账户如实计费）；第二次仍不可读才 `MethodSynthesisRoundRecorded{UNREADABLE, asks:2}`。被注册协议 REJECTED 的回复不重问。
- Planner 侧：`parse_plan_proposal` 在文本只有 `<method_proposal>` 时抛 `BlockError(proposal_wrong_block)`，`PlanningRejected.reason = proposal_wrong_block`，`REPAIR_HINTS` 新增对应提示（「你是 Planner，不提方法……」）经既有通道进下一轮包；`operations == []` 在进 codec 之前抛 `NoApplicableMethodDeclared` → `PlanningRejected{no_applicable_method, rationale}`（无修复提示）。
- 宽容别名归一**不做**：三条真实回复没有一条能靠改名变成可准入（缺 `arguments` 值语言、`evidence_requirement`、content_hash），§18.5 边界拒绝不改写；理由与真值记在 journal §2i。
- 测试：`tests/orchestrator/full_target/test_synthesizer_schema_alignment.py` 16 用例（三条原文入 `fixtures/htn/replies/`）；6 条变异全 KILLED；`FROZEN_PROMPT_DIGESTS` 登记 synthesizer v1/v2、planner v3/v4；`NEW_EVENT_TYPES` 补 `MethodSynthesisRoundRecorded` 与新事件。既有三处测试随口径改（saturation VN 改为真被注册协议拒绝的方法；chooser 默认 v4）。
- 计数：full_target 2739 passed + 2 skip（基线 2719 + 2，净增 20 = 新文件 16 用例 + 冻结摘要表 4 条参数化）；旧模式回归 step02/05/06/07 + p34/p35：560 passed / 13 skipped / 0 failed（skip 全是既有的 real-provider / pinned tokenizer，零新增失败）（零新增失败）；ruff 全清；`mypy src/agent_orchestrator` 17（基线）。**无新增配置项**，策略快照 digest 不变，legacy 事件字节不变。
**P2.3h：根评审包必须携带可读证据、叶子准则按 criterion_links 限定、c-change-explained 有承诺人、requirements_revision 对齐。** 真实 Grok 局 `H-L3-C3-r0`：`code.fix-by-patch` 四叶验收齐全、隐藏评分器 PASS，根评审却 REJECTED，且三条 findings 对它看到的包全部成立——包里只有 artifact_id 没有正文；每个叶子（含 facts）的子评审把两条根准则全盖 PASS；`c-change-explained` 链在只交代码的 patch 步；叶子修订号 1–4 对根 5 被读成过期。分支 `p2.3h-root-review`，基 `8b8466d`。

- **包携带可读证据**（`orchestrator/root_review.py`）：每条 `accepted_outputs[]` 附 `excerpt`（经 `read_verified` 读内容寻址字节并复核哈希；UTF-8 文本原文，单件 `EXCERPT_MAX_CHARS=4096`、包级 `EXCERPT_BUDGET_CHARS=32768`，超出分别标 `truncated` / `omitted`；二进制、库里没有、字节不符 → `binary` / `unavailable`，只给 hash+size）与 `covers_root_criteria`；`criteria[]` 加 `covered_by`（承担该准则的 acceptance/task/叶子准则/evidence_requirement/ports）；contribution 加 `carries_root_criteria`（含 `leaf_review_verdict`）；`evidence.readable`。摘录进请求正文而非只引用：请求 `content_hash()` 即意图 `context_version`，摘录是内容寻址字节的纯函数，包 hash 可复现且覆盖评审员真正读到的内容；存库 `ReviewPackage` 契约字节不变。
- **叶子准则限定**（`orchestrator/leaf_acceptance.py`、`accepted_outputs.py`）：`criteria_for` 不再回退到 `binding.requirement_refs`（`refines_parent` 步骤原样继承的父准则）；叶子只欠自己的 `coverage_criteria`、`criterion_links` 链到本 occurrence 的根准则（按 `child_criterion_id`，statement 带 `evidence_requirement`）、或 `c-leaf-verified`。新 `CarriedCriterion` / `carried_criteria_in_revision` / `carried_criteria_for`。
- **承诺人**（`seed_methods/code/methods.json`）：`fix-by-patch` / `fix-by-revert` / `fix-by-assessed-revert` 的 `c-change-explained` 改链到 `verify`（report 端口，prose），`evidence_requirement` 改写为 report 必须展示的内容；`c-test-passes` 同步。Worker 上下文在 `declared_output_ports` 旁多一块 `carried_root_criteria`（`HierarchicalDispatch.carried_root_criteria_for`，与叶子验收读同一批行）；Worker 提示词不改。
- **修订号**：contribution 的 `requirements_revision` 改名 `accepted_at_requirements_revision`，顶层 `requirements_revision_semantics` 说明单调计数；数字本身不动。
- **提示词**：只加 `root-reviewer-v2`（`_revise` 自 v1，说明新字段；硬约束 1 补「证据在 excerpt.text、叶子 PASS 只对其承担的根准则有效、covered_by 为空判 false」，新增 1b「修订号小于根是正常形态」）；v1 保留可 pin，sha256 钉在测试里。
- 测试：`tests/orchestrator/full_target/test_root_review_evidence.py` 27 条（C3 原包夹具钉缺陷、摘录/截断/二进制/预算/篡改、叶子准则、承诺人、修订号、提示词冻结、**按证据判的脚本化评审员端到端到 COMPLETED** + 对照组 REJECTED、4 条变异全部 KILLED、I05 钉子）。full_target **2746 + 2 skip**（上一段 2719，+27）；旧模式回归 1 failed / 1854 passed / 20 skipped（唯一红仍是已知的 p33 `test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged`，与基线逐项一致，零新增失败）；ruff 改动文件全清；mypy 改动文件 0 条。
- 口径：根评审员看到的 JSON 变了（新字段、改名），`context_version` 因而不同；runner FREEZE-candidate 需重生成（methods.json）；契约变更请求 +1（`CriterionLink.evidence_port`，可选、默认退化为现行为）。
- **迁移口径（核验 P1-1）**：`code.fix-by-patch` / `code.fix-by-revert` / `code.fix-by-assessed-revert` 的 `method_version` **1 → 2**（criterion_links 是契约字节，`register_method` 对同 (id, version) 不同字节抛 `StoreConflict`）。已装过旧 code 域库的持久化 store 无需迁移：旧 `@1` 行保留（旧 mission 的 method_ref / plan revision 仍按原字节读回），新世界只准入并提供 `@2`，两版并存；点名 `@1` 的新提案按 §7.3 未准入拒绝。runner 的 FREEZE-candidate / 题单里引用 `code.fix-*@1` 的地方须改为 `@2`。测试 +4（旧库再装不炸 / 两版并存且规划取 @2 / 版本号钉住 / Worker 上下文块经 `_decide` 接线，核验变异 M6 已杀）；full_target **2770 passed / 2 skipped（核验基线 2765 / 3 skipped，收集数 2768 → 2772，+4；本机少一条环境性 skip）**。

**P2.3i：注册协议的「可修正拒绝」也重问一次；第二问预算不足按 `budget_exhausted` 如实停机。** 分支 `p2.3i-synthesis-reask`，基 c7cfedd（本条目候选）；版本号由协调方决定是否升 0.12.3。真实 Grok 局 `runs/h-arm` H-L3-C1-r0：合成器（v2）回复**已被 codec 读懂**——六步、六条序、两条 criterion link、每个 content_hash 与目录一致——注册协议只拒一条 `PORT_UNAVAILABLE: step 'summarize' binds input port 'report', which task type 'code.summarize-review' does not declare`（包里明写 `input_ports:["findings"]`，一处笔误）；P2.3g 的重问只覆盖「读不懂」，REJECTED 当结论，合成轮就此终结，Planner 三轮正确声明 `no_applicable_method` → `planning_failed`（32K token）。

- **可修正 / 不可修正划分**（`planning/htn/synthesis.py`，按 `RejectionCode` 逐个分类，import 时断言两集合划分整个枚举）：可修正 = 引用/形状类，包里已有正确值——`PORT_UNAVAILABLE`、`UNKNOWN_TASK_TYPE`、`UNKNOWN_OPERATOR`、`UNKNOWN_SCHEMA`、`FORM_MISMATCH`、`MALFORMED_DEFINITION`、`ORDERING_CYCLE`、`ROOT_COVERAGE_GAP`；不可修正 = `MODEL_CLAIMED_STATUS`（§18.5/§6.3 边界）、`ALREADY_REGISTERED`、`UNKNOWN_PREDICATE` / `PREDICATE_TYPE_ERROR`（前置条件，包里无谓词清单，I18 不放宽）、`UNKNOWN_CAPABILITY`（部署事实）、`UNBOUNDED_RECURSION`、`SIZE_BOUND`（策略界限）。`rejection_is_correctable(receipt)` 要求 REJECTED 且每一条 problem 都可修正。
- **重问**（`_collect_synthesizer`）：可修正拒绝 → 记新事件 **`MethodSynthesisReplyRejected`**（键按 ordinal，payload `goal_task_id / ordinal / method_id / verdict / problems`）→ 同锚、序号 +1、同 `mission_planning` 账户如实预留，`schema_feedback` = 协议原话（`CODE: detail`）+ 一句「只改点名的引用/形状，保留 method_id 与 method_version」；仍受 `MAX_SYNTHESIS_ASKS = 2`，第二次仍拒才 `MethodSynthesisRoundRecorded{REJECTED, asks:2}`；不可修正拒绝照旧不重问。新事件而非给 `MethodSynthesisReplyUnreadable` 加 `kind`：那个事件的名字与 `block_defect` 都在说「没被解码」，按类型过滤的读者不该看到注册判决混进来。
- **P2.3g 核验 P2-1 一并修掉**：第一问事件改为在第二问**真的开出来之后**才写；`BudgetExhausted` 单独接住 → `_stop_planning_round(reason="budget_exhausted", stop_reason=BUDGET_EXHAUSTED, detail{dimension, requested, remaining, account, phase:"method_synthesis", ordinal, goal_task_id, scope})`（与 P2.3d P0-1 的 Planner 轮同一口径，`run()` 正常返回）；其它开不出的原因写进 `MethodSynthesisRoundRecorded.payload.retry_refused` 独立字段，不再并进 `problems`。
- **提示词**：只加 `method-synthesizer-v3`（`_revise` 自 v2，两句：schema_feedback 可能是解码问题或以拒绝码开头的注册协议拒绝理由；后者只改点名的引用/形状——输入端口须在算子 input_ports、output 引用须在上游 output_ports、三个 ref 照抄、links/ordering 只引用自己的 local_id——保留 method_id 与 method_version）；v2 字节不动（sha256 `27ccb234…`）可 pin，默认 v3（`a38309fd…`），`FROZEN_PROMPT_DIGESTS` 两条都登记。
- 测试：`tests/orchestrator/full_target/test_synthesis_rejection_reask.py` 15 用例（C1 原文入 `fixtures/htn/replies/grok_synthesizer_c1r0_round1.txt`，对本仓库 code 域**逐字**复现拒绝、去掉一处绑定后同一注册表 TRIAL_ADMITTED、真 `HierarchicalDispatch` 上入库；真 `Orchestrator.run()` 端到端：记第一次拒绝 → 带原话再问 → 修正后 TRIAL_ADMITTED → `PlanRevisionCommitted`；不可修正不重问；两次拒绝 asks:2 无第三问；第二问预算不足两种第一问参数化停机原因如实）；6 变异全 KILLED；`NEW_EVENT_TYPES` 加新事件；`test_evidence_saturation` 的「被拒不重问」样例由未知算子改为未声明能力。full_target **2785 passed / 2 skipped**（基线 2770 / 2，+15 = 新文件 15 用例，其余计数不变）；旧模式回归 step02/05/06/07 + p34/p35：**560 passed / 13 skipped / 0 failed**（与基线逐项一致，skip 全是既有的 real-provider / pinned tokenizer）；ruff 全清。
- 口径：`MethodSynthesisRoundRecorded.payload` 多 `retry_refused`；合成意图 `context_version` 随默认 v3 变；**无新增配置项**，策略快照 digest 不变，legacy 事件字节不变（`_new_mode` 哨兵 18）；`contracts/` 0 改动。详见 journal 第四部分 §2k。
**P2.3j：根评审 REJECT 之后的修复轮方法库。** 真实 Grok 局 `H-L3-C1-r1` 与 `H-L3-C2-r0` 同形：合成一问 TRIAL_ADMITTED → revision 1 → 六叶 COMPLETED、六端口齐全 → 根评审 REJECTED（分别 `c-change-explained FAIL` / `c-test-passes FAIL`）→ `PlanningRejected{root_review_rejected}` → 修复轮 Planner 包 `method_library []` / `applicability []` / `open_compound_goals []` → `no_applicable_method` → `hierarchical_no_dispatchable_work` 空转（C2-r0 烧 292K token）。根因：包只按**未细化**的 open goals 取库，根被 revision 1 细化后修复轮什么都看不到；`goals_needing_method` 同样不认「被拒」。分支 `p2.3j-repair-round-library`，基 `c7cfedd`；`contracts/` 零改动（`RetireMethodOperation` 与编译/commit 侧的退方法路径 0.12.2 已有，缺的是 Planner 侧编排）。

- **修复轮包**（`planning/htn/planner_package.py`）：`method_library` 按 open goals ∪ 根评审拒绝的 occurrence 的 goal_signature 取（种子 + 已准入合成方法），每条加 `rejected_by_root_review`；`applicability` 对被拒 occurrence 重评、排除被拒 method_ref；新段 `rejected_refinements[]`（occurrence / goal / obligation / `rejected_method_instance_id` / `rejected_method_ref` / plan_revision / review_package_id / findings ≤8 条×1200 字 / requiredness / statement / typed_parameters / requirement_refs / repair_round）。「被拒」读自系统写的 `PlanningRejected{root_review_rejected}`（detail 现带 `occurrence_id` / `method_instance_id` / `method_ref`），只对当前 revision 上仍 ADOPTED 的实例生效。包版本 `planner-package-hierarchical-v4`、`HIERARCHICAL_PLANNER_PACKAGE_VERSION` 3。
- **换方法**（`orchestrator/hierarchical_dispatch.py`）：`compile_proposal` 接受「恰好一个 `refine` + 至多一个 `retire_method`」，retire 必须点名被 refine 的 occurrence 上 ADOPTED 的实例；裸 retire、对已细化目标不 retire 直接 refine 仍 `CompilationRefused`。编译走 `retire_instance_ids`，`build_command` 在有退方法时由系统置 `running_work_policy=REQUEST_STOP_THEN_RECONCILE`；同一 revision 内退旧上新，旧叶子随 membership 离开网络（TG §9.3）。
- **声明无方法 → 带 findings 的合成轮**：`goals_needing_method` 在修复轮 Planner 已答且没有新 revision（`repair_round_answered`）后把被拒根视为需要新方法，候选数排除被拒方法（I18 系统判不放松）；`synthesis_round = plan_revision + 1`，意图键 `…:round:{n}:{ordinal}`、`MethodSynthesisRoundRecorded` 键 `{mission}:{goal}:round:{n}`（round 1 键不变，载荷多 `synthesis_round`）；`SynthesisRequest.review_feedback`（`to_json` 多一键）。准入后照常开 Planner 轮 → 新 revision。
- **有界 / 诚实停止**：受 `max_root_review_repairs`（**每 Mission** 累计，默认 1；同一 revision 永不二次修复）与既有合成上限约束；`fail_mission(NO_DISPATCHABLE_WORK)` 的 detail 多 `root_review{reason: root_review_rejected, status, package_id, plan_revision, repairs_used, repairs_used_on_revision, rejected_method_refs, max_root_review_repairs, findings}`。
- **顺手修的潜在缺陷**：修复轮任何 commit 都会被 `READ_SET_STALE` 拒（`compile_proposal` 把 requirements 修订钉在 0，现传 `latest_requirements_revision`）；替换方法被 `BUDGET_INSUFFICIENT` 拒（`_materialise_occurrences` 把被退叶子按 ceiling 计入，现对网络不再命名的行只计 `min(ceiling, reserved+settled)`）；`compiler._merge` 让被退实例的孤儿 occurrence / 绑定 / 实例 / coverage / typed edge 离开合并网络；`_read_network` 跳过 RETIRED 实例。
- **提示词**：`planner-hierarchical-v5`（自 v4，说明 `rejected_refinements` 与 retire_method+refine 修复写法；包版本 3 只登记 v5，钉 v1–v4 的部署回落 v5）、`method-synthesizer-v4`（与 P2.3i 合并后自其 v3 修订，说明 `review_feedback`：必须不同于被拒方法、要回答 findings，例如先写一条会失败的测试；成为默认 `METHOD_SYNTHESIZER`）；旧版保留、sha256 钉住（`FROZEN_PROMPT_DIGESTS`：planner v5、synthesizer v3、默认 v4）。
- 测试：`tests/orchestrator/full_target/test_root_review_repair_library.py` 13 条（C1 夹具钉「修复轮包为空」1、包 3、编译 4、真 `run()` 端到端 3：换方法到 COMPLETED / 声明无方法→带 findings 合成→准入→新 revision→COMPLETED / 恒 REJECT 诚实 FAILED 且 stop reason 带 root_review_rejected、判断 2），夹具 `fixtures/htn/c1_repair_round/`；6 条变异全部 KILLED。full_target **2785 passed / 2 skipped**（上一段 2770，+13 测试 +2 冻结摘要）；旧模式 step02/05/06/07/p34/p35 560 passed / 13 skipped / 0 failed；ruff 全清；`_new_mode` 决策点 18 处与哨兵一致；legacy 事件字节 golden 不变。
- 口径：Planner / 合成器看到的 JSON 变了（新段、新字段），`context_version` 因而不同；runner FREEZE-candidate 需重生成；契约变更请求：无。未做：真实模型上跑通「REJECT → 换方法或合成 → ACCEPT」。
- **核验处置（`reviews/核验-P2.3j-83eaa84-合并fc07312-2026-09-17.md`，修后可合）**：P0-1 重新采用曾被退的方法实例撞 `method_instances` UNIQUE → `StoreConflict` 逃逸 `_cycle`、`run()` 崩、Mission 永远 ACTIVE——`compile_proposal` 对该 occurrence 上被根评审拒绝过的方法（任何 revision）按名拒绝（`method_rejected_by_root_review`）、对会与已存实例撞 id 的 draft 拒绝（`method_instance_already_stored`），`_collect_plan_hierarchical` 接住 `StoreConflict` 记 `plan_commit_refused{store_conflict}`；P1-1 修复上限按 revision 计、被拒历史不累计 → outer↔alt 振荡——上限改按 Mission 累计，新增 `rejected_method_refs`（跨 revision 的被拒方法集合）供库标记 / applicability 排除 / 候选扣除 / 编译拒绝共用；P1-2 `retire_method` 对任何 ADOPTED 实例放行且无人 stop/reconcile——只接受 `rejected_refinements` 内的实例，且被退子 occurrence 无未终态 Attempt（`retirement_not_a_repair` / `running_work_not_reconciled`）。补 V1/V5 两处测试盲区。测试 13 → 23 条，处置后变异 7/7 KILLED；full_target **2810 passed / 2 skipped**、旧模式 560 / 13 / 0；P2-1/P2-4/P2-5/P2-7 记入 journal §2l「未做」。

**P2.3k：Grok 第 2 批 L3 诊断的四条 SDK 缺陷（N3 / N1 / N2 / N4）。** 分支 `p2.3k-legacy-merge-on-htn`，基 fc07312（本条目候选 + P2.3i + P2.3j 合并）；版本号不动。诊断 `plans/taskSys2/升级planV1/impl/Grok验收-第2批L3诊断-2026-09-17.zh-CN.md`；`contracts/` 零改动。

- **N3（P0）根目标已解决后被遗留 artifact 冲突判定推翻**：C3-r0/r1 隐藏评分 PASS、根评审 ACCEPT、`GoalResolutionCommitted` 之后紧接 `MissionFailed{artifact_conflict}`——`_evaluate_criteria` 用 legacy `merge_accepted` 建裁决树，它以 `Task.dependency_ids` 判「独立分支」，而分层 occurrence 行该字段为空是设计如此，于是多叶各写的 REPORT.md 全是冲突。修法与 P2.3d D4 同形：`_evaluate_criteria` 加模式分支（`_new_mode` 第 19 处），分层 Mission 的树从根决议的贡献读（CURRENT 验收的 `accepted_artifacts` + P2.3h 的 `acceptance_outputs`），同路径后验收者胜、criterion_links 指向步骤的产物压过其余，不抛冲突；记一次新事件 **`ArtifactMergeNotApplicableUnderHierarchical`**（`superseded[{path, kept_task_id, kept_by, superseded_task_ids}]`）。legacy 的 `merge_accepted` 与其调用字节不动。
- **N1（P1）inspect / summarize 叶看不到补丁**：合成方法的 inspect 步无输入端口（`code.inspect-changeset@1` 不声明），工作区永远是未打补丁快照。目录新增 `code.inspect-changeset@2`（可选 `patch` / `report`）与 `code.summarize-review@2`（`findings` + 可选 `patch` / `report`），@1 行字节不动、仍可解析（存库方法 / P2.3i 回复夹具不受影响）；`MethodSynthesizer._offers` 每个 task type 只列最高版本；`method-synthesizer-v5`（承担「解释改动」的步骤必须通过输入端口接到补丁/报告），v4 字节钉住可 pin。`methods.json` 未改，无方法版本升级。
- **N2（P1）根评审包没有用户目标**：包里 `goal_statement` 只是目标签名模板句，C2/C4 的 `c-test-passes`「named failing test 由红转绿」对绿基线题不可满足。`RootReviewRequest` 加 `mission_goal`（Mission 目标原文）与 `goal_parameters`（根绑定 typed_parameters），进 `content_hash` / `context_version`；`root-reviewer-v3`（statement / evidence_requirement 以 mission_goal 为准解释；named test 基线已绿则要求覆盖用户目标的测试由红转绿或新增并通过），v2 字节钉住可 pin。seed 的 `c-test-passes` 措辞**不改**（对真红测试题是正确要求；另一半在 Host 构造），取舍见 journal §2m。
- **N4（P2）只读叶继承 code_test**：C3 的 facts / reproduce 叶因基线红各多烧一次 attempt。`occurrence_tasks.read_only_leaf(binding)`（side_effect ∈ {none, external_read} 且无 `*.write` 能力且无 resource_writes）；`occurrence_policy(read_only=)` 对只读叶不加默认 `code_test`（叶子自己的 `pytest:` 准则仍加；部署声明的策略只收窄不改写）。**范围（核验 P2-4 / P1-2）**：豁免覆盖全部只读类型——facts / reproduce / inspect / summarize / 各 observer 叶 → `format_check, rule_check`；但 **criterion_links 指向的叶不豁免**（`occurrence_task(criterion_linked=)`，由 `plan_commits` 按 `obligation_coverage` 传入）——`verify` 是 `external_read` 却仍含 `code_test`；apply-patch / revert-commit 仍含 `code_test`。**只读在运行时强制**：`read_only_rewrites` 在 `_collect_attempt` 检查只读叶是否改了它起步时就有的文件（seed ∪ 上游输入，受保护路径另有 `protected_path_rewritten`），改了 → `ResultRejected{read_only_leaf_rewrote_workspace, paths}`、不登记产物、走 RETRY_WAIT 反馈；新建文件不算改动。
- 测试：新文件 4（`test_hierarchical_judgment_tree.py` 5 含真 `Orchestrator` 端到端到 COMPLETED、`test_read_only_leaf_policy.py` 8、`test_root_review_user_goal.py` 5、`test_inspect_leaf_patch_input.py` 7），夹具 `fixtures/htn/c2_root_review/`（C2-r0 真包，去标识）与 `fixtures/htn/c1_inspect_input/`（C1-r1 存库方法）；6 条变异全部 KILLED；`NEW_EVENT_TYPES` 加新事件；`FROZEN_PROMPT_DIGESTS` 加 synthesizer v4、默认改 v5。full_target **2826 passed / 2 skipped**（本机基线 2800 / 2，+25 测试 +1 冻结摘要参数化）；旧模式 step02/05/06/07 + p34/p35 **560 passed / 13 skipped / 0 failed**；ruff 全清；`_new_mode` 决策点 19 处与哨兵一致；legacy 事件字节 golden 不变。
- **核验处置**（`reviews/核验-P2.3k-a5d2d3b-合并4022da9-2026-09-17.md`，「修后可合」，修在合并提交 4022da9 之上）：**P1-1** 裁决树权重改 `(linked, port, order)`，被 link 的端口产物永不丢弃——同路径输掉的一份保留在 `accepted-outputs/<task>/<port>/<path>`，`superseded[]` 记 artifact id / content_hash / kept_at，两 linked 写者相争 `kept_by=acceptance_order_between_linked`（C1-r1 形状真 `Orchestrator` 端到端：评审 c-test-passes 所读的 verify REPORT.md 在交付树里）；**P1-2** 见 N4 段（linked 叶保留 code_test + 收集处强制只读）；**P1-3** `goal_parameters` 进包前脱敏（`sanitised_goal_parameters`：工作区根 `<workspace>`、根下 `<workspace>/<rel>`、其它绝对路径 `<path>`），同一状态换 worktree 路径 `content_hash` 不变；**P2-1** 补「REVOKED 验收不进树」测试（M7 已杀）；**P2-3** `_offers` 先按 primitive/域筛再取最高版本。核验后变异 V1–V5 全部 KILLED。
- 口径：根评审员看到的 JSON 多两个字段（参数已脱敏）、合成器 `operators` 少 @1 行、默认提示词 root-reviewer-v3 / method-synthesizer-v5（旧版可 pin）、分层叶 `verification_policy` 变（只读且非 linked 的叶无 code_test）、新 `ResultRejected` 理由 `read_only_leaf_rewrote_workspace`、裁决树可能多出 `accepted-outputs/…` 路径；runner FREEZE-candidate 需重生成。**无新增配置项**，策略快照 digest 不变，legacy 事件字节不变。未做：真实模型上四项均未验（第 3 批）；Host 侧 H1 / D2a 未动；Host 给 RequirementsRevision 加 `pytest:` check id（核验 P1-2(c)）。详见 journal 第四部分 §2m（含「核验处置」）。

## 0.12.1 — P2.3d：Grok 验收暴露的分层闭环缺陷修复（2026-09-17）

代码候选提交 b6b7700（分支 `p2.3d-fix`，基于 `e53395c`，已 ff 进 main）；发布身份以本条目所在的版本提交为准。独立审阅（需修后合）与两轮独立核验的记录在 `plans/2026-09-16-full-target/P2.3c/reviews/`；审阅处置后的真实模型冒烟再次 COMPLETED（`mission-1ac94ffc1d28e2b0`，deepseek-flash，454 097 token，journal §2f）。Grok 验收 H 臂 40 局 `mission_status = COMPLETED` 为 **0**，四类失败全部可复现、全部确定性；本段按诊断报告逐条修复。旧模式（`orchestration_semantics_version=legacy`，默认）零回归：legacy 事件字节 golden、旧函数源码 hash、`_ExplodingDispatch` 三例全绿。

- **D3 终结步输出端口**：端口集合的定义由「被 `DataRequirement` 消费」扩为「被 `DataRequirement` 消费 **∪** 被 `composition.criterion_links` 引用（尤其 `finalizer_step`）」。新增 `accepted_outputs.output_ports_in_revision()`，上下文包、验收索引与 `OUTPUT_PORT_UNCLAIMED` 三条生产路径共用同一答案（另含诊断漏点的第四处 `leaf_acceptance._outputs`）。此前终结步叶子从未被告知端口、`outputs` 永远为空、根评审以 `evidence.kind=none` 判 FAIL，10 局倒在最后一米。
- **D4 分层 Mission 不再开 legacy 管理轮**：`_request_management` 加模式分支，记新事件 `ManagementNotApplicableUnderHierarchical{SEMANTICS_IS_HIERARCHICAL, redirect: commit_plan_revision}`；不再产生 `ManagementRequested` / `ManagementDecided`，manager 轮次不再被必然失败的修复循环烧掉，`no_progress` 上限也不再作用于分层 Task（短路后不进 `_enforce_no_progress` 那条路，重复验证失败只受 `max_attempts` 约束）（L1 20 局 `management_exhausted` + L4 3 局 `no_progress`）。
- **D1 AppWorld 分层 Worker**：新增提示词 `worker-appworld-hierarchical-v1`（由 `worker-appworld-v3` 派生，保留六个 AppWorld 工具含 `appworld_execute`，加 decision 4 的 `outputs` 字段）；`HIERARCHICAL_WORKER_VERSIONS` 改为域模块注册后冻结；`_hierarchical_worker_template` 的兜底按 `domain_for(mission)` 选域版本；域→版本指针放在新映射 `governance.domains.HIERARCHICAL_WORKER_TEMPLATES`，**不进 `DomainProfileV1.role_templates`**（那张表被调用方整体遍历为角色名），域档 `APPWORLD_PROFILE` 逐字节未动。此前 L1 20 局 Worker 报 `tool_not_exposed` 且读到代码域「跑 pytest」的提示词。
- **D5-A 根验收 REJECT 的修复路径**（§9.1 最小分支）：findings 含 `severity=blocker` 时，把 findings 作为 `PlanningRejected{root_review_rejected}` 反馈重开一轮 Planner；上限 `OrchestratorConfig.max_root_review_repairs`（默认 1 次/plan_revision），超限才进 idle stall。
- **D5-B 嵌套 compound**：规划触发条件扩为「`CREATED` 或存在未细化的 compound 占位」，每 plan_revision 一轮；判据与 `goals_needing_method` 相同（`adopted_instance_for is None`，不是 `ReadinessReason.NEEDS_REFINEMENT`）。已提交计划的 Mission 不再被一次事后规划轮判死。
- **D2c 规划拒绝理由码拆分**：`__cause__` 是 `BlockError`（或回合根本没 COMMITTED）→ `proposal_unreadable`；读懂了但被内容规则拒 → 新理由码 `proposal_not_grounded`。`max_planning_attempts` 出厂默认仍是 2（改默认会动配置快照字节；runner 显式传参）。
- **D2b 证据饱和**：`goals_needing_method` 新增「同一命题被同一观察器 OBSERVED ≥N 次且真值仍 UNKNOWN」的判定，该 `NEEDS_EVIDENCE` 视同可被别的方法绕开、准入合成轮；N = `HierarchicalDispatch.evidence_saturation_rounds`（默认 2）。**I18 未放宽**：不把 UNKNOWN 变成 TRUE，也不开安全闸。
- 冒烟收口断言补强：`test_real_provider_hierarchical_smoke` 现在要求**每一条** `AcceptanceCommitted.accepted_outputs` 非空——0.12.0 的 COMPLETED 是评审员宽容而非机制成立（同一缺陷下 deepseek-flash 判 ACCEPT、grok-4.6 判 REJECT）。
- 独立审阅（对抗式，结论「需修后合」）的处置，全部先红测试后修：**P0-1** D5-A/D5-B 新开的 Planner 轮没接 `BudgetExhausted`，账户见底时整个 `run()` 带 traceback 退出、Mission 停在 ACTIVE 且没有 `MissionFailed`——现在走 `_stop_planning_round`（仍在 PLANNING 的逐字节走老路，已有计划的走 `fail_mission`，并级联停掉未完工作）；**P1-1** D2b 只做了半程——合成方法准入后无人再问 Planner、准入只进内存不进库（`compile_proposal` 读的是 `htn_store`）、规划梯子还跟合成轮赛跑——三处都补上：合成轮在途时梯子等待，每条准入的方法买一级 (`_synthesis_credits`)；**P1-2** 冻结提示词 `worker-hierarchical-v1` 写「required=true **且下游确有消费者**的端口必须被认领」，与 D3 相反——新注册 `worker-hierarchical-v2`（v1 保留可 pin、摘要仍冻结）；**P1-3 / P2-5** D5-B 「每修订一轮」的守卫既没被测试钉住（变异 M11 存活）也只活在进程内——补测试并改为 durable 事件 `HierarchicalRefinementRequested`。另修 P2-2（修复轮自己的拒绝理由被同 ordinal 键吞掉）、P2-4、P2-6（optional 的 criterion-linked 端口不进集合，变异 M05 补杀）、P2-7（按 occurrence 取 binding，不再按下标对齐）、P2-8、P2-10。独立核验又抓出 P0-1 的一处残留——修复轮/细化轮**被拒之后**，`_planning_rejected` 沿普通梯子开的下一级仍是裸调用（runner 传 `max_planning_attempts=3` 时必然走到），`BudgetExhausted` 照样逃出 `_cycle()`——已一并接上；另补齐 6 条存活变异的测试（`fail_planning` / `fail_mission` 的分辨、`RoutingUnavailable` 与 `ContextRejected` 对 ACTIVE Mission 的处理、`method_synthesis_refused`），并把 `ContextRejected` detail 新增的 `ordinal` 键限制在非 PLANNING 路径上，**legacy 的 `MissionFailed.detail` 因此一个字节都没变**。
- 口径变化：`OrchestratorConfig.to_json()` 与 `policy_snapshot()["config"]` 多 `max_root_review_repairs` 键（配置 digest 改变；该字段已登记进 `governance.policies.SNAPSHOT_FIELDS`）；`HIERARCHICAL_WORKER_VERSIONS` 不再是单元素集合。域档版本**未**变（`resolve_domain("appworld-v1").version` 仍是 `"3"`）。
- 测试：`tests/orchestrator/full_target` **2710 条**（上一段 2648）；旧模式回归 1 failed / 1966 passed / 20 skipped（唯一的红为已知的 Python 3.14 `ast.dump` 漂移）。真实模型冒烟已跑到 **COMPLETED**（DeepSeek 官方端点 `deepseek-flash`，`mission-3243bfd86e5035d5`，四叶含终结步 `report` 端口全部有产物，根评审 1 刀 ACCEPT，137 906 token；详见 journal 第四部分 §2c/§2d）。

## 0.12.0 — FULL-TARGET-1.4 P0–P2：HTN 递归任务分解端到端（2026-09-17）

候选提交 00a4379（P2.3c 第三部分 c）。旧模式（`orchestration_semantics_version=legacy`，默认）零回归：legacy 事件字节 golden、allocator/frontier 源码 hash、冻结提示词摘要全部不变。

- 契约：TaskSemanticBindingV1、MethodContract/MethodInstance、ObligationLedger（递归燃料 BOUND_REACHED）、四值谓词逻辑（§6.6）、ValidityWitness 与 epoch 屏障、DataRequirement → BoundInput → InputManifest、Acceptance / GoalResolution / DeliveryReceipt 分离、OperationEnvelope 三轴。
- HTN 核心：方法注册表与准入、grounding、refinement（AND-OR、共享只读子目标）、编译为 TaskNetwork、PANDA/HDDL 后端适配（本机 stub）。
- 接线：`commit_plan_revision`（ADR-13：整数闸门 + 语义 read-set 11 通道，不 rebase）、occurrence→Task 物化与预算份额守恒、`allocate_v2` 派发闸门（readiness → admit_for_dispatch，form=compound 永不派发）、未装配部署 fail-closed、叶子验收链路与 `acceptance_outputs`、Worker 显式输出端口声明、demand 在计划提交事务内 admit、根 MISSION_FINAL 评审裁剪协调器（有界重裁、系统永不自填 PASS）、Mission COMPLETED 只经根 GoalResolution、空转确认一轮后 `no_dispatchable_work`。
- 存储：迁移 16（31 张 STRICT 表）、17（验收回执与输出索引）、18（见证主体列）；旧迁移校验和不变。
- 观察器：code / appworld 只读谓词观察器（允许表、三态、解析失败永不为 FALSE），L2 业务状态谓词六条。
- 真实模型冒烟（deepseek-flash）：分层 Mission 由根 Resolution 触发到 COMPLETED。
- 策略快照 digest 因新增 `max_root_review_cuts` 变为 `7cf60224…`；分层 Mission 收尾两次 mission 账户模型调用（root reviewer + Mission Judge）。
- 测试：`tests/orchestrator/full_target` 2648 条；编排范围 4614 通过（p33 的 Python 3.14 AST 断言为已知失败）。

## 0.11.0 candidate — P3.3 A–E（F验证完成，未发布）

领域、引用、准则评估、证据不足出口、来源失效与文档人工仲裁的SDK候选；F完成无新增回归与干净安装验证（存在明确既有失败），G文档报告交付仍待完成。

F 验证完成（保留既有红集）：干净源码 `5bcca08fe666b8e20524206b76ce2afbba63db4d` 整仓3241 passed /60 failed /18 errors /13 skipped（547.16秒）；与同依赖旧源码a4aae8c的78项红集按kind+nodeid完全相同，新增0。0.11.0安装验证1402 passed /11 skipped /1既有迁移失败（506.99秒）；304包文件逐字匹配，258实际加载模块均来自安装包且哈希一致。F不是整仓全绿或新正式发布；G、Host与真实flash仍未完成。

## 未发布 — P3.3 切片 E 源码里程碑

E 干净源码 `cf40b8ec86a2f307d0f8b8f89cf7f0166e5de121` 完整编排 **1199 passed /8 skipped /0 failed**（481.36秒），watchdog481.63秒，PG25036无残留。8项skip为未启用真实Provider。A–E完成SDK源码验证；F/G、wheel、Host与真实flash仍未完成。

## 2026-09-12 Phase3 D 源码里程碑

文档证据不足可带结构化局限有限接受；补证据重试受冻结额度约束，人工恢复需同结果批准；Mission 超过半数原始准则证据不足时明确停止。DOC3 adapter 与 schema3 保留旧数据兼容。542 项定向通过；干净d3d3fd8完整编排1119 passed /8 skipped（446.64秒）；未发布新 wheel。

## 未发布 — P3.3 切片 C（SDK 源码，验收中）

- 新增 schema 10 准则评估，绑定冻结合同/引用/claim revision/产物，accept 同事务落盘；旧 accepted 记录不回填。
- 文档来源归属由系统构造，推论封顶 SUPPORTED；下游信任标记、保留键、同级显式争议及同身份 supersedes 已接通。
- 旧规则评估重跑保留已完成 Critic 来源；完整长引文可在正式记录中带出处，模型输入上限不变。

定向 P33 + step04 445 passed / 1 skipped；963b090 干净全量 979 passed / 8 skipped（475.47 秒），旧 code 重放兼容已修复。尚未发布新 wheel 或完成 D–G/Host/真实 Provider 验收。

## 未发布 — P3.3 切片 B（SDK 源码验证完成）

来源登记进 CAS，来源更替与撤销复用审批并保留历史；任务意图冻结版本。
来源根只读并保持信任标记；重试不继承撤销资料，重启不抹掉篡改证据。
新增严格 SourceCitation（契约 schema 2）和七种失败状态的确定性解析，匹配坐标与包含限定条件的展示块分开。
文档结果禁止 pytest/tool-run 证据；发布 handoff 检查整个库的来源存储隔离。
定向 300 passed；独立审查无剩余 P1/P2，干净源码 `fb58bf1` 编排全量 867 passed / 8 skipped / 0 failed（488.39 s）。8 个真实 Provider 用例未启用。未发布新 wheel，完整知识分级/Host 原生门仍待 C–G。

## 未发布 — P3.3 切片 A（SDK 源码）

文档领域的角色、上下文与工具集现在遵循 Mission 冻结的领域快照；code-v1 保留原默认提示与政策选版。
旧 Critic 执行恢复后按真实 intent 记录版本，人工审阅仅复用匹配层。补齐其余四个 Task 入口的拒绝/回滚控制。
嵌套工作区的 pytest 8 不再读取父目录配置或 conftest，保留工作区配置优先级，不扩大沙箱权限。

源码 `1eaa91f`：编排全量 651 passed / 8 skipped，真实 Provider 用例未启用。
P3.3 来源、分级、Host 接线与原生验收尚未完成；没有发布新 wheel。见 [handoff](plans/2026-09-12-phase3/HANDOFF.md)。

## 0.10.0 — agent_orchestrator 0.10.0: isolated execution and real, controlled delivery (P3.2)

Model-written code now runs through a **sandbox executor port** (`runtime/sandbox.py`).  The
receipt of every run says what really happened: whether it was isolated, which limits are
hard (CPU time, file size, wall clock, output) and which are soft (memory, process count —
sampled and reaped, because this platform has no cgroups), and whether every process of the
run is gone, measured after the reaping.  The macOS **seatbelt adapter** denies the network,
denies file content outside a read whitelist computed from the interpreter it runs, allows
writes only into the run's own throw-away copy and scratch directory, and denies signals,
mach lookups and information about other processes.  Its processes are found by *sandbox
identity* (a per-run canary plus `sandbox_check`), so a grandchild that left the process tree
with `setsid`, a double fork, `chdir("/")` and closed descriptors is still found and removed.
`ProcessOnlyExecutor` is **not** a sandbox and says so (`isolated=false`) — for trusted code.
A capability probe (8 behavioural checks) must pass before a deployment may call itself
sandboxed.  `DeploymentPolicy.code_execution` is `off` / `sandboxed` / `process_only`; the old
`local_code_execution` switch keeps working in both directions.

**Artifacts are content-addressed** (`artifacts/store.py`): bytes are written once, read-only,
under `<evidence_root>/artifacts/sha256/`, and every reader goes through one entry that
re-checks the hash and never follows a symlink.  They outlive the workspace.  Model-written
code runs in a **throw-away copy**, so what it writes never reaches the Attempt's own tree,
and the **verification copy is rebuilt from the recorded bytes** — what is verified is what
was recorded and what an approval binds.  Every copy the system makes refuses a symlink
(`workspace_symlink`) instead of dereferencing it.  Workspaces are **registered** (schema v7):
a rebind compares the registered identity, never the directory's content, and finished
Missions' directories are cleaned after a retention period while the registry rows and the
bytes stay.

**Real delivery**: `FilePublishConnector` publishes one verified Artifact into a directory the
user authorised.  The commit point is a single `os.link` — it fails if the name is taken, so
nothing is overwritten — and the connector writes its intent to its own ledger before it, so
every crash is decidable.  A connector now declares `lookup_authority`; an action at L2 or
above may only run on an authoritative one, and "I found nothing" from a best-effort lookup
leaves the action UNKNOWN for a person instead of being retried.  **Compensation** is a new
business action (`<action>#comp-<n>`) with its own approval and idempotency key — the original
fact stays as recorded — and `MissionControlV1.propose_compensation` is the way in.  A
candidate names *which* Artifact to publish; the system binds its identity.

Recalled Journal text no longer reaches a model as a SYSTEM instruction: it arrives as a USER
message inside an untrusted-history frame it cannot close early.

Correction to the 0.9.11 notes below: the Task budget floor is a *necessary condition at
reservation time* — it assumes one turn settles within its reservation — not a promise that a
first Attempt and its Critic will both fit (review P2-2).

## 0.9.11 — agent_orchestrator 0.9.4: P3.1 follow-up fixes (Phase3)

`agent_orchestrator` 0.9.4 (same wheel).  Fixes found by the Host's native acceptance
(plans/2026-09-12-phase3/p31-fixes).  **Task budget floor** (F-ORCH-1): the Graph Manager
refuses a Task whose effective token budget cannot carry a first Attempt and its Critic —
`k × (base + critic)` with k candidates per Task from the Mission's bound policy, the critic
part only when the policy names critic_review, `base` the largest
`default_max_output_tokens` of the config and every profile (`OrchestratorConfig.
min_task_tokens`: None = derived, 0 = off).  Initial graphs and graph changes alike;
refused with `task_budget_below_floor`, the Planner and the Manager are told the floor in
their input packages and why the proposal failed — nothing raises a model's number
silently; system tasks are exempt; the floor is a necessary condition, not a promise that
repairs will be affordable.  Only the Orchestrator injects the floor — a bare
`CommitService`, `validate_graph` or `validate_change` behaves as before.  **Artifact
verification status** (F-ORCH-3): an accepted result's artifacts become VERIFIED and a
failed result's REJECTED, each in its commit transaction
(`Store.update_artifact_verification`); superseded candidates stay UNVERIFIED.  An
Attempt's RETRY_WAIT after its Mission ended is by design (§25.2: a failed Attempt's
terminal state) — documented, unchanged.  (0.9.8–0.9.10 were the Host-support slices: the
local code execution switch, the P3.1 external control facade and their review fixes;
recorded in plans/2026-09-11-agent-orchestrator/host-support-0.9.8/.)

## 0.9.7 — agent_orchestrator step 9: learning from history and controlled promotion (source candidate)

`agent_orchestrator` 0.9.0 (same wheel).  Step 9 of ORCH-BUILD-v1.0 — the last step of the
original design's stage four (§28: "收集 Trace → 离线训练或规则改进 → 生成新策略版本 →
Offline Evaluation → A/B Test → 审批后上线"; "不要让在线 Agent 直接自我修改核心安全和调度
规则").  **Policy registry** (orchestrator schema v6): a policy is the promotable layer
over the deployment configuration — the §29.3 allocator weights, candidates per task,
exploration slots, a per-Mission concurrency under the deployment cap, the Manager
thresholds, the aging window, routing overrides and each role's prompt version — always
stored resolved and content-addressed; safety boundaries, budgets, the deployment
policy, ablations, timeouts and layer switches can never be part of one.  Proposals
carry their provenance; evaluation → human approval (nonce, receipt) → promotion →
rollback follow a closed state table, with a bounded step, a cooldown
(`DeploymentPolicy.policy_cooldown_seconds`) and no widening under backpressure; a
rollback is immediate and returns only to a previously ACTIVE version.  **Binding**:
every Mission is bound to one version in its creation transaction (`MissionCreated`
carries it, Replay projects it) and runs under it to the end — allocation, routing,
Manager thresholds and prompt templates read the bound version, never a later promotion
or another configuration; a production library seeds the resolved built-in policy,
records configuration drift and interpreter drift, and evaluation libraries take pinned
policies only.  **Rule improver** (`governance/learning.py`, `rules-v1`, a heuristic —
never called a trained model): reads history read-only, refuses untrustworthy
(replay / attribution), mixed or thin history and registers nothing then; R1 moves
allocator weights on contested allocations, R2 routes a failing task kind to its
escalation target; reputation per role × prompt × profile is evidence only.  **Gates**
(`governance/gates.py`): candidate vs ACTIVE on held-out cases in new evaluation
libraries, the step-8 statistics (per case and side, harness errors → INSUFFICIENT,
clearly-worse cost only), task-identity leakage refusal; PASSED means non-inferior
within the samples.  Online Agents cannot set policy: a Worker's `policy/` file or a
Manager's configuration operation is refused on record.  `PolicyApi`, CLI `policy
propose / evaluate / approve / reject / promote / rollback / list / show / status`, `demo
--scenario policy-promotion`.  No SDK (`simple_harness`) API change.

## 0.9.6 — agent_orchestrator step 8: contribution attribution, replay and policy evaluation (source candidate)

`agent_orchestrator` 0.8.0 (same wheel).  Step 8 of ORCH-BUILD-v1.0 — explaining one run
and comparing strategies on evidence (original §23, §28 stage four).  **Replay**
(`observability/replay.py`) rebuilds facts that already happened: a pure fold of a
Mission's events (ordered by seq, deduplicated by event id) into the formal state —
Mission, Tasks, Attempts, Results, knowledge, conflicts, actions, approvals, overrides —
compared field by field with the library, which is only ever read through a copy opened
read-only (`Store.open_readonly`); for this build's libraries the coverage is 100 %,
missing or older events are reported as `not_covered` with structural gap rules and the
missing event ids, never back-filled; a failure timeline tells what went wrong in order.
New events `ActionSuperseded` / `ActionCancelled` put every action state change on
record; events are read page by page.  **Attribution** (`observability/traces.py`)
follows the final products (the integrated tree) to the Tasks, Attempts, Agents, roles,
models and prompt versions that produced them and the layers that passed them, adds the
knowledge path, actions and people, lists everything else as exploration with its
reason, and splits the imported usage row by row (Attempt, its Critic, planner /
manager / judge) with an unclassified bucket that must stay empty; unpriced money is
null.  **Policy snapshot** (`governance/policies.py`): every configuration field
classified, every version constant with its source, role templates, profiles, routing,
connectors and provider identity without credentials; `snapshot_diff` names each
difference's source; evidence adds `attribution.json` and `policy_snapshot.json`.
**Ablation** is an explicit change of the effective policy (`OrchestratorConfig.ablations`:
critic, blackboard, graph_changes; safety boundaries refused): an ablated layer is
recorded `NOT_REQUIRED` with `ablated=true`, never a silent PASS.  **Evaluation**
(`observability/evaluation.py`): cases × strategies × trials, every run a new Mission in
its own new directory and library, whitelisted strategy overrides, test services only,
harness errors kept out of the denominator, success rates with Wilson intervals,
comparisons by Fisher's exact test and non-overlapping ranges, hidden oracles for
verification misjudgment, fixture results marked as a mechanism check, cases derived
from old evidence only when the charter hashes to the recorded spec.  CLI `replay` and
`evaluate`, `demo --scenario evaluate-policies`.  Code review round 1: an evaluation
refuses any service but the local test service on every case and again on what each run
is handed (`EvaluationRefused`, never a harness error); an ablation that leaves a Task no
verification layer is an `ERROR`, never a zero-layer PASS; a missing outcome event is a
structural gap and its field becomes `not_covered`; attribution flags refuted claims on
the path (`claim_refuted`) and `reconciled` also checks the budget ledger; comparisons
are paired by case and say "insufficient evidence" on opposite directions or uneven
harness errors; `replay` / `evaluate` answer bad calls with exit 2.  No SDK
(`simple_harness`) API change.

## 0.9.5 — agent_orchestrator step 7: human-in-the-loop and controlled real actions (source candidate)

`agent_orchestrator` 0.7.0 (same wheel).  Step 7 of ORCH-BUILD-v1.0 — the "scale and
safety" stage of the original design (§14.4, §15, §21–22, §24).  An Agent never performs
a real change: it writes an action candidate (`actions/<name>.json`: connector, operation,
target, params, reason) declared in its Task's outputs.  Verification always checks a
candidate (schema, deployment policy, the Mission's `action:<connector>.<operation>:<target>`
scope, declared outputs) and the accept transaction re-reads the stored bytes and
registers it in an action ledger (`actions`, schema v5) under a stable business action id;
changed content is a new version that supersedes only an *open* one — a handed-off or
executed version is never superseded (`action_in_flight` / `action_already_executed`).
Risk levels follow original §22 (L0/L1 automatic, L2 one approval, L3 two; connectors
declare levels, deployments may only raise them; connectors are off unless a deployment
enables one, and L2+ needs idempotency and reconciliation).  Approvals (`approvals`,
`approval_decisions`) bind mission, task, action id, version, params hash and artifact
hash; decisions come only from a caller's `Principal`, carry a nonce and a receipt hash,
are counted once per receipt (and once per principal for L3 by default — a deployment
convention), and can be rejected, revoked, expire or be cancelled with the Mission.  The
Action Executor (`runtime/actions.py`) hands off only as the last step of the Mission
judgment: `begin_handoff` re-checks the binding, reserves `action:<key>` (one tool call)
and writes HANDED_OFF with owner, lease and decision receipts in one transaction before
the connector is called in a thread under a timeout; a mismatching or lost receipt is
UNKNOWN (reservation held), reconciled by idempotency key (COMPLETED → SUCCEEDED,
CONFIRMED_NOT_STARTED → one re-hand-off with the same key, otherwise a person rules with
evidence); an ended Mission is still reconciled.  The judgment runs in two stages (non-action
criteria booked once per integrated tree; waiting for a person is no progress, so `run()`
goes idle; the runtime cap excludes human waiting).  Human review is deployed as the sixth
verification layer: a result waits SUSPENDED for a person, resumes reusing the layers that
passed, and a Critic may answer `needs_human` (never short-circuiting the code tests, one
escalation per Task).  Verifier conflicts go to a person as arbitration (a Conflict Task
out of attempts; a judge Critic disagreeing with the Task Critics).  Takeover (stop /
retry with a note), comments and rulings are `HumanOverride` / `HumanCommentAdded` events
with their basis and never widen scope.  `api/approvals.py`, CLI `approval
list|approve|reject|revoke|comment|review|arbitrate|takeover|resolve --as`, `demo
--scenario approval-action` (local test configuration service; `--pause-for-approval`),
evidence `actions.json` / `approvals.json` and trace / metrics sections.  A dedicated test
service passing grants nothing for production.  No SDK (`simple_harness`) API change.

## 0.9.4 — agent_orchestrator step 6: many Missions, many models, backpressure, isolation (source candidate)

`agent_orchestrator` 0.6.0 (same wheel).  Step 6 of ORCH-BUILD-v1.0 — controlled
concurrency.  Missions share one orchestrator under an optional Global Budget
(`budget:global` above every Mission account, §18.2; unnamed dimensions inherited) and
two new budget dimensions, tool calls (reserved per Attempt, settled on the gateway's
count, capped at the gateway) and wall-clock runtime; a pool that cannot fund planning
stops the Mission visibly.  `scheduling/backpressure.py` registers §18.5's six caps and
raises / clears backpressure on high / low watermarks (hysteresis) — state and the
`BackpressureRaised` / `BackpressureCleared` events are written in one transaction; while
raised the Allocator halves Worker concurrency, expands only conflict and starving Tasks
plus one exploration slot, shrinks reservations and refuses `add_task` from the Manager.
Verification runs in a bounded set of tasks (`verifier_workers`).  `runtime/model_router.py`
maps runtime profiles (provider, model, price, output caps) to one `AgentRuntime` and one
execution library each; the route is frozen into the dispatch intent (`ModelRouted`) and
proven by the provider echo; failures climb the §9.3 ladder to a stronger profile with the
earlier Attempt kept; an unavailable profile falls back or makes the Task wait a bounded
time (`runtime_unavailable`); a restart only resumes an Attempt in its own pool.  Tool
permissions are Mission ∩ Task ∩ Role ∩ Deployment; the Tool Gateway checks in §21.1
order (identity, permission, schema, policy, rate) and every refusal is a
`ToolCallRejected` event; upstream inputs are read-only in a downstream workspace;
`Artifact.workspace` records the producing workspace.  An undeployed verification layer
is refused at commit (`verification_policy_undeployed`) and blocks at run time
(`verifier_unavailable`).  Evidence adds `trace.json` (per-Attempt versions: profile,
requested and echoed model, prompt, context, retrieval, allocator, router, verifier),
`metrics.json` and `scheduler.json`, and is scanned for credentials before it is written.
CLI `demo --scenario multi-mission`.  Schema v4 (`scheduler_state`, tool-call columns).
No SDK (`simple_harness`) API change.

## 0.9.3 — agent_orchestrator step 5: the Task DAG changes on execution evidence (source candidate)

`agent_orchestrator` 0.5.0 (same wheel).  Step 5 of ORCH-BUILD-v1.0 — the dynamic Task
Graph (§6.2) without touching the §25 state machines.  A non-candidate Result Envelope
(blocked / failure / no_progress / proposed_subtasks) is kept as history and never
verified; it opens exactly one deduplicated management decision per trigger.  The Manager is
a BaseAgent role (`manager-v1`) that sees the trigger, the Verifier's feedback, the
affected subgraph, the graph version and the hard limits, and answers with a
`<graph_change_proposal>` from a system-defined operation vocabulary (add_task,
supersede_task, retarget_dependencies, set_priority, pause/resume_task, cancel_task,
set_role).  `commit_graph_change` validates the whole proposal against the merged graph
(cycles, depth, task count, proposals per source Attempt, the Mission budget pool counting
settled and in-flight tokens of superseded work, duplicates, sibling outputs, goal drift,
§25.1 legality) and applies it in one transaction with a graph_version CAS (disjoint stale
proposals are rebased, overlapping ones refused), an idempotent receipt and a
`graph_changes` ledger (schema v3): executing Tasks are superseded by a new entity
(ACTIVE→CANCELLED, late candidates history only), BLOCKED dependents are rewired in place,
completed Tasks are only referenced.  Artifact lineage now orders ancestors topologically
from the edges.  Repeated no-progress must end in a change of approach (§29.2 worker
variants explorer / exploiter / simplifier / connector / failure_analyst) or an explicit
stop (`no_progress`, `management_exhausted`); a refused proposal is fed back once.  The
Allocator ranks the frontier with §29.3's starting formula (weights verbatim, versioned
input scales, conflict Tasks first, starvation guard after an aging window) and freezes the
score on the Attempt.  Evidence adds `graph_history.json` (v1 → v2 with basis and old work).
CLI `demo --scenario dynamic-dag`; kill switch `dynamic_graph`.  Review round 1: `pause_task`
is legal only for READY/BLOCKED Tasks; a Manager decision is also requested after a PASS that
still carries `proposed_tasks` and after `manager_after_failures` verification failures; the
manager intent settles only once the change is durable; `AllocationDecided` carries the frozen
score; an `add_task` without a budget gets a bounded share.  No SDK (`simple_harness`) API
change.

## 0.9.2 — agent_orchestrator step 4: team knowledge, conflict arbitration, synthesis (source candidate)

`agent_orchestrator` 0.4.0 (same wheel).  Step 4 of ORCH-BUILD-v1.0 — the Blackboard (§11)
in four layers.  Claims stay proposals with typed fields (`key`, `stance`, per-claim
`evidence`, `supersedes`, `contradicts`; `status` may only be PROPOSED) and are graded by the
system inside the accept transaction from the verification that actually ran: VERIFIED only
when a cited `pytest:` target was run and passed by `code_test` (a whole-tree run covers no
claim), SUPPORTED with trusted evidence, otherwise unsupported (untrusted external sources
count for nothing).  Only VERIFIED claims are projected into a separately stored Verified
Knowledge table with full provenance (source Task/Attempt/Result/Agent, evidence, verifier,
dependencies, used_by, supersedes/superseded_by, disputed_by/confirmed_by/resolves).
`used_knowledge` is a checked reference (VERIFIED, same Mission, not SUPERSEDED — re-checked
inside the Commit) that records the reuse chain (`KnowledgeUsed`); supersession is explicit
and legal only VERIFIED→SUPERSEDED.  Contradictions (explicit `contradicts` or same key with
opposite stance) are detected before grading: contested claims are capped at DISPUTED and
never projected, VERIFIED knowledge is only marked `disputed_by`, and a system-defined
Conflict Task is opened as a trailing leaf (funded from an explicit `conflict_reserve_tokens`
or deferred with `ConflictOpenDeferred`) whose Arbiter must resolve by an external check
reviewed by an independent Critic — never by a count.  The fixed synthesis Task
(`MissionSpec.synthesis`) depends on every Planner leaf, is budgeted in the §18.2 sum, is gated
by open conflicts (`SynthesisGated`, no rewiring) and must pass verification again.  Retrieval
is deterministic (relevance / trust / DAG distance / recency / reuse, deduplicated, superseded
marked, Mission-bounded; `retrieval-v1`), the Context Builder (`context-builder-v3`) carries
all eleven §10 items under worker / verifier / critic / arbiter / synthesizer visibility
templates, summaries are deterministic compressions that never change a claim's status, a
failed retrieval degrades or blocks explicitly (`RetrievalUnavailable`, stop reason
`retrieval_unavailable`), external content under `untrusted_sources` is marked data at the tool
gateway, and the final report carries the result's lineage.  Orchestrator schema v2 (in-place
upgrade with backup; artifact versions unique per (mission, path) — step-3 L3-2).  CLI
`demo --scenario knowledge-sharing`; kill switch `knowledge_sharing`.  No SDK
(`simple_harness`) API change.

## 0.9.1 — agent_orchestrator step 3: Planner-decomposed static DAG executed in parallel (source candidate)

`agent_orchestrator` 0.3.0 (same wheel).  Step 3 of ORCH-BUILD-v1.0: the Planner proposes a
whole Task graph (`<task_graph_proposal>`, budgets normalised then checked as a whole —
cycles, missing/self dependencies, duplicates, sum of task budgets within the Mission,
tools, shape, independent siblings declaring the same output path) and the Commit Service
applies it atomically with a replayable receipt (roots READY, the rest BLOCKED; a rejected
graph writes only `TaskGraphRejected` and is fed back to the next Planner proposal).  The
control loop runs the Frontier through a bounded Allocator (`max_concurrency`,
`candidates_per_task`): parallel Attempts on independent Tasks, a downstream Attempt seeded
with every ancestor's accepted artifacts (frozen as inputs in the dispatch intent, protected
against rewrite unless the Task declared the path in `outputs`; independent branches that
disagree on a path are an `artifact_conflict`, never a silent pick), artifact versions per
(mission, path) lineage.  Accepting a result, superseding the losing candidates (their
results kept as history) and unblocking the dependents happen in one transaction; a stop
cascades to READY/ACTIVE/VERIFYING Tasks while BLOCKED Tasks end with the Mission (§25.1 has
no BLOCKED→CANCELLED edge); Mission-pool exhaustion blames no Task.  Two orchestrator
instances share the libraries (one `owner_scope`, one `owner_id` per instance, orchestration
lease ≥ 2× the SDK Run lease): a lapsed lease is taken over on the same Attempt and the
same SDK turn, a vanished executor is LOST and retried, `recover()` heals the frontier and
orphan candidates.  The Mission is judged on the integrated tree of every Task's accepted
artifacts (pytest / file / independent Critic).  CLI `demo --scenario static-dag`.  No SDK
(`simple_harness`) API change.

## 0.9.0 — agent_orchestrator step 2: the reliable single-Task Mission closure (source candidate)

New package `agent_orchestrator` shipped in the same wheel (modular monolith after the
design's §27; `simple_harness` root `__all__` unchanged).  Step 2 of ORCH-BUILD-v1.0:
Mission → Planner (one BaseAgent proposing exactly one Task Contract) → Commit →
Reserve → Attempt → Worker BaseAgent in an isolated workspace with four confined tools
(`workspace_read_file/write_file/list`, `run_tests` as a killable child pytest) →
Result Envelope (§13/§26.4, strictly parsed, identity-checked, system-derived id) →
Verifier Router (§14.1 order: format → rule → independent Critic BaseAgent → real code
test; NOT_REQUIRED never counts as PASS) → repair Attempts with feedback or a visible
stop (`max_attempts_reached` / `budget_exhausted`) → Task COMPLETED → independent
Mission-level judgment of `success_criteria` (runs its own Critic when a criterion
needs one) → MissionCompleted / MissionFailed.  Orchestration state lives in its own
`orchestrator.db` written only by the Commit Service (§15/§17.5): idempotent Mission
creation, receipts for proposals, CAS on every entity version, idempotent Events
(§16.2 names), dispatch intents freezing the full AgentConfig + input Message so a
crash at any of six cross-database instants replays the very same Agent/Turn
(S2-04/05), leases/heartbeats from real executor liveness (an UNKNOWN provider outcome
keeps the Attempt blocked and its reservation held, S2-08), two-layer budgets
(allocation Reserve/Settle vs. SDK invocation facts imported once each; `unpriced` is
never written as zero).  CLI `python -m agent_orchestrator` (mission / attempt /
artifact / demo with an evidence directory).  SDK: one read-only facade
`SqliteExecutionUnitOfWork.list_provider_invocations(run_id)`.

## 0.8.0 — BaseAgent: durable Agents, bounded Context, session recall (source candidate)

New public surface under `simple_harness.agents` (`build_agent_runtime`, `AgentRuntime`,
`BaseAgent`, `AgentConfig`, contracts) built on the existing Run kernel: a BaseAgent
Run never reaches a terminal state; every input is one durable AgentTurn with a
staged-then-committed result; `agent_delegate` creates one child Agent per turn;
`close` / `cancel_turn` are control-plane intents, never kernel cancels; idempotent
`create_many` batches; per-turn limits from durable baselines; a Journal-backed bounded
working Context (`ContextPolicy`, `TokenizerPort`) with structural summaries and
exact read-back; Agent-scoped hybrid recall (FTS5 trigram + words, injected
`EmbeddingPort`, RRF) with explicit degradations; `session_history_search/read`.
Execution schema v10 (fresh descriptor; v7/v8/v9 descriptors frozen) with the explicit
backup-first `migrate_execution_to_v10` (after `migrate_execution_to_v9`). Root
`__all__` adds only `migrate_execution_to_v10` and `ExecutionBaseAgentUpgradeReceiptV1`;
legacy Runs, `react_loop.py` policy semantics and the H079/H0710 artifacts are unchanged
(the ReAct loop gained an optional companion write in its final checkpoint CAS).
A driver exception inside an admitted AgentTurn is a visible failed turn
(`base_agent_driver_exception`); the upgrade receipt binds the retained backup to the
source image (`source_root_hash`); `agent_delegate` hands its tool permit back while it
waits for the child so `max_concurrent_tool_calls` cannot deadlock a delegation.
`migrate_execution_to_v9` now upgrades a v7/v8 library written before the explicit
audit schema existed (it bootstraps the audit objects under the write lock, after the
backup is retained) and is a no-op on a library already at v10, so a Host may call the
v9 and v10 upgraders unconditionally at startup.

## 0.7.10 — bounded nullable Tool schemas (source candidate)

Tool schema validation accepts exactly one existing non-null type paired with
null, in either order. Required, enum/const, resource bounds and non-null branch
constraints are preserved; the root remains a single object. No general unions,
combinators, input normalization or execution schema changes. Host owns the
unused-value interpretation. H079 artifacts remain unchanged; main owns the
single successor build and installed validation.

## 0.7.8 — concrete start-mode driver selection (candidate)

Add SDK-owned StartModeDriverRouter: select the actual ordinary or Host-control
driver from the validated durable start mode before invocation and recording.
Only the exact selected SDK implementation receives its existing recording
contract; custom routers/subclasses remain unverified. Host-control authority
validation is unchanged. Execution schema9 and all H077 artifacts are unchanged.

## 0.7.7 — authorization terminal proof and bounded expiry recovery

Root React tool-authorization expiry/denial now commits exact terminal evidence.
Explicit public eligibility/recovery supports proved legacy first-expiry roots;
exact public terminal metadata replaces Host SQL. Original decisions and receipt
bytes are preserved; unknown legacy states are rejected. Execution schema9 is
unchanged. H075/H076 artifacts remain frozen.

<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Changelog

## 0.7.6 — 2026-09-06 (isolated candidate)

- Permit TIME_DUE from actual RESCHEDULED to TRIGGERED; retain exact authority/ref and time checks. No event/recurring or wire/schema changes.

## 0.7.4 — 2026-09-06 (isolated candidate)

- Bind real Memory use receipts to immutable Provider request/time, atomic claim
  and consumed handoff; exact replay and confirmed-not-started fresh grants.
- Add typed Context sidecars, payload-free use views, admission scope pins and
  terminal/recovery checks without synthesizing task/no-recall route receipts.
- Explicit backup-first execution schema7→8 migration; old binaries refuse8.
  Host migration/wiring is separate; no user data is implicitly upgraded.

## 0.7.3 — local successor candidate

### Added
- SDK-owned operation-audit producers and metadata-only public Run, command and pre-Run
  stage snapshots with stable page continuation. Actual Provider/tool/effect, pre-effect
  rejection, core runtime, command, delivery and Memory-port boundaries retain unknown
  outcomes and exact canonical associations. Existing child/workflow receipts are reused.
- Explicit observational audit schema2, separate from execution7; exact audit1 is extended
  atomically on write-open and read-only never migrates. Legacy writer gaps remain visible.
- Exact ordinary/root terminal evidence exposes separate payload and row hashes; pages
  revalidate actual terminal state/unique source through indexed reads. Host must retain
  exact Run/event/state/payload comparison, including SDK-owned terminal outbox metadata.

### Limits
- Current-source completeness is not all-operations recording or consumer audit completion.
  Legacy absent witnesses and unleased calls without state changes are unreconstructable;
  mutable canonical heads are not invented immutable all-transition histories. Host,
  Memory and Service operation producers/optimization findings are separate domains.
- Frozen0.7.2 and original failed candidates are unchanged. No public release/push/tag is
  implied. Independent installed public consumer and actual Host terminal composition are
  distinct gates; see plans/2026-09-05-run-operation-audit/ for evidence and scope.

## 0.7.2 — candidate

### Fixed
- Resume after a successful Context route change now checks the immutable initial checkpoint
  against the start route, while preserving the current route and reserved work. Previously a
  later authorization resume could fail because the current route differed from the initial one.
- Recovery validates checkpoint payload hashes and Run identity for both initial and current
  records. Missing or conflicting initial anchors remain rejected.

### Compatibility
- StartSnapshot v7 and checkpoint v6 wire formats are unchanged; legacy no-initial checkpoints
  remain readable. `ReactCheckpointPort` implementations must provide
  `read_initial_react_checkpoint(run_id)` returning the immutable version-zero checkpoint.
  The SQLite implementation reads existing append-only records without migration.
- This is a local candidate under the approved S5b route-recovery exception, not a release.

## 0.7.1 — candidate

### Added
- Adds `ContextRouteReceipt` v3 with origin-specific `context_tool` and `host_initial`
  provenance. Host-initial routes require exact TaskScope/binding receipts and Host authority while
  forbidding fabricated tool call/effect identities.
- Adds ordinary `StartSnapshot` schema v7 and ReAct checkpoint schema v6 so an initial Host route
  is frozen before the first Provider turn and recovered with conflict detection.

### Compatibility
- Start snapshot schemas 1–6 still decode without an initial route; Host-control snapshots remain
  schema v6. Context route receipt v1/v2 decoding is unchanged.

## 0.7.0 — candidate

### Breaking
- Replaces unconditional pre-Provider Memory recall with an explicit, same-Run Context route
  barrier. Host-issued Context snapshots become the sole Provider request authority.
- Adds versioned Human Memory, TaskScope, evidence, disclosure, route receipt, and
  per-effect Task execution authority contracts.
- Adds strict typed workspace-root identity, Manual authorization challenge/decision, Host-issued
  Auto mode snapshot, Host-verified grant, and append-only binding-set receipt contracts.
- Main-model analysis executors now return a strict result envelope with a separately verifiable
  Host-durable delivery receipt; Memory validation/application receipts retain their distinct role.
- Replaces free-form cognitive mutation/recall drafts with schema-v2 typed Episode, Semantic,
  Procedure, and Prospective payloads, exact Evidence spans, revisioned targets, canonical DAGs,
  and Host-bound Recall selectors.
- Upgrades cognitive mutation to strict schema v5 with explicit Semantic `claim | relation` payloads;
  V1 exposes only `applies_to`, exact existing/same-plan-created endpoints and a closed type matrix.
- Replaces Recall Decision schema v2 with the strict v3 wire. `RECALL` contains selected memories
  only; `NEEDS_USER_CONFIRMATION` contains typed conflict-group candidates only. There is no legacy
  Recall Decision decoder during the prototype phase.
- Moves `PrivacyClass` and `InformationAttribute` to one dependency-free classification protocol,
  while preserving the existing Memory imports as re-exports. Evidence item authority is now a
  Host-only schema-v3 record, versioned by public `EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION`, with mandatory privacy, information attributes, and classification
  authority; no v2 decoder or optional classification default exists.

### Safety
- Route-required effects cannot cross the route barrier in the same Provider tool batch.
- Durable Provider and effect replay bind exact Context, capability, root, and binding-set
  revisions while excluding raw hidden reasoning and transport credentials.
- Context snapshot revisions and snapshot ID/payload bindings are monotonic across Provider turns.
  Public response recovery rejects extra fields, private metadata, unsupported content blocks,
  and non-integral schema values before replay.
- Generic Tool authorization receipts and model metadata are not workspace authority. Project
  route receipts and per-effect envelopes bind the exact binding-set receipt identity/hash in
  addition to its frozen revision and root identity.
- Binding-set receipts commit the sorted unique root identity set: genesis uses the canonical
  empty-set parent digest and every later receipt verifies exact parent-set union one new grant
  root. Context route schema v2 carries this authority; legacy v1 decoding is standalone-only.
- Self-consistent analysis delivery DTOs are not provider-result authority: consumers must verify
  the exact Host durable delivery record, including issuer, request/result, attempt, and response.
- Typed observations cannot replay across admitted evidence; conversation Tool causal parents must
  precede the current item in the authenticated causal group.
- Span verification requires the exact Host `AdmittedEvidenceAuthority` type, validates the v3 item
  authority hash/bindings, resolves admitted evidence once, and returns that verified item authority
  so downstream classification joins reuse the same proof.
- Typed observations accept only Tool/Trusted Tool or External/External Source provenance and always
  require the exact resolved receipt. Mutation DTO validation binds epistemic labels to evidence:
  in particular, `VERIFIED_EXTERNAL` requires an external typed observation and source-verified
  state, while user/model/Context evidence cannot self-grant external verification.
- Recall rejects expired Context, selector removal, evidence-lineage drift, and unknown, external,
  or untrusted disclosure. Conflicting candidates pass the same disclosure gate, require at least
  two unique candidates per group, and cannot overlap selected results. Outcome-specific reason
  codes prevent rejected/no-recall decisions from impersonating positive dependencies; both deny
  outcomes expose a zero candidate count. The unreferenced private v1 Recall Decision decoder is
  removed. Mutation plans require authority-resolved `strict_atomic` apply receipts covering every
  canonical operation and one base-to-committed revision transition. Semantic relation plans reject
  missing/forward dependencies, non-CREATE producers, relation endpoints, illegal types, self loops
  and malformed wire before they can create partial Memory state.

### Compatibility
- Terminal committed-turn outbox delivery remains available for the staged Memory SDK cutover;
  automatic pre-Provider recall and recall-release calls are no longer part of the production loop.

## 0.6.4 — 2026-08-29

### Fixed
- Reissuing a user-confirmation nonce now deeply thaws already-frozen authorization metadata
  before constructing the replacement request. Nested lists and objects therefore survive the
  durable confirmation boundary instead of failing strict JSON validation.

### Compatibility
- Public APIs and execution schema v6 are unchanged. This is a patch-only contract repair.

## 0.6.2 — 2026-08-25

### Fixed
- Runtime capability search now adds a conservative six-character prefix for long lowercase
  English words, so common morphology differences such as `translation` versus `translate` do
  not hide an otherwise exact Skill or Tool match. Short words, identifiers, and non-Latin text
  retain exact matching.
- Tool handler failures now emit a privacy-safe SDK diagnostic containing only the Tool name,
  exception type, and an allowlisted stable code. Exception messages, response bodies, paths,
  and credential-bearing values remain outside both logs and model-facing results.

## 0.6.1 — 2026-08-25

### Fixed
- `build_react_driver` now accepts a Run-local Tool exposure resolver and passes the resolved port
  into every `ReActRunInput`. This closes the public composition seam required by a Host to use
  same-Run progressive exposure; the rejected 0.6.0 prepublish artifact was never released.

## 0.6.0 — 2026-08-25

### Added
- Provider-neutral Runtime capability records for executable Tools, Skill resources, and Workflow
  profiles, with bounded search/describe, nonce-bound activation receipts, and executable-only
  Provider projection.
- Run-local Tool exposure checkpoints. Each new ReAct `ready` attempt reprojects direct plus
  activated Tools, while a `provider_reserved` request replays its exact frozen schema snapshot.
- Fresh execution schema v6 and an explicit backup-first v5-to-v6 migrator. The legacy
  ProviderToolSpec fingerprint remains separate from the complete v6 catalog envelope digest.

### Safety and compatibility
- Catalog visibility never invokes a handler or grants authorization. Target execution still goes
  through ToolRegistry, authorization, EffectExecutor, and the durable effect ledger.
- Terminal activation receipts deterministically restore visibility after restart; forged,
  cross-Run, stale, out-of-order, missing, changed, or extra handler identities fail closed.
- Normal loading still rejects legacy schemas. Operators must close the Runtime and explicitly run
  `migrate_execution_v5_to_v6` with a caller-selected adjacent backup path.

## 0.5.2 — 2026-08-24

### Fixed
- Durable start commands now deeply thaw their frozen JSON input before constructing `RunStart`.
  Nested capability snapshots and message metadata therefore retain their canonical JSON shape
  instead of being rejected after Memory Context staging.

### Compatibility
- Public APIs, command schema version, and fresh execution schema v5 are unchanged. There is no
  migration or legacy-data compatibility path.
- Released from source commit `136b3539b938516156a9b336f38c6b4404d3adb8`; the frozen wheel
  SHA-256 is `09ba6041a0220cdd952f50e6ec8defcf1d8bbe60d9a044d9d7fc9106985639de`.

## 0.5.1 — 2026-08-24

### Fixed
- Durable start commands now carry a closed Tool catalog fingerprint through the public intent,
  execution database, `RunStart`, and `StartSnapshot`; restart resolves the exact catalog snapshot
  and fails closed before Driver/Provider handoff when that authority is unavailable or drifted.

### Compatibility
- Fresh execution schema v5 and the public command schema version remain unchanged. There is no
  migration or legacy-data compatibility path.
- Released from source commit `441709e518aae041829e64796cabe13ab265abbf`; the frozen wheel
  SHA-256 is `1298aa7d44f748ebf39265920d648f40c130bf014452dce3c6ae049aa5f6590c`.

## 0.5.0 — 2026-08-24

### Added
- A closed durable command API: `submit_start`, `submit_continue`, `submit_cancel`, and
  `get_command`, with typed receipts, snapshots, and stable errors.
- Fresh execution schema v5, which commits command acceptance before Memory preparation,
  Provider, Tool, or delivery work begins.
- Durable FIFO acceptance sequencing, replay/conflict detection, cancel fencing, lease recovery,
  and terminal command-output projection for restart-safe product integration.
- Closed command output convergence: running is `PENDING`, valid completed output is `PRESENT`,
  failed/cancelled is `ABSENT`, and missing, corrupt, or conflicting completed output is
  fail-closed `UNKNOWN`.

### Fixed
- An expired command owner can no longer kill the sole command pump by attempting to settle a
  claim already converged by its takeover owner; each command failure is isolated.
- Legacy starts perform readiness and complete typed preflight validation before their permanent
  run-mode reservation, while still reserving before any external call.
- Root and continuation output writes now expose explicit before/after fault cuts inside the same
  terminal transaction.
- A completed command can project only the `conversation_outputs` row that names that exact
  command; earlier commands in the same Run remain `ABSENT` after a later continuation owns the
  final output.

### Compatibility
- Agent Memory v1, observability v1, and the legacy RunClient surface remain compatible.
- Normal open of schema v4 fails closed without writing; 0.5.0 creates fresh schema v5 only and
  provides no v4-to-v5 migration.
- Released from source commit `ac2e2add7e6f5efb5d4dd7b26fb138f9d750d334` after the Memory
  SDK 0.5.1 exact-wheel compatibility matrix passed (receipt commit `0b25ac54`).

## 0.3.0 — candidate

### Breaking
- Execution persistence now uses fresh schema v4. The normal loader rejects schema v1-v3;
  schema v3 requires the explicit backup-first offline migrator and a complete identity map.
- The public query/sink and reserved query/write Memory ports are retired. Consumers pass one
  `AgentMemoryPort` implementation, such as Memory SDK 0.4 `MemoryManager`, to the official
  consumer or production builder; no public Memory adapter or manual recall/write lifecycle
  remains.

### Added
- Official `AgentMemoryPort` v1 contract with trusted four-part identity, personal/family
  scopes, canonical recall/release/committed-turn DTOs, stable errors, failure policy, and
  explicit borrowed/runtime ownership.
- `ConversationContextProviderPort` for product-owned non-Memory Context and automatic
  `RunClient.start_conversation()` / continuation preparation.
- `ConversationContinuationInput.context_source_snapshot_ref` lets each continuation bind its
  own product Context snapshot. When omitted, the SDK derives a deterministic content-addressed
  reference from that continuation's current message.
- Fresh execution schema v4 with immutable Agent identity bindings, richer Context staging,
  durable recall-release retry, and a terminal-only canonical committed-turn outbox.
- Lease/epoch-fenced committed-turn dispatch with restart replay, bounded backlog cleanup,
  transient retry, permanent/conflict dead-letter, and privacy-safe `REJECTED_ERASED` settlement.
- Backup-first explicit execution v3-to-v4 offline migration with a digest-verified neutral
  manifest, deterministic four-way legacy event classification, target identity remapping,
  and a versioned cursor for post-migration continuation supersession.
- A packaged PEP 561 `py.typed` marker so strict type checking follows the public Agent Memory
  and execution migration contracts from an installed wheel.

### Changed
- `build_consumer_runtime` is the official easy composition root and accepts one Memory
  instance; recall failures degrade to a frozen empty partition and replay does not recall
  again.
- Old query/sink and reserved query/write ports are retired from public exports. Consumers
  migrate to one `AgentMemoryPort`; manual preparation helpers, adapter-facing Memory DTOs and
  `ContextPreparationMode` are private implementation details rather than compatibility exports.
- Conversation start and continuation enqueue no longer create tentative Memory writes. A
  completed root or continuation commits its user+assistant pair atomically with terminal facts;
  failed/cancelled turns produce no outbox row and replay rejects missing, added, or changed turns.
- Context preparation persists the effective root or continuation snapshot reference in the
  durable claim before invoking the product provider. Continuation replay reuses that exact
  reference; changing either the reference or payload for an existing continuation ID conflicts.
- A duplicate caller now waits through the bounded Context request/lease horizon. If the durable
  owner disappears, the waiter deterministically takes over the expired lease and freezes the same
  stage instead of failing after a fixed one-second polling window.
- Existing execution schema v1-v3 databases remain fail-closed in the normal loader. A closed,
  exact-v3 database can be upgraded only through the explicit offline migrator with a complete
  legacy identity map and a caller-selected same-directory backup path.

## 0.2.0 — candidate

**Focus:** Durable conversation Memory integration without replacing the 0.1.5
structured-message, tool-catalog, Provider-budget, or projection authorities.

### Added
- Typed conversation turn/continuation/output DTOs and bounded recall/apply Ports.
- Fresh execution schema v3 with immutable user/session ownership, durable private context
  staging, and a transactionally coupled conversation Memory outbox.
- Four atomic root/continuation commands, lease-based Memory dispatcher recovery, and
  SDK- or consumer-prepared context modes whose private snapshots replay byte-for-byte.
- Strict production composition that requires all authorities and owns projection, Memory,
  and SQLite lifecycle resources.

### Changed
- StartSnapshot schema is v5; schemas v1–v4 remain readable. New conversation fields are
  additive and generic runs remain supported when conversation Memory is disabled.
- Existing execution schema v1/v2 files now fail closed and require a fresh v3 storage set.
- CI builds one authoritative candidate and tests the exact wheel on Python 3.11–3.13.
  Release publication is manual and uploads the tested bytes without rebuilding.

## 0.1.5 — candidate

**Focus:** Durable per-Run context authority for SDK-first product integration.

### Added
- Typed structured message content (`ContentBlock` / `MessageContent`) with canonical
  persistence, StartSnapshot/ReAct recovery, and OpenAI-compatible serialization. Structured
  content is never coerced through `str(list)`.
- Nullable Provider usage dimensions for cached and reasoning tokens.
- Atomic per-Run Provider resolution via `ProviderBindingResolver`, binding the physical
  Provider, optional frozen estimator, budget policy, and restart-checkable fingerprint.
- Immutable, content-addressed tool-catalog generations persisted in SQLite and resolved by
  exact generation/fingerprint across WAITING and process restart.
- A transactionally coupled Provider settlement projection outbox with stable cursor reads.
- Optional `deskpet_public_progress` normalization; missing, blank, or wrong-type metadata is
  stripped without blocking business tool arguments.

### Changed
- SQLite schema version is 2. Existing 0.1.4 databases migrate in place.
- StartSnapshot schema version is 4 and remains backward-readable for schema versions 1–3.

### Integration boundary
- Product code remains responsible for enforcing the 8 MiB per-content-block and 16 MiB
  aggregate-per-Run ingress limits before constructing SDK messages.
- Catalog generations are retained indefinitely in 0.1.5; a future GC may delete them only
  after every referencing Run is terminal.

## 0.1.4 — candidate

**Focus:** Release-blocking hardening of the consumer facade and the release/CI pipeline.

### Fixed
- Delivery no longer fabricates `DELIVERED`: the no-op `_DefaultDeliverySink` was removed
  from the production namespace. `build_consumer_runtime` now accepts an optional
  `delivery_sinks` mapping; when omitted, no sink is registered and deliveries stay PENDING
  (fail-closed). `DeliveryDispatcher` now permits an empty sink set. A test-only
  `NoopDeliverySink` lives in `simple_harness.testing`.
- Tool calls now pass a real execution context (`run_id` / `request_id` / `call_id`) to
  `ToolExecutorPort.execute` instead of an empty dict.
- `build_consumer_runtime` is documented as a demo/basic facade; production consumers
  should assemble `RuntimePorts` directly (the facade uses a zero-cost price estimator and
  no-op reconciliation).
- The Database opened by `build_consumer_runtime` is now closed on Runtime shutdown via a
  `close_hook` (registered by the facade, not by the generic `Runtime.close()`), so a
  consumer-built runtime no longer leaks its SQLite connection.
- Added a driver-failure terminalization regression test: a raising driver durable-
  terminalizes the run to FAILED, the failure log carries `run_id` via `extra` (no
  secondary logging `TypeError`), and the public payload never exposes `private_cause`.

### Changed
- `MemoryQueryPort` / `MemoryWritePort` are marked `reserved` (declared but not yet wired
  into the Runtime); consumers must not assume recall or working memory is active.
- Release/CI hygiene: `ci.yml` now runs the full pytest suite plus scoped ruff/mypy;
  `release.yml` gates publish on a same-file `test` job (full pytest + conformance) since
  `needs` cannot reference a separate manual workflow; hardcoded version literals were
  removed from `release-candidate-conformance.yml` and `verify_release_gate.sh` in favour
  of the single `src/simple_harness/version.py` source.

### Backward compatibility
- `build_consumer_runtime`'s new `delivery_sinks` argument is optional; 0.1.3 consumers
  build and run unchanged. `Runtime`/`build_runtime` gain an optional `close_hook` (default
  `None`, no behaviour change).

## 0.1.3 — candidate

### Observability (post-release, 2026-08-19)

- Added structured stdlib `logging` events on the SDK's core execution paths so hosts
  can observe the engine: `run.start` / `run.complete` / `run.fail` / `run.cancelled` /
  `run.admission_denied` (kernel), `provider.invoked` / `provider.usage_untrusted` /
  `provider.charge_unknown` / `reconcile.unknown_settled` (dispatch), `tool.invoked` /
  `tool.authorized` / `tool.denied` / `tool.effect_settled` (executor), and
  `budget.refused_on_unknown` / `budget.exceeded` (budget).
- Events follow a `<module>.<action>` name with structured `extra` fields; tool
  arguments are logged as keys only and never as values.
- Added a regression suite (`tests/unit/runtime/test_logging_observability.py`) that
  locks the observability contract via caplog behaviour tests, AST existence checks,
  and redaction assertions.

**Focus:** Fix two consumer-layer design defects.

### Fixed
- `ConsumerRuntimePorts` now accepts `model` (default `"consumer-model"`); the consumer
  provider adapter uses it as `ProviderTarget.model` instead of the hardcoded constant.
  This lets real consumers whose `ProviderPort` echoes a real model name have their usage
  trusted, instead of always landing in `BudgetCharge.unknown()` and refusing multi-turn runs.
- `ConsumerRuntimePorts` now accepts `tool_schemas` (a name → closed input schema mapping);
  tools with a declared schema accept their arguments. Tools without a schema keep the
  fail-closed no-argument default (the SDK JSON-Schema subset forbids `additionalProperties`).

### Backward compatibility
- New fields are appended after existing fields and carry defaults, so 0.1.2 consumers build
  and run unchanged.

### Semantics note
- When usage is trusted, it is recorded as `TRUSTED_USAGE` at the consumer price estimator,
  which is currently a frozen zero-price estimator — trusted usage therefore books at zero
  cost. This is intentional for the consumer facade and not a pricing path.

## 0.1.2 — candidate

**Focus:** Ease of integration for external projects.

### Post-release fixes (2026-08-19, docs/examples only — no SDK code changes)

- Fixed `examples/minimal-consumer/`: the demo previously printed
  `Run completed: None` (it printed `wait_idle()`'s `None` return), always
  exited 0, and could not be re-run (hardcoded IDs + persistent
  `execution.db`). The demo now reads the real terminal state via
  `client.query(run_id)`, exits 0 only on `COMPLETED`, and uses fresh
  run/session IDs plus a temporary database per invocation.
- Fixed the example's mock provider to the real 0.1.2 provider contract
  (`Message`/`CallId`/`ProviderUsage(input/output/total_tokens)`), and
  documented three integration gotchas discovered while repairing it:
  1. `RunStart.input` must set `max_output_tokens`, otherwise the provider
     reservation is unpriceable and the run fails with `react_cost_exceeded`.
  2. The consumer adapter pins `ProviderTarget(model="consumer-model")`;
     a provider response whose `model` does not match gets its usage recorded
     as an unknown charge, also tripping `react_cost_exceeded`.
  3. The consumer adapter registers placeholder tool specs
     (`additionalProperties: false`, no properties), so tool calls with
     non-empty argument mappings fail schema validation; consumer-level tools
     effectively cannot take arguments in 0.1.2 (use the 10-Port
     `RuntimePorts` API for real schemas).
- Rewrote `docs/quickstart.md` for the real 0.1.2 API: installation section
  now states the only acquisition path (clone repository + `uv build` +
  `pip install dist/simple_harness_sdk-0.1.2-py3-none-any.whl`), and exactly
  one self-contained runnable ```python block (all other snippets marked
  `python fragment`).
- Promoted `build_consumer_runtime` as the recommended integration path in
  `docs/integration-guide.md` and `docs/api/ports.md`; the full 10-Port
  `RuntimePorts` API is now labeled advanced usage.
- Added `examples/minimal-consumer/verify_from_zero.sh`: clean-clone gate that
  extracts build/install commands and the runnable example verbatim from
  `docs/quickstart.md`, executes them, and runs the demo twice (structured
  PASS/FAIL, exit-code gated).
- Added `examples/minimal-consumer/conformance_host.py`: a consumer-level host
  for the SDK conformance protocol covering the `provider` and `tool` suites
  (exercises the provider/tool contracts directly, so it is unaffected by the
  two 0.1.2 consumer-adapter limitations above).
- Added `scripts/verify_release_gate.sh`: one-shot release gate that installs
  the `dist/` 0.1.2 wheel into a clean venv, runs `minimal-consumer`, and runs
  `python -m simple_harness.testing --suite provider,tool` against the
  conformance host (structured PASS/FAIL, exit-code gated).
- Recorded 0.1.2 provenance in `dist/BUILD_INFO.txt` and `dist/SHA256SUMS`
  (wheel built locally at commit `cb1f245`, before `896b685`'s observability
  fix; wheel SHA-256
  `387c8d1d97c0f89e4664347fb57ca6a43a0e7fa772b07a0f34c6f3a6e86efd4c`).

### Documentation
- Added comprehensive Integration Guide (`docs/integration-guide.md`) with step-by-step Port implementation examples
- Added Quickstart guide (`docs/quickstart.md`) for 10-minute first-run experience
- Added complete API reference documentation:
  - `docs/api/ports.md` — All Port interfaces with implementation examples
  - `docs/api/runtime.md` — Runtime lifecycle and error handling
  - `docs/api/workflow.md` — Official workflows and host services
- Added minimal consumer example (`examples/minimal-consumer/`) with working code
- Updated AI Phone handoff document with v0.1.1 changes and Memory integration guide

### API Surface
- Added `MemoryQueryPort` and `MemoryWritePort` interfaces for future Memory SDK integration
- Exported Memory ports from `simple_harness.runtime` module

### Developer Experience
- Created runnable minimal consumer example demonstrating:
  - Mock LLM provider implementation
  - Tool executor with calculator and echo tools
  - Authorization port integration
  - SQLite context persistence
  - Complete runtime setup and execution flow

### Internal Cleanup
- Removed unused `ModelPersonalWorkflowMatcher` class from `turn_authority.py`

## 0.1.1 — candidate

- Added typed provider/tool/runtime/workflow consumer operations with SDK-owned
  case verifiers; consumer Hosts provide observations but never verdicts.
- Added one shared CLI and pytest runner with fail-closed protocol/capability and
  required-case handling.
- Added redacted, fixed-schema conformance reports.
- Made `AuthorizationPort.bind_effect_handoff(...)` mandatory before every
  physical Tool handoff; Hosts that only implement the 0.1.0 `authorize(...)`
  seam must add decision and handoff receipt binders.
- Frozen the ReAct policy fingerprint in start snapshot schema v3 and added
  crash-window delivery/lifecycle recovery coverage.
- Added deterministic candidate build attestations and a non-publishing remote
  three-platform workflow contract.

## 0.1.0

- Initial durable runtime and workflow foundation.
