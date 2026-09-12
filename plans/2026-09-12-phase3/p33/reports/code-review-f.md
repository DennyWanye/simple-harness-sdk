# F 独立回归与制品审查

2026-09-12；主唯一pytest runner，Kepler只读审查。

- version-only candidate审查通过；首次完整回归5个公共API快照红属真新增，修复仅version字段，历史列表逐字不变，独立ACCEPT。
- Kepler独立提取final/baseline原日志的kind+nodeid，两者60FAILED+18ERROR集合完全相同，新增0。2个Memory断言同因、18ERROR前置缺失未关闭；不称整仓绿或raw⊆73。
- 制品SHA/manifest/BUILD_INFO/SHA256SUMS一致；wheel304文件、sdist698预期输入逐字匹配5bcca08，ZIP CRC/RECORD/metadata/entrypoint无缺失或漂移。限定artifact ACCEPT。
- installed预检与本轮实际执行同一wheel；来源验证覆盖主pytest解释器258模块、304packagefiles，违规0。唯一迁移assert10==7在新旧包同因；Kepler最终安装限定ACCEPT：orchestrator1199/8skip、agents177/3skip、publicAPI6通过、migration20通过/1既有失败；不包含Host/真实模型/G最终制品。

F 验证完成（保留既有红集）：干净源码 `5bcca08fe666b8e20524206b76ce2afbba63db4d` 整仓3241 passed /60 failed /18 errors /13 skipped（547.16秒）；与同依赖旧源码a4aae8c的78项红集按kind+nodeid完全相同，新增0。0.11.0安装验证1402 passed /11 skipped /1既有迁移失败（506.99秒）；304包文件逐字匹配，258实际加载模块均来自安装包且哈希一致。F不是整仓全绿或新正式发布；G、Host与真实flash仍未完成。
