# Host 接线支持 · SDK 0.9.8 / agent_orchestrator 0.9.1 · 计划

- 日期：2026-09-11
- 来源：Host 计划 `simple_harness/plans/2026-09-11-orchestrator-host-integration/plan.md` 第 2 版 §3.1（切片 S1）；plan review 第 1 轮的 P0-1 与 P1-1（Host `reports/plan-review-round1.md`）
- 基线：main `ae8f0ec`（0.9.7 / 0.9.0，wheel 源 `88e5582`）
- 验收：Host `acceptance.md` §A 的 SA-1 至 SA-7
- 编排方式：与第 2–9 步相同——测试先行（`tests/orchestrator/host_support/`）、按切片提交、全量回归红集 ⊆ 73 条基线、独立代码评审、wheel 在干净 venv 中验证、中文记录。这个切片只改部署政策与校验，不改模型行为；真实模型证据随 Host 的 HA-11 一起取。

## 1. 要解决的问题

1. **本机代码执行没有部署级开关（P0-1）。**
   - `code_test` 层是否运行由 Task 的 `verification_policy` 决定，这个字段由 Planner（模型）填写（`runtime/role_templates.py:59`）。
   - Manager、改图、冲突任务、合成任务的默认策略都含 `code_test`（`graph/changes.py:146`、`planning/manager.py:30,113`）。
   - 没有 `pytest:` 条件时，`code_test` 照样对整个验证副本跑 pytest（`verification/deterministic_checks.py:183-184`）。
   - `code_test` 属于安全边界，不能消融（`runtime/assembly.py:57-69`）；`DeploymentPolicy` 也没有关闭它的字段。
   - 结果：一个不想在本机执行模型代码的部署（Host 桌面 App）没有办法做到。
2. **`MissionApi.create` 绕过了编排实例的检查（P1-1）。**
   - 它不检查动作条件，不传 `provider_kind`（记为 `"unknown"`），也不传 `policy_defaults` / `policy_pin`（`api/missions.py:51-70` 对照 `orchestrator/event_handler.py:734-742`）。

## 2. 设计

### 2.1 本机代码执行开关

- `DeploymentPolicy.local_code_execution: bool = True`：默认值不变，已有部署与测试的行为不变；`to_json` 输出这个字段。
- 为 False 且 `allowed_tools` 含 `run_tests` 时，`__post_init__` 报错：这两个设置互相矛盾。
- 新函数 `deployed_layers(deployment) -> frozenset[str]`：从 `STEP2_IMPLEMENTED_LAYERS` 里去掉 `code_test`（当开关为 False 时）。

开关关闭时，五个位置必须一致：

| 位置 | 行为 |
|---|---|
| Graph Manager 检查（`graph/task_graph.validate_graph`、`graph/changes.validate_change`）与 Commit 提议检查（`CommitService._check_task_proposal`） | 含 `code_test` 的 Task 按现有的 `verification_policy_undeployed` 拒绝。部署层集合由 `CommitService(deployed_layers=…)` 提供，Orchestrator 从部署政策算出后传入 |
| 系统默认策略（改图 `add_task` 缺省、合成 Task 模板缺省） | 按部署层过滤，关闭时去掉 `code_test` |
| 冲突（§14.4） | 冲突 Task 的成功条件本身就要求在本机跑探针测试（`planning/manager.py` 的 `pytest:<仲裁目录>/test_probe.py`）。如果关闭时只去掉 code_test，就会出现"写了探针却从没运行"的假证据，因为规则层只检查证据里有没有引用 `pytest:`。所以**关闭时不创建冲突 Task**，改走已有的 DEFERRED 路径（D4-19），`deferred_reason = "local_code_execution_disabled"`；有争议的 Claim 保持 DISPUTED，不进入正式知识。人工仲裁仍然可以从"判定分歧"这条路径进入（`event_handler.py` 的 topic `judgment`，不需要执行代码） |
| 验证路由 | 旧 Mission 可能在开关打开时建过含 `code_test` 的 Task，这类 Task 的该层记为 `ERROR` "本部署关闭了本机代码执行"（`undeployed: true`），不能伪装成 PASS 或 NOT_REQUIRED |
| Mission 判定（`pytest:` 条件） | 不跑 pytest；该条判定为 `met=false`，原因写明"本机代码执行已关闭" |
| 工具网关 `run_tests` | 纵深防御：即使 Task 的工具集里有，也拒绝执行，并按 `on_rejected` 记入时间线 |
| Planner / Manager 提示 | 输入包新增 `deployed_verification_layers`。模板升为 `planner-v4` / `manager-v2`：只能从这个列表里选验证层；列表不含 `code_test` 时，不能写 `pytest:` 条件。旧的 `planner-v3` / `manager-v1` 保留登记，已有库的 ACTIVE 策略（seed 时钉了旧版本）照旧可用 |

### 2.2 编排感知的创建入口

- 新增 `Orchestrator.create_mission(*, tenant_id: str, request: Mapping) -> tuple[Mission, bool]`，依次执行：
  1. 用与 `MissionApi.create` 相同的解析规则把请求变成 `MissionSpec`；
  2. `validate_spec(spec, available_tools=部署政策的 allowed_tools)`；
  3. `_check_action_criteria`：未启用的连接器或不允许的操作，直接拒绝；
  4. 本机代码执行关闭时，拒绝 `pytest:` 条件；
  5. 调用 `commit.create_mission(spec, provider_kind=…, policy_defaults=…, policy_pin=…)`。
- `submit_mission` 改为共用第 2 到第 5 步。
- `MissionApi.__init__(commit, *, orchestrator=None)`：传入 orchestrator 时，`create` 走上面这条入口，拒绝统一包装成 `MissionRequestError`。
- CLI `mission create` 改为 `MissionApi(orchestrator.commit, orchestrator=orchestrator)`。

### 2.3 版本

- simple_harness 0.9.8，agent_orchestrator 0.9.1。
- `simple_harness` 的执行库 schema 不变，仍是 10。

## 2.4 S2：P3.1 外部控制面（0.9.9 / agent_orchestrator 0.9.2）

依据：用户的 Phase3 计划（Host `plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md`）§3.3–§3.4，以及 P3.1-A01 至 A08。Host 目前在进程内直连 SDK，按 P3.1"只选实际主路径"的要求，把直连路径收敛到同一个 Facade 与合同上，不改 Service SDK。

- `api/facade.py`，`MissionControlV1(orchestrator, tenant_id)`：
  - `create(command)`：严格映射字段。
    - 开放的字段：`goal`、`success_criteria`、`idempotency_key`、`budget.max_tokens` / `budget.max_attempts`、`stop_conditions`、`untrusted_sources`、`synthesis`、`conflict_reserve_tokens`、`workspace_seed`。
    - 未知字段一律拒绝。
    - 暂不开放的字段（`allowed_tools`、`risk_level`、`task_kind`、金额预算）明确报错，不静默忽略（P3.1-A06）。
    - 内部走 `Orchestrator.create_mission`。
    - 返回持久回执 `{mission_id, created, spec_hash}`。同一个 key 且内容相同时返回原回执；内容不同则报 `conflict`（P3.1-A03）。
  - `snapshot(mission_id)`：返回 `MissionViewV1 {mission_id, through_seq, graph_version, state_version, snapshot}`，快照与游标在同一个读事务里取得（P3.1-A05）。
  - `events(mission_id, after_seq, limit ≤ 200)`：返回 `{events, through_seq, has_more}`。
  - `cancel(mission_id)`：幂等。
  - `artifact_read(artifact_id)`：按不可变 id 读取；先核对文件 hash 与记录一致；只读文本，有大小上限。不接受本机路径。
  - `decide(request_id, …)` 与 `comment(target_id, text)`：经 `ApprovalApi`；调用方的 Principal 在构造时固定。
- 归属：每次读写都先检查 `mission.tenant_id == tenant_id`；不符合就一律报 `not_found`，不泄露对象是否存在（P3.1-A04）。审批与产物按它所属的 Mission 判断归属。
- `Store.read_view()`：只读的一致性读取，内部是一个 DEFERRED 事务，只用于 SELECT，不跨 await。
- 测试：`tests/orchestrator/host_support/test_facade.py`，覆盖 P3.1-A03 至 A06 的 SDK 部分。

## 3. 切片

| 片 | 内容 | 测试 |
|---|---|---|
| A | 测试先行：`tests/orchestrator/host_support/`（SA-1 至 SA-6） | 此时应当红 |
| B | 部署开关与五个位置 | SA-1 至 SA-4、SA-6 |
| C | 创建入口、MissionApi、CLI | SA-5 |
| D | 版本号、全量回归、wheel、独立代码评审与处置、记录、推送 | SA-7 |
| E | 代码评审第 1 轮的源码修改与补测（journal §4） | 新增的决定性测试 |
| F（S2） | P3.1 外部控制面 `api/facade.py`、`Store.read_view()`，版本 0.9.9 / 0.9.2；随后全量回归、构建 wheel、代码评审、推送 | Host acceptance §A2 SB-1 至 SB-6 |
