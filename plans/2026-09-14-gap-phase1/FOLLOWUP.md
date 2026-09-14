# testPhase1 后续修复与验证

最后更新：2026-09-14 17:40 CST。155项定向测试和完整编排2096PASS/20SKIP/0FAIL通过，当前源码原生冷启动通过；本地真实四题16次复测进行中。

## 当前上下文测试范围（2026-09-14 18:33 CST）

按用户最新要求，后续上下文测试只使用以下档位，不再新增小窗口对照：

|上下文总窗口|主要测试模型|用途|
|---|---|---|
|128K（131072 tokens）|本地双DGX qwen38-flash-next|较低上下文档位；必要时另建完整DeepSeek Flash对照块|
|256K（262144 tokens）|本地双DGX qwen38-flash-next，默认|当前四题16次复测使用此档；必要时另建完整DeepSeek Flash对照块|
|512K（524288 tokens）|仅DeepSeek Flash|不得向本地双DGX模型发送512K测试请求|

上述是上下文总窗口，不代表每次请求的实际输入长度，也不是累计Mission token预算。报告分别记录总窗口、有效输入、单次输出预留与累计预算。当前16次冻结试验保持256K和原实验身份；既有小窗口或小输入证据仅作历史记录，不列为新的目标档位。单次输出配置单列，尚未根据本次上下文要求修改。新增档位没有执行证据前不标记为已通过。

## 修复范围

|问题|修复及边界|
|---|---|
|预算规划|AppWorld Mission显式绑定实际runtime_profile_id，复用已有256K Planner/Manager floor和图提交校验。带Critic单候选Task最低522240=261120+261120；全局1600000不增加，原旧Mission语义不变。下限仅保证首轮容量，不保证任意多轮完成。|
|R选择|appworld-r-self-selection-v1；允许解释前缀，唯一封闭JSON，版本/整数范围检查，选择结果文本最多16384字符（不是上下文上限）；拒绝重复字段、多对象、残缺JSON、bool和越界，无静默替选。|
|成功统计|valid_success要求驱动/finalization无异常、用量非fatal、runtime_success和official_success均为True。原始official_success单列；旧结果不回填。|
|网关异常|回调异常记录failed并向SDK传播；取消等待物理线程结算后记录unknown，保留锁与零重放语义，记录有限错误码且不写异常消息。|

SDK冻结快照 `b8245f361c2357911dfa5a68986987199b44fc37`，341个源码文件指纹 `5b5ca475cae8f76dc995a2ea8b191b76e799bac192cea5e3af98db74f4daa548`。

## 验证

- 同一预算回归在旧v46冻结SDK上出现2个预期失败（58192 != 522240），pytest0.57秒/runner0.893秒，0残留。只读旧源码、未回写原实验，证明当前修复针对真实预算绑定差异。

- 完整编排2096 PASS/20 SKIP/0 FAIL，pytest686.51秒，runner687.033秒，资源组49164已退出、0残留；132个源码文件mypy通过，ruff与diff检查通过。20 SKIP为显式真实Provider opt-in及可选tokenizer，不能称作通过。

- 定向155 PASS，pytest11.88秒。D/F实际Orchestrator拒绝250000计划，修订800000后完成Worker和Critic，没有创建失败计划的Attempt。
- 主代理预算测试夹具最初缺少完整计数器契约、无法识别AppWorld前置域提示、缺少必需Claim；失败原样保留，不计为生产回归。
- 复用原四道预选dev题，每题完整S/R/D/F各一次，共16次，原失败不覆盖。最初单题配置被协调器在调用前拒绝，启动失败保留，0模型调用；沿用四题校验，不放宽既定协议。
- 每臂与原试验相同：1600000输入/总token、131072输出、60调用、1200秒；本地qwen38-flash-next，总窗262144/输入228352/输出预留32768/安全1024；实际输出8192起、最高32768，物理1。DeepSeek0调用。
- 未修改Host UI业务流程；此前原生v45/v46各保留源码范围，本轮AppWorld验证不作为UI验收。不打包、不扩大96次，不修改远程模型服务容量。

## 当前源码原生冷恢复

`native-gap-v47`实际UI点击恢复已有COMPLETED任务，打开VERIFIED的REPORT.md并读取原观察ID/工作区hash及范围声明。SDK与真实试验同一冻结提交b8245f3，Host a68f70bc。12张持久表精确相等，5个产物仅storage_uri由v46重定位到v47且内容哈希一致，15条模型调用记录精确相等、新增0调用；runner228.444秒，正常退出、0残留。证据位于Host `.local-test-evidence/2026-09-14/dgx-local-connect/native-gap-v47/`；acceptance.json SHA-256 `3a5559aed919ec3b382ac659c63cabd837e6ed89f69e5efbf088b275ea33476e`。这证明当前源码冷读兼容，不是新Mission执行验收。

## 子代理效果

|任务|实际模型/推理|耗时秒|未缓存输入|缓存输入|输出|主审结果|
|---|---|---:|---:|---:|---:|---|
|R protocol|GPT-5.6 Terra / medium|143.26|57824|594176|6498|主审补长度/重复键边界，并修一处lint|
|gateway outcome|GPT-5.6 Sol / medium|191.25|83415|1198592|7562|异常/取消语义与测试接受|

两个新子代理保持指定模型；并行耗时不能相加为总墙钟。推理tokens包含在输出中。主代理协调/返工未独立计时，计入总墙钟，不估造节省比例。

## 证据索引

本机ignored目录：`simple_harness/.local-test-evidence/2026-09-14/gap-phase1-followup/`。包含start.json、pilot-source.json、focused-v1/、full-orchestrator/、pilot-local-followup-v2/、pilot-local-followup-v2-process/、usage-latest.json。结束时补充时长、状态和SHA-256。

## 真实复测中间检查（17:51 CST，非最终结论）

第一题S与R有效成功；D已按522240/522240/555520分配Task并启动Worker，但17次模型调用、230835总token后，Task1下一请求额度仍不足，保持budget_exhausted停止。原先250000小于261120、零Worker启动的问题已被配置绑定和硬准入修复；后续多轮开销与固定Task分配仍可能失败，不据此宣称预算规划效果全面完成。保持当前矩阵与费用，不借用未来Task额度、不缩小256K或Critic保护，也不混入付费模型替换失败臂。

## 预算和本地思考耗时复核（2026-09-14 18:39 CST）

已完成前8次的标量账本检查：网关202条均有终态，SDK工具重下发0；四个已结束D/F的Mission预留均为0。当前未触发真实网关回调异常，因此不能把0缺失解释为真实异常路径已再次覆盖。

独立Sol只读审查未发现重复Worker预留。图下限只覆盖首Worker请求和首Critic的预留必要条件；固定Task额度耗尽会直接终止Task，F此时不会进入Manager重分配。原先不足首轮容量的问题已修复，多轮工作量可行性仍需校准。已安排矩阵完整结算后执行一条独立D技术校准：累计输入/总tokens160万→400万，其余模型、256K窗口、物理1、60调用、1200秒及输出设置不变；不计入四臂对照成绩，也不提高产品默认额度。

本地与服务器chat_template.jinja的SHA-256相同，模板默认reasoning_effort=xhigh，实际vLLM启动参数没有显式思考档位覆盖。第二题F首Planner调用耗342.98秒，usage中8192输出tokens全部属于reasoning，返回provider_empty_response；下一请求成功，耗220.53秒，输出5263/其中reasoning4288。证据说明本地思考会明显影响耗时；本轮未修改服务设置，主会话GPT-6 Astra/high不变。这是单次生成诊断，不是新增小上下文测试。

|新增子代理任务|实际模型/推理|耗时秒|未缓存输入|缓存输入|输出|接受结果|
|---|---|---:|---:|---:|---:|---|
|Task预算准入只读审查|GPT-5.6 Sol / medium|125.6|80717|857600|5110|结论接受；无代码修改、无额外模型测试调用|

局部证据（Host ignored followup目录）：

|相对路径|SHA-256|
|---|---|
|`reasoning-deployment-check.json`|`2b51f50a14f5518bc5bf550b3ba325539b0bac494278aec1eebf4c0e873c2695`|
|`reasoning-call-check.json`|`18a8f18ea06db643eede85b59599bd1d2c2435f980321f4e5cb4bba686de22e5`|

### 上下文与累计预算的关系（公式推导，非新增真实测试）

假定三个Task各自都包含单候选和critic_review，安全余量为1024，输出预留等于输出最高额度时，首次准入必要下限如下。只含format_check/rule_check的准备Task不适用双份下限。该下限不是推荐预算，也不保证多轮完成；每次实际输入通常远小于窗口，后续多轮仍会累计消耗。

|总窗口|三个Task首次准入必要下限|160万累计额度减去下限|
|---|---:|---:|
|128K|780288|819712|
|256K|1566720|33280|
|512K|3139584|-1539584|

因此前两题那种三个Task均带Critic、256K下把160万近乎均分为三份的计划，会留下很少的额外规划空间；512K仍仅限DeepSeek Flash，并且不能照搬同一累计预算假定三Task可准入。当前没有因这张推导表修改运行中试验或产品默认值。

## 本地后继矩阵终态（2026-09-14 18:56 CST）

STOPPED_UNKNOWN_USAGE：计划16次，实际9次终态、7次未执行，有效成功3次。S2/2、R1/2、D0/3、F0/2只描述各臂已执行子集，不据此发布完整四臂通过率或优劣结论。183次物理调用，已知2296521tokens，另1次用量未知，不能称实际总tokens完整。runner5207.513秒（约86.8分钟），进程组51160正常退出、0残留。

第三题D达到1200秒时限，取消请求用量未知，协调器停止接纳剩余episodes。该Mission持久账本保留257731tokens预留、17290已结算；独立Provider计量器已知245525tokens。两层账本阶段不同，不能把未完成的Attempt预留静默清零或称作重复扣账。前四个预算失败D/F终态预留为0，第三题取消案例不能沿用该结论。网关终态缺失0、SDK工具重下发0。

原计划的本地400万单条校准没有启动（完整矩阵与已知用量前置条件不满足）。按用户预先授权，在本地时限不足后转用独立DeepSeek Flash256K探针，先使用同样160万累计额度；必要时才做更高额度校准。新结果独立标识，不恢复或覆盖停止的本地实验。主会话Astra/high与本地默认256K设置不变。

本机摘要 `gap-phase1-followup/local-stopped-summary.json` SHA-256 `35f119869596268c6b49f898a7c71591c6a695febff034db1f4f5a080faf7ed2`；其中索引原始experiment、public-summary、mechanism-summary与resource的hash。

## AppWorld结果契约修复（2026-09-14 19:11 CST）

独立Flash160万/60调用探针结束：第三题D前三个Task完成，最终Task失败；60次调用、640557tokens、其中缓存输入499072，runtime190.624秒/runner210.874秒、0未知用量、0进程残留。停止前存在3次envelope_invalid（提示词要求schema_version而严格ResultEnvelope拒绝）、1次SDK工具协议失败，以及之后的experiment_budget_exhausted；Mission最终显示max_attempts_reached。不能把这组失败完全归因于预算或把调用上限提高视为根因修复。

已定位并修正生产模板冲突：新AppWorld domain profile默认version2，八个输出ResultEnvelope的角色使用对应appworld-v2模板，去掉非法schema_version；Planner/Manager/Critic模板保持原版本。v1 profile与全部旧模板仍注册，旧冻结Mission不改语义；解析器仍严格拒绝未知字段。

修复前：直接取八个角色给模型的JSON示例送入真实严格ResultEnvelope解析器，8个案例全部复现非法字段失败（0.09秒）。修复后：新示例严格解析、旧版本恢复，以及D/F实际执行驱动使用发布示例而非另写的成功夹具，gap_phase1整组143PASS/8.92秒，runner9.453秒、0残留；ruff/mypy/diff检查通过。完整编排回归和新冻结源码真实复测进行中，不提前宣称最终验收。暂不增加token或调用上限。
