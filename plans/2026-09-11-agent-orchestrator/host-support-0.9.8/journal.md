# Host 接线支持 · SDK 0.9.8 / agent_orchestrator 0.9.1 · 记录

- 日期：2026-09-11 起
- 计划：同目录 `plan.md`；验收：Host `simple_harness/plans/2026-09-11-orchestrator-host-integration/acceptance.md` §A（SA-1 至 SA-7）

## 0. 现在做到哪里 / 下一步（handoff，每次提交时更新）

- 2026-09-11：
  - 切片 A、B、C 已完成：测试先行写好 `tests/orchestrator/host_support/`（先红），实现后 18 条全绿，ruff 与 mypy（79 个源文件）都干净。
  - 版本号已升到 0.9.8 / 0.9.1，`627b90e` 已推送；全量回归在后台运行（scratchpad `regress-098`）；代码评审第 1 轮已处置（§4）。
  - 下一步：
    1. 回归跑完后，修改评审 P1 / P2 涉及的源码并补测试；
    2. 切片 S2：P3.1 外部控制面（`plan.md` §2.4，版本 0.9.9 / 0.9.2）；
    3. 再跑一次全量回归，构建 wheel，做代码评审；
    4. 回到 Host 做 H1，钉 0.9.9。
  - 方向依据：用户 2026-09-11 22:39 放入 Host 的 `plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md`。当前的 Host 接线即其中的 P3.1（Host 直连路径）。
- 接手须知：
  - 全量回归在后台跑时，不要改源码与版本号，也不要往 `tests/` 放新测试。
  - 真实模型一律用 deepseek-flash。

## 1. 实现要点（切片 B / C）

| 位置 | 改动 |
|---|---|
| `governance/policies.py` | `DeploymentPolicy.local_code_execution: bool = True`；为 False 时 `allowed_tools` 不能包含 `run_tests`，否则构造即报错；`to_json` 输出该字段；新增 `deployed_layers(deployment)` |
| `contracts/models.py` | `SYSTEM_DEFAULT_POLICY`，以及 `default_change_policy(deployed)`。关闭本机执行时，默认策略从 `format_check / rule_check / code_test` 改为 `format_check / rule_check / critic_review`：用独立 Critic 代替测试，不留下只有格式与规则两层的空策略。放在 contracts 是为了避开 `graph.changes → planning.manager` 的循环导入 |
| `graph/task_graph.validate_graph`、`graph/changes.validate_change`、`CommitService._check_task_proposal` | 三个"验证层是否已部署"的判定点都改用部署层集合；`CommitService(deployed_layers=…)` 由 Orchestrator 按部署政策传入 |
| `graph/changes.NewTaskNode.from_json` / `add_tasks`、`planning/manager.synthesis_task` | 缺省策略由调用方按部署层给出 |
| `CommitService._open_conflict` | 部署层不含 code_test 时，`deferred_reason = "local_code_execution_disabled"`，不创建冲突 Task |
| `verification/verifier_router.py` | 开关关闭时，旧 Task 的 `code_test` 层记为 ERROR（`undeployed: true`） |
| `orchestrator/event_handler.py` | Mission 判定阶段不跑 `pytest:` 条件，判为 `met=false`，理由写明本机代码执行已关闭；新增 `create_mission` 入口，与 `submit_mission` 共用 `_check_mission_door`（动作条件检查 + pytest 条件检查）和 `_commit_mission`；Planner 与 Manager 的输入包带上 `deployed_verification_layers` |
| `runtime/tool_gateway.py`、`runtime/assembly.py` | 开关关闭时 `run_tests` 以 `local_code_execution_disabled` 拒绝（纵深防御，权限交集本来就会先把它挡掉） |
| `runtime/role_templates.py` | 升到 `planner-v4` / `manager-v2`，由旧版本经精确替换得到（锚点缺失时导入即报错）；`planner-v3` / `manager-v1` 继续登记 |
| `api/missions.py`、`__main__.py` | 新增 `spec_from_request`；`MissionApi(orchestrator=…)`；CLI 的 `mission create` 走编排入口，provider_kind 按实际记录 |

## 2. 测试

| 验收 | 测试 | 结果 |
|---|---|---|
| SA-1 | `test_a_planner_asking_for_code_test_is_refused_and_the_replan_completes` | PASS：第一版图被 `verification_policy_undeployed` 拒绝，重新规划后完成；两次 Planner 输入包都带着部署层 |
| SA-2 | `test_model_written_test_files_never_run` | PASS：Worker 写了 `conftest.py` 和 `test_probe.py`（导入时会写标记文件），但标记文件不存在，`run_pytest` 间谍的调用次数为 0 |
| SA-3 | 模板与默认策略两条测试 | PASS |
| SA-4 | `test_a_contradiction_is_deferred_without_a_conflict_task` | PASS |
| SA-5 | `test_create_mission_entry.py`，共 9 条（含 CLI 子进程） | PASS |
| SA-6 | 默认值不变；设置矛盾时报错 | PASS |
| 开关打开前建的 Mission | code_test 层记为 ERROR 且 Mission FAILED；`pytest:` 条件判为未满足，理由写明原因，停止原因为 `mission_criteria_unmet` | PASS |

过程记录：`test_a_pytest_mission_criterion_…` 第一次失败，原因是测试写错了事件名。Mission 判定发出的是 `MissionSuccessJudged`，`MissionCriteriaJudged` 属于动作路径。诊断确认实现行为正确：pytest 一次也没有运行，条件判为未满足，理由写明本机代码执行已关闭。之后修正了测试，并收紧断言：逐条核对每个条件的判定结果与停止原因。

## 3. 回归与 wheel

| 次 | HEAD | 结果 |
|---|---|---|
| 1 | `627b90e` | **作废**：跑到 74% 时挂住 13 分钟（CPU 占用 5.9%，一直在等），手动终止。原因是 `step06/test_observability.py::test_a_required_verifier_that_is_not_deployed_…` monkeypatch 的是 `task_graph.STEP2_IMPLEMENTED_LAYERS`，而 0.9.8 把"哪些层已部署"的判断改到了部署政策这边（CommitService 从 `governance.policies` 取值），测试的补丁不再生效，Planner 的提议被拒，夹具脚本随即耗尽，结果是 UNKNOWN、挂起（HANDOFF §4 的陷阱）。已改为 patch `governance.policies`（`8444a39`），测试意图不变 |
| 2 | `8444a39` | 58 failed / **2428 passed** / 13 skipped / 15 errors，用时 300 s；红集 73 条，与基线完全一致，**0 新红**。脚本 `scratchpad/regress-098/watchdog.py` 加了看门狗：20 分钟未结束就杀掉整个进程组，并从进程打开的临时目录推断挂住的测试 |
| 3 | `4bd8c52`（S2，0.9.9 / 0.9.2） | 58 failed / **2439 passed** / 13 skipped / 15 errors，用时 303 s；红集 73 条 = 基线，**0 新红**；mypy 80 个源文件无问题 |

### S2 实现要点（P3.1 外部控制面）

- `api/facade.py` 的 `MissionControlV1`（`FACADE_VERSION = "mission-control-v1"`）：
  - 创建：严格映射字段；未开放的字段点名拒绝；预算只开放 `max_tokens`、`max_attempts`；回执带 `spec_hash`；同一个 key 对应不同内容时报 `conflict`。
  - 取消：幂等，对终态 Mission 原样返回。
  - 审批相关的 decide / takeover / comment：先检查归属，再过密钥检查，然后交给 ApprovalApi 处理。
  - 读取：
    - `missions` 列表；
    - `snapshot`：放在 `read_view` 里，快照和 `through_seq` 来自同一次读；
    - `events`：有上限 200，靠多取一条来判断 `has_more`；
    - `approvals`；
    - `artifact_read`：只接受 id，读前重新计算 hash，最多读 256 KiB。
  - 不属于调用方的对象，和根本不存在的对象，一律返回同一句 `not_found`，消息里不带 id。
- `Store.read_view()`：只读的一致性读，内部是一个 DEFERRED 事务；如果已经在事务里，就直接并入。`last_event_seq()`：取这个 Mission 最大的事件 seq。
- `spec_from_request` 补上三个字段的映射：`untrusted_sources`、`synthesis`、`conflict_reserve_tokens`。这是 `test_open_fields_round_trip` 抓到的问题，也就是用户 Phase3 计划里的差距 G02：以前请求里带了这三个字段也会被收下，但字段本身被静默丢掉。
- 快照结构的实测记录（给 Host 投影用，脚本 `scratchpad/shape/sample.py`）：
  - `seq` 是全库共用的自增序号，同一个 Mission 的 seq 不连续；
  - 快照里有 `intents`（内部配置）和 `storage_uri`（本机路径），Host 不能透传给界面；
  - 快照不含用量金额。

## 4. 代码评审

第 1 轮（`reports/code-review-round1.md`）的结论是 SHIP_WITH_FIXES。评审确认：开关关闭时，编排运行期没有在本机执行模型代码的路径。下面逐条处置；带"源码"字样的，都等全量回归跑完后再改。

| 编号 | 处置 |
|---|---|
| P1-1 关闭时 Task 级 `pytest:` 条件没人判定却能 PASS | 接受（源码）。部署层不含 code_test 时，三个判定点（图、改图、提议）以及合成模板都拒绝 Task 级 `pytest:` 条件（`verification_policy_undeployed`，写明原因）。对于开关打开时就已建好的旧 Task，`rule_check` 把这类条件判为 FAIL，理由写明本机代码执行已关闭，不留"条件从未验证却 PASS"的空档 |
| P1-2 SA-4 与原文不符 | 选择改写 SA-4（评审给了两个方案，这里取第二个），不在关闭时新开一条"无冲突 Task 的仲裁"状态路径。理由有两点：一是现有仲裁裁决要靠冲突 Task（`_apply_conflict_ruling` 的 unresolved 分支会 `stop_task`），离开冲突 Task 需要新增状态路径；二是用户 Phase3 计划的 P3.3 专门处理非代码任务的冲突，P3.3-A05 接受"双方带范围进入 DISPUTED"。改写后的 SA-4 是：关闭时冲突进入 DEFERRED（`local_code_execution_disabled`），有争议的 Claim 保持 DISPUTED、不进入正式知识，Mission 不死锁，最终报告列出未解决的冲突。关闭状态下的人工冲突仲裁登记为遗留，归 P3.3 |
| P1-3 SA-2 的判定口径分不出新旧行为 | 接受。同一个场景按开关开/关各跑一次：Worker 写好探针后调用 `run_tests`；打开时标记文件出现、间谍记录到调用，关闭时两者都没有。另加一条直接测试网关兜底分支的用例：`WorkspaceToolGateway(local_code_execution=False)` 绑定了 `run_tests` 时，拒绝码为 `local_code_execution_disabled` |
| P2-1 评测不受开关约束 | 登记为遗留。评测只能从 CLI 触发，Host 不暴露；Host 文档写明"评测会在本机跑 oracle pytest" |
| P2-2 旧库沿用 v3 / v1 提示词 | 登记（行为如此）。更新 `role_templates.py` 里那段过时的注释 |
| P2-3 `CONTEXT_BUILDER_VERSION` | 接受（源码），升到 v4 |
| P2-4 `create_mission` 的 docstring | 接受（源码）。docstring 写明 `MissionConflict` 会原样抛出（同一个 key 对应不同内容），`submit_mission` 保持原有调用约定 |
| P2-5 合成模板在入口处没有检查 | 接受（源码）。在 `_check_mission_door` 里检查合成模板 |
| P2-6 最小配置会报错 | **不采纳**。默认的 `TOOL_NAMES` 恰好就是"三个工作区工具加 `run_tests`"，代码分不清调用方是用了默认值，还是明确列出了全部四个工具。如果自动去掉 `run_tests`，就会悄悄收窄一个调用方明确给出的配置，这比直接报错更糟。现在的报错已经写明两个设置互相矛盾；Host 一直显式传入工具集，不受影响 |
| P2-7 缺少集成测试 | 接受。补测 `NewTaskNode` 的缺省策略、`synthesis_task` 的缺省策略、`_check_task_proposal` 的拒绝，以及关闭时的 `validate_graph` / `validate_change` |

### 4.2 第 2 轮（S2 外部控制面，`reports/code-review-round2-s2.md`）：SHIP_WITH_FIXES → 已处置，版本 0.9.10 / 0.9.3

| 编号 | 处置 |
|---|---|
| P1-A synthesis 绕过严格字段校验 | 接受。<br>① facade：`_strict_synthesis` 定了子键白名单，范围是 goal、success_criteria、rationale、outputs、verification_policy、priority、`budget.{max_tokens, max_attempts}`；`allowed_tools` 这类已关闭的子键会按名字报错拒绝。<br>② 编排入口：`_check_synthesis_template` 核对 goal 与条件为非空字符串、验证层都已部署、预算不超过 Mission 预算、工具在 Mission 允许范围内、动作条件合规。<br>③ 模板里的文字也做密钥扫描。<br>测试：`test_a_synthesis_template_meets_the_door`（8 种夹带方式逐一被拒）和 `test_a_synthesis_template_round_trips` |
| P1-B `read_view` 不禁止写 | 接受。<br>① 加 `_reading` 标志，视图里开 `transaction()` 直接抛 StoreError。<br>② 视图内抛异常时 ROLLBACK。<br>③ 状态重置放进内层 `finally`，不论 COMMIT 成败都会执行。<br>测试：`test_store_read_view.py` 共 3 条，包括"COMMIT 失败后仍复位，后续事务照样原子" |
| P1-C 验收场景缺决定性测试 | 接受。新增的测试：<br>① 跨租户调用 decide（批准、拒绝）、takeover、comment（目标是 Task 或审批）、artifact_read、`approvals(None)`，全部返回 not_found，报错文字一致，状态和事件数都不变；<br>② 快照的两次 SELECT 之间，另一个连接插入事件，`through_seq` 不包含它，验证快照和游标出自同一次读取；<br>③ 截断时 `truncated=True`，并且不会把多字节字符截成半个；<br>④ 重复创建不会多开预算账户；<br>⑤ stop_conditions、budget、synthesis 都能原样回读 |
| P2-1 字符串被当成序列拆开 | 接受。facade 和 `spec_from_request` 都要求列表字段是字符串列表，`workspace_seed` 必须是 `Mapping[str, str]`。stop_conditions 的取值集合没有限定，登记为遗留：SDK 目前没有这个集合的单一定义 |
| P2-2 artifact_read 有检查与读取的竞态 | 接受。改为一次流式读取，边算 hash 边保留前 MAX+1 字节。非 UTF-8 内容返回 `encoding: "binary"`、`content: None`，不再静默替换 |
| P2-3 cancel 有竞态 | 接受。提交时如果状态刚好变成终态，就重新读取，按幂等返回 `changed: False` |
| P2-4 错误映射有漏洞 | 接受。① decide 和 takeover 先校验请求的形状，再查归属，形状不对报 `invalid_request`。② decide、takeover、comment 捕获 StoreError / ContractError / ActionCommitError / ApprovalRequestError；create 捕获 BudgetError / ContractError |
| P2-5 旧 Task 的 pytest 条件要靠策略里恰好有 rule_check 才会 FAIL | 接受。关闭本机执行时，只要 Task 带 `pytest:` 条件，就强制把 rule_check 加进必需层。测试：`test_a_legacy_pytest_criterion_fails_even_without_rule_check_in_the_policy` |
| P2-6 seq 是全库共用的自增序号 | 登记。分页本身没有缝隙；但跳号会暴露同库其他租户的事件量。Host 目前单租户，不受影响，写进 ARCHITECTURE。按 Mission 分别编号属于 schema 变更，归入 P3.5 |
| P2-7 `_clean` 没扫 workspace_seed，snapshot 暴露 storage_uri | 接受前半：密钥扫描现在覆盖 workspace_seed 和模板文字。后半由 Host 投影层处理：它从不透传 `storage_uri`；SDK 的 snapshot 保持原样，CLI 和回放要用它 |
| P2-8 P2-6 不采纳的理由写得不准 | 接受，改写措辞：保守的直接报错可以接受，理由不是"分不清"（用 None 作哨兵其实能区分），而是"不静默改写调用方给出的配置"。`pytest_criteria` 辅助函数的统一登记为遗留，属于代码整洁问题 |

另：Host 集成测试发现 `demo_approval_action_provider()` 把工具写死成含 `run_tests` 的四个，导致只开放工作区工具的部署规划被拒、夹具脚本耗尽后挂起。已加 `allowed_tools` 参数（`testing/fixtures.py`）。

修复过程中，测试抓到一处我自己引入的缺陷：插入 `_check_synthesis_template` 时，关闭本机执行时拒绝 `pytest:` 条件的那段检查被挪进了新函数，只有带 synthesis 的请求才会执行，普通 Mission 的 `pytest:` 条件不再被拒。`test_create_mission_entry` 的两条用例当场变红，已拆开修正。

## 5. 遗留

（待填）
