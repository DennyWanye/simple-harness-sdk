# N1 同机共享模型容量接纳

最后更新：2026-09-15 05:40 CST。实现与定向测试已完成；新完整回归、真实Qwen多进程及源码UI联调待验。N1–N8整体仍OPEN，Flash0。

同一个规范化local profile文件的相邻capacity-v1.sqlite3为共同账本，物理endpoint作为池身份（模型别名不另开容量），上限2槽/393216在途tokens。配置冲突拒绝，重复同配置绑定幂等，冲突嵌套绑定在出站前拒绝。主对话保守预留整个262144服务窗口，编排/实验使用已绑定tokenizer的最终wire输入加输出上限。两个执行器内部2槽不再等于两份独立总容量。

SDK ProviderInvocationCoordinator在记录handoff之前等待共享容量；BaseAgent恢复tool_calls之后计数。CapacityProvider和MeteredProvider的同task/同请求嵌套共用一次grant，评测等待取消计为0物理调用，既有Task/Mission预算身份及冻结旧请求不改。排队被标为非计费slot wait，保留原有stall与取消纪律。

SQLite FIFO/短事务保存WAITING、RESERVED、HANDED_OFF、UNKNOWN和终态；随机owner epoch防陈旧回调。进程终止或PID复用只释放前置状态，出站状态转UNKNOWN继续占槽与tokens，绝不靠TTL放行。PID+OS进程创建时间核验依赖SDK新local-capacity可选extra psutil（本机7.2.2，uv.lock已锁定）；身份不可读时保持保守，旧缺创建时间条目也不猜测释放。

可信恢复根据同一SDK数据库namespace、invocation和handoff ordinal核对：存在完整terminal usage、明确CONFIRMED_NOT_STARTED，或容量标记后SDK handoff未提交的CLAIMED记录才可清理；对账proof只存opaque identity/version。复制库到另一目录不能释放原库容量。该对账接口不是模型工具。

范围：只约束采用同一profile/账本的同机客户端；直接HTTP、另一个profile文件或其他电脑不受它约束。当前不改DGX服务配置、不声称跨机器全局限流。源码UI仍由完整launcher管理唯一Vite/backend，不打包。

|定向检查|结果|实测时间|
|---|---|---|
|SDK原语/出站/Orchestrator/计量/恢复/历史结果合同|40PASS；含进程杀死、PID复用、取消、缺usage未知保持、同DB可信对账、错DB拒绝、实际runtime嵌套一次占用|2.59秒|
|Host本地profile/主对话与编排共池/绑定幂等及原LAN请求|15PASS，配置不符拒绝、原provider身份保持|以host-review-v4.log原始时间为准|
|静态检查|5个SDK源mypy通过；Ruff检查收尾中|工程未独立计时|

保留初始失败：父级把Agent.run_id(str)当RunId.value导致真实runtime用例失败，已修正；Host测试夹具空messages先于输出cap校验失败，已补真实消息。独立审查发现重复绑定自锁和PID复用陈旧队列，已新增回归与修复，未通过放宽断言隐藏。

真实协议预声明：Qwen256K；三个160K输入进程（最多2并行），两个210K输入进程（加权后串行），每请求输出上限4096、超时600秒；先查服务idle，未知用量停止后继接纳。具体任务文本、源hash和实际server usage将在运行前后存本机ignored证据，不把预声明当已执行。

## 实机发现的校时问题

最后更新：2026-09-15 06:01 CST。N1真实三进程v1为FAIL：两次物理调用321104tokens、0新增抢占，第三路前置排队被错误释放（已知0出站）。系统校时影响psutil.create_time造成身份误判，旧源3反例FAIL；改为psutil稳定process hash（>=7.2.2），新25PASS/1.85秒，完整及实机后继待验。N1–N8仍OPEN，Flash0。

稳定身份使用psutil公开Process hash；macOS/Linux由内核单调创建身份组成，Windows使用创建身份。unsupported平台/旧缺identity记录保守保持，只在PID确定不存在时按原状态恢复；新列process_identity与旧process_started分开，禁止跨算法比较。散列碰撞只会保守保留，不会误释放。真实v1原始结果不改写；第三路无usage是出站前拒绝，不是物理调用未知，修正审计另存parent-audit.json。

## 原生v57发现的响应等待误判

2026-09-15 06:27 CST：前台真实3540tokens/2.565秒成功且与Mission实际两路重叠，但后台非流式模型响应超过180秒被误报executor_stalled，已记录失败并取消后续尝试；在途用量保留，未宣称UI通过。容量包装现在对实际物理await设置固定600秒截止，AgentBridge仅在该活跃有界await期间标记provider_response_wait（billable=true），不伪造progress。队列仍为非计费slot wait，截止/取消清除活跃标记；未知物理结果仍占容量。新27定向PASS/2.58秒（实际Orchestrator慢响应越过短stall及超时UNKNOWN保留），旧liveness桥反例FAIL；当前完整/原生重跑待验。上一e80cfa8完整2306PASS/32条件SKIP/713.40秒只覆盖上一源码。

额外旧SDK出站/预算/恢复78PASS/3.58秒；一条历史测试时钟未使租约过期，c60bf518也FAIL，补足有效租约不抢占和过期后恢复两阶段。两项额外Memory互操作测试因环境缺少兼容simple_harness_memory未收集，不算通过。


### 2026-09-15 最终 Mission 评审预算修复

本地源码 UI v58 不再出现停滞误杀：Task COMPLETED、code_test 实跑 83 PASS、三产物 VERIFIED；Mission 仍 FAILED，最终 Critic 在出站前被拒绝（已知0调用）。原因是 Mission 自身预留从6000扩到实际请求9894时，误进入系统 Task Critic 的保护额度路径，要求不存在的 Attempt。限定合法 Mission judge、同 Mission 账户且无 Task 归属时回到普通预算账本；总预算不变，其他 Task/外来账户仍拒绝。旧源码决定性1 FAIL；新关联47 PASS/5.05秒。当前生产完整编排及原生复验待；前一8b5cbc1范围2308 PASS/32条件SKIP/668.54秒，620源码测试hash无变化。

Host本机证据索引：`.local-test-evidence/2026-09-15/shared-capacity/judge-growth-old.log`、`judge-growth-v2.log`、`native-wave-a-context-v58/failed-judge-audit.json`。v58八次物理调用50661已知tokens、1前置拒绝、0未知新增、0抢占、容量归零；Mission阶段463.944秒。不得把已验证Task计作Mission交付。N1–N8仍OPEN。


最新f122b8c范围完整编排＋容量/出站专项：2304PASS、32条件SKIP、6FAIL/672.22秒（受管672.626秒）。6个失败均为运行环境缺tiktoken；在已有兼容测试环境重跑相关两个文件49PASS/0.87秒，生产代码未改，621个输入hash不变。这是“广回归＋针对依赖失败的复验”，不把原6FAIL抹去或写成一次全绿。原生v59同题正在运行；Host有界响应等待误显示UNKNOWN另已修复36PASS，新Host UI待。

- Host `.local-test-evidence/2026-09-15/shared-capacity/full-v4/command.log` SHA256 `21e48c7b4d0ea4cab3ba56258bf24d4a648f9d1581f86d85de0aa105cdcedcc6`

- Host `.local-test-evidence/2026-09-15/shared-capacity/full-v4-tokenizer-followup.log` SHA256 `685c682c5230ada624b6405d1f86936138088ec94f5c60062bf6e6a35cff7b6e`

- Host `.local-test-evidence/2026-09-15/shared-capacity/source-after-full-v4.json` SHA256 `fcfaca5ea021142f6db81fdbdacff979423aaea97645ef1abb97c6d5c99140ed`

- Host `.local-test-evidence/2026-09-15/shared-capacity/judge-growth-v2.log` SHA256 `5452822f184c9827778c0d3bab091730b2fb6f6cbb1a07cfb5b325d69534c0d3`


### 原生同题 v59 正式交付（保留 UI 展示缺陷）

Host a7518d19 / SDK f122b8c，题目/成功条件/预算/工具与v58完全相同。Mission mission-6165b70b4c813fd2正式COMPLETED，Task Critic PASS、实际code_test 65PASS；Planner另选human_review，父级读源码/测试/报告后在UI填写“助手测试复核，非用户本人审阅”并通过，三个产物VERIFIED。83事件回放差异0/未知事件0/缺口0，预留0。此轮最终Mission复用了Task Critic，不能据此声称独立Mission judge增长路径已实机验证。

本地SDK和HTTP均14次真实调用、112753已知tokens（Mission109099，前台3654）；无新未知、无AttemptTimedOut。模型跨度988.051秒，包含助手审批等待后终态1095.251秒；观察器1190.643秒不算模型时长。共池14grants全部SETTLED，峰值2槽/271582、抢占增量0。服务全局计数+15，比本地SDK/HTTP多1，无法归属的那次不编造tokens，也不声称服务独占。

累计本轮续作299个本地可归属调用、6875081已知tokens下限，1早先未知保留；另记服务全局未归属增量1。Flash0。新Host a93073ce更新后v60冷复制129文件逐个hash相同，源码启动中；独立的针对性case先固定于v60-focused-protocol.json，不替换同题结果。

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v59/final-audit.json` SHA256 `641de67192a38cecdb70d518b70cdbedde4df3cd7811752781493a8e602139f3`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v59/delivered.png` SHA256 `c68abe420a1fc16429d211211bec56ef75f14ce26715664e7a75962cc3fba183`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v59/replay.png` SHA256 `8689222a7a668e6e81797dd0a32e9321caf0e3387e0e14aba0122e4a0d34c5ac`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v59/mixed-capacity/summary.json` SHA256 `56ab65e86598e05a8a5434b74cdc99ba948c7e2d90be921f304892c7aa05c0cf`

- Host `.local-test-evidence/2026-09-15/shared-capacity/v60-focused-protocol.json` SHA256 `4f66e574d6b7102e79b5d2104d43d4d251d85cc66821972adada7781ba037b4e`


### 本地独立最终评审 v60 已交付（2026-09-15）

Host a93073ce / SDK f122b8c，Mission `mission-ad21508d89234c57`，与v59不同的预声明专项。Task只rule_check/code_test，实跑60PASS、三产物VERIFIED且文件哈希复核相同；最终成功条件两条均由 `source=independent` Critic 判定通过，Mission COMPLETED/verification_passed。第一次独立评审输出达到8192上限而失败，第二次通过；该输出上限不是256K上下文容量。两次初始6000的独立Mission额度分别实际增长到19961/31678，结算19961/28722；没有Task账户的额度增长路径已真实验证。

共24次可归属调用、255431已知tokens，模型跨度1713.610秒；0新增未知、0AttemptTimedOut、最终预留0。共享容量24个新grant全部SETTLED，服务全局请求增量24与本机一致，抢占增量0，最终服务空闲。连续观察器在1500.573秒先结束，尾段只有终态快照，不能声称全程连续采样。真实UI查看正式交付及回放：104事件、0差异/未覆盖/未知类型/证据缺口。旧Host最终Judge期间仍显示排队，后继374aa70a专项待验。

这些是范围固定的UI/运行机制验收，不是任意输入完备性或正式benchmark成绩。累计323个本地可归属调用、7130512已知tokens下限；1早先未知以及v59服务全局额外1次未归属分别保留，Flash0。

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v60/final-audit.json` SHA256 `c1f476a50f0c85bb90d5e6717234921e4e5c48c08f287ec7ce0d6eb82d8ea13f`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v60/delivered.png` SHA256 `a47a0e9d0a4d752274a4a0bdd0b75b7123ebeee4396a466cc44ac48d9ef99975`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v60/replay.png` SHA256 `79b66ccd2d3cc291b45b1cb1a43c73a2d3b6f47a28500414937b1f091fae8104`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v60/terminal-capacity.json` SHA256 `d272a46f6b5b7cd45cb337e86cfcab2739f5874bbfaeae2a4a318be67b5692a9`


### 最新源码 v61 原生验收完成（2026-09-15）

Host生产374aa70a / SDK生产f122b8c；源码快照v6 Host与v4 SDK/Python。153个数据文件冷复制逐hash一致，5个旧Mission JSON完全相同；实际打开恢复后的正式交付任务、VERIFIED产物。没有打包。

两个相同预声明小专项分别为 mission-0942dac3b3d7ceb0、mission-2c10b179ee2c316c。首例6调用14138tokens/96.742秒，正式交付但未及时捕获短暂待验证画面；为补采而重复的第二例6调用13333tokens/144.183秒，额外开销单列。第二例在所有Task完成、最终独立Critic运行时亲自打开任务，列表及详情均显示“待验证”，随后正式交付；截图保存时间位于TaskCompleted与MissionCompleted事件之间。没有改变题目、预算或输出/上下文配置。两例Task均只rule_check，无Task Critic/human/code_test；最终条件由独立Mission Critic确认。NOTES.md恰为5字节READY，VERIFIED且文件hash与界面相同。第二例真实回放41事件、未覆盖/差异/未知类型/缺口均0，预留0。12个新共享grant全部SETTLED，最终服务空闲、占用0。这里没有连续压力观察，不以此宣称两路压力通过。

累计本轮后续335个本地可归属调用、7157983已知tokens下限；1早先未知和v59全局额外1次未归属保留。Flash0。最新SDK广回归仍如实记录2304PASS/32条件SKIP/6缺tiktoken环境失败，两个关联文件在兼容环境49PASS；不是一次全绿广回归。源/测试621hash不变。Host最终评审投影旧源反例FAIL，新37PASS/8.47秒；实际UI证据现已补齐。

N1此次共享容量、稳定进程身份、长响应等待、独立Judge额度与两个UI状态缺陷已完成各自限定验收；N1整包的长时恢复和B512K仍OPEN，N1–N8与正式A96/B96没有被本次关闭。最终评审在途额度显示按事件更新，短时间可能落后于数据库增长，不能拿中间UI数字当即时供应商账单。当前保留原生v61运行及防熄屏。

#### 整体计划当前状态与已记录时间

|工作包|已完成切片 / 未闭合门槛|已记录实际时间（非累计工程时间）|
|---|---|---|
|N1 运行与容量|共享2槽/加权接纳、独立Judge、长响应与当前原生UI通过；长时恢复/B512K仍待|5调用压力428.455秒；v60 1713.610秒；v61两例96.742/144.183秒|
|N2 观察与知识复用|可信观察接线/负控完成；困难AppWorld仍FAIL，跨Task消费收益未证实|v4 1557.359秒；本次只读审计0.013秒|
|N3 检索/结果契约|两道原失败题后继交付；完整困难矩阵仍待|后继290.623秒|
|N4 配对正式矩阵|通用runner已实现；A96/B96均未开始|正式运行0|
|N5 AgentDojo|正常/攻击各一例官方utility通过；完整黑板传播待|209.980/1046.198秒|
|N6 ARE/Gaia2|自定义ARE硬判通过；完整Gaia2/独立judge待|275.301秒|
|N7 效果归因|预声明与区间统计接线；配对机制收益待|工程未独立计时，不能用其他测试时间代替|
|N8 正式评测/交接|本次源码UI、证据和主分支交付；完整正式split仍待|工程未独立计时；与上述测试重叠|

#### 当前部分详细完成情况

|事项|结论|实际测试时间|
|---|---|---|
|共享容量真实5调用|2槽和加权单路、0抢占、0残留|428.455秒；外层499.215秒另计|
|最终独立Judge额度增长|定向47PASS，v60真实独立分支交付|5.05秒；v60整例1713.610秒|
|有界物理响应UI|真实在途显示运行，不再误标UNKNOWN|36关联PASS/8.39秒；UI包含v60内|
|最终Mission状态UI|37关联PASS；v61列表/详情待验证→交付|8.47秒；两小例96.742/144.183秒|
|广回归及环境失败复核|2304PASS/32SKIP/6环境FAIL；关联49PASS|672.22秒及0.87秒；不合称一次全绿|
|冷恢复与回放|153文件hash、5旧Mission保持；真实回放0差异|未独立计时；0额外模型调用|

#### 还剩多少工作包与时间估计

|范围|尚未关闭的包数|下一门槛|粗略剩余工程估计；模型/等待另计|
|---|---|---|---|
|N1|1|有限长时恢复、Flash512K容量资格|1–3小时＋真实运行|
|N2/N3|2|困难知识产生/消费、检索与摘要完整矩阵|合计5–11小时＋真实调用|
|N4/N7|2|冻结A96/B96与消融，解释质量/成本/失败|合计4–9小时＋两轮模型运行，尚无可靠总时长|
|N5|1|实际黑板传播攻击链与官方对照|4–10小时＋真实调用|
|N6|1|完整Gaia2、动态协议及独立judge|8–18小时＋judge可用性/费用门槛|
|N8|1|正式split/完整任务组、最终交接|3–6小时＋正式运行及闲时窗口|

仍有8个包未整体关闭，包内功能已经多项完成；以上为粗估而非承诺，部分工作可重叠，不应相加当作确定交期。下一优先做无付费依赖的N2/N3困难机制闭环准备和N5传播扩展；不为追PASS放宽现有失败任务或盲目重跑。Flash512K集中闲时执行，费用上限/独立judge未确认前不启动相应付费块。

#### 本次续作新增完成

|事项|结果|实际时间|
|---|---|---|
|收尾v60真实独立评审|24调用255431tokens正式交付，首次评审输出失败保留|整例1713.610秒；此前已启动，不能全部算成本次工程时间|
|v61最新源码冷恢复/最小任务|首例交付，第二例补齐短暂UI状态；12调用27471tokens|96.742＋144.183秒，两次分别计|
|N2既有失败账本复核|合法额度拒绝，0已接受知识/0下游消费；未新增付费或本地模型重跑|0.013秒只读审计，工程时间未独立计量|
|架构/计划/主分支交付|仅文字结论/索引/hash入Git；原始证据留本机|未独立计时；Git同步结果见最终交付消息|

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v61/first-final-audit.json` SHA256 `dc3df7e6e11673693740d2b6c1616390d13973a41929a04e745a338005e7193f`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v61/final-audit.json` SHA256 `4a1fb69bc69480c5d2e58314564a7d24fe92abf0b46cce457a4968acbb7f8b09`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v61/verifying-detail.png` SHA256 `0695cb7ee026ce6d341b802e74efc990af63922e7521b0bba89d45cd2e01a84e`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v61/verifying-live-audit.json` SHA256 `2a6875a1bd2516157092f6c7672588e6d374a941c6ee62c8fc8133c236f0a25a`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v61/repeat-replay.png` SHA256 `488d218bbb83e84ea398dce0c01aae79a4853d6148907718ef31c891bb4e866c`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v61/terminal-capacity.json` SHA256 `ac77d98a3c05b7a499c40b73c75edef19e5751e234ee2298a77f74eb6720e40f`

- Host `.local-test-evidence/2026-09-15/shared-capacity/native-wave-a-context-v61/cold-missions-audit.json` SHA256 `db20a414df5cffd103f27bd894d4064148f6094954641e1deec68c14ee401438`
