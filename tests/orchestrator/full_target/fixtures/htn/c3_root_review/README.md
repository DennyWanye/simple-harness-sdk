# C3 真实根评审包（脱敏夹具）

来源：Grok 验收 H 臂 `H-L3-C3-r0`（2026-09-16，grok-4.6，种子方法 `code.fix-by-patch`），
只读证据目录 `simple_harness/.local-test-evidence/2026-09-16/htn-acceptance/runs-p23f-probe/h-arm/episodes/H-L3-C3-r0/`。
四个叶子各自 Acceptance、端口齐全、隐藏评分器 PASS，根评审（MISSION_FINAL）却 REJECTED；P2.3h 据此修包构造。

- `package_before.json`：修前根评审员收到的请求正文（`execution.db` → `workflow_checkpoints.provider_request_snapshot` 的 user 消息，逐字节）。
- `verdict_before.json`：评审员的回复（`<critic_verdict>` 块内 JSON），三条 findings 全部成立。
- `artifacts/`：四个 accepted_outputs 端口对应的产物原文（按 content_hash 从内容寻址库取出）：
  `facts.json`（facts 端口）、`diagnosis.md`（diagnosis 端口）、`window.py`（patch 端口，即 `stats/window.py`）、
  `REPORT.md`（verify 的 report 端口）；另附 `patch_REPORT.md`（patch 叶子工作区里的说明，修前不在任何端口上）。

脱敏：不含任何密钥、端点、代理 id（`produced_by`）、路径以外的机器信息；task/acceptance/artifact id 是内容哈希，照抄。
