# 第 9 步代码独立评审 · 第 1 轮（原文要点归档）

- 评审者：独立子代理（claude-opus-5，只读），2026-09-11；范围 `git diff a8ce3b6..c519a1c -- src tests`（切片 A–F）
- 评审者自测：`tests/orchestrator` 356 passed / 8 skipped；step09 35 passed / 1 skipped
- 全量扫描（沿用第 8 步第 2 轮的 pytest 插件做法，插件在 scratchpad `review9/`）：205 个库、231 个 Mission（正式 218、评测沙箱 13）——回放覆盖率 100% / 0 不一致 / 0 缺口：0 例违反；绑定行存在且与 `MissionCreated.policy_version_id` 一致：0 例违反；归因未对账 2 例（`test_s2_03_replays_do_not_duplicate`、`test_p1_model_echo_mismatch_holds_reservation_and_stops`，第 8 步代码上同样，既有）；版本库一致性：192 个正式库 0 问题，13 个评测库全部报出（P2-3）
- 结论：**无 P0；2 条 P1 修完再收尾**；P2 可登记。处置见 `journal.md` §1（代码评审表）

## P1

- **P1-1** 升级后的旧库回放覆盖率掉到 100% 以下（`replay.py` `formal_from_snapshot` / `OPTIONAL_FIELDS`）：迁移写了 `policy-legacy` 绑定行，库的正式状态有 `policy_version_id`，但旧 Mission 的 `MissionCreated` 没有，于是计入分母、落进未覆盖。实测：a8ce3b6 代码生成的 v5 库未迁移时 `replay` 退出 0、覆盖率 1.0；被第 9 步代码打开一次（Orchestrator 恢复或只读的 `policy list`）后 `replay` 退出 1、覆盖率 0.909091。修法：legacy 绑定不输出该字段；补"旧格式库迁移后回放覆盖率 1.0"的测试。
- **P1-2** S9-08 演练没有做"高负载下频繁调整策略"（`test_policy_guard.py` 266–316）：只在种子版本下跑 3 个 Mission，过程中没有提出 / 晋级 / 回滚，未核对 Global 预算；`candidates_per_task <= 3` 由范围保证恒成立，峰值上界用 `max_concurrency × Mission 数` 而非本测试的约束。修法：在 Attempt 在途时于两次运行周期之间连续晋级 / 回滚、在 RAISED 下尝试扩张被拒；每周期断言每个 Mission 的开放 Attempt ≤ min(绑定 `mission_concurrency`, `max_concurrency`)、Global 与 Mission 账户不超支、部署政策与安全边界哈希不变。

## P2

1. S9-06 Manager 检测漏报：只记录带 `key` 或命名字段的越界操作；`{"op":"raise_hard_cap_micros","value":…}`、`{"op":"disable_code_test"}`、`{"op":"set_budget","task_id":…,"value":…}`、合法操作夹带 `max_concurrency` 等键都不记录（都不生效，缺的是审计）。
2. Worker 检测只匹配根目录 `policy/` 与三个固定文件名：嵌套路径与大小写变体漏报；用户项目里正常的 `policy/` 或 `deployment.json` 会留下误报（不阻塞）。
3. 评测库的版本库一致性必然报错：钉版插入的 sandbox 版本没有事件。
4. CLI 只读命令用 `Store.open`：路径写错会新建空库并退出 0；对 v5 库会执行迁移（叠加 P1-1 使回放失败）。
5. `policy promote --cooldown` 让任何 CLI 调用方可传 0 绕过部署冷却；不传 `--nonce` 自动生成使防重放形同虚设；`propose --params` 未传 `max_concurrency` / `profiles`，人工提议不能调高并发、拼错的路由目标会被接受。
6. `provider_kind`：有 profiles 时直接读 `RuntimeProfile.provider_kind`（默认 `"fixtures"`，且只看默认档位），外部调用方给真实 provider 忘设时会被记成 fixtures。
7. 幂等键吞复现：`PolicyRouteUnavailable` 路由器按版本缓存，每进程只有第一个 Mission 记录；`PolicyConfigDrift` 键 `激活次数:配置哈希`，漂移—改回—再漂移（中间无激活）第二次不记录。
8. `evaluate_candidate` 运行前不查提议状态：对 PROMOTED / REJECTED 提议会先跑完评测（真实 provider 会花 token）再在记录时被拒。
9. Commit 层 `propose_policy` 不校验参数，白名单 / 范围 / 模板只在 `PolicyApi` 与学习器里查。
10. 测试缺口：没有登记第二个 Worker 模板证明 `prompt_versions` 运行时真的切换；泄漏测试两侧都用 `spec_task_identity`，证明不了学习器侧与门槛侧一致（评审者探针验证 3 个 spec 与演示 history 6 个 Mission 两侧哈希全部一致）。
11. R2 的 Wilson 区间按首次 Attempt 计数、样本门槛按 Mission 计，DAG 型 Mission 的多个样本不独立，区间偏窄——启发式，报告写明即可。

## 已核实没有问题（摘要）

S9-04 全部决策点读绑定版本（分配、候选份额、提交守卫、Manager 三阈值含恢复路径、Worker / Planner / Manager / Critic 路由、Arbiter / Synthesizer 经 `template_for`、Critic `verifier_version` 与层复用），无残留读白名单配置或常量；路由健康不按版本拆开；绑定在 `create_mission` 同一事务；legacy 沿用部署配置与迁移前一致。状态迁移表完整无回边，晋级附加条件逐项生效，回滚只回 RETIRED、跳过 ROLLED_BACK、不受冷却。幂等键与单一写入者正确。学习器只读、三道检查、结果分三类、不写"提升"。门槛与第 8 步同口径、钉版只进评测库。Principal 只能是人。CLI 与演示退出码与脱敏正确；step02 改写保留语义。
