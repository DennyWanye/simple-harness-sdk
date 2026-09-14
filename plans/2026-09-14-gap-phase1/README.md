# testPhase1 执行记录

**当前接续（2026-09-14 19:32 CST）：** SDK功能源码5406fb5修正AppWorld Worker结果契约，新domain profile默认v2、旧v1冻结保留；完整编排2105PASS/20SKIP及原生v48冷读通过。原本地后继因1200秒取消/1未知用量停于9终态、7未执行，独立Flash校准已出现四Task＋官方成功；完整Flash16次后继正在运行。全部采用256K，窗口与累计预算分开；512K只限Flash。见[当前源码、结果与运行边界](FOLLOWUP.md)。下文16:47检查点与旧代码表均属于历史实验，不代表当前源码仍未修复。

最后更新：2026-09-14 16:47 CST。用户授权的T0–T6测试执行已结束，发现的后续修复单独列出；内部编排，未使用plan-test skills。墙钟约280分钟，含并行验证与模型等待，不等于各项工程时间之和。最终结论见[RESULTS.md](RESULTS.md)。后续带时间章节保留历史检查点，不作为最终状态。

|任务|当前状态|主要测试模型|实际测试耗时 / 剩余门槛|
|---|---|---|---|
|T0 冻结与证据|完成；源码/依赖/模型/预算/四题与失败记录均保留|无LLM|正式矩阵SDK2f8eacfc；功能SDK84b0a2e1/Host e0aed2aa快照；工程未分计|
|T1 K01–K03|功能验收完成；自由主张不自动VERIFIED，系统观察保留范围|确定性＋本地|关联196PASS34.79秒，最终全编排2079PASS/20SKIP/0FAIL879.28秒；检查范围重叠|
|T2 K04–K06|原文消费、Host默认工具门、原生复核/冷恢复完成|本地qwen38-flash-next|原文5调用113.63秒；通过的UI791.213+160.791+500.158秒，先前失败955.09秒另保留|
|T3 AppWorld|适配/隔离评分完成；5校准与16正式执行结束|本地|5校准1官方成功4失败，校准不并入正式指标|
|T4 S/R/D/F|四臂已实际运行并计量；暴露预算与R自选协议待修项|受控＋本地|S有效3/4，R有效2/4（官方终态4/4，另2次JSON解析失败），D/F各0/4|
|T5 16 episodes|16/16已回收；不等于16项成功|本地，同一完整实验块|282模型调用3076186tokens；矩阵5758.78秒，外部runner5784.209秒|
|T6 有限负载|有限恢复/预算/重复下发检查完成；两路传输PASS，格式FAIL保留|受控＋本地|两路65K输入51.77秒；所有D/F终态预留0，SDK工具重下发0；不宣称两路近256K|


默认被测模型保持双DGX `qwen38-flash-next`、262144总窗口、228352有效输入、32768输出预留、1024安全余量。物理槽默认1。当前未调用付费DeepSeek。96次扩展、AgentDojo、Gaia2、打包发布仍在范围外。

## 当前实现与已验证边界

T1：新Mission默认 `code-v1` profile version2及独立role模板；真实同Attempt/Result/artifact哈希/工作区快照/pytest目标的执行观察才产生范围化system知识。任意自然语言主张不会因pytest通过晋级VERIFIED。悬空引用、跨Mission/Attempt、旧hash、未记录执行与缺receipt均不能借用验证。旧冻结profile v1重放不改历史语义。17个聚焦测试包括合法范围正控；历史step04测试显式固定v1，不冒充新语义覆盖。

T2：`retrieval-v3-evidence-relevance`优先完整ID/完整路径，零词面关联不凑召回数；同义/跨语言通过消费Agent的 `knowledge_list/knowledge_read` 按当前Mission读取原文。目录有稳定hash，原文每页2048字符及hash续读；撤回/替代不作为当前知识。真实夹具的关键条件位于2000字符后，本地模型实际查目录、读两页中文原文，英文回答保留断网恢复、双人书面同意、满90天，并引用K-retention-42和来源路径。此为受控知识→真实消费链路，不是来源Claim晋级或原生UI证据。

T3：AppWorld0.1.3.post1使用独立环境服务及独立评分进程，Agent只见公开instruction/execute。第三领域通过显式handler分派，不落入document分支。官方 `load_state` 关闭模拟时钟后未恢复，触发后续close的freezegun异常；兼容服务仅在精确包版本恢复task clock。真实complete_task改变supervisor状态、保存、还原原始状态及候选状态均核对；单纯宣布完成仍被外部评分判失败，未泄露隐藏答案。

首个技术探针因缺Jinja2在调用前失败；补齐hf-tokenizer extra后，第二个技术探针24次调用/251285tokens在任务剩余额度21782、下一请求需27447时被预算拒绝。其原始退出/评分停滞记录保留；评分移至独立进程后可结束。上述不计正式16次预演。校准预算初设每episode800000总tokens、60调用、1200秒、物理1，输入/输出分别另设上限；校准后再冻结正式预算。

T4：S是真正一个BaseAgent多步工具执行；R是同一BaseAgent两候选再自选，宿主保存/恢复world且不透露评分，选择回合禁止新world操作；D/F使用真正Orchestrator的Planner/Worker/Critic，分别固定初始DAG/允许动态管理。所有角色共享一个Provider计量器，失败、cache、reasoning及未知用量分开记录。S/R受控实际运行及D/F真实Orchestrator执行已经发生；正式外部效果与预算分配影响在T5逐题报告。

## 子代理记录

省token策略作用于Codex开发代理，不改变被测simple_harness算法。主会话保持GPT-6 Astra/high。最多3个独立范围子代理，无全历史继承、父代理统一测试。首批Terra/medium与Sol/high恢复任务后，实际turn_context显示Astra/high，不能按原模型宣称成本；不再用恢复方式继续低价模型任务。新计量器委派明确Sol/high、仅两个新文件，已关闭；父测通过后另补合法失败用量及cache记录。原始input/cache/output/推理子集、运行跨度、接受/返工在dispatch及usage-latest记录；跨度包括等待，不与其他代理相加，不声称固定节省比例。

## 证据与来源

原始证据只在Host `.local-test-evidence/2026-09-14/gap-phase1/`，不提交Git。基线Host34ea4aad8035cc56b7654cd04f37341a57813a97，SDKecfee4d506f7c81c6ae10d5e755d0ba10a7c4497。旧runner只hash tracked diff，不能覆盖新untracked实现；正式冻结必须补充完整源码manifest。新live-arm已保存完整SDK/src文件hash。

|本地相对证据|SHA-256|
|---|---|
|`t0-sdk-baseline-v1.json`|`c68ffd1447bd304b52d4098eb569e549767cb73d2c1366366da1c2e560355889`|
|`t1-t3-t4-v1.json`|`663a50408c60e81e85265b88e70d3d4701642ef3de3db493d4db36bfcb5b3a36`|
|`t2-tools-v1.json`|`860d99821a04251ddc6f7322a7eebc0985fa0c7f27dce0d6595ae04dea299974`|
|`t2-config-v1.json`|`f4537c221cd421b5430284eee893838181da695367aa703313c3010cc009bdcb`|
|`t2-live-v1.json`|`43ae98e5746c4fba54339992b141684afbb49adf8c265af17a34ec60db6913a8`|
|`knowledge-live-v1/fixture.json`|`aff952572293eb60a23b97c97317e78eabf9fb614bc33dc657d7c7a72ceefc4a`|
|`knowledge-live-v1/gateway.json`|`5a97d61270dd8dc5290185977ed2004285924a8e1377415277a16e7b0bc3bb40`|
|`knowledge-live-v1/summary.json`|`b66526d9a319ac0b4bab4a23a733222c8da0921ac083fca5ece3b34c9de8f683`|
|`appworld-orchestrated-v2/runtime-summary.json`|`ed540e1996778a65615eef6c6916021482a5a8651e502c1867607e794fb7b512`|
|`appworld-orchestrated-v2/score-isolated.json`|`cdf413bcdb5d616f37a8222ef82104e2aa0009da9506eef3ead7118c34408a50`|
|`t3-lifecycle-v5.json`|`9af6b4f234e9758409722ecf38da7f3a2e5bb5f4fe5c13aaaea0a8270727653d`|
|`lifecycle-state-v5.json`|`987d245d385e867add7f5b343b389fc62525ee577dafc06f914ed8692451cc19`|
|`t4-meter-v2.json`|`ae71a4b04c7fcdd3990b12da956e8ad36b284df2b8a02943b58ca336a8187b3a`|
|`appworld-samples.json`|`2ef334ed11056e1ac9a01e9a0d14f554a09be6ca828ead437073444a1717bd39`|


## 14:09 CST 用户进度快照

T0–T6仍在执行，非整体完成。T1/T2核心定向验证通过，最新原生及冷读仍待。T3五题校准已结束3题（S预算/框架失败、R外部成功、D传输超时），F进行中，S第五题未开始。T4实际S/R控制通过，D/F与新完整矩阵coordinator待验；T5正式0/16。T6已完成限定取消/恢复控制及两路本地中等输入传输，严格格式因前导空行失败保留。

|新增可核查结果|实际时间|
|---|---:|
|R2，同一Agent两候选自选，官方PASS；28调用523655tokens|503.07秒|
|S1，29调用774992tokens，预算后等待错误，官方FAIL；含父代理取消等待|1147.72秒|
|D3，首请求180秒超时，1次用量未知，停止无效等待，官方FAIL|478.87秒|
|跨分支与首次请求定向27PASS|23.15秒|
|范围化写入恢复7控＋原挂起用例共8PASS|3.11秒|
|预算/取消等组合106PASS|14.06秒|
|两路本地传输2calls/130115tokens；内容匹配但严格格式FAIL|51.77秒|
|首轮全套1773PASS/33FAIL/16SKIP后挂起中断；后续定向修正|915.87秒|

第二轮完整SDK回归14:08观察到86%后仍运行，不提前宣称全绿。新版startup反例已写；Sol复审指出SDK自动恢复先于Host绑定/审计安装，待修复验证。新coordinator已交付但未测。F4校准600秒HTTP等待/1200总deadline；与旧180秒校准分开记录。只读DGX日志显示D超时时约24生成tokens/秒，之后无在途/排队；不能判模型能力不够。付费DeepSeek0次。7个开发子代理分批运行，全部关闭；2个历史恢复代理实际升为Astra，已披露并停止用恢复维持低价模型。后续新建显式模型，无全历史继承；不能声称固定节省比例。

剩余粗估：T1/T2最新原生＋冷读0.5–1.5小时；校准/接线0.5–1小时；T4集成0.5–1小时；T5按已结束校准约8–19分钟/episode预留2–5小时；T6新启动修复及回归0.5–1.5小时；文档/提交0.25–0.5小时。存在重叠，整体暂估4–8小时墙钟；运行失败会更新，不是30–60工程小时初估的实际耗时。

## 2026-09-14 14:33 CST 检查点

完整第二轮2051PASS/7FAIL/20SKIP，760.36秒，7旧断言已定向修正；后续44PASS/1父代理修正断言FAIL71.01秒，最终新模块与policy合计126PASS11.33秒。冷启动工具未绑定反例明确tool_not_bound失败，提前绑定/审计后31PASS12.83秒；冷恢复28PASS34.63秒。

五题校准完成：S5正式Agent committed但官方FAIL，27calls218246tokens268.62秒；F4任务budget_exhausted，18calls160366tokens685.06秒。F任务预算420000，其中首次Critic按256K保护261120tokens，Worker剩余约159K；这是算法预留开销，不伪报全局80万耗尽。正式16次全部四臂统一增加到160万总tokens、输入160万/输出131072/60calls/1200秒、每请求600秒、物理1、相同256K窗口；不改某个臂模型。

独立源码快照SDK2f8eacfc6255e110e1ee2dafd4d4dac180fc42c9（基于工作树、未推送）隔离了后续修改；349个SDK身份输入，全部221个载入模块来源正确。原生任务mission-5921e84c7d810e22已从UI提交，正在执行，不能提前PASS。原生输入曾出现剪贴板延迟和焦点延迟，提交前已逐字段修正并截图核对，不算模型错误。Host源码测试进行中。

外部评分增加从保存的supervisor状态只读判断declared_task_status，准确计算false_completion；真实R2声明成功且评分成功，原lifecycle声明成功却评分失败，两个隔离复读共2.54秒，未新增LLM或改原始任务world。

`gap-full-orchestrator-v3.json` SHA256 `3c2fc8c29be6b2a52eee1cb42bb681e8963f6e729f2b0ea03bf17c909068c457`。

`gap-startup-witness-v3.json` SHA256 `d673b6b1d5de052f1b39591c7d38bd0484eae00dd1e82a24c3aa20f6d07a3564`。

`gap-startup-fix-v2.json` SHA256 `07dc8b67e6d4ae51dbe5fb03209c2b731b0cc0ead200512517041e3717d20a08`。

`gap-cold-related-v1.json` SHA256 `aef54f793fe187bf9cb4eefd922b19593fbf38e9672257b1a946cc32c9e1ca0c`。

`gap-late-regression-v1.json` SHA256 `8cfb876d559cc51021712a13cfaf093b4303fa029538a4b30340b27940b35e5d`。

`gap-final-focused-v2.json` SHA256 `5ac8f968cb83384a4f13974172da4a8c2a98ac70f7788a873d5ebd49bb8d0b16`。

`calibration-f4-v1/result.json` SHA256 `6e2d4b47402360c6e4a5f95db29e83cc8fc0b24c545a540d2b06caa50fbc3db3`。

`calibration-s5-v1/result.json` SHA256 `69c36e39883c16e9d086f5faaedcf0cd404a2bf73fec279c22c1bcdd32ce7a7e`。

`pilot-source.json` SHA256 `e2c165a56df7502b8a9c37e399cf3ce0aa8bccb4847464bcc8ee672038386cba`。

## 2026-09-14 14:51 CST Host与原生检查点

v44原生任务A真实通过code_test并产生system test_observation；任务B所需knowledge_list/read在Host默认工具门中遗漏，无法在旧Mission冻结权限中补入。实际UI取消并等待结算60768tokens/10成功调用/0预留，955.09秒正常退出、无残留；不能将其算完整消费验收。

Host部署默认工具门现加入两个只读knowledge工具；新增Host入口测试覆盖真实gateway目录与不存在ID拒绝。旧跨分支夹具改用实际范围化test_observation，保留真实pytest和图操作。并发夹具改为按Attempt分配步骤，修正package嵌套字段；原失败记录仍在。四个相关测试文件共9PASS/23.88秒pytest、24.39秒runner。

Host全回归先前316PASS/6FAIL/5SKIP/5DESELECT，397.90秒；其中4个功能/夹具问题已由上述定向检查关闭，2个要求installed wheel RECORD的资源检查不适用于editable-source，本次不打包、不改写该oracle。此为全套失败后定向关闭，不伪称当前全套单次全绿。

v45隔离源码Host d598c22381dbc7333da101e6ce83d04661d5e9cc、SDK8524893b349eadcb56eaf866904cb081e250c1f3（SDK生产内容与pilot冻结2f8eacfc相同，提交元数据不同）。从UI新建mission-4d17ed084949406c，256K窗口/160万总预算/12次上限；正在执行，不提前PASS。

`gap-host-reader-v3.json` SHA256 `7dd96d84dca6000ea30dbd4b05648a7b668718ca87fa31310e0a304c4c1c2829`。

`gap-host-reader-v4.json` SHA256 `830e1f0e846fd39dcaa8bc501dad9f78ac2d06b5d43d990c80a212e9557f756c`。

`gap-host-source-v1.json` SHA256 `176b47016696f5345515101d4d85aa7eeda1adbd2dd7ac4fc140f135c1f69867`。

## 2026-09-14 15:05 CST 原生闭环通过，正式矩阵启动

v45同源码真实UI创建两依赖任务、打开代码/测试/报告，任务A与B均独立pytest18PASS。B实际通过knowledge_list/knowledge_read读取A的system test_observation原文；下游Result.used_knowledge准确引用。报告保留测试目标、A workspace哈希、来源Attempt/Result与限界，不将测试外推为一般备份安全。Critic PASS；人工复核理由明确仅接受当前测试和准确引用，不接受未经证据支持的环境/未来版本描述。7个模型文字Claim仍SUPPORTED，仅2条system pytest观察VERIFIED。

15成功Provider调用/90994tokens（input78401/output12593，cache字段未提供，不写0），预留0。人工复核前冷启动后13张选定持久表及15Provider行哈希逐项一致，零重调；真实UI复核通过后两Task和MissionCOMPLETED，三份最终产物VERIFIED，报告哈希保持。源码UI生命周期791.213秒，冷启动/复核160.791秒，均正常退出且无残留；两者包括读取/等待，非纯模型耗时。首次冷启动漏传--resume，被启动器拒绝且无运行残留，0.973秒失败记录保留。

T0–E0对应源码/依赖/服务/样本/配置冻结；T1/T2–E1/E2为知识语义与当前消费门槛；T3–E3为第三域与外部评分；T4/T5–E4为四臂小样本；T6–E5为有界负载恢复。原文所引独立D2/E0–E5全文未在定向文件检索中定位，本映射仅覆盖用户已提供报告中的范围，不臆造未提供细项。旧46项Phase3证据保留原源码scope，相关变更另行回归，不把旧数量当当前整套通过数。

正式pilot-local-v1按预选4题×S/R/D/F×1=16启动，拉丁方轮换顺序。SDK生产源码仍冻结2f8eacfc（与v45 SDK内容相同），模型qwen38-flash-next、总窗口262144、物理1、160万总token/60调用/1200秒每episode/600秒HTTP。六小时批次外部保护、3GiB所属进程组RSS保护；未知物理用量停止接纳后续episode，失败不选择性重跑。SDK最终冻结全回归正在执行；尚未宣称T4/T5或整体通过。

本地 `../dgx-local-connect/native-gap-v45/pending-before-cold.json` SHA256 `31df04d292f63cfbdb8083bca155e7c97f8a43deb98cb4ddd8810e9b98ccfc29`。

本地 `../dgx-local-connect/native-gap-v45/pending-after-cold.json` SHA256 `1220be6885a232cb1358891913142d89544e9b4e3f6f4da6ae20a6d023a9675a`。

本地 `../dgx-local-connect/native-gap-v45/completed-after-approval.json` SHA256 `453221dcb7fe861a8f82c2233ea37cb5e0371b530a40d6f87afc2c35f5ebe521`。

本地 `../dgx-local-connect/native-gap-v45/18-delivered-report.png` SHA256 `275c57880948ffbcc34388b061ab80efdbbca6ffb2da267bf35a07d5683b679b`。

## 2026-09-14 15:33 CST 最终功能源码与失败关闭

SDK全套v5原始结果2060PASS/18FAIL/20SKIP，972.86秒。18项失败分为6项Host测试venv缺tiktoken、10项损坏的AGENT_CREATED Critic意图在SDK启动前过早抛错、1项冷恢复10秒预热超时、1项旧300ms停滞夹具误杀健康pytest重试。原始失败不删除。独立SDK测试venv补齐原有dev依赖，不修改生产包依赖来掩盖测试环境。

启动修复仅对尚无持久SDK turn的AGENT_CREATED意图保留原恢复失败语义；存在SDK turn时校验agent/input/turn身份并在自动恢复前绑定工具和reader，SUBMITTED仍验证冻结意图。非法已提交意图仍拒绝启动，不宣称已实现所有Mission隔离恢复。新增真实after-submit/pre-receipt Critic崩溃见证；Critic只读verify调用不写Worker workview计费证据，父代理修正该测试oracle。健康重试夹具阈值从300ms改为2秒，仍保留5秒故意停滞、TIMED_OUT和重试完成判据，生产默认未改。

最终冻结SDK84b0a2e115bbd4d279a3d421bf0d0df21f68e57f，Host e0aed2aad0ef41aea44e321a13a24f0aae3681de。v46独立SDK dev环境执行gap_phase1、p33大读取/分页/judge恢复、step02重试/恢复矩阵、p35冷恢复，196PASS，pytest34.48秒/runner34.79秒；覆盖上述18失败类别，不能表述为单次完整全套全绿。ruff与132源文件mypy通过。Host9项受影响测试通过；两项wheel RECORD检查保留本次source-only不适用边界。

v46在新目录复制v45已关闭数据，通过真实UI重新打开COMPLETED Mission与原VERIFIED报告；12业务表和15Provider行相同，5个artifact记录仅storage_uri映射新目录，其他字段及实际文件SHA256逐个相同；零新增调用。初始“13表完全相同”假设因预期路径迁移不成立，递归差异已记录，不能冒充同目录重启；同目录冷恢复证据来自v45。v46正常CmdQ退出500.158秒，峰值1110224KiB，无残留。

正式矩阵仍冻结旧2f8eacfc生产源码；v46变更只涉及冷恢复，矩阵每episode使用新库且不注入崩溃。本轮不把运行中的实验偷偷升级为新源码，也不将后续结果伪标v46。

当前正式首题37a8675_3：S运行自报成功但官方FAIL/false_completion=true，28calls327244tokens；R官方PASS，29calls501944tokens；D官方FAIL/budget_exhausted，12calls122585tokens；F官方FAIL/budget_exhausted，11calls120163tokens。D首任务40万预算保护首次Critic261120 tokens后可用Worker输入空间不足，剩余13840小于下一请求上界36193；全局160万并未耗尽。F同属任务级分配与预留开销。所有失败保留，正式四题未结束前不推断算法优劣、不启动96次扩展，不以换模型掩盖此问题。

本地 `gap-full-orchestrator-v5.json` SHA256 `eea3e28a52ed48e10dab7c88ef2f331b5ed16f09e1fe651590437edf34687742`。

本地 `gap-final-scope-v46.json` SHA256 `92b70a780536d419856131496ddd3f5ff79f30466706e9bb0fadac9a19c25872`。

本地 `gap-startup-critic-gap-v4.json` SHA256 `412575411eeb52af3cb52de415bd28ce9774a599b2c741ceafd1dd4fde3fca7c`。

本地 `usage-latest.json` SHA256 `fa85e5d19f9c84f0fd95c49b48fdac524e84e1964c49cd8a5fe0ac96e6d24241`。

本地 `../dgx-local-connect/native-gap-v46/acceptance.json` SHA256 `e8b3cb78b8f545082d5c7e685f60182a1fe2f2cc429cd006ddf8431f538eecca`。

## 2026-09-14 15:57 CST 完整最终回归通过

冻结SDK84b0a2e1、完整SDK dev环境在启动修复后执行全部 `tests/orchestrator -q -o faulthandler_timeout=60`：2079PASS/20SKIP/0FAIL，pytest879.28秒、外部runner880.047秒，所属进程组20990，峰值231664KiB，正常退出无残留。20项是已有显式真实Provider opt-in及未注入的可选DeepSeek tokenizer门槛，不能算通过；真实本地调用与原生验收由独立记录覆盖。日志中60秒faulthandler栈是耗时用例诊断，最终均返回通过，并非删除失败。代码未再修改；本次全套结果取代此前“只做18失败类别定向关闭”的当前验证状态，原失败历史保留。

源码已推送：SDK功能84c32358fb4fb36c765ed92b7212b6423ed06a35、交接说明3c88df4；Host23cb7d374bda190e629268d250af3b03a39b49b4。提交前60个SDK及6个Host代码/测试/依赖文件与最终快照逐字节一致。不会把仍在运行的pilot源码2f8eacfc改标为后续提交。

正式矩阵8/16结束：第一题R成功、S错误自报完成、D/F任务级预算失败；第二题S/R成功、D/F任务级预算失败。四个D/F运行尚未产生KnowledgeRecord、选择知识引用或GraphChange，不能宣称已测得知识复用或动态图收益。第二题R有1次完全相同的工具请求；可能是合法查询或候选重置后重查，仅统计重复，不自动判为无关重做。计量未出现未知物理用量，未新增付费DeepSeek。后两题仍执行，不能提前得出最终优劣。

本地 `gap-full-orchestrator-v46/resource.json` SHA256 `58deabd15d0d19564fb822f2ee35dccd351a7d5df07e80d38a84e517d0cfcdbc`。

本地 `gap-full-orchestrator-v46/source-identity.json` SHA256 `a1099431b665faf1c7d6e8d163b439127a406312345eba3f07911556ebd97a0e`。


## 2026-09-14 16:47 CST 最终收尾

正式矩阵完整结束，外部runner退出0、无残留，AppWorld环境服务73152已正常结束，18244端口关闭；防熄屏保持用户设置。正式16次282模型调用/3076186tokens，全部物理用量已知，峰值1槽；所有失败计入。

必须区分官方终态与算法闭环：S官方3/4且有效3/4；R官方终态4/4，但第三、四题自选输出带解释文字，json.loads抛JSONDecodeError，尚未恢复选定候选，因此有效R仅2/4。之前检查点中R“成功”若只看官方标签，不代表完整自选协议通过。原结果未重解析晋级、未重跑。D/F各0/4，全部任务级预算停止；没有实测到知识复用或动态图收益。第三题D首任务250000 tokens低于首次Critic261120预留，Worker尚未创建；其余有执行中准入失败。下一轮应先修预算规划和R自选协议，再决定扩样。

网关工具记录322条，SDK持久工具请求323条（321成功/1失败/1拒绝；其中1次在SDK拒绝、未进入网关），重下发0。第三题S的工具异常缺网关outcome，但SDK有tool_handler_failed持久结算；分析初次因缺字段报错，后从SDK对账补齐统计。未改原始记录。R异常未返回arm_result工具总数，最终表采用实际gateway计数，不将缺失计为0。自由主张、外部任务评分、协议完成是不同判据。

当前仍有3项后续工作：Planner预算可行性修复（估2–4工程小时）、R自选协议兼容/明确拒绝与新匹配验证（估1–2工程小时，模型运行另计）、AppWorld异常网关终态补齐（估0.5–1小时）。本轮测试执行完成不意味着这3项已实现。96次扩展、AgentDojo、Gaia2、打包仍后置。全部已授权开发代码和本报告同步main；原始证据仍仅本机。

本地 `pilot-local-v1-process/resource.json` SHA256 `e96f5c40ad50a43e400a9b20e79a900084930b3f7a80e6a76f8976a97701bfde`。

本地 `final-cleanup.json` SHA256 `56aeffe7e0072e14be4adae77a0a19d8ae97e49f96dbc025471f9b83dcb676c1`。
