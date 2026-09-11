# 第 8 步代码独立复核 · 第 2 轮（原文要点归档）

- 复核者：独立子代理（claude-opus-5，只读），2026-09-11；范围 `git diff 49106bf..23fcde5 -- src tests`（修复切片 F）
- 复核者自测：step08 68 passed / 1 skipped；另写 pytest 插件，在 step03–08 全部测试的每个编排器关闭前对库中每个 Mission 做回放、归因对账与"零层 PASS"检查（178 个 Mission 快照），并对 6 个 fixture 演示做 CLI 回放；探针在 scratchpad `review-f/`，仓库无改动
- 结论：**修复范围小**——1 条新 P1（旧的投影漏项被新不变量改报成"缺记录"），修完跑 step08 与回归即可打 wheel、推送，不需要再做完整评审；P2 可登记放行。处置见 `journal.md` §1（第 2 轮表）

## P1

- **P1-A** `ResultRejected`（Worker 提交无效 / 伪造，`reject_result`：Attempt RUNNING → RETRY_WAIT）被列为无正式状态事件，投影里 Attempt 停在 RUNNING。49106bf 报"不一致"，23fcde5 的 `attempt_terminal_missing` 把它改报成缺口、覆盖率 0.78–0.92。扫描中 8 例 / 6 个测试（s3_08b、s4_08、s6_07、s6_06 ×2、step06 review p0_2）；真实运行中一轮失败很常见。建议 `ResultRejected` 且 `reason != "superseded"` → Attempt RETRY_WAIT（Task 保持 ACTIVE），`superseded` 的迟到结果只作历史；补"被拒后重试"的回放测试。

## P2

1. P0-1 两个口子：`isinstance` 允许子类；没有要求"每次运行一个新的测试服务文件"（工厂可忽略 `root` 共用状态文件）。建议 `type(s) is TestConfigService` 且 `s.path` 在本次运行目录下。
2. P1-1 边缘：已成为 VERIFIED 知识、后被仲裁取代的落败方经 `resolves` 边进入 lineage，其 Attempt 被加进路径且 `claim_refuted=false`；`test_review_p1_1_*` 的探索分支从未执行。
3. P1-4 口径：只与 SETTLED 预留对账，unknown 用量（不结算）只出现在 `unsettled_usage_tokens`，`reconciled` 仍可能为 true；`settled_tokens` 是结算时对同一张表求和，不是完全独立的来源。
4. P1-3 另一半未处置：Critic 消融后 Task 级自由文本准则无人判定，只要还剩别的层结果照样 PASS，未写进连带影响。
5. 既有：库在 `AttemptCreated` 时把 READY 的 Task 设为 ACTIVE，投影要到 `AttemptStarted` 才设，运行中途的库出现 READY 对 ACTIVE 不一致（s3_07a/b、s6_08 共 6 例）。
6. 小：`cmd_evaluate` / 模块 docstring 没写退出码；P2-11 测试只断言前缀（推导本身已核对正确）。

## 已核实没有问题（摘要）

P0-1 的检查位置与异常传播；P1-3 的 `emptied` 判断（Task 契约本身禁止空政策，178 个 Mission 零层 PASS 为 0）；P1-2 其余新规则在 178 个 Mission（多候选、冲突重开、人工挂起 / 通过、取消、失败、崩溃前缀、多 Mission）上 0 误报，相关事件都同事务提交；P1-4 在 178 个 Mission 上全部 `reconciled=true`；P1-5 口径正确（样本数相等时不可能出现辛普森悖论，方向检查足够）；P2 修复未引入回归；新测试除 P1-1 的探索分支外都是决定性的。
