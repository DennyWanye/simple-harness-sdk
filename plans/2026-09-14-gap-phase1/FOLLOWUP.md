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
