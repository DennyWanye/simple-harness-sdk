# LLM-native HTN 实施日志

工作目录：`simple-harness-sdk-h0`（分支 `h0-llm-native-baseline`）。  
对照计划：Host `simple_harness/plans/taskSys2/升级planV1/v1.4/simpleharness-llm-native-htn-execution-plan.zh-CN.md`（HTN-LLM-NATIVE-1.0）。

## 工作方式（全程）

- **实施与核验分属不同会话。** 本日志的实施会话只做交付；独立核验会话对照本目录 `reviews/` 归档报告，实施者不得自核自过。
- **测试先行。** 先红后绿；H0 无功能代码，回归以实测冻结数字为准。
- **变异。** H1 起每片至少一组针对性变异，KILLED 才算门过。
- **归档。** 审阅 / 核验报告写入本目录 `reviews/`，文件名含短 SHA 与日期。
- **不改范围外代码。** H0 尤其禁止改 `src/`、`tests/`、`contracts/`。
- **测试命令。** 一律 `PYTHONPATH=src uv run pytest …`；先确认 `agent_orchestrator.__file__` 指向本树。忽略 stop-hook 旧账本提示。

---

## H0 · 冻结当前基线（2026-09-18）

**会话角色：实施者。只记录，零功能 diff。**

### 基线身份

- HEAD `7f839f0e3d83aa17a0d0e2e54157ca9b1c9465a5`（`release: simple-harness-sdk 0.12.2`）
- tag `v0.12.2`
- 代码候选 `c0e13a4`（发布提交相对它只加文档）
- 包版本 `0.12.2`；`agent_orchestrator.__version__` 仍 `0.11.1`

### 实测（导入已指向本目录 `src/`）

| 套件 | 命令摘要 | 结果 | 用时 |
|---|---|---|---|
| full_target | `PYTHONPATH=src uv run pytest tests/orchestrator/full_target -q -p no:cacheprovider > /tmp/h0-full.txt` | **2960 passed / 2 skipped / 0 failed** | 132.30 s（封装 134 s） |
| 旧模式 step02/05/06/07/p34/p35 | 同上路径，输出 `/tmp/h0-legacy.txt` | **560 passed / 13 skipped / 0 failed** | 205.44 s（封装 208 s） |

与预期 2960/2、560/13/0 **一致**。2 skip 实测为 PANDA parser 缺席 + `--run-real-provider`，不是拆解里写的「两条都是 real_provider」。

### 其它钉子

- `_new_mode(mission)` **19**（`test_hierarchical_event_flow.py:1540`）
- 库 schema **18** / 迁移 18 `orchestrator-full-target-witness-subject`
- 包串 `planner-package-hierarchical-v4`；配对号 `3`；提案 schema `1`
- `SYSTEM_BOUND_FIELDS` 14 项与金表一致
- 提示词 sha 见 `H0-基线冻结-2026-09-18.md` §⑤ / `baseline.json`
- 14 局「不劣于」对照：c0e13a4 的 9 局 + f2dfa64 归档 5 局（审计包 `episodes.csv`）
- 已知问题 **N18**（不阻塞；归 H4）

### 未做

未改功能；未跑真实模型；未跑 grok CLI；未 push。H0 无独立核验代码 diff 可审，核验会话只需核对本目录三份文件与 `/tmp/h0-*.txt` 尾行。

### 下一步

**等待计划修订需求裁定。** Host `v1.4/LLM-native-HTN计划-修订需求-2026-09-18.zh-CN.md`：H1 代码须等「第一组」裁定后再开，避免线上格式返工。H0 不依赖任何裁定，本阶段到此结束。

---

## 2026-09-18 重排（H0 基线包对齐 V2 布局）

- 依据 `HTN-LLM-NATIVE-2.0`（`simpleharness-llm-native-htn-execution-plan-v2.zh-CN.md`）第 6 节与第 56 节重排 H0 交付物目录。
- 目录变更（`git mv`，未重跑任何测试）：
  - `plans/2026-09-18-llm-native-htn/H0-基线冻结-2026-09-18.md` → `plans/llm-native-htn/H0/baseline.md`
  - `plans/2026-09-18-llm-native-htn/baseline.json` → `plans/llm-native-htn/H0/baseline.json`
  - `plans/2026-09-18-llm-native-htn/journal.md` → `plans/llm-native-htn/journal.md`（全升级共用日志）
- 从 `baseline.json` 拆出：`test-results.json`（full_target / legacy / new_mode_sentinel / ruff）、`prompt-digests.json`（prompt_shas / package_versions / prompt_freeze_gaps）、`event-golden-digests.json`（legacy_event_golden / legacy_function_hashes）。
- 新增 `git-status.txt`：写入基线提交 `7f839f0` 完整哈希、移动前 `git status --short`（空）、`python3 --version`、`sqlite3 --version`。
- `baseline.md` 顶部新增「与 V2 计划的对应」小节；全部数字仍取自 2026-09-18 实测，本次未重跑。
- 提交信息：`docs(h0): realign the baseline package to the V2 plan layout`。未 push。
