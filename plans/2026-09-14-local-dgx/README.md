# 双 DGX 本地模型接入验收

最后更新：2026-09-14 CST。当前请求的源码接入与验收完成。Host生产源码922b2d7b / SDK6d4ddc7，冻结source-snapshot-v43；后续提交只整理文档。原Phase3的46项源码验收与2项用户暂缓打包条件保持原证据范围，未重跑为本地模型的全量Phase3质量认证。

当前连接到用户既有LAN服务，模型qwen38-flash-next；服务报告max_model_len=262144。服务器运行参数TP=2，未更改远程部署。本轮没有调用付费DeepSeek API。API密钥从原本已存在的LOCAL字段读取；本机默认注册到OS Keychain，两处本地配置仅启用dgx-local。费用账本中的估算/未计价不代表实际付费API收费，也不代表电费为零。

整体计划完成情况（时间不相加；测试时间不是开发工时）：

| 范围 | 完成情况 | 可验证时间 |
|---|---|---:|
| 默认本地连接与配置 |完成|开发/配置未独立计时|
| 256K实际容量与工具协议 |完成|近上限请求111.089s；整组125.19s|
| 源码原生UI及冷读 |完成|原生生命周期370.415s，冷读72.388s|
| 原Phase3打包/安装器 |仍按用户要求暂缓2项|本轮未执行|

当前部分细节：

| 检查 | 结果 | 测试时间 |
|---|---|---:|
| SDK provider/HF counter/RequestGuard/Agent |105 PASS|5.92s|
| Host配置、启动器、上下文、wiring |81 PASS|18.33s|
| 主对话LAN修复及现有适配器回归 |44 PASS|4.29s|
| MissionsView |81 PASS|1.40s整组|
| TypeScript、Ruff |PASS|未独立计时|
| 实际短聊天、工具请求、工具续接 |3 PASS，输入计数56/345/402逐一等于服务器|1.246/4.812/2.540s|
| 近256K容量请求 |输入260001、输出179、合计260180；首/中/尾标记全部正确|111.089s|
| 原生代码Mission |COMPLETED，rule/Critic/code_test PASS，实际pytest2PASS，两个产物VERIFIED|146.749s|
| 原生主对话 |正确返回DGX_UI_OK_256K|包含在原生生命周期内|
| 冷启动 |6张选定持久表及Provider记录完全一致；0新增调用|72.388s生命周期|

剩余任务：

| 范围 | 数量 | 预计时间 |
|---|---:|---:|
| 本次模型连接、256K与源码验收 |0|0|
| 已暂缓的Phase3打包/安装器条件 |2|不在本次范围，尚未估时|

本次请求以来完成的事情：

| 事情 | 结果 | 花费时间口径 |
|---|---|---:|
| 核对现有DGX端点并复制实际tokenizer |匹配服务，4个输入文件SHA绑定，无模型权重下载|未独立计时|
| 修复LAN HTTP、HF5返回值、工具参数表示、主对话遗漏 |回归通过；初始失败证据保留|包含开发总区间，未拆分|
| 3个Sol子代理 |审计108.30s；HF325.86s；启动器287.57s，均已关闭|并行耗时不能相加|
| UI交付和冷读 |任务6调用15119tokens；聊天1调用3535tokens；全部本地，0错误/rehandoff|370.415s +72.388s|

预算口径：256K=262144是输入输出共享总窗口。Mission输入最多228352（223K），输出预留32768，安全余量1024；输出从8192起步，既有有界恢复最高32768。一物理模型槽。接近260K的直接SDK容量测试把输出上限设为512，它不是Mission默认223K输入配额的测试，也不证明一般长文/多模态质量。主对话全局model_overrides窗口为262144，设置页用十进制显示262.1K，压缩阈值保持80%。

接入路径：源启动器默认`--provider local`，须传`--local-profile <absolute JSON>`；缺失/身份不一致拒绝启动，不会改用付费模型。JSON字段为base_url、model、max_total_tokens=262144、tokenizer_path、tokenizer_files（SHA256映射）、chat_template_kwargs={}。在源码Python环境安装SDK的hf-tokenizer可选依赖。此机profile位于用户Library/Caches/simple_harness/model-tokenizers/qwen38-flash-next/profile.json（非Git）。新机器需从同一实际部署复制tokenizer并生成自己的绝对路径profile；不复制密钥进Git。旧冻结DeepSeek Mission不会被静默改绑模型，应新建本地任务。

HF counter离线加载、禁用远程代码，将HTTP工具参数JSON字符串转换为HF模板需要的对象副本，HTTP请求保持原JSON字符串；显式return_dict=False避免HF5把BatchEncoding容器长度误作token数。SDK RequestGuard优先使用精确整请求计数hook，原counter逻辑保留。局域网HTTP由Host显式启用；SDK仍拒绝公网IP、DNS、link-local明文地址。实际前台与任务两条适配器均已覆盖。

原生Mission ID：mission-e9333bd71f9bfa95。成功产物sum_local.py SHA256 184dc2fe6e1d2f7108d62731e539aeeeac2b165f4ee5e87607076f6b92082513；test_sum_local.py SHA256 33fff85a05b918477692e54c3e1a14b0a545fbfe0d4c99b859ee1eabe196c23d。Critic看到实际test_output，独立code_test保留真实执行receipt、exit0、2PASS、无残留。实际UI打开并冷读文件；不是接口注入代替UI。

初始失败保留：HF5默认返回BatchEncoding计数错误；HF续接arguments字符串模板错误；SDK LAN拒绝；Host旧三参hook兼容；原生v42前台未设置LAN opt-in，4次本地构造失败、零HTTP调用后停止。一个HF旧测试期望字符串导致104PASS/1FAIL，修正为对象+HTTP不变契约后105PASS；错误测试路径选择无测试执行，不作PASS。v42生命周期183.188s退出0、无残留；v43原生/冷读都退出0且进程组清空。首次源码启动依赖加载约90s，冷启动约10s；未做启动性能优化或安装包验收。

子代理实际模型、cache/uncached用量与返工见Host plans/2026-09-14-local-dgx/agent-observations.md。主会话仍Astra/high；不以不可比任务宣称节省tokens。

本机原始证据根`.local-test-evidence/2026-09-14/dgx-local-connect/`；下表仅索引和哈希，原始文件不上传Git。SDK集中测试日志在其`.local-test-evidence/2026-09-12/p33-g/`，标签g-local-sdk-final-v5、g-local-host-profile-v5、g-local-foreground-v1、g-local-sdk-probes-v3、g-local-sdk-near256-v1。近上限测试起于工作树、随后提交，实际counter指纹与提交6d4ddc7一致；runner的旧start_head不是完整冻结证据，冻结原生由v43单列。

| 本地证据 | SHA256 |
|---|---|
|acceptance-summary.json|eb9c455debf63da229eb7f9e9ffcd5cbd4782c71704a86c6681aa74447641066|
|near256-v1/sdk-probes.json|44c767c1c05d8d7e45b0387440e8ab5b4b7f22cd916e95db7e664a4f38f262b8|
|probe-v3/sdk-probes.json|9c64892403feae9d1a6371c84c9518416631d7ede64bf3ef602e4eded27e250f|
|native-before-cold.json|43900faa218a52818c2041fb4d1f30de6edc26cfb87418eebaf310570c232b84|
|native-after-cold.json|43900faa218a52818c2041fb4d1f30de6edc26cfb87418eebaf310570c232b84|
|configuration-summary.json|6d899e514123541965aa0dc2a072dca416fdfc9490a2e6e71381701ef6b4b803|
|usage-latest.json|8aee09853a55992f3aa2199343bfa1b4918125709a871f1f8d8b6f29d08c978b|
|ui-tests.log|ebf0149629a7d604994ff2321dfbe98a8ec7913195bb1ca0cbab166c77266920|
|typecheck.log|5b7678e1378357b8e083c5d0c9913336411378485c091e11ef7cbd1372a06460|
|native-v43-default.png|a81ff65fd43eed2f4ee5192b635ddf41e311db731111f4b37e824839234e6889|
|native-v43-delivered.png|d2913db364b6abd672f4528240232378339b124870ba883f336d162ef34ae376|
|native-v43-chat.png|d3018b59d37fab45a500cc4153ea3faf256285aff8a7d477c6b8a48bfeed1b86|
|native-v43-settings.png|603133126755103a2dac579f5f85760cb9cab57d8219f9428e95944bdebd1170|
|native-v43-cold.png|198d85d1fa478a7308f051f25f8e7a6f63421caa9c889cb4932563b4f7eca153|
|native-v42-chat-fail.png|6660f743feab492ed8562d6ccafc24e7619f51989649393f07159cfe6ac02d1f|
|native-v42-live/resource.json|e2e604a66124c0bf8ba1567ab2d4b9481a65fb51acc0314557f9d67c34ef9f87|
