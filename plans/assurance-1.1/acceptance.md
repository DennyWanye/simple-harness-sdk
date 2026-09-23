# Assurance 1.1 acceptance

plan-status: finalized (用户批准 1.1 + R01–R03 后，于 2026-09-22 明确要求开始隔离开发)

原 48 SDK 组、继承 66、OCC 12、BODY-WIRED 和 12 模型 trial 的不可删减预期，分别见本目录 sdk-cases.json、inherited-coverage.json、occ-coverage.json、BODY-WIRED.md、model-scenarios.json。完整正文与审查修正同时有效。

当前为主体开发，不声称任何上线切片完成。FULL：多阶段状态机、schema、跨层契约；集中主 Agent 编码。共享集成等待最终 HTN 与 TaskGraph 公共文件交接，独立模块继续。整个计划所有 MUST 保留。

先冻结的决定性反例：R01 每个自然 UNIQUE 键不同 PK 的 REPLACE 必须拒绝；R02 同 seq 异 fingerprint 必须回滚 cursor 与入队；R03 无业务事件到期后产生唯一原 Event 和更高真实 seq，旧 worker 不得 ACK；语义检查不伪造 PASS，缺失 CHECKED 结果保持 UNKNOWN；catalogue 不是 disclosure；退钟不延长到期。实际 SDK/runtime、原生及模型验收在完整 BODY_WIRED 后进行。
