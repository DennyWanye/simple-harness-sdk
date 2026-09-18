# H0 基线冻结（LLM-native HTN）

## 与 V2 计划的对应

本节说明本基线包已按 `HTN-LLM-NATIVE-2.0`（`simpleharness-llm-native-htn-execution-plan-v2.zh-CN.md`）第 6 节（交付物布局）与第 56 节（H0 基线冻结）重排：

- 目录已重排为 `plans/llm-native-htn/H0/`，共用日志移至 `plans/llm-native-htn/journal.md`。
- 基线正文 `baseline.md`；环境快照 `git-status.txt`；测试结果 `test-results.json`；提示词摘要 `prompt-digests.json`；事件/函数金值 `event-golden-digests.json`。
- 文中全部数字取自 **2026-09-18 实测**，本次重排**未重跑**任何测试或命令，仅移动/拆分文件并补充本说明。

---

**日期：2026-09-18**  
**性质：只记录，不改功能代码。**  
**工作树：`simple-harness-sdk-h0` 分支 `h0-llm-native-baseline`**  
**依据：** Host `plans/taskSys2/升级planV1/v1.4/simpleharness-llm-native-htn-execution-plan.zh-CN.md` §68 H0、§69；实施拆解 `H1-PlanningDecision协议-实施拆解-2026-09-18.zh-CN.md` §1。数字一律以本会话实测为准。

导入核对（测前）：

```text
PYTHONPATH=src uv run python -c "import agent_orchestrator; print(agent_orchestrator.__file__)"
→ /Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk-h0/src/agent_orchestrator/__init__.py
```

未改 `src/`、`tests/`、`contracts/`；未读密钥；未跑 grok CLI；未碰 `.local-test-evidence` 下 `runs*`。

---

## ① HEAD、tag、版本号

| 项 | 现值 |
|---|---|
| HEAD | `7f839f0e3d83aa17a0d0e2e54157ca9b1c9465a5` |
| 短 SHA | `7f839f0` |
| 提交说明 | `release: simple-harness-sdk 0.12.2` |
| 父提交（代码候选第 6 版） | `c0e13a4927717abfa5281e78d6fdca1ee32d732c`（`7f839f0` 相对它只加文档：`CHANGELOG.md`、`docs/release/v0.12.1.md`、`docs/release/v0.12.2.md`） |
| 分支 | `h0-llm-native-baseline` |
| 工作树（动工前） | 干净 |
| annotated tag | `v0.12.2`（`git tag --points-at HEAD`） |
| 包版本 | `src/simple_harness/version.py` → `0.12.2` |
| 编排步骤号 | `src/agent_orchestrator/version.py` → `0.11.1`（ORCH-BUILD 轴，与包版本独立） |

---

## ② 数据库 schema 版本与迁移号

从源码读出（`agent_orchestrator.storage.schema`）：

| 项 | 位置 | 现值 |
|---|---|---|
| `SCHEMA_VERSION` | `src/agent_orchestrator/storage/schema.py:558` | `18`（`MIGRATIONS[-1].version`） |
| `SCHEMA_NAME` | 同文件 `:559` | `orchestrator-full-target-witness-subject` |
| 迁移表 | 同文件 `:538-557` | 1…18，末项 `Migration(18, "orchestrator-full-target-witness-subject", DDL_V18)` |
| 迁移 16 DDL | `storage/htn_schema.py`（`schema.py:22` 导入为 `DDL_V16`） | HTN 表 |
| 迁移 17 DDL | `storage/acceptance_receipt_schema.py`（`:20`） | 验收收据 |
| 迁移 18 DDL | `storage/validity_subject_schema.py`（`:23`） | 有效性见证主体 |

合同侧 schema 号（非库迁移，一并钉住）：

| 常量 | 位置 | 现值 |
|---|---|---|
| `PLAN_REVISION_PROPOSAL_SCHEMA_VERSION` | `src/agent_orchestrator/contracts/htn.py:63` | `1`（`plan-revision-proposal-v1`） |
| `METHOD_CONTRACT_SCHEMA_VERSION` | 同文件 `:62` | `1` |
| `EXECUTION_FEEDBACK_SCHEMA_VERSION` | 同文件 `:64` | `1` |

编排语义：`src/agent_orchestrator/orchestrator/plan_commits.py:114-122` — `legacy` / `hierarchical`，别名 `full-target-v1`。默认仍是 `legacy`。新协议 `planning-decision-v1` **不存在**（H1 才加）。

---

## ③ 实测测试

测前已确认 `agent_orchestrator.__file__` 指向本目录 `src/`。

### 3.1 full_target 全量

命令：

```bash
env PYTHONPATH=src uv run pytest tests/orchestrator/full_target -q -p no:cacheprovider > /tmp/h0-full.txt
```

| 项 | 实测 |
|---|---|
| 结果 | **2960 passed, 2 skipped, 0 failed** |
| 墙钟 | 会话封装 **134 s**；pytest 尾行 **132.30 s (0:02:12)** |
| 退出码 | 0 |
| 日志 | `/tmp/h0-full.txt` |

尾行：

```text
2960 passed, 2 skipped in 132.30s (0:02:12)
```

2 skip（实测，与 H1 拆解「整文件 real_provider」不完全相同，以本机为准）：

1. `tests/orchestrator/full_target/test_panda_backend.py:595` — `no real pandaPIparser configured via SH_PANDA_PARSER`
2. `tests/orchestrator/full_target/test_real_provider_hierarchical_smoke.py` — `needs --run-real-provider`（文件 `pytestmark = pytest.mark.real_provider`，`:74`）

与预期 **2960 / 2** 一致。

### 3.2 旧模式 step02 / 05 / 06 / 07 / p34 / p35

命令：

```bash
env PYTHONPATH=src uv run pytest \
  tests/orchestrator/step02 tests/orchestrator/step05 \
  tests/orchestrator/step06 tests/orchestrator/step07 \
  tests/orchestrator/p34 tests/orchestrator/p35 \
  -q -p no:cacheprovider > /tmp/h0-legacy.txt
```

| 项 | 实测 |
|---|---|
| 结果 | **560 passed, 13 skipped, 0 failed** |
| 墙钟 | 会话封装 **208 s**；pytest 尾行 **205.44 s (0:03:25)** |
| 退出码 | 0 |
| 日志 | `/tmp/h0-legacy.txt` |

尾行：

```text
560 passed, 13 skipped in 205.44s (0:03:25)
```

13 skip 全是既有 real-provider / pinned tokenizer，零新增失败。与预期 **560 / 13 / 0** 一致。

---

## ④ `_new_mode` 哨兵

| 项 | 现值 |
|---|---|
| 计数 | **19**（`inspect.getsource(event_handler).count("self._new_mode(mission)")`） |
| 哨兵测试 | `tests/orchestrator/full_target/test_hierarchical_event_flow.py:1540` `assert source.count("self._new_mode(mission)") == 19` |
| 说明注释 | 同文件 `:1459-1531`（第 19 处是 P2.3k `_evaluate_criteria`） |

与预期 19 一致。

---

## ⑤ 冻结提示词与包版本

主表：`tests/orchestrator/full_target/test_output_port_claims.py:193-288` `FROZEN_PROMPT_DIGESTS`；守卫 `:317` `test_a_shipped_prompt_keeps_its_bytes`、`:338` `test_the_frozen_digests_cover_the_prompts_this_slice_depends_on`。  
域模块注册：同文件 `:294-299` `FROZEN_REGISTERED_DIGESTS`。  
DAG Planner 另钉：`tests/orchestrator/full_target/test_planner_typed_proposal.py:512-515` `FROZEN_PLANNER_PROMPTS`。

sha256 由本机对 `TEMPLATE_VERSIONS[role][version].instructions` 重算，与冻结表逐字一致。

### 5.1 Planner

| 版本 | sha256 | 模板位置 | 是否在 `FROZEN_PROMPT_DIGESTS` | 包配对 |
|---|---|---|---|---|
| `planner-v3` | `353b0dd0a0727b4a8ce74fe82354d17a02b6afe0a6c883f011b77ee22c4287e9` | `runtime/role_templates.py:56` `PLANNER_V3` | 否（在 `FROZEN_PLANNER_PROMPTS`） | DAG |
| `planner-v4` | `13537f0abf6322c7075af9b5ddb3c0b7316c0271830311f49c3d6c195f5c9aad` | `:148` `PLANNER`；版本串 `:30` | 是（键 `PLANNER`） | DAG |
| `planner-hierarchical-v1` | `2acb2294fca09f55c30831ffd43dd7eae5685af72daa4c85de8759b440e43830` | `:80-90` | **否（H0 缺口）** | 包配对号 1 |
| `planner-hierarchical-v2` | `f8a8bba7221bfcc1c33d8b3f71517678bced6c905c7dbfdcee1558f0374a0387` | `:81` 版本、`:511` `PLANNER_HIERARCHICAL` | **否（H0 缺口）** | 包配对号 1 |
| `planner-hierarchical-v3` | `ba244a12bf504051d7ebc954f462cf23f9c7734dff980c539187834a0a670acb` | `:82` / `:551` | 是 | 2 |
| `planner-hierarchical-v4` | `5ae3b39acf9326888e20bab934848ed6e1d21482884ccda932ac693b31122223` | `:581-582` | 是 | 2 |
| `planner-hierarchical-v5` | `2517d5fe727ba27ffffa105d72e786e72109893c342e56adebeaa033794f7608` | `:607-608` | 是 | 3 |
| `planner-hierarchical-v6` | `b13d16f7d1d5aaa8919d983e93b7639105446bd6d95bd73d05353571a9a185a6` | `:635-636` | 是 | 3 |
| `planner-hierarchical-v7` | `5b87b9624fbf4a4e1c31e6d9c4a765de2ac4ec689b5d708000b427676cb50f15` | `:655-656` | 是 | 3（**当前默认**） |

默认选择器：`orchestrator/event_handler.py:3087-3126` `_hierarchical_planner_template` → 未 pin 时 `PLANNER_HIERARCHICAL_V7`。

### 5.2 Method synthesizer

| 版本 | sha256 | 模板位置 |
|---|---|---|
| `method-synthesizer-v1` | `9341ab10390015fb45d528d95dac0af5b29658f0ee9b70060369d607f8f0ae32` | `role_templates.py:909-917` |
| `method-synthesizer-v2` | `27ccb23492ef00b73404735a03f51439d2bf0eb1339a6a6b4320950ee8beaccb` | `:961-978` |
| `method-synthesizer-v3` | `a38309fdb328c929d6ddefa37ff4de294628f0f508dfad82c7a27bb1cd0e6c9c` | `:1059-1072` |
| `method-synthesizer-v4` | `8d457abe7a74d614642ac7e2446e656aea9c46e4a7f509820353dd04f93f39b6` | `:1107-1108` |
| `method-synthesizer-v5` | `6973e125b9cc3b02b8af77190a9b0ff4b5a1ddd3cdfe8cc1f60900304fe21e6b` | `:1137-1138` |
| `method-synthesizer-v6` | `75a8a4a1a888e2ac165d16a711df9e981ad44da92c15b652bbc29bbc97774c42` | `:1159-1160` |
| `method-synthesizer-v7` | `4aa25e682ede38479a09a2a8d00da023aaed617e32384e7da85c44641a4e2f9b` | `:1181-1182`（**当前默认**） |

均在 `FROZEN_PROMPT_DIGESTS`。

### 5.3 Worker

| 版本 | sha256 | 模板位置 | 冻结表 |
|---|---|---|---|
| `worker-v2` | `c0c35d2d2639ea6655c66bf7b30b6f46cf04ffbb458a79caba63b6477ae46f37` | `role_templates.py:161` | `FROZEN_PROMPT_DIGESTS` |
| `worker-v3` | `c587ce55ff9a01e38ba5b362f8bb9de518b99404f712e63409f871d2d3f0d285` | `:31` / `:204` | 同上 |
| `worker-hierarchical-v1` | `e82e74aff9b37d4746da0e982b38855e3cb639efe848fb1a15116a18023e7de2` | `:751-763` | 同上 |
| `worker-hierarchical-v2` | `120372b8a49162ab1d96c6cf2725d6fcf7adec1988c7c8646f378c21649870b7` | `:795-796` | 同上 |
| `worker-hierarchical-v3` | `ed827cf56debe82de0fdf5604ce8ba570b84a3500ecc63beba89e4ba01efc19d` | `:808-809` | 同上 |
| `worker-hierarchical-v4` | `d59d78049330d71d8a837f709b7b72003275be3999e36f8c3b20ce2ea6d61c52` | `:825-826`（**当前默认**） | 同上 |
| `worker-appworld-v3` | `8fbea8282c1e8f4814e75fc9943bc41da21016db0af2026fe037f001b81bdd88` | `runtime/appworld_templates.py:8` | `FROZEN_REGISTERED_DIGESTS` |
| `worker-appworld-hierarchical-v1` | `9ad842af04458dd7f57d1935fb669a57fdb4b63d850427b5974716e3da994917` | 同文件 `:9` | 同上 |

### 5.4 Root reviewer

| 版本 | sha256 | 模板位置 | 冻结常量 |
|---|---|---|---|
| `root-reviewer-v1` | `2b5ebe37c1701f60e955a0e71d1d682fc344f2ccf6d06ed12606e2df9b9aa59a` | `role_templates.py:1196-1205` | `test_root_review_evidence.py:91` `FROZEN_ROOT_REVIEWER_V1` |
| `root-reviewer-v2` | `75debfd9640f1c635b808cdaf7657168ce10782d9e0cd93f08450fdc0d744c76` | `:1250-1251` | `test_root_review_user_goal.py:51` `FROZEN_ROOT_REVIEWER_V2` |
| `root-reviewer-v3` | `21a7814076b72957f41c47bf21fb340d2c2243e3fc0687fb397a143373930980` | `:1306-1307`（**当前默认**） | **无独立 `FROZEN_*` 字面量**（H0 缺口：测试只钉 v2 字节 + v3 含「以 mission_goal 为准」） |

### 5.5 其它已钉（DAG Critic）

| 版本 | sha256 | 位置 |
|---|---|---|
| `critic-v2` | `8eb51a32c06bfa16da88e4e89a28f48b50ce2e06969aec467abd803078a1c5ce` | `role_templates.py:213` |
| `critic-v3` | `427fb096fc0c4cf6acc67358cd631d3f3c4c39ce2fea60b768a529f6290b7120` | `:32` / `:230` |

### 5.6 包版本串

| 常量 | 位置 | 现值 |
|---|---|---|
| `HIERARCHICAL_PACKAGE_VERSION` | `planning/htn/planner_package.py:67` | `"planner-package-hierarchical-v4"` |
| `HIERARCHICAL_PLANNER_PACKAGE_VERSION` | `runtime/role_templates.py:715` | `3`（v5/v6/v7） |
| `HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE` | 同文件 `:719-740` | `1→{v1,v2}`；`2→{v3,v4}`；`3→{v5,v6,v7}` |

---

## ⑥ legacy 事件 golden 与旧函数 hash

| 项 | 位置 | 现值 |
|---|---|---|
| `NEW_EVENT_TYPES` | `tests/orchestrator/full_target/test_hierarchical_event_flow.py:1392-1421` | 20 个分层事件名的 frozenset |
| 旧路径不得产生新事件 | 同文件 `:1425` `test_the_legacy_run_appends_none_of_the_new_event_types` | 断言 `kinds.isdisjoint(NEW_EVENT_TYPES)` |
| 旧路径不进装配 | 同文件 `:1357` `test_the_legacy_path_never_enters_the_assembly_at_all` | 参数化 |
| `LEGACY_FRONTIER_SHA256` | `tests/orchestrator/full_target/test_allocator_form_gate.py:113` | `0ae7cd4c24ee902e1fac2f8e8193920a64e9ff10c408baf0f33d1d6cc366e1b8` |
| `LEGACY_ALLOCATE_SHA256` | 同文件 `:114` | `5940ab39e18168a83f9af8c422a9924758faf41ddf8793021b5a89c6cb5ab78c` |
| 守卫 | `:478` `test_the_legacy_frontier_source_is_byte_for_byte_unchanged`；`:483` `test_the_legacy_allocate_source_is_byte_for_byte_unchanged` | 对 `inspect.getsource(frontier/allocate)` 钉字节 |

---

## ⑦ `SYSTEM_BOUND_FIELDS`（14 项）

源：`src/agent_orchestrator/planning/planner.py:33-50`。  
金表：`tests/orchestrator/full_target/test_planner_typed_proposal.py:279-296` `GOLDEN_BOUND_FIELDS`；守卫 `:299` `test_the_bound_field_set_is_exactly_the_golden_set`（`len == 14`）。

现值（排序后与金表相同）：

```text
authored_by
authorization_ref
budget_account
budget_grant_revision
grant_ref
manager_epoch
mission_id
opened_by
principal
principal_id
provenance
registry_status
scope
scope_id
```

---

## ⑧ 真实模型场景清单（0.12.2 验收口径 14 局）

**以后各阶段「不劣于」对照。** 来源：Host 审计包 `plans/taskSys2/升级planV1/audit-2026-09-18/episodes.csv`；成绩释义对照 Host `impl/Grok验收-第6批诊断-2026-09-18.zh-CN.md` §0。  
口径：第 6 批 `c0e13a4` 的 9 局 + 第 5 批 `f2dfa64` 归档未重跑的 5 局。**不含** `superseded-batch5-f2dfa64/`。模型 grok-4.6 / medium。

汇总：COMPLETED **12/14**；官方通过（`valid_success`）**10/14**；编排假完成 **0**；隐藏测试未覆盖 raw `false_completion` **2**（C1×2）；`budget_conserved=true` **14/14**；`usage_fully_known=true`、`unknown_usage_calls=0` **14/14**。

| 局 | sdk_commit | mission_status | stop_reason | hidden | public | declared_complete | false_completion | valid_success | attempts | tokens | 秒 | 归类 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| H-L3-C1-r0 | c0e13a4 | COMPLETED | verification_passed | FAIL | PASS | true | true | false | 6 | 249307 | 1122.18 | 隐藏测试未覆盖（非编排假完成） |
| H-L3-C1-r1 | c0e13a4 | COMPLETED | verification_passed | FAIL | PASS | true | true | false | 6 | 292723 | 909.93 | 同上 |
| H-L3-C2-r0 | c0e13a4 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 6 | 222954 | 590.08 | 真完成 |
| H-L3-C2-r1 | c0e13a4 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 6 | 237284 | 767.55 | 真完成 |
| H-L3-C4-r0 | c0e13a4 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 7 | 252845 | 728.85 | 真完成 |
| H-L3-C4-r1 | c0e13a4 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 7 | 250006 | 794.88 | 真完成 |
| H-L4-M2-r1 | c0e13a4 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 11 | 473548 | 1782.51 | 真完成；N17 已闭合 |
| H-L4-M3-r0 | c0e13a4 | FAILED | planning_failed | FAIL | FAIL | false | false | false | 4 | 244733 | 728.57 | 题目设计 + P2.3v 早停具名 |
| H-L4-M3-r1 | c0e13a4 | FAILED | planning_failed | FAIL | FAIL | false | false | false | 4 | 239630 | 679.23 | 同上 |
| H-L3-C3-r0 | **f2dfa64** 归档 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 4 | 118806 | 320.67 | 真完成（未在第 6 版重跑） |
| H-L3-C3-r1 | **f2dfa64** 归档 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 4 | 116674 | 271.22 | 同上 |
| H-L4-M1-r0 | **f2dfa64** 归档 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 4 | 118887 | 282.76 | 同上 |
| H-L4-M1-r1 | **f2dfa64** 归档 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 4 | 116602 | 240.49 | 同上 |
| H-L4-M2-r0 | **f2dfa64** 归档 | COMPLETED | verification_passed | PASS | PASS | true | false | true | 4 | 173220 | 493.92 | 同上 |

CSV 行：`runs/h-arm` 107–115 行（c0e13a4）；`runs-f2dfa64-batch5` 86–90 行（归档 5 局）。`result_json_sha256` 见该 CSV，本冻结不复制 runs 目录。

题单身份（拆解 §1.5，H 臂冻结题集）仍有效，本表只钉 **0.12.2 验收 14 局成绩**，不是整份 L1–L4 题单的跑分。

---

## ⑨ ruff 状态

历史编排门（与既往 journal 同口径）：

```bash
uv run --frozen --group dev ruff check src/agent_orchestrator tests/orchestrator/full_target
→ All checks passed!
```

全树（本会话另记，**非本阶段引入**）：

```bash
uv run --frozen --group dev ruff check src tests
→ Found 119 errors.（14 条 `--fix` 可修；含既有 E501 / I001 等）
```

H0 不修 ruff。后续阶段仍以编排门「`src/agent_orchestrator` + `tests/orchestrator/full_target` 全清」为回归门槛。

---

## ⑩ 已知问题

引用 Host `plans/taskSys2/升级planV1/impl/Grok验收-第6批诊断-2026-09-18.zh-CN.md` §9 / §12。

| 编号 | 级别 | 阻塞发布？ | 摘要 |
|---|---|---|---|
| **N18** | P2 | 否 | P2.3v 早停后修复轮提案无法 merge：M3 两局 N=3 后正确 `PlanningRejected{repeated_verification_failure}`，随后 `proposal_not_grounded`（assess 实例仍 ADOPTED、compound 任务不在当前网）。建议 N=3 时 RETIRE 所属 method instance 并 reconcile。不修则 M3 仍按构造 FAILED，停机已具名。应并入 H4。 |
| N16 | 观察 | 否 | 本 9 局未出现；5xx 短退避仍是可选增强。 |
| C1 hidden 未对齐 | 题目 | 否 | C1×2 `false_completion=true` 判定为隐藏测试未覆盖（B 为主 C 为辅），**不是编排假完成**。 |
| M3 构造 | 题目 | 否 | `root_requirements=null` + `broken.py`；不进 COMPLETED 地板。 |

已闭合、不再列为新缺陷：N13 / N14 / N4 / N17（第 6 批真实局证据）。

H0 记录缺口（纯测试钉，不算功能债）：`planner-hierarchical-v1/v2` 未进 `FROZEN_PROMPT_DIGESTS`；`root-reviewer-v3` 无独立冻结字面量。

---

## 本阶段交付（对照计划 §69）

H0 不改代码。交付：本文件、`journal.md`、`baseline.json`。独立核验属另一会话。
