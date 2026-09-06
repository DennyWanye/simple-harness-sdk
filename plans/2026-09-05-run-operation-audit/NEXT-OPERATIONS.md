# 下一项 Agent operation audit：只读 inventory 核对

2026-09-06。本次只有 git/文本/本地 wheel ZIP 字节读取及本文写入；没有 pytest、模型、
Provider、构建、安装、数据库 probe。未改冻结 H073、Service/Memory 代码或 Host。
原始范围依据 Host `plans/2026-09-05-agent-operation-audit/PLAN.md`；不使用 plan 系列技能。

**结论：不建议继续给已覆盖的 Harness command/provider/effect 重造 ledger。最有价值的
一个真实剩余 producer 是 Service Realtime 的工具结果→ACK→后继 response.create 操作链。**
它有实际网络发送和后继 Agent 响应，不是音频帧 telemetry；没有经过 Harness Run ledger。
当前 Host primary foreground 不走该链，所以不是 H073 terminal identity 合成的阻断项。
这也不是发现“Host 已发送但审计漏记”的新实测故障；以下是固定源码推导。

## 本次核对基线

- H073 frozen buildsource `0282fa982995b24bc893fdf6bed69d2caacd6587`；owner tree
  `/Users/denny/projects/simple-harness-sdk-operation-audit` HEAD `8bdc738`（后继仅文档/consumer）。
- Host `/Users/denny/projects/simple_harness-primary-candidate` HEAD
  `78647bb0e5a672f5779edf30f3aa280c7a1e186a`，H073/M0612/S0312；主组合测试待 slot。
- Service frozen source `/Users/denny/projects/simple-harness-service-sdk` HEAD
  `47f372adc641d8d3516599dd21cb94cf5955d6a7`，version0.3.12。
- Service 已有独立审计分支 `/Users/denny/projects/simple-harness-service-sdk-operation-audit`
  HEAD `66b6dc63c0bdbc26df962f091dcd5f72efba918d`；既有 source review 是 scoped ACCEPT，
  不把该分支当 Host 当前已安装能力。无需重做它的 Service request observer。
- 本地读 Host vendored0312 ZIP：service.py、realtime/session.py、realtime/observability.py
  与 frozen Service source 字节相同；ZIP 不含 operation_audit.py、run_audit.py、realtime/audit.py。
  session.py SHA256 `a03b8b3775d8c150f673a065f1bb37fe6d6eebe5eb8527df266a2199cb52daa4`。
  这是本地字节核对，未调用远程 release/API，不冒称重做三 SDK installed 验收。

## H073 已有 producer 与公共读取位置

以下路径均相对 Harness owner tree 的 `src/simple_harness/`，旧 COVERAGE.md 早期 pending
段不能覆盖后续 source/artifact 已完成状态。此表说明已有来源，不给所有历史作全覆盖保证。

| 原 inventory 域 | 当前实际 producer / reader | 仍需保留的语义 |
| --- | --- | --- |
| C1/C2 proposal/batch/TaskExecutionEnvelope/route gate | runtime/drivers/react_loop.py:96 EffectBatchExecutor 及各 runtime_operation；tools/executor.py:249 起 record_tool_audit；execution/sqlite/audit.py 的 proposals/effect link | pre-effect deny 可以没有 effect；提案不算物理执行 |
| C3 Provider binding/budget preclaim、attempt、retry/unknown | execution/dispatch.py:298/388 runtime_operation；canonical provider_invocations/reconciliation；sqlite/audit_witness.py + audit.py | preparation、head、transition、physical attempt 不重复计数；timeout 不是未发 |
| C4 Context/fresh selection | react_loop.py 的实际 context runtime_operation + canonical checkpoint；execution/context_staging.py、sqlite/stage_audit.py | 不是 Memory 内部检索/变更的证明 |
| C5 command/control/preRun | sqlite/uow.py command ingress/CAS + sqlite/command_audit.py；runtime/kernel.py:849/857 command open/page | noRun 有独立 command namespace；不为了审计制造 Run |
| C6/C7 children/workflow/control | sqlite/audit_core.py:6 CORE_SOURCES：child_commands/links/terminal/signals/acks，workflow admission/operation/recovery/fork/spawn/terminal receipts，projection/wait/continuation joins | 可读的当前 mutable head 不等于丢失历史的完整 transition archive |
| Delivery | execution/delivery.py 与 sqlite/delivery_audit.py 的实际 claim/handoff/settle；既有 CAS receipt | claim/lease expiry 不证明发送/未发；parent 有独立引用域 |
| committed-turn MemoryPort | execution/memory_outbox.py + sqlite/memory_port_audit.py；kernel terminal 原子 sdk_memory_outbox anchor | outbox created 不等于 Memory applied；receipt 不等于其内部 materialization |
| noRun prepare/release + Run consumption | sqlite/stage_audit.py、stage_audit_schema.py；kernel.py:833/841 stage open/page；indexed binding_sequence/root/continuation verification | 旧无 witness、无状态变化的 release 无法补历史 |
| C8 recording continuity / whole selected snapshot | sqlite/audit_coverage.py:8，audit_pages.py:108/255；kernel.py:812/820 Run open/page | birth alone 不能覆盖 old072 中途恢复、custom driver/UoW、未闭合 execution interval |
| raw SDK terminal identity | sqlite/audit.py:332 terminal_evidence；public RunTerminalAuditEvidenceV1.matches | Host 已核 raw SDK namespace；不是 Host envelope/record hash |

没有从这份已约定 inventory 找到需要立刻再给同一个 Harness 操作建立第二套事实的理由。
但也不能称全覆盖：audit_coverage.py 显式判 legacy_birth_unverified、
canonical_event_interval_unverified、runtime_operation_interval_unclosed、
driver_or_uow_recording_unverified 等；命令/stage 无 Run 的域需要消费者单独读，
terminal-only Host consumer 不自动消费它们。SDK source complete 不是消费者已审完，更不是
Memory/Service 生命周期或被旧写入者遗漏的历史已经齐全。

## Service 的实际区别：委托操作与独立操作

1. **普通 command 是委托。** Service source `service.py` 的 HarnessAdapter submit_start/
   submit_continue/submit_cancel/get_command 直接走 public Harness command API；审计分支
   `_call`/`_observe`（:251/:424）在外层记录 request attempt。一个 Service retry 或 RETURNED
   不是新增 Provider/effect，也不证明客户端收到 ACK。H073 既有 command receipts 必须复用。
2. **已有 Service 审计 source 还不是 durable 全史。** 分支 operation_audit.py:138 的
   ServiceOperationSnapshot 明确 durable=False、process_lifetime_only、
   pre_service_transport_unobserved、client_ack_unobserved。Unix transport :145-215 在
   typed Service 之前做 auth/decode，在之后 write/drain；这些阶段不应倒推为 Harness receipt。
   当前冻结0312甚至未包含该新 observer，不能借 H073 安装把它标成已上线。
3. **读穿仍有一个独立消费缺口，但不是本次优先的新 producer。** 分支 run_audit.py:15
   RunAuditRequest limit<=256、service.py:220 仅调用 bounded read_run_operation_audit，
   client.py:56 同样如此；没有 H073 stable open/page，也没有 Unix audit RPC。
   将来直接 owner-bound 转发 H073 cursor，不做第二份 Service snapshot/ledger。
4. **Realtime 有独立 Agent 后继操作。** 下面的 tool result/followup 直接调用连接 send_text，
   不调用 HarnessAdapter、ProviderInvocationCoordinator 或 EffectExecutor。工具本身仍由
   调用方执行；Service 所拥有的是结果传输、ACK 验证及后继响应请求，不能声称工具 effect 成功。

## 唯一推荐后继：Service tool-result / followup 的实际操作来源

固定0312与审计分支的 `src/simple_harness_service/realtime/session.py`：

- :50 `_ToolRecord` 仅在内存保存 state/output/ack/payload/attempt/lock；:680 接受真实
  ToolCallRequested 后建立 REQUESTED。已有 response/item/call 身份校验必须保留。
- :213 `submit_tool_result` 校验现 tool、锁定同一调用，:234 `_submit_tool_result_attempt`。
- :239-244 编码结果并置 RESULT_SENT；:248 调实际 `connection.send_text(result_payload)`。
  **RESULT_SENT 在 send await 之前就置位，不能把枚举名称直接当发送成功。**
- :262 等真实 ACK；:632-644 核 call/item/type/status/output 后置 RESULT_ACKED。
- :273-283 编码 followup，:276 置 FOLLOWUP_REQUESTED，:283 再调实际 send_text；
  :598-600 收到 introduced response 后置 FOLLOWUP_STARTED。
- OpenAI adapter :126/:138 将这两步编码成 `conversation.item.create(function_call_output)`
  和 `response.create`；Qwen 同类 adapter 拥有自己的真实 wire。不要由审计猜 Provider 状态。

**可执行触发例（本次未运行）：** public RealtimeSession 已收到合法 tool request；调用
`submit_tool_result(call_id, output)`，传输记录已收第一个结果 payload 但 send await 超时。
随后调用方按原契约重试同一 output，SDK复用冻结 payload，真实 ACK 到来后发送 followup。
现在 `_ToolRecord.attempt` 被下一次 Task 替代，状态只保留 head；没有按 physical attempt 可读的
安全来源，H073 也没有对应 Run/event。既有 diagnostics 还只对每种 received event kind 首次
采样（session.py:464），不能用 ring 中 event 数推断实际 tool-result/followup 次数。

独立 Service audit 分支 realtime/audit.py:12 的公开投影只有 correlation/stage/closed code/
generation/frame/byte/duration；无 per-call/attempt/response linkage，且明确 bounded_diagnostics_only
与 realtime_no_verified_harness_binding。即使装上该分支，也不能补出这些丢失的操作身份。

### 最小后继范围，避免重复 ledger

- 仅上述接受后的 tool-result 发送、ACK、followup 发送/started 边界；复用 `_ToolRecord`
  的真实协议状态/锁和已验证调用身份。在实际发送入口/返回/异常及真实 ACK 回调取事实，
  每次 physical attempt 独立ID，冻结 logical tool identity；不建立第二个 tool execution 状态机。
- 按现 Service observation/sink 机制扩展一个明确的 typed family，传 safe opaque
  session/generation/call/attempt/link、实际时间和 closed outcome。不要将它塞成 start/get，
  不把 argument/output/凭据或任意 Provider error 写入 audit。
- 无 Harness binding 时 sdk_run_id=None，按 Service session/operation 域审计。不能凭 call_id、
  文本或相近时间挂到某个 Host Run，更不能假造 Provider usage/费用。
- 持久消费复用宿主已有 operation-audit journal 与幂等 receipt 机制；不要在 Service 新建
  平行 execution ledger。缺少 durable sink/持久回执时明确 unavailable/coverage gap；单有
  内存新 DTO 不得宣称 crash/reopen 全史闭合。该 durable seam 应在后继契约中明确后再实现。
- 观察失败不重发业务；send timeout 维持 unknown，ACK/response.started 只能由实际回调证明。
  不改变原重试、授权、tool execution、codec 或取消行为。不扩到每音频帧/heartbeat。

可复用既有真实 in-process transport tests（均只读查看名称，本次未跑）：
`tests/test_realtime_session.py:1095` ACK→followup；:1134 并发同输出只一次实际发送；
:1180 ACK timeout 不乱重发；:1465 result send retry 复用 exact payload；
:1510 followup retry 复用 payload。后继只给这组加 actual attempt/source 断言与持久 sink
重开零业务重发负例、missing sink 明确不完整，不重复大 Realtime suite 或 paid call。

## 与当前 Host 的关系和停止边界

Host `backend/deskpet/realtime_voice.py:203` 构造 RealtimeClient，随后交 LocalRealtimeChannelController；
当前该 controller 没有 submit_tool_result 分支，本次 Host 搜索也没有调用此 public tool-result API。
因此这是已有 SDK public/standalone 工具后继能力的审计缺口，不是已经证明当前 Host UI 使用的漏记路径。
其价值是补原用户“相关 SDK Agent 操作”中可明确定位的独立 producer；若下一交付只服务
当前 Host terminal UI，则先保持本项显式未覆盖，不阻挡已经限定 ACCEPT 的 H073/Host leaf。

本次不启动实现、不重跑已有验证、不分配后继版本或构建 wheel；不把以上静态触发例写成
已复现红例。H073 全部原始红/legacy gap、Service source-only/非 durable 边界继续保留。
