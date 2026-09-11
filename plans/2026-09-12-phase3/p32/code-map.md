# P3.2 代码地图（2026-09-12，只读核查，Explore 子代理产出，原文为中文）

路径缩写：`AO` = `src/agent_orchestrator`，`SH` = `src/simple_harness`，`T` = `tests`。

## 1. 模型写的代码 / 测试在哪里执行

- **执行入口**：`AO/runtime/tool_gateway.py:109-141` 的 `run_pytest`。它用宿主解释器跑 `sys.executable -m pytest -q -p no:cacheprovider --color=no [-- path]`，工作目录是工作区根目录。
  - 环境变量只放行 `ENV_WHITELIST`（`:62`），但这份白名单**包含 HOME**、PATH、TMPDIR 等；另外再加上 `PYTHONDONTWRITEBYTECODE`、`PYTHONHASHSEED`。
  - 用 `start_new_session=True` 让子进程独立成组；取消或超时时 `os.killpg(pid, 9)`。stdout 与 stderr 合并。
- **现有隔离的缺口**：
  - 输出没有上限：`communicate()` 会把全部输出读进内存，直到 `TestRun.to_json` 才截取最后 8000 字符（`:100-106`）；
  - 与宿主同一个 uid，共用宿主的 site-packages；
  - 没有网络、CPU、内存、PID 的限制，文件头 `:13` 也写明不承诺网络隔离。
- **调用点**：
  - Worker 工具 `run_tests`：`tool_gateway.py:329-349`；
  - `code_test` 验证层：`AO/verification/deterministic_checks.py:180-203`；
  - Mission 级 `pytest:` 判定：`AO/orchestrator/event_handler.py:3344-3370`；
  - 评测 oracle：`AO/observability/evaluation.py:288-316`；
  - 冲突仲裁的探针没有自己的执行路径，走的是 `code_test`。
- **`local_code_execution` 门控**：
  - 定义：`AO/governance/policies.py:46-59`；
  - `deployed_layers`：`:142-149`；
  - 传给 gateway：`assembly.py:370-374`；
  - VerifierRouter 的处理：`verifier_router.py:212-219`；
  - Mission 入口：`event_handler.py:775-784`；
  - 冲突转 DEFERRED：`commit_service.py:1716-1719`。

## 2. Connector 协议

- **协议定义**：`AO/runtime/connectors.py:130-146`，包括 `name`、`operations`、`supports_idempotency`、`supports_reconciliation`，以及 `normalize_target`、`execute(...)->Receipt`、`lookup(key)->Receipt|None`。
- **None 的含义**：协议里**没有写 lookup 返回 None 代表什么**。执行器（`AO/runtime/actions.py:133-145`）把 None 当作 `CONFIRMED_NOT_STARTED`，允许用同一个 key 再交接一次；抛异常、超时、或 `supports_reconciliation=False` 时，则记为 `STILL_UNKNOWN`。
- **错误分类**：`ConnectorRejected` 表示明确拒绝，结果为 FAILED；`ConnectorTransportError` 或其他异常，结果为 UNKNOWN（`actions.py:84-96`）。
- **`OperationSpec`**：`:52-73`，包含 level、required_params、mutates、kind、cost_micros_ceiling。
- **`Receipt`**：`:76-127`，包含 key、connector、operation、target、params_hash、applied、before、after、service_ref、applied_at、receipt_hash。
- **`TestConfigService`**：`:149-265`。
  - 用一个 JSON 文件同时存状态和 ledger，加锁用 flock，写入方式是 tmp 文件再 replace；
  - read / set / delete 三个操作分别为 L0 / L2 / L3；
  - 支持两种故障注入：`lose_receipt_after_apply`、`reject_next`。
- **`PaymentConnectorStub`**：`:268-287`，只用于拒绝测试。
- **注册方式**：没有注册表。由 `Orchestrator(connectors=dict)` 传入，并由 `DeploymentPolicy.enabled_connectors` 决定是否启用（`policies.py:36`，在 `action_decision` 里检查）。facade 不涉及 connector。
- **协议里没有 `publish` 方法**，只有 `execute`。

## 3. 动作管线

- **候选文件**：`actions/<name>.json`（`action_commits.py:148-151`）。字段固定为 connector、operation、target、params、reason，多出的字段一律拒绝（`:68-84`）。
- **Mission 范围**：来自成功条件 `action:<connector>.<operation>:<target>`（`:87-118`）；`check_candidate` 在 `:121-145`。
- **强制规则检查**：结果里带 `actions/` 时，一定跑 rule_check（`event_handler.py:2182-2210`）。
- **级别与审批数**：级别由 `policies.py:94-126` 的 `action_decision` 决定；所需审批数由 `permissions.py:74-77` 决定，L2 需 1 人，L3 需 2 人。
- **提议**：`propose_action` 在 `:212-375`，在 accept 事务里执行；幂等键为 `{action_id}:v{version}`。
- **审批**：`decide_approval` 在 `:424-544`，`revoke_approval` 在 `:546`，`expire_approvals` 在 `:610`。
- **交接**：`begin_handoff` 在 `:629-720`，`_handoff_refusal` 在 `:722-772`。
- **执行**：`ActionExecutor`（`actions.py:28-154`）。
  - 执行结果：回执与动作不匹配时判为 UNKNOWN（`record_action_outcome`，`:828-853`）；
  - 核对：`reconcile` 配合 `record_reconciliation`（`:855-905`）；
  - 每个动作最多交接 2 次；
  - 人工裁定 UNKNOWN：`override_action_outcome`（`:907-958`）。
- **补偿**：**没有任何实现**，只有 `cancel_open_actions`（`:1051`）。
- **target 规范化**：`_normalize`（`:102-104`）会调用 connector 的 `normalize_target`，但 TestConfigService 只做去空白，没有 canonical path 这一层。

## 4. Attempt 工作区（`AO/artifacts/workspace.py`）

- **目录位置**：`<evidence_root>/workspaces/<attempt_id>/`，验证用副本在 `-verify/`。
- **路径解析**：`resolve` 拒绝绝对路径、`..` 和末级 symlink；单个文件上限 512KiB。
- **创建**：`create` 在目录已存在时直接返回，不做任何校验；重试时复制上一次 Attempt 的树，写入 seed，并按字节拷贝上游输入（调用方负责核对 sha256）。
- **受保护的文件与只读输入**：由 `event_handler.py:2242`、`:553` 算出，交给 gateway 强制执行（`tool_gateway.py:253-289`）。
- **`snapshot`**：产出 Artifact；其中 `storage_uri` 就是工作区里的文件路径。
- **缺失**：没有独立的 workspace ID，没有 base snapshot，没有持久化的登记，**也没有任何清理**。

## 5. [R13] 召回原文以 SYSTEM 角色进入 provider 消息

- **构造位置**：`SH/agents/runtime.py:342-395` 的 `_RecallAdapter.__call__`，把召回的原文拼成 `"[会话召回 seq …]\n{text}"`，构造成 `Message(MessageRole.SYSTEM, …)`（`:379-389`）。
- **接入位置**：`:211-219`；`SH/agents/context/port.py:205-245` 把它插在开头那段 SYSTEM 消息之后。
- **编排器也受影响（推断）**：`AO/runtime/assembly.py:382` 没有设置 `recall_limit`，默认值是 6（`SH/agents/ports.py:78`），所以编排器的 Agent 应该也开着召回。编排器自己的 Context 包以 USER 角色发送（`agent_worker.py:162-163`）。
- **测试缺口**：现有测试（`T/agents/test_session_memory.py:227-254`）只断言召回消息 `derived=True`，**不检查 role**。

## 5.1 本机隔离手段探测（2026-09-12）

本机为 macOS 26.6。没有装 docker、podman、colima、lima。系统自带的 `/usr/bin/sandbox-exec`（seatbelt）可用，三项探测结果：
- `(deny network*)`：curl 连不上，返回 000；
- `(deny file-read* (subpath HOME))`：`ls ~/Library` 报 Operation not permitted；
- `(deny file-write* (subpath "/"))`，只放行工作区与 `/private/var/folders`：在工作区里能写，在工作区外写被拒绝。

据此，P3.2 在 Host 上第一个部署的隔离适配器，候选方案是 seatbelt，配合以下几项：
- 资源限制（`setrlimit`：CPU、进程数、文件大小）；
- 独立进程组，并能整组杀掉；
- 输出有上限；
- 清理环境变量，HOME 指向工作区内的临时目录。

限制：没有管理员权限，无法切换到独立的 uid，只能用当前的非 root 用户运行。这一点要在计划和 ARCHITECTURE 里如实写明。

**spike（2026-09-12，scratchpad，一次性，不入库）**：在 seatbelt 里用 SDK venv 的 python 真实运行 pytest。

- 第一次失败：`execvp() … Operation not permitted`。原因是 `(deny file-read* (subpath HOME))` 把 HOME 下目录的元数据读取也拦掉了，而 venv 的 python 是指向 `~/.local/share/uv/python/…` 的软链，解析路径时需要这些元数据。
- 第二次改用以下规则后成功：
  - `(deny file-read-data (subpath HOME))`，只拦文件内容，放开元数据；
  - 对 venv prefix、base prefix、SDK `src`、工作区四处单独放行 `file-read-data`；
  - `(deny network*)`；
  - `(deny file-write* (subpath "/"))`，只放行工作区、`/private/var/folders`、`/dev/null`、`/dev/tty`；
  - 环境变量用 `env -i` 清空，只保留 PATH、HOME（指向工作区内的 `.home`）、TMPDIR（指向工作区内的 `.tmp`）。
- 第二次的结果：4 条测试全部通过。其中 3 条是越界探针，都得到了预期的拒绝：
  - 读 `~/.ssh` 被拒；
  - 写 `~/sbx-escape.txt` 被拒，事后确认这个文件不存在；
  - 连 `1.1.1.1:443` 失败。

启示：规则应当拦"读取内容"，而不是拦全部读取；解释器和依赖所在的目录必须明确放行，范围由部署给出，不能把整个 HOME 放开。

## 6. 现有测试

| 方面 | 测试文件 |
|---|---|
| 执行与门控 | `T/orchestrator/step02/test_workspace_and_gateway.py`；`T/orchestrator/host_support/*`；`T/orchestrator/step06/test_governance.py` |
| 工作区 | `T/orchestrator/step06/test_workspace_isolation.py` |
| 动作 | `T/orchestrator/step07/test_action_ledger.py`、`test_action_execution.py`、`test_approvals.py`、`test_approval_action_closure.py`；`T/orchestrator/step08/test_evaluation.py` |
| SDK effect 层 | `T/conformance/test_reconciliation_contract.py`、`T/integration/execution/test_effect_reconcile.py` |
