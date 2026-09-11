# 第 7 步代码独立评审 · 第 1 轮（原文要点归档）

- 评审者：独立子代理（claude-opus-5，只读，没有改动仓库），2026-09-11
- 范围：`git diff 4340f20..5abe6ff -- src/ tests/`（切片 A–E）
- 评审者自测：`tests/orchestrator/step07` 55 passed / 1 skipped；另写 scratchpad 探针实测证实 P1-1
- 结论：**无 P0，3 条 P1 必须先修**，10 条 P2；处置见 `journal.md` §1（代码评审表）

## 已核对无问题

- 只执行一次：`begin_handoff` 在一个 `BEGIN IMMEDIATE` 事务里做再校验、按状态 CAS、预算预留、写 HANDED_OFF 与决定回执；执行器不写库；重交接只用同一幂等键且至多一次；UNKNOWN 只在权威核对"确认未开始"后才再交接；两进程并发核对时第二个被状态检查拒掉。
- 取消 Mission：`_cascade_stop` 不碰 HANDED_OFF / UNKNOWN，核对覆盖已终止 Mission。
- 审批绑定：交接前按当前版本与绑定哈希再校验；回执哈希含 request_id，旧版本回执不能用于新请求；同 nonce 重放返回原回执；批准 / 审核 / 仲裁都要求 Mission ACTIVE；拒绝 / 撤回 / 过期后不能交接。
- 单一写者：`src/` 中除 Commit Service 的两个 mixin 外没有地方写四张新表或 `set_result_verification`；事务内没有跨 await。
- 判定分段与人工审核：按集成树键缓存判定；只剩等待人工时返回 False；SUSPENDED 不被重拾；`needs_human` 不短路、code_test 失败就不问人、政策没写也强制人工层、每个 Task 只升级一次；结果被替代时审核请求一起 CANCELLED。
- 接管与证据：不能复活终态 Task；retry 受 max_attempts 约束；人工文本在 API 与 Commit 两处做密钥检查；证据经 `redact_text`；CLI 身份只来自 `--as`。

## P1

- **P1-1** L3 同一审批人第二次批准没有被拒，仍写决定、发 `ApprovalGranted(counted:false)`，且 `begin_handoff` 把所有 grant 回执写进 `decision_receipts`（实测 alice n-1、alice n-2、bob n-3 三次都成功，3 条 ApprovalGranted，交接记录 3 个授权回执，实际只计 2 次）。违反 S7-05"同一审批人第二次批准被拒"与 D7-4' 审计链。处置：写入前抛 `ActionCommitError`；测试：第二次抛错、ApprovalGranted 数 = 计数、`decision_receipts` 长度 = `required_count` 且审批人不同。
- **P1-2** 独立 judge 没跑成（ContractError / 预算不足）时写成 `met=False, source="independent"`，被 `judgment_conflict` 当作"Verifier 冲突"送仲裁，人可以在从未有独立验证的情况下裁成 met；非动作 Mission 原本在这种情况下 FAILED。违反 D7-8'②（要求真的判为未满足）与 ORCH §12.4（没运行是 ERROR）。处置：标 `source="unavailable"` 并从冲突判断中排除；测试：judge 调用失败 → `mission_criteria_unmet` FAILED、无仲裁请求。
- **P1-3** 两条决定性测试缺失：冲突仲裁①的编排接入点 `_arbitrate_conflict`（冲突任务次数用完）完全没被测到，且改变了第 4 步旧行为；plan §6 列明的"新 Mission 提交相同内容 → 新 action_id → 服务再应用一次"没有测试。

## P2

1. 多个动作准则会部分执行：一个 APPROVED 就交接，另一个还在等审批；之后另一个被拒则现实已改了一半。建议全部可交接或已 SUCCEEDED 才开始交接。
2. `revoke_approval` 接受 PENDING 请求（AWAITING_APPROVAL → REVOKED 不在状态表），应只允许 GRANTED 撤回。
3. `decide_approval` 发现过期时先 `_expire_request` 再抛错，事务回滚，过期没落盘。
4. 执行器超时后在 finally 里把 key 移出 `_inflight`，线程里的 execute 还在跑；本进程核对可能提前判"确认未开始"。建议线程结束前一直算在途。
5. 交接前没有用 `params_hash(action["params"])` 重算，也没复核候选字节。
6. 崩溃后在租约期内重启：`waiting_on` 不列 HANDED_OFF，核对只在 run 开始与每 50 轮做，`run()` 空闲返回、动作停在 HANDED_OFF，演示退出码 1 而不是 4。
7. 仲裁②条件用 `task_critic_passes > 0`，比 D7-8'②"各 Task 的 critic 都 PASS"宽，且没有逐条比较 Task Critic 对该准则的判断。
8. CLI 没捕获 `CommitRejected` / `StoreError`，例如对 BLOCKED Task 做 takeover stop 会打出 traceback。
9. `_decide_actions` 在本轮直接 await 连接器调用（最长 `connector_timeout_seconds`），期间其他 Mission 本轮无法推进。
10. S7-01 断言偏弱：`MissionCriteriaJudged == 1` 被事件幂等键掩盖；用例没有 `pytest:` 与自由文本准则，证明不了"等待期间不重跑 pytest / Critic"。

## 评审者建议的收尾顺序

先修 P1-1、P1-2，补 P1-3 两条测试；P2 的 1、2、6 建议本步处理，其余登记；修完重跑 `tests/orchestrator` 再打 wheel、推送。
