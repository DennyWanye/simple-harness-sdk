# testPhase1 四臂试验与源码验收结果

最后更新：2026-09-14 16:48 CST。16/16 固定开发集 episodes 已执行完毕；测试完成不表示所有算法都成功。

本轮优先应修复 Planner 子任务预算与真实运行时预留要求的不一致，以及R自选输出的协议闭环，然后再做新身份的匹配对照。不要直接扩大到96次，也不应以切换模型掩盖预算拒绝。新代码知识语义、当前来源消费、Host工具门和启动恢复修复已完成源码及原生验收。

## 固定条件

双DGX `qwen38-flash-next`；总窗口262144、有效输入228352、输出预留32768、安全余量1024，输出从8192有界扩展至32768。四臂物理模型槽均为1；160万输入/总tokens、131072输出tokens、60调用、1200秒/episode、600秒/HTTP。World seed100、默认模型采样，无模型seed参数。4题×4臂×1次，拉丁方顺序；不补跑失败、不混入校准、不选择性换模型。

SDK实验源码冻结 `2f8eacfc6255e110e1ee2dafd4d4dac180fc42c9`。R使用同一BaseAgent两候选自选，并明确使用模拟世界保存/恢复；此为项目开发试验设置，不是官方完整榜单协议。外部评分在执行停止后进行，隐藏答案未传给Agent。

## 四臂结果

|臂|运行时成功|官方终态成功|有效闭环成功|错误宣布完成|模型调用|全部tokens|SDK工具请求|全部episode耗时秒|每有效成功的tokens|
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
|S|4/4|3/4|3/4|1|82|749575|91|943.23|249858.33|
|R|2/4|4/4|2/4|0|104|1289571|112|1287.27|644785.50|
|D|0/4|0/4|0/4|0|50|599094|64|1624.10|无成功，未定义|
|F|0/4|0/4|0/4|0|46|437946|56|1904.13|无成功，未定义|

正式16次合计282次模型调用、3076186tokens（输入2945874，输出130312）；批次墙钟5758.78秒。全部成功与失败均计入，逐episode时间之和与批次墙钟略有边界开销差异。

有效闭环成功要求运行时完成且官方评分成功，两项原始结果仍分别列出。R第三、四题自选输出包含解释文字，不满足约定纯JSON，解析抛JSONDecodeError，尚未执行选定候选的恢复；外部评分看到的是异常发生时留下的候选世界状态。因此官方终态4/4只能作为状态评分，R有效自选闭环为2/4，不能宣称R全成功或优于S。没有重新解析原输出、补跑或把这两次失败改成成功。

R异常时arm_result没有返回工具总数；上表从SDK持久效果计数，共323次工具请求。网关实际收到322次；多出的1次在SDK内已被拒绝，没有进入网关。原基础汇总中工具计数未知的记录保留，不以0替代。

缓存tokens和推理tokens若未由Provider提供，保持未知，不写0。付费DeepSeek调用为0；DGX电力与设备成本未计价，不能把总运行成本记成0。四道不同dev题各一次没有统计显著性保证，不报完整SGC或官方榜单成绩。

## 全部16次结果

|题目|臂|运行时结果|官方成功|错报完成|tokens|调用|时间秒|停止原因|
|---|---|---|---|---|---:|---:|---:|---|
|37a8675_3|S|success|False|True|327244|28|247.80|正常返回|
|37a8675_3|R|success|True|False|501944|29|343.69|正常返回|
|37a8675_3|D|failure|False|False|122585|12|279.92|budget_exhausted|
|37a8675_3|F|failure|False|False|120163|11|467.33|budget_exhausted|
|d4e9306_1|R|success|True|False|215416|23|242.63|正常返回|
|d4e9306_1|D|failure|False|False|270958|21|524.44|budget_exhausted|
|d4e9306_1|F|failure|False|False|97940|11|681.13|budget_exhausted|
|d4e9306_1|S|success|True|False|112050|14|167.49|正常返回|
|4ec8de5_1|D|failure|False|False|9463|1|343.83|budget_exhausted|
|4ec8de5_1|F|failure|False|False|138319|13|423.32|budget_exhausted|
|4ec8de5_1|S|success|True|False|191889|23|401.12|正常返回|
|4ec8de5_1|R|failure|True|False|330179|27|463.35|JSONDecodeError|
|6c2c621_3|F|failure|False|False|81524|11|332.35|budget_exhausted|
|6c2c621_3|S|success|True|False|118392|17|126.82|正常返回|
|6c2c621_3|R|failure|True|False|242032|25|237.60|JSONDecodeError|
|6c2c621_3|D|failure|False|False|196088|16|475.91|budget_exhausted|

## 原因与机制证据

|臂|已创建Worker Attempts|图修改数|Knowledge记录数|选入请求的知识引用数|SDK工具重复下发数|
|---|---:|---:|---:|---:|---:|
|D|3|0|0|0|0|
|F|4|0|0|0|0|

预算拒绝不是“全局160万全部消耗完”。第三题D只产生一个Planner调用（9463tokens），首任务预算250000，低于首次Critic需要的261120预留，Worker未创建即停止。其余失败还包含Worker执行后，在保留Critic额度时无法接纳下一请求。当前小预算任务切分与全窗口预留配合不良，规则拒绝可以正确工作，但计划仍可能无法执行。

动态配置开启、已有知识模块都不等于本轮产生了收益。上表只报告实际记录的机制使用；没有逐机制反事实/消融，不能给出因果复用贡献。完全重复的工具请求也可能是合法查询或R重置候选后重查，不能直接当作无关重做。

全部已结束D/F Mission预留tokens均为0。SDK工具效果状态合计：{'succeeded': 321, 'failed': 1, 'rejected': 1}；网关有1条缺失outcome。第三题S的一次缺失由SDK持久记录确认是tool_handler_failed，未发生自动重下发；该题还保留一次工具拒绝，最终官方仍成功。原网关记录未被改写，分析脚本改为区分缺失字段与SDK权威结果，不把缺失当成功。

## 功能验收与保留边界

- 最终功能源码：SDK84b0a2e1/Host e0aed2aa隔离快照；与已推送SDK84c3235/Host23cb7d37的相关代码文件字节一致。后续提交仅补充报告。
- 完整编排回归2079PASS/20SKIP/0FAIL，pytest879.28秒；20项为显式真实Provider opt-in及可选tokenizer，不能算通过。此前失败原样保留。
- 最新关联196PASS/34.79秒；Host相关9PASS/24.39秒。旧wheel RECORD两项不适用于editable source，本轮不打包。
- 原生v45真实两Task、实际原文消费、pytest各18PASS、独立Critic和人工审批完成；15调用90994tokens，13表与15Provider行同目录冷恢复一致，零重调。v46复制数据重开仅5个产物storage_uri迁移，实际文件哈希和业务状态保持。
- T6两个65K输入调用传输通过；严格文本格式因前导空行失败保留。未证明两路近256K容量，默认物理槽仍1；未启用条件性的付费高并发压力。
- 五题开发校准独立保留1成功4失败，其中一次旧180秒超时用量未知，不拼入正式16次的成功率或精确用量。

## 后续建议（不是本轮已实现）

|优先级|工作|建议测试模型|估计时间|
|---|---|---|---|
|P1|让Planner分配满足运行时最低预留的预算；保留总额限制，验证任务可启动及总额内重分配|确定性负控＋本地|工程2–4小时，后续匹配实验运行另计|
|P1|R自选输出协议：解释文字等非纯JSON必须明确拒绝或按新冻结协议解析；选择失败不能用遗留候选状态冒充闭环成功|确定性协议负控＋本地|约1–2小时，重新配对另计|
|P2|补齐AppWorld工具异常路径的网关终态记录，继续以SDK持久结果为准|确定性故障注入|约0.5–1小时|
|后置|修复后以新身份做匹配对照，之后再决定96次扩展|本地优先；确有能力/容量需要才另开DeepSeek完整块|小样本约本次批次量级；96次尚未授权|

本轮没有实现上述预算策略优化、完整自然语言知识证明、AgentDojo、Gaia2、打包或发布。不能把测试执行完成写成完整编排设想全部完成。

## 本地证据索引

相对于Host `.local-test-evidence/2026-09-14/gap-phase1/`。原始证据留本机，不上传Git；详细原生索引及历史失败见执行记录。

|文件|SHA-256|
|---|---|
|`pilot-local-v1/experiment.json`|`c6617849ec70cd75bab672bb0995116f772b7aa7a8b4c740193d6acb395140bf`|
|`pilot-local-v1/public-summary.json`|`7007ba8c5b2bdcc2c6aa07265dcad6ad0012a96668f7b4121ac6744d3440ca87`|
|`pilot-local-v1/analysis-summary.json`|`bd42ac675918174ceefe12761ae625965b7bb9162d04de92810c2b8c7371fdd2`|
|`pilot-local-v1/mechanism-summary.json`|`6941224ae121a0eadb7bbf480ea58a4f245ea3bc55b99a4698a55f38df339714`|
|`pilot-local-v1/appworld-pilot.json`|`41e7ce75cd587a44b0203a781f996428a32289a2690d9fba9925ee7f6d507d94`|
|`pilot-local-v1/environment.json`|`171c9f40ff9330be4c9207acb0e7e2be411031f4c4d6ed755c2417197ada3844`|
|`pilot-local-v1/settings.json`|`1a5d8a0d26ab0e47cc582bbb4452e0c043f18c0f894175155ea4cea51cca00bb`|
|`pilot-local-v1/source.json`|`e2c165a56df7502b8a9c37e399cf3ce0aa8bccb4847464bcc8ee672038386cba`|
|`pilot-local-v1-process/resource.json`|`e96f5c40ad50a43e400a9b20e79a900084930b3f7a80e6a76f8976a97701bfde`|
|`gap-full-orchestrator-v46/resource.json`|`58deabd15d0d19564fb822f2ee35dccd351a7d5df07e80d38a84e517d0cfcdbc`|
