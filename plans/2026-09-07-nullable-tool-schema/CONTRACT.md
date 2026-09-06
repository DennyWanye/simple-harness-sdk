# 可选无值：SDK/Host 最小后继

2026-09-07。H079 vendor manifest指定源码f841098b11c40192f1ad6b5676148533567ba706；SDK自有稀疏树simple-harness-sdk-nullable-tool-schema/feat/nullable-tool-schema，无新环境。Host自有simple_harness-corpus-clock/feat/context-route-nullable基主2c02be03。旧H079制品/installed不改，后继0.7.10已由主定版，由主统一构建一次，本叶不造wheel。

|边界|旧|新|
|---|---|---|
|SDK type|仅单一类型|原单一类型保持；额外只支持长度恰2、包含恰1个null和恰1个原非null类型的数组，接受两种顺序；不支持一般union或anyOf|
|SDK约束|按原类型验证|非null分支继续原全部关键词/长度/数字/数组/对象检查；null仍受enum/const限制，required继续检查键存在；root仍必须单一object|
|SDK畸形schema|部分会抛TypeError|空、重复、嵌套、非字符串、多非null类型均明确SchemaDefinitionError；不静默转换schema或输入|
|Host可选字段|reuse_workspace_of/expected_source_hash为string|仅这两字段string或null。三处reuse判断统一value is not None；缺键与JSON null均不请求复用，空串/空格/占位字符串不是null|
|Host来源|非空reuse需exactsource/binding|保持；create_new有非空reuse但hash为null仍拒绝。resume_existing非空hash仍核精确来源|
|审计|原proposal/hash|不删null键、不归一raw：null与缺键语义相同但hash不同，沿原持久/幂等合同，不借语义等价复用不同raw|

expected_source_hash本来兼用于resume_existing，不能笼统称只属于workspace。memory_standalone无任务来源需求，应省略或给真实JSON null；不得使用非空假hash。已按参数适用性拒绝非null值，不声称辨别hash真假；resume_existing原pin验证保留。

必要控制只围绕本叶：SDK新增nullable形状/两顺序、required与enum/const、旧string和非null边界、对象保留字段/资源边界；Host真实wire→SDK→handler/ledger的null与省略、非空假reuse/假hash拒绝、create_new真来源成功和缺hash拒绝。夹具/真实公共路径分别标明，不把本地控制叫模型质量。C01-10/13/20原FAIL全部保留，不重复模型请求。
