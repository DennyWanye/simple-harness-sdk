# C1-r1 修复轮请求包（脱敏夹具，P2.3j）

来源：Grok 验收 H 臂 `H-L3-C1-r1`（2026-09-17，grok-4.6，SDK 0.12.2 候选 c7cfedd），
只读证据目录 `simple_harness/.local-test-evidence/2026-09-16/htn-acceptance/runs/h-arm/episodes/H-L3-C1-r1/orchestrator/orchestrator.db`
（表 `dispatch_intents.config_json.message.content` 与 `events.payload_json`）。提取日期 2026-09-17。

这一局：三条种子方法对 `code.fix-failing-test` 全部 NEEDS_EVIDENCE（`code.test-is-failing` 观察 FALSE）→ 合成器第一问
`code.fix-by-reproduce-patch-verify-explain` TRIAL_ADMITTED → plan revision 1 → 6 个叶子全部 COMPLETED、六端口齐全 →
根评审 REJECTED（`c-change-explained FAIL`，summary 产物自述未修改源码）→ `PlanningRejected{ordinal 4, root_review_rejected}`
重开 Planner（ordinal 5）→ **修复轮的包 `method_library []` / `applicability []` / `open_compound_goals []`** →
Planner 只能答 `no_applicable_method` → `HierarchicalMissionStalled{hierarchical_no_dispatchable_work}` → FAILED。

| 文件 | 内容 |
|---|---|
| `package_ord4.json` | 修订 0 时最后一轮（ordinal 4，产生 revision 1 的那轮）Planner 请求包的 `plan / method_library / applicability / planning_rejected / planning_attempt / package_version` 段：方法库 4 条（3 种子 + 1 合成）、适用性 3 条 |
| `package_ord5.json` | 修复轮（ordinal 5）同样六段：**方法库与适用性都是空数组**，`open_compound_goals` 为空、`committed_primitives` 六条 |
| `root_review_rejected.json` | `HierarchicalRootReviewRejected` 事件载荷（去掉 `reviewer_agent_id`）：一条 blocker finding、`criteria` 两条 |
| `planning_rejected.json` | 五条 `PlanningRejected` 载荷（ordinal 1/2/3 修订 0 的三轮；ordinal 5 两条：`root_review_rejected` 修复记录与该轮自己的 `no_applicable_method`） |

脱敏：本机绝对路径（`typed_parameters.repository` 等）统一替换为 `<workspace>`；不含密钥、端点、代理 id。
task / occurrence / acceptance / package id 是内容哈希，照抄。

用途：`test_root_review_repair_library.py` 用 `package_ord5.json` 钉住缺陷形状（修前修复轮包三段皆空），
用 `root_review_rejected.json` 的 findings 作为脚本化根评审员的拒绝理由，并断言修后同形的修复轮包
带有根 occurrence 的方法库 / 适用性 / 被拒方法标记。第二条佐证 `H-L3-C2-r0`（`c-test-passes FAIL`，ordinal 4 修复轮同样三段皆空）
未入仓，见 journal §2l。
