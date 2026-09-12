# P3.3 非代码 Mission 与证据闭环 · 记录

## 0. 交接（冷会话先读这一节）

- **当前位置**：计划第 3 版已定稿（两轮各两位独立评审，四份原文在 `reports/`，处置表在 `plan.md` §7）。**切片 A 实施中**。
- **中心断言**：文档领域的 VERIFIED 只意味着「这份文件的这个版本的这几行里，逐字写着这句话」，且要在记录层 / 消费层 / 交付层三层同时成立。详见 `plan.md` §0。
- **切片顺序**：A 领域画像与五处闸门 → B 来源与证据解析 → C 评估记录与分级 → D adapter 与证据不足出口 → E 冲突与失效 → F 回归与 wheel → G Host 与原生验收。
- **每切片完成即跑 `tests/orchestrator` 全量**（不等切片 F），并同步更新本文件。
- 真实模型只用 `deepseek-flash`；记录与回复一律中文。

## 1. 计划评审

### 1.1 第 1 轮（A 威胁模型 READY_WITH_CHANGES / B 代码一致性 NOT_READY）

共 10 条 P0、18 条 P1、9 条 P2，全部采纳或给出裁决理由，处置表见 `plan.md` §7。最关键的三条：

1. **来源字节没有权威副本**（A P0-2）：验证副本写入顺序是 seed → inputs → **artifacts** → protected，而 `_protected_seed`（`event_handler.py:2407`）只保护 `tests/` 与 `pytest:` 目标——Worker 把来源改写后登记成 artifact，就覆盖了验证副本里的来源。第 1 版只靠"重算 hash 等于登记表版本"兜底，而那条检查是当作 A08 的新鲜度检查写的，没意识到它实际承担防篡改职责。
2. **引文"区间内字面包含"能剥离否定词**（A P0-3）：来源写「我们**不**建议采用方案 A」，引 `"建议采用方案 A"` 字面成立。
3. **系统模板硬编码 pytest**（B P0-3）：`conflict_task` 的准则、`CONFLICT_POLICY`、synthesis 默认政策全都写死了 pytest / `code_test`。文档领域只设下限挡不住——系统会自己建出被自己闸门拒掉、或者永远跑不完的任务。

唯一不采纳的是 A P1-5（按 claim 类型分叉判冲突），取 B P0-1 的更严方案：范围只加注不豁免。理由是 A 的分叉在 attribution 上放行，而 `checked_scope` 缺省未定仍会静默关掉 code 领域的冲突检测。

### 1.2 第 2 轮（两位均 READY_WITH_CHANGES）

两位独立指出同一个中心 P0：**第 2 版的断言为假**——被约束的只有 `content`，而 `key` / `stance` / `supersedes` / `contradicts` 仍是模型自由填的，且检索、冲突判定、上下文装配**全都按 `key` 工作**（`commit_service.py:1585`、`conflicts.py:51`、`retrieval.py:172`）。另外三条：

- INCONCLUSIVE 在"能否被接受"的语义下比 FAIL **宽松**，所以"只能下调等级"的约束写错了层次，必须写在**层状态**上；且"来源与主张矛盾 → FAIL"不能由模型 adapter 判。
- 报告正文与 claim 之间没有任何绑定 → 结论区改由系统按 claim 渲染。
- 仲裁这条路上还有**四处** pytest 硬编码：`_open_conflict` 的部署闸门（`commit_service.py:1735`，纯文档部署里每个冲突都 DEFERRED）、`check_arbitration` 的目录约束、Arbiter 角色模板、`context_builder` 文案。

三份评审给的代码依据我都逐条核过，**全部属实**。

## 2. 实施

### 2.1 切片 A：领域画像与五处闸门

新增 `governance/domains.py`（**不叫 `profiles`**——该词在本仓库已指运行时模型画像）：

- `DomainProfileV1` 能**替换**而不只设下限：`default_policy` / `conflict_template` / `synthesis_default_policy` / `runs_layers` / `external_check`。
- `runs_layers` 是**显式声明**而不是从 `default_policy` 推断出来的。第一版实现用推断，会顺带拒掉 `code-v1` 今天能通过的 `formal_check`——那会破坏 A07（未部署的 formal 层应当在既有的 `deployed_layers` 闸门被拒、并在 router 里 ERROR，而不是被领域闸门多拒一次）。`test_p33_a13` 是这条的钉子。
- 注册两份：`code-v1`（逐字复刻今天的常量，含 `SYSTEM_DEFAULT_POLICY` 与 `CONFLICT_POLICY`）与 `doc-research-v1`。`LEGACY_DOMAIN` 就是 `code-v1` 本身，不另设第二个 id。
- `MissionSpec.domain` 在 `to_json()` 里**只在非默认时出现**，否则升级后 Host 重发同一个请求会因为 `spec_hash` 变化而拿到 `MissionConflict`（`test_p33_a16` 钉住）。

五处闸门（第 2 轮评审纠正了第 1 版说的"三处"）：

| # | 位置 | 覆盖 |
|---|---|---|
| 1 | `graph/task_graph.py::validate_graph` | 整图提案 |
| 2 | `graph/changes.py::validate_change` | 图变更 |
| 3 | `commit_service.py::_check_task_proposal` | 单 Task 提案 / Manager `add_task` |
| 4 | `commit_service.py` 冲突模板的 `insert_task` | 系统模板 · 冲突 |
| 5 | `commit_service.py` 综合模板的 `insert_task` | 系统模板 · 综合 |

4 与 5 走新的 `_check_system_template()`：系统自己写的任务和模型提的任务过同一个检查——一个领域会拒绝的模板是**这里的 bug**，不该等到四个切片之后变成"一个永远完不成的任务"才被发现。

仲裁路径上的两处硬编码也在本切片前移处理（第 2 轮 B P2-4：原计划放切片 E，中间四个切片里文档冲突任务必 FAIL）：

- `check_arbitration` 增加 `domain` 参数。code 领域判据一字不变；`decides_with == "human_review"` 的领域不再要求引一条它本来就不允许出现的 `pytest:` 证据。
- `_open_conflict` 的部署闸门从写死 `code_test` 改为按画像的 `decides_with` 判断。

schema 迁移**追加式**推进，一个切片一版：v8 `mission_domains`（本切片）、v9 `sources`、v10 `criterion_assessments`。

回放（D9）：`MissionCreated` payload 带 `domain_id`，`FORMAL_FIELDS["mission"]` 与 `OPTIONAL_FIELDS` 各加一项，`formal_from_snapshot` 与 `Store.snapshot` 同步补 `mission_domain`（照 `actions`/`approvals` 用 `has_table()` 守卫，否则旧库读新代码会抛）。

## 3. 回归

（逐切片记录）

## 4. 真实与原生

（待填）

## 5. 遗留

计划里已登记 F-P33-1..4，见 `acceptance.md` 退出门槛一节。
