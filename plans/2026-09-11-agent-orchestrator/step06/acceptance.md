# 第 6 步验收标准（MUST 9 条 = ORCH-BUILD §8.4 S6-01～09）

| ID | 场景 | 必须观察到的结果（纲要原句 → 本步可判定形式） | 判定 | ORIGINAL-30 |
|---|---|---|---|---|
| S6-01 | 同时运行两个 Mission | 均有进展，配额不互相挪用，跨 Mission 资料隔离 → 两个 Mission 在同一 Orchestrator 内交替推进并都 COMPLETED；每个 Mission 的 `budget:<mission>` 账户只记本 Mission 的预留/结算，任一 Mission 的池耗尽不影响另一个；Mission A 的 Worker 读不到 Mission B 的工作区/知识（Tool Gateway 只绑定本 Attempt 的工作区，检索只查本 Mission）；证据目录按 Mission 分开 | `test_multi_mission.py::test_s6_01_*` | — |
| S6-02 | 人为放慢 Verifier | 验证队列有界；Worker 并发下降；停止低优先扩展；恢复后逐渐放开 → 待验证结果数 ≥ 高水位时 `BackpressureRaised(verifying)`：不再新建 Attempt（只允许冲突/饥饿档）；降到低水位以下才 `BackpressureCleared`（滞回）；期间 Worker 并发观测值下降 | `test_backpressure.py::test_s6_02_*` | 30-15 |
| S6-03 | 小模型失败后升级 | Trace 中实际 provider/model 改变，保留先前 Attempt 与费用，而不是只改标签 → 同一 Task 的 Attempt-1 记 `runtime_profile_id=small`（provider 回显 model = 小模型），失败后 Attempt-2 记 `runtime_profile_id=large`（回显 = 大模型）；两个 Attempt 与各自 usage 都在账本；事件 `ModelRouted{attempt, profile, reason}` | `test_model_router.py::test_s6_03_*` + 真实两模型报告 | — |
| S6-04 | 两个 Attempt 写同名文件 | 位于不同 Workspace；无覆盖；正式版本只经验证后提交 → 两个并行 Attempt 各自 `workspaces/<attempt>/` 写 `out.md`，内容不同互不覆盖；只有通过验证的那份成为 accepted artifact；另一份只作为历史 artifact 记录 | `test_workspace_isolation.py::test_s6_04_*` | 30-06 |
| S6-05 | 模型索要额外工具或读取其他工作目录 | 网关拒绝；Prompt 和模型置信度不能授权 → 调用未在 Mission∩Task∩Role∩部署政策交集里的工具 → `tool_not_allowed`；`../` 或绝对路径 → `workspace_error`；信封/Prompt 里的"已授权"文本不改变交集；拒绝写入审计 | `test_governance.py::test_s6_05_*` | 30-01/30-11 |
| S6-06 | 一个模型服务不可用 | 相应工作有界等待/明确降级；其他可执行 Mission 继续 → profile X 的 provider 连续失败 → `RuntimeProfileUnavailable{profile, until}`，绑定该 profile 的 Task 显式等待（`BudgetReleased`，不新建 Attempt，事件可见）或按路由表降级到备用 profile；其他 profile 的 Mission 照常完成 | `test_model_router.py::test_s6_06_*` | — |
| S6-07 | Token/工具/运行时间某一维耗尽 | 停止新分配；已发生费用和在途预留仍保留正确 → 三个维度各一条：达到上限后不再 `AttemptCreated`；账本里 settled 与 reserved 之和不变、不为零、不重复相加 | `test_governance.py::test_s6_07_*` | 30-13 |
| S6-08 | 重启多 Runtime 执行池 | 旧 Attempt 回到原目标配置，不能被另一模型静默接管 → 崩溃前 Attempt 绑定 profile=small；重启后只有 small 池恢复它（`recover` 按 `runtime_profile_id` 分区），large 池不打开它；事件里 provider 回显模型不变 | `test_model_router.py::test_s6_08_*` | — |
| S6-09 | 日志检查 | 无密钥；所有 Result 可追到 prompt/model/retrieval/allocator/verifier 版本 → 证据目录 grep 密钥模式 = 0；`lineage.json`/`trace.json` 对每个 Result 给出 prompt_version、context_version、runtime_profile_id + 回显 model、retrieval version、allocator_version、verifier layers 版本 | `test_observability.py::test_s6_09_*` | 30-01 |

附加门槛：`tests/orchestrator`（step02–06）全绿；SDK 全量红集 ⊆ 基线；安装 wheel 后跑 `tests/orchestrator` 与 `python -m agent_orchestrator demo --scenario multi-mission --provider fixtures --evidence-dir evidence/s6`；真实多模型小规模负载报告（deepseek-flash 为小模型、deepseek-v4-pro 为升级目标）；独立 review 处置；推送 origin main 且本地干净。
