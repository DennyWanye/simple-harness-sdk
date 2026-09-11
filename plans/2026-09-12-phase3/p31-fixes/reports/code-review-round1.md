# P3.1 遗留修复 · 代码评审第 1 轮

- 评审对象：SDK main `a84e2a4`（agent_orchestrator 0.9.4 / simple_harness 0.9.11），HEAD `6871376` 只含 P3.2 计划文件
- 评审员：独立评审子代理（只读，未运行 pytest）
- 已跑检查：`.venv/bin/ruff check`：All checks passed；`ruff format --check`：80 files already formatted；`.venv/bin/mypy src/agent_orchestrator`：Success，80 个源文件无问题

## 结论：SHIP_WITH_FIXES

实现与计划第 2 版一致，没有 P0。有 2 条 P1，都是验收证据缺口：FX-5 的"被取代的保持 UNVERIFIED"没有测试；Manager 的下限注入没有端到端测试。补上测试即可，不需要改源码。另有 10 条 P2，其中 P2-3（FX-4 的措辞与覆盖）和 P2-4（计划承诺的真实测试 `max_planning_attempts=3` 没有落实）建议本轮一并处理，至少在 journal 里记为偏差。

## 发现表

| 编号 | 位置 | 问题 | 具体失败场景 | 建议修法 |
|---|---|---|---|---|
| P1-1 | `tests/orchestrator/host_support/test_p31_fixes.py`（FX-5 组）；acceptance FX-5 | 验收 FX-5 明写"被取代的保持 UNVERIFIED"，但没有任何测试覆盖被取代的候选。现在的实现靠 `_close_attempt`（`commit_service.py:1439-1446`）只把结果的验证状态改成 `REJECTED/superseded`，不碰产物；`accept_result` 只改 `stored.artifacts`（`commit_service.py:3062-3063`） | 以后如果有人把"关闭候选"也接到 `update_artifact_verification(...,"REJECTED")`，或者把 accept 改成遍历整个 Task 的产物，全套测试照样绿，验收项实际上没有证据 | 仿照 `step03/test_static_dag_closure.py::test_s3_05`，设 `candidates_per_task=2`，用 `holds` 扣住第二个候选，等第一个被接受后，用 `Store.open_readonly` 读回：接受者的产物是 VERIFIED，被取代者的产物是 UNVERIFIED。注意 Task 预算至少要 `2×(4096[+6000])` |
| P1-2 | `test_p31_fixes.py::test_the_manager_package_names_the_floor`；`event_handler.py:2442` | 这条测试直接把 `budget_floor={...}` 传给 `build_manager_package`，再断言原样出现。它只证明字典被拷贝了一遍，接近空转。Orchestrator 在 Manager 路径上的注入 `budget_floor=self._budget_floor(mission.id)`，以及 Manager 被拒后能否在下一轮 `rejections` 里看到 `task_budget_below_floor`，都没有端到端证据（验收 FX-3 后半句） | 删掉 `event_handler.py:2442` 那一行，或者 k 取错，所有测试照样通过 | 加一条端到端测试：`demo_dynamic_dag_provider(manager_steps=[低于下限的 add_task, 合法的变更])`，用 `_capturing` 的方式包住 manager 步，断言：①两次 Manager 包的 `budget_floor == {"min_task_tokens": 4096, "min_task_tokens_with_critic_review": 10096}`；②有一条 `TaskGraphChangeRejected` 事件，detail 里带 `task_budget_below_floor`；③第二次包的 `rejections` 带这个原因。脚本步数要和 Manager 实际轮数一致，不然会耗尽脚本导致悬挂 |
| P2-1 | `event_handler.py:371-391`；`assembly.py:157-160` | `_budget_floor_rule`（base 取 config 与各 profile 的最大值、`min_task_tokens` 显式正数、为 0）和 `_candidates_for`（k 取绑定策略）都没有针对性测试。端到端测试只覆盖了 k=1、无 profile 的情况。`min_task_tokens` 为负数或 bool 时报 ValueError，也没有测试 | profile 取最大值写错（比如只取 config 的值），或者 k 改成读 config，测试不会发现。k 读 config 的情况有 `step09/test_policy_binding.py:188` 的源码正则守着，这一项有保护 | 端到端各加一条：①config `candidates_per_task=2`，Planner 包的两个字段应为 8192 / 20192；②传入 profile `default_max_output_tokens=8192`，字段应为 8192 / 14192；③`min_task_tokens=5000`，字段应为 5000 / 11000；④`OrchestratorConfig(min_task_tokens=-1)` 和 `min_task_tokens=True` 都应报 ValueError |
| P2-2 | `task_graph.py:161-178` 的 docstring；CHANGELOG 0.9.11；`event_handler.py:3219-3235`、`1984` | 下限写成"保证第一个 Attempt 与它的 Critic 都能预留"，这只在预留那一刻成立，而且前提是 Attempt 结算的用量不超过它的预留。①k=1 且预算正好等于下限时，Worker 的预留经 head_room 压到 `base`（=default_max_output_tokens），而一轮真实结算包含输入 token、还可能有多次模型调用（原生验收一轮 22003）。结算以后，Task 账户剩余额不足 critic，Critic 的预留（记在 Task 账户上，`event_handler.py:1984`）会失败。②k≥2 时，第一个候选分到的份额是 `max_tokens//k`（`event_handler.py:3223-3225`），它本身已经含 critic 份额，所以第二个候选被压到 base；第二个 Critic 要等第一个候选结算后才能预留 | 真实模型下，预算正好等于下限的 Task，第一轮之后 Critic 就预留不到，最后以 budget_exhausted 结束。计划 §1 的"说明"其实已经承认这是必要条件，但代码注释和 CHANGELOG 的措辞比实际更强 | 把 docstring、CHANGELOG 和 Host ARCHITECTURE 的措辞改成"预留层面的必要条件：假设一轮的结算不超过它的预留"。可选（放到 P3.5）：Worker 份额改为 `max_tokens//k - critic_share`，让 k 个候选对称 |
| P2-3 | acceptance FX-4；plan §2.1 倒数第二条；`test_a_pool_that_cannot_hold_one_floor_ends_as_planning_failed` | 计划写的是"预算池连一个 Task 的下限都容不下时……失败原因里带 `task_budget_below_floor`"，但测试里 Planner 两次都提议显式的 5000，这其实是"Task 低于下限"，不是"池容不下"。如果 Planner 在 9000 的池里提议 10096，先触发的是 `fits_within`（`task_graph.py:336-341`）或总和检查（`:352-357`），`planning_failed` 的原因里没有 `task_budget_below_floor`。而且池不够这件事可以提前判定，Planner 却把重试次数白白用完 | 真实 Planner 看到下限字段后，老实给出 ≥ 下限的数字，Mission 仍然以 planning_failed 结束，但原因写的是"超出 Mission 预算"，读的人找不到真正的根因 | 二选一：①在 `commit_graph` 或 `begin_planning` 里预检，池减去系统预留后小于最小下限时，直接以 `task_budget_below_floor` 拒绝（或者在 Planner 包里加 `pool_below_floor: true`）；②把 plan 和 acceptance 的措辞改成"提案低于下限"，另外补一条"提案 ≥ 下限但超出池"的测试，钉住实际的原因文本。同时，份额算出来的预算被拒时，拒绝文本还写着"give at least N"，可以加一句"或写明显式预算、减少 Task 数" |
| P2-4 | plan §2.1 最后一条；journal §1"真实模型风险"；`tests/orchestrator/step04/test_real_provider_knowledge_sharing.py:38-52`、`step05/test_real_provider_dynamic_dag.py:44-51` | 计划写了"本仓库的真实 opt-in 测试把 `max_planning_attempts` 调到 3"，但 a84e2a4 没有改任何 real 测试，journal 里也没有记为偏差。这两个 real 测试用 `default_max_output_tokens=8192`、`critic_reserve_tokens=30000`，含 critic 的下限是 38192；缺省 `max_planning_attempts=2`（`assembly.py:82`）。`__main__.py` 里 approval 和 multi-mission 的真实模式（REAL_KNOBS 在 `:646-657`，real_knobs 在 `:520-531`）也一样 | 真实 deepseek-flash 的 Planner 连续两次给出低于 38192 的预算，Mission 就以 planning_failed 结束，real 回归出现新红 | 按计划给 real 测试加上 `max_planning_attempts=3`（至少 step04/05 这两个带 REAL 调参的），真实模式的 demo 也考虑一并调整；如果决定不做，就在 journal §2 里记为偏差 |
| P2-5 | `store.py:1467-1485`；`event_handler.py:2036-2052` | `update_artifact_verification` 找不到行时抛 StoreError。事务语义是对的：嵌套时并入调用方的事务，`read_view` 里调用会被 `transaction()` 拒绝（`store.py:373-374`），抛错时整个 accept 或 fail 一起回滚。但调用点只捕获 `CommitRejected / IllegalTransition`，StoreError 会冒出 run 循环 | 目前走不到这里：`record_result` 在同一个事务里 upsert 了全部产物（`commit_service.py:2676-2687`），artifacts 表从 DDL_V1 起就存在。只有损坏或被手工改过的库才会触发 | 保持大声失败即可，在 docstring 里写一句"不存在即库损坏"；或者跳过缺失的行并记一条 note。低优先级 |
| P2-6 | `store.py:141-142`；`commit_service.py:1356-1358、1445-1447` | 同一个词在两处含义不同：被取代的结果，结果的 verification_state 是 `REJECTED`（verdict=superseded），它的产物却保持 `UNVERIFIED`；产物的 `REJECTED` 指的是判了 FAIL | Host 读模型如果把结果状态和产物状态并排显示，会看到"结果 REJECTED、产物 UNVERIFIED"，容易误读 | 在 Host ARCHITECTURE（FX-7）和 CHANGELOG 里写清：产物的 REJECTED = 判了 FAIL；被取代 = 没有判过，所以保持 UNVERIFIED |
| P2-7 | `observability/evaluation.py:69-87`（PLAN_CONFIG） | 评测计划可以设 `critic_reserve_tokens` 和 `default_max_output_tokens`，这两个值会抬高下限，但不能设 `min_task_tokens` | 如果一个评测计划把 critic 调到 30000，fixture 用例（MULTI_MISSION_TASKS、COMPARE 的 C，都是 30000 且含 critic）的脚本化 Planner 会被拒，脚本耗尽后悬挂。目前没有计划或测试这样用，属于潜在风险 | 把 `min_task_tokens` 加进 PLAN_CONFIG，或者在评测文档里说明 |
| P2-8 | `context_builder.py:252-253、363-364` | Manager 包现在总是带 `budget_floor` 键，直接构造时为 `{}`；Planner 包在注入下限后多了两个字段。所以同样的输入，0.9.4 算出的 `context_version` 与 0.9.3 不同 | 已核对，没有功能影响：service intent 按 `subject_id` 幂等（`commit_service.py:635-638`），升级时正在规划的 Mission 不会冲突；回放的 `formal_from_snapshot` 不比对 context_version，也不比对产物 | 仅作记录，不用改 |
| P2-9 | `governance/policies.py:195` | `min_task_tokens` 登记为 include，所以所有配置的 policy 快照哈希都与 0.9.3 不同 | 跨版本对比证据时（step08 的 `snapshot_diff`），会多出一行 `config.min_task_tokens`。回放不比较快照，没有功能影响 | 仅作记录。CHANGELOG 已经提到快照分类，可以再补一句"快照哈希因此变化" |
| P2-10 | `journal.md` §2 第二轮 | step05 `test_p1_3_a_crash_between_proposal_and_commit…` 挂住 10 分钟，按"时序敏感"处理了，还没有结论 | 我按代码核对过：这个 fixture 里 Manager 的 add_task 是 E=20000（format、rule）和 B2=30000（含 code_test），k=1，下限都是 4096，不会被下限拒。崩溃重启后，新实例在 commit 路径上调 `policy_for`，Mission 已绑定策略，不会抛错。可以排除下限这个原因 | 等整批重跑（带 faulthandler）的结果；如果再挂，看 faulthandler 的堆栈 |

## 重点问题逐项核对

### 1. 下限计算与入口

- 公式：`floor_for = max(1,k) × (base + critic·[critic_review∈policy])`，base≤0 时返回 0，即关闭；`floor_refusal` 在 floor 为 None 或 granted 为 None（不限）时不检查。和计划一致。
- base：`min_task_tokens` 为 None 时取 `max(config.default_max_output_tokens, 各 profile 的非空 default_max_output_tokens)`，与 `assembly.py:388` 的 `profile.default_max_output_tokens or config…` 一致；为 0 时关闭；为正数时就用这个数；critic 取 `config.critic_reserve_tokens`。critic 预留属于 NON_PROMOTABLE，不走策略，这与运行时 Critic 实际预留用的 `self._config.critic_reserve_tokens`（`event_handler.py:2744`）一致。
- 入口：提案只有两个入口，`commit_graph → validate_graph` 和 `commit_graph_change → validate_change`，两处都注入了下限。在 src 里 grep 过：`TaskGraphProposal` 只出现在 planner 解析和 commit_service 里；facade、MissionApi、CLI 都不能直接提交图。CLI 直接构造的 `CommitService(store)`（`__main__.py:182、1105、1244`）只做取消、审批、只读查询，没有下限也没关系。图变更的操作集合（`changes.py:38-47`）里，只有 add_task 和 supersede 的替代节点会创建 Task；没有能改已有 Task 预算的操作，所以不存在绕过下限的路径。
- 系统任务：合成与冲突 Task 由 CommitService 直接创建，不经过这两个 validate，不受下限约束；`validate_graph` 里合成模板那段检查也没动（有测试）。
- `_candidates_for` 与策略绑定：`create_mission` 在同一个事务里 `bind_policy`（`commit_service.py:587`）；迁移 v6 把旧 Mission 绑到 `policy-legacy`，其 params 为 None，于是 `policy_for` 回退到 `_config_policy()`，里面含 `candidates_per_task`；评测库走 pin，也有 params。所以 `policy_for` 在正常路径上不会抛错。极端情况下绑定到一个库里没有的版本会抛 ContractError，但这是原有风险：Planner 路径上的 `_template` 更早就会抛。在 commit 事务里抛出只会回滚，不会写坏数据。
- 注意：Orchestrator 在 `min_task_tokens=0` 时仍然注入 `candidates_for`，每次 commit 都会调一次 `policy_for`，有缓存，没有副作用。

### 2. 被拒之后

- Planner：GraphRejected 写入 `TaskGraphRejected`；下一次 Planner 包的 `planning_rejected` 带 reason 和 detail，FX-3 端到端测试已证明。
- Manager：`commit_graph_change` 捕获 GraphChangeRejected，写 `TaskGraphChangeRejected{reason,detail}`（`commit_service.py:1017-1032`）；`_change_rejections` 按 trigger 读回来给下一轮 Manager 包。代码路径是通的，缺端到端测试（P1-2）。
- fixture 与 demo 的逐项对照。缺省配置下 base=4096、critic=6000，所以下限为：无 critic 4096，含 critic 10096，k=2 时为 8192 / 20192。

| 场景 | Task 预算 / policy / k | 结论 |
|---|---|---|
| single-task `DEMO_PROPOSAL` | 50000，含 critic 与 code_test，k=1 | 通过 |
| static-dag `DEMO_DAG_TASKS` | 20000 / 30000，不含 critic；`test_s3_05` 的 k=2 需要 8192 | 通过 |
| knowledge-sharing `COMPARE_TASKS` | 30000，其中 C 含 critic；合成模板不受约束 | 通过 |
| dynamic-dag `RECORDER_TASKS` + Manager 的 E / B2 | 30000 不含 critic；E=20000，B2=30000；k=2 的测试需要 8192 | 通过 |
| multi-mission（fixtures） | profiles 没设 default_max_output_tokens，所以 base=4096；30000 含 critic | 通过 |
| approval-action | 30000，不含 critic | 通过 |
| policy-promotion | 50000，含 critic | 通过 |
| `step05/test_manager_decisions` 的 X1..X3 | 5000，不含 critic | 通过，但只比 4096 多 904，余量很小 |
| `step06/test_workspace_isolation` | k=2，30000，含 code_test | 通过 |
| `step03/test_static_dag_closure:596` | k=3 | 直接构造的 CommitService，没有下限 |
| `step06` 里 max_tokens=100 的两条测试 | Planner 本身就预留不起 | 在规划之前就以 budget_exhausted 结束，不受影响 |
| 真实模式（REAL_KNOBS：8192 / 30000） | Planner 是真实模型，含 critic 的下限为 38192 | 有重试风险，见 P2-4 |

  结论：在缺省配置和 fixture profile 下，脚本化 fixture 和 demo 都不会因为下限被拒而耗尽脚本、落到 UNKNOWN 悬挂。唯一的新风险在真实模式和评测计划调参（P2-4、P2-7）。

### 3. 产物状态

- 事务：只在 accept 和 fail 的外层事务里调用，嵌套时并入同一个事务（depth>0），出错时整体回滚；FX-5 的崩溃注入测试是决定性的：更新发生在故障点之前，故障点在 `commit_service.py:3098`。在 read_view 里调用会被拒。
- 幂等：accept 的早返回分支（DONE+PASS）和 fail 的早返回分支（DONE+FAIL）都在更新之前返回，第一次提交已经写过，所以幂等。accept 内部因为知识过期、合成被冲突阻塞或动作候选被拒而转去 fail_result 时，会标为 REJECTED，语义正确。产物 id 由 (attempt, path, hash) 组成（`ids.py:37`），不同候选、不同 Attempt 之间不会共享一行，所以不存在一个候选的 FAIL 把另一个的 VERIFIED 覆盖掉的问题。
- 被取代：`_close_attempt` 只改结果的状态，产物保持 UNVERIFIED，与计划一致（缺测试，见 P1-1）。
- 读者：src 里没有任何代码读 `verification_status`。evidence 和 `store.snapshot` 会把它原样导出；回放的 `formal_from_snapshot` 不含产物；lineage、trace 和评测只读 id、path 和 hash。没有读者依赖旧值。

### 4. 包、哈希、快照、公共 API

- context_version：变了（见 P2-8），但没有跨版本的比对者。
- policy 快照：`test_s8_07_every_configuration_field_is_classified` 要求每个配置字段都登记，已经登记；快照哈希变化见 P2-9。
- `tests/unit/contracts/public-api.json` 只收录 `simple_harness*` 模块，没有 agent_orchestrator，所以不需要登记 `TaskBudgetFloor` / `floor_refusal`；版本号已同步为 0.9.11。

### 5. 测试是否决定性

- FX-1：参数化覆盖了边界两侧（刚好等于下限、差 1），也覆盖 k=2 和 base=0；池份额的用例正反两面都有。是决定性的。
- FX-2：低于下限、高于下限、不传 floor、缺省份额，都覆盖了。是决定性的。
- FX-3：断言了拒绝事件、两次 Planner 输入的字段、第二次输入里的拒绝原因、最终预算 30000、Mission COMPLETED。是决定性的。
- FX-4：断言有效，但只覆盖"提案低于下限"，没有覆盖"池容不下"（P2-3）。
- "关闭"那条：决定性的。
- FX-5：用新开的只读连接读回；accept 和 fail 两面都断言了，产物非空也断言了；崩溃注入那条是决定性的。
- 缺的场景：被取代保持 UNVERIFIED（P1-1）；Manager 端到端（P1-2）；k 与 profile 的接线（P2-1）；池容不下但提案 ≥ 下限（P2-3）。
