# c2_root_review 夹具来源（P2.3k / N2）

- 真实局：Grok 验收第 2 批 H 臂 `H-L3-C2-r0`（SDK c7cfedd，Host 23a3d6eb，grok-4.6 medium）。
- `package_before.json`：根评审意图 `…:root-review:pkg-root-e507f37b…:1` 的 `config.message.content`，即评审员（root-reviewer-v2）真正读到的包。去标识：mission id 改为 `mission-c2r0`，agent id 改为 `agent-redacted`，工作区绝对路径改为 `<worktree>`；其余字段（acceptance/task id、摘录正文、evidence_requirement）原样。
- `verdict_before.json`：`HierarchicalRootReviewRejected` 事件载荷（`c-test-passes` FAIL 的 blocker finding 原话）。
- `mission_goal.json`：该局 Mission 的用户目标原文与根绑定的 `typed_parameters`（`failing_test` 指向本来就绿的可见套件 `tests/test_public_pipeline.py`）。
- 用途：钉住缺陷形状——包里没有 `mission_goal` / `goal_parameters`，评审员只能按 `evidence_requirement` 的「named failing test」字面裁决。证据目录只读，未写入任何文件。
