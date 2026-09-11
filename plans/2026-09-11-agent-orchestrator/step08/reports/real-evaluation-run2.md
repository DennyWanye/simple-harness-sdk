# 第 8 步 · 真实模型评测运行 2（deepseek-flash，评审修复之后）

- 时间：2026-09-11；代码：main `23fcde5`（修复切片 F：代码评审第 1 轮全部处置之后）
- 命令：`--run-real-provider tests/orchestrator/step08/test_real_provider_evaluation.py`，内部执行 `python -m agent_orchestrator demo --scenario evaluate-policies --provider env --unpriced --test-timeout 120`
- 模型：`SH_MODEL=deepseek-flash`（按用户指示只用 flash）；密钥从本机凭据文件 `source` 进环境变量，不打印、不落盘；输出在进程内脱敏
- 结果：`1 passed in 97.95s`，exit 0
- 性质：**真实模型试验，与确定性回归分开报告**；每个策略只有 2 个样本

## 1. 计划

与运行 1 相同：case `parse-kv`（只有 `pytest:` 准则，带隐藏 oracle `x=1;y=2`）；策略 `full-policy`（完整验证政策）与 `no-critic`（消融 `critic`）；每个策略 2 次独立试验，共 4 次运行，每次运行新目录、新库。

说明：Mission id 由租户与幂等键 `eval:<plan>:<strategy>:<case>:<trial>` 决定，所以与运行 1 相同；两次评测的目录与库完全分开，运行 1 的库没有被打开。

## 2. 逐次运行

| 策略 | 试验 | Mission | 结果 | Attempt | tokens | 墙钟 s | oracle | 消融政策下的 PASS |
|---|---|---|---|---|---|---|---|---|
| full-policy | 1 | mission-2d7a9dd677134058 | success | 2 | 23737 | 16.5 | 通过 | 否 |
| full-policy | 2 | mission-c035e8a74e859aee | success | 2 | 26294 | 28.1 | 通过 | 否 |
| no-critic | 1 | mission-549b11355e94dca6 | success | 2 | 23456 | 23.4 | 通过 | 是 |
| no-critic | 2 | mission-d63c8ff2b9ba163b | success | 2 | 32498 | 29.1 | 通过 | 是 |

## 3. 汇总与比较

- full-policy：2/2 成功，Wilson 95% [0.3424, 1.0]；耗时 16.4–28.0 s；tokens 23737–26294；验证误判 0/2；脚手架错误 0
- no-critic：2/2 成功，Wilson 95% [0.3424, 1.0]；耗时 23.3–29.1 s；tokens 23456–32498；验证误判 0/2；脚手架错误 0
- 逐 case 成对（评审 P1-5）：`parse-kv` 2/2 vs 2/2，Fisher p = 1.0，方向"相同"
- 成功率：结论"证据不足：样本量 2 / 2 低于 3"；耗时与 tokens：都写"证据不足（样本不足）"（运行 1 自查发现的口径问题已修，本次按新口径输出）
- 策略差异取同一 case（`parse-kv`）的两份开始快照比较；中文报告有"消融连带影响"一节
- 金额：未定价（null），不写 0。

## 4. 与运行 1 对照（只作观察，不下结论）

运行 1 完整政策约 51 s / 5.1 万 tokens、每次 3 个 Attempt；本次完整政策约 16–28 s / 2.4–2.6 万 tokens、每次 2 个 Attempt。同一 case、同一策略在两次评测间差出一倍，说明真实模型的单次波动远大于两次样本能分辨的策略差异——这正是报告写"证据不足"的原因。

## 5. 证据检查

- 证据目录 126 个文件（其中 8 个是数据库）；真实密钥逐字节命中 **0**。
- `sk-[A-Za-z0-9_-]{20,}` 在 4 个运行库 `orchestrator.db` 内有 453 处匹配，全部是标识符 `…:task-…` 中的 `sk-` 片段（匹配前 3 字节均为 `:ta`）；加单词边界的 `\bsk-[A-Za-z0-9_-]{20,}` 命中 **0**，数据库以外的文件命中 0。与第 3 步记下的陷阱一致（脱敏与扫描用 `\bsk-`）。
