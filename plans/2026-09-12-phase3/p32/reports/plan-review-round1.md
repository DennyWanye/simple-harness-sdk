# P3.2 计划评审报告 · 第 1 轮（独立评审员，只读）

- 评审对象：`plans/2026-09-12-phase3/p32/{plan.md,acceptance.md,code-map.md}`
- 方向依据：Host Phase3 §4（193–240 行）、§11 P3.2-A01..A08、参考表 R09/R13/R15/R16
- 只读手段：grep + 源码通读 + `/private/tmp` 下一次性 seatbelt/rlimit/进程树探测（已删除）
- 全量回归正在跑，本轮未运行 pytest

## 结论：READY_WITH_CHANGES

方向正确、与 §4 与 A01..A08 基本对齐，动作管线（审批绑定 `artifact_hash`、`receipt_mismatch`、
`reconcile`、幂等键）本就为 A03..A06 打好底子。但沙箱设计有几处**会让 A01/A02 的验收在真实攻击下
被绕过**，且有一处**产物字节存储与清理自相矛盾**，须在切片 A/C 落地前修正。均可在本计划内修，不改方向。

---

## 本机探测实测（seatbelt / rlimit / 进程树，macOS 26.6，均已清理）

| 探针 | 结果 | 对计划的含义 |
|---|---|---|
| 直接读 HOME 密钥 | 拒绝（spike 规则 `deny file-read-data (subpath HOME)`）| D1 读拦有效 |
| 在工作区内 `ln`(硬链)到密钥后读 | 拒绝 | 硬链路径无效 |
| 在工作区内 `ln -s` 软链到密钥；沙箱内读→拒绝；**但宿主 `shutil.copytree` 解引用后得到含密钥明文的普通文件** | **逃逸** | 见 P0-1 |
| 沙箱向同 uid 宿主进程发信号 | spike 规则**成功杀死**；加 `(deny signal)(allow signal (target same-sandbox))` 后被拒 | D1 规则缺 signal，见 P1-2 |
| 读同 uid 进程环境变量 `ps -E` | 不可见（macOS 自身限制）| 可接受 |
| 回环 TCP + Unix socket 连接 | `deny network*` 一并拦下（含 AF_UNIX）| 禁网充分 |
| launchctl submit 越权写文件 | 未逃逸（spike 与硬化规则都没写成）| mach-lookup 逃逸风险低，仍建议 deny |
| `ulimit -u 64`（RLIMIT_NPROC）| 因该 uid 已有 431 进程，**立即 fork 失败** | NPROC 按 uid 计，见 P1-3 |
| `setrlimit(RLIMIT_AS/DATA, 200MB)` | `current exceeds maximum`，**设不进去** | 计划未用 AS/DATA（对），但也就**没有任何内存上限**，见 P1-3 |
| 测试内 `os.setsid()` 造孙进程后 `killpg` | **孙进程存活** | killpg 无法回收 setsid 逃逸者，见 P0-2 |
| deny-all-read + 白名单(venv/base/ws) 跑 venv pytest | 通过，HOME/`/private/var/folders` 均拦 | 白名单式优于 HOME 黑名单，见 P1-4 |
| `(deny process-fork)` | 能跑 `python -c`，但 pytest 需要 fork → 与 run_tests 不兼容 | 不能靠 deny fork 限制，只能靠计数 |

---

## 发现表

### P0（阻断，必须在切片落地前解决）

**P0-1｜工作区内软链 + 宿主 copytree 解引用 = 越沙箱读取宿主密钥/密钥外泄**
- 位置：`artifacts/workspace.py` `create()`(:159 `copytree(previous,…)`)、`verification_copy()`(:214)、`integrated_copy()`；发布 D5 与 `facade.artifact_read`(:432) 均按 `storage_uri` 直接 open。
- 场景：`run_tests` 里模型的 pytest 在工作区内 `os.symlink('/Users/taiwan/.ssh/id_rsa','leak')`（seatbelt 允许在 ws 内写/建链）。沙箱内读被拦，但**宿主进程**后续 `shutil.copytree(symlinks=False)` 会**解引用**，在下一次 Attempt 的工作区（或 `-verify`）里生成一个**含密钥明文的普通文件**；`snapshot()` 的 `_walk` 只在当前树跳过 symlink，复制成普通文件后即会被登记为 Artifact，进而可被批准发布。D5 只对**发布 target** 做了 symlink 防护，工作区树内部完全没防。
- 失败场景对 A01：沙箱"拒绝范围"看似生效，实测可跨进程外泄。
- 建议：① 所有 `copytree` 加 `symlinks=True` 并在拷贝后**拒绝/剥离**任何 symlink（或改用只按 `snapshot` 登记过的普通文件重建工作区）；② `create/verification_copy` 完成后扫描并拒绝树内 symlink；③ seatbelt 增加 `(deny file-write*)` 对 symlink 创建的等价限制不可行（seatbelt 无此粒度），故防线必须在宿主侧；④ `artifact_read`/发布读取用 `open(..., O_NOFOLLOW)` 或先 `lstat` 拒绝 symlink。

**P0-2｜`killpg` 无法回收 `setsid` 孙进程，A02 会被"假通过"**
- 位置：`runtime/tool_gateway.py:130/137` `os.killpg`；plan D1"超时或取消时 killpg 整组"；acceptance P32-3。
- 实测：子进程 `os.setsid()` 后另起进程组，`killpg(原组)` 不触及它，孙进程存活。P32-3 只写"fork 出孙进程"（同组，killpg 能杀），会**恰好通过**而真实逃逸仍在。macOS 无 PID namespace，killpg 不是进程树回收。
- 建议：① 验收补一条 **setsid/双 fork 守护进程** 逃逸用例，要求 `ps` 查不到；② 实现改为**枚举后代**（轮询进程表按 sid/ppid 链，或用 `proc_listpids`）逐个 SIGKILL，或以"新会话 + 超时后反复扫描直到无残留"并把 `tree_killed` 置为**实测确认**而非"发过 killpg"；③ ProcessOnly 适配器同样标注此限制。

### P1（重要）

**P1-1｜产物字节只存在于工作区，D4 清理会抹掉"内容寻址产物"——自相矛盾**
- 位置：`workspace.py:127` `storage_uri=str(path)`（产物即工作区文件，无独立 CAS，全仓 grep 无 blob/CAS）；plan D4"删除工作区目录，但保留登记和产物，产物本身是内容寻址的"。
- 问题：没有独立内容寻址存储，删了工作区目录=删了产物字节。届时 `facade.artifact_read`、D5 发布（按 artifact_id 读内容核 hash）、评测 oracle、`materialise_inputs`(:163 读 `storage_uri`) 全部失效。
- 建议：D4 若要清理，必须先**把待保留产物字节落到真正的内容寻址库**（`<evidence_root>/artifacts/<hash>` + 硬链/copy），并把 `storage_uri` 改指向该库；否则"保留产物"为假。或明确"清理只删中间文件、发布相关产物永不清理"。这直接影响 A05 的重启核对（重启后要能按业务键回读原文件）。

**P1-2｜D1 seatbelt 规则缺同 uid 残余风险的封堵（signal / process-info / mach-lookup）**
- 位置：plan D1 只列 `file-read-data / network / file-write`；code-map §5.1 spike 同样只这三类。
- 实测：spike 规则下沙箱可**杀死同 uid 宿主进程**；`(deny signal)(allow signal (target same-sandbox))` 后被拦。`(deny mach-lookup)(deny process-info* (target others))` 亦应加（Keychain/securityd、他进程信息）。
- 建议：D1 规则集补 `(deny signal)`（放行 same-sandbox）、`(deny mach-lookup)`、`(deny process-info* (target others))`、`(deny process-fork?)`（注：fork 不能禁，pytest 需要，见 P1-3）。能力探针增加"向宿主进程发信号应被拒"一项。

**P1-3｜资源限制在 macOS 上的实际可用性：NPROC 会误伤宿主、无内存上限**
- 位置：plan D1 `setrlimit`(CPU/NPROC/FSIZE)、`max_processes`；风险节未提内存。
- 实测：RLIMIT_NPROC **按 uid 计**，当前 uid 已 431 进程，`ulimit -u 64` 直接让宿主 shell fork 失败——设低会误伤宿主，设高则无效。RLIMIT_AS/DATA 在本机**根本设不进**（current exceeds maximum），故计划不用它们是对的，但**结果是沙箱完全没有内存上限**；而本机 24GB、有 OOM 强杀历史（记忆库多次记录）。CPU/FSIZE 的 setrlimit 有效。
- 建议：① `max_processes` **不要**用 RLIMIT_NPROC 实现，改为按会话/后代进程计数超限即 killpg+扫描；或用 seatbelt 无此能力，如实登记"进程数只能事后回收，不能硬性阻断"；② 内存上限：本机无 cgroup，只能用**驱动循环采样 RSS 超阈值即终止**（记忆库已有该做法）并写入 `effective_limits`，plan 与 ARCHITECTURE 如实写明"内存为软限制、事后回收"。

**P1-4｜读放行用黑名单(HOME)不足；应改 deny-all-read + 白名单**
- 位置：code-map §5.1 spike 用 `(deny file-read-data (subpath HOME))`。
- 实测：该黑名单下 `/private/var/folders`（他人临时文件）、`/Volumes`、`/Users/Shared` 的**文件内容仍可读**。prompt 关注的"读 /private/var/folders 下他人临时文件"确实成立。
- 建议：改为 `(deny file-read-data (subpath "/"))` + 显式白名单（`/System /usr /bin /private/etc /dev` 只读所需、venv prefix、base prefix、SDK `src`、工作区）。实测此式可正常跑 venv pytest。能力探针增加"读 `/private/var/folders` 他人目录应被拒"。

**P1-5｜R13 的回归面可能打偏：需先确认 Host 主对话是否真的走 `_RecallAdapter`**
- 位置：`SH/agents/runtime.py:211/379`；plan D3"Host 主对话也用这个 runtime（JournalContextPort），要跑 Host tests/sdk_adapters 基线"。
- 事实：Host `backend/deskpet` **全仓无 `simple_harness.agents.runtime` / `JournalContextPort` 导入**；CLAUDE.md 载明认知 Memory SDK 已于 2026-09-10 移除、`AgentMemoryPort` 为空端口（召回恒空）。真正会命中 `_RecallAdapter` 的是**编排器内部 BaseAgents**（assembly 未设 `recall_limit`→默认 6），而编排器 Context 本就以 USER 发送。
- 影响：D3 把 SYSTEM→USER 的**主要回归覆盖点错放在 Host sdk_adapters 基线**；真正需要覆盖的是**编排器 Worker/Critic 的召回注入**（现有 `T/agents/test_session_memory.py:227` 只断言 `derived`，不查 role，正是缺口）。
- 建议：① 先核实 Host 主对话是否启用 SDK 召回；若否，删掉"Host 主对话召回场景"这条原生验收或降级为说明；② 补 SDK 侧"召回消息 role≠SYSTEM、正文在 `<recalled_history>` 数据框、注入文本不获得 SYSTEM 身份"的单测（对齐 P3.4-A07）。这也是"更小做法"：改 `_RecallAdapter` 一处足矣，无需触碰 Host。

### P2（建议）

**P2-1｜A04 与 `business_action_id` 的键控冲突未说清**
- `business_action_id = hash(mission,connector,operation,target)`，不含内容；`propose_action` 对已 SUCCEEDED 的同键版本返回 `action_already_executed`。故"报告内容变化后再次发布到同一逻辑 target"若走普通 `propose` 会被拒。计划靠 D5 文件名带 version 让多文件共存、靠 D7 `publish_new_version` 补偿，但 plan 未点明"改内容再发=补偿动作，而非新 propose"。建议在 D5/D7 显式写死这条路径，并让 acceptance P32-8 覆盖"SUCCEEDED 后改内容→必须走补偿而非新提议"。

**P2-2｜`lookup_authority` 默认 best_effort 会回归现有 step07 语义**
- `actions.py:145` 现在只要 `supports_reconciliation` 就把 None 当 CONFIRMED_NOT_STARTED；D6 收紧为还需 `authoritative`。默认 best_effort 后，`TestConfigService` 不显式标 authoritative 就会让 `test_s7_05`（期望 CONFIRMED_NOT_STARTED）变红。建议：D6 落地时同步把 `TestConfigService` 与 `FilePublishConnector` 标 authoritative，并在切片 A/D 的红集说明里预告这批既有用例需改断言。

**P2-3｜L3 retract 双人审批的测试只能在 SDK 层做**
- Host `service.py:188` 因单用户把 `max_action_level="L2"`，L3 在 Host 一律被 ceiling 拒。`decide_approval`(:475) 的 distinct principals 已具备。故 P32-11"启用后需要两个不同的人审批"必须是**SDK 单测**用两个合成 Principal，不能在 Host 原生验收。建议 acceptance 注明测试层级，避免 F/G 切片空转。

**P2-4｜能力探针需保证探测目标不落在读白名单内、且探针由系统固定**
- 探针"读 HOME 敏感目录应被拒"在改白名单式后要确保目标路径确实不在白名单；否则探针恒过=不可信。建议探针路径与白名单生成同源计算、互斥断言。

**P2-5｜工作区登记 schema v7 的必要性成立，但注意与既有 create() 的静默复用冲突**
- `create()`(:155) 现在"目录已存在直接返回"；D4 要"登记不一致则拒绝"。两者要在同一处改，避免登记表与 `create` 复用逻辑各行其是。schema 迁移当前到 v6（`storage/schema.py` Migration 1..6），v7 需带迁移+备份+回放投影（`ActionProposed` 等在 replay.py 已有范式，`WorkspaceRegistered/Cleaned` 照抄即可）。评价：登记必要（A01 要 base snapshot / 只读输入 / 可写输出），不过度。

**P2-6｜切片划分**
- A（D1+D2）与 D（D5+D6+D7）偏大但内聚，可测；无明显空转。建议把 P0-1/P0-2/P1-1 作为**切片 A 与 C 的显式验收项**（现 P32-3 不足以证伪 setsid 逃逸，P32-1 不含 signal/白名单/symlink 三类）。与 P3.3–P3.5 边界清楚（非代码领域、动态搜索、负载恢复不在本轮），符合 §4 收敛意图。

---

## 与 A01..A08 覆盖核对（修正后）

| A0x | 计划覆盖 | 评审判断 |
|---|---|---|
| A01 沙箱真实有效 | P32-1/2/16 | 需补 signal/白名单/symlink 三类探针（P0-1,P1-2,P1-4）后才算"非仅查 cwd" |
| A02 超时无遗留 | P32-3 | 必须补 setsid 逃逸用例并改回收实现（P0-2） |
| A03 审批后发布 | P32-7/16 | 结构已具备（artifact_hash 绑定 + receipt 回读）✓ |
| A04 内容变化重批 | P32-8 | 需点明"改内容=补偿路径"（P2-1）✓ |
| A05 回执丢失按业务键 | P32-9 | 依赖 P1-1 修好产物字节留存 |
| A06 非权威不重发 | P32-10 | D6 设计正确，注意默认值回归（P2-2）✓ |
| A07 拒绝权限放大 / L3 双审批 | P32-11 | target 防护 ✓；双审批须 SDK 层测（P2-3） |
| A08 恢复与补偿分离 | P32-11 | `propose_compensation` 独立身份/审批，方向正确 ✓ |

## 沙箱废弃表述诚实性
plan 风险节"seatbelt 已 deprecated 但本机 26.6 仍可用、每次启动探针复验、Linux/Windows 不在本轮"表述**诚实**；"不做 uid 隔离、只非 root 同 uid 运行"如实登记为限制，符合 §4.3"部署环境未知不得宣称已具备"。建议再补一句：同 uid 残余风险（信号/临时文件/mach）靠 seatbelt 规则而非 uid 边界收敛，且内存/进程数为事后回收型软限制。
