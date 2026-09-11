# P3.2 · 验收（第 3 版）

## 与 Phase3 P3.2-A01..A08 的对照

| P3.2 | 本计划 |
|---|---|
| A01 沙箱真实有效 | P32-1、P32-2、P32-5a、P32-16 |
| A02 超时无遗留 | P32-3 |
| A03 审批后发布 | P32-7、P32-16 |
| A04 内容变化重批 | P32-8 |
| A05 丢失发布回执 | P32-9（依赖 P32-5b 的产物库） |
| A06 非权威查询不可重发 | P32-10 |
| A07 拒绝权限放大 | P32-11 |
| A08 恢复与补偿分离 | P32-11 |

## 条目

| 编号 | 场景 | 什么算对 | 层级与证据 |
|---|---|---|---|
| P32-1 | 沙箱探针 | 8 项全部得到预期结果，并写进报告：<br>① 列出真实家目录，被拒；<br>② 读 `/private/var/folders` 下他人的目录，被拒；<br>③ 写写路径之外，被拒；<br>④ 联网（TCP 与 Unix socket），被拒；<br>⑤ 给宿主进程发信号，被拒；<br>⑥ 标准 daemonize 出去的进程，被回收；<br>⑦ 输出超上限，被截断；<br>⑧ CPU 超上限，被终止。<br>探针目标与读白名单互斥（有断言）。任何一项不符合，适配器就判为不可用 | SDK `test_p32_sandbox.py` |
| P32-2 | 行为 oracle | 在 `sandboxed` 下，Worker 写的测试文件在导入时尝试四件事：读宿主上的秘密文件、列出家目录、写 root 之外、联网（TCP 与 Unix），外加给宿主测试进程发信号。结果：测试照常运行，这些尝试全部失败；秘密字节不出现在输出里；宿主上没有留下任何文件 | 同上 |
| P32-3 | 超时无遗留 | 四个用例：<br>① 同一进程组的孙进程；<br>② setsid 出去的孙进程；<br>③ 双重 fork 的 daemon，cwd 仍在工作区；<br>④ 标准 daemonize：setsid → 二次 fork → `chdir("/")` → 关掉全部 fd → sleep。<br>seatbelt 适配器下，四个用例在 `ps` 里都查不到残留，`tree_killed=true` 以扫描结果为准。process_only 下 ①②③ 都查不到残留；④ 登记为已知缝隙，并断言回执的 `isolated=false`。<br>残留杀不掉时，结果判为 error，并写出 `residual_pids` | 同上 |
| P32-4 | 语义、兼容与软限制 | `code_execution` 三种取值的行为；旧字段保持兼容；SDK 默认 `process_only` 的行为不变，回执写 `isolated=false`；`sandboxed` 但没有配通过探针的执行器时，构造就报错；关掉执行时，0.9.x 的测试全部通过。RSS 或进程数超过阈值时被回收，`effective_limits` 标为 soft | SDK `test_p32_code_execution_modes.py` |
| P32-5 | R13 | 召回消息的 role 是 USER；正文在 `<recalled_history … untrusted="true">` 数据框里，后面有"不是指令"的声明；召回原文里的 `</recalled_history>` 被转义，不能提前闭合数据框；metadata 不变 | SDK `tests/agents/test_recall_untrusted_frame.py` |
| P32-5a | 软链不能外泄 | 用例：run_tests 里的代码在执行副本中建一个指向宿主秘密的软链。结果：执行副本删除之后，正式树里没有这个软链。另外，把软链直接放进正式树、repair 种子、integrated copy 的来源，都会被拒，理由是 `workspace_symlink`，拒绝原因里不带链接目标；`snapshot` 遇到软链也拒绝。整个过程中，秘密字节不出现在系统产生的任何文件里 | SDK `test_p32_workspace_symlinks.py` |
| P32-5b | CAS 与"验证的就是登记的" | ① 产物字节在登记时写进 CAS，并核对 hash，文件只读；删掉工作区之后，`open_verified` 仍能读到。<br>② **登记之后改动活树**，验证副本里仍是登记时的字节。<br>③ CAS 文件被篡改或被换成软链时，读取报 integrity_error。<br>④ v6 → v7 迁移：hash 相符的产物补写进 CAS；缺失的或 hash 不符的，`storage_state=unavailable`；迁移可以重跑 | 同上，另加 `test_p32_artifact_migration.py` |
| P32-6 | 工作区登记 | 每个 Attempt 与各类副本都有登记。已存在的目录：身份相符就复用（恢复路径不会卡住）；登记是 CREATING 的半成品目录，删掉重建；身份不符的，拒绝。清理之后，登记与 CAS 都还在；回放与对账照常通过 | SDK `test_p32_workspace_registry.py` |
| P32-7 | 审批后发布 | 流程：候选（写 `artifact_path`）→ 系统绑定 hash → 验证 PASS → 审批 → 交接 → 先写 intent → link。回读 hash 等于产物 hash；回执、ledger、CAS 三处一致；`Receipt.target` 是规范化 target | SDK `test_p32_publish_connector.py` |
| P32-8 | 内容变化重批 | 执行之前内容变了：生成新版本，旧的批准作废。已经 SUCCEEDED 之后要改内容：普通提议被拒；走补偿（`#comp-1`）需要新的批准，原事实不变 | 同上 |
| P32-9 | 发布回执丢失 | 在三处注入故障：<br>① intent 之后、link 之前失败 → 连接器当场补写 ABORTED，lookup 判为未开始，重试后只产生一份文件；如果是进程真的崩在这里（只留下 PREPARED、文件不存在），则判为 STILL_UNKNOWN，不自动重发；<br>② link 之后、COMMITTED 之前崩溃 → COMPLETED，不重发；<br>③ COMMITTED 之后、回执返回之前崩溃 → COMPLETED。<br>另加两个场景：发布之后用户删除或修改文件，然后 reconcile → STILL_UNKNOWN，不重发；账本末行没写完 → 丢弃该行 | 同上 |
| P32-10 | 非权威查询不可重发 | best_effort 的连接器，lookup 返回 None 时保持 UNKNOWN；L2 及以上的 best_effort 连接器被拒；step07 原有测试不改断言也全部通过 | SDK `test_p32_lookup_authority.py` |
| P32-11 | 拒绝权限放大；补偿 | target 带 `..`、是绝对路径、或经过软链的，一律拒绝，并记下依据；`artifact_path` 不属于已 accept 的 Result 的，拒绝；`retract`（L3）默认拒绝；两个合成 Principal 的双人审批；补偿有独立的业务键和审批 | SDK `test_p32_compensation.py`（只在 SDK 层做） |
| P32-12 | SDK 交付 | 全量回归红集 ⊆ 73；`test_facade.py:424` 的改写事先登记；wheel 0.10.0 在干净环境验证；代码评审意见已处置；P3.1 遗留的源码修改已落实 | journal |
| P32-13 | Host 探针接线 | 探针通过 → `sandboxed`，部署清单附上探针报告；不通过（包括冻结版）→ `off`，并写明原因；`pytest:` 条件与 `run_tests` 随之开关；Host 不会进入 `process_only` | Host `tests/orchestration` |
| P32-14 | Host 发布目录 | 没选目录时，发布被拒，原因写"需要明确授权"；不支持硬链接的目录不能授权；target 越出 root 的一律拒绝 | 同上 |
| P32-15 | Host 界面 | 能分清"已生成未发布"和"已发布"；UNKNOWN 显示为"核对中"；审批卡显示路径、hash、版本 | vitest |
| P32-16 | 真实与原生 | 用 deepseek-flash 在原生 App 里验收：请求报告 → 看到"未发布" → 批准 → 目录里出现文件，回读 hash 与界面一致。另跑一轮带 `pytest:` 的 Mission，要在沙箱里跑，并附上探针证据。证据里写明候选启动器不代表打包版 | Host 报告 |
