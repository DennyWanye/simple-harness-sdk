# Slice 2 计划挑战（第 1 轮）与裁决

- 挑战者：独立子代理（breadth 模式，2026-09-10，基于 `701d89c` 的 plan v1）
- 裁决人：主编排者（用户已授权"技术取舍自行裁决、记账即可"）
- 结果：2 条 P0、10 条 P1、4 条 P2/建议；**全部处置见下表，未留悬空**

| # | 发现（挑战者原话摘要） | 严重度 | 裁决 | 落点 |
|---|---|---|---|---|
| C1 | **中途放弃的 Turn 留下在途 ReAct checkpoint**：per-turn 超限（`before_tool_batch` 在 provider 返回之后抛）、协作取消都会把 run 级 checkpoint 停在 `provider_reserved/tool_batch_reserved`，下一轮会**复用冻结请求**或被 T7 身份闸门判 FAILED 终态化 | P0 | **采纳**。driver 新增 `_settle_failed_turn`：任何**确定性**失败的 Turn（超限、取消、工具协议违约、provider 确定拒绝、admission 期 deadline 已过）都把 checkpoint 复位到 `ready`（只清在途请求/响应/工具进度，totals 不动）；UNKNOWN 路径**不**复位 | `agents/execution.py`；`tests/agents/test_turn_limits.py::test_per_turn_model_call_limit_fails_the_turn_not_the_agent`（断言下一轮 provider 请求是新请求、工具不被重放） |
| C2 | **T10 的 `continuation_capability` 组合期旋钮会改写 policy_fingerprint**，已存在 Run 下次唤醒即 `runtime_policy_mismatch` → FAILED | P0 | **采纳，删掉该旋钮**。T10 改为在 `agents/wire.py` 把"无正文且无 tool_calls"的最终响应转成 `ProviderEmptyResponseError`（`provider_empty_response`，definite failure），Turn 失败、Agent 存活；`provider_binding_fingerprint` 不变 | `agents/wire.py`；`tests/agents/test_provider_response_durability.py` |
| C3 | F-BA-1 的根因假设（reasoning 块）在 wire 上不可达：`OpenAICompatibleProvider._parse` 永远给 `str` 正文，只有"空正文"这一条闸门可达 | P1 | **采纳**。T10 只处理空正文；真实端点 2026-09-10 又跑 4 次全部 committed（`finish_reasons=[tool_calls,stop,stop]`，`empty_contents=[True,False,False]`——第一个 True 是 tool_calls 响应，属正常），累计 10 次未复现，F-BA-1 降级为"已有可见失败码 + 证据通道"，见 journal §4.3 | journal |
| C4 | **per-turn 预算每次恢复都刷新**：基线取自当前累计 totals，而非该 Turn 首次准入 | P1 | **采纳**。`base_agent_turns_v1` 新增 `tool_call_ordinal_from`（写一次，COALESCE），`provider_turn_ordinal_from` 同样只写一次；driver 从 `mark_agent_turn_running` 返回的行读回持久基线；deadline 锚定 `turn.created_at` | `turns.py::mark_turn_running`；`tests/agents/test_turn_limits.py` |
| C5 | **closing→closed 无人收敛**：drain 超时后 binding 永远 `closing` | P1 | **采纳**。收敛放在执行层 `turns.finalize_turn` 同一事务尾部（内核不 import agents）；drain 语义明确为"已受理输入（含排队）都会跑完" | `tests/agents/test_agent_close.py::test_closing_converges_to_closed_when_the_open_turn_finalizes` |
| C6 | **cancel_turn 无租约 stage 结果**违反围栏纪律 | P1 | **采纳，改设计**：调用方只写**持久意图**（控制命令行 + generation+1）并触发进程内 token；持有租约的 driver 在 loop 的取消点观察到后经**正常的 outcome 路径**产出 `agent_turn_cancelled` 失败结果；跨进程/排队中的 Turn 在准入时读到意图直接失败、不调 provider | `agents/runtime.py::cancel_turn`、`agents/execution.py`；`tests/agents/test_cancel_turn.py` |
| C7 | **cancel 与最终答案竞态把 Run 停摆**（结果已 stage → `_commit_agent_turn` hash 冲突 → `_abandon_run_authority`） | P1 | **随 C6 消失**：调用方不再 stage；最终答案先到则回执 `already_settled`，取消点在 provider 调用前与工具批次后（不打断在途调用），文档与测试如实声明 | `test_cancel_turn.py::test_cancel_that_loses_the_race_to_a_final_answer_is_already_settled` |
| C8 | **批量预检漏掉既有 binding 冲突**（S1 的 `create_many` 或单个 `create` 已占用同 key 且 config 不同 → 半批 + 裸 `ValueError`） | P1 | **采纳**。写前预检逐 index 读既有 binding：owner 不符或 config_hash 不同 → `AgentBatchIdentityConflict`，零写入 | `test_create_many_batches.py::test_existing_binding_conflict_is_caught_before_any_write` |
| C9 | **create_many 让每个 IDLE Agent 持有租约/fence/心跳任务**（100 个即 100 个续约任务） | P1 | **采纳，内核最小改动**：base_agent Run 空转（无输入、无阻塞器）时 `_abandon_run_authority`；`_wake_continuation` 在有在途 drive 时等它结束再唤醒，避免唤醒丢失。legacy Run 路径不变 | `runtime/kernel.py`；`test_input_queue.py::{test_idle_agent_holds_no_lease_fence_or_heartbeat,test_inputs_are_never_lost_under_a_burst}` |
| C10 | L3 只闭一半（per-agent lifetime 未执法） | P1 | **采纳（文案）**：AC10 收窄为 per-turn；per-agent lifetime 精确执法移交 S5（driver 单一 policy fingerprint 的结构性限制） | acceptance.md AC10、journal 遗留 |
| C11 | AC13 不在 DoD 与覆盖矩阵；任务数写 9 | P1 | **采纳（文案）** | acceptance.md DoD 1、plan.md 附 A/执行顺序 |
| C12 | AC8(c)"任何公开入口"与 `AgentRuntime.uow/kernel` 公开属性矛盾 | P1 | **采纳（文案）**：收窄为 `open/binding/create/BaseAgent` 入口；`uow/kernel/driver/ports` 明确记为可信调用方的 escape hatch | acceptance.md AC8 |
| C13 | 主要方面/里程碑错配：真正的结构性风险在 T6/T7/T8 的"run 级 checkpoint 跨轮复用" | P1 | **部分采纳**：T3.5 里程碑保留（已绿），另以 `test_turn_limits.py::test_per_turn_model_call_limit_fails_the_turn_not_the_agent` + `test_cancel_turn.py::test_turn_failure_keeps_session_and_cost` 作为第二里程碑（中途放弃后下一轮干净） | journal §4.2 |
| C14 | 建议把 cancel_turn 推迟到 S5、删控制命令表 | P2/建议 | **不采纳**：C6 的协作式设计已消除两条 P1，而 cancel_turn 属 program.md S2 范围，缩减范围是用户的决定；保留 | — |
| C15 | plan 行号漂移、`_fault` 注入点数量写错 | P2 | **采纳**：T9 以测试 `test_uow_facade_shape.py::test_fault_points_keep_their_slice_1_names` 断言 6 个注入点名字与顺序 + `after_commit` 在 facade | tests |
| C16 | 委派路径绕过新配额（child 无 `max_pending_inputs`；委派建的 binding 是否计入 `max_agents` 未说明；close 子 Agent 会让在途委派失败） | P2 | **采纳前两条**：委派路径传子配置的 `max_pending_inputs`；`max_agents` 按 owner_scope 计数（委派子 Agent 与父同 owner，计入）。第三条记为已知行为（journal 遗留） | `agents/tools/delegate.py`、`agents/runtime.py::create_many` |
| C17 | v10 描述符重算的影响面只是断言 | P2 | **采纳（证据化）**：仓库无任何 `*.db` 夹具（`git ls-files | grep .db$` = 0）；v9 checksum `d9cb3ed5…` 不变（`test_only_one_v10_descriptor_and_v9_unchanged`）；本地 S1 时期建的 v10 开发库需删除重建（journal 明示） | journal §3 |

**D7 补充裁决（执行期发现）**：plan 拟用 checkpoint 的 `active_turn_id` 作"同 Turn 恢复"锚点，但 `TerminationState.to_json` 一旦写入 `active_turn_id` 就强制 `schema_version=7`，而 v7 要求 `context_use_authority_scope` 非空（`termination.py:261-264`）——它绑定在 Context-use 协议上，BaseAgent 不能借用。替代锚点改为**持久序号规则**：在途 checkpoint 的 `provider_turns_reserved_total <= 本 Turn 的 provider_turn_ordinal_from`（首次准入即写死）即属于别的 Turn → `base_agent_turn_identity_conflict`。仍是 run 单键、`react.termination.v1` 单 namespace（不分键）。
