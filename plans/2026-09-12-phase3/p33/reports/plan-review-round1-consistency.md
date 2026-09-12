# P3.3 计划评审 · 第 1 轮 · 评审者 B（与既有代码、既有不变量的一致性）

- 日期：2026-09-12
- 结论：**NOT_READY**
- 一句话：方向、威胁模型、诚实边界三块是对的，但有 5 处会在实现第一天就撞墙——一条与 Phase3 原文相反，一条让新领域的系统模板必然失败，一条把等级决定权交回给模型，一条自己推翻了「全量回归不变」的承诺，回放那一块整段缺失。

## P0

### P0-1 D6「范围无交集 → 不是冲突」与 §5.4 原文相反，且会静默关掉 code 领域的冲突检测

§5.4 原话：「旧资料与新资料冲突：保留双方、记录版本/时间/条件**并进入 Conflict Task**」。计划 D6 把这个被点名要进 Conflict Task 的场景排除掉了，acceptance P33-16 还把它钉成断言。

第二层更危险：今天判定只比 key + stance（`conflicts.py:51`），命中即 DISPUTED、不投影（`commit_service.py:1556-1562`）。code-v1 的 claim 根本没有 `checked_scope`；若「有交集」按字面实现（两个空集不相交），**所有旧 Mission 的冲突检测静默失效**——第 4 步最核心的不变量被一个「加个范围比较」的改动关掉。

建议：范围比较只用来给冲突**加注**，不用来**豁免**；同 key 反 stance 一律 DISPUTED + Conflict Task。「二者适用范围不同、各自成立」必须是 **Conflict Task 的一种裁决结果**，不是 accept 时的预判——裁决权在仲裁流程，不在 `find_contradiction` 的一个布尔表达式。并写死缺省：**`checked_scope` 缺失 = 全域**。

### P0-2 用模型自己写的 `type` 决定等级 = 原则三被绕过

`ClaimProposal.type` 是自由文本无枚举（`models.py:574,592`），今天完全不参与分级（`claims.py:142-187` 一次没读它，`commit_service.py:1582` 只是原样抄进 KnowledgeRecord）。计划 D3 把它作为通往 VERIFIED 那一行的前置条件——模型只要改一个字符串就能洗白，比 pytest 那条路更好走。

建议（三选一，推荐第 2）：① 等级完全不看 type，由 adapter 判「content 规范化后字面落在引文区间内」决定；② 保留 type 但只允许它**降低**上限（与 `verification_policy` 不得低于 floor 同构），不在枚举内按最严的 statement 处理；③ attribution 上限也只到 SUPPORTED。另外 type 要从自由文本收成枚举，否则 `attribution `/`Attribution`/`attribution\n` 各写一套。

### P0-3 doc 领域禁 pytest，但系统模板硬编码 pytest —— 综合、冲突、无政策任务全部必然失败

| 位置 | 内容 |
|---|---|
| `planning/manager.py:68` | `conflict_task` 的 `success_criteria=(f"arbitration:{key}", f"pytest:{directory}/test_probe.py")` |
| `planning/manager.py:30` | `CONFLICT_POLICY` 含 `code_test` |
| `deterministic_checks.py:88-92` | `check_arbitration` 要求仲裁 claim 必须引 `pytest:` 证据 |
| `models.py:47` / `manager.py:96` | `SYSTEM_DEFAULT_POLICY` 与 synthesis 默认政策都含 `code_test` |

而 `code_test` 无目标时跑整棵树（`deterministic_checks.py:192-193`），文档 Mission 里没有测试 → pytest exit 5 → `passed` False（`tool_gateway.py:97-100`）→ FAIL。

后果：文档 Mission 触发冲突 → 系统自建带 `pytest:` 的 Conflict Task → 要么被自己的闸门拒，要么建出来**永远无法完成**（`check_arbitration` 要 pytest，领域又禁 pytest），跑满 `CONFLICT_MAX_ATTEMPTS=2` 后冲突 UNRESOLVED。A05 在文档领域是死胡同。synthesis 与 Manager `add_task` 同理必 FAIL。

计划 D1 只写了「不得**低于** floor」，这是下限，挡不住「多了一个跑不了的层」。

建议：`DomainProfileV1` 必须能**替换**而不只设下限——`default_policy`、`conflict_template`/`synthesis_template`、以及把 `check_arbitration` 从「必须引 pytest」抽象成「必须引该领域声明的外部检查证据类型」。切片 A 的验收要从「闸门能拒」扩成「doc 领域下系统模板任务能跑通」（现在 25 条里一条都没覆盖系统模板）。

### P0-4 `tool-run:`/`knowledge:` 改真解析是全域改动，自己推翻了 P33-19；而且 `tool-run:` 按 id 查做不到

今天两个前缀无条件 trusted（`claims.py:80-81`），改掉会把一批 SUPPORTED 降成 unsupported，与 A07 / P33-19「逐字一致」直接矛盾。

更要命的是做不到：`call_key = f"{run_id}:{call_id}"`（`event_handler.py:585,597-601`），**模型写信封时两个都不知道**；而且只有 `view == "work"` 的调用才记（`event_handler.py:593`）。「必须查得到」在实践中等于「所有 `tool-run:` 一律 unresolved」。

建议：改成**领域内收紧**——`code-v1` 保留旧语义逐字不变，`doc-research-v1` 的 `allowed_evidence_kinds` 直接不含 `tool-run:`。要让它可核验，先解决「模型怎么拿到这个 id」（gateway 回显稳定的 `tool_run_id`），否则留作遗留。

### P0-5 事件与回放整段缺失（第 8 步那个坑的原样复现）

- `replay.py:361`：任何新事件类型只要既没进 `apply()` 也没进 `NO_FORMAL_EFFECT`，就进 `unknown`，而 `test_replay.py:119` 断言 `unknown_event_types == {}`。
- `replay.py:33-44`：`FORMAL_FIELDS` 里**没有 claim 这一项**，claim 等级今天根本不在回放投影里；`ClaimDisputed` 在 `NO_FORMAL_EFFECT`。
- `replay.py:45-46`：`OPTIONAL_FIELDS` 只有 `("mission","policy_version_id")` 一个兼容口子。
- `schema.py:414-417`：`workspaces` 表明写「Operational state, not a Mission fact: no event, not part of the replay projection」——另一条先例。

计划 D1/D3/D7 全要写库，却没有一个字说发什么事件、新字段进不进 `FORMAL_FIELDS`、旧库怎么进 `OPTIONAL_FIELDS`，而 P33-22 已经断言「全部由事件折叠、覆盖率 100%」。计划与验收对不上。

建议：对领域冻结（照 `policy_version_id` 先例做正式状态）、`sources`（建议正式状态，A08 的「历史不改写」要靠它）、`criterion_assessments`（建议不进正式状态，照 `verifications` 表先例）、`needs_recheck`（不进投影）逐个定性并写进计划；不管选哪条，切片 A 第一件事就是跑 `test_replay.py` 确认 `unknown_event_types == {}`，不要等切片 F。

## P1

- **P1-1 评估记录的写入通道没定，FAIL/INCONCLUSIVE 的评估现在写不进去**：`event_handler.py:2182-2187` 只把 **PASS 的层**传进 `accept_result`；adapter 跑在 verification 阶段（那里才有 `verification_copy`），accept 时验证副本已不在手上。且结果 FAIL 时 accept 根本不跑，`criterion_assessments` 一行都不会写——而 P33-03..06 都写着「记录里带失败码」。建议：解析结果与 verdict 搭 `LayerResult.detail` 的便车经 `record_verification_layer` 落 `verifications` 表，accept 事务从 detail 重放出 assessment 行；并说清 FAIL 路径的记录落在哪。
- **P1-2 `inconclusive_retry_limit` 落哪层**：进 `OrchestratorConfig` 就**必须**登记 `SNAPSHOT_FIELDS`（`policies.py:197`，否则 `policy_snapshot` 直接抛）；同类次数上限都在 `PROMOTABLE`，而 `verification_policy` 在 `NON_PROMOTABLE`。建议放 `DomainProfileV1.completion_rules`（部署常量、随 Mission 冻结、不可晋级）。
- **P1-3 领域冻结存哪、facade 怎么开都没写**：`Mission` 是 frozen dataclass 没地方放；`domain` 不进 `facade.py:45-57` 的 `OPEN_FIELDS`，`_strict` 会直接拒。建议照 `mission_policies` 做绑定表并补 facade 改动。
- **P1-4 落点**：`governance/` 已经是「部署给定、Mission 冻结、模型不能改」的归属地且有整套内容寻址+冻结+快照机制；而 `profiles` 一词在本仓库已指**运行时模型画像**（`policies.py:313,359`、`profile_failure_threshold` 等），再叫一个 `profiles.py` 会让快照里出现两个语义不同的 profiles。建议 `governance/domains.py::DomainProfileV1`。`evidence_resolver.py` 与 `adapters/` 的落点没问题。
- **P1-5 D7 在本轮没有任何入口，A08 三条测的是造出来的场景**：`workspace_seed` 在 Mission 创建时一次性写死进 `final_report` 且受 protected 保护，**一个 Mission 生命周期内来源根本不会变**。要么补真实入口（facade register/revoke + Commit Service 方法 + 事件），要么如实缩成「跨 Mission 复用时的版本核对 + 历史不改写」。
- **P1-6 `needs_recheck` 会让下游 rule_check 硬 FAIL，没有出口**：`check_used_knowledge` 的 problems 非空 → FAIL → 重试 → 模型再引同一条 → 再 FAIL，正是 A04 要避免的无穷返工。建议改成让 `retrieval` 把它排除出可用知识，或走正式的 SUPERSEDED 路径。
- **P1-7 INCONCLUSIVE → human_review 的升级机制不存在**：router 里唯一能强制第六层的是 Critic 的 `needs_human`（`verifier_router.py:128-129,201-216`），且每 Task 限一次。adapter 给 INCONCLUSIVE 时谁置 `escalated`、算不算用掉额度，计划没写。
- **P1-8 `VERIFIER_VERSION` 要不要 bump 没决定**：它进 policy snapshot 的 hash（`policies.py:269`），又决定挂起恢复能复用哪些层（`human_review.py:55-60`）。不动 → 恢复会原样复用带 adapter 的层；动 → snapshot hash 变、挂起中的 Mission 恢复时层不可复用。必须选一边。
- **P1-9 切片顺序有 fixtures 悬挂风险，风险表一条没有**：脚本耗尽是直接抛 AssertionError（`fixtures.py:104,514,524`），切片 B/C/E 每一个都可能触发。建议每切片**完成即跑** `tests/orchestrator` 全量，不等切片 F。
- **P1-10「三处闸门」与代码对不上**：实际只有两个校验点（`task_graph.py:317-331` 整图提案、`changes.py:526-541` 图变更与 add_task 同一个）。真正需要第三个闸门的是**系统模板**（`conflict_task`/`synthesis_task` 由 Commit Service 直接 `insert_task`，`commit_service.py:1806`，两个校验点都绕过）——这正是 P0-3 的机制原因。

## P2

- **P2-1** `parse_evidence` 现在「先剥前缀立刻 `_normalise`」（`claims.py:58-64`），整串 `source:...#L3-L5"引文"` 丢进去会被当路径揉。实现必须**先按语法切四段，只对 path 做 normalise**。
- **P2-2** 挂起恢复复用的是上次记录的 LayerResult（`verifier_router.py:166-170`），若评估搭 detail 便车则一起带回来（正确）；若改成 accept 时重跑 adapter，恢复路径就会缺。计划里点一句。
- **P2-3** `_grade_and_project` 是顺序循环（`commit_service.py:1516`），同一 Result 里 claim B 引用 claim A 刚生成的知识能否 resolve 取决于迭代顺序。写一句「同事务内新建的知识不作为证据」最省事。
- **P2-4** 二进制来源本轮没有载体：`workspace_seed` 类型是 `Mapping[str, str]`（`missions.py:60-66` 明确要求文本）。诚实边界改成「本轮来源只能是文本；二进制没有登记通道，登记为遗留」。
- **P2-5 acceptance 的问题**：P33-19 与 D2 的收紧必然打架；P33-16 后半、P33-10、P33-11 **今天就已经成立**（`grade_claim` 从不读 critic verdict；`covering_target` 已要求覆盖），它们是回归钉子而不是 A03 的正面证据；P33-22 断言与 D 段的缺失对不上；缺三条——doc 领域系统模板能跑通、每切片全量仍绿、回放 `unknown_event_types == {}`。其余 18 条可执行且确实测到了声称的东西，P33-07（越权与不存在返回**完全相同**）写法尤其好。

## 明确没问题的部分

§2 现状差距表六条**逐条核对全部属实**，没有一条是推测（尤其 `tool-run:`/`knowledge:` 无条件 trusted、冲突只比 key+stance 这两条是真缺陷）；§3 的诚实边界干净，「字面包含不是意思差不多」是全篇最关键的自我约束，必须保住；D2 的六个失败码 + 两个标记分开存是整份计划最好的一段，同时满足 A02 与 A06；`evidence_resolver.py` 与 `adapters/` 落点正确（解析需要 verification_copy + store，只有 verification 阶段两者都在手上）；「INCONCLUSIVE 不是新 Task 终态」判断正确；「Critic 不能单独升 VERIFIED」加结构测试正确；schema v8 迁移写法与 `schema.py:433-441` 的追加模式一致，没问题。

## 最小修改清单（改完可重评）

1. D6 改判定方向 + 写死 scope 缺省=全域
2. D3 的等级不再读模型写的 `type`
3. `DomainProfileV1` 从「设下限」扩成「可替换默认政策与系统模板」，切片 A 补系统模板验收
4. `tool-run:`/`knowledge:` 收紧改为领域内生效，`code-v1` 逐字不变
5. 新开一节「D9 事件与回放」，对 4 个新对象逐个定性并列出 `FORMAL_FIELDS`/`OPTIONAL_FIELDS` 改动，同步改写 P33-22
6. D3 补评估记录传递通道、D1 补冻结存哪+facade `OPEN_FIELDS`、落点改 `governance/domains.py`
7. 切片表每格加「完成即跑全量」，风险表补 fixtures 悬挂
