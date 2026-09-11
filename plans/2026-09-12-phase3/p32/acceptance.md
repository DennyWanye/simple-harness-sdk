# P3.2 · 验收（第 2 版）

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

| 编号 | 场景 | 什么算对 | 测试层级与证据 |
|---|---|---|---|
| P32-1 | 沙箱探针 | 8 项探针全部得到预期结果，每项都写进报告：读白名单外的敏感目录（HOME/.ssh）→ 被拒；读 `/private/var/folders` 下别人的目录 → 被拒；写工作区外 → 被拒；联网（TCP 与 Unix socket）→ 被拒；给宿主进程发信号 → 被拒；setsid 出去的孙进程 → 被回收；输出超上限 → 被截断；CPU 超上限 → 被终止。探针的目标路径与读白名单互斥（用断言保证）。任何一项不符合，适配器就判为不可用 | SDK `test_sandbox.py` |
| P32-2 | 行为 oracle | 模式为 `sandboxed` 时，Worker 写的测试文件在导入时会尝试读 `~/.ssh`、写 HOME、联网、给宿主测试进程发信号。测试照常运行，这四件事全部失败：宿主进程还活着，也没有留下任何文件 | 同上 |
| P32-3 | 超时无遗留 | 三个用例：同组孙进程；`setsid` 出去的孙进程；双重 fork 的守护进程（它的 cwd 在工作区里）。超时后三个用例都查不到残留进程（`ps`），`tree_killed=true` 以实测为准，输出有上限。还要验证：残留杀不掉时，结果判为 ERROR，并写出 `residual_pids` | 同上 |
| P32-4 | 语义、兼容与软限制 | `code_execution` 三种取值各自的行为；旧字段 `local_code_execution` 保持兼容；默认部署的行为不变；`sandboxed` 但没有通过探针的适配器时，构造就报错；0.9.x 下 `off` 的测试全部通过。RSS 超过阈值、进程数超过阈值时会被回收，`effective_limits` 标为软限制 | SDK `test_code_execution_modes.py` |
| P32-5 | R13 | 召回消息的 role 不是 SYSTEM；正文在 `<recalled_history …>` 数据框里，并注明"不是指令"；召回原文里的注入文本也拿不到 SYSTEM 身份 | SDK `tests/agents/…` |
| P32-5a | 软链不能外泄（评审 P0-1） | Worker 在工作区里建一个指向宿主密钥的软链。下一次 Attempt 的工作区、`-verify` 副本、集成副本里都不会出现密钥字节；这个结果判为 ResultRejected（`workspace_symlink`），拒绝原因里不带链接的目标；`snapshot` 不登记软链；`artifact_read` 和发布遇到软链一律拒绝 | SDK `test_workspace_symlinks.py` |
| P32-5b | 内容寻址产物库（评审 P1-1） | 产物字节在登记时写进产物库，并核对 hash；删掉工作区目录后，`artifact_read`、发布、评测 oracle 仍能按 hash 读到内容；从 v6 迁移到 v7 时会补写产物库，原文件已经缺失的标记为 `unavailable` | SDK `test_artifact_store.py` |
| P32-6 | 工作区登记 | 每个 Attempt 都有登记记录：base_snapshot、只读输入、可写输出。目录已存在但登记对不上时，拒绝；Mission 终态加保留期之后清理目录，登记与产物库都还在；回放与对账扫描照常通过 | SDK `test_workspace_registry.py` |
| P32-7 | 审批后发布 | 流程：候选 → 验证 PASS → 审批 → 交接 → 以带稳定身份的文件名原子写入。回读的 hash 等于产物 hash；回执、ledger、产物库三处一致 | SDK `test_publish_connector.py` |
| P32-8 | 内容变化重批（评审 P2-1） | 执行之前内容变了：生成新版本，旧的批准对新版本无效。已经 SUCCEEDED 之后再用同一个业务键改内容：普通提议被拒（`action_already_executed`），必须走补偿 `publish_new_version`，而且补偿需要新的批准 | 同上 |
| P32-9 | 发布回执丢失 | 故障注入：link 已经完成，但回执还没写就崩溃。重启后按稳定业务键核对原文件，结果是 SUCCEEDED；目录里没有第二份文件 | 同上 |
| P32-10 | 非权威查询不可重发 | `best_effort` 的连接器，lookup 返回 None 时保持 UNKNOWN，不重发；`authoritative` 的才能确认"没开始"；TestConfigService 标为 authoritative，step07 原有的测试照常通过 | SDK `test_lookup_authority.py` |
| P32-11 | 拒绝权限放大；补偿 | target 带 `..`、是绝对路径、或经过 symlink 的，一律拒绝并记下依据；`retract`（L3）默认拒绝；两个合成 Principal 的双人审批机制；补偿动作有独立的身份和批准，原事实不变 | SDK `test_compensation.py`（只在 SDK 层做） |
| P32-12 | SDK 交付 | 全量回归红集 ⊆ 73（step07 因权威查询语义要改的断言事先登记）；wheel 0.10.0 在干净环境验证；代码评审意见已处置；P3.1 遗留的几项源码修改已落实 | journal |
| P32-13 | Host 探针接线 | 探针通过 → `sandboxed`，部署清单里带上探针报告；不通过 → `off`，并写明原因；`pytest:` 条件与 `run_tests` 随之开关 | Host `tests/orchestration` |
| P32-14 | Host 发布目录 | 用户没选目录时，`action:file_publish…` 被拒，原因是"需要明确授权"；选定之后才启用；target 越出 root 的一律拒绝 | 同上 |
| P32-15 | Host 界面 | 能区分"已生成未发布"和"已发布"；UNKNOWN 显示为"核对中"；审批卡显示路径、hash、版本 | vitest |
| P32-16 | 真实与原生 | 用 deepseek-flash 做原生 App 验收：请求报告 → 看到"未发布" → 批准 → 目录里出现文件，回读的 hash 与界面一致。另跑一轮带 `pytest:` 的 Mission，要在沙箱里跑，并附上探针证据 | Host 报告 |
