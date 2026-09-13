# LC2 真旧库接入及新上下文原生共存

最后更新：2026-09-14 CST。PASS（限定范围，原失败保留）。旧 SDK `6360c205` / source-snapshot-v6 以公开创建、提交和实际受控 Provider 路径生成旧库；新 Host `2eee204e` / SDK `c19bbd0` / source-snapshot-v36 接入，随后以原生 UI 创建新256K/512K任务并冷读。

| 项目 | 结果与边界 |
|---|---|
| 真正旧库 | 一项已完成任务、四次受控调用600tokens；一项 CREATED 任务已有旧 Planner frozen intent。context与外部admission从创建起就是None，没有删现代sidecar冒充旧库。父任务补齐第二项来源登记。准备0.436秒，零外部HTTP；第一次环境准备误用无tomllib的系统Python，改用快照Python后完成。 |
| 旧已完成任务 | mission-22949a20e0bfabed：新UI与冷UI均打开原73B NOTES.md，dcebb4aee4699876ff4350ad2ace66d3049e97fb36c00a66f6029473340ff061，600tokens不变。原四条Provider完整记录哈希保持，不只比计数。 |
| 旧冻结任务续跑 | mission-9787cef45cd58527：实际旧input/config哈希未变；所有后续intent保持default/contextNone/admissionNone。首Planner输出转义JSON导致FAILED，第二Planner正常继续，9真实调用40942tokens完成。实际新报告及引用在UI/冷UI可读，f755b2e04c8098d20662d4b90ec8f42e20e7ce782b207d9a3a0c157560720039。不宣称首次规划成功。 |
| 新256K | mission-7dab7e4cc7c1282f：5真实调用11072tokens，7B CONTEXT256.md内容LC2-256；使用deepseek-context-256k-v1及有界admission。 |
| 新512K | 第一项mission-0e3ce98304d041e4因父任务手填400K预算低于557056floor，2调用2879tokens后planning_failed，保留FAIL。另建同目标mission-829a4661b35101cc采用默认8M总预算，5调用10602tokens正式交付，7B CONTEXT512.md内容LC2-512；冻结deepseek-context-512k-v1。没有修改原预算/原失败。 |
| 冷启动 | 5项任务状态和选定持久化表哈希全部一致，25Provider记录=4旧受控+21真实，0rehandoff，零新增调用。实际打开四项已完成任务的文件；原失败仍显示失败。 |

新真实总消耗65595tokens（含失败2879），旧受控600另计，预留0。原生进程组30645生命周期353.520秒，冷组32403为94.208秒，均正常退出且组内无残留；包含人工操作等待，不等同模型推理时间。防熄屏保留。

原始证据Host ignored `.local-test-evidence/2026-09-13/p33-g/source-ui-legacy-v36/`，截图01–12、旧manifest、before-new-host、before/after-cold及case-summary。summary SHA256 `ca9fca8972256e61c035d33e40251c5b10224ae6a68ad9b15eacdd8ed4ffe7c2`。冷前543e10f75041bcdcdd32f36da6f7f4daae329c54624ed5c54a5133840d404d70，冷后ba170911c466fd5502adebee5ad3dc8c48feeb216a5fb2051705ca4b05b3609d。

本次原生任务按序运行，不声称同时压满全局物理槽位；全局cap、UNKNOWN与OS-kill属于既有LC2受控测试证据。无法推断没有任何历史派发事实的旧default身份，这个边界仍保留。512K不足预算只在规划后解释、普通产物长hash可能溢出，是现有可见体验限制。整体Phase3仍待严格P34真实对照与最新累计审计；打包/安装器暂缓。
