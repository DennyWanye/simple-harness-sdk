# 第 9 步 · 真实模型门槛评测运行 1（deepseek-flash）

- 时间：2026-09-11；代码：main `c519a1c`（切片 A–F 已提交，代码评审进行中）
- 命令：`--run-real-provider tests/orchestrator/step09/test_real_provider_policy.py`，内部执行 `python -m agent_orchestrator policy evaluate <提议> --provider env --unpriced --trials 2 --case parse-kv --test-timeout 120 --timeout 1800`
- 模型：`SH_MODEL=deepseek-flash`（按用户指示只用 flash）；密钥从本机凭据文件 `source` 进环境变量，不打印、不落盘；输出在进程内脱敏
- 结果：`1 passed in 178.95s`；CLI 退出码 1（门槛结论不是 PASSED）
- 性质：**真实模型试验，与确定性回归分开报告**；每一方只有 2 个样本

## 1. 设置

- 正式库：以部署配置物化的种子版本 `policy-c632140c6b700bc6` 为生效版本（`no_progress_limit=2`）
- 候选：人工参数（`PolicyApi.propose`，来源 `human:tester`，"人工参数，非规则改进"）——`no_progress_limit=3`，版本 `policy-50738b67894efe66`
- 评测：生效版本与候选各钉进一个策略，在各自的新评测库里运行同一个留出 case `parse-kv`，各 2 次

## 2. 逐次运行

| 一方 | 试验 | Mission | 结果 | Attempt | tokens | 墙钟 s | 说明 |
|---|---|---|---|---|---|---|---|
| active | 1 | mission-bcbe89843bb25cfe | failure | 3 | 43042 | 43.3 | 停止原因 `no_progress`：模型连续交了没有产物的结果（rule_check "no artifacts were submitted"），达到生效版本的 `no_progress_limit=2` 后停止 |
| active | 2 | mission-270020c68cdb65fa | success | 2 | 42305 | 37.9 | |
| candidate | 1 | mission-cc7293fc3602283a | success | 3 | 63460 | 46.7 | |
| candidate | 2 | mission-3b71a63ff1d50c18 | success | 2 | 42427 | 50.7 | |

## 3. 门槛结论

- **INSUFFICIENT**：理由"每个 case 每一方至少 3 个样本；不足：['parse-kv']"。门槛按每个 case、每一方计样本（plan D9-7'），真实证据下限 3。
- 逐 case：active 1/2、candidate 2/2，Fisher p = 1.0；active Wilson 95% [0.0945, 0.9055]，candidate [0.3424, 1.0]。
- 结论已经 Commit 记进该提议（`PolicyEvaluated`，证据种类 real），提议状态为 INSUFFICIENT，**不能被批准或晋级**；`gate.json` 同样写明。
- 金额：未定价（null），不写 0。

## 4. 解读边界

- 候选多给了一次"无进展"机会（3 对 2），active 第 1 次正好在第 2 次无进展时停止——这是一个可观察的机制差异。两个样本不能说明候选更好，门槛也没有这样写。
- 这正是纲要 §11.3"数据不足与效果不佳也是完整结果"的真实样例：系统如实给出"样本不足"，没有自信地上线一个未充分评测的候选。

## 5. 证据检查

- 证据目录 149 个文件（含正式库与四个评测库）：真实密钥逐字节命中 **0**，`\bsk-[A-Za-z0-9_-]{20,}` 命中 **0**。
