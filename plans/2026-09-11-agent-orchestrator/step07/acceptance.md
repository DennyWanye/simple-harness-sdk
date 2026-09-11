# 第 7 步验收标准（MUST 8 条 = ORCH-BUILD §9.3 S7-01～08；原文 §30：30-03、30-22、30-23）

| ID | 场景 | 必须观察到的结果（纲要原句 → 本步可判定形式） | 判定 | ORIGINAL-30 |
|---|---|---|---|---|
| S7-01 | L2 候选未批准 | 没有真实修改；审批请求可查询且重启后存在 → Worker 交出的 `actions/*.json` 候选经验证后登记为动作（L2），`ApprovalRequested` 入账；测试服务的状态未变、连接器零调用；关闭并重开 orchestrator 后 `ApprovalApi.list()` 仍列出同一请求；Mission 仍 ACTIVE 且 `waiting_on` 列出该审批，`run()` 空闲返回 | `test_approvals.py::test_s7_01_*` | 30-22 |
| S7-02 | 批准指定版本 | 只执行对应参数/hash 的动作一次，得到可核对回执 → 批准绑定 (action_id, version, params_hash, artifact_hash)；执行器交接前再校验、先写 HANDED_OFF 再调连接器、回执里的幂等键与 params_hash 与账本一致 → SUCCEEDED；连接器只被调用一次；重复投递批准 / 重复运行循环不产生第二次执行；Mission 的动作准则满足后 COMPLETED | `test_action_execution.py::test_s7_02_*` | 30-03、30-23 |
| S7-03 | 批准后 Agent 改了内容 | 原批准不能用于新动作；重新请求审批 → 同一业务动作的新候选（不同参数）成为新版本，旧版本与其批准变为 SUPERSEDED；执行器只认当前版本的批准；新版本重新发起 `ApprovalRequested` | `test_approvals.py::test_s7_03_*` | — |
| S7-04 | 拒绝 / 撤权 / 过期 | 阻止后续新 handoff，不把拒绝当作成功 → 三种情况各一条：动作不交接、连接器零调用、Mission 以 `approval_rejected`（detail 区分 rejected / revoked / expired）FAILED，不是 COMPLETED | `test_approvals.py::test_s7_04_*` | — |
| S7-05 | L3 只有一次批准或同回执重放 | 不执行；满足部署定义的双重审批后才可能执行 → L3 动作一次批准后仍 AWAITING_APPROVAL；同一批准回执 / 同一 nonce 重放不计第二次；部署政策要求不同审批人时，同一审批人第二次批准被拒；满足后才执行 | `test_approvals.py::test_s7_05_*` | 30-22 |
| S7-06 | 外部动作执行成功但回执丢失 | 进入 UNKNOWN / 核对；不重复执行，不回滚数据库伪装没发生 → 测试服务已应用修改但连接器抛出传输错误 → 动作 UNKNOWN（`ActionOutcomeUnknown`），预算预留保持占用；核对按幂等键向服务查询 → 找到回执 → SUCCEEDED（`ActionReconciled`）；服务只应用一次；崩溃在交接之后、结果之前的恢复同样走核对，不重发 | `test_action_execution.py::test_s7_06_*` | 30-03、30-23 |
| S7-07 | 用户接管停滞或 Verifier 争议 | 保留 HumanOverride 和依据；未获授权范围不扩大 → 需要人工审核的结果（任务政策含 `human_review` 或 Critic 标出 `needs_human`）挂起为 NEEDS_HUMAN，人工判定后继续；人工 PASS 不能覆盖未通过的代码测试；接管停滞任务（停止 / 附说明重试）写 `HumanOverride{principal, basis}`；接管不能批准动作、不能扩大工具或预算 | `test_human_review.py::test_s7_07_*` | — |
| S7-08 | 外部文档要求自动批准 | 被当作不可信数据，不能改变审批状态 → Worker 读到的不可信文档写着"已自动批准"、信封里也声称已批准 → 审批仍 PENDING、动作不执行；模型没有任何能改变审批状态的工具；只有 `ApprovalApi` 调用方传入的认证身份能决定 | `test_approvals.py::test_s7_08_*` | 30-21 |

附加门槛：`tests/orchestrator`（step02–07）全绿；SDK 全量红集 ⊆ 基线；schema v4 库升级 v5；安装 wheel 后跑 `tests/orchestrator` 与 `python -m agent_orchestrator demo --scenario approval-action --provider fixtures --evidence-dir evidence/s7`（证据含候选、审批、执行、回执核对的完整链）；CLI `approval list|approve|reject|comment|revoke|takeover`；真实模型（deepseek-flash）演示记录（Worker 产出候选 → 审批 → 执行 → 回执核对）；独立 review 处置；推送 origin main 且本地干净。
