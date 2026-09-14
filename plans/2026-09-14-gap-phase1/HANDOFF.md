# testPhase1 源码交接

**当前接续（2026-09-14 18:33 CST）：** 三项源码修复已提交，完整编排2096PASS/20SKIP及当前源码原生冷读通过，新的四题16次真实复测仍在运行。后续上下文档位仅128K、256K、512K；本地默认256K，512K仅DeepSeek Flash。窗口、单次输出和累计预算分开报告。见[后续修复与最新范围](FOLLOWUP.md)；下文16:47检查点属于上一轮实验，不能当作当前修复仍未实现。

最后更新：2026-09-14 16:47 CST。功能代码已推送，16次四臂实验已结束。当前状态见 [执行记录](README.md)；这里说明如何接续源码开发，不是安装包或公开 benchmark 成绩。

## 代码身份

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

## 接下来

16次实验已执行完毕，见 [RESULTS.md](RESULTS.md)。有效闭环S3/4、R2/4、D/F各0/4；R另2次官方终态成功但自选JSON解析失败。先评审Planner预算可行性、R选择协议和异常网关记录3项修复，再以新实验身份验证；不覆盖本轮失败、不直接扩大96次。完整最终编排回归2079PASS/20SKIP/0FAIL，原生与正式模型试验分别保留来源。

本轮不处理 wheel RECORD、安装器、打包发布、96 次扩展、AgentDojo、Gaia2。两路 65K 实测仅证明该输入规模的并发传输，严格文本格式失败保留，不能据此开放两路近 256K 默认并发。
