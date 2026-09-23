# Assurance 主体开发检查点

最后更新：2026-09-23。状态 IN_PROGRESS；BODY_WIRED 未通过，完整验收未执行。

## 隔离与来源

候选 `/Users/denny/projects/simple-harness-sdk-assurance-impl`，初始实际 dirty 源快照提交
`187e1f4dc0147cace1d3c2092b9de9acc2757b11`。所有新实现由主 Agent 编码，子代理仅只读评测。

2026-09-23 接收 TaskGraph 主体交接后，将 `taskgraph-23/sdk` 的 123 个差异文件
以初始提交为基线接入，无双方重叠修改；14 个 Assurance 新文件逐字节保留。
未复制 venv、缓存、数据库或运行证据。原 Host、HTN candidate 保持不变。
最终 Host 接线必须从 `taskgraph-23-host-ui2/host` 取增量，当前尚未创建 Assurance Host 副本。

来源和接入逐文件 hash：Host ignored
`.local-test-evidence/2026-09-23/assurance-integration/sdk-three-way.json`。
该报告是源码来源记录，不是运行验收。

## 已写代码（未声称完整运行能力）

- 内部严格 JSON / Pin / Ref，公式与 CHECKED/SEMANTIC 三态检查，证据标签、读集和披露链。
- 本地 checker recorder、证书合同与最终绑定检查、持久时钟转换。
  原 VerifierRouter 的 format/rule（含文档 citation）已增加可装配的实际执行 recorder；
  代码/输入/环境在调用前固定，每次本地检查前后重算实际输入，工作区变化为 ERROR，
  异常为 ERROR，逐断言输出落原 CAS/Artifact，原 Commit
  导入实际 AssuranceLocalCheckFinished Event。此检查只证明相应原算法，不能升级为
  内容正确；local CheckBinding 已接原导入事务（见下节），code_test 仍走原 executor，
  真实 execution receipt importer 尚未完成。
- AssuranceStore 的 policy、disclosure、certificate、pin 与 clock side record 操作；
  权威 issuer/caller、原 runtime manifest 导入仍须在 Commit 集成层落实。
- 原 Store 同连接的 pending/cursor、claim、准备后 effect+ACK、lease recovery；
  同目标异 fingerprint 具名拒绝，savepoint 防止外层捕获异常后留下部分写入。
- R01 全自然 UNIQUE 不可变保护；R02 包括直接 REPLACE 重置队列的拒绝；
  R03 原 Event 到期唤醒，按精确 consumer/use/root 去重。
- 累计 rechecks/首次重评时间持久化；新 target 不重置累计上限；
  32 次/300 秒进入 MANUAL_REQUIRED，前三次立即、后续 500/1000/2000 ms 等待。
- 原 Store metadata exact reader：25 类内部引用的固定适配器（原 17 类＋6 个固定原
  source Event 桥＋artifact/source），精确版本/原 body hash、
  tenant/Mission 关联；不把 metadata reader 当当前权限、CAS 可用性或来源真实性证明。
- CompleteRead 从同一 Store read_view 穷尽固定查询；快照与 source barrier 共用冻结
  53 表库存（含作用域 epoch 与固定 source Event 子集）。严格解码 JSON 列，包含正反证、未采用分支、
  原 schema hash、两级 epoch、clock generation、完整空集和稳定集合摘要。
  缺表/读失败/超界不产生 COMPLETE；执行外库与当前 authority 仍须独立真实 reader。
- migration 26 增加同事务来源屏障及原 Event 唤醒。Mission 聚合分区缺失不当零，
  原方法/政策表全局失效；shared input manifest 通过 origin+全部 bindings 精确映射。
  UPDATE 同体重放不 bump；跨 Mission 的 PK/自然 UNIQUE REPLACE relocation 被阻断。
- 全局屏障显式接原 PolicyCommitsMixin 与 HtnStore 两个方法注册 writer。
  原来没有 commit_receipts 的 typed writer 在同一 UoW 写 INVALIDATION_AUDIT_ONLY
  回执（wake_event_ids 仅是失效唤醒，绝非原来源证明/授权/真假结论），已有原回执则复用。
  source 变更、epoch、原 Event、回执一同提交；原 Store 现处理 deferred FK 的
  COMMIT 失败并回滚，避免放开锁后残留 SQLite 事务。
- 原 grounded_closure 增加有界 worklist 路径，复用原 premise、source/path 算法。
  Assurance compute_grounded_support 先计算全部正负支持，再从当前外部 anchors 重新
  计算排除所有冲突 key 正反两面的 clean closure；原 assumption-free 计算不等于该
  conflict-free 语义。两次计算和冲突扫描共用 10k literals / 20k rules / 1m visits /
  250ms CPU 预算，不能用第一次派生 TRUE 重新当 anchor。正式 certificate evaluator 仍待接线。
- 严格 AssurancePolicy 合同、安装 environment receipt 核对、factory binding/epoch/
  四 cursor 原子初始化已接原 CommitService.create_mission。新 Mission 创建在同一
  savepoint 写原 Requirements、activation Event/receipt、creation lane、reconciliation
  manifest/hash 与初始 pending，cursor=实际 activation seq。实际 Requirements interpreter、
  reconciliation adapter 和部署 installer 仍缺；default profile 暂为 None，不是已启用能力。
- 原 migration 26 内仅一次用真实 MissionCreated/冻结 protocol 事实分类历史 LEGACY /
  COMPLETION_V1，保留原 Mission/Event 正文；运行时缺 creation lane 拒绝，不猜 legacy。
  该 migration 使用 TEMP stage 和原 classification receipt，不安装旧 Mission 的 Assurance。
- 原 Orchestrator tick 已有 AssuranceTick 接点：先 ingest 四 cursor，再轮转最多 8 个
  Mission，每 consumer 最多 8 项；单 claim 有界准备（默认 20s/30s timeout/lease），
  原 receipt+effect+ACK 同事务，检查真实 receipt 精确 hash 和 Mission。无业务 Event 时仍
  扫 expiry。时钟回拨先 ingest 不 claim；await 中回拨以 highwater 入 WAIT，恢复后才继续。
  持久时钟经原 Commit receipt/Event writer；4 个真实 consumer 仍未装配。
- 原 artifact/source/operation payload CAS 精确读取：持久 PREPARING/BOUND pin、nofollow、
  整体 byte limit/hash/size/operation codec 检查，禁止在写事务读大 blob。返回 bytes 前
  和最后使用事务都强制当前 authorize guard＋epoch/metadata/pin 复核。
  PREPARING typed receipt 绑定分配的 package/review/ref；BOUND 再核正式 review binding，
  保持 pin→CAS→ReviewPackage 的无循环创建顺序。pin 只保活，绝不是授权；真实 root/ACL
  adapter、builder 和 GC owner 接线仍未完成。
- 原 offline restore 在 verified staging 发布前生成新 root ID 与 restore-quarantine。
  已校验 marker/manifest 的恢复上下文仅豁免 artifact.storage_uri 重定位的 epoch 变化，
  canonical 结构比较保留其他所有 metadata 变化；避免重写原 formal history。
  startup 全读门、当前重新授权与 Host 入口仍未接线，不能宣称恢复防披露能力完成。

只读评测发现 pending REPLACE 旁路，已补代码并复审确认；expiry purpose 漏检报告
经源码反证撤回（原代码一直包含 purpose 条件）。这些均为静态评测，不是行为验收。

## 接入身份与窄检查

Assurance 在隔离候选登记 migration 26；HTN 1–24、TaskGraph 25 的名称及 checksum
逐项与交接源一致。SDK 两处版本改为 `0.13.0.dev20260923+assurance.1`。
未构建 wheel、未冻结新的完整 deployment manifest；不得沿用 taskgraph.23 的验收资格。
codec v5 覆盖的原类型/codec 字节未变，当前读取仍能通过自身源码指纹校验。

已运行的窄检查：新增模块语法/导入、真实父 schema 上的增量 DDL、合并后的全新空库
由原 Store runner 建到 26，以及旧迁移 checksum 对照。临时数据库随后删除。
证据：Host ignored `assurance-integration/schema26-static.json`。
没有执行 SDK 批量单测、回归、真实模型或原生 UI；历史 TaskGraph PASS 不继承给新候选。

2026-09-23 后继窄检查：新增模块 ruff 与原受影响入口 AST/import、完整 schema 建到26、
52 表字段及全部跨 Mission UNIQUE 库存与真实 schema 对照；原 Store 的 source
insert/replay/revoke、真实原事件 seq、缺原回执的 deferred FK 整体回滚；无锚循环不自证；
restore JSON 键序变化不被当作内容更改，verification 变化不被豁免。
这些是编码接缝检查，不是 SDK acceptance 或 BODY_WIRED。
ignored 证据分别为 `barrier-seam.json`、`schema26-barriers-static.json`、
`restore-compare-static.json`（同 Host assurance-integration 目录）。schema26 仍是 WIP，
后继 descriptor 以最后文件中的 hash 为准，较早定点报告不能替代最后 bytes 的整体验收。

只读评审已反馈并在源码处理：CompleteRead 漏检查表、未 strict-decode JSON、
policy/authority/OCC/claims writer 漏库存；失效 wakeup 被误名为 source_event_ids；
restore JSON 文本键序比较。主体阶段不以这些静态关闭充当产品通过。

2026-09-23 后继定点记录（同 Host ignored 目录）：

- `tick-factory-seam-20260923T011036747279.json`：fixture root/Requirements/consumer，
  原 Store/Commit 创建与 activation seq/seed、真实 source Event 全正文 hash 与防覆盖、
  await 中回拨→WAIT→恢复后的原 effect+ACK、冲突分支与独立 clean 分支。
- `legacy-classification-seam-20260923T010628780633.json`：从 TaskGraph23 原 SDK 实际
  建 schema25 旧库，再由当前迁移器分类；原 Mission/Event 行未改变。记录对应较早
  migration26 hash `0c2e...`，不能替代后继 descriptor 的最终验收。
- `local-check-pin-seam-20260923T013407092617.json`：原 VerifierRouter 真实 format/rule
  →源码摘要/原输入 manifest→原 Event/CAS；真实抛错→ERROR；相同 run 重放不重复，
  同 run 异体/错误 Event ref 拒绝；无关 pin receipt 拒绝；PREPARING 先于 package 可读，
  但当前 guard 在 CAS 期间撤权则不返回 bytes；原无 recorder 的层状态保持，输入冻结后
  工作区变化为 ERROR。当前 root/ACL 为明确 fixture，非部署证据。
- 本轮仅运行以上编码接缝与修改文件 Ruff；未运行 SDK48、继承66、OCC12、批量单测、
  回归、模型或 UI。`tick-factory-seam.json` 是早期固定文件名报告，曾覆盖；后继记录
  已改为时间戳独占创建，不能拿早期报告补充当前源码身份。

当前 migration26 descriptor 保持 `df86c276cc77433f394505280377751b7a08219c3b24b88292080327d90e2a09`
（后续改 DDL 后应重新记录，仍非冻结发行）。新增纯 Python 接线不沿用 schema 报告的能力结论。

本轮独立静态 review（Terra，仅只读）指出并复核：typed pin receipt 不应被当授权；
CAS pre-read、返回前和 final-use 强制注入当前授权检查的接口位置已核对。真实 ACL/root
适配器未安装依然是 WIP。local-check replay 现同时核原 receipt 全字段/类型与精确 Event
payload/task/attempt，不能仅凭 source_hash 复用另一 run 的 Event。原 source Event REPLACE
保护也同时比较 prior.type/NEW.type。上述静态 review 与定点脚本均不是 BODY_WIRED。

## 下一步和未关闭门

1. metadata reader 继续补 CAS/pin/真实 execution/issuer/当前授权适配；完整 validity
   evaluator 将穷尽快照、所有分支、新鲜 anchors、原规则准入和有界 closure 接起来，
   最后短写事务检查 source/root/access/policy/epoch/time 并签发实际 use certificate。
2. factory 的真实 Requirements/reconciliation 部署；CheckPolicy/local CheckBinding 原 writer
   已写（见下节），executor import、六 purpose 原 builder/transport/collector、真实 reserve/Provider
   input/turn 来源，原 acceptance/OCC 和所有终态 guard。
3. 四真实 consumer 与 startup reconciliation 装配；closeout 消费原 TaskGraph convergence、
   UNKNOWN/accounting 责任，保留原唯一 terminal writer。
4. 恢复 root quarantine/当前重新授权的 SDK 接线已写（见下节）；真实 Host 当前认证/ACL
   collaborator、所有允许精确披露的消费面、固定 caller 的三个 Host verb 和 MissionsView 待接。
5. 新部署身份、BODY_WIRED 审计后统一验收，完整后默认 ON，同交付回写 ARCHITECTURE。

## 2026-09-23 续接：恢复根与当前只读授权（主体 WIP）

主 Agent 新增 `assurance/root_gate.py` 与 `orchestrator/assurance_root_commits.py`，
接在原 Store/Commit 后、runtime pools/accounting/workspace recovery 之前。
根 gate 存在且未获执行授权时，`__aenter__` 只开放管理实例，不运行原 startup_assembly；
新 `assurance_root_setup` 只装配当前认证管理 collaborator。已登记根即使状态文件丢失，
也由原根回执识别为必须检查；不能因省略 callback 退回旧运行分支。

- 原 `restore_offline` 写新 root id、QUARANTINED marker、原库库存及真实 context sidecar
  hash；旧 profile 无 context sidecar 时按备份实际库存处理，不假造一个必需文件。
  冷启动与授权准备读取库存并做 SQLite quick_check，精确读取仍核缺库和 sidecar hash。
- 原 Commit 写 native installation/read reauthorization receipt；固定 Principal/tenant
  来自 Facade，命令不能带 caller。未知非空根必须经过显式当前认证安装，origin 根据
  实际历史/其他文件区分；本片未安装 Host 自动接线，未把所有 legacy 根视为已启用。
- 库回执先提交，再原子写 0600 根状态文件；文件必须对应该根最新授权回执。中断窗口
  保持隔离；同命令/同 scope/当前 ACL 与 policy 一致时可幂等补文件。
- 每次重授权查询注入的当前 authority；每次 artifact 披露前与返回前重查，exact ref、
  principal、tenant、purpose、policy witness、root identity 和 TTL（最多24h）必须一致。
  使用原环境初始化/clock observation writer 记录管理模式的时间高水位，回拨不延长授权。
  根状态文件不在原备份库存里；数据库内历史授权单独存在不能打开新恢复根。
- Facade/MissionApi/TaskGraph/context/citation、原恢复/cycle/dispatch/Provider/tool 入口
  已加 gate。ActionExecutor 在原 begin_handoff 前和实际 worker 的 execute/lookup/observe
  前检查；TaskGraph followup 在 claim 前/handler 前检查。已产生的原 UNKNOWN/费用事实
  不被改写为成功/失败，也不因只读授权重新执行。

小范围编码接缝（不是正式验收）：
`PYTHONPATH=src /Users/denny/projects/simple-harness-sdk-h1h-impl/.venv/bin/python -B
/Users/denny/projects/simple_harness/.local-test-evidence/2026-09-23/assurance-integration/root-gate-seam.py`
（实际命令一行）。最新报告：Host ignored
`.local-test-evidence/2026-09-23/assurance-integration/root-gate-seam-20260923T033527264954.json`。
报告包含7个相关源文件 SHA-256；真实原 backup/restore、Orchestrator startup、Commit、CAS
与 Facade 路径，验证新根不继承 grant、回执后中断/幂等补文件、exact artifact 可读但不续跑、
跨 caller/tenant、缺库、当前撤权/policy/到期/时钟回拨拒绝。实际 worker gate 使用无副作用
fixture connector；post-claim race 使用明确 fixture claim，只证明对应调用边界。
Provider calls=0；未声称真实模型、外部 ACL、Host 或 UI 验收。

新增两模块完整 Ruff 通过；修改的原入口仅定点 F/E9 检查与导入/上述接缝；已有 TaskGraph
叠加源码仍有格式类 lint，不把此轮检查称全库 lint 通过。Terra 只读 review 发现的
Action connector 与 followup pump 两处旁路已修并定点复核关闭。

仍未完成：部署的当前认证/外部 ACL、native root installation 的真实 Host 入口；聚合图、
列表、历史 citation/context 目前继续要求 native 执行根，未声称可消费任意 read grant。
后续只能给可精确绑定的对象增加读取路径，不能按 mission_id 扩大披露范围。
CheckPolicy/CheckBinding 与 CheckSpec 完整合同的后继源码进展见下节；完整 validity
consumer 仍待接。较早 local-layer recorder 报告不替代后继完整合同和实际冻结 Scope 的证据。
BODY_WIRED、SDK48/继承66/OCC12/model12、隔离 Host/UI、默认 ON 与 ARCHITECTURE 收尾门均未关闭。

## 2026-09-23 续接：原检查映射、自动绑定与当前使用准备（主体 WIP）

### 原始生产链

- 新 `assurance/check_specs.py` 严格实现完整 CheckSpec v1。schema、scope-rule 文档为实际
  包内文件的字节 SHA-256 pin；部署摘要覆盖真实算法/recorder/导入代码及合同文件。
  四份批准 kit 的 common/check-spec/local-receipt/check-binding schema 原样放入包目录；
  输入 schema 与层级 scope-rule 从实际 freeze 输入和原 format/rule 算法定义。
- local receipt 删除冻结 schema 不允许的额外 `checker_implementation_hash` 字段；算法
  身份通过 exact CheckSpec 的 implementation_hash 核对，保留原 recorder hash。
  原 format/rule 调用按 monotonic duration 核 CheckSpec 上限；返回超时保留实际输出但
  execution_state=ERROR/verdict=UNKNOWN，不宣称同步函数已被硬中止。
- 新 `assurance/check_bindings.py` 实现 CheckBinding v2 严格合同。
  `orchestrator/assurance_check_import.py` 核原 Event、Result、冻结 Attempt 输入/Scope、
  input manifest、registry、原 import receipt、实际 output Artifact metadata；原
  local receipt 导入事务内自动写 CheckBinding＋原 Commit receipt＋原 Event。
  精确重放不重复；不能把旧 Result 重绑到新 plan Scope；executor 来源仍未实装。
- `approve_assurance_check_policy` 从 Facade 固定 Principal/tenant，经原 Commit 同一 UoW
  写批准 receipt、policy 与 human event；映射必须对应原 Requirements 和当前 Scope。
  新 `read_task_check_policy_projection` 复用原 scope/criteria，允许在 Critic 或 Result
  通过前批准；派生准则保留 Task 原 verification_policy，不用假 LayerOutcome PASS。
- CHECKED 保留全部原 required checks，找不到部署的 checker 维持 UNRESOLVED。
  SEMANTIC 只接受原显式 SEMANTIC 且无 required checks 的准则。独立 review 的 P1
  “空检查映射的非语义准则可任意填 format check”已修复；没有原映射保持 UNRESOLVED。
  当前 registry 仅 format/rule；批准 OR 等价替代和其他 native/executor checker 尚待接。

### 当前使用接缝

新增 `orchestrator/assurance_check_use.py`，将历史 local assertion 准备为当前用途的
内部 `PreparedCheckUse`，只证明注册层断言；不是证书，也未接正式 review collector。

- 原导入器拆出 `read_local_check_binding_locked`，registry 增加严格只读 receipt 路径。
  当前使用查询不执行算法、不自动注册、不补写 binding 或回执。
- 一致读核 exact binding 原回执、Result/Scope/Task/Requirements、原 local run 和 manifest，
  穷尽共用来源库存的全部候选集合（包含未引用分支），固定 epochs/clock/root/consumer。
  注意 OCC task pin 的 contract_hash 与完整 task semantic binding hash 是不同域；
  两者由原 Scope reader 关联，读集记录实际 binding hash，不把它们冒充同一 ref。
- 事务外经原 pin/CAS reader 读取全部 manifest artifact 与实际断言 output；核完整 bytes，
  相同准备预算累计扣减。每项依赖查询当前 authority，捕获 ACCESS/POLICY/expiry，权限
  来源仍是明确的部署 collaborator，尚未声称真实 Host 外部 ACL 装配。
- 返回前和原消费事务都核身份、当前 root、同一 adapter/environment、时钟/epoch、精确
  metadata、有效 pin 与当前权限。consume 只做有界对象复核，不扫描完整候选集合/求 closure。
  此处标准化 CheckResult 的 source_valid 来自实际读路径，不能从 Host/model 布尔值导入。
- 到期使用半开区间；所有依赖取最早 authority/binding deadline；单次 local check 不能
  用作 MAINTAIN 连续保障。完整 validity 仍须原规则准入、正负 support/clean closure，
  不因这一步返回 PASS 就签发 USABLE 或推进 terminal。
- 独立 review 的第二个 P1（prepare 后换部署仍可用旧 PASS）已修复并复核：consume
  必须比较 binding.adapter_ref/recorder_hash 与 environment_hash，变化返回 RECHECK_REQUIRED。

### 窄检查与剩余门

只运行本切面编码接缝，没有批量测试。脚本/JSON 原始证据均在 Host ignored
`.local-test-evidence/2026-09-23/assurance-integration/`：

- `check-binding-seam-20260923T044250603641.json`：完整 schemas 对 actual input/CheckSpec/
  local receipt/Binding 验证；原计划/冻结 Scope/Attempt/Result writer；实际 format 算法；
  自动 binding/重放；真实 checker 异常与模拟 monotonic 超时保持 UNKNOWN；原 SEMANTIC
  批准和未映射 nonsemantic 拒绝。后继 read-only 重构已改部分 source bytes，此为对应版本证据。
- `check-use-seam-20260923T045354934565.json`：后继当前源码的实际 Store/Scope/format/CAS
  接缝；8 个相关文件 SHA-256；准备前后 SQLite total_changes 不变；当前 normalized grade；
  caller/adapter 切换、撤权 witness 改变、expiry 边界、损坏 CAS、未引用的新 assertion 分支
  导致重核均被拒；MAINTAIN 不被点状检查放行。root/authority/pin producer 为显式 fixture，
  不算真实模型/Host/UI/外部 authority 验收。检查器超时为模拟计时，不是等待120秒。
- 旧 `local-check-pin-seam.py` 的最小 Task/Attempt fixture 不含原冻结 completion scope，
  不能再用旧 PASS 代替当前自动绑定链。pin 读取独立历史结论也不等于新链整体通过。

执行方式（单行）：`PYTHONPATH=src /Users/denny/projects/simple-harness-sdk-h1h-impl/.venv/bin/python -B /Users/denny/projects/simple_harness/.local-test-evidence/2026-09-23/assurance-integration/check-use-seam.py`。

剩余主体：正式 policy/review builder/collector 消费、executor receipt importer、完整 validity
证书签发/消费、四 consumer 装配、当前 Requirements/authority/reconciliation 的真实部署、
原 OCC/acceptance/terminal guards、隔离 Host handler/UI。BODY_WIRED、SDK48/继承66/OCC12/
model12 与原生验收、默认 ON、ARCHITECTURE 完成回写仍开放；未合并、未构建/安装 wheel。

## 磁盘恢复

此前 shell 临时文件和任务锁均实际 ENOSPC，三方接入未发生。
用户明确授权后执行 `uv cache clean --cache-dir /Users/denny/.cache/uv`，未加 --force；
退出 0，移除 71080 个缓存文件（工具统计 1.8 GiB），系统实际可用约 637–638 MiB，
临时文件写/读/删除恢复。未删除源码、venv、数据库或原始证据。
2026-09-23 本次续接实测可用 82 GiB；上述 637–638 MiB 是清理当时的历史读数。
本轮未继续删文件，未复制依赖或构建大包。

## 2026-09-23 审查 transport / runtime source / official import 续写

用户已明确停用 plan-task；后继直接由主 Agent 开发。未恢复旧评测子代理。

新增实际代码：

- `assurance/reviews.py` 严格 ReviewBinding / Invocation / RecordBinding side 合同；
  三份批准 schema 原字节进入 SDK assets，公共 TypedRef/ReviewRecord V1 合同未改。
- 原 Commit 的 invocation writer 将原 ReviewPackage、原 service reserve、intent、
  ReservationLinked、side binding 和 ensure receipt 放同一个 savepoint UoW；
  原派发三处新 profile 边界接当前 handoff validator，缺真实装配就拒绝派发。
- 原 plan collector 分派 Assurance：实际 AgentBridge/runtime UoW 读取持久 Agent/Turn/
  Result/Provider invocation/ContextSelection/Journal，先保存 raw CAS/原 source 和
  TurnImported。曝光只接受最终 Provider request 实际含有的原消息，不推定已看到。
  使用原 Provider codec 核对显式 null 与 Agent 消息省略可选字段的原存储差异；
  未修改请求或输出字节。原 raw 超过审查 codec 上限仍先保存，拒绝其审查解释。
- 精确实际 response model 与原期待模型核对；格式无效、曝光不可得、执行失败分开。
  初始 disclosure 绑定实际 input/message/turn；真实 readonly tool 的追加披露尚未接通。
- 原 HtnStore 的 official writer 增加持久 lane guard；Assurance 新 Mission 不接受
  裸 verdict。新 importer 核对实际分类 receipt/TurnImported/输入 manifest/完整披露链，
  CAS/pin/原 check source、当前权限及 epoch，再写原 official Record + evidence manifest +
  RecordBinding + 原导入 receipt/Event。同包唯一 official；精确重放复用原 receipt。
- 旧 CriterionOutcome V1 拒绝 SEMANTIC PASS+NOT_RUN。当前保持原 codec 字节，旧字段
  使用 UNKNOWN/NOT_RUN 投影，模型 grade/effective grade/check gate 留在严格绑定的原
  input manifest。**接受端仍待改为消费此投影及独立当前 UseCertificate；不能声称接受闭环。**
- 新 REVIEW consumer 接现有 WorkStore claim/prepare/effect+ACK；无模型 await、无新池。
  其 pending 源为实际分类 Event，现可准备 official 导入并从原 receipt 冷重放。
  四 consumer 整体部署尚未装配；源变更 requeue、错误分类与所有用途完整接入仍待完成。
- 同包最多一次格式修复 wrapper 和 REVIEW 格式分支已写：原失败必须确定结束，
  原费用 UNKNOWN 阻断，使用相同固定材料和新原 reserve。第二次格式失败留具名耗尽回执。
  该后继分支当前仅静态检查，尚未通过实际两调用窄链路。

单条编码接缝（非批量验收）使用真实 Store/Scope/预算/Agent kernel/Provider ledger/
Journal/collector/HtnStore/WorkStore：一个 scripted Provider 调用，1 个 official，52 个
完整查询集合；重复 ensure、重复收包、无凭据裸 official 写入拒绝、正式导入重放、
REVIEW claim→effect+ACK 与历史 receipt 读取通过。Requirements/authority 为显式 fixture，
没有实际模型、Acceptance/UseCertificate、Host/native UI 或完整六用途验收。
来源 hash 和证据在 Host ignored：
`.local-test-evidence/2026-09-23/assurance-integration/review-import-seam-20260923T053725094142.json`。
随后格式修复代码有变化，该报告仅绑定其记录的旧源码 hash，不继承为当前整体验收。

BODY_WIRED 仍未通过。仍须完成六原 builder、真实当前 Validity evaluator/UseCertificate、
接受与终态所有 writer、executor checks、只读证据工具、四 consumer 部署和恢复入口、
隔离 Host/UI、最终统一验收和架构事实回写。未 merge、build wheel、替换 Host 或默认启用。

## 2026-09-23 原 Critic runner / current handoff / physical settlement 续写

仍是隔离 candidate 的主体 WIP，不是 BODY_WIRED、功能验收或 Host 集成通过。
用户明确禁止 plan-task；没有启动该 skill、子代理开发、大批量测试或进一步磁盘清理。

本段真实接线：

- `read_task_content_candidate` 从原 frozen Result/Attempt/Scope 和审查前 criterion
  projection 构造内容候选；不要求预先 VERIFIED/PASS。原 leaf package 增加不预写选项，
  由 `ensure_task_content_review` 同原 transport 写入。缺批准的 CheckPolicy 仍拒绝。
- `AssuranceReviewRuntime` 接原 `_run_critic`、`_await_service_turn`、`_dispatch`、
  collector 和 REVIEW WorkStore。先导入实际原回复/曝光/official record，才转 CriticVerdict；
  INCONCLUSIVE 仍为未完成验证。原 V1 CriterionOutcome 的 UNKNOWN/NOT_RUN 未伪造为检查成功。
  exact official replay 不 reroute、不再次预留、不再调用 Provider；provenance 读原 invocation。
- TASK_CONTENT 改用原 `critic` kind；传原 Result 的实际 attempt_id，复用首次 Critic 的
  protected tail、system hold、Selection 账户和期限。Selection 的 subject 识别增加
  严格原 prepared binding/receipt/Result 身份核对，不靠 Assurance 字符串前缀直接放行。
- `AssuranceReviewHandoff` 在原 create/submit/format-repair 边界核对真实 root、固定 consumer、
  当前权限、包/Scope、实际池 ContextPolicy/tokenizer/model、模板 hash、原材料字节及活 pin。
  不继承旧 verify-workspace 的工具权；readonly evidence tool 尚未实现，当前不授予工具。
  这是披露 gate，不是接受/执行/完成 Mission 的 UseCertificate。
- MODEL_IDENTITY_MISMATCH / TURN_FAILED / EXPOSURE_UNAVAILABLE 不走格式修复。原 findings
  保存在 evidence manifest 并投影到验证层；冷恢复复用实际分类与模型回执。
- 来源历史 reader 进一步核对 prepared/ensure receipt、row mirror、实际 reserve/intent 和
  原输入身份。原接受/Resolution 共用的 Review binding 检查拒绝无来源的 assured official。
- REVIEW consumer 对其他已 official 的迟到 turn 或已停止 subject 记录具名 LateTurn；
  official 的最终 writer 再检查 subject 仍活动，防准备/提交间取消竞态。
  当前 ACL/CAS/epoch 不可得保留持久重试，不误记成不可恢复的模型解释错误。
- `AssuranceEvidenceChanged` 唤醒相同未完成 review-import 工作，仍引用原分类 receipt，
  不重置累计次数/起始时间。枚举限额溢出明确拒绝，不截断后当完整。
- Tick 单项 Assurance/BudgetError 进入原工作项累计重试，不打断后续 consumer 摄取。
  最多 32 次/300 秒后 MANUAL_REQUIRED 的限制保留。
- 原未知 Provider 等待路径写去敏、幂等 reconciliation receipt；原
  `rehandoff_service_intent` 同时拒绝 assured Mission 重发。不存在靠换 executor 获得新调用。
- `AssuranceSettlement` 复用原完整 execution history reader（Agent/Turn/Provider/effect/
  actual imported usage），在原 `_settle_subject` 内再验 physical/accounting settled。
  新 profile 的 known-only 请求仍用严格 ledger settlement；缺部署 reader 保留 hold。
  late accounting 扩展为处理 assured unguarded/未实际 materialize 的原 intent，账户从
  exact ReviewPackage 的原预算责任读取；没有另开账户。
- 取消/失败 terminal preparation 不再提前清 assured reserve；AGENT_CREATED 丢 submit
  回执窗口仍保留实际采集入口。收不到实际结果且物理责任未收敛时，不关闭原 late-result
  采集身份；迟到的真实 review raw 仍通过原 collector 持久化。

本段仅具体编码接口检查：

1. `content-review-seam-20260923T060837827880.json`：原内容 builder→kernel→collector→
   official+REVIEW ACK，含重复调用；该版本后又有结算与恢复改动。
2. `critic-runner-seam-20260923T061013739923.json`、
   `critic-runner-seam-20260923T061407068231.json`：原 `_run_critic`→原 dispatcher→实际
   AgentRuntime/Store/Provider ledger→collector→REVIEW WorkStore→official→CriticVerdict，
   重放仅 1 次 ScriptedProvider 调用。fixture routing/ACL/lease 与单 consumer pump
   明确列在报告；不是完整四 consumer 部署、真实模型、Acceptance、原生 UI。
   报告内 source_hashes 只覆盖其列出的源码快照；后继 source-wakeup、accounting 和
   after-stop 改动不继承该 PASS。
3. 本段新增/修改文件的 Ruff F/E9 与 git diff --check 通过，未启动 suites。

所有原始证据在 Host ignored `.local-test-evidence/2026-09-23/assurance-integration/`。
没有 commit/merge、构建或替换 Host wheel、默认 ON 或完成态 ARCHITECTURE 回写。

明确剩余：完整 current Validity evaluator/UseCertificate 与 scoped 接受端（SEMANTIC
真实 grade + 原 OCC）；其余五 purpose 的原 builder/consumer 接入（METHOD_PLAN 当前
没有可伪造的 completion_scope，必须正式支持批准的 purpose scope）；executor check
receipt importer；readonly evidence tools/真实追加曝光；pin BOUND/release 生命周期；
四 consumer 与 factory/Requirements/当前 authority/startup reconciliation 的真实部署；
所有 Input/Context/Scope/use/terminal writers 的完整接线与 closeout；隔离 Host UI；
最终集中验收/默认 ON/架构事实回写。全局 BODY_WIRED 仍 OPEN。

本段后继继续：

- Assurance 原 `_import_usage` 改用完整实际 execution reader，避免旧 unguarded 路径
  将缺失 token usage 补成 0。格式修复不仅检查 imported UNKNOWN，还必须由同一
  physical/accounting reader 确认原调用完整收敛；缺 actual source 不申请第二次预留。
- `critic-format-repair-seam-20260923T062129925437.json` 和后继 pin 接线后的
  `critic-format-repair-seam-20260923T062457443924.json`：一个连续定点接口案例，
  ScriptedProvider 第一次 malformed、第二次合法；原 runner/dispatcher/collector/
  REVIEW consumer 发起且仅发起两次调用，两个实际 reserve 结算，唯一 official，
  再次调用无第三次 Provider 请求。用量为 fixture 明示 10+10 tokens/调用，非真实模型测量；
  routing/ACL/lease、单 consumer pump 仍是明确 fixture。用于解决第二 invocation 的
  原账户/来源/重放接口阻塞，不是集中验收。
- pin 现在于原 package+binding UoW 内 PREPARING→BOUND；后来新增 raw/check pins 也
  核对绑定后转 BOUND。初始材料/CAS/transport 准备失败且包尚未成立时释放 PREPARING。
  RELEASED 不复活；重试使用新的 pin identity，单对象最多 32 个准备历史，歧义拒绝。
  handoff 精确查当前 BOUND pin，并验证原 transition receipt。正式历史的 BOUND pin
  继续保留；当前应用没有 CAS GC，不增加定时删除。启动时孤儿准备的恢复尚待整体验收。

后继集中阶段必须验证未知费用/迟到 turn、源变化重核、pin release/reacquire 的完整反例。
不能将上述脚本 Provider 的局部 PASS 当成 BODY_WIRED 或功能交付完成。
