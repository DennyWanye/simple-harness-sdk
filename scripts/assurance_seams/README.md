# 小范围接线诊断源码

仅用于主体编码过程中解决具名接线问题；禁止批量运行这些脚本并当作产品验收。

从 Host 仓库根执行：

```bash
uv run --project sdk/simple-harness-sdk --frozen --with jsonschema sdk/simple-harness-sdk/scripts/assurance_seams/critic-format-repair-seam.py
```

这是实际 AgentRuntime + ScriptedProvider 的两次调用、一次格式修复、一个 official 与幂等重放诊断。ACL、routing、lease、Requirements 和单 consumer pump 是 fixture；不证明实际 Provider、完整四 consumer、acceptance、Host UI。

脚本由本机历史诊断源移植，路径改为仓库相对定位，输出保存到根 `.local-test-evidence/<日期>/assurance-integration/`。没有复制 schema-tooling 依赖包；jsonschema 由上述 `uv --with` 安装。其它脚本保留作后续诊断源码，不承诺当前所有脚本皆绿。详细未完成清单见根 HANDOFF。
