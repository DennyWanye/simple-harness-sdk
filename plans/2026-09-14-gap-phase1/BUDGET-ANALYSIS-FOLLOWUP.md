# Planner累计预算说明与N7分析补充

最后更新：2026-09-15 00:50 CST。正式A96/B96未开始；本文件记录源码修复与分析工具验证，不是完整N2/N7验收。

## 问题与改变

N2本地v2官方任务530b157_1在429.665秒失败，17调用236536tokens，未知0。Planner明确提出261120/261120/261120/522240四份预算；normalise_budgets没有改写它们。这些值恰好等于首请求接纳下限。4M总预算未耗尽，第一Task因累计额度不足被正确拒绝。原始失败、24次已执行工具和官方失败评分保留，不重发旧调用。

父审与Sol只读审计确认：build_planner_package原把通用budget_allocation_semantics放在source_workload分支内，AppWorld/code没有文档工作量，因此收不到累计额度说明。现在所有新Planner包都收到原有说明，并明确min_task_tokens只是首请求下限，不是建议累计额度或上下文上限。文档专用criteria/source描述仍只在原分支；已有持久化Planner请求不重写，默认即可使用新提示。预算、接纳算法和normalise_budgets未改，不强制分配整个预算池。模型是否因为缺提示而选最低额度是推断；新模型结果才能检查实际行为，不能承诺仅改提示就成功。

N7增加显式可选task-block bootstrap percentile区间：题内重复先平均，按任务块重采样。样本数100–100000、固定seed、confidence和方法写入独立analysis_identity；默认报告结构兼容。至少2个任务，完整效用配对才生成区间；成本未知独立保留，不能抑制已有效用配对，也不能写成成本收益已证明。提供小样本限制，不报告p-value或臆测收益。正式批次冻结时显式指定分析参数；当前只是确定性合成数据测试。

## 验证与边界

- 旧源码快照上的新AppWorld/code实际Planner dispatch测试：2FAIL/0.33秒，均缺budget_allocation_semantics。之前两次测试夹具的JSON解析/环境缺绑定错误已保留在本地初版日志，不计作决定性缺陷证据。
- 修复后父级组合87PASS/5.49秒：预算dispatch、原文workload、域prompt、AppWorld知识链与N7分析；ruff通过、两源码mypy通过（ignore-missing-imports只用于可选外部依赖）。
- N7子测试38PASS/0.08秒；有12×2仍12独立单位、3任务反伪重复、符号/seed/缺失/未知成本负控。只有测试计时，不充当总开发时间。
- 完整2240PASS/32SKIP/668.77秒属于前一d495382源码；不声称覆盖本文件两项后继变化。当前相邻87测试是新增源码的明确范围。
- 新Qwen校准appworld-n2-local-v3已用359文件快照启动：F/256K/1物理槽/4M/40调用/1800秒，旧输出8192起32768顶保持。独立episode，不加入正式96次；N7离线模块不参与该执行。真实终态仍待。
- 新源码UI v52已完成78文件冷复制和360项SDK源/元数据固定，启动后实际交互验收继续；不把进程运行当UI通过。此前v51冷恢复/回放通过属于上一源码。

|证据相对Host .local-test-evidence/索引|SHA-256|
|---|---|
|2026-09-15/gap-two-wave/budget-old-source.log|f3f33cb10ba5ecfcc086161c6dcb6ae0a76e47e5ecabc1f645c9a5b5ffd49a62|
|2026-09-15/gap-two-wave/n2-n7-parent.log|f610a9a2debdf00c592ebd7a5fd40beab94325a25bfdf29b4985ab4adf064234|
|2026-09-15/gap-two-wave/n2-budget-forensics.md|87b6088c46f8eab7a5c6420cdb1fabd4c9c39d1a91cad49a85b13aeed5b692a1|
|2026-09-15/gap-two-wave/n2-source-v3.json|66d9ca03fbb3270ac1c999eed28b546ca7be387ea4fb4c6536b96fa10c213b16|
|2026-09-15/gap-two-wave/n7-analysis/final-pytest.xml|e68d14ad5febbd7687e3948f7eb94721b21c572cf326397db80dbbdee03181ae|

## 00:55 源码UI后继验收

当前359 SDK源码文件加pyproject的360项attestation与新N2/N7源一致。v52实际点击任务、代码产物、回放、支持报告通过；旧Mission/Task/42事件/5次Provider调用/4工具效果逐行相同。3个VERIFIED产物仅storage_uri按新根重定位，内容hash和其他字段不变；回放41事件、未覆盖/差异/证据缺口均0；本地支持报告9169bytes，SHA99cde161913255bfd65733f040d21c51fdec08f526e37c17c7c6ef6fb9028870。

冷复制78文件保留；最初漏准备新版launcher身份导致两次启动前拒绝，已用canonical source_identity生成新快照身份后正常启动，未绕过校验。旧已停止且无WAL的DB用immutable读取，新运行库用mode=ro包含WAL；不以进程/接口正常代替UI点击。新UI保留运行和防熄屏，本次UI0新模型调用。

证据：Host .local-test-evidence/2026-09-15/gap-two-wave/native-wave-a-context-v52/parent-audit.json，SHA256 a9071c05f02bb72bc631634dfeed56501e104daede01f72e9eaf4fdb2b6bfd70。

新N2 v3 Planner已提出200万/100万/100万的3Task累计预算，窗口仍256K，总4M不变；只是一次行为观察，不证明提示改动因果收益。当前真实执行仍待终态。新完整回归v7正在验证全域Planner提示变化，646源码/测试/配置固定，不能提前报通过。

## 01:07 完整回归与快照断言收尾

新完整v7：2259PASS、32条件SKIP、1FAIL，657.44秒；646源码/测试/配置hash未变，受管生命周期658.022秒、0残留。唯一失败是p32普通Planner历史message/context hash：新增通用预算语义有意改变hash。移除且只移除新增说明的完整文本段后，消息仍精确匹配旧SDK9f70e00基线3c9ec151…。

测试后继保留旧基线校验并明确断言新budget语义、新消息hash c5017c64…及context ctx-c4042b6c85b13dbd；未改任何生产代码以追测试。相邻52PASS/0.71秒、ruffPASS。没有再重复整个全量；不把这次52PASS写成新的全量结果。359生产源码与真实v3/原生v52快照逐项一致。

N2 v3第一Task已进入VERIFYING，后继2Task依赖阻塞；真实终态、知识消费和独立效用仍待。N7统计工具范围已验收，机制收益和正式矩阵未验收。所有21个开发子代理已关闭，近期两子代理实际均Sol/high；完整成本记录仅在私有Host计划，SDK不公开个人开发token明细。

|证据相对Host .local-test-evidence/索引|SHA-256|
|---|---|
|2026-09-15/gap-two-wave/core-regression-v7/command.log|128a492db8174c63740583c49a499ce00d80b8211bcd7446cc3c671d5c1888a7|
|2026-09-15/gap-two-wave/core-regression-v7/resource.json|1b9ced09f14301425e4f5a3fd74710d57f21f1c3a88d87add01eadfb8762acbd|
|2026-09-15/gap-two-wave/core-regression-v7-source-audit.json|152a6f4eca4112b6410d509de621663c6303ca3b54d256ffd159414fc3ce1976|
|2026-09-15/gap-two-wave/hash-followup.log|e5fa25de4203d527715b5d7125370a5f5b5fc2965d790bb67ea8f73a066ce60a|
|2026-09-15/gap-two-wave/planner-hash-migration-audit.json|19c2dbe9a33e487873d758e826e9d10692b673af458b69fc60c60fd7954fbd82|
