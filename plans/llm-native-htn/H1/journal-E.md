# H1-E 实施日志：分层规划提示词第 8 版、版本配对与冻结指纹

**片名：** H1-E
**基线：** main `b13e757`
**依赖：** H1-A1（已合入 `contracts/planning_decisions.py` 协议核心）
**权威规格：** 《SimpleHarness LLM-Native HTN Core 代码级执行计划 V2》§9、§41、§13、§24–§30；《LLM-native HTN 计划 V2 裁定补遗》§七、§八
**实施者：** Codex（DeepSeek flash），全程走测试先行
**本片日志：** 仅本文件；其他片的 journal 未动

---

## 1. 目标与结论

本片交付三件事：

1. 新增层次模式 Planner 提示词 `planner-hierarchical-v8`，面向新协议
   `planning-decision-v1`：一轮只提出一个决定，只输出一个 `<planning_decision>` 块，
   覆盖 §41 的十二个必含要点。
2. 新增「新协议包版本 = 4」及可测的配对规则：`planner-hierarchical-v8` 只能配
   package 4，package 4 不能配 v7 及更早；**现有默认 `HIERARCHICAL_PLANNER_PACKAGE_VERSION`
   保持 3 不变**（缺省仍是旧协议）。1–3 的映射与全部旧模板文本逐字节不动。
3. 冻结指纹收口：登记 v8 的 sha256，并补齐 H0 记录的**三处缺口**
   （最早两版分层规划提示词、现行根评审提示词的冻结字面量），只登记现值、不改文本。

**结论：** 新测试与既有冻结测试全绿；`ruff` 无告警；工作树已提交且干净（见 §7）。

---

## 2. 测试先行（红 → 绿）

### 2.1 先写会失败的测试

新增 `tests/orchestrator/full_target/test_planning_decision_prompt_v8.py`（10 个用例），
覆盖：

- v8 已注册为独立分层 Planner 版本；
- v8 文本逐条包含 §41 十二要点的关键短语（`POINT_PHRASES` 表）；
- 示例 JSON 可解析、核心字段名集合与 §13 一致（信封类型尚未合入，故只校验结构与
  可被 `contracts.planning_decisions` 接受的那一个引用四元组）；
- package 4 是新常量、默认 package 仍为 3；
- 配对规则正反例（v8↔4 真；v7↔4 假；v8↔3 假），以及模式级集合等于各包并集；
- 全部既有提示词 sha256 与 **H0 `prompt-digests.json`** 中的现值逐一相等（从该文件读
  期望值，不预置副本）；
- H0 三处缺口已登记；
- v8 自身已入冻结表，且 `prompt-v8.md` 的提示词块与代码逐字一致。

模块级 import 只引用今天已存在的符号，新符号用属性读取，保证每条缺失各自失败、不
在收集期整体报错。

### 2.2 红

```
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/orchestrator/full_target/test_planning_decision_prompt_v8.py -q -p no:cacheprovider
8 failed, 1 passed in 0.55s
```

失败点分别是：v8 未注册、十二要点缺失、示例缺失、package 4 与配对函数不存在、
冻结表缺 v1/v2/v8 三项、根评审 v3 无字面量。

### 2.3 绿（本片文件）

```
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/orchestrator/full_target/test_planning_decision_prompt_v8.py \
  tests/orchestrator/full_target/test_output_port_claims.py \
  tests/orchestrator/full_target/test_root_review_user_goal.py -q -p no:cacheprovider
61 passed in 0.72s
```

---

## 3. 改动清单（只改白名单内文件）

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/agent_orchestrator/runtime/role_templates.py` | 改（纯追加） | 新增 `PLANNER_HIERARCHICAL_V8` 模板与版本常量；新增 `PLANNING_DECISION_PACKAGE_VERSION = 4`、`HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE[4]`、`hierarchical_planner_pairing_is_valid`；模式级集合加 v8；`__all__` 追加新名 |
| `tests/orchestrator/full_target/test_planning_decision_prompt_v8.py` | 新增 | 本片主测试（10 用例） |
| `plans/llm-native-htn/H1/prompt-v8.md` | 新增 | v8 提示词全文（从代码导出、逐字一致）与逐段设计说明 |
| `tests/orchestrator/full_target/test_output_port_claims.py` | 改（仅追加表项） | 冻结摘要表 `FROZEN_PROMPT_DIGESTS` 追加 `PLANNER_HIERARCHICAL_V1`、`PLANNER_HIERARCHICAL`、`PLANNER_HIERARCHICAL_V8` 三行 |
| `tests/orchestrator/full_target/test_root_review_user_goal.py` | 改（仅追加常量与断言） | 新增 `FROZEN_ROOT_REVIEWER_V3` 字面量并断言现行根评审提示词 sha256 等于它 |
| `plans/llm-native-htn/H1/journal-E.md` | 新增 | 本日志 |

**未改（明确）：** `planner_package.py`、`event_handler.py`、`contracts/`（含
`contracts/planning_decisions.py`）。旧模板文本、1–3 的包映射逐字节不动。

---

## 4. v8 提示词与 §41 十二要点对照

`planner-hierarchical-v8` 全文与逐段说明见 `plans/llm-native-htn/H1/prompt-v8.md`；
该文件中的 fenced 提示词块由代码导出，测试断言其与
`PLANNER_HIERARCHICAL_V8.instructions` 逐字相等。sha256（UTF-8）：
`dba73c4f583256ff36147d59a936aef48f3d1392797cebeba468142417e85ada`。

| §41 要点 | v8 中的落点（关键短语） |
|---|---|
| 1 只提出一个决定 | 「一轮回复里只提出一个决定」 |
| 2 只输出一个 `<planning_decision>` | 「只输出一个 `<planning_decision>`…`</planning_decision>` 块」 |
| 3 类型取 `enabled_decision_types` | 「`decision_type` 只能取请求包 `planning_protocol.enabled_decision_types`」 |
| 4 `subject_key` 照抄 | 「`subject_key` 照抄请求包里给你的 `subject_key`」 |
| 5 引用从 `visible_refs` 照抄四元组 | 「从请求包的 `visible_refs` 里完整照抄四元组」+ 逐字列出四字段 |
| 6 禁止系统字段并列名 | 「禁止系统字段」段逐个列出 §32 字段名 |
| 7 本阶段不能请求取证 | 「`REQUEST_EVIDENCE`…不在本阶段 `enabled_decision_types` 里」→ `DECLARE_BLOCKED`/`NO_CHANGE` |
| 8 被拒展开用一个 `REPAIR/REPLACE_METHOD` | 「用一个 `REPAIR` 决定表达修复，`payload.repair_kind = "REPLACE_METHOD"`」 |
| 9 共享用 `BIND_EXISTING_GOAL` | 「`BIND_EXISTING_GOAL`：把一个已有目标共享/复用…」 |
| 10 无可用方法 `DECLARE_BLOCKED` 交系统合成 | 「系统据此决定是否进入方法合成轮…你不需要也不能自己合成方法」 |
| 11 不输出内部思维链 | 「不要在回复里写出内部思维链（CoT）」 |
| 12 块外禁止文字 | 「块外不要输出任何文字」 |

最小合法 JSON 示例：一行完整 `REFINE`，字段与 §13 核心字段集合、§24 payload 一致；
`method_ref` 是合法四元组，由 `PlanningRefV1.from_json` 校验通过。

---

## 5. 配对与默认值

- `PLANNING_DECISION_PACKAGE_VERSION = 4`（新常量）。
- `HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE[4] = frozenset({"planner-hierarchical-v8"})`。
- `HIERARCHICAL_PLANNER_PACKAGE_VERSION = 3` **不变**；缺省 `hierarchical_planner_versions()`
  仍返回包 3 的 `{v5, v6, v7}`。
- `hierarchical_planner_pairing_is_valid(prompt_version, package_version)`：
  v8 只在包 4 为真；v7（及更早）在包 4 为假；v8 在包 3 为假。
- 模式级集合是各包并集，因此 v8 已加入 `HIERARCHICAL_PLANNER_VERSIONS` 且无孤儿。

---

## 6. 冻结指纹与 H0 三处缺口

H0 `plans/llm-native-htn/H0/prompt-digests.json` 的 `prompt_freeze_gaps` 记了三处缺口，
本片在**同一片**补齐（补遗 §七.4）：

| 缺口 | 处理 |
|---|---|
| `planner-hierarchical-v1 not in FROZEN_PROMPT_DIGESTS` | 在 `FROZEN_PROMPT_DIGESTS` 追加 `PLANNER_HIERARCHICAL_V1`，值取 H0 `prompt_shas` 现值 |
| `planner-hierarchical-v2 not in FROZEN_PROMPT_DIGESTS` | 追加 `PLANNER_HIERARCHICAL`（版本 `planner-hierarchical-v2`），值取 H0 现值 |
| `root-reviewer-v3 has no FROZEN_* literal` | 在 `test_root_review_user_goal.py` 追加 `FROZEN_ROOT_REVIEWER_V3` 字面量并加断言 |

另外登记 v8 自身：`FROZEN_PROMPT_DIGESTS["PLANNER_HIERARCHICAL_V8"]`。以上**只登记现值、
不改任何提示词文本**；测试从 H0 文件读期望值，逐一比对 H0 记录的全部 29 个注册版本。

---

## 7. 全量验证与提交

全量回归（本片改动后）：

```
PYTHONPATH=src .venv/bin/python -m pytest tests/orchestrator/full_target -q -p no:cacheprovider
3024 passed, 2 skipped in 133.36s
```

（2 个 skip：`test_panda_backend.py` 未配置 `SH_PANDA_PARSER`；
`test_real_provider_hierarchical_smoke.py` 需 `--run-real-provider`。均与本片无关。）

`ruff`：

```
.venv/bin/ruff check <本片改动文件>
All checks passed!
```

git 提交与工作树见文末「提交」小节（提交后由核验会话复核）。

---

## 8. 边界与未做

- 未改 `planner_package.py`、`event_handler.py`、`contracts/`：包 4 的字段与字符串标签
  （`planner-package-hierarchical-v5`）是 H1-D 的范围；本片只落整数版本常量与配对。
- 未改任何旧模板文本；未改 1–3 的包映射。
- 信封类型（`PlanningDecisionEnvelopeV1`）尚未合入，故示例 JSON 只做结构与单引用校验，
  未做信封级校验——这与任务书一致。
- 未跑真实模型、未读密钥、未 push。
- journal 只写本片文件 `plans/llm-native-htn/H1/journal-E.md`。

## 提交

提交信息（`git log --oneline -1` 可核对）：

```
feat(h1-e): planner-hierarchical-v8 prompt, package-4 pairing and frozen digests
```
