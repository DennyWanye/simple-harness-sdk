# 第 7 步 · 真实模型运行 1（deepseek-flash，approval-action）

- 时间：2026-09-11；代码：main `5abe6ff`（切片 A–E 已提交，代码 review 尚在进行）
- 命令：`--run-real-provider tests/orchestrator/step07/test_real_provider_approval.py`，内部执行 `python -m agent_orchestrator demo --scenario approval-action --provider env --unpriced --as demo-operator`
- 模型：`SH_MODEL=deepseek-flash`（按用户指示只用 flash）；密钥从本机凭据文件 `source` 进环境变量，不打印、不落盘；运行输出在进程内脱敏
- 结果：`1 passed in 11.47s`，exit 0

## 1. 发生了什么

1. 真实 Planner（flash，1223 tokens）把 Mission 拆成一个 Task：outputs 为 `CHANGE.md`、`actions/set-new-ui.json`，验收层 `format_check`、`rule_check`，准则里还自己加了 `file:actions/set-new-ui.json`。
2. 真实 Worker（flash，11702 tokens）读 `docs/ACTION_FORMAT.md`，写出 `CHANGE.md` 与候选：
   ```json
   {"connector": "test_config", "operation": "set", "target": "feature_flags.new_ui",
    "params": {"value": "on"}, "reason": "在测试配置服务上启用新 UI 特性开关，为后续测试验证做准备。"}
   ```
   候选只有规定的五个字段，没有夹带 `approved` / `level` 等字段。
3. 验证强制跑候选检查并通过；accept 事务里从已存字节重验并登记为 L2 动作 `action-be790671a41ae8bb:v1`，发出 `ApprovalRequested`；`run()` 空闲返回。
4. 演示操作员 `demo-operator` 经 `ApprovalApi` 批准（决定回执 `0c4aeb77…`）。
5. 第二次 `run()`：非动作准则已入账，执行器在判定最后一步交接（`handoffs=1`），测试服务应用一次（`service_ref=test-config#1`，`applied_count=1`），回执与账本核对一致（回执哈希 `159ccc08…`）→ 动作 SUCCEEDED → Mission COMPLETED（`verification_passed`）。

## 2. 报告行（进程内已脱敏）

```json
{"chain": {"actions": [{"action_key": "action-be790671a41ae8bb:v1", "approval_request_id": "approval-action-be790671a41ae8bb:v1",
  "artifact_hash": "318a28980eb8ce218e4b8fe43667368117ac6b4360373b2156936e8f3ca39691", "candidate_artifact_id": "artifact-b1d4fd29c5b8af1c",
  "connector": "test_config", "decision_receipts": ["0c4aeb773188065388935bb73e8e99696d087abd8ece0bb0f1f51b675a29f1c0"], "handoffs": 1,
  "idempotency_key": "action-be790671a41ae8bb:v1", "level": "L2", "operation": "set",
  "params_hash": "30e50379633aad16fd8b87d1baa72fbb5e20275cbedd01ccc5b59ac77a3bc78e",
  "receipt_hash": "159ccc089e47c0bd3485c43a2609e972cbf1d50e2923ef4f25cb741a8e1904f3", "service_ref": "test-config#1",
  "state": "SUCCEEDED", "target": "feature_flags.new_ui", "version": 1}],
  "service": {"applied_count": 1, "config": {"feature_flags.new_ui": "on"}, "kind": "test service (not production)"}},
 "elapsed_seconds": 11.26, "exit_code": 0, "model": "deepseek-flash", "status": "COMPLETED", "stop_reason": "verification_passed", "waiting_on": []}
```

## 3. 证据检查

- `baseline.json`：`provider_kind=env`、`model=deepseek-flash`；`metrics.json`：tokens 按角色 planner 1223 / worker 11702（真实调用，未定价记为 unpriced）；`human`：1 个动作请求 GRANTED、1 条决定、等待 0.059 s；`actions`：SUCCEEDED 1、交接 1。
- `trace.json`：Worker span 的请求模型为 deepseek-flash；Planner 服务调用 SETTLED；actions 链含决定回执与服务回执。
- 密钥扫描：证据目录 29 个文件（不含数据库），用真实密钥逐字节比对命中 0 次，`sk-[A-Za-z0-9_-]{20,}` 命中 0 次。

## 4. 结论与局限

- S7-01/02 的真实闭环成立：真实模型只能交候选；未批准前服务零调用；批准后只执行一次并核对回执。
- 本次是"演示操作员"在同一进程内批准，身份是自报的 `--as`（本地构建的约定，真实部署需接认证）。
- 本轮 Worker 一次写对，没有触发重试、拒绝、UNKNOWN 等分支；这些分支由 fixtures 上的确定性测试覆盖（S7-03…S7-08）。
