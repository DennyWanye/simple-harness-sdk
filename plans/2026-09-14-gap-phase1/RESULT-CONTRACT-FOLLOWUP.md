# N3 结果封套与困难消费后继

最后更新：2026-09-15 02:44 CST。完整N1–N8仍OPEN；本文件分开记录自然失败、受控恢复和新源码验证。

## 已结束的真实本地运行

N2 v4官方AppWorld/F任务530b157_1仍FAIL：28次物理调用、912645tokens/未知0，1557.359秒（受管1561.140秒），40工具效果。Planner分配1.2M/1.5M/1.3M；第一Task因自身预算终止，仅1Attempt，后两Task未执行。此处是既有SDK的Task预算准入拒绝，不是新evaluation meter适配触发；meter admission_denials为空。29个SDK invocation记录=28succeeded+1claimed且handoff_attempt=0、handed_off_at=null，不能计成29次模型调用或物理未知；既有p35恢复合同允许这种零出站claim表示。0已接受Task/知识/复用，官方10检查仅1PASS/9FAIL。保留原失败，不扩大预算，不能宣称新AppWorld知识刷新已获真实验证。

N3均使用Qwen3.8 256K配置、1物理槽、max_inflight393216；实际输入远小于256K，不能冒充近窗压力。三种初始用例后追加一个按操作日期、适用范围和例外优先级判断的场景，其三份资料都是VERIFIED，不能仅靠失效目录过滤回答。

|用例|最终交付|调用|已知tokens|模型/编排实测耗时|
|---|---|---:|---:|---:|
|bilingual-exact-id|COMPLETED / PASS|5|18808|53.731秒|
|conflict-withdrawn|COMPLETED / PASS|4|14985|56.736秒|
|paged-tail-condition|FAILED / FAIL|9|63238|118.588秒|
|temporal-scope-exception|FAILED / FAIL|8|93127|163.716秒|

四个场景的实际原文读取/分页校验均通过；两个失败场景在工作区留下了正确候选答案，但没有VERIFIED产物，不能记成功。分页场景最终envelope非法JSON；日期/范围/例外场景最终为`<result_envelope>{"json":"placeholder"}</result_envelope>`，被严格拒绝。前两个只是基础控制/失效目录过滤，不夸称完整语义冲突或embedding能力。撤回fixture使用SUPERSEDED而非源码不存在的KnowledgeRecord.REJECTED，并不代表真实来源撤回转移已测。

上述四例结束时的历史累计为226次/5156159已知tokens；加入后继受控恢复和新源码两例后，当前两轮后续累计261次本地物理调用、5430502已知tokens下限、1个早先未知，Flash0。所有合成Provider调用排除；不包含此前单独Flash256历史批次。

## 结果封套提示修复

实际任务包的output_contract给出`<result_envelope>{json}</result_envelope>`，存在把json当字段名的歧义；真实返回与此一致，但仅凭两例不能证明唯一因果。新增code profile v4的冻结completion_rules能力candidate-json-v1，在八类结果角色包中提供合法、扁平JSON示例，使用真实Task/Attempt ID，artifact/evidence路径来自当前Task.outputs。明确JSON文件与最终envelope不同、不要json包装/placeholder、按实际工作填summary/claims等字段，rule_check仍要求可核验claim。样例只是候选，不授予成功或verified状态。

v1–v3保留原文字，新v3历史常量完整canonical hash 53f88dc8102523114527b5e500d4ffc3d96cdcf39115ae95ae728150180329f7 与a30639e一致。新能力默认用于新code Mission；旧持久化profile/request不改。没有放宽解析器、预算、claim/evidence或产物验证；AppWorld/document/ARE输出合同不在本次范围。

定向组合90PASS/11.13秒，含真实Provider dispatch、8角色身份、JSON引号转义、缺文件拒绝、空claims拒绝以及旧profile/角色/Planner快照。旧a30639e源码下8个新契约反例JSON解析失败；此前测试夹具漏补系统赋予的result id导致10FAIL，以及正向夹具没提交claim导致1FAIL均保留，修复夹具后没有放松生产验收。Ruff/两源码mypy通过；最新完整回归、真实模型和原生结果见下方完成检查点。本次工程没有独立计时，测试时间不当总工时。

## 独立受控恢复（旧a30639e源码）

同分页场景保持3.6M/40调用/1200秒，显式给2次Attempt。在第一个实际模型envelope后注入一次坏JSON，保存原响应及变更前后hash、原实际用量；必须观察envelope_invalid、链接到首Attempt的第二Attempt和最终VERIFIED答案才算恢复通过。不能把注入错误计成自然模型失败率，也不能改写此前单Attempt自然失败。

该运行器先用合成Provider验证：14次合成调用/0网络、2Attempt及真实文件/验证/收尾PASS（3.712秒）。初始注入器直接dataclasses.replace(Message)把只读metadata带入构造而抛错，产生一个合成SDK未知；已显式复制metadata字典，停止并保留旧fixture，未影响真实DGX或累计用量。真实恢复最终COMPLETED/PASS：19调用134416tokens、232.946秒（受管236.627秒），未知0、19个SDK invocation均succeeded。实际第二Attempt请求包含首个envelope_invalid/invalid_json反馈，retry_of正确，最终产物hash与答案父审通过。重要限定：父审发现被替换的原始响应本身schema_invalid，故只能证明非法响应后的实际有界重试恢复，不能称为从有效响应人为破坏的因果实验；不改写自然单Attempt失败。

原N3 helper父审补齐LAN地址、完整连续分页/hash核验、失败退出、未知用量停止和累计/峰值输入区分；3正10负评分控制和3实际core阴性已通过，另外15次合成调用验证三组真实tool→VERIFIED正路径（4.373秒）。这些都不是模型成绩。

|证据相对Host .local-test-evidence/2026-09-15/gap-two-wave/|SHA-256|
|---|---|
|result-contract-combined-v2.log|97cf67c42ec8d6ed3e15556f8d70c7e9a7d59f9c4c4fe4f67c230d321551f7c8|
|result-contract-old-v2.log|c58e3b67344da6e24a1b92cef9b930150964bbfcbb82f876b9a287b658dd8de8|
|core-regression-v8-source-audit.json|7b272b325660cf117b8b205b5ee8276bd9ea8bec045fab5e6ebb9235cd7dab09|
|n3-hard-protocol-v1.json|46ce5fdd1e1e98765127111b6aa14c1dc5f79ea2d1e831661f4bcfb31cb5c7c5|
|n3-hard-protocol-v2.json|5faa0e36f916a3664fa56932ed77c1f012d71d87c7536a31d7d9c794c76a87fb|
|n3-temporal-protocol-v1.json|160b1e29629c4c87519098a347377bdd9484c3af4a3eee40de08eb7dea2dd1e5|
|n3-recovery-protocol-v1.json|68753f254d4a42502042795ab2829269d8891e1dc7865acfbfd3c12742030a67|

AppWorld v4父审：Host .local-test-evidence/2026-09-14/gap-two-wave/appworld-n2-local-v4/parent-audit.json，SHA256 1bd5943e53a5fb2db0a8078ec67a9e3e3fb82234699de5d9c4d62915351a5742。

## 2026-09-15 02:44 CST 新源码验收检查点

SDK生产3e2792f22df1e7d1ff858f11258f98411ec57067，默认code profile v4。冻结两个helper逐项确认与原失败的GOAL、原文、oracle、3.6M tokens/40调用/1200秒/1Attempt相同；无注入。

|当前部分|结果|实际模型/测试时间|
|---|---|---|
|分页尾部条件自然复验|COMPLETED/VERIFIED；10调用78473tokens，3目录页、完整原文2页及hash正确|149.604秒；受管155.483秒|
|时间/适用范围/例外自然复验|COMPLETED/VERIFIED；6调用61454tokens，3份原文均2页完整读取，法律保留例外拒绝删除正确|141.019秒；受管145.125秒|
|原非法响应后的受控错误反馈恢复|PASS但原始响应也非法，不作有效响应破坏的因果声明；19调用134416tokens|232.946秒|
|完整编排v9|2275PASS/32条件SKIP/1FAIL；613源码与测试hash不变；唯一失败是默认code profile旧version3断言|662.10秒；受管662.670秒|
|版本断言后继|code严格claim测试改为version4并显式断言scoped-observation-v2及candidate-json-v1；28PASS，旧profile hash兼容测试保留。无生产再改|2.33秒|
|ARE官方依赖后继|另一个旧code版本断言同步；实际官方环境50PASS。两次误用缺pytest环境失败保留；隔离环境补pytest9.0.3/pytest-asyncio1.3.0后执行|16.57秒|
|源码原生v54冷恢复|80个冷文件复制核对；实点旧Mission/代码产物/回放/支持报告，1Mission/1Task/42events/5调用/4effects不变；3artifact只重定位storage_uri，0新增调用|操作约02:35–02:40，与其他工作重叠，未独立计时|

两个新自然运行SDK物理调用均全部succeeded、rehandoff0、未知0。实际峰值输入分别11602/14979，配置256K不等于近窗测试。两例成功不构成成功率、耗时或token节省的统计结论。完整v9原1FAIL保留，后继为定向关闭，不拼接成虚构的一次全套全绿。源码UI使用Host固定f3ed734a（相关UI源与62c8ce10一致）、SDK snapshot-v5 3e279；当前保留运行及防熄屏。旧Mission仍使用其冻结profile，冷恢复不冒充新Mission UI执行。

|整体计划|当前完成/未完边界|已记录时间（不作总工时）|
|---|---|---|
|N1|进程内加权接纳和部分近窗/恢复已测；跨客户端总容量、长期稳定仍OPEN|加权大/小请求226.644/130.255秒；工程未分计|
|N2|可信API观察/陈旧拒绝已实现；官方复杂v4失败，真实有效复用/动态图收益OPEN|v4模型1557.359秒；工程未分计|
|N3|原文分页/困难日期条件及封套修复有新真实证据；完整检索/摘要及语义召回比较OPEN|原4例392.770秒，新2例290.623秒，恢复232.946秒|
|N4|通用协调器实现，正式A96/B96均0/96|正式批次未开始|
|N5|官方正常/攻击首对通过；传播扩展与冻结组OPEN|209.980/1046.198秒|
|N6|ARE自定义动态硬判通过；完整Gaia2/独立judge OPEN|动态275.301秒；本次官方测试16.57秒|
|N7|任务级分析/区间实现；真实消融和收益OPEN|相关定向测试有记录，无正式模型时间|
|N8|当前源码原生冷恢复通过；正式split/最终交付OPEN|本次UI未独立计时；无打包|

|仍需处理|剩余工程粗估|模型运行/外部门槛|
|---|---|---|
|N1跨客户端容量与稳定性|2–4小时，需入口核查后修订|本地256K；不直接改共享DGX服务|
|N2真实知识复用/动态图|3–6小时|先本地校准，失败保留|
|N3其余检索/摘要边界|1–3小时|只据实际缺陷选择优化|
|N4冻结和两轮96次|1–3小时|模型墙钟未知；B后置闲时且付费上限待答|
|N5传播与冻结覆盖|4–10小时|本地后统一Flash|
|N6 Gaia2完整协议|8–18小时|独立judge模型/账户待明确|
|N7实际消融分析|3–6小时|依赖正式成对数据|
|N8正式split/交付|3–6小时|完整批次规模未定；不打包|

上述工程粗估有重叠，不能简单相加；不是模型执行承诺。N1–N8没有因本轮局部PASS整体关闭。

|本次继续新增|完成内容|花费时间|
|---|---|---|
|生产修复|code profile v4具体合法结果示例、真实ID、保留旧版本|工程未独立计时|
|困难真实复验|两个原失败题相同参数自然复验均交付，另保留恢复证据限制|新2例290.623秒；恢复232.946秒|
|回归审计|完整2275PASS/1旧断言FAIL，定向28PASS及官方ARE50PASS|662.10＋2.33＋16.57秒，非总工程工时|
|原生冷恢复|最新SDK源UI实际操作/DB比对/本地支持报告|约5分钟交互跨度，非独占|
|记录与交接|原失败、实际费用下界、剩余门槛、ARCH同步|未独立计时|

新增证据均在Host本机ignored目录，Git仅保存下列索引与SHA。

|相对Host .local-test-evidence/2026-09-15/gap-two-wave/|SHA-256|
|---|---|
|n3-code4-pair-v1.json|4f0c6a8f0914606807b41ea1ba56295dc34518595bdd7ca0c957ffbc7b83928d|
|n3-hard-prep/local-code4-paged-v1/paged-tail-condition/parent-audit.json|89832ce91925c53902eaca7550a63966a3d932b39c583b31613e1dae8dea24c7|
|n3-hard-prep/local-code4-temporal-v1/temporal-scope-exception/parent-audit.json|a8f3bacfae1ba93aa122ed4babe589ea26bb408d6b0b6769eede73a06f4579d7|
|n3-hard-prep/local-recovery-v1/paged-tail-condition/parent-audit.json|3231cbbe69a156f4ca2762d594505f0c8ab23013056345ac775ec3acc0eed7fd|
|core-regression-v9-source-audit.json|d992aa012c1b55dc6fd99de98f2a8bee1bb0fc7213b8b983cbdf8a710eee9809|
|core-regression-v9/command.log|53db8db6baeec6995b2f1e6c7b852c05c33f2934b3a98d6077b205aecf505ac3|
|core-regression-v9/resource.json|d2c3957a052e05417a1ebd7b49f57e07fa9c370cb47099d9e80981c886e5f1c3|
|result-contract-final-targeted.log|e5f786b21afd84209b2e7bcb29eb5c57eb3289db74761ccd8676ef8ab70f933a|
|result-contract-are-final-v3.log|ece63f1d506ff7c4e0651d869de33e548d4717fea10ac18c043d868c5fe1972e|
|result-contract-ruff-final.log|82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18|
|native-wave-a-context-v54/parent-audit.json|c747972c94809db947d4c00d8207007fc66de289be83d83c881f31a7cca49182|
