# P3.2 计划评审报告 · 第 2 轮（独立评审员）

- 评审对象：`plan.md`（第 2 版）、`acceptance.md`（第 2 版）、`journal.md` §1；参考 `code-map.md`、`reports/plan-review-round1.md`
- 方向依据：Host `plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md` §4（193–240 行）；术语参照 `agent-orchestration-theory/`（09 幂等、13 §12 回滚与补偿）
- 手段：通读相关源码 + 在 `/private/tmp/p32-review2-spike` 里做一次性 seatbelt / 进程回收实测（已删除，无残留进程）；未跑 pytest，未用真实模型，未改仓库其他文件
- 附带发现：`tests/orchestrator/p32/` 与 `tests/agents/test_recall_untrusted_frame.py` 已有未提交的测试草稿（切片 A/B/C），本报告顺带对照了它们

## 结论：READY_WITH_CHANGES

第 1 轮 13 条意见都有落点，方向没有问题。但有 4 条新的 P1，都是第 2 版自己的设计之间、或者设计与现有代码之间对不上：D1 与 D3 互相冲突；进程回收漏掉标准 daemonize，而本机有更简单可靠的办法（已实测）；验证的字节不等于登记的字节；D6 的 lookup 在现有协议下做不到"权威"。4 条都能在本计划内修掉，不改方向。没有 P0。

---

## 本轮实测（macOS 26.6，SDK venv python 3.14.7 / pytest 8.4.2）

| 实验 | 结果 | 含义 |
|---|---|---|
| 按 plan D1 原文规则：读取"全部拒绝 + 白名单 `/System /usr /bin /private/etc /dev` …"，然后 `sandbox-exec … /usr/bin/true` | **SIGABRT，rc=134**（python 同样如此） | 白名单少了 `(literal "/")`；补上后正常。只补 `/private/var/db/dyld` 无效 |
| 补上 `(literal "/")`，其余规则与 plan 完全一致（含 `deny mach-lookup`、`deny process-info* (target others)`、`deny signal`），用 `env -i` 跑 venv pytest | 6 条用例中 5 条通过；fork 与 `subprocess` 正常；读 `~/.ssh` 被拒 | `deny mach-lookup` 不影响解释器和 pytest |
| 同上，`getpass.getuser()` | **OSError**：`pwd.getpwuid` 在 deny mach-lookup 下失败（走 opendirectoryd），`env -i` 又清掉了 USER/LOGNAME；pytest 自己回退为 `pytest-of-unknown` | env 应显式给出 USER/LOGNAME |
| TMPDIR 指向工作区内 `.tmp`，测试用 `tmp_path` | 工作区里**必然出现 2 个软链**：`.tmp/pytest-of-unknown/pytest-current` 和 `…/test_tmp_pathcurrent` | 与 D3"发现软链就拒绝"直接冲突，见 P1-1 |
| 沙箱内起两个逃逸进程：① setsid 孙进程，cwd 在工作区；② 标准 daemonize（fork→setsid→fork→`chdir("/")`→关闭全部 fd→sleep） | 两者都在微秒级被 reparent 到 pid 1；`lsof +D <ws>` **只看见 ①，看不见 ②** | plan 的"曾见后代集合 + lsof"对标准 daemonize 必然漏掉，见 P1-2 |
| 宿主枚举本 uid 的进程，对每个 pid 调 `sandbox_check(pid,"file-read-data",PATH\|NO_REPORT, 金丝雀/诱饵)` | 428 个同 uid 进程（其中 296 个处于某种沙箱），**精确命中 ①② 两个**，没有误中，耗时 21.9 ms；SIGKILL 后无残留 | 能认出"本次沙箱里的全部进程"，不管 setsid、双重 fork、chdir 还是关 fd，见 P1-2 |
| 开销 | `ps -A -o pid=,ppid=,rss=` 每次 11.7 ms（本机 720 个进程），10 Hz 轮询约占一个核的 12%；`lsof +D`（300 个文件的目录）0.21 s | lsof 代价可以接受，但它解决不了问题 |

---

## 第 1 轮意见落实核对表

| 编号 | 是否落实 | 说明 |
|---|---|---|
| P0-1 软链外泄 | **部分落实** | D3 已写 `copytree(symlinks=True)`、扫描拒绝、`snapshot` 拒绝软链、`lstat`+`O_NOFOLLOW`，代码层面可行（`workspace.py:159/214` 两处 copytree、`:97` 目前是静默跳过）。但与 D1 的"HOME/TMPDIR 指向工作区内部"冲突：pytest 的 `tmp_path` 必然产生软链，所有用 tmp_path 的 Result 都会被误判 `workspace_symlink`（实测）。另外有更根本的做法：让 Worker 的正式树里根本出现不了软链。见 P1-1 |
| P0-2 setsid 回收 | **部分落实** | "曾见后代集合 + `lsof +D` + RLIMIT_CPU"对 setsid 且 cwd 在工作区的进程有效；对标准 daemonize（chdir / + 关 fd + 睡眠）三道都失效（实测），计划只把它写成剩余风险。剩余风险的后果也写轻了（漏了内存和改写工作区）。P32-3 与草稿 `test_p32_sandbox.py` 的 `DOUBLE_FORK_DAEMON` 保留了工作区 cwd，属于容易的那种变体，仍然会"假通过"。见 P1-2 |
| P1-1 内容寻址产物库 | 方向已落实，细节不足 | CAS、`storage_uri` 改指产物库、清理只删工作区，都写了。缺：迁移时要校验 hash、`unavailable` 记在哪一列、9 个读取点统一入口、facade 篡改测试要改（P2-3）；而且 `verification_copy` 仍从活树复制（P1-3） |
| P1-2 signal / mach / process-info | **已落实** | 规则已补。实测：补上 `(literal "/")` 后，这三条不影响 python、pytest、fork |
| P1-3 NPROC 与内存 | **已落实** | 不用 RLIMIT_NPROC，进程数和 RSS 采样后回收，如实写成软限制。但 D1.5 的剩余风险漏了"残留进程的内存不受约束"（P2-2） |
| P1-4 读白名单 | **部分落实** | 已改成"全部拒绝 + 白名单"，但照计划原文的白名单，任何程序一启动就 SIGABRT（缺 `(literal "/")`，实测）。journal §1 说"实测此式可正常跑 venv pytest"，按计划原文无法复现（P2-1） |
| P1-5 R13 测试位置 | **已落实** | D5 只改 `_RecallAdapter`（`runtime.py:379` 目前还是 `MessageRole.SYSTEM`），回归测试放在 SDK；草稿 `test_recall_untrusted_frame.py` 已覆盖角色、数据框、提前闭合、metadata |
| P2-1 执行后改内容 | 已落实 | 执行前走新版本、执行后走补偿，都写明了。补偿动作的业务键、内容来源、入口没写（P2-7） |
| P2-2 best_effort 默认 | 已落实 | 实测影响面很小，见 P2-6。Protocol 的 docstring 需要同步改 |
| P2-3 L3 只在 SDK 测 | 已落实 | D8、P32-11 都写了测试层级 |
| P2-4 探针互斥 | 已落实 | 草稿有 `test_p32_1_probe_targets_are_disjoint_from_the_read_whitelist` |
| P2-5 create 与登记同处改 | **部分落实** | 写了"同一处改"，但没定义"登记对不上"比的是什么。`recover()` 每次重启都会对已存在的目录调用 `_bind_workspace`（`event_handler.py:884`），按目录内容比会让恢复卡死（P2-4） |
| P2-6 切片 | 已落实 | P0/P1 已成为 A/C 的显式验收项。顺序建议见文末 |
| 诚实性 | 基本落实 | §2 写得诚实；D1.5 剩余风险不全（P2-2） |

---

## 新发现

### P0

无。

### P1

**P1-1｜D1 与 D3 冲突：HOME/TMPDIR 放在工作区里，再加"发现软链就拒绝"，会误判所有用 tmp_path 的结果，还会把沙箱的临时文件登记成产物**

- 证据：
  - plan D1 第 40 行"HOME 和 TMPDIR 都指向工作区内部"，第 37 行写路径含"工作区内的 `.tmp`"；D3 第 96–99 行要求复制后发现软链就拒绝、`snapshot` 遇到软链就拒绝，并判 ResultRejected。
  - 实测：pytest 的 `tmp_path` 在 TMPDIR 下固定建出 `pytest-current` 和 `<test>current` 两个软链。
  - `workspace.py:33` 的 `IGNORED_DIRS` 只有 `__pycache__/.pytest_cache/.git`，所以 `.home` 与 `.tmp` 下的文件会被 `snapshot()`（`event_handler.py:1827`）登记为产物，写进 CAS，并被下一次 Attempt 的 `create(previous=…)` 带走。
- 建议：定一条不变量，**模型写的代码只在一次性副本里执行，Worker 的正式树只由 gateway 的文件工具写**。
  - `run_tests` 在 `<attempt>-exec-<n>/` 副本里跑，结束就删；`code_test` 本来就在 `-verify` 副本里，`pytest:` 在 integrated copy 里，评测 oracle 在 tempdir 里。
  - HOME/TMPDIR 指向这次执行专属、位于工作区之外的 scratch 目录，写白名单加上它。
  - gateway 的文件工具建不出软链：`resolve` 拒绝末级软链，`write_text` 不建链。这样正式树的软链从来源上就没有了，D3 的扫描退为纵深防御，也不会误伤。
  - `run_pytest` 现在就用 `-p no:cacheprovider` 加 `PYTHONDONTWRITEBYTECODE`，本意就是不回写，改成在副本执行不会改变语义。切片 A 里要 grep 确认没有测试依赖 run_tests 回写文件。
  - 如果坚持在原树执行，就必须把 scratch 目录加进 `IGNORED_DIRS`，复制时排除，并规定软链扫描不看这些目录。

**P1-2｜D1 回收漏掉标准 daemonize，后果不止 CPU；本机有更简单可靠的办法（已实测）**

- 证据：
  - 实测里，fork→setsid→fork→`chdir("/")`→关闭全部 fd 的进程在微秒级 reparent 到 pid 1。100 ms 一次的 ppid 轮询看不到中间进程；`lsof +D` 看不到它（它已离开工作区、没有打开的文件）；它在睡眠，RLIMIT_CPU 永远不触发。
  - plan D1.5 说它"出不了网，写不出工作区，CPU 也有上限"，但漏了三点：
    - RSS 采样在执行结束后就停了，RLIMIT_AS 又设不进去，所以它可以无限占内存（本机 24 GB，有 OOM 强杀史）；
    - 它仍能写本次执行的写路径，也就是 Attempt 工作区，这直接引出 P1-3；
    - 它在 pytest 进程退出后依然存活。
  - 草稿 `test_p32_sandbox.py` 的 `DOUBLE_FORK_DAEMON` 保留了工作区 cwd，恰好是 lsof 能抓到的那一种。
- 建议：seatbelt 适配器改用**沙箱身份扫描**。
  - 每次执行的规则里加一条只对本次执行放行的金丝雀：`(allow file-read-data (literal "<scratch>/canary-<execution_id>"))`，同一目录下再放一个不放行的诱饵。
  - 回收时枚举本 uid 的进程（`ps -U` 或 libproc），对每个 pid 调 `sandbox_check(pid, "file-read-data", SANDBOX_FILTER_PATH | SANDBOX_CHECK_NO_REPORT, path)`。放行金丝雀、又拒绝诱饵的，就是本次沙箱里的进程。沙箱跨 fork、exec、setsid 继承，而且进程自己退不出去。
  - 实测：428 个进程中精确命中 2 个逃逸者，21.9 ms，杀完无残留。`sandbox_check` 与 `SANDBOX_CHECK_NO_REPORT` 都是 `libsystem_sandbox.dylib` 的导出符号，可以用 ctypes 调；它是未公开接口，Chromium 和 WebKit 在用，与 seatbelt 本身同属"已废弃但可用"，由启动探针每次验证。
  - 扫描时机：执行正常结束后、超时或取消时，以及宿主启动时（按未清理的 execution_id 扫一遍）。`tree_killed` 以扫描结果为准。
  - RSS 和进程数的软限制也可以按这个集合统计，不必每 100 ms 起一次 `ps`。
  - `ProcessOnlyExecutor` 没有沙箱，只能保留 ppid + lsof 的做法，它的名字已经写明只用于可信代码。
  - P32-3 加一个 `chdir("/")` + 关 fd 的 daemon 用例，要求 seatbelt 适配器下 `ps` 查不到它；process_only 下允许登记为已知缝隙。

**P1-3｜被验证的字节不一定是被登记、会被发布的字节**

- 证据：
  - `snapshot()` 在结果提交时登记产物（`event_handler.py:1827`）；`verification_copy()` 在更晚的验证开始时从**活树** `copytree`（`workspace.py:214`，调用处 `event_handler.py:1954`）。两者之间，活树可以被 P1-2 的残留进程改动。
  - 攻击路径：登记恶意内容 X，验证前把树换回良性内容 Y。code_test 和 Critic 判的是 Y，得到 PASS；进 CAS、被批准、被发布的是 X。只有 `actions/*.json` 在 accept 事务里按 hash 重读（`action_commits.py:981`），报告正文没有这道保护。
  - 这让 Phase3 §4.4 的"required verification PASS → approval bound to exact content"在内容层面断开。
- 建议：
  - 切片 C 引入 CAS 后，`-verify` 副本改为用"seed + 上游输入（CAS）+ 本 Attempt 登记产物（CAS）+ 受保护文件"重建，与 `integrated_copy` 用同一种方式，不再复制活树。
  - 验收加一条：登记后改动活树，验证副本里仍是登记时的字节。
  - 这样 P0-1 的软链复制路径也少了一条。`create(previous)` 的修复种子，对没有登记产物的 Attempt（例如超时）仍需复制活树，保留 D3 的 `symlinks=True` + 扫描拒绝即可。

**P1-4｜D6 的 lookup 权威性论证在现有协议下不成立**

- 证据：
  - (a) 协议是 `lookup(idempotency_key)`，只收 key（`connectors.py:146`，调用处 `actions.py:138`）。D6 的回退"final 文件存在且 hash 等于绑定 hash"需要 target（stem、后缀、子目录）和绑定 hash。在"link 已完成、ledger 未写就崩溃"这个点上（正是 P32-9 要测的），connector 手里只有 key，找不到文件；除非全目录 glob，那样既慢又有歧义。
  - (b) ledger 放在用户的发布目录里（`.publish-ledger.jsonl`），用户可以删改。用户删掉已发布文件后（目录是他的），会出现"ledger 缺失 + 文件缺失"：authoritative 连接器返回 None，被当作 CONFIRMED_NOT_STARTED，于是用同一个 key 再发一次（`actions.py:145-151`）。按理论 09 章的幂等与权威查询定义，"权威"要求执行记录由服务自己持久保存、不会被第三方回滚，这里不满足。
  - (c) 回执比对会检查 `target`（`action_commits.py:174`）。D6 说回执"含 canonical 路径"，如果把带版本号的最终路径写进 `Receipt.target`，发布成功也会被判 `receipt_mismatch:target`，结果成为 UNKNOWN。
- 建议：
  - 采用**先写意图（write-ahead intent）**。执行前把 `{key, final_path, content_hash, state=PREPARED}` 写进由 connector 独占的 ledger，放在 root 之外（例如 `<evidence_root>/connectors/file_publish/`），加 flock 与 fsync。然后依次：link、fsync(目录)、回读、追加 `COMMITTED`。
  - lookup 的真值表：
    - 没有 intent：CONFIRMED_NOT_STARTED。link 是唯一的提交点，intent 在它之前落盘；
    - 有 intent，文件存在，hash 相符：COMPLETED，并据 intent 重建 Receipt；
    - 有 intent，但文件缺失或 hash 不符：STILL_UNKNOWN，转人工（可能已经发布后被用户删了或改了）；
    - jsonl 末行没写完：丢弃这一行。intent 没有完整落盘之前不会做 link，所以按"没有 intent"处理是安全的。
  - 也可以扩展协议为 `lookup(key, *, action=…)`，但那样仍然解决不了 (b)。
  - `Receipt.target` 保持动作的规范化 target，最终路径与回读 hash 放在 `after` 和 `service_ref` 里。
  - P32-9 的故障注入点扩成三处：intent 后、link 前；link 后、COMMITTED 前；COMMITTED 后、回执返回前。再加"发布后用户删除文件，然后 reconcile"，期望 STILL_UNKNOWN，不重发。

### P2

**P2-1｜D1 规则细节**
- 读白名单缺 `(literal "/")`，按原文规则任何程序都会 SIGABRT（实测 rc=134）。
- `deny mach-lookup` 已实测不影响解释器和 pytest，plan §5 的这条风险可以关闭。但要在 env 里显式给出 USER 和 LOGNAME（不敏感），否则 `getpass.getuser()` 抛 OSError。
- `/dev/tty` 不需要放进写白名单：新会话里没有控制终端。
- 时区数据 `/private/var/db/timezone` 不在白名单里，本轮没有核对它对 `localtime` 的影响，切片 A 顺带确认即可。
- 元数据（stat）仍然全局可读，可以探测文件是否存在、大小、mtime，建议写进 §2 的诚实边界。

**P2-2｜剩余风险描述不全**
§2 和 D1.5 应补上"残留进程的内存不受约束"和"残留进程仍能写本次执行的写路径"两点。采纳 P1-2 与 P1-3 之后，这两条可以大幅收窄，再按实测结果改写。

**P2-3｜D3 迁移与读取点细节**
- 迁移机制可行：`_apply_migration` 除 DDL 外，还有按版本号挂的 Python 钩子（`store.py:282-292`，v2 和 v6 有先例）。升级前的备份已由 store 自动完成（`store.py:268-278` 的 `pre-schema-N.backup`），计划里的"备份库"不必另做。
- 补写产物库时，必须满足"原文件存在，**且 sha256 等于 content_hash**"才拷贝，否则标记 unavailable。计划原文只写了"存在就拷贝"。
- `unavailable` 记在哪里没有定义：Artifact 模型只有 `storage_uri`（`models.py:960`）。要么加一列，要么约定 `storage_uri=""` 另加状态，并列出各读取点遇到它时的行为（facade 返回 integrity_error、oracle 跳过、`materialise_inputs` 抛 ArtifactConflict）。
- 迁移在 SQLite 事务里做文件 I/O，要能重跑。CAS 按 hash 写，本身幂等。
- 回放投影不含 artifacts（`replay.py` 没有产物投影），所以改写 `storage_uri` 不影响回放和对账，这一点可以写进计划。
- CAS 文件改为只读后，`tests/orchestrator/host_support/test_facade.py:424` 直接改写 `storage_uri` 的篡改测试会 PermissionError，需要改测试并登记进红集说明。
- `storage_uri` 有 9 个读取点：`facade.py:432`、`evaluation.py:309/311`、`versioning.py:163`、`event_handler.py:1290/2291/3378`、`action_commits.py:981`、`policy_commits.py:686`、`__main__.py:224`。建议统一走 `ArtifactStore.open_verified(artifact)`，同时做 hash 校验与 `O_NOFOLLOW`，不要各自 open。

**P2-4｜D4 "登记对不上"的判定对象**
- `_bind_workspace` 在 `recover()` 里（`event_handler.py:884`）和每次 dispatch 时（`:1223`）都会对已存在的目录调用。所以比的应是登记身份：attempt_id，以及用 seed 与上游输入清单重算出的 base_snapshot。不能比目录内容，否则 Worker 改过的树必然"对不上"，恢复会卡死。
- 加一个 CREATING 状态，处理"mkdir 之后、填充完成之前崩溃"留下的半成品目录。现有代码同样有这个问题：目录存在就直接返回。
- `-verify`、`<mission>-judge-<owner>-verify` 与 P1-1 的 exec 副本也应纳入登记与清理。

**P2-5｜D6 细节**
- 文件名取"action_id 前 12 位"，而 action_id 是 `"action-"` 加 16 位 hex（`action_commits.py:166`），前 12 位里只有 5 位 hex（20 bit），容易撞名。应改为取 hex 部分的 12 位。
- link 之后要 fsync 目录。
- 用户选的目录可能不支持硬链接（exFAT、FAT、部分网络卷会返回 EPERM 或 ENOTSUP）。应在授权目录时就探测，不支持就不授权，而不是等到运行时变成 UNKNOWN。
- 检查"途经软链"与做 link 之间有 TOCTOU。建议逐级 `os.open(O_DIRECTORY|O_NOFOLLOW)`，再用 `os.link(..., dst_dir_fd=…)`。
- 临时文件用点号开头的名字，失败或恢复时清理掉。
- 候选怎样指明要发布哪个产物（例如 params 里的 path），系统怎样把它绑定成 `{artifact_id, content_hash, size}`（应限定为同一个已 accept、已 PASS 的 Result 里的产物），都没写。候选字段是严格白名单（`action_commits.py:68-84`）。

**P2-6｜D7 影响面（已核实）**
- 全仓 Connector 实现只有三个：`TestConfigService`；它在测试里的子类 `FlakyService`（`test_action_execution.py:30`，继承属性）；`PaymentConnectorStub`（`supports_reconciliation=False`，不受影响）。SDK 测试里没有其他 connector 替身，Host 也只用 `TestConfigService`（`service.py:173-181`）。
- 所以 TestConfigService 标为 authoritative 之后，step07 预计不用改任何断言。P32-12 的"事先登记"可以改写为"预计为空"。
- 需要同步两处：
  - `connectors.py:131` 的 Protocol docstring 现在写着"supports_reconciliation 即承诺权威 lookup"，与新的默认值 best_effort 矛盾；
  - `policies.py:117` 是否允许 best_effort 连接器开放 L2/L3，要写明。建议 L2 及以上必须 authoritative。

**P2-7｜D8 补偿的缺口**
- 补偿动作的业务键必须和原动作不同。否则 `propose_action` 会按 business_action_id 命中已 SUCCEEDED 的版本，被 `action_already_executed` 拒掉（`action_commits.py:259`）。
- 新内容来自哪个产物、由谁调用（facade、审批 API 还是 CLI）、Host 界面上的入口在哪，都没写。

**P2-8｜D2 的 SDK 默认值与 Phase3 §4.3 有出入**
- Phase3 §4.3 要求"生产未配置隔离器时拒绝执行未受信任代码"，而 SDK 默认仍是 `process_only`，草稿 `test_default_deployment_is_process_only_and_unchanged` 还把它锁死了。
- 建议在计划里显式登记这个偏差和理由，并让回执和 status 标明"process_only：未隔离"。
- 或者把默认值改为"探针通过就 sandboxed，否则 off"，测试夹具里显式设 process_only。

**P2-9｜D9 与打包版 Host**
- 打包版 Host 的后端是 PyInstaller 冻结的 exe（`backend_launch.rs:14/34`），在里面 `sys.executable -m pytest` 跑不起来。
- `SandboxSpec` 或执行器应显式接收解释器路径，白名单按这个解释器的 prefix、base_prefix 与 `.pth` 计算。冻结版上探针失败就设为 off，并如实写进部署清单。
- P32-16 用候选启动器，里面是 venv python（`launch_native_candidate.py:81`），能跑；但证据里要写明它不代表打包版。
- Host 启动时跑 8 项探针，每项都要起一次解释器，建议按 environment_digest 缓存结果，并放到后台异步执行。

**P2-10｜D5 小项**
- 计划正文要写明 `</recalled_history>` 的转义规则（草稿已有测试）。
- provider 只有 `openai_compatible.py`，它不约束角色交替，所以召回改成 USER 之后出现连续的 USER 消息不会被拒。

---

## 切片顺序与可以直接开工的切片

建议顺序：**B → C → A → D → E → F → G**，计划原文是 A → C → B → D → E → F → G。
- B 很小、独立，草稿已就绪，可以马上做完。
- C 修的是今天默认部署里就存在的 P0-1，不依赖 A；而且 P1-1 与 P1-3 的不变量（执行只在一次性副本里做、验证副本由 CAS 重建）应该由 C 先定下来，A 照着实现。
- D 依赖 C 的 CAS。
- 如果坚持先做 A，就必须先在 A 里定下 P1-1 的 scratch 布局。

可以直接开工：
- **切片 B**：立即开工。
- **切片 C**：先把 P1-1、P1-3、P2-3、P2-4 写进 plan D3/D4 和 P32-5a/5b/6，再开工。
- **切片 A**：先吸收 P1-1、P1-2、P2-1（草稿 `test_p32_sandbox.py` 要加 chdir-/ daemon 用例，并改用 canary 扫描），再开工。
- **切片 D**：必须先按 P1-4 重写 D6 的 lookup 与 ledger 设计，并同步改 P32-9。
- E、F、G 依赖前面的切片。
