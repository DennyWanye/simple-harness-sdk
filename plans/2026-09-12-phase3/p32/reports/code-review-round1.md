# P3.2 代码评审 · 第 1 轮

**结论：SHIP_WITH_FIXES**（P0 0 条 / P1 5 条 / P2 13 条；最关键的一条是补偿发布的文件名与原动作完全相同，D8 的核心场景走到连接器一定被判 `conflict`）

评审范围：`4981722`（切片 B）、`a18e785` + `3da2958`（切片 C）、`81f8016`（切片 A + D），以及工作树里未提交的切片 E 改动。
参照：`plan.md`（第 3 版）、`acceptance.md`（第 3 版）、`journal.md` §1/§1b/§2.3–§2.8，术语按 `agent-orchestration-theory/`。

---

## 一、已跑的检查

| 检查 | 命令 | 结果 |
|---|---|---|
| 提交范围 | `git log --oneline -10`、`git diff --stat 4981722~1 -- src tests`、`git diff --stat` | 40 个文件、+4392/−210；未提交的切片 E 为版本号 0.10.0、CHANGELOG、`PLAN_CONFIG` 加 `min_task_tokens`、`__main__` 加 `max_planning_attempts=3`、两处 docstring、public-api.json 版本 |
| ruff（改动文件） | `ruff check <9 个 src 文件> tests/orchestrator/p32/` | **All checks passed** |
| ruff format | `ruff format --check src/agent_orchestrator tests/orchestrator/p32` | **93 files already formatted**（仓库全量有 45 个待格式化文件，全部在本次范围之外，属既有状态） |
| mypy | `.venv/bin/mypy`（pyproject 里 scoped 到 `src/agent_orchestrator`） | **3 errors in 2 files**，三条全部落在本次新增/重写的行上（见 P1-4） |
| 文件名生成 | 直接调用 `_name_for()` 比对原动作与补偿 | 两者产出**同名文件**（见 P1-1） |
| 读白名单 | 直接构造 `SeatbeltExecutor.for_interpreter()` 打印 `base_read_paths` 与 profile | 13 条，其中 7 条在真实家目录之下（.venv、uv 的 cpython、仓库 `src`）；`(literal "/")`、`(deny mach-lookup)` 等规则与计划一致 |
| 回执限额措辞 | 直接调用 `SandboxSpec().effective_limits()` | `network = {"value": "none", "enforcement": "hard"}`，**process_only 回执同样这么写**（见 P1-3） |

按约束**没有跑 pytest**（主进程在跑全量回归）。实测只在 `/private/tmp/p32-cr1-*` 下做，已删除；没有留下 `sandbox-exec` 进程或 `orch-exec-*` 目录。仓库文件除本报告外未改动，未提交 git，未读取 `.env` 与 credentials，未调用真实模型。

---

## 二、发现

### P1-1　补偿发布的最终文件名与原动作完全相同，D8 的核心场景必然被拒

**位置**：`src/agent_orchestrator/runtime/connectors_publish.py:60-69`（`_name_for`）；`src/agent_orchestrator/orchestrator/action_commits.py:383-424`（`propose_compensation` 的 `action_id` 与 `target` 生成）

**问题**：`_name_for` 先按 `":v"` 切出版本号，再对前半段按**第一个 `-`** 切，取后 12 位十六进制。补偿的 action_id 是 `<根 action_id>#comp-<n>`，`#comp-1` 挂在字符串末尾，被 `[:12]` 直接截掉：

```
_name_for("action-2f8a1c4d9e0b7766:v1",        "reports/r.md") -> r.2f8a1c4d9e0b.v1.md
_name_for("action-2f8a1c4d9e0b7766#comp-1:v1", "reports/r.md") -> r.2f8a1c4d9e0b.v1.md   # 同名
```

`propose_compensation(target=None)` 默认沿用 `original["target"]`，所以 stem、后缀、hex、版本号四项全同。

**后果**：计划 D6/D8 与验收 P32-8 的主场景——"已经 SUCCEEDED 之后要改内容，必须走补偿"——走到 `FilePublishConnector.execute` 时，`_existing` 会读到原动作那份文件，内容不同，抛 `ConnectorRejected("conflict: … exists with other content")`。补偿在 SDK 账本层能立起来、能审批，但**永远发布不出去**。这直接影响 Phase3 §4.1 用户场景第 5 步和 A04。

**建议**：文件名里必须带进动作的完整身份。最省的改法是不要按 `-` 切字符串，直接对 `idempotency_key` 整体取 `sha256(...)[:12]`（补偿与原动作的 key 本来就不同），版本号仍取 `:v` 之后那段；或者在 `#comp-n` 存在时把它拼进名字。修完补一条"补偿经过 FilePublishConnector 真正落一个新文件"的测试（现在没有，见 P2-11）。

---

### P1-2　`execute()` 把 `PREPARED` 当成已完成，返回 `applied=True` 却从不核对文件

**位置**：`src/agent_orchestrator/runtime/connectors_publish.py:194-196`

```python
done = self._entries(idempotency_key)
if done and done[-1].get("state") != "ABORTED":  # this version already reached the link
    return self._receipt(done[-1])
```

**问题**：这一句把"末条不是 ABORTED"等同于"已经挂上链"。但模块自己的真值表（同文件 12-18 行、`lookup` 268-286 行）说得很清楚：末条是 `PREPARED` 而文件不在，**恰恰**是"进程崩在中间、不确定"。`execute` 这条路径既不读文件也不核 hash，直接按 intent 重建一份 `applied=True` 的回执。

**后果**：任何一次"同 key 再次执行"，只要上一次是在写完 intent 之后被 SIGKILL（不是抛异常，所以没补 ABORTED），系统就会拿到一份"发布成功"的回执并把动作记成 SUCCEEDED，而目标目录里什么都没有。即使当前派发路径总是先 reconcile（`lookup` 会抛 `ConnectorTransportError` → STILL_UNKNOWN → 转人工）能挡住，这也是把不变量放在调用方身上；`begin_handoff` 允许一次 re-handoff（`MAX_HANDOFFS_PER_ACTION = 2`），一旦某条路径跳过 lookup 就会命中。同一个洞还有个当场可见的版本：两次 `execute` 之间文件被用户删掉，第二次仍然返回"已发布 + 路径"。

**建议**：这条快路径改成走和 `lookup` 一样的判定——末条 COMMITTED/PREPARED 时先 `_existing` 核 hash，相符才返回回执，不符或缺失就抛 `ConnectorTransportError`（让它停在 UNKNOWN，而不是谎报成功）。

---

### P1-3　`process_only` 的回执声称 `network = none / hard`

**位置**：`src/agent_orchestrator/runtime/sandbox.py:87-96`（`SandboxSpec.effective_limits`）、`336-350`（两个适配器共用它填回执）

**问题**：`effective_limits()` 挂在 `SandboxSpec` 上，两个适配器共用。ProcessOnly 不做任何网络限制，但它的回执里照样写着 `"network": {"value": "none", "enforcement": "hard"}`（已实测确认）。

**后果**：这正是计划 §2 与评审要求避免的那类措辞——把没做的事写成硬限制。SDK 默认就是 `process_only`，Host 界面与部署清单会直接展示这份回执；`isolated=false` 只说明"没有隔离"，不足以抵消一条明写着"网络=none，硬限制"的字段。

**建议**：`effective_limits` 接一个 `isolated`（或由适配器覆写），非隔离模式下把 `network` 写成 `{"value": "unrestricted", "enforcement": "none"}`，`max_file_bytes`/`cpu_seconds` 这两项 `setrlimit` 的仍可保留为 hard。顺带在 `test_receipt_shape_and_limits_are_labelled` 里断言这一项。

---

### P1-4　mypy 由绿转红 3 条，其中一条是运行期必崩的 `TypeError`

**位置**：`src/agent_orchestrator/runtime/connectors_publish.py:67`；`src/agent_orchestrator/artifacts/workspace.py:328`、`:378`

```
connectors_publish.py:67: Argument 1 to "_hash" has incompatible type "str"; expected "bytes"
workspace.py:328: Incompatible types in assignment (expression has type "Path | bytes", variable has type "Path")
workspace.py:378: Need type annotation for "removed"
```

**问题**：三条都在本次新增/重写的行上（`connectors_publish.py` 是新文件；`workspace.py:328` 是新的重建分支，用 `source` 覆盖了外层同名的 `Path`；`:378` 是新的 `sweep_exec_copies`）。第一条不只是类型噪声：`digits = (digits or _hash(idempotency_key))[:12]` 这条兜底在 `idempotency_key` 不含 `-` 时会真的执行，`_hash` 只接受 `bytes`，当场 `TypeError`。

**后果**：`pyproject.toml` 把 `src/agent_orchestrator` 整个列为 release-owned 的类型检查面，交付门禁（P32-12）不该带着红的 mypy 出门；兜底分支一旦被非标准 idempotency key 命中会直接崩在发布路径上。

**建议**：`_hash(idempotency_key.encode("utf-8"))`；`workspace.py:328` 的循环变量改名（`for relative, item in …`）；`removed: list[str] = []`。

---

### P1-5　`_reap` 对可能已被回收的 pid 调 `killpg`，每次执行都跑一遍

**位置**：`src/agent_orchestrator/runtime/sandbox.py:325`、`358-375`

**问题**：主循环在 `waiter.done()` 时跳出，而 `process.wait()` 返回意味着 asyncio 的 child watcher **已经 waitpid 收走了这个子进程**，`run.root_pid` 此刻已经是一个空闲 pid。紧接着 `residual = await asyncio.to_thread(self._reap, run)`（跨线程调度，窗口是毫秒级）里无条件执行：

```python
os.killpg(run.root_pid, signal.SIGKILL)
_sigkill(run.root_pid)
```

**后果**：pid 被复用时，这会 SIGKILL 掉本用户一整个**无关的进程组**。这条路径不是只在超时/超限时走，**每一次正常结束的执行也走**（`run_tests`、`code_test`、`pytest:`、oracle 全部经过），暴露面是每次执行一次。概率低，后果重（用户自己的编辑器、另一次 pytest 都可能在那个组里）。

`ProcessOnlyExecutor._survivors`（441-449 行）里的 `run.seen` 也有同一类问题：早先采样到的后代 pid 退出后被复用，会被当作 survivor 杀掉。计划里"曾见后代集合"这个做法本身带这个风险，但 journal 没登记。

**建议**：`_reap` 里把 `killpg`/`_sigkill` 收窄成"只在 waiter 尚未完成时执行"（正常结束时进程组本来就空了，根本不需要杀）；`run.seen` 记录时连 `(pid, 启动时间)` 一起记，或在杀之前用 `ps -o lstart=` 复核。至少要把这条剩余风险登记进 journal 与 §2 诚实边界。

---

### P2 清单

| 编号 | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `sandbox.py:398-411`（`_Run.start` / `close`） | **计划 D1「回收」第 3.iv 条"宿主启动时，按还没清理的 execution_id 扫一次"没有实现**，journal §2.6 也没登记这处偏差。`close()` 在 `finally` 里删掉 marks 目录，所以宿主崩溃后金丝雀/诱饵一起消失，逃出去的沙箱进程**再也认不出来**（`sweep_exec_copies` 只删目录，不扫进程） | 要么实现启动时扫描（marks 目录保留到扫描之后再删），要么把"宿主崩溃时逃逸进程无法回收"如实登记进 §2 与 journal |
| P2-2 | `connectors_publish.py:154-159`（`_append`） | **计划 D6 明写"写入时加 flock 并 fsync"，实现只有 append + fsync，没有 flock**，未登记。现在靠"connector 独占账本"这个隐含假设撑着 | 加上 `fcntl.flock`，或把"单写者"写进 docstring 与 journal |
| P2-3 | `connectors_publish.py:211-216` | 计划 D6 说"目标文件已存在：内容相同**并且 intent 属于同一个 key**，按 COMPLETED 处理"。实现只比内容，不看 intent 归属，然后补一条 COMMITTED 把别人的文件认领成自己的成果 | 按计划补上 key 归属判断；未登记的偏差 |
| P2-4 | `action_commits.py:150-181`（`bind_artifact_params`） | 守卫是**有条件的**：`if path is None: return bound` 在检查 `BOUND_ARTIFACT_FIELDS` 之前就返回。也就是说模型只要不写 `artifact_path`，就能自由地写 `content_hash` / `storage_uri` / `size`。对 `FilePublishConnector` 恰好封死（它的 `required_params` 含 `artifact_path`，缺了会在 execute 时被拒），但这是靠连接器的必填参数兜住的，不是靠这道守卫 | 把 `set_by_model` 的检查提到早返回之前，无条件生效 |
| P2-5 | `sandbox.py:199-204`、`333-334` | `RLIMIT_CPU` 是**每进程**的，不是整次执行的总量；`limit_exceeded == "cpu"` 只在**根进程**被 SIGXCPU 打死时才置上，孙进程烧 CPU 不会被识别。docstring 与 CHANGELOG 都把 CPU 写成"hard limit"，容易被读成整次执行有上限（真正兜底的是 `wall_seconds`） | 在 docstring / CHANGELOG 里点明"每进程"，或按沙箱身份汇总 CPU |
| P2-6 | `sandbox.py:110`、`tool_gateway.py` | 计划 D1 里 `limit_exceeded` 取值含 `output`，实现的注释与代码都**只有 cpu / rss / processes**，`output` 永远不会出现。未登记 | 去掉计划里的 `output`，或补上；同时修 P2-12 那条测试断言 |
| P2-7 | `tool_gateway.py:118`（`run_pytest`） | `runner = executor if executor is not None else ProcessOnlyExecutor()`——**`None` 被当成 process_only，而不是 off**。目前四个调用点都在上游另行判了 `local_code_execution`（event_handler:3551、verifier_router、gateway、evaluation 显式判 `executor is not None`），所以现在是安全的，但语义上"关掉执行"和"没传执行器"被合并成了同一件事 | `executor=None` 时直接抛 `SandboxUnavailable`，让"off"只有一种表达 |
| P2-8 | `tool_gateway.py:15`（模块 docstring）、`:60`（`ENV_WHITELIST`） | docstring 仍写着"environment whitelist"，而环境变量现在由 `sandbox.py` 显式构造；`ENV_WHITELIST` 已成死代码（只在 `__all__` 里被引用） | 删掉常量、改写 docstring（顺带说明隔离与否由回执的 `isolated` 决定） |
| P2-9 | `workspace.py:55-73`（`scan_symlinks`） | 目录名命中 `IGNORED_DIRS` 时先 `continue` 再判 `is_symlink()`，所以一条名叫 `__pycache__` / `.git` / `.pytest_cache` 的**软链不会被报出来**。目前无害（这些名字在 `_copy_tree` 与 `_walk` 里也被忽略，不会被复制或登记），但"扫到软链就拒绝"这条不变量在这里有例外 | 先判 `is_symlink()` 再判忽略名 |
| P2-10 | `event_handler.py:1355-1370` | 收养分支只判 `.exists()`。若 `<workspaces>/<attempt_id>` 是一条软链，`exists()` 为真 → 收养 → `create()` 因 `root.exists()` 直接复用 → 整个工作区落在软链目标里。默认 `process_only` 下模型代码本来就能写任意位置，所以不算新增暴露，但纵深防御在这里断了 | 收养前加 `is_symlink()` 判断，命中就按 `workspace_identity_mismatch` 拒绝 |
| P2-11 | `event_handler.py:314-317` | 每次 `__aenter__` 都 `backfill(self._store.list_all_artifacts(), …)`，把**全部** artifact 行读进内存。Mission 多了之后每次启动都要全表扫一遍 | 迁移做成一次性标记（比如按 schema 版本或一行 marker），或只扫 `storage_uri` 不在 CAS 下的行 |
| P2-12 | `sandbox.py:362-374` | `_reap` 在每次执行结束时最多跑 6 轮，每轮一次 `ps -A`（seatbelt 还要对本 uid 的每个进程调一次 `sandbox_check`）+ 50 ms sleep。正常结束的执行本来没有 survivor，却要付这份开销 | 先扫一次，空了就直接返回（现在第一轮就会返回，但仍付了一次 `ps`+killpg；结合 P1-5 一起改） |
| P2-13 | 计划 §5 与 journal | 计划 §5 要求把四条剩余风险（seatbelt/`sandbox_check` 已废弃、软限制、无 uid 隔离、元数据可读）"写进 §2 与 **ARCHITECTURE**"。仓库里没有 ARCHITECTURE 文档，`docs/` 下也搜不到 seatbelt / process_only 的任何说明；这些话目前只活在 plan 与 CHANGELOG 里 | 切片 E 里补进面向使用者的文档（`docs/integration-guide.md` 或 README 的部署一节），否则集成方看不到这些边界 |

---

## 三、测试质量

**真能证伪的**（抽查了断言的"反向"是否会红）：

- **"没有残留进程"**——`test_p32_sandbox.py:278-303` 拿回执之外的**独立证据**判定：从工作区读出孙进程写下的 pid，再 `_wait_dead(pid)`（`os.kill(pid, 0)` + `waitpid(WNOHANG)` 处理僵尸）。四个逃逸形态（同组、setsid、双重 fork、完整 daemonize）都覆盖了，seatbelt 下连 `chdir("/")` + 关全部 fd 的那只也要求真的消失。`:317-329` 还用 `kill=lambda pid: None` 这个缝，正面验证"杀不掉 → `status=error` + `residual_pids` 里有它"。这几条是扎实的。
- **"验证的就是登记的"**——`test_p32_workspace_symlinks.py:212-220`：登记之后**再改活树**，然后断言验证副本里是登记时的字节。这条直接证伪了"复制活树"的旧实现。
- **"密钥没有外泄"**——`_no_secret_under()`（:70-82）遍历整棵树，逐个文件读原始字节找 `SECRET`，并且同时断言没有软链幸存；拒绝文本里也断言 `str(secret) not in …`（不透露指向）。P32-2 那条更强：在真沙箱里跑 Worker 写的测试，六种越权尝试全部要求非 `ALLOWED`，再断言秘密字节没进 stdout、宿主上的 `escape.txt` 不存在。这是行为 oracle，不是自证。
- `test_p32_1_a_widened_profile_fails_the_probe_and_is_unusable` 用"故意放宽白名单 → 探针必须变红"反向证明探针有效，很好。

**偏弱或有假通过风险的**：

1. `test_p32_3_output_is_bounded:341`——`assert receipt.limit_exceeded in (None, "output")`。因为 `output` 永远不会被置上（P2-6），这条断言无论如何都真，等于没断言。
2. `test_p32_9_a_half_written_ledger_line_is_ignored:144-150`——先用 `fail_after="intent"` 让末条变成 ABORTED，再追加半行。`lookup` 返回 None 其实是 ABORTED 决定的，半行被不被丢弃都一样。分不出这两种原因。改成在 COMMITTED 之后追加半行、断言仍能重建回执，才能真正证伪。
3. **P32-9 ① 的"真崩溃"分支没有测**。现有参数化测的是 `fail_after="intent"`（连接器自己补 ABORTED）；验收里另一半"只留下 PREPARED、文件不存在 → STILL_UNKNOWN，不自动重发"只测了 `lookup` 的间接形态（用户删文件那两条），**没有测"留下 PREPARED 之后再次 execute"**——而那正是 P1-2 的洞所在。
4. **补偿从没走过真正的发布连接器**。`test_p32_compensation.py` 全程用 `test_config` 服务，`test_p32_compensation_facade.py` 用的是 `_Commit` 假对象。所以 P1-1 的文件名冲突在整个 117 条 p32 测试里无人触碰。
5. **切片 D 缺一条端到端**：`bind_artifact_params`（单测）、`FilePublishConnector`（单测）、审批与交接（step07 的既有路径）三段各自测到了，但"候选写 `artifact_path` → accept 事务绑定 → 审批 binding → 交接 → link → 回读"这条完整链路没有一条测试串起来。P32-7 的"回执、ledger、CAS 三处一致"目前是分段推断出来的。
6. `test_p32_6_*` 里若干场景靠直接改 SQLite（`UPDATE workspaces SET base_snapshot = …`）来制造"身份不符"。作为白盒可以接受，但"换了 seed / 换了上游输入导致 base_snapshot 变化"这种**真实**成因没有被覆盖。

---

## 四、与计划/验收的对照

### journal 里登记的偏差

§2.4（4 条）、§2.5（4 条）、§2.6（3 条）、§2.7（ABORTED 一条）——**逐条核对，全部合理，也确实不影响对应验收**：

- 迁移放 `__aenter__` 而不是 schema 钩子：`backfill` 按 hash 幂等，重跑安全（`test_p32_5b_backfill…` 有"再跑一次无变化"的断言）。只有性能问题（P2-11）。
- 用 `storage_uri == ""` 代替新列 `storage_state`：Artifact 契约不变，`read_verified` 把空 uri 判为 `unavailable`，九个读取点都处理了。可接受。
- 执行副本不进登记表、按时间清扫：与 P32-6 的措辞（"每个 Attempt 与各类副本"）有出入，但计划 D4 的 kind 里本来就有 exec，journal 明确改成不登记并说明了理由（较新的副本可能属于另一个实例）。可接受。
- 不写 `WorkspaceRegistered`/`WorkspaceCleaned` 事件、不进回放投影：理由成立（运行状态不是 Mission 事实），且能防回放漂移。
- 去掉 RETAINED 状态、清理只在启动时做、端口只留 `execute`、输出保留尾部、off 时 oracle 不跑：都往简单方向取，且都说明了理由。**"off 时 oracle 不跑"这条尤其正确**——oracle 会导入模型写的代码，它必须服从这个开关。
- ABORTED 那条是把计划 D6 与验收 P32-9 的矛盾摆平，计划与验收都同步改了，处理方式诚实（"常见的当场失败可以重试，真崩溃仍然转人工"）。

### 未登记的偏差（4 处）

1. **启动时的沙箱身份扫描没做**（计划 D1 回收 3.iv）——P2-1。
2. **账本写入没有 flock**（计划 D6 明写）——P2-2。
3. **"目标文件已存在"少了 intent 归属判断**（计划 D6）——P2-3。
4. **`limit_exceeded` 取值少了 `output`**（计划 D1）——P2-6。

另有一条措辞层面的：计划 §5 要求剩余风险写进 ARCHITECTURE，实际没有落点（P2-13）。

### 验收项的证据

| 验收 | 判断 | 说明 |
|---|---|---|
| P32-1 探针 | **充分** | 8 项齐全、顺序断言、互斥断言、反向用例（放宽白名单必须红）都有 |
| P32-2 行为 oracle | **充分** | 真沙箱里跑 Worker 写的测试，六种尝试全拒，秘密字节不进输出，宿主无残留文件 |
| P32-3 超时无遗留 | **充分** | 四种逃逸形态 × 两个适配器，判定用独立的 `ps`/`kill(0)` 证据，不看回执自述；"杀不掉 → error" 也正面测了 |
| P32-4 语义与软限制 | **充分** | 三种取值、旧字段双向兼容、矛盾报错、RSS/进程数软限制真的触发回收、SNAPSHOT_FIELDS 排除有断言 |
| P32-5 R13 | **充分** | 角色、数据框、提前闭合转义（`</recalled_history>` 只出现一次且注入内容留在框内）、metadata 不变，四条都能证伪 |
| P32-5a 软链 | **充分** | 六条路径（验证副本、repair 种子、integrated copy、执行副本、snapshot、软链目录）全覆盖，oracle 是秘密字节本身 |
| P32-5b CAS | **充分** | 四个子项齐全，包含只读位、篡改后 hash 报错、软链替换、迁移可重跑 |
| P32-6 工作区登记 | **基本充分** | 五种登记路径 + 两条清理路径都有；扣分项是"身份不符"只用直接改库制造，未覆盖真实成因 |
| P32-7 审批后发布 | **不足** | 绑定与连接器分别测到，**缺完整链路**；"回执、ledger、CAS 三处一致"是分段推断 |
| P32-8 内容变化重批 | **不足** | 账本层全绿，但**补偿从未经过真实发布连接器**，而那条路径现在是坏的（P1-1） |
| P32-9 回执丢失 | **不足** | 三个故障点与两个用户场景都测了，但**"只留下 PREPARED 之后再次 execute"这个分支没测**，正是 P1-2 的洞 |
| P32-10 非权威查询 | **充分** | 真值表五种组合 + L2 闸门 + L1 放行 + 测试服务仍可用，step07 断言未动 |
| P32-11 权限放大与补偿 | **基本充分** | 越界 target 五种、软链目录、hash 不符、名字被占都测了；L3 双人审批与越界拒绝在账本层充分。缺"补偿真的发布出去" |
| P32-12 SDK 交付 | **不足** | mypy 红 3 条（P1-4）；wheel 0.10.0 尚未在干净环境验证；代码评审处置（journal §4）待填。红集说明本身写得清楚（step02、test_facade:424、step08 三处有意改动都有登记） |
| P32-13~16 | 不在本轮范围（Host 切片 F/G） | — |

### 建议的处置顺序

1. P1-1、P1-2 必须在切片 F/G（Host 接线与原生验收）之前修掉——它们决定"真实受控交付"这条主线能不能跑通，各配一条测试。
2. P1-3、P1-4 属交付门禁，切片 E 内解决。
3. P1-5 与 P2-1 至少要在 journal 与 §2 里如实登记，最好直接修。
4. 其余 P2 可并入切片 E 的收尾。
