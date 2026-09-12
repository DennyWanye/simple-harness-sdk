# P3.3 F：整仓与安装制品验证

2026-09-12 19:40开始。沿用定稿plan/原46AC，覆盖A07后半、P33-19/21/22/36；G20/45/46仍开放。本片不更改业务行为。

先行oracle与独立挑战：Kepler只读核对基线与构建脚本，main采纳以下要求。

| 门 | 先行预期 |
|---|---|
| 整仓 | 不ignore、不maxfail，--continue-on-collection-errors；逐nodeid记录failure/setup/collection，与a4aae8c同依赖隔离源码复核。原机73=58fail+15error只有汇总，不能仅凭同数量宣布集合包含。额外Memory缺包3项单列，不默许删除测试或新增生产依赖。 |
| 版本 | 两个runtime version使用新0.11.0，contract/schema不因打包再升级；旧0.10.0制品不覆盖。 |
| 构建 | clean HEAD运行现有reproducibility.py，两次wheel/sdist字节相等，SOURCE_DATE_EPOCH=0，记录source/hash；planned-tag不创建或发布tag。 |
| 安装 | 新隔离venv无editable/PYTHONPATH，两个runtime的__file__均来自目标site-packages、版本0.11.0；全部agent_orchestrator包文件与wheel逐字比对。 |
| 安装运行 | 原编排全量在安装包执行，保持正式真实Provider skip；BaseAgent和既有迁移失败也单独核对，不默认忽略旧红。 |
| 边界 | G新增SDK判定树挂源/原子来源创建/引用读取后必须再构建最终制品；F wheel仅A–E candidate，不能当作G最终wheel或Host/原生证明。 |

历史资料：p31-fixes/journal§3、p32/journal§3。旧15setup要求真实H077_LEGACY_075_TARGET，当前与baseline保持同环境；不伪造旧数据。artifact fixture会重建制品，保留对应身份，不混称本片冻结wheel的验收。

baseline archive包含src/tests/scripts/.github/docs/provenance/LICENSES和pyproject.toml/uv.lock/README/CHANGELOG/NOTICE/REUSE/source-manifest/.gitignore；tests跨目录fixtures必须完整。不存在原始基线日志的红以同环境旧源码重现为准。所有原始产物放本机ignored p33-f，主唯一pytest runner。
