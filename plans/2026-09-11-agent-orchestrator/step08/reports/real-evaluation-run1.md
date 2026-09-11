# 第 8 步 · 真实模型评测运行 1（deepseek-flash，evaluate-policies）

- 时间：2026-09-11；代码：main `49106bf`（切片 A–E 已提交，代码评审进行中）
- 命令：`--run-real-provider tests/orchestrator/step08/test_real_provider_evaluation.py`，内部执行 `python -m agent_orchestrator demo --scenario evaluate-policies --provider env --unpriced --test-timeout 120`
- 模型：`SH_MODEL=deepseek-flash`（按用户指示只用 flash）；密钥从本机凭据文件 `source` 进环境变量，不打印、不落盘；输出在进程内脱敏
- 结果：`1 passed in 153.12s`，exit 0
- 性质：**真实模型试验，与确定性回归分开报告**；每个策略只有 2 个样本

## 1. 计划

- case：`parse-kv`（只有 `pytest:` 准则，带隐藏 oracle `x=1;y=2`）
- 策略：`full-policy`（完整验证政策）与 `no-critic`（消融 `critic`）
- 每个策略 2 次独立试验，共 4 次运行；每次运行是新的 Mission、新目录、新库

## 2. 逐次运行

| 策略 | 试验 | Mission | 结果 | Attempt | tokens | 墙钟 s | oracle | 消融政策下的 PASS |
|---|---|---|---|---|---|---|---|---|
| full-policy | 1 | mission-2d7a9dd677134058 | success | 3 | 51622 | 50.8 | 通过 | 否 |
| full-policy | 2 | mission-c035e8a74e859aee | success | 3 | 51394 | 51.2 | 通过 | 否 |
| no-critic | 1 | mission-549b11355e94dca6 | success | 2 | 40513 | 26.9 | 通过 | 否（真实 Planner 这次给 Task 的验证政策里没有 critic_review，谈不上被消融） |
| no-critic | 2 | mission-d63c8ff2b9ba163b | success | 2 | 36386 | 23.4 | 通过 | 是 |

## 3. 汇总与比较

- full-policy：2/2 成功，Wilson 95% [0.3424, 1.0]；耗时 50.7–51.1 s；tokens 51394–51622；验证误判 0/2
- no-critic：2/2 成功，Wilson 95% [0.3424, 1.0]；耗时 23.3–26.8 s；tokens 36386–40513；验证误判 0/2
- 成功率比较：Fisher p = 1.0，结论"证据不足：样本量 2 / 2 低于 3"
- 耗时 / tokens：报告写了"有差异（区间不重叠）"——**这是自查发现的口径问题**：样本量低于下限时，耗时与 tokens 的比较也应写"证据不足"。已登记在 journal §1，随代码评审修复一起改（修复后重跑本评测）。
- 金额：未定价（null），不写 0。

## 4. 证据检查

- 证据目录 135 个文件（不含数据库）：真实密钥逐字节命中 0，`sk-[A-Za-z0-9_-]{20,}` 命中 0。

## 5. 解读边界

- 两个样本不能说明去掉 Critic "更快更省而同样成功"：这只是在一个简单 case 上的 4 次观察；按口径不下结论。
- 去掉 Critic 后每次少一次 Critic 调用、Attempt 数 3 → 2 是可观察的机制差异（S8-04 要求报告"关闭了什么、哪些变化可观察"，不预设方向）。
