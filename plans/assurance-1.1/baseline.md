# 隔离源码基线

源 /Users/denny/projects/simple-harness-sdk-h1h-impl 的 HEAD + 实际 dirty 源码（含未跟踪代码），复制前后逐文件 SHA-256 对比无变化；未复制 venv、运行数据库、凭据或 wheel。

候选 /Users/denny/projects/simple-harness-sdk-assurance-impl；初始提交 187e1f4dc0147cace1d3c2092b9de9acc2757b11。证据索引在 Host .local-test-evidence/2026-09-22/assurance-implementation/sdk-baseline.json。

绿色基线 NOT_RUN：遵循用户“主体编码前不批量测试”，现有 HTN 历史 PASS 不是此候选验收。期间仅具体阻塞静态/极小探针。未安装或改动共享 Host SDK，未占用生产数据库或端口。
