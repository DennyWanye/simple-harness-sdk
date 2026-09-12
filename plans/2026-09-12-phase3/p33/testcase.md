# P3.3 验收索引

最后更新：2026-09-12。原始 MUST 与退出门槛见 [acceptance.md](acceptance.md)；本页只记录实际执行结果，不改冻结预期。

## 切片 A

| 断言与步骤 | 回归入口 | 结果与边界 |
|---|---|---|
| P33-09 闸门 1：经整图入口提交 pytest 准则被拒；另一个合法文档图可成功提交 | test_p33_domain_binding.py（a19/a20） | PASS；整图拒绝与独立合法对照 |
| P33-09 闸门 2–5：经图变更、单 Task、冲突插入、综合插入提交 pytest 准则；拒绝并保持事务原状态；恢复合法准则后同实例重试成功 | test_p33_remaining_domain_gates.py | PASS；这是 Task 准则入口，结果 evidence kind 后续在 B/C 覆盖 |
| P33-22 领域部分：绑定快照后替换注册表、关闭重开库，检查规则仍取冻结值；损坏新版字段拒绝 | test_p33_domain_binding.py；test_p33_domain_prompts.py | PASS；sources 事件与回放亦已由 B 覆盖 |
| A07：文档 11 个角色采用自己的文案和工具；code 默认模板与上下文保持兼容；实际派发配置记录相应版本 | test_p33_domain_prompts.py；step09/test_policy_binding.py | PASS；实际 SDK 的确定性 Provider/dispatch 控制，不是真实模型质量或 App 验收 |
| 恢复：旧 Critic 成功但尚未记录层时崩溃；重开库复用原 ordinal，并在人工审阅后复用匹配层；错误身份与版本不可复用 | test_p33_critic_provenance.py | PASS；两个真正关闭/重开 SDK 与数据库的参数化场景 |
| 嵌套工作区代码测试：父配置及 conftest 不被加载，工作区有效配置与错误语义保留；沙箱逃逸控制继续运行 | test_p33_pytest_workspace_config.py；p32/test_p32_sandbox.py | PASS；pytest 8，ProcessOnly/Seatbelt；不声称 pytest 9 兼容 |
| 消失执行器：在旧 B 消费脚本前门控，关闭 runtime 后模拟不可见，再接管；B LOST 后重试成功，A 不重跑 | step03/test_multi_scheduler.py | PASS；修复 fixture 的隐含调度假设，生产恢复语义不改 |

命令、首次失败、前后源码对照、完整回归的提交身份与 SHA-256 见 [journal.md](journal.md) 和 [baseline.md](baseline.md)。原始证据留在 ignored 的本地目录。

## 切片 B

| 断言与步骤 | 回归入口（tests/orchestrator/p33） | 结果与边界 |
|---|---|---|
| 登记、审批、幂等、ABA、租户绑定、Unicode 路径拓扑、回放 | test_p33_sources.py | PASS；74 项，实际 SQLite/CAS/facade |
| 七类失败码、完整句/列表/表格/标题、locator/display_block、跨租户同形拒绝 | test_p33_evidence_resolver.py | PASS；56 项；resolved 不等于知识 VERIFIED |
| 派发冻结版本、重开库、撤销/文件目录替换、ACTIVE 篡改保留、文件工具信任标记与收集拒绝 | test_p33_source_runtime.py；test_p33_source_workspaces.py | PASS；SDK runtime 确定性 Provider 控制，不是原生 App |
| 发布批准、handoff/re-handoff、全库来源隔离、CAS/工作区/符号链接别名 | test_p33_publish_source_guard.py | PASS；实际审批/outbox/文件连接器 |
| citations 严格契约、文档 evidence kind 拒绝 | test_p33_citations.py；test_p33_result_evidence_gate.py | PASS；空 citations 保留旧信封编码 |

干净源码 `fb58bf1`：完整编排 867 passed / 8 skipped / 0 failed（488.39 s）；专项 P33 + workspace registry + step02 workspace/gateway 共 300 passed（8.13 s）。失败反例、审查闭环、命令与证据 SHA-256 见 journal；8 个真实 Provider skip 不计为真实模型验收。

## 后续门

C 评估与 attribution、D 证据不足、E 失效和冲突、F 整仓/wheel、G Host/真实 flash 原生验收仍未完成。A/B 的 PASS 不能扩大为 P3.3 完成。
