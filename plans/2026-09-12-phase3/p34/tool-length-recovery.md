# 有界工具输出截断恢复

最后更新：2026-09-14 CST。源代码验证已通过；后继源码原生验收、严格真实对照尚未运行。

已确认的缺口：OpenAICompatibleProvider 对坏工具调用保存有效usage及白名单finish_reason/parse_stage，但AgentExecutionDriver只对provider_empty_response进入输出增长。现在仅对provider_protocol_error + tool_parse + 明确length + 有效usage复用原增长分支。仍共用原empty_response_retries（兼容字段名）、原ceiling与deadline；默认8K→16K→32K输出，不更改256K/512K输入上下文。没有修补JSON、执行残缺工具参数或覆盖已失败invocation。增长请求有新provider-turn身份及新准入；取消、模型调用次数、金额/Token预算不足均阻止后续真实调用。缺finish_reason/stop/非tool解析/未知usage不增长。

| 检查 | 实际结果 |
|---|---|
| 原生产代码确定性红例 | 4FAIL/7PASS，0.53秒（runner0.72），g-tool-length-red-v1；正例、上限、取消分类和再准入分支不满足 |
| 初次修复相关检查 | 33PASS/1.64秒，runner1.91；含旧empty增长及真实HTTP adapter负例 |
| 扩展控制首次 | 19PASS/1FAIL/1.08秒：测试误认为被拒前没有CLAIMED记录；实际0handoff的第二CLAIMED记录保留，已按真实协议纠正 |
| Agent整套及相关HTTP检查 | 205PASS/5SKIP/20.26秒（runner20.51）；2项需pinned tokenizer后继已补，另3项真实Provider/embedding opt-in不计PASS |
| 最终截断/冷重开/期限/模型次数/真实priced guard/长Context | 28PASS/7.72秒（runner8.03），g-tool-length-final-v4；其中真实8K/16K/32K上限、所有终态新runtime冷读零调用、原消息保持；原Task费用不足时仅一次HTTP，第二CLAIMED无handoff、无第二grant；两份实际已知费用幂等导入 |
| 静态检查 | ruff通过；两修改源文件mypy通过。原execution.py有4个payload变量类型重复错误，immutable-v36 shadow-file确认原有；仅重命名成功payload局部变量消除，诊断内容不变 |

原预算集成用例使用正式CommitService/ProviderBudgetGuard/AgentBridge与两SQLite账本。只验证SDK Turn、Provider grants和已知用量导入，Orchestrator Attempt仍活跃，不能手动settle_subject伪造完整Mission收尾。子代理初版该调用已移除。未知用量原5控制仍不释放UNKNOWN。

修复前268d231（生产c19bbd0）完整编排及相关模块1970PASS/9真实opt-inSKIP，643.26秒（runner643.71），g-phase3-cumulative-v36。这是修复前基线，不冒称后继整个默认套件全绿；后继执行核心整套与相关控制结果如上。

原始证据均在SDK ignored .local-test-evidence/2026-09-12/p33-g/ 对应run的json/log。历史v12 COMPARE没有finish_reason，不能据8192输出反推其必然属于此分支，原严格FAIL不改。一次新固定版本对照可评估后继，但不得循环抽样、删失败或改变原oracle来取PASS。输入材料/预算profile/验收条件必须保持原固定合同。
