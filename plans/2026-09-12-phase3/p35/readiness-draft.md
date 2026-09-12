# P3.5 readiness draft：真实负载、长任务与恢复

状态：**DRAFT / 必要预算与物理槽位子集已实施、限定控制通过 / P3.5 整体验收未完成**。最后更新：2026-09-13。

**当前增量事实以 §9 为准。** §1–§6 保留最初只读调查的基线、差距和建议，不代表这些代码至今未变；原 8 项 AC 不缩减。用户随后授权为 N1 先实施必要的 P3.5 预算/物理槽位子集，尚未完成 tail 保护、priced 完整预算或全部真实负载验收。本次仅更新本文件，生产/测试保持 freeze；pytest 结果均引用主线程唯一 runner，本文作者没有运行 pytest、模型、安装、构建、UI 或提交。

## 1. 原要求、当前身份与范围

原始事实源：[Host Phase3 plan](../../../../simple_harness/plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md) §7、§11 P3.5 八 AC；前序设计见 [P3.4 readiness](../p34/readiness-draft.md)。用户当前授权是 P3.1–P3.5，**暂停打包，不含 P3.6**。源码 UI 可以承载本轮功能验收；不能因此改写历史 frozen 失败或宣称安装包通过。

初稿调查时的历史 `git rev-parse`（不是当前快照）：Host `d0c1ee4cb2e75603cc7c451e8041868da180c6b6`；SDK `a5c8fca659be8b491d4d0f3f3f5536a5e711ce48`。SDK 有主线程正在修的 P3.3 prompt/accept-Critic gate，HEAD 不能代表实际源码字节，当前不标 clean，也不据此锁定 P3.5 基线。

主线程报告、非本次重跑：Host 最终 71 passed / 6.55s、execution manifest current、同 Python 3.12 lint 对照无新增；独立 editable 冷进程经 `verify_runtime_identity`、加载 Orchestrator / simple_harness.runtime / ProductSdkRuntimeStack 后复验，198 个 SDK 模块均在 source，Service 真验通过；`source-sdk-preflight-v1.json` 记录 `editable-source / source_verified=true / installed_wheel_verified=false / 307 inputs`。这仅关闭对应来源接缝，不替代 P3.3 UI、P3.5 容量或恢复证据。正式 UI 前须待 SDK 冻结，重新 attestation 并冷重启。

以下源码路径均相对 SDK 根；`Host:` 表示相邻 simple_harness 仓库。原始证据只写 ignored `.local-test-evidence/<date>/p35/<run>/`；Git 仅留结论、命令、索引和 hash，不含密钥、原始长日志、DB 或截图。

## 2. 原八 AC 与初稿代码差距（当前子集进展见 §9）

原 AC 必须观察到的结果逐句保留，不以新方案替换原验收含义。

| AC / 原要求 | 当前已核对基础 | 缺口与最小决定性 oracle |
|---|---|---|
| **P3.5-A01 物理槽位不抬高**：实际并发不超过2；逻辑并发不改硬上限 | `runtime/assembly.py::OrchestratorConfig.__post_init__` 当前仍将 model cap 提高到 `max_concurrency * candidates_per_task`。`assemble_orchestrator_runtime` 每 profile 各建 Runtime，并向每个传相同 model cap；`simple_harness/agents/wire.py::AgentProviderWire` 各有本地 semaphore/in_flight | 这是现存行为差距。保留显式硬上限，引入同部署共享准入；全局和 profile 上限同时满足。用至少两 profile、三 Mission、逻辑候选数大于 2 的真实 invoke 时间区间证明**总** peak≤2；不能把每池 peak≤2 相加当通过。源码 UI 与物理请求观测关联，非法 cap 配置明确拒绝 |
| **P3.5-A02 排队不算失联**：不把排队Attempt标LOST，不增加重复执行；总Mission期限仍有效 | `event_handler._observe_liveness` 仅对 RUNNING 且未 blocked 的 Attempt 检查 stall，按 SDK liveness 续租；仅 turn 缺失走 LOST。`AgentBridge.liveness` 读取 `BaseAgent.turn_snapshot` 的 durable blocker，而 wire semaphore 没有独立资源等待身份 | 不能删掉 cap 自动抬高后便宣称修完。新增 slot wait 的原因/开始/准入/取消身份，接入实际 blocker 与 lease。让等待超过 stall/lease 观察窗口但尚未到 Mission deadline：同 agent/turn/attempt，不产生重复 Worker；另一例真的到总期限必须自然终止。等槽、服务不可用、human、UNKNOWN 分开显示 |
| **P3.5-A03 验证背压闭环**：队列有界、Worker减速，冲突/验证保留额度并可排空 | `scheduling/backpressure.py` 有六限制、高低水位及滞回；`CommitService.record_backpressure` 持久化状态/事件。`event_handler.step` 先驱动 bounded `_verifying`（`verifier_workers`），再观察压力并轮转 Mission；`scheduling/allocator.py` 在压力下减速，优先 conflict/老化任务并限制探索 | 已有闭环基础，不能写成“只有曲线”。仍须对真实候选完成洪峰证明 pending queue **实际硬边界**，定义在途完成如何计入容量；为最终验证/冲突同时保留费用与物理准入，不能只提高排序。慢 verifier→RAISED→低价值 Worker 减速→冲突/验收继续→低水位 CLEARED→排空；取消等槽任务不泄漏 permit |
| **P3.5-A04 预算不能双花**：单事务预留正确；未知支出不释放；等待槽不是第二次计费 | `governance/budgets.py::BudgetLedger.reserve` 对 subject 幂等并沿 Task→Mission→Global 链检查；依赖 `Store.transaction` 的 BEGIN IMMEDIATE。usage 按引用导入去重，`settle` 拒绝 unknown；event handler 保留未知支出 reservation | 不新建第二套费用账。resource permit 只能管理容量，不能再 reserve/charge。并发抢最后余额只允许可负担的一方；等待/取消/重启保留原 subject、reservation、usage refs；未知成本始终 held。补真实 usage 超预留的可观测事实，不能把预留等同实际账单上界；priced 与 unpriced 分别报告 |
| **P3.5-A05 交叉故障恢复**：恢复身份与receipt一致；已完成任务不重跑；UNKNOWN先核对 | `event_handler.recover` 重绑 intent/workspace、heal Mission、导入未结算 usage，再逐 profile recover；`run` 先 reconcile actions。intent 已有 creation/input/agent/turn/profile 身份。Step02/06/07 已有多种 crash/lost receipt 控制；Host 已有子进程 kill/reopen 用例 | 在新增资源队列和 P3.4 后继候选上复用这套身份，不重做任务状态机。覆盖“Agent创建后/submit后receipt前/SDK结果落库后/验证层提交后/效果发生但回复丢失”的恢复矩阵，含真实冷进程。UNKNOWN 必须查原业务键；已提交 Worker 仅恢复验证。队列 permit 的旧 owner 不能在重启后继续占用或与新 owner 双授予 |
| **P3.5-A06 控制面及时生效**：新非法handoff被拒绝；在途事实可结算；UI明确未完成与未知 | Host `service.cancel`/SDK cancel、审批与终态 action guards 已存在；`projection.ui_state` 已区分 queued/running/verifying/waiting_person/unknown，不缺基础标签。`simple_harness/execution/dispatch.py` 当前先 `hand_off_provider_invocation`，然后进入 wire `invoke` 内部 semaphore | 精确接缝在 **handoff 前** 的容量准入与**等槽后**的取消/授权/版本复核，不能仅在 Provider 外包一层 semaphore。堵满两槽时从真实 UI 取消排队 Mission，释放槽后其旧请求不得外发；已在途真实 usage/结果可记事实，不能接受为被取消 Mission 的新成果。记录点击→处理→确认时间及资源观测；实施前冻结数值阈值，不能事后按本次耗时调门槛 |
| **P3.5-A07 长Context不丢关键要求**：保留任务约束/未决状态；冻结请求恢复不重新检索；历史可回读 | `simple_harness/agents/context/` 与 `tests/agents/test_context_journal.py` 已有窗口选择、协议组保护、原文回读、tokenizer/policy hash、冻结请求恢复控制。P3.4 draft 核实 profile/assembly 尚缺 Context 端口的完整产品接线 | 此项直接依赖 P3.4-A06/A07 的实际接线。真实长回合超过工作窗口，最早约束、未决审批与当前输入仍生效；核对 request hash/selected seqs 与 Journal 原文。UNKNOWN 恢复沿原冻结请求，不重新检索或改变 policy。模型“记得了”一句话不构成证据，必须有实际产物与可回读原文 |
| **P3.5-A08 多库备份恢复**：库身份和产物hash对齐；未能核对的外部动作不自动重发 | `runtime/assembly.py::execution_db_for` 已分 default execution.db 与 execution-<profile>.db；编排 orchestrator.db 独立。`storage/store.py` 与 SDK migration helpers 有单库 SQLite backup，但检索 SDK/Host orchestration 未发现协调多库的 backup/restore 协议 | 需暂停新准入、确认安全切点、用 SQLite/WAL 一致方式导出每库及清单、冻结 Artifact/CAS、schema/config/identity/receipt；不能直接复制正在写的 db 文件。在隔离副本按清单核验并恢复，缺库、错 profile、错 artifact hash 任一拒绝。外部真实动作未核对前不可重发；不能宣称跨库原子事务或外部副作用随本地回滚撤销 |

### 2.1 不应误判为已缺失或已解决的部分

- 排队/运行/待人/UNKNOWN 的 UI 基础词汇已存在；当前不足是物理等待原因、时间和准入事实没有贯通，不能通过修改文字假装修复。
- 背压有实际 allocator 行为及 bounded verifier 集合；尚未证明真实负载下生产/消费容量与在途结果上限，不能把这些既有函数存在当 A03 完成。
- `AgentProviderWire` 的计数可作单池基础；它进入内层调用前的等待目前发生在 ledger handoff 后，外层简单串行 wrapper 不足以证明 A02/A06。
- 单进程中共享对象只证明该进程/部署实例内总上限。若继续支持共享库多 owner/进程，必须共享持久 claim/资源服务并测 fencing；不能保留多进程路径却称本地 semaphore 是全局限制。先明确“部署”是否还包括 Host 主对话/实时路径共用同一配额，已知 Host settings 注释指出聊天共享 Provider quota。
- P3.2 **F-P32-1** 仍需独立列入恢复审计：sandbox `_Run.close()` 删除 marks，既有 journal 记载宿主崩溃后逃逸进程缺可识别线索。涉及 code/tool 故障试验时必须处理或明确阻塞对应验证；不能只看父进程退出就报无残留。

## 3. 最小 P3.4 前序，不扩大依赖

| 前序 | 对 P3.5 的真实依赖 | 准备/实施顺序 |
|---|---|---|
| P3.3 来源/正式结论/Critic 闭环 | 三 Mission 的业务验收不能建立在尚未修好的 N1 接受链上 | 当前 accept-Critic gate 由主处理。P3.5 只读准备可继续，正式模型/UI 运行前必须有新来源快照与可用基线；不重写 P3.3 状态 |
| P3.4-A06/A07：Context 配置、tokenizer/policy、真实请求选择及历史数据边界 | **A07 的直接必需项**，也是长请求容量/恢复身份的输入 | 接通 profile→assembly→request/receipt 并冻结版本；通用 tokenizer 测试不证明 flash tokenizer 适配。这个前序不能跳过 |
| P3.4-A03/A08：后继图/迟到身份、预算不清零、有界停止 | 新资源准入后必须继续遵守相同 Task/intent/预算和截止语义 | 准入/资源 permit 不成为新 Attempt，不重置停滞或预算。压力场景包含至少一次局部修订/迟到结果，复用 P3.4 oracle |
| P3.4-A04/A05：选择/综合/片段复用 | 对通用槽位实现不是先决条件；但若 P3.4 已默认支持，其 selection/synthesis/pending evidence 必须纳入容量与备份清单 | 不为写 resource admission 等待新搜索算法；在最终 P3.5 回归中保留获批模式，不关掉它以让压力测试变轻。新的比较等待不能永久耗尽执行槽 |
| P3.4-A01/A02 角色与局部改图 | 复用真实工作负载与角色标识，有助于按业务阶段观测 | 不另引入 planner/manager 系统；P3.5 限制其速度与恢复，不改变搜索语义 |

最小前序是**冻结现有实际生产契约，完成 Context 接线及身份/截止语义**，不是要求重新开发整个 P3.4。用户已授权把解决 N1 所必需的 P3.5 子集提前实施，不要求机械遵守阶段号；这不替代任何阶段的原 AC 收尾。

## 4. 初稿建议的最小实现切片（实际落地路径见 §9）

1. **资源准入 + 时钟 + handoff 一条链**：按原计划新增 `scheduling/resource_admission.py`，承载部署/profile 双限与 queue identity；复用 runtime Port、dispatch ledger、现有取消/授权门。必须触达 `simple_harness/execution/dispatch.py` 的 handoff 前边界及 Agent blocked 状态，不能只改 Orchestrator。保持已冻结请求/usage 主键；未知版本失败闭合。Verifier/Tool 与模型槽分开，避免拿模型槽等待 human。跨进程支持方式在实施前明确，不能暗中削减。
2. **背压与额度排空**：沿现有六限制、allocator 优先级/老化、BudgetLedger 实现预留容量；先定义 `pending + in-flight potential arrivals` 的上限与计数口径，再做冲突/最终验证专用进度保护。不得把请求等待记成第二次费用。对超预留实际支出显式记录，不改原未知成本守卫。
3. **恢复/备份和 Host 观测**：沿 DispatchIntent/ActionLedger/SDK journal 补资源 claim 恢复与 backup manifest；Host 投影实际 wait reason、queue/admitted timestamps、容量峰值、held reservation 与控制确认。保留原 Missions 页面和审批按钮权威。当前无多库备份 UI/命令接缝，需最小维护入口调用同一正式 helper；不以 DB 手改演示。
4. **真实负载与恢复闭环**：先跑新增决定性 oracle，再对既有 selector 回归，最后真实 Provider + 源码 UI。新测试建议放 `tests/orchestrator/p35/`，上下游 admission/Context 测试归各模块；具体接口由实现前独立审查冻结，本稿不猜 wire DTO。

备份清单至少绑定：backup_id、切点/准入停止状态、SDK source attestation 与 Host 身份、每库角色/profile/路径/schema/完整性及文件 hash、artifact/CAS inventory、有效配置/策略版本、未完成 dispatch/approval/action 及 UNKNOWN 清单。执行库可能含 Journal/索引，须按实际 Runtime 枚举，不能硬编码只有两个 db；可重建索引不成为唯一历史副本。恢复先验证到独立 staging，不覆盖原 userdata；有未知在途服务事实的集合不能标为可无条件恢复执行。

## 5. 可复用的精确测试入口

以下均已只读核对名称，**没有执行**。后续主线程在 SDK 新基线冻结后，用唯一 runner 先跑新增目标，再选这些回归；不在此填推测 PASS 总数。

| 能力 | 现有 selector（相对 `tests/`）与边界 |
|---|---|
| 背压/公平 | `orchestrator/step06/test_backpressure.py::test_s6_02_a_slow_verifier_raises_backpressure_and_work_resumes_after_it_clears`；`::test_under_pressure_only_conflict_and_starving_tasks_expand_plus_one_exploration_slot`；`step06/test_backpressure_state.py::test_raise_at_high_hold_in_between_clear_only_at_low`。不是部署跨池总并发证明 |
| 预算与截止 | `orchestrator/step02/test_store_and_budgets.py::test_budget_chain_reserve_settle_and_unpriced`；`step06/test_governance.py::test_s6_07_runtime_dimension_stops_new_allocation_after_the_mission_clock_runs_out`；`step06/test_step06_review_fixes.py::test_p1_2_parallel_tasks_share_the_tool_call_pool_without_a_false_exhaustion`；`::test_p1_6_a_reservation_held_by_an_unknown_charge_is_listed_and_on_the_timeline` |
| 跨库 dispatch | `orchestrator/step02/test_recovery_matrix.py` 中 `test_s2_04_crash_after_agent_created_replays_the_same_agent_and_turn`、`test_s2_04b_crash_after_submit_before_receipt_replays_without_a_second_turn`、`test_s2_05_crash_after_result_submitted_only_resumes_verification`、`test_s2_05b_crash_after_turn_committed_before_result_recorded`、`test_s2_05c_crash_after_a_verification_layer_passed`、`test_s2_08_unknown_provider_outcome_stays_blocked_with_reservation_held` |
| 多池恢复 | `orchestrator/step06/test_model_router.py::test_s6_08_after_a_restart_an_attempt_stays_with_its_own_pool_and_is_never_taken_by_another_model`；`step06/test_step06_review_fixes.py::test_p1_4_a_submitted_turn_of_an_absent_pool_does_not_hold_run_and_is_resumed_by_its_pool` |
| 实际效果 UNKNOWN | `orchestrator/step07/test_action_execution.py::test_s7_06_a_lost_receipt_is_unknown_holds_the_reservation_and_is_reconciled_not_resent`、`::test_s7_06_a_crash_after_the_service_applied_is_found_by_the_lookup`、`::test_s7_06_cancelling_the_mission_does_not_turn_unknown_into_failed` |
| Context | `agents/test_context_journal.py::test_unknown_resume_reuses_the_frozen_request_without_a_new_selection`、`::test_originals_are_exactly_readable_after_window_rotation`、`::test_required_content_too_large_fails_before_any_provider_call`、`::test_current_input_is_present_even_when_a_turn_outgrows_the_read_window` |
| Host 生命周期 | `Host:backend/tests/orchestration/test_restart_recovery.py::test_killed_while_waiting_on_a_person_continues_without_rerunning`、`::test_killed_inside_a_model_call_is_shown_and_can_be_taken_over`；`test_service_missions.py::test_cancel_stops_dispatch_and_is_idempotent`；`test_review_fixes.py::test_degraded_still_cancels_but_refuses_new_missions`。这些是自动控制，不算 CUA UI |

建议新增负控：全局2/两profile各2仍总≤2；逻辑3×2不改cap；等槽超过stall后不重派；释放槽瞬间与cancel/权限/版本变化竞争；人审不占模型/Verifier permit；完成洪峰不越pending边界；unknown支出跨permit重领仍不释放；同一 usage 重放只结一次；backup缺库/错profile/错CAS逐项拒绝；恢复后外部已应用但本地未见receipt禁止重复执行。

## 6. 不依赖 packaging 的真实负载 UI 闭环

### 6.1 载体和可执行前提

- 使用主已经准备的 `Host:.local-test-evidence/2026-09-12/p33-g/source-sdk-venv/bin/python` 与 Tauri dev/Vite 源码路径。SDK 输入冻结后，由 `Host:backend/deskpet/sdk_adapters/runtime_paths.py::capture_sdk_source_attestation` 捕获新快照，启动器设 `DESKPET_SDK_RUNTIME_MODE=editable-source`、`DESKPET_SDK_SOURCE_ATTESTATION=<新ignored绝对路径>`、显式 backend dir / DESKPET_PYTHON；不修改 vendor pin、Service 或用 PYTHONPATH 假冒 wheel。
- 复用主 `Host:.local-test-evidence/2026-09-12/p33-g/launch-source-ui.py` 的 keyfree userdata/redacted log/进程管理方法，另开 **P35 唯一 run、userdata、端口、应用标识**。它是当前可复用的本机 ignored 脚本，不是发布入口。不能照抄仍在运行的 P33 端口/路径；不手动再起 backend 或第二个 Vite，不读取/输出 broker key。
- 当前 Host settings 默认逻辑/物理各1；P35 试验配置明确设模型总槽2、Verifier槽2，至少3 Mission，逻辑需求超过2。三者是原计划起点，不是性能承诺。Host 当前只向 SDK 传单 provider，验证多 profile 总限还需要前述最小配置接线；**当前不能只修改 settings 就宣布完整实验已可运行**。
- 三 Mission 均从真实 CUA 点击创建/导入来源。只读日志/receipt辅助判定；禁止 WS 直注创建、DB 造状态或直接调用内部 commit helpers 替代 UI。备份/故障控制可由显式测试维护入口触发，但 Mission/审批/取消的业务动作继续走 UI。

### 6.2 最小运行矩阵

| Run | 真实 UI 动作及受控条件 | 必须取得的证据 / AC |
|---|---|---|
| L1：三 Mission 共用2槽 | UI 创建 M1 多来源比较、M2 含真实人工审批的受控交付、M3 长文档多轮分析。保留最早不可丢的约束与末尾可核事实。至少两条请求同时在途，第三条真实排队；配置多profile时从同部署汇总 | 有业务结果与真实 HTTP/Provider invocation 区间，不仅返回200；global/profile live permits、actual invoke peak、queue原因/时间、各Mission进展、usage refs。A01/A02/A04/A07 |
| L2：慢验证与控制 | 在**真实 verifier 执行边界**施加有界可释放延迟，仍使用真实结果和验证守卫；UI 保持可查看，取消一个排队任务，批准 M2 的真实待人请求。移除延迟后验证/冲突队列排空 | RAISED/CLEARED及计数曲线、Worker减速、最终验收费用/槽位实际可用；排队取消后的旧请求零新handoff；human等待不占执行槽；在途费用如实结算。A02/A03/A04/A06 |
| R1：交叉故障 | 由主对**专属 run 的确切进程/调用阶段**故障注入，分别覆盖实际turn结果已落库未回收、SUBMITTED待验证、外部动作已应用未收receipt；冷启动同一userdata，在UI查看并继续批准/取消 | 旧/新PID与owner、dispatch/agent/turn/result/receipt身份比对、Worker无重复、UNKNOWN先lookup、预算不释放。关闭/重开UI窗口不是backend crash的替代证明。A05/A06 |
| B1：隔离恢复 | 从UI确认停止新准入；维护入口导出WAL一致备份和完整清单；主将副本恢复到另一ignored userdata，冷启动源码UI查看同Mission与未决审批 | 每库/产物hash/角色/schema对齐；原userdata保留；已完成不重跑，未知外部动作核对前不重发。对缺库/错hash副本单独拒绝，不能修改原库来制造成功。A08并回归A05/A07 |

M2 的真实 human 不能仅由 prompt 文字“请人工确认”保证，须走既有正式 review/action/source_change 合同。普通 review 不替代 doc conflict 仲裁。M3 的长 Context 要通过 P3.4 接线后的真实角色工作预算制造窗口轮转，不能只把一份很长但未进入请求的源文件算长上下文。

### 6.3 受控故障与真实模型证据分开

现有 `DESKPET_ORCHESTRATION_TEST_SCENARIO` 仅登记 `approval-action`，并限制单 Mission；不能直接拿它跑三 Mission 压力。SDK `agent_orchestrator.testing.fixtures.RoleScriptedProvider`、`graph_proposal_step`、`envelope_step` 可复用来做确定性资源/故障 oracle，但 scripted replies 不是真实模型质量证明。

若自然 Provider/Verifier 速度无法稳定产生压力，最小追加是复用同一 `.local-test-evidence` 隔离规则的新 scenario 与**真实边界延迟/故障门**：明确 run/subject/request 身份、超时可释放，不改 verdict/receipt/业务状态，不直接写图或库。真实模型 run 的延迟门继续传递原 Provider 响应，仅如实标记人工延迟；不能在 handoff 之后套 Provider 等待来冒充已经修好的资源准入。新 scenario/维护入口尚不存在，须实施及独立审查后才能执行 L2/R1/B1。

建议将故障切点选择建立在现有 dispatch/action durable stage 上，而不是靠 sleep 猜时序。需同时记录后台自然退出/强制退出与所涉子进程；测试结束检查本 run 的 backend、Vite、sandbox、browser/driver 残留，不能按泛化进程名杀掉别的工作。

## 7. 完成门与当前结论

当前结论：**必要预算/物理槽位子集已有实现和限定控制，P3.5 尚未 ready for acceptance**。§9 记录取消自动扩容、共享准入与 handoff 重验的实际进展；tail 保护、priced 完整预算、完整负载/恢复矩阵、多库备份清单仍未完成。不能用这些定向控制代替真实容量、冷进程恢复与源码 UI 验收。

完成须同时满足：原8 AC逐项有决定性 oracle；至少三 Mission/两模型槽/两Verifier槽的真实源码 UI 负载闭环；真实指标支持硬上限/计费/排空结论；交叉故障与隔离备份恢复成功、反例确实拒绝；新默认能力不以关闭其他已完成能力换通过；受影响旧 code/doc/审批/UNKNOWN/Context 语义回归。仅声明已测试的平台、部署拓扑和并发范围，不声称全平台压测，不等待或开展 P3.6 打包发布。

本次交付只写本 draft；不改 ARCHITECTURE/其他 plan。已限定通过的生产事实及整合结果由主同步架构事实源与 journal，P3.5 整体验收保持未完成。

## 8. 2026-09-13 补充：在途 usage 与终态收集的账务扩审

**状态：具体 timeout 漏账已复现并经主修复，已限定验证通过；其他入口仅列 P3.5 待审，不宣称已修复或已复现。** 不放宽费用断言、不直接改整个 ledger 为 upsert，不把此项扩大成 P3.6 发布工作。

主串行 `g-critic-recovery-v1` 为 4 passed / 1 failed / 0.75s。失败 selector：`tests/orchestrator/p33/test_g_critic_dispatch_recovery.py::test_unanswered_critic_timeout_keeps_one_intent_until_real_charge_is_collected`。受控 Provider 走真实 SDK，第二次 Critic 调用由 Event 保持在途；超时后只保留一个 intent，取消与终态收集已执行，但 token 结算断言为 150 != 300。这不是索引错误：`BudgetLedger.usage_for(subject)[0]` 明确为该 subject 的输入、输出 token 合计。

独立只读对照两库：SDK 两条同 agent invocation 最终均 `succeeded`，各 input=100/output=50；Host 编排 `imported_usage` 对第一条为 100/50，第二条却为 0/0、unpriced=1、unknown=0，reservation 已 SETTLED 且 settled_tokens=150。原路径在 timeout 的 result=None 时先 `_import_usage`，`AgentBridge.usage_facts` 将仍 handed_off、无 usage 的调用映成 0；append-only `ON CONFLICT(usage_ref) DO NOTHING` 随后拒绝同身份的真实 150。不能把“未定价”解释成“执行已结束且费用为零”。

本轮最小修复由主执行：将 timeout result=None 分支移至 `_import_usage` 前，保留 SUBMITTED/reservation，直到 collector 确认原 SDK turn 终结再导入真实事实。300-token oracle 保持，主另补 `reservation.settled_tokens == 300`。主报告 `g-critic-recovery-v2` 首批5＋第二批5共 **10 passed / 1.39s**（wrapper 1.64s）；随后 `g-p33-compat-v7` **874 passed / 35.70s**，包含两批控制和该 reservation 断言及 P3.3/旧 recovery 兼容。修复纳入 SDK `e346689`；Host 当时为 `e690bdcf`。本任务未执行这些测试，结果引用主 runner/journal，不将其扩大为全部角色的账务验证或 N1v4 真实 UI 通过；下列 v1 失败证据仍完整保留。

P3.5-A03/A05/A06 必须扩审的入口与 oracle：

- `_reimport_unsettled`、recover、liveness/missing-turn、各角色 after-stop collector 是否可能在 invocation 非 final 时导入 provisional zero。逐一检查实际 SDK state/usage/charge，不能只依据 Attempt 终态或“未定价”判断。将 CLAIMED/HANDED_OFF、实际 SUCCEEDED/FAILED、真正 UNKNOWN 分开；UNKNOWN 未核对继续占用原 reservation。
- 以真实慢 Provider 分别覆盖 priced/unpriced：调用 1 完成、调用 2 在途时触发取消、超时或恢复；最终已知事实逐 invocation 与 imported_usage、reservation、Task→Mission→global account chain 对齐。冷重开及重复收集后不漏、不重复计费，也不提前释放；测试不能只核 `usage_for` 一个聚合值。
- Critic AGENT_CREATED 已纳入本轮 exact-turn 收集修复；其他 plan/manager/attempt 等角色的“SDK 已 submit、Host receipt 未落库”与 `_cascade_stop` 交叉窗口仍需逐角色审计。不能推定 Critic 的 guard 已覆盖所有角色，不能因 Host 状态未 SUBMITTED 就按零费用关闭真实在途调用。
- 不用普通 upsert 掩盖已经结算后的修改：若扩审发现需支持原 provisional/UNKNOWN 事实向 final 核对，必须定义可允许的状态迁移、原身份绑定、账户差额和 replay 幂等，再由独立 oracle 证明；这不是本次 timeout 时序修复的已实现内容。

本地证据相对 SDK 根目录；原始日志/库不进 Git。读取时已确认编排 WAL 为 0、execution 无 WAL，使用只读 immutable 连接核对，未忽略未 checkpoint 的日志：

| 证据 | SHA-256 |
|---|---|
| `.local-test-evidence/2026-09-12/p33-g/g-critic-recovery-v1.log` | `d28bccff27a6576404aa8759d676a40ab8668534faccff6a95b057ce734c2618` |
| `.local-test-evidence/2026-09-12/p33-g/pytest-tmp/g-critic-recovery-v1/test_unanswered_critic_timeout0/orchestrator.db` | `bf5ac49cac1c207d50c47f119d981b59122ece135802e4423e3c8ea1f52e21de` |
| 同目录 `execution.db` | `ca5e646ec2935199d98bf35f35a9335a316b6e758ff0438b089d50805af6f9d2` |


## 9. 2026-09-13 当前必要子集：provider 逐请求预算与物理槽位

### 9.1 已实施边界

真实 N1v4b 暴露的是 **Task 90k 在一个 SDK turn 内消耗 224780 tokens / 20 次实际调用，之后下一次 reservation 失败**，不是 Mission 已花满 400k。用户据此授权提前实施必要 P3.5 子集，不扩大 400k 上限。当前源码在 `runtime/provider_budget_guard.py`、`simple_harness/execution/provider_admission.py` 与实际 runtime/dispatch/ports/wire 接缝中：

- 每次 provider request 在实际 wire prepare/restore 完成后计算 public input allowance，加同 Agent 已结算的全部 prior output（estimator 要求时），再加最大输出上界；无已支持 estimator/protocol 不猜测放行。此前 terminal FAILED **有真实 usage** 的可恢复 empty/length 响应结算并纳入 prior output；缺 usage 的 FAILED 与 HANDED_OFF/UNKNOWN 保持未知，不按零处理。此 bound 是绑定 profile/protocol 的保证合同，不宣称任意模型精确输入计数。
- 在原 Task→Mission→Global reservation 同事务 grow token allowance，使用持久 `provider_token_grants` 统一争用槽位；不另建第二套费用账，不把等待算为物理 handoff。配置的 `max_concurrent_model_calls` 不再随逻辑并发和候选数自动扩容；共享范围是使用同一编排 Store 的已接入 runtime/profile，并非整个 Host 所有聊天/实时 provider 路径。
- 等槽在实际 AgentBridge liveness 中标记 `provider_slot_wait` / nonbillable，原 Mission deadline 仍有效；取消/owner/原 intent 与请求身份在 handoff 前复核。DB 事务内没有网络 await。UNKNOWN grant/原 reservation 保持；overrun 先持久化实际事实，再拒绝新准入，恢复不能因抛异常回滚事实。
- 已 SUBMITTED 的 service 续调依据实际 SDK Run lease 和原 Agent/turn 身份，不因旧 dispatch claim 到期或编排 owner 变化就误释放。RELEASED grant 重授权仍验证完整身份及 bounds。
- coordinator 记本地实际在途调用，例行 reconcile 不将其改为 UNKNOWN。SQLite handoff CAS 同事务写入 `run_events` 的 `provider.handoff_authority.v1`，事件 ID 为 `provider-handoff:{invocation_id}:{handoff_ordinal}`，记录真实 owner/epoch；恢复 UNKNOWN 的事务仅在**原 handoff owner/epoch**仍持有有效 Run lease 时拒绝。新 epoch 不能替旧 handoff 保活。历史无此事实且当前 lease live 时保守 hold，不猜原 owner。

最后一项经过实际 SQLite 第二连接/真实 lease claim 的接管控制，以及实际 runtime reopen 后 active call 的旁观者负控；**不是完整 OS 多进程 crash/kill/recovery 矩阵**。§2 A01/A02/A04/A05/A06 得到局部实现与 oracle，任何一项都未据此单独标为全面验收完成。

### 9.2 当前验证与独立审查

以下为主线程唯一 runner 的实际报告，本任务没有运行 pytest：

| Run | 结果与可声明范围 |
|---|---|
| `p35-admission-recovery-v3` | **15 passed / 1 failed / 6.41s**，wrapper 6.68s。包括原 admission、UNKNOWN/known failed、overrun、取消和真实 Orchestrator planner→Worker tool turn→Critic 链控制；冷 owner 恢复 test 的第二次实际 handoff 被 extra reconcile 误改 UNKNOWN，失败证据保留 |
| `p35-cold-owner-v4` | **3 passed / 0.44s**，wrapper 0.69s，PG11016 已退出（主报告）。原红项、新 lease 接管旧 HANDED_OFF、priced 明确拒绝三个控制通过；不把两批相加伪装成同一完整兼容批 |
| 静态收口 | 本任务执行 owned Ruff check、14 文件 Ruff format check、主指定 mypy 范围、`git diff --check`，均 exit 0；无 pytest/build/install/commit |
| 独立窄审 | Ohm 已 **限定 ACCEPT，无剩余 P0/P1**：核对 handoff 同事务 authority、原 owner/epoch live 免 UNKNOWN、新 epoch 不保旧调用、legacy 保守 hold 与对应控制。范围不含完整 P3.5 |
| SDK 整合兼容与源码 UI | 主正安排 P3.3/P3.4/P3.5 + SDK wire/context/recovery 的整合兼容及随后源码快照运行；本次未收到结果，**保持待验证**，不沿用旧 874 PASS 作为当前改动证据 |

可复跑的三控制命令（SDK 根，仍由主唯一 runner 执行）：

```sh
.venv/bin/python -m pytest -q \
  tests/orchestrator/p35/test_provider_budget_recovery.py::test_new_runtime_owner_recovers_submitted_service_and_old_owner_cannot_handoff \
  tests/orchestrator/p35/test_provider_budget_recovery.py::test_new_lease_does_not_keep_old_handed_off_invocation_alive \
  tests/orchestrator/p35/test_provider_budget_identity.py::test_current_token_guard_explicitly_rejects_priced_configuration
```

### 9.3 明确未完成的 tail 与 priced 合同

**Tail 尚未保护。** 当前 grow 会尊重已存在的 reservation，但没有为未来 Critic、冲突或 final synthesis 建立不可被 Worker 消耗的真实 hold。`_next_attempt` 的 critic share/headroom 与图预算里的 synthesis/system reserve 只是分配规则，不能当作已创建 reservation；一个 Worker 的多次 provider grow 仍可能吃掉后续角色所需余额。后续最小方案须兼容 P3.4 selection round：先真实保留有界验证/综合预算，再在同事务把 hold 转给实际 subject，公共祖先不重复扣款、无 release→reserve 竞争窗口，UNKNOWN 不可被拿来腾出额度。这个方案尚未实施，也不保证任意长 Critic 一定能完成。

**Priced 完整实现尚未支持，当前明确拒绝。** 新 token guard 不宣称 `cost_micros` 可随 token grow 同步满足共享硬上限：`ProviderBudgetGuard(priced=True)` 拒绝；带该 guard 的非 unpriced AgentRuntimePorts 拒绝；Orchestrator 配置 estimator 且任一 profile 有 price_table 时拒绝。原来不启用此 guard 的 priced 运行语义保持，不能将其当作新 guard 已支持 priced。后续须以输入/最大输出的冻结价格预留，在原账户链同步 grow token 与 money，证明并发、取消、UNKNOWN、跨 owner 恢复及最终差額结算；本次真实 flash 无 price_table 的 token 验收不能覆盖这些门。

其他原 AC 仍保留：多 profile/三 Mission 真实峰值、长时间等槽与 UI 控制时延、完成洪峰背压/最终验证排空、全冷进程故障矩阵、Context 业务验收、协调多库备份与恢复都没有因本子集通过而关闭。§8 其他角色/其他入口 provisional usage 的扩审仍在待办；本轮 `AgentBridge.usage_facts` 已排除非 final provisional 记录，不据此推定所有恢复/收集入口已经逐一验收。

### 9.4 原始证据索引

原始文件留在 SDK ignored 目录，不复制日志/DB 到 Git；下列 SHA-256 是本次只读对文件字节计算的索引：

| 文件 | SHA-256 |
|---|---|
| `.local-test-evidence/2026-09-12/p33-g/p35-admission-recovery-v3.log` | `43e3855035df06b1ec0135b013258970a1bf8f5cdadf0753802aaaa57e3cfebc` |
| `.local-test-evidence/2026-09-12/p33-g/p35-cold-owner-v4.log` | `3a75cdc9908b3a2bc2fc2c788afa5fe7624c777b6d68d869d817d96e1a51725e` |
| `.local-test-evidence/2026-09-12/p33-g/p35-cold-owner-v4.json` | `d378e244ed58e7b685ce5d3f0e3bcdad3cab073d85cd26a38afef37c40e45cbd` |

## P34/P35 runtime integration, 2026-09-13 02:50 CST
- `p35-admission-collection-v1` exit1, wrapper0.7s; PG17691. Command `python -m pytest -q tests/orchestrator/p35/test_admission_collection_runtime.py --maxfail=3`; raw sibling SDK `.local-test-evidence/2026-09-12/p33-g/p35-admission-collection-v1.log`, SHA-256 `002acc7c54c87c34d539ae54e29a4aaf76deee1282017ecbd175d9350d82b8dd`.
- `p35-admission-collection-v2` exit1, wrapper1.02s; PG18132. Command `python -m pytest -q tests/orchestrator/p35/test_admission_collection_runtime.py --maxfail=3`; raw sibling SDK `.local-test-evidence/2026-09-12/p33-g/p35-admission-collection-v2.log`, SHA-256 `a8b10200c6f1dbad90ecb34760f79e54a70c720172bd67f7abbb365c00cf283d`.
- `p35-admission-collection-v3` exit0, wrapper0.56s; PG18144. Command `python -m pytest -q tests/orchestrator/p35/test_admission_collection_runtime.py --maxfail=3`; raw sibling SDK `.local-test-evidence/2026-09-12/p33-g/p35-admission-collection-v3.log`, SHA-256 `12c13b571d8e8108a46c806d2eb1b853b9b760aa4925f151f6ea89c325c4e808`.
- `p35-priced-cold-v1` exit0, wrapper0.64s; PG18391. Command `python -m pytest -q tests/orchestrator/p35/test_priced_budget_cold_reopen.py --maxfail=1`; raw sibling SDK `.local-test-evidence/2026-09-12/p33-g/p35-priced-cold-v1.log`, SHA-256 `59b9466559aeabf218a54ec041fd83cd357cf9bf1d80a17c47aab62ed0c205bf`.
- `p35-first-default-v1` exit0, wrapper0.8s; PG18526. Command `python -m pytest -q tests/orchestrator/p35/test_first_protected_tail_hooks.py --maxfail=3`; raw sibling SDK `.local-test-evidence/2026-09-12/p33-g/p35-first-default-v1.log`, SHA-256 `83ef11ace12d1ef8964708e04e58196db7ea23f86721f1686aa707958b594b11`.
- `p34-selection-fragment-v1` exit1, wrapper2.7s; PG18644. Command `python -m pytest -q tests/orchestrator/p34/test_candidate_selection_runtime.py tests/orchestrator/p34/test_fragment_scope.py tests/orchestrator/p34/test_fragment_runtime.py tests/orchestrator/p34/test_search_replay.py --maxfail=8`; raw sibling SDK `.local-test-evidence/2026-09-12/p33-g/p34-selection-fragment-v1.log`, SHA-256 `ffc52228cb494729224fc3a2a44e757d25f40c29a59bb0ee33286e27365aa1b2`.
Admission collection v1:3 FAIL/.43s from an empty list_intents query; v2:1 PASS/2 FAIL/.75s from main FIRST-release keyword-only call misuse. Both fixed; v3:3 PASS/.36s, actual SDK refusal routes stop or preserve UNKNOWN without blind retries. A SUCCEEDED provider record missing usage remains held because the existing late-accounting API cannot supplement a SUCCEEDED record; no automatic release or fabricated reconciliation.
Priced cold:1 PASS/.40s after both SQLite connections actually close/reopen, new owner epoch and actual response reconciliation preserve original price and charge once; this is not an OS-kill test. FIRST default:6 PASS/.58s, actual production Orchestrator (no overlay subclass) Worker write → Critic read/derived verdict → acceptance, failed/cancelled/UNKNOWN and terminal unused-tail release. Independent review scoped ACCEPT. Mission-level future conflict/synthesis pool implementation remains open.
P34 selection/fragment v1:3 FAIL/4 PASS/5 setupERROR,2.27s, maxfail8. Candidate mount identity mismatch and fixture tuple-vs-JSON contract require fixes. A separate code review found missing inherited candidate source dependencies in document C; fix and dedicated oracle are in progress. No P34 completion claim. Frontend search UI51 controls passed/1.01s total; its new test unused React import was fixed after a typecheck error; final typecheck rerun in progress. Native fixture/N1 and the remaining full audit still open.
