# 代码评审第 2 轮：S2 外部控制面与第 1 轮修复（独立评审，opus，只读）

- 日期：2026-09-11
- 对象：
  - `git diff 10cfffe 8444a39`：第 1 轮评审后的修复；
  - `git diff 8444a39 4bd8c52`：S2 `MissionControlV1`、`Store.read_view` / `last_event_seq`、`spec_from_request` 的字段映射。
- 结论：**SHIP_WITH_FIXES**
- 处置：见 `../journal.md` §4.2

## 1. 总评

- **第 1 轮修复**：P1-1、P1-3、P2-3、P2-4、P2-5 基本正确。P1-1 在三个关卡和入口都已拦住（`task_graph.py:282`、`changes.py:529`、`commit_service.py:767`、`event_handler.py:774-785`）。
- **单一写入者**：facade 的写入都经过 `Orchestrator.create_mission`、`commit.cancel_mission` 或 ApprovalApi，没有直接写库。
- **需要修的三处**：
  - `synthesis` 能绕过严格字段校验；
  - `read_view` 没有禁止写入，出异常时仍会提交半截写入；
  - 验收要求的 SB-3、SB-5 测试不全。

## 2. 发现

### P1

**P1-A　`synthesis` 让已关闭的字段和未部署的层绕过严格校验**（`facade.py:49,146-173`；`manager.py:101,113-116`；`commit_service.py:898-917`）

- **问题**：模板内容完全不做校验。`allowed_tools`、`budget.max_cost_micros`、`formal_check`、任意键都能夹带进来，模板里的文字也不做密钥扫描。
- **实测**：`validate_spec` 接受了一个带 `run_tests`、连接器工具、`max_cost_micros=1e12`、`formal_check` 的模板。
- **后果**：
  - 模板预算超过 Mission 预算时，图提交阶段 `open_account` 抛 `BudgetError`（`budgets.py:137`），规划失败；
  - 未部署的层到验证时才报 ERROR；
  - 多出来的工具在运行时被交集静默丢掉（`policies.py:138`），违反 A06。
- **建议**：
  - `_strict` 给模板定子键白名单：goal、success_criteria、rationale、outputs、verification_policy、priority、`budget.{max_tokens, max_attempts}`；已关闭的子键按名字拒绝；
  - 入口处再核对：`verification_policy ⊆ deployed_layers`；预算不超过 Mission；goal 和 criteria 是非空字符串；
  - 对模板文字做密钥扫描和动作条件检查。

**P1-B　`read_view` 不禁止写入，出异常时照样 COMMIT**（`store.py:405-424`）

- **可复现的问题**：
  - 视图里调用 `transaction()` 会走嵌套分支，既不 `BEGIN IMMEDIATE`，也不回滚；视图的 `finally` 又无条件 COMMIT。在内存库上复现：视图里写了一行后抛异常，这一行仍被提交。
  - COMMIT 写在 `finally` 里，且排在重置 `_depth` / `_holder` 之前。一旦 COMMIT 失败，`_depth` 会一直停在 1，此后所有 `transaction()` 都退化成自动提交，整个进程失去原子性。
  - 嵌套分支没有做 holder 检查。
- **当前影响**：facade 在视图内调用的 `snapshot`、`list_approvals`、`waiting_on`、`get_mission_policy`、`has_table` 都只读，所以目前是潜在风险，还没有真实触发。
- **读一致性本身成立**：WAL 下，DEFERRED 事务的读快照在第一条 SELECT 时固定。
- **建议**：
  - 视图期间在 `transaction()` 入口直接抛 StoreError；
  - 出异常时 ROLLBACK；
  - 把重置放进内层 `finally`，保证不论 COMMIT 成败都会执行。

**P1-C　验收场景缺少决定性测试**（`test_facade.py:150-240`）

- SB-3 缺：decide、artifact_read、takeover；以 Task 或审批为目标的 comment；`approvals(None)`。
- SB-5 缺：截断测试。
- SB-2 缺：断言"不重复预留预算"。
- SB-1 缺：`synthesis`、`stop_conditions`、`budget` 的回读。
- SB-4 的测试在 Mission 结束后才取快照；删掉 `read_view` 也照样通过。建议在 `snapshot` 的两次 SELECT 之间，用第二个连接插入一条事件，断言 `through_seq` 不包含它。

### P2

1. **字符串被拆成单个字符**（`missions.py:67,70-72`）：
   - `success_criteria` 传字符串时会被拆成单字符元组，`validate_spec` 仍然接受；
   - `stop_conditions` 的取值没有校验；
   - 建议要求 `list[str]`，`stop_conditions` 限定在允许集合内，`workspace_seed` 要求 `Mapping[str, str]`。
2. **artifact_read 存在检查与读取之间的竞态**（`facade.py:335-336`）：
   - 先算 hash 再读内容，中间文件可能已被改；
   - 整个文件读进内存，非 UTF-8 内容被静默替换；
   - 建议一次流式读取，边算 hash 边保留前 MAX+1 字节。
3. **cancel 的终态判断在事务外**（`facade.py:177-180`）：竞态时 IllegalTransition 会漏出去。建议把幂等判断挪进 `cancel_mission` 的事务内。
4. **错误映射有漏洞**：
   - decide、takeover、comment 没有捕获 StoreError / CommitRejected / ContractError；
   - takeover 没有捕获 ApprovalRequestError；
   - comment 没有捕获 ActionCommitError；
   - create 没有捕获 BudgetError。
5. **旧 Task 的 `pytest:` 条件只在策略里有 rule_check 时才会 FAIL**（`verifier_router.py:92-94`）：一个"format_check + critic_review"策略的旧 Task，靠 Critic 给出 PASS 就能完成。建议关闭时强制把 rule_check 加入必需层。
6. **seq 是全库自增**：分页本身没有缝隙，但跳号会暴露其他租户的事件量。至少在文档里写明。
7. **`_clean` 不扫 `workspace_seed`**：这部分内容会进入 Worker 上下文。另外，`snapshot` 会把本机的 `storage_uri` 暴露出去。
8. **P2-6"不采纳"的理由写得不准**：用 `None` 哨兵其实可以区分"没传"和"显式传了默认值"。保守地直接报错可以接受，建议改写措辞。另外，`task_graph.pytest_criteria` 这个辅助函数已经有了，另外三处仍在内联同样的判断。

`_owner_of` 按 id 前缀区分，各前缀互不重叠，不会误判；not_found 的报错文字统一，不带 id。

## 3. 评审者运行过的命令

- `pytest tests/orchestrator/host_support`：36 passed，用时 3.50 s。
- 三段只读的 Python 验证：
  - 字符串被拆成字符；
  - `synthesis` 夹带已关闭字段仍被接受；
  - 视图内写入后抛异常，写入仍被提交。
