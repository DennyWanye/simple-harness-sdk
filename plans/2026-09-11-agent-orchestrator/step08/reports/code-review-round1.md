# 第 8 步代码独立评审 · 第 1 轮（原文要点归档）

- 评审者：独立子代理（claude-opus-5，只读），2026-09-11；范围 `git diff a24e18a..49106bf -- src/ tests/`
- 评审者自测：step08 56 passed / 1 skipped；另写 scratchpad 探针 `probe8.py` 复现 P0-1、P1-1、P1-2、P2-1
- 结论：**还不能进入收尾**；1 P0 / 5 P1 / 11 P2；处置见 `journal.md` §1（代码评审表）

## 已核实没有问题

投影只由事件决定、不从库回填；只读副本复制了 `-wal` / `-shm`，执行库在哈希范围内，只读目录测试有效；导入图不含 runtime / providers；分页读事件正确；未定价金额从不写 0；Fisher 与 Wilson 公式正确；每次运行新幂等键、新目录、新库；超时时 `run_pytest` 杀进程组、`__aexit__` 取消验证任务并关库；oracle 文件 Agent 看不到；派生前后旧库哈希不变；`REAL_KNOBS` 都在计划白名单内。

## P0

- **P0-1** 评测"只允许测试连接器"没有强制：`validate` 只在 case 含 `action:` 准则时才检查连接器类型，`_run_once` 把工厂返回的任何服务都启用；没有 action 准则而带 `PaymentConnectorStub` 的 case 能通过（Worker 仍可经 `actions/` 产物提出动作）；`validate` 检查的对象不是每次运行实际使用的对象。违反 D8-7'、S8-04、上轮 P0-4。处置：`validate` 无条件检查，`_run_once` 对实际拿到的服务逐个检查，不合规就拒绝（不能记 harness_error）。

## P1

- **P1-1** 冲突落败方的已接受 Attempt 仍在成功路径内（`refuted` 只阻止经知识路径加入的 Attempt）；测试只断言 `refuted_claims` 非空。按 D8-4' 应为探索（被驳倒），或修订 plan 并标 `claim_refuted`。
- **P1-2** 结构性缺口检测不全：D8-2' 列的"有 VerificationPassed 却没有 TaskCompleted"未实现；只用事件文件删掉 `VerificationPassed` 或 `MissionCompleted` 时 `gaps == []`、Mission 显示 ACTIVE；`compare` 把旧值算作已决定，覆盖率偏高；S8-05 测试有选样偏差。建议补不变量（已完成 Task 的 accepted result 不是 DONE/PASS、终态 Mission 仍有未结束的 Attempt / Result、有 MissionSuccessJudged 却无终态事件等），违反时写 gap 并把字段置 not_covered；参数化删除四类事件、文件与带库两种方式测试。
- **P1-3** Critic 消融可能让有效政策为空（Planner 可提出 `["critic_review"]`），`all([])` 为 True，零层验证 PASS；Task 级自由文本准则在消融下也无人判定。违反 §12.4。处置：有效集合为空时记 ERROR / FAIL（"ablation leaves no verification"）。
- **P1-4** 归因的 `reconciled` 仍是恒等式（未归类桶也被加进等式）。处置：未归类行数为 0 且各桶之和等于总量，并与独立来源（例如 `budget_reservations.settled_tokens`）对账；测试插一行未知主体用量断言 False。
- **P1-5** 策略比较未按 case 成对（合并 2×2 有 Simpson 悖论风险）；harness_error 不对称不影响结论；fixture 下耗时 / tokens 仍会写"有差异"。处置：逐 case 成功数与 Fisher p，方向一致才给总体结论；harness_error 数不同时降级；fixture 三项都写"不适用"。

## P2

1. `{"ablations": "critic"}` 字符串写法让自由文本检查失效，运行时每次都 harness_error 而 CLI 退出 0；CLI 计划里 `global_budget` / `price_table` 写成 dict 同理。建议 `validate` 先试构造 config 并规范 ablations。
2. approval-action 演示的开始快照在 `run()` 之后才算；multi-mission 的 baseline 无快照，`drift` 恒为 None。
3. `_provider_identity` 对 `OpenAICompatibleProvider`（`__slots__`：`_endpoint` / `_target`）只记下类名；fixtures 缺脚本摘要。无密钥泄露。
4. `snapshot_diff` 对 profiles / routing / connectors / provider 的来源只给顶层键名；评测取每个策略最后一次运行的快照比较，最后一次若是 harness_error 会拿不同 case 比较。
5. CLI：`replay` 无库又无 `--events` 或 Mission 不存在时 traceback；`--attribution` 无库时静默缺失；有缺口或覆盖率 < 1 仍退出 0。`evaluate`：计划文件不存在 traceback；未知 case 退出 1 而非 2；全部 harness_error 仍退出 0。
6. 断点使对应 Attempt 被归为 `not_used_by_final_products`，本身也是补边；`breaks` 有重复条目。
7. Replay 100% 覆盖的场景不够宽：候选被取代、`KnowledgeSuperseded`、冲突 DEFERRED / UNRESOLVED、仲裁 / 接管、审批撤销 / 过期、动作 FAILED / UNKNOWN / Reconciled 未覆盖；若干推导规则无测试。
8. "不外调"测试的 monkeypatch 探针形同虚设（replay 本不导入被打桩模块），真正起作用的是导入图测试。
9. 评测报告未写消融连带影响（needs_human 升级、第 ② 类仲裁随之消失）。
10. 派生 case 未校验"只接受本版本演示证据"，也未按场景名指定 provider 工厂。
11. harness_error 记录不带 `mission_id`，超时的运行无法据此回放。

## 收尾前必须完成（评审原话要点）

修 P0-1；修 P1-3、P1-2；P1-1、P1-5 修代码或修订 plan；改 P1-4 定义；各配决定性测试；补真实 deepseek-flash 小规模评测记录（本轮评审时尚未产出）。完成后重跑 step02–08 全绿再打 wheel、推送。
