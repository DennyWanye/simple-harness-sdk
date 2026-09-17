# C3 verify 叶工作区未预铺补丁（修前形态夹具）

来源：Grok 验收第 3 批 H 臂 `H-L3-C3-r0`（只读证据
`simple_harness/.local-test-evidence/2026-09-16/htn-acceptance/runs/h-arm/episodes/H-L3-C3-r0/`）。
本目录只保存修前失败形态与种子/补丁字节，**不写入**证据目录。

现象：种子方法 `code.fix-by-patch@2`，facts / diagnosis / patch 三叶 AcceptanceCommitted，
patch 端口产物已验收且隐藏评分器 PASS；随后 verify 叶 `VerificationFailed` ×9，每次
`rule_check`：`artifact 'stats/window.py' is not a recorded workspace file`
（`checked_artifacts: stats/window.py, REPORT.md`）→ `MissionFailed{budget_exhausted}`。

根因（本片要修）：verify 的 InputManifest 只绑了 `patch.diff`，派发工作区仍是未打补丁的种子；
Worker 自己把已验收的 `stats/window.py` 带进信封，收集处按 P2.3m 同哈希口径不登记为本叶产物，
`rule_check` 的「recorded workspace file」索引里没有它。

- `verification_failed.json`：attempt-1 的 `VerificationFailed` 载荷（问题句与 checked_artifacts）。
- `seed_window.py` / `seed_init.py` / `seed_test_public_window.py`：局工作区快照（未打补丁）。
- `patched_window.py` / `patch.diff`：patch 叶已验收的源码与 diff。

脱敏：无密钥、无端点、无机器路径。
