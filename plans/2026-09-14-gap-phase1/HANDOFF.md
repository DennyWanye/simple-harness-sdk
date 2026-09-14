最后更新：2026-09-15 02:44 CST。code profile v4生产3e2792f：原分页/时间例外两个失败题自然复验均VERIFIED（16调用139927tokens/290.623秒），旧失败保留。完整v9为2275PASS32SKIP1旧版本断言FAIL，后继28PASS及官方ARE50PASS，无生产再改；当前源码UI v54冷恢复实点通过，0新调用。累计261本地调用5430502已知tokens下限/1早先未知，Flash0。N1–N8/A96B96仍OPEN，无打包。 当前明细及四张表见[结果封套后继](RESULT-CONTRACT-FOLLOWUP.md)；下方为历史检查点。

# testPhase1 源码交接

01:36接续：最新增量为AppWorld v3有效知识刷新与评测额度终止适配；69定向PASS，真实v3失败保留，新v4/全量v8/原生后继仍待。具体状态以 BUDGET-ANALYSIS-FOLLOWUP.md 最新条目为准。下方eeeba33仅为前一生产检查点。

最新源码：SDK main `eeeba337f36b354248abc75853aa551746c9f2e9` 已推送；跨域Planner累计预算说明和N7任务块区间已经接入。最新原生v52冷恢复/产物/回放实点通过，原5调用不变；新全量2259PASS/32SKIP/1旧hash断言失败，测试快照按预期消息变化更新后52PASS（未再全量重复）。N2 v3真实终态仍待；完整N1–N8和A96/B96未完成，Flash0。后继状态以[预算与分析跟进](BUDGET-ANALYSIS-FOLLOWUP.md)为准，下面旧检查点均保留为历史。

**执行更新（21:25 CST）：** 已按用户指示开始；当前局部通过、两路抢占和剩余门槛见[执行记录](TWO-WAVE-EXECUTION.md)。以下原计划的“未开始”描述保留为冻结前检查点。

**当前接续更新：2026-09-14 20:55 CST。** 用户已将范围扩展为原提案剩余N1–N8；先集中Qwen3.8 256K，再集中闲时Flash512K。N4每轮96次共192次；当前Host实配1路，目标总上限2须近窗验收。模型分配、困难用例、独立judge、预算与闲时准入见 [两轮评测协议](TWO-WAVE-EVALUATION.md)。新增实现和矩阵尚未执行；下文“不做96次/新基准”等限制为旧轮范围，不再代表后续计划。旧实验参数与失败证据保持不变。

**最后更新：2026-09-14 20:10 CST — testPhase1后续修复与独立Flash16次回收完成。** 当前SDK功能5406fb5，完整编排2105PASS/20SKIP；256K四题S/R各3/4、D/F各4/4，有效14/16，648调用8760086tokens，runner2234.319秒。0未知用量/网关终态缺失/工具重下发，8个D/F终态预留0；知识复用和动态图收益未得到证明。旧本地9终态/7未执行/1未知保留。用户发现白屏已通过完整源码重启与实际点击恢复，原0残留仅指受管组；当前UI有意保持运行，独立评测服务已结束。 [最终四臂结果及四张进度表](FLASH-FINAL.md)。

历史20:10接续：四项源码缺陷已交付，完整Flash实验已结束。剩余评审点为本地长任务耗时/预算、AppWorld可核验观察及机制收益、128K与仅Flash的512K新压力档位；本轮不继续增加付费矩阵或打包。两个仓库拉取main，以editable SDK源码核对实际import路径。原始证据仅在本机ignored目录。

**当前接续（2026-09-14 19:32 CST）：** SDK功能源码5406fb5修正AppWorld Worker结果契约，新domain profile默认v2、旧v1冻结保留；完整编排2105PASS/20SKIP及原生v48冷读通过。原本地后继因1200秒取消/1未知用量停于9终态、7未执行，独立Flash校准已出现四Task＋官方成功；完整Flash16次后继正在运行。全部采用256K，窗口与累计预算分开；512K只限Flash。见[当前源码、结果与运行边界](FOLLOWUP.md)。下文16:47检查点与旧代码表均属于历史实验，不代表当前源码仍未修复。

最后更新：2026-09-14 16:47 CST。功能代码已推送，16次四臂实验已结束。当前状态见 [执行记录](README.md)；这里说明如何接续源码开发，不是安装包或公开 benchmark 成绩。

## 当前源码与接续方式

拉取两个仓库的最新main；当前SDK功能提交为`5406fb5b60f03fcfeb781725131e70ebf94e4ee0`，Host功能代码仍为`23cb7d374bda190e629268d250af3b03a39b49b4`，后继文档提交不改变功能身份。原生v48冻结SDK5406fb5/Hostde9a83be；AppWorld后继使用同一个SDK快照。新电脑仍需使用editable SDK源码并核对实际import路径，旧wheel不含本轮修复。

原本地pilot-local-followup-v2已停止，不要更改其记录、清除未知用量或把余下7次标为完成；新的pilot-flash-followup-v3结果单独结算。只读取本机ignored证据，不能假定它们随Git同步。

## 历史代码身份（16:47检查点）

|用途|SDK|Host|
|---|---|---|
|当前远程 main 功能提交|`84c32358fb4fb36c765ed92b7212b6423ed06a35`|`23cb7d374bda190e629268d250af3b03a39b49b4`|
|最终测试隔离快照|`84b0a2e115bbd4d279a3d421bf0d0df21f68e57f`|`e0aed2aad0ef41aea44e321a13a24f0aae3681de`|
|已完成的四臂实验|`2f8eacfc6255e110e1ee2dafd4d4dac180fc42c9`|不经过 Host UI|

提交前逐文件比较：SDK 60 个代码/测试/依赖文件、Host 6 个代码/测试文件与最终快照完全相同。main 提交包含新的架构文字，故提交 SHA 与快照不同。正式四臂实验冻结在启动修复前的快照，每个 episode 新建库，不注入冷恢复；不得改写原实验源码身份。

另一台电脑继续功能开发时，拉取两个仓库的 main，并让 Host 的开发 Python 以 editable 方式使用此 SDK checkout。只看到 package version `0.11.1` 不足以证明加载了新代码，应核对 `agent_orchestrator.__file__` 和 Git SHA。旧固定 wheel 不包含本次实现；本轮没有改成发行包。

## 验证入口

|范围|入口|已验证边界|
|---|---|---|
|新的知识、AppWorld 和恢复控制|`tests/orchestrator/gap_phase1/`|确定性正负控；不能替代真实模型或 UI|
|最终 196 项组合|上述目录加 p33 大读取/分页/judge 恢复、step02 重试/恢复矩阵、p35 Context 冷恢复|精确命令见执行记录关联 `gap-final-scope-v46.json`|
|Host 默认读取工具|`backend/tests/orchestration/test_knowledge_tool_door.py` 及三个关联夹具|9 PASS，包含真实 Host gateway|
|原生 UI|Host `scripts/native/launch_source_orchestrator.py` 的源码启动路径|只起 Tauri，由其管理 backend 和唯一 Vite；指定源码 backend 目录与开发 Python|
|AppWorld 环境服务|`python -m agent_orchestrator.evaluation.appworld_service --root <本地评测根目录> --port 18244`|绑定 loopback，精确兼容 `appworld==0.1.3.post1`|
|四臂协调器|`evaluation.appworld_experiment.run_appworld_pilot`|固定四题、S/R/D/F、一次重复；调用方提供冻结的 Provider/计数器/配置|

AppWorld 是可选评测依赖，普通 Host 启动不导入它。评测使用独立 venv 和官方数据根目录。当前环境记录为 AppWorld `0.1.3.post1`、transformers `5.16.1`、Jinja2 `3.1.6`、httpx `0.27.2`；原生 Host 与 SDK dev 测试 venv 单独记录，不宣称它们是同一个环境。

Provider 凭据从本机凭据来源读入内存，不写进启动器、Git 或证据。`run_appworld_pilot` 的工厂必须绑定实际 Provider identity、600 秒请求超时和对应 tokenizer。SDK 的独立计量器覆盖所有角色、失败与未知用量；发现未知物理用量会停止接纳余下 episodes。

## 实验固定参数

- 本地双 DGX：`qwen38-flash-next`，总窗口 262144、有效输入 228352、输出预留 32768、安全余量 1024；实际输出从 8192 起、有界扩展至 32768。窗口容量不等于每次都发送 256K。
- 每 episode：1600000 输入/总 token 上限、131072 输出 token 上限、60 次模型调用、1200 秒、1 个物理模型槽；D/F 逻辑 Agent 并发最多 3。
- 预选 dev 题：`37a8675_3`、`d4e9306_1`、`4ec8de5_1`、`6c2c621_3`；拉丁方轮换四臂顺序，world seed 100；模型使用 SDK 默认采样，无模型 seed 参数。
- R 为同一个 BaseAgent 的两候选、自选；使用宿主保存/恢复模拟世界，属于明确声明的开发实验设置，不能冒充标准不可回滚环境成绩。外部评分在执行结束后进行，不进入 Agent 提示。
- 本次不补跑失败臂、不混入 DeepSeek 结果、不计算不完整任务组 SGC，也不扩大为 96 次。

本机运行目录为 Host `.local-test-evidence/2026-09-14/gap-phase1/`。其中 `run-pilot.py` 是绑定本机路径和凭据读取的启动器，不提交；算法实现、四臂控制、预算计量和评分隔离在上述 SDK 模块中。换电脑不能直接复用绝对路径，也不能假定 ignored 原始证据随 Git 同步。Git 仅保存文字结论、运行身份、证据索引与 SHA-256。

## 历史接续建议（16:47）

16次实验已执行完毕，见 [RESULTS.md](RESULTS.md)。有效闭环S3/4、R2/4、D/F各0/4；R另2次官方终态成功但自选JSON解析失败。先评审Planner预算可行性、R选择协议和异常网关记录3项修复，再以新实验身份验证；不覆盖本轮失败、不直接扩大96次。完整最终编排回归2079PASS/20SKIP/0FAIL，原生与正式模型试验分别保留来源。

本轮不处理 wheel RECORD、安装器、打包发布、96 次扩展、AgentDojo、Gaia2。两路 65K 实测仅证明该输入规模的并发传输，严格文本格式失败保留，不能据此开放两路近 256K 默认并发。
