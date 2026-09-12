# O4 replay-v14 finding classification

审计起止：2026-09-13 05:08:03 CST – 2026-09-13 05:08:39 CST（本次父审返工）。

范围：只读 `.local-test-evidence/2026-09-12/p33-g/replay-v14-all-durable.json`，并对照 P33-22、O4 插件及原测试函数；未改 snapshot、生产/plugin/tests，未 spawn、未 commit、未跑 pytest。v14 数据的 source head 是 SDK `6866` + WIP，和当前审查 checkout SDK `c9a1f183b4b4268f89539beacae91bfa2328e689`、Host `45c09756f57fdcb2eb095e825d228dd60b7e644d` 分开，不能把 v14 归因于当前 c9a1。父审证据 ID：`d10dbef56b388f31ee85f11686b3cede69bbfb08f54656198c4631023ab72b79`（raw JSON SHA-256）。

固定 selector 与预期：

- O4 主 selector：`g-checkpoint-integration-v14/all-durable-after-run`。
- 回放预期：`unknown_event_types == {}`；事件流完整后 `comparison.coverage == 1`、`mismatches == []`、`not_covered == []`；部署流另查 registry 一致性；故意反例必须保留 finding，不能豁免整项测试。
- 原测试 selector 取自每个 evidence temp 目录对应的完整函数名；测试预期按函数体核对，不按目录短名猜测。

## 17 findings

| # | observation_id | 原测试 selector | 具体 comparison / gaps / errors | 分类与理由 |
|---:|---|---|---|---|
| 1 | `f1392aa8d5594b21109c6cd04dba32d88f1fc0aa8f7ffed60454bc87392d88fd` | `tests/orchestrator/p33/test_g_replay_audit_plugin.py::test_scan_uses_all_events_beyond_store_default_page` | `coverage=1`，无 gap/error；`unknown={Probe:10001}` | 有证明的故意反例：测试真实写入 10,001 个未知事件，预期保留 unknown 并 gate OPEN，证明未截断。 |
| 2 | `4a6a9995961870ec666a8051c4353a741036d005186c6b3ecf6d3833f4a81878` | `...::test_observer_does_not_persist_raw_event_payloads` | `coverage=1`；`unknown={Probe:1}` | 有证明的故意反例：故意注入私有 payload，预期只报告类型、不导出 payload；不是回放漏读。 |
| 3 | `e65e3025dcc0f1b8d3a0d9ee802fa08cd50bf351a332e05dfd6315124c51592f` | `test_p33_source_summary.py::test_stale_summary_projection_excludes_current_knowledge_without_history_rewrite` | `coverage=.75`；`not_covered=knowledge old/valid status,superseded_by`；无 error | 有证明的故意反例：fixture 用 `upsert_knowledge` 直接造 summary 投影状态，没有对应事件；O4 正确揭示非事件驱动状态，不能冒充生产回放 gap。 |
| 4 | `7a5cbe0b9ceb5e11e9a0da51f2b0f5883ba7b6b841f541fa4d820153b98d1d20` | `test_g_replay_audit_plugin.py::test_scope_routing_does_not_hide_unknowns_or_global_formal_drift[unknown_global]` | deployment `unknown={Probe:1}`，registry 一致 | 有证明的故意反例：故意向 deployment stream 写未知事件，预期全局 unknown 被保留。 |
| 5 | `b700f22e0a53526b5e2b7923631aee68b4a6117d6c24bfccd61aef92da3901cc` | 同上 `[policy_on_mission]` | mission `unknown={PolicySeeded:1}`，comparison 完整 | 有证明的故意反例：故意把部署事件放入 Mission，预期 scope routing 报 unknown。 |
| 6 | `727dab8c3e32be699900357ae126bc7afc20e586540489fe8db49c5d8715c3b6` | `...::test_global_finding_resolves_to_exact_exported_observation` | deployment `unknown={Probe:1}`，registry 一致 | 有证明的故意反例：故意 global unknown，测试要求 finding 的 observation_id 精确可回指。 |
| 7 | `ce1e6cf6b538cf0326469bb6ee646a4216e2b0f757c3ab30575c22f3fff8c42e` | `...::test_events_without_mission_row_remain_in_all_mission_inventory`（owner） | owner `gap=mission_created_missing`，`coverage=0` | 有证明的故意反例：故意把事件迁移为 orphan，预期仍保留 owner 与 orphan 诊断。 |
| 8 | `e078a9ba1d75c7fa085ade03feafcd4ff2f9c0bfcafb38082849c2d1a15f27b5` | 同上（orphan） | orphan `errors=[snapshot:StoreError]` | 有证明的故意反例：无 Mission row 的 orphan 不可形成正式 snapshot，预期错误可见且 gate OPEN。 |
| 9 | `06a0d8b5b6c2fc75e6ddd63c5aa061944e9798034afeff0c09cd9c7e6d34e6ab` | `...::test_memory_and_same_path_reopen_preserve_nodeid_and_do_not_double_count_unknowns` | `coverage=1`；`unknown={Probe:1}` | 有证明的故意反例：`:memory:` 已关闭库与重开同路径库的重复/unknown 计数场景，预期 Probe 只计一次。 |
| 10 | `03cfa49e1dd6a1b5f634f3ff1c0193b1047a65c1650183bbf6dcffd8008c41f8` | `...::test_corrupt_mission_json_does_not_hide_its_events_or_other_missions` | `unknown={Probe:1}`；`errors=[snapshot:KeyError]` | 有证明的故意反例：故意把 Mission JSON 损坏，预期事件仍可扫、snapshot 错误不隐藏。 |
| 11 | `96404a570bab6353f638b91ce27fc04f7df730d6e54cf135709997fc932f8295` | `test_p33_source_summary.py::test_stale_used_dependency_masks_only_its_consumer_summary` | `coverage=.857143`；`not_covered=knowledge old status,superseded_by` | 有证明的故意反例：summary 测试用直接 knowledge/monkeypatch fixture，未构造完整事件历史；缺口属于 fixture 边界。 |
| 12 | `67aee77dff1d807b3b656a55cc8e58bba656bd71b961fcc1046c967c72a14f89` | `...::test_discovery_keeps_direct_sqlite_bad_mission_without_migrating_it` | `scan=false`；`errors=[events:ValueError,snapshot:KeyError]` | 有证明的故意反例：手工仅建 `missions` 表且 JSON `{}`，预期只读发现、拒绝迁移并 gate OPEN。 |
| 13 | `bf742fc0842db67394e34c7503eee41c0e97e660584b05e049a7e73c97b339c2` | `...::test_unknown_and_formal_drift_are_not_a_whole_test_exemption` | `coverage=1`；Mission `status` mismatch；`unknown={Probe:1}` | 有证明的故意反例：同一库中故意损坏 negative Mission，positive Mission 仍完整；预期 finding 不能豁免全测试。 |
| 14 | `66b17d56b0b531dbf7ce2201841f23a283914eb7e63b0670051a12f2b7a8b8db` | `tests/orchestrator/p34/test_fragment_scope.py::test_consumer_requires_accepted_validation_and_does_not_promote_file_claims` | `coverage=1`，`unknown={}`；Task `...:task-3 status` replay/library SHA mismatch | 未解释真实 gap：该测试走 accepted fragment consumer 的生产式事件路径，结果不是故意损坏、无 error/unknown 可解释；需主线后续定位事件折叠与 Task status 的差异。 |
| 15 | `0805c9e499731031bba73fa286126a1ab438f1e144eff6f19ad9d3a658ec90c3` | `test_g_replay_audit_plugin.py::test_scope_routing_does_not_hide_unknowns_or_global_formal_drift[registry_drift]` | deployment registry consistency mismatch；无 unknown | 有证明的故意反例：故意把 policy version 置 RETIRED，预期 registry drift finding。 |
| 16 | `1f6ee9573b427a281bd4af1a1d852baa3562624cc43684e18eaefd3b6fedc40e` | `...::test_bad_event_json_keeps_raw_unknown_detection_and_decode_error` | `scan=false`；`errors=[events:JSONDecodeError]`；raw unknown 仍保留 | 有证明的故意反例：故意破坏 MissionCreated payload，预期 decode error、partial raw unknown 和 OPEN 同时保留。 |
| 17 | `195c0e721c5c9edb863e34166791dbfda749abf37b156a71ac7272d03063e2ac` | `tests/orchestrator/p33/test_p33_doc_arbitration.py::test_e06_direct_conflict_accept_cannot_use_caller_or_real_ordinary_review_pass[True]` | `coverage=1`；`gap=approval_requested_missing` for `ApprovalGranted` | 有证明的故意反例：测试明确导入 pre-E pending ordinary-review fixture，再授予普通 review；预期缺少冲突专用 ApprovalRequested 时仍不得完成冲突裁决。 |

## 两条 discovery error

- `invalid_sqlite_header`：`.../test_host_readonly_resolver_pr0/new/execution.db` 是 `test_host_readonly_resolver_preserves_old_pool_and_enables_fresh_pool` 明确 `touch()` 的空文件；该测试预期 resolver 只读并返回 `None`，所以这是可解释的故意坏输入。它仍必须保留为 O4 diagnostic，不能删除或计 PASS。
- `OperationalError`：`test_actual_sibling_execution_db_is_inventoried_without_hiding_mission_damage` 先创建 SDK execution DB，随后故意执行 `CREATE TABLE missions(broken TEXT)`。该 DB 的真实表集合同时有 SDK 的 `runs/run_events/provider_invocations` 等表和这个错误 schema 的 `missions`，没有 `events`。plugin 的 inventory 特判要求“不含 missions/events”才按 SDK execution DB 盘点，因此被错误 schema 的 `missions` 吸入 Mission 分支，`SELECT DISTINCT mission_id FROM missions` 得到 `no such column: mission_id`，随后 `events` 查询得到 `no such table: events`。这是已解释的故意 schema-collision 反例；raw error 仍保留，不能删除或计 PASS。

## symlink 覆盖限制

444 条 `symlink_not_followed` 全部来自真实 `os.walk(..., followlinks=False)` 的目录/文件链接。定向检查结果：436 个目录 link 的 `realpath` 均出现在非 symlink walk 的 visited directory 集合；8 个文件 link 的 target parent 均已被访问；444 个 link target 均存在。结论是扫描覆盖成立，但仍保留每条 raw OPEN diagnostic，不能改为跟随链接，也不能把覆盖等同生产一致性。

本轮 discovery pass：`files_checked=3516`、`sqlite_files=901`。它证明声明 run root 内的当前可见文件被检查；不证明检查点之间已删除的文件，JSON 自身也明确该限制。

## 其他覆盖限制与最小下一步

- `memory` 数据库已关闭后无法从该次运行恢复；删除数据库、未声明 root、检查点间短命文件均属于 after-run 盲区。
- `execution_databases[*].execution_replay_verified` 仍为 `false`：SDK execution DB 被盘点不等于 Mission/event replay 已验证。
- `rawOPEN` 必须继续保留；本次没有创建假 PASS，也没有把所有负例全豁免。总体 gate 仍为 `OPEN`，当前未解释项为 #14；OperationalError 已定位为故意 schema-collision 反例。
- 最小下一步：主 sole runner 按原 selector 修复并重验 #14 的 Task status replay mismatch；不得先改 scanner 或删除 symlink 检查。

修改文件：仅更新本报告；无生产/plugin/tests 修改。固定测试 selector、预期和返工原因已在上文记录。返工原因：父审发现第 2 行 observation_id 的一位数字错误；本次从 raw JSON 程序取值纠正，移除末尾孤立反引号，并补充 v14 source head / 当前审查 checkout 分离说明及 OperationalError 的 schema/query 定位。


## Main repair follow-up — 2026-09-13 05:25 CST

Original observation #14 is preserved as a real historical FAIL. Production replay now derives TaskCommitted readiness from prior dependency completion. The original immutable database replays 43 events with coverage1.0, no unknown events/gaps/mismatches; receipt `.local-test-evidence/2026-09-12/p33-g/replay-v14-gap14-repaired.json`, SHA256 `acf7819fe5afa18d30ca4d14e3cc983fb37f372d9bf1518725eda25935a6b138`. New production-fragment oracle failed before the repair; fragment/search/step08 replay selectors passed42 tests/36.16s. Independent review found noP0/P1. This closes this observation, not the full all-run O4 audit.
