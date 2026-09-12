# P3.4 readiness draft：证据驱动的动态搜索与跨分支组合

状态：**DRAFT / 未执行 / 未验收**。初稿 2026-09-12，设计细化更新 2026-09-13；这不是批准后的接口冻结稿，也不将既有测试源码计作本轮 PASS。§9 给出待独立审查的具体 schema/API/事务与 oracle，优先于前文概念字段示例。

## 1. 范围、事实源和基线

- 最新用户授权：P3.3 → P3.4 → P3.5；采用**源码 UI 测试，暂停打包，不含 P3.6**。本次只准备 P3.4，不改生产、测试或 P3.3 文档，不运行 pytest、模型、构建或提交。
- 原始要求：[Host Phase3 plan §6、P3.4 八 AC](../../../../simple_harness/plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md)。原 AC 全部保留，下面只将它们拆成可执行 oracle，不重新定义完成门。
- 设计依据：[完整设计](../../../../simple_harness/plans/taskSys2/agent-orchestration-layer-complete-design.md) §6–11、15、17、18.6、19–20；[理论目录](../../../../simple_harness/plans/taskSys2/agent-orchestration-theory/README.md) T02 §5/8–10，T03 §4–6/11，T04 §7/9–10，T05 §7–10，T07 §9，T08 §9–10，T10 §5–6/14。
- 理论边界：角色是搜索偏置；Judge 选择候选，Synthesizer 形成新的综合候选；路线、执行依赖、知识血缘是不同关系；模型提出方案，程序决定预算、权限、版本和正式 Commit。没有必要增加 Proof-DAG 数据库、MCTS、学习型调度器或第二套 Task 状态机。
- 初稿检查时 SDK HEAD `a5c8fca`，Host HEAD `0e19fb48`；当时工作树已有主线程 P3.3 文档及测试变更，不能称为 clean。该历史观察不代替启动 P3.4 实施时锁定的新基线。
- 主线程已报告 SDK clean `a5c8fca` 编排全量 `1302 passed / 8 skipped / 487.75s`，本次未重跑；8 个历史模型 skip 不算真实 Provider 证明。P3.3 的源码 UI/价值门与全 Mission 审计按其原清单继续，不能因进入 P3.4 准备而结清。此前 frozen 启动失败也不改写成通过；打包暂停后不再把重新构建当本轮前置。
- 执行纲要中原 wheel/Host 重钉步骤与最新指示冲突时，采用最新源码验证方式；须记录实际加载的 SDK 路径/提交及 Host 路径/提交，不能关闭 exact identity 检查来掩盖使用旧 SDK。源码加载接缝由主线程统一明确。
- 2026-09-13 补充：主已实现并验证 Host 显式 `editable-source` 身份，来源 snapshot/实际 import 与 baseline wheel 分开；不再将该接缝写成待设计。主报告 N1 v3 已经真实 UI 提交，随后因大结果只有 preview、后文不能回读而取消并退出，**不是 N1 业务验收通过**。本轮仅细化文档，不改变正在修复的生产/测试；后续实施重新锁定实际源码快照。
- §9 收尾时主报告 SDK `e346689` / Host `e690bdcf` 已提交，P3.3/旧 recovery 兼容 `g-p33-compat-v7` 874 passed / 35.70s；两批 Critic lifecycle/recovery 共10控制通过，均引用主 runner/journal而非本任务执行。生产为 N1v4 冻结，本任务仅编辑 P3.4/P3.5 草案、不提交；N1v4 即将开始不等于业务门已通过。

以下源码路径以 SDK 根目录为基准；带 `Host:` 的路径位于相邻 `simple_harness` 仓库。

## 2. 已有组件与真实差距

| 能力 | 当前已核实事实 | P3.4 必须补的最小部分 |
|---|---|---|
| 动态图、Manager | `graph/changes.py` 已有 `TaskGraphChange`、严格操作解析、深度/提案数/替换链/预算检查。`CommitService.commit_graph_change` 已原子提交、幂等、拒绝重叠旧 base，允许不相交 rebase。`event_handler._request_management` 已冻结触发结果、相关子图、反馈、剩余预算与 intent | 不重写 GraphPatch。补实际搜索场景的反馈分类/证据绑定、进展去重，以及新复用/选择记录和既有图链的组合验证 |
| 角色指令 | `runtime/role_templates.py` 已注册 explorer/exploiter/simplifier/connector/failure_analyst 派生角色；`role_for_task` 与 `_template` 已实际选角色和冻结领域版本 | 原计划 G05 不能写成“Explorer 无 prompt”。缺口在 `context_builder.ENABLED_TEMPLATES` 仍不含 explorer，`build_worker_package` 对其回退 worker 可见性。需要显式角色可见性矩阵，证明实际 Provider 输入差异 |
| 候选资料获取 | `_gather_knowledge` 已查询 PROPOSED/UNDER_REVIEW/SUPPORTED 与 REJECTED；`_knowledge_section` 已有 critic/explorer 候选区 | 不缺查询入口；需在 Explorer 接通后做有界相关性选择，避免把全 Mission 候选无上限填进必需合同。低等级材料不能借角色标签晋级 |
| Context 基础设施 | `simple_harness/agents/ports.py` 已支持 tokenizer/context_policy/embedding；`agents/context/budget.py` 已有工作窗口预算；`context/port.py` 已记录 selected_seqs、dropped_ranges、policy_hash、tokenizer_fingerprint，并绑定实际 provider_request_id/request_hash。每次请求有预算 guard，必需内容过大拒绝；冻结请求恢复已有测试 | `RuntimeProfile` 尚无 `context_profile_ref`；`assemble_orchestrator_runtime` 未传上述三个端口，当前使用 BaseAgent 默认。补部署配置解析、冻结身份、跨层可追踪接线；复用已有 receipt，不另造一套 request hash |
| 历史不升格 | `simple_harness/agents/runtime.py::_RecallAdapter` 已由 P3.2 修成带 provenance 的 USER `recalled_history` 数据块，转义闭合标签、带“不作为指令”提示，且不写回 Journal | 原计划 R13 已修，不能列成待修 SYSTEM 漏洞。P3.4 加实际编排角色/长回合/恢复链验证；不得改成假 tool result 或无边界用户指令 |
| 多 Attempt | `OrchestratorConfig.candidates_per_task`、allocator 和 `create_attempt` 已支持多个候选；全部占同一 Mission/Task 预算。`accept_result` 首个 PASS 完成 Task 并 SUPERSEDE 开放兄弟 | 尚无 `planning/candidate_selection.py`，也无 `COMPARE_THEN_SYNTHESIZE` 选择政策/记录/等待关闭路径。需分离候选验证与最终接受，仅显式批准模式延迟正式接受；旧 FIRST_VERIFIED 保持 |
| 综合与知识 | `planning/manager.py::synthesis_task` 已建固定综合 Task，依赖 Planner 图叶子；知识分级、去重、来源失效检查、summary 过滤、血缘已存在 | 固定综合不等于同 Task 候选比较。补“输入片段选择理由 → 新 CandidateArtifact → 再验收 → 一次 Commit”，不能直接合并 PASS 或盲拷贝文件 |
| 失败和迟到 | `record_outcome` 保存非候选失败提案但将其 Claim 置 REJECTED；`fail_result` 保留失败事实，不能据其整体结果形成可靠片段。`record_late_result` 仅记录 summary/artifact 路径的历史拒绝事件 | 独立局部验收/复用入口未闭合。历史路径本身不证明可读字节；需要先固定原片段、CAS/引用及原结果身份，再生成新受限验证，不改原失败/迟到结果 |
| 有界探索 | `max_graph_depth`、max_proposals/max_supersede_chain、max_manager_rounds、no_progress_limit、reserve/settle、背压、老化优先已存在。allocator 当前 progress_signal 主要来自尝试/失败数 | 复用硬门；对新候选/片段/重复路线建立稳定计数，角色更名和重复图改动不能重置停滞/额度。不是新增全套预算系统 |
| Host 可解释性 | `Host:backend/deskpet/orchestration/projection.py` 已投影 Tasks/Attempts/Results/Artifact/审批与 Mission policy，但 `graph_changes` 仅数量；`MissionsView.tsx` 已显示结果与各验证层；SDK `observability/graph_history.py` 有图历史 | 需要展示有依据的改图、被停路线、片段复用、未采纳候选及成本。不是重建 Mission 页面；仅计数和角色颜色不足以满足 §6.1 |

P3.3 的 source/CAS、领域冻结、完整引用、assessment、INSUFFICIENT、人工冲突与当前性门都是本片依赖。任何复用不能绕开它们；来源归属仍不等于世界事实。

## 3. 推荐三个完整功能切片

每片采用“先 actual-runtime oracle 红 → 生产闭环 → 独立审查 → 定向/兼容回归 → 对应源码 UI”顺序；接口表只是提案。不要先提交一层孤立模型/数据库而称为功能完成。主线程安排唯一测试 runner。

### A：角色真实接收到受预算约束的证据包（A01、A06、A07）

用户可以从一次新 Task 的详情看见实际角色、工作配置、采用/未采用资料及请求身份；Explorer 得到明确标注的候选线索，Verifier 得到独立证据。Worker 的权限仍来自合同。

1. 在 RuntimeProfile 增加可序列化 `context_profile_ref`，解析成不可变的工作配置。装配将实际 tokenizer、ContextPolicy、embedding/检索设置传入既有端口。rerank 若只是现有确定性排序，明确记录算法版本；无 embedding 则记录 lexical-only，不能声称已部署向量/rerank 模型。
2. 新 Context 配置记录模型部署/任务类别、tokenizer fingerprint、工作输入上限、模型输入限制、总窗口、输出预留、安全余量及检索版本。最终输入上限采用原计划 min 公式。工具 schema/协议开销也计入；若既有 render_slack 非零，纳入安全余量，不能将允许超出的 slack 写成严格未超预算。
3. 当前合同/原 Mission 准则、硬约束、直接依赖与真实未决状态不可静默丢弃。可选候选资料在组包前按版本化策略筛选；不足时明确拒绝/请求拆资料，不能截掉否定条件。工具多轮仍由 BaseAgent 对每次 Provider 请求复核。
4. 开启 Explorer 可见性，保留 Critic 反证/失败资料；Connector 得到可复用条目及条件，而非原始全 Journal。沿用 P3.3 的 code/doc 等级差异；不能把 doc 的所有“verified attribution”写成可信世界结论。
5. 上层 intent 冻结 context 配置与资料选择身份；下层 selection/request receipt 以实际 run/turn/request 关联。新请求允许新 selection；恢复已冻结请求不得再次召回或按新配置重计后替换原请求。
6. Host 详情增只读 Context evidence 子区：profile/ref、tokenizer、预算、选择原因、被排除项、实际 request hash。默认不公开敏感原请求文本；证据本地脱敏保存。

先行 oracle（建议新 `tests/orchestrator/p34/test_role_context_runtime.py`）：

- 同 Mission 相同基础资料，真实 SDK dispatch Explorer/Exploiter/Connector/Critic/Verifier；检查 Provider 接收到的 instructions/数据区，而非只检查 role 字段。注入 PROPOSED/REJECTED/过期来源和历史“提升权限”文本，确认标记、隔离、工具拒绝与等级均正确。
- 两 RuntimeProfile 配两种可区分 tokenizer 与不同预算，实际到 `invoke` 的每条请求分别计数并核 selection/intent/request；长 tool 往返后的新请求也计数。必需合同超限时零 Provider 调用，可选候选溢出不挤掉必需项。
- 冻结请求后重启，配置注册表/检索索引变化不改其 bytes/hash，也不新增费用；新 Task 使用新配置。无凭据、不可用 embedding、未知 profile、坏类型/NaN/负预算分别显式处理。

### B：失败路线经局部修复，可靠片段独立回收（A02、A03、A05、A08）

用户看到 A/D 已交付分支保持，B 缺少前置 E，被 Manager 以真实依据替换为 B2；失败 B 中一个可独立验证的片段被新验证接受并由另一分支复用。B 的失败和实际费用不变。

1. 沿现有 Worker Proposal → Manager intent → TaskGraphChange → Commit，使用程序已知的 verification/provider/permission 错误；模型的原因分类仅是建议，不能解除权限/预算或确定性失败。对缺前置、实现错误、方法不适用、权限不足、未知、无进展给出可区分反馈。
2. 片段提案绑定原 Mission/Task/Attempt/Result、原 Claim revision、精确 artifact hash/locator 或 SourceCitation、适用条件及独立准则。只收真实存储中的片段，不接受模型自己填写 grade/receipt；原失败 envelope 不重写。
3. 最小执行选择：通过既有图提交建立一个**有明确局部合同的验证 Task**，使用既有 Attempt/Verifier/Commit。它用 provenance 引用失败来源，不把失败 Task 当必须 COMPLETED 的执行依赖。不得把原 Task 的全部准则偷换为局部准则并接受原 Task；也不能凭模型随意减少局部证据要求。
4. 验证输入从固定 CAS 片段物化；部分代码产物缺少运行依赖不能验过，doc 引用仍需原 P3.3 resolver/assessment/current gate。新输出有新 IDs 与验证记录，记录源自失败片段；只该范围可复用。原 rejected Claim 不原地改状态。
5. 若迟到结果从未被安全收集，仅有路径事件，先作为 unverified 线索；有界地固定可核验快照再走上述新 Task。不可回读现在的同名文件冒充原输出。找不到原字节则明确不可复用，原迟到拒绝不变。
6. 新验证 Task 及 Manager/Verifier 占原 Mission 剩余预算，受提案数、图深、尝试数、轮次及停滞门约束。相同原片段/准则/版本提案幂等；无新证据的角色改名、相同内容改述不算进展。
7. Host 改图详情从真实 graph_history/receipt 展示 old/new version、trigger/result、affected/unaffected、替换和片段新旧验证身份。不得把知识血缘画成执行依赖，也不把 failed/source Task 标成完成。

先行 oracle（建议 `test_local_repair_runtime.py`、`test_fragment_reuse_runtime.py`）：

- 实际 Manager 增 E、替换 B→B2、执行 E/B2；A/D 的 result_id、artifact hash、Attempt 数与费用不变。注入并发旧 Proposal：重叠拒绝，不相交依旧允许有记录 rebase；不能将“旧 Proposal 均拒绝”误当兼容。
- 原 Attempt 整体确定性 FAIL，但一个局部 Claim 具有完整独立证据：真实新验证 PASS→新 Knowledge→另一分支实际 read/context/used_knowledge。原失败状态/验证历史完全保留。反例：错误片段、缺字节、跨 Mission、改 hash、截掉前提、撤销来源均不可采用；ERROR 不降成普通未知。
- 重启发生在“局部提案记录后/新验证 Task 创建后/验证后”三处：新 Task/费用/投影各一次，重复/迟到内容不覆盖当前图、不自动进入事实区。无已收集字节的迟到候选只能显示不可复用，不能为了正例捏造 receipt。
- 持续重复/无进展、图深已满、预算仅够收尾、尝试数耗尽均明确停止；不能以 salvage 或换角色继续无限派生。硬失败保持硬失败。

### C：批准的多候选比较、组合与最终一次交付（A04，组合 A01–A08）

用户显式选择已批准的比较政策，在固定截止内查看候选理由；系统保留互补片段，生成新 C 并独立验收。快到达的 A 不抢先正式交付，未选 B 不成为最终必须完成的依赖。

1. 新增计划指定 `planning/candidate_selection.py`，在已有 Attempt 之上记录候选、结果证据、选择模式、上限、截止与决定。默认仍 FIRST_VERIFIED。`candidates_per_task > 1` 本身不意味着批准了新模式。
2. 扩展现有 policy 注册/验证/审批及 Mission 冻结，不创建旁路 origin 权限系统。Host 当前 policy 入口只读；为本片提供“引用已批准 policy”的最小创建参数/选择控件，并在服务端核真实 policy binding。若需要用户新批准，复用既有审批机制，不接受模型自称 approved。
3. COMPARE 模式把验证完的候选放进有界选择集合，在此阶段不完成 Task、不 supersede 兄弟、不发布 Action、不将整个结果直接写成正式可用 Knowledge。候选验证证据可读但不是最终交付事实。
4. 确定性资格过滤先于模型比较：准则/输出绑定、必需层、来源当前性、冲突/依赖有效性。模型排序和合成理由不能覆盖 FAIL/ERROR。原 v4 doc 当前性与 code 实测等级保持。
5. 选择一个合格候选可进入现有最终接受；选择多个片段则创建新的 synthesis 执行 intent/Attempt，仍属于同一 Task 的候选，带冻结输入清单与实际合成角色。不要直接复用固定跨 Task synthesis 的“依赖全部叶子”来表达同 Task 比较，避免自依赖/等待失败候选。
6. 新 Candidate C 在隔离 workspace 生成新 Artifact，再跑完整目标合同及适用的 Mission 判断。组合 lineage 取实际用到的原条目/版本；不存在“输入都有 PASS 所以 C PASS”。同路径文件碰撞必须由合成明确解决，不按字典遍历或到达顺序覆盖。
7. 截止含最大候选数、原预算、实际时限及停滞；持久化截止值，重启不重新起表。到期若已有满足完整合同的候选，可按预先批准的确定性规则选取；无合格完整候选则失败/已有合法人工出口，不能以比较耗尽为由接受不完整输出。为最终验收预留预算；预留不足时不继续展开。
8. 正式接受仍一个事务；决定重复/并发、崩溃恢复、兄弟迟到都不能双重交付/双写 Knowledge/重复发布。未选候选成本保留，取消后仍结算未知用量。
9. Host 显示候选集合、保留/舍弃理由、被采用片段、C 的独立验证及实际交付清单，关联原 Task/Attempt/Result/receipt。包括 loading/empty/error、长列表与冷重开；页面不以最高 confidence 或绿色颜色推导正式状态。

先行 oracle（建议 `test_candidate_selection_runtime.py`、`test_search_policy_compatibility.py`）：

- 同 Task 两候选：FIRST_VERIFIED 原首 PASS 即完成保持；COMPARE 中 A 先 PASS 不提前完成/发布，B 到齐后产生 C；C 有新的 Provider 请求、artifact hash、verification rows，最终一次 Commit。
- 互补 A/B，组合结果故意遗漏一个硬条件：C 必须 FAIL，即使 A/B 的局部片段均通过。成功对照要核实际采用片段与全目标，不能只断言“调用了 Synthesizer”。
- 输入/到达顺序重排、分数并列、重复候选：最终选择遵循冻结 tie-break，语义重复不算独立支持。不要求真实模型两次生成 bytes 相同；要求冻结决定恢复相同。
- 候选来源在验证与最终接受之间撤销，或旧图改掉相关合同：拒绝旧证据/重新验证；独立有效替代可采用。原历史 receipt 不变。
- 等不到另一候选、Provider ERROR、预算耗尽、选择记录后崩溃、最终 Commit 前后崩溃：有界收敛、全部费用保留、无新 deadline、无双重 Knowledge/Action。综合过程不绕过必要 human approval。

## 4. 建议的最小接口边界（实施前独立裁决）

| 接缝 | 提案 | 约束 |
|---|---|---|
| Runtime 工作配置 | `RuntimeProfile.context_profile_ref: str | None`；装配 `context_profiles: Mapping[str, ResolvedContextProfile]` | snapshot 只保存可序列化 ref/version/fingerprint/limits，不能存 provider/key/可变对象。未知新 ref 拒绝；None 是明确 legacy 行为而非新功能成功 |
| Context 查询 | 复用 KnowledgeContext、rank、BaseAgent ContextPolicy/selection receipt；增加版本化 role visibility 与 candidate selection metadata | 上层 package hash 与实际 Provider request hash 都保留，明确二者不同；render/source/role 指令变更使用新版本，不原版改已发布 prompt |
| 局部片段提案 | §9 的 `FragmentProposalV1(origin, criterion_ids, claim_refs, material_refs)`；不开放任意 `criteria` 文本；决定性 id 由系统对规范数据计算 | 原 Task 合同快照/指纹、1基 criterion IDs、独立新验证 Task 及旧新准则映射均由程序绑定。验证结果绝非模型字段 |
| 候选选择 | `SelectionPolicyV1(mode, max_candidates, deadline_seconds, tie_break, synthesis_limit)`；`SelectionDecisionV1` 冻结 policy id、task semantic revision、候选 result/artifact/receipt identities、采用/拒绝理由、remaining-budget snapshot | 预算 snapshot 只解释决定，真正支出由 ledger 当前余额原子控制。禁止用模型返回的余额授权；模式限制和批准事实以实际注册 policy 为准 |
| 正式写入 | Commit Service 增有界选择/片段提案记录与完成入口；复用 DispatchIntent、TaskGraphChange、Attempt、verification、accept_result | 新事件同片补 replay/snapshot/幂等；不能靠测试把 unknown events 放过。现有无新记录的路径不进入新模式 |
| Host DTO | 在真实 SDK facade snapshot/graph_history 上增加 `search` 只读区，包含 policy/decisions/fragments/graph-change details/context receipt references | 实际 SDK shape 的集成测试先行，禁止 synthetic fixture 发明顶层字段。稳定 Task/Result/receipt 身份防同 Task 跨结果关联错误；不扩权模型工具 |

两处 P1 的具体决策见 §9.2–9.6：确定性 scope 投影与 ready side record，不新增公共 Task 状态。独立审查后才能冻结，必须由实际 oracle 证明，不能仅写 dataclass 算完成。

独立挑战补充（Kepler，只读；采纳为实现约束，具体 schema 仍待 oracle 后冻结）：

- **局部 scope**：origin 还必须绑定原 Task semantic revision 与 criterion IDs；系统采用版本化 scope 投影规则，只允许完整原准则子集或可确定解释的目标 path/引用范围。结构准则仅证明结构，模型填写 `file:` PASS 不得晋级内容 Claim；禁止字符串截断、删前提或自动语义弱化。无法从原合同确定导出的新命题，走现有图审批/提交的新合同完整验收，产生新 Claim，原失败保持。增加“有效文件存在但片段命题错误”“错原准则 revision”“截掉条件”的真实拒绝 oracle。
- **候选 ready 不等于正式接受**：当前 `accept_result` 对 `DONE/PASS` 直接幂等返回，不能用这两个字段暂存比较资格，也不能提前将 Attempt 标 COMPLETED。建议在独立 candidate/selection receipt 记录 ready，原 Result 保持 RUNNING、Attempt 保持 VERIFYING；仅显式 COMPARE 调度消费该记录，避免重复验证，并区分等待选择与实际占用执行槽，不删除 Attempt 总数或费用。为 synthesis 留次数和预算。需以当前 lease/reconcile 的实际路径测试这一状态方案，不能仅靠跳过 allocator 掩盖永久占位。
- **最终接受单门**：事务内复核真实 candidate receipts、完整规则、source 当前性、Task semantic revision 与 selection revision；接受时必须有当前有效 Attempt lease。新 COMPARE 入口明确拒绝 `owner=None`（现 `_require_lease` 对 None 直接返回），所有比较模式接受调用均经过同一门；不修改默认 FIRST_VERIFIED 的旧调用语义。增加 ready 后冷重开、lease 过期/被接管、绕过 selection 直接 accept、并发最终接受与重复恢复 oracle。
- **源码身份**：复用主已实现的 Host editable-source attestation；验证 root/commit/生产输入 inventory 与实际 module origin。它是启动快照，不能在同进程重新 attestation 冒充已加载代码热切换；不放宽旧 wheel guard。

若实现包含“用户中途新增目标限制”，应另走显式修订 Proposal/审批/受影响范围与后继 Task，不修改终态合同。本最小方案不新增自由聊天改目标入口；不能在 UI 暗中接受该能力。A03 原旧 Proposal/迟到路径仍必须完整验证。

## 5. 原八 AC → 现有基础、增量 oracle 与真实门

原句逐项保留。表中现有 selector 是已核对的测试源码定位，**非本轮执行结果**；新增测试名均为建议、尚不存在。

| 原 AC 与必须观察到的结果 | 可复用现有精确 oracle（`tests/` 下） | 还需证明/切片 |
|---|---|---|
| **P3.4-A01 真实角色差异**：实际Provider输入按角色不同；未验证材料均带标签 | `orchestrator/step05/test_manager_decisions.py::test_s5_02_a_change_of_role_continues_the_task_with_the_new_approach`；`orchestrator/step04/test_retrieval_context.py::test_verifier_and_critic_templates_withhold_the_submitter_and_arbiter_sees_the_dispute` | A：真实 dispatch 各角色输入矩阵；真实 deepseek-flash 请求与产物差异。UI 标签不能替代 |
| **P3.4-A02 局部动态修复**：Manager提交局部GraphPatch；新前置被执行，无关成果不重跑 | `orchestrator/step05/test_dynamic_dag_closure.py::test_s5_05_and_s5_06_unrelated_results_are_kept_and_the_superseded_late_candidate_is_history` | B：实际缺前置分类→E→B2；真实 Provider 必须出现至少一次有依据局部改图，UI 可解释影响范围 |
| **P3.4-A03 旧Proposal及迟到结果**：不覆盖当前图；可重用部分需验证，预算不清零 | `orchestrator/step05/test_graph_changes.py::test_s5_03_two_managers_on_the_same_base_rebase_when_disjoint_and_are_refused_when_they_overlap`；上项 closure | B/C：迟到/失败片段独立新验证、恢复和费用守恒；不能将“晚到只拒绝”当完整复用证明 |
| **P3.4-A04 多个候选择优组合**：按已批准选择策略保留片段、合成新Candidate、再验收；不机械拼接 | `orchestrator/step03/test_static_dag_closure.py::test_s3_05_two_candidates_first_pass_accepted_second_superseded`（只保护旧模式，不证明新 AC） | C：批准比较政策、同 Task 候选集合、互补片段实际合成及失败反例、一次最终接受、UI 决定清单 |
| **P3.4-A05 失败尝试的有效片段**：局部提案可单独验收复用；原Attempt失败事实不改变 | `orchestrator/step04/test_retrieval_context.py::test_s4_02_unverified_claims_never_reach_a_worker_as_fact_and_citing_one_fails`（信任基线） | B：新局部验证后真正下游采用；原失败/原拒绝 Claim 不改；引用/范围错误保持失败 |
| **P3.4-A06 Context真实生效**：使用各自tokenizer/工作预算；保护必需资料，记录实际选择和request hash | `agents/test_context_journal.py::test_policy_hash_and_counts_are_not_reused_across_tokenizers`、`::test_required_content_too_large_fails_before_any_provider_call`、`::test_unknown_resume_reuses_the_frozen_request_without_a_new_selection` | A：Orchestrator RuntimeProfile→assembly→实际多轮 Provider 请求→关联 receipt。`agents/test_context_real_provider.py::test_real_model_input_tokens_stay_within_budget` 可复用测试方法，但不能据通用 Tiktoken 配置宣称 flash 原生 tokenizer 已核实 |
| **P3.4-A07 历史不升格指令**：作为受控数据渲染，不能生成权限/控制命令；不使用SYSTEM身份承载原文 | `agents/test_recall_untrusted_frame.py::test_recall_is_never_a_system_message`、`::test_recalled_text_cannot_close_the_frame_early` | A：实际编排长回合/恢复含攻击历史，真实 Provider 下来源只是数据、程序无越权；不能只验证字符串标签 |
| **P3.4-A08 有界探索会停止**：按原预算、停滞和图深度限制停止/调整；不无限生成新节点 | `orchestrator/step05/test_manager_decisions.py::test_s5_02_repeated_no_progress_without_a_change_of_approach_stops_explicitly`；`orchestrator/step05/test_graph_changes.py::test_s5_08_depth_proposal_count_and_budget_limits_are_enforced_with_reasons` | B/C：新局部复用/比较闭环中的额度、重复、截止跨重启保持；UI 显示实际停止原因，真实运行自然收敛 |

原 `orchestrator/step05/test_real_provider_dynamic_dag.py::test_real_dynamic_dag_closure` 允许 Mission COMPLETED 或 FAILED，且改图只在发生时检查；因此不能不加约束就将其旧 PASS 计作 §6.6 的“至少一次改图、一次跨分支复用、一次最终综合验收”。保留原测试语义，另加新价值 oracle。

## 6. 兼容、恢复及测试执行计划

1. 锁定主线程完成 P3.3 后的准确 SDK/Host 源码基线、加载路径、失败 selector 集及 skip 原因。不得用早期 73 条历史基线红集合替代当前精确对照，也不把受控测试与模型 skip 相加为完整验收。
2. 默认 FIRST_VERIFIED、旧 code envelope/Task/intent 的 canonical bytes、prompt/version、结果回放和预算语义保持；新字段缺省省略或按显式 legacy 解码。不能把历史缺字段当新功能默认成功。当前全部已冻结 doc 版本的 P3.3 语义不改，包括真实 Critic proof、来源当前性与普通人审不能代替冲突仲裁。
3. 新 prompt/context/policy 使用后继注册版本，不能改已发布 v1 文案。新能力实现并验收后按测试阶段要求默认可用；COMPARE 仍需原计划要求的显式已批准政策，不强迫所有 Mission 多路搜索。未知版本和不完整新冻结 metadata 应 fail closed。
4. 真实默认 code 模式和显式比较 code 模式都覆盖；code 的测试/形式验证未部署时的拒绝、action 审批、沙箱、路径/CAS 保护不弱化。doc 的 source 失效、普通 human 不能绕 conflict 专用裁决等 P3.3 反例保留。
5. 每片由唯一 runner 跑该片新增 oracle、实际 runtime 接缝，再跑受影响原 selector。完成累计后安排 SDK 编排全量、BaseAgent Context 相关套件、Host orchestration、前端 Missions 测试/typecheck；有已知失败则逐 selector 基线对照。命令及总数由实际运行产物记录，此 draft 不填推测 PASS 数。
6. 建议新增测试目录 `tests/orchestrator/p34/`；Host 测试扩 `backend/tests/orchestration/` 与 `tauri-app/src/views/MissionsView.test.tsx`/stores 现有入口。每条新增事件在实际冷重开+replay 比对中必须可解释；断电/丢回复的同请求恢复局部验证属于本片，P3.5 的大规模压力/多库备份不是本片交付条件。

## 7. 真实 Provider 与源码 UI 价值门

**必须分开记录三类证据：**受控 Provider 驱动真实 SDK 的确定性行为；真实 `deepseek-flash` 推理；用户通过实际 Tauri 源码 UI 的点击/输入/查看。pytest/vitest 或 WebSocket 直注不是 UI 证明。此次准备没有调用模型或读取 key。

最小连贯场景建议沿原 recorder 任务做一个有独立证据的局部修复与综合，复用 `testing/fixtures.py` 的 RECORDER_SEED/固定测试作为任务材料而不是伪造 Provider 返回。将输入规范的真实缺项、两个互补方法和固定验收标准预先写清：一支先完成独立文档/检查，另一支实际反馈缺规范，经 Manager 新增前置后继续；一个整体失败实现中的独立合法片段经验证复用；比较两个候选后形成最终 C。也可用 P3.3 已验证的 Host 文档来源场景承载，但 literal doc 的 criterion/来源归属边界不能被“研究质量”自然语言替代。

- 先运行同材料 FIRST_VERIFIED 对照，再用明确已批准 COMPARE 政策；记录实际有效成果、重复、成本、停止与采用清单。若多路无收益如实报告，保留简单模式；不能以 Agent 数或节点数上涨算成功。
- 真实模型未触发所需改图/局部复用时，该次是未证明/失败，不能后台造事件后标真模型通过。构造有真实信息缺口的场景、预设有界尝试，禁止无限重跑直到挑出一次漂亮结果。
- A06 的模型 tokenizer/窗口资料必须在实施时核实：当前 `RuntimeProfile` 与 Host provider 源码不足以证明部署具有准确 flash tokenizer。优先实际部署配套 tokenizer；若只能保守估计，标记 fingerprint/降级并保留准确配置门待验。至少核每次实际 Provider usage，不能把另一模型 tokenizer 测试当作本模型精确性证据。
- UI 最小序列：独立 userdata 启动源码 Tauri → 确认 Host/SDK source identity 与 provider model → 创建 Mission/选择已批准搜索政策 → 查看角色与 Context 选择 → 观察缺前置及图改动 → 点开原失败/局部验证/复用血缘 → 查看 A/B 未选原因与 C 新验收 → 查看最终交付/费用 → 冷重开同 Mission 重查，确认无新调用/新费用。
- 在上述界面加真实空候选、预算停止、来源失效/读取错误、长列表与冷加载状态检查；硬失败不得由 UI 过滤隐藏成已通过。展示完整采用关系，不把所有候选都画成交付依赖。
- 仅由 Tauri 启动并拥有一个 backend；源码模式确认实际 `DESKPET_BACKEND_DIR`/Python 和 SDK 路径，Vite 只由一个入口管理。保持隔离 userdata/端口，结束核残留；不启动 frozen launcher、不构建/安装新 wheel、不重做 PyInstaller/Tauri bundle。
- Provider 使用用户既定 `deepseek-flash`；沿既有 provider/config 源注入凭据到进程，不能复制 key 进计划、日志、命令回显或截图。原始证据放 `.local-test-evidence/<date>/p34-<run>/`，Git 仅写结论、命令、索引和 hash。

P3.4 最终关闭条件：八 AC 各有指定原 oracle 的证据，§6.6 连贯场景实际出现改图、跨分支复用、综合再验收；源码 UI 与真实模型门均完成，基线兼容及冻结恢复无新增未处理回归。P3.3 未完成项独立保留，P3.5 后续容量/长任务工作不提前算完成，P3.6 不在授权范围。

## 8. 准备记录与待分工

- 本次开始：2026-09-12 23:02:05 +08:00；仅阅读与写本 draft。完成时间见最终交付消息，测试/模型运行时间均为 0。
- 建议按完整切片分工：SDK 主执行/选择事务 owner；Context/角色 owner；片段证据/验证 owner；Host DTO/UI owner。每片同步明确单写文件范围，主线程串行测试；不是把 backend/frontend 两端拆成各自宣称完成的阶段。
- 先由独立评审裁决 §4 两个窄接口及 policy 兼容策略，再冻结 oracle 名称与基线。当前不承诺总工时：比较模式的 lease/Result 状态复用和真实 tokenizer 尚需实施前小范围核验；已有组件显著减少工作量，但不能据其存在省掉真实闭环。

## 9. 精确接口与独立 oracle 草案（未应用的实施设计）

本节完成于 2026-09-13，只是下一轮 vertical slices 的可审查设计。以下新增类型、port、表和 selector **均为提案，尚未实现/运行**；现有函数会明确标出。原八 AC、默认 code/FIRST_VERIFIED、所有已冻结 doc 版本及专用仲裁语义不变。实施须等主解冻相应源码并明确写所有权，当前 SDK `src` 与两批 Critic 测试保持冻结。

### 9.1 通用表示与持久边界

- 新对象有 `schema_version: 1`，严格拒绝未知字段、错误类型及未知枚举。`Hash` 为规范 JSON 或实际 bytes 的 SHA-256 小写 64 hex；两者按字段区分，不哈希机器路径或时间戳代替内容。`Id` 复用现有 opaque ID；整数拒绝 bool，期限拒绝 NaN/Infinity。hash 输入包含 schema/version，列表中有业务顺序时不排序。
- `Task.version` 是行 CAS 版本，会随执行状态变化，**不能作为 semantic revision**。criterion ordinal 沿现有 `criterion_id` 使用 **1 基**；P3.3 citation index 继续 receipt 的 **0 基**，不混用。
- 原始 bytes 只引用现有 Artifact/CAS/Source registry。新 receipt 写入现有不可变 receipt 存储并关联事件；不新建 Proof-DAG。COMPARE 仅需可按 Task/result 查询的 round/candidate 状态索引，其 canonical payload 和事件一起由 Commit Service 写入、回放；不以 UI 缓存或 Python 内存集合为权威。
- 所有新变更入口携带 `command_id`。幂等键为 `(mission_id, command_id)`，保存规范输入 hash 与输出 receipt ID；同键同 hash 返回原 receipt，同键异 hash 拒绝。失败不得留下半份图/预算/ready 记录。只读结果重放不重新执行模型、验证或外部动作。

### 9.2 TaskRevisionV1 与原 criterion 身份

`TaskRevisionV1` 的字段如下；由程序从 origin 的 durable DispatchIntent 冻结合同构建，模型只能引用 revision ID。

| 字段 | 精确含义 |
|---|---|
| `schema_version, mission_id, task_id, origin_intent_id` | 同 Mission 的原合同来源；intent→Attempt→Task 关联必须成立 |
| `observed_task_version: int` | 记录当时行版本供解释/CAS，不进入 semantic hash |
| `contract` | 现有 assessment `_contract` 的完整规范对象：task_id、kind、goal、rationale、success_criteria、verification_policy、outputs；不删自然语言前提 |
| `task_contract_revision: Hash` | 原样调用现有 `verification.assessments.task_contract_revision(contract)`，不改变已发布算法 |
| `mission_contract_revision: Hash` | 调用现有原 Mission goal/criteria hash；Task scope 不替换原 Mission 合同 |
| `execution_constraints` | 原允许工具、预算上限、执行依赖 IDs、冻结直接输入身份、domain id/version、policy binding、既有 context 中影响执行/权限的合同字段；不含 spent/reserved/lease/progress 等运行计数 |
| `constraints_revision: Hash` | 对上述约束规范对象的 hash，补足现有 task_contract_revision 不含权限/依赖的边界；字段投影算法单独版本化 |
| `criteria: [{id, ordinal, text, kind}]` | 原顺序完整列表；id 使用现有 `criterion_id(task_contract_revision, ordinal, text)`；重复文本也保留原 ordinal，不靠文本去重 |
| `revision_id: Hash` | schema + mission/task IDs + 两种 contract revision + constraints_revision；不含 observed row version |

当前合同重验分两层：原 receipt 永远解释原 snapshot；**新执行/新接受**必须与当前非终态 Task 的 semantic revision 和约束一致。图改动无关分支不使本 Task 失效；改依赖、权限、输出、准则等相关字段即拒绝旧选择并重新提案。不能以原 revision 可查为由继续接受已被替换的 Task。

### 9.3 FragmentProposalV1：确定性范围投影及验证 port

```text
FragmentProposalV1 = {
  schema_version: 1,
  origin: {mission_id, task_id, attempt_id, result_id, task_revision_id},
  criterion_ids: [Id, ...],
  claim_refs: [{claim_id, claim_revision}, ...],
  material_refs: [ArtifactMaterial | CitationMaterial, ...],
  rationale: string
}
ArtifactMaterial = {kind: "artifact", artifact_id, content_hash,
                    byte_start: int, byte_end_exclusive: int}
CitationMaterial = {kind: "citation", receipt_id, citation_index: int}
```

`criterion_ids` 非空、无重复，程序恢复原 ordinal 顺序。artifact 是实际已收集的不可变 bytes，范围为 `[start,end)`、0 基 bytes，文本边界不得切断 UTF-8；citation 必须按 origin result/receipt 的原 0 基 index 解析，不能拿 display preview 作为证据。没有原 bytes 的迟到 summary/path 只能给出 `material_unavailable`，不能补读现在同名路径。

冻结 `scope_projection="whole-criterion-v1"`，其规则是：

1. 每个 ID 必须精确属于原 TaskRevision；输出映射、原 criterion 全文字节及其前提不变。v1 **不做** criterion 字符串裁剪、合取拆分、改 pytest target、把引用范围改小后声称原命题成立等语义改写。
2. 投影只产生“这些原准则在这些固定材料上得到何种独立验证”的**新局部合同**，不是原 Task 的缩减版成功条件。新验证 Task 的 goal/rationale 由固定模板引用原 goal、所选 IDs 和 scope；仍携带完整原 Mission 准则、共同硬约束、原验证层及必要直接输入。未选准则在投影中标明 outside_scope，不能据局部验证将其标为 met=true 或据此完成原 Task。
3. 仅允许当前 verifier 能确定其输入闭包的准则投影：如完整原 `pytest:` target 连同测试/fixtures/依赖，或完整原 file/cite 检查及来源条件。不能确定独立范围、依赖其他未满足前提、或 action/arbitration 涉及专用授权/目标绑定时，返回 `scope_not_derivable`；走现有显式新合同/图提交和完整验收，不能自动削弱原合同。这里拒绝的是无依据的自动投影，不取消原 A05 的局部复用能力。
4. `file:` 只证明存在/结构；不能据其 PASS 将“方案正确”之类内容 Claim 晋级。原 Claim 文本、scope、limitations 不可因提案而改写；新验证只能为明确可验证的新 Claim 生成真实 assessment/证据。code_test 等级仍需实际 code 验证，doc attribution 仍不证明世界事实。原失败/拒绝 Claim 与 Attempt 保持历史真值。
5. artifact 子范围只是输入材料；不得把子范围冒充整个原文件来满足 file/pytest 检查。完整运行依赖必须另列并从原快照物化。来源撤销、版本变化、缺运行依赖或缺实际 Critic proof 都必须经过原 verifier/currentness 门。

局部 scope 注释放在 projection receipt，不擅自给旧 `CriticVerdict` 增枚举。Critic 仍按原 schema 回报完整原 Mission criteria 的真实 met/reason；局部验证不支持的项不得写 met=true。局部 Task PASS 与最终 Mission 全合同判定分开，保留当前 doc5 的实际独立 Critic receipt 门，不新建“局部免 Critic”出口。

建议新增三个窄 port，均由现有 Commit Service 调用，不给模型新增写权：

| Port | 输入 → 输出及事务 |
|---|---|
| `project_fragment(proposal) -> ScopeProjectionV1` | 纯确定性验证；输出 origin revision、原完整 criterion、支持输入闭包、原→新 criterion ID 映射、继承约束、不可证明范围、projection hash；不写 PASS、不花预算 |
| `commit_fragment_validation(proposal, *, command_id, base_graph_version, source) -> receipt` | **一个事务**重查 origin/bytes/source/scope、额度、图版本，落 projection receipt + 新验证 Task + 既有 graph change/event；返回 `fragment_id, validation_task_id, projection_receipt_id, graph_change_id`。内部复用图提交逻辑，不能先记录提案成功再另一次调用建 Task |
| `fragment_input(fragment_id, *, consumer_task_revision_id) -> CandidateInputV1` | 只在新验证 Task 真实接受后提供该 scope 的新 Claim/receipt/material；验当前性、同 Mission、下游适用条件。引用失败 origin 是知识血缘，不是要求 origin Task COMPLETED 的执行依赖 |

Fragment 状态由投影 receipt、验证 Task 与实际接受 receipt 推导；不再建一套 PASS 字段。重复提案以 origin revision + criterion IDs + material hashes + projection version 去重，消耗和停滞计数不因换文案/角色清零。

### 9.4 SelectionPolicyV1、ready receipt 与合成输入

新政策采用**后继 policy schema**，旧 `governance.promotion` 要求精确原字段集合的版本继续原样解析/哈希；不能直接扩大旧 PROMOTABLE 集合使历史 bytes 失效。新 schema 增 `search_selection`：

```text
SelectionPolicyV1 = {
  schema_version: 1,
  mode: "FIRST_VERIFIED" | "COMPARE_THEN_SYNTHESIZE",
  max_candidates: int,                 # v1 1..3，并受原 attempts/预算限制
  deadline_seconds: finite_positive,
  tie_break: "verified_rank_then_result_id-v1",
  synthesis_limit: int,                # v1 0..1；组合场景必须为 1
  on_deadline: "best_complete_else_stop",
  synthesis_reserve: {tokens, cost_micros, tool_calls},
  synthesis_attempts_reserved: int     # v1 组合为 1，仍计入原 Task/Mission 上限
}
```

`rank` 必须来自冻结选择 receipt，未提供有效 rank 时按 result ID 稳定排序；模型分数不能越过确定性资格门。policy 中预算值是现有上限**以内的预留**，不是增发额度。未部署的验证层、未知 policy/version、无真实审批记录、max_candidates 超原能力等直接拒绝。

`max_candidates` 计本 round 的探索候选（包括 probe），不把同一结果的重试读取计成新候选；一次 synthesis 是另一个真实 Attempt，另占已预留次数。二者总和仍受原 Task/Mission attempts、执行并发及预算约束，不能借这个区分产生免费的 C。

创建入口建议增可选 `search_policy_version_id`，仅引用现有 registry 经实际 evaluate/approve/promote 后可绑定的新 policy。服务端核 tenant、真实 principal 与 policy 状态，再冻结 `SearchPolicyBindingV1={version_id, approval_receipt_id, policy_hash, effective_scope}`；没有该显式 binding 保持 FIRST_VERIFIED。Task 可从 Mission 绑定选择作用范围，但不能由 Worker/Manager 自报 `approved: true` 开启。Host 只展示真实可用的已批准选择，不在 UI 拼造审批事实。

`CandidateReadyV1` 是不可变验证资格 receipt，至少包含：`mission_id, task_id, task_revision_id, round_id, result_id, attempt_id, verification_rows_hash, artifact_refs, source_binding_hash, critic_receipt_ids, human_receipt_ids, required_layer_set, eligibility_version, created_at`。source hash 指向冻结目录/版本，**不替代最终 live source 检查**。全部现有必需验证通过才可 ready；FAIL/ERROR 仍是失败。只通过独立片段验证的材料走 `FragmentInput`，不能把其原失败 Result 标 ready。

`CandidateInputV1` 为二选一：`{kind:"complete_candidate", ready_receipt_id, result_id, material_refs}` 或 `{kind:"validated_fragment", fragment_id, validation_result_id, projection_receipt_id, material_refs}`。两者都只读、同 Mission、实际当前有效。合成端按 origin/result/hash 命名空间物化，保留原逻辑 path 映射；同名文件必须让 Synthesizer 明确生成新输出，不能顺序覆盖。输入选择是 lineage，不能作为对失败原 Task 的执行 dependency。

`SelectionDecisionV1` 记录 `round_id, expected_round_version, task_revision_id, policy_hash, considered:[{input_ref, eligible, rank?, reason}], selected_inputs, action, judge_intent_id?, budget_snapshot`。`action` 仅为 `accept_complete | synthesize | probe | stop`：probe 仍受已冻结候选数/截止/停滞门；synthesize 只能一次；所选 input 必须属于考虑集合且通过程序资格门。Judge Provider 回复只是提案，程序生成正式决定 receipt；无实际 Judge 调用的确定性 deadline 决定以 `judge_intent_id=null` 和确定性规则版本如实记录。

### 9.5 事务状态及 scheduler/lease 接口

| 场景 | Round/候选索引 | 原业务状态与必需行为 |
|---|---|---|
| `begin_selection_round(task_id, command_id)` | 新 round `COLLECTING`，固定 policy/revision、`deadline_at=store.now+deadline_seconds`、round version | 一个事务核剩余额度并保留综合/最终验证资源；同 Task/revision 至多一个开放 round，重复恢复不重置期限 |
| `record_candidate_ready(result_id, *, owner, round_id, expected_round_version, command_id)` | 写 ready receipt；索引 `READY`，round version 前进 | Result **RUNNING / verdict=None**，Attempt **VERIFYING**，Task **VERIFYING**；不写 DONE/PASS、不完成 Task、不 SUPERSEDE 兄弟、不产正式交付/Action |
| scheduler 看到 READY | 查询实际 side receipt 并核 round/revision | 不再 `_verify` 同一 result；释放已完成 verifier/Provider 工作占用，但不删除 Attempt/费用或全局 OPEN 状态。等待选择与实际执行分别计数 |
| `decide_selection(...)` | CAS `COLLECTING → DECIDED`，或有界 probe 留在 COLLECTING；固定决定 receipt | 重验当前资格/期限/预算；record ready 后有新验证行、原输入失效或合同改变则候选 INVALIDATED，不消费陈旧资格 |
| `start_synthesis(decision_id, *, owner, command_id)` | `DECIDED → SYNTHESIZING` | 同 Task 新 Attempt/真实 DispatchIntent，冻结所选输入、原完整合同与综合角色；不新建自依赖 Task，不直接继承输入 PASS |
| 综合验证 | round 仍 SYNTHESIZING；新 C 有独立验证记录 | C 全合同通过后走 §9.6；失败不继续无限开综合 ordinal。政策 deadline/final reserve 决定采用合格完整候选或明确停止 |
| 最终接受/耗尽/修订 | `COMMITTED / EXHAUSTED / INVALIDATED` | 明确原因与原支出；关闭新准入、取消未采用在途工作但继续真实费用收集；未知费用占用不释放 |

两处 scheduler 必须一起改：一是待验证队列识别 ready；二是 candidate 并发计数将该 round 的 READY 等待者与真实执行者区分，从已预留的 synthesis slot 创建 C。**原 Task/Mission 总 attempts、已花费与库中 Attempt 状态均不清零**；只改“正在执行的槽”判定，不能全局删掉 VERIFYING 或绕过 `max_open_attempts` 后无限生成。所选策略准入时若剩余 attempts 不够综合一次，拒绝组合配置，而非等 A/B 占满后死锁。

选择等待期间沿用实际 Attempt owner/lease fencing：只对当前活 round、当前 owner、当前有效 ready 的等待工作续租，phase 明确为 `selection_wait`；复用本轮已修的事务内 `renew_lease(minimum_remaining_seconds=lease_seconds/2)` 节流。它证明 coordinator 仍在处理选择，不声称 Provider 仍执行。scheduler/reconcile 必须识别此等待状态，不能因原 SDK turn 已完成把 ready 当 LOST；到达 round 截止、Task/Mission 终态即停止续租。owner 变更不等于任务取消，旧 owner 不得取消新 owner 的工作。接管走已有过期 lease CAS，所有最终修改仍重查当前 owner。

`deadline_at` 是本 round 从探索到最终决定的持久上限，进入综合或重启都不延长。新综合/验证的运行窗口取配置上限与 round 剩余时间的较小值；不足以启动必要验证则采用政策规定的完整候选或停止，不能开始一轮保证超期的综合。SDK 调用仍在途时超时必须 cancel 原 exact turn、保留收集身份和 reservation，不以 result=None 创建新 Critic ordinal或导入 provisional zero；终态晚费用继续核实结算，不能反向接受超期新结果。

预算最小 port：`reserve_selection_tail(round_id, task_account, reserve)` 在开始展开前占用原 Task→Mission→global 链；暂不计一次执行。`transfer_selection_reserve(round_id, attempt_subject, allocations)` 在同一事务中将剩余 hold 转到真实综合 Attempt 及其必需验证服务 reservation；总 reserved 守恒，实际创建才计 attempt。不得先 release 再异步 reserve，或同时双占使合法综合被拒。最终 unused hold 可显式释放；存在 UNKNOWN 的真实调用 reservation 独立保留。转移的每个 subject/amount 进入 receipt/replay，这个 port 是待实现事项，不能用 budget_snapshot 代替。

### 9.6 最终接受单门与 owner=None 拒绝

建议 `accept_selected_result(result_id, *, round_id, decision_id, expected_round_version, owner: str, command_id) -> acceptance_receipt`。`owner` 不能为空或 None。入口使用一个 `BEGIN IMMEDIATE` 事务，按以下顺序完成：

1. 验证显式 search binding、round/decision 的 Mission/Task/result 关联及 command hash；拒绝跨 Mission/跨 result、未知模式和伪 decision。检查当前 Task/原 Mission 合同、execution constraints、domain/policy 版本。
2. 对**新接受**重查 Attempt 当前有效 lease 和 owner、所有终态、当前 round version、选中资格 receipt，以及真实验证 rows/当前材料 hash。来源 supersede/revoke、引用/assessment invalidation、human/conflict/action 当前性沿现有接受门核实；所有必需验证层与 doc5 实际 Critic proof 保留。
3. 把现有 `accept_result` 的资格验证提取为内部共享检查，ready 和 final 共用但 final 必须重新执行当前性检查。最终只有这一步复用原“Claims/Knowledge → Result DONE/PASS → Attempt/Task COMPLETED → 兄弟 SUPERSEDED → dependencies/events”的写事务，连同 round COMMITTED 和 acceptance receipt 一起提交。
4. `accept_result` 在 **现有 DONE/PASS 早返之前**分流：任务具有显式 COMPARE binding 时，普通直接调用拒绝，必须进入选择接受门；不能通过提前写 DONE/PASS、owner=None 或提供假的 verifier_results 绕过。FIRST_VERIFIED 的原早返/owner 缺省兼容保持。
5. 同命令在成功后重放可只读返回原 acceptance receipt，核 command hash/result/decision 和非空 owner；这不是再接受，不要求已终态 Attempt 永久续租。新命令、改 result/decision、过期未完成决定仍走完整门。读取过去 receipt 不声称来源现在仍有效，UI 使用当前 source 状态另行提示。

接受身份区分两条合法路径：`accept_complete` 的 result 必须就是决定选中的完整 ready result；`synthesize` 的 C 必须属于 `start_synthesis` receipt 绑定的那一个新 Attempt/DispatchIntent，并经完整独立验证。C 的 ready receipt 可在本 round 的 SYNTHESIZING 阶段落库，不重新进入探索候选计数或触发第二次综合。不能拿未参与决定的第三份 PASS result 冒充 C，不能把 A/B 输入 receipt 当作 C 的验证 receipt。

外部发布/action 不在这个数据库事务中假装已完成；仍走原正式审批、exact identity dispatch 与 UNKNOWN 核对。选择不授予 Connector 新权限。冷恢复若发现只有 ready/decision 没有 acceptance receipt，绝不能从 UI 绿色状态补一份“成功”。

### 9.7 Context 配置与真实请求 port

`RuntimeProfile.context_profile_ref` 在 `runtime/model_router.py` 增量接线；`None` 为旧冻结行为。新配置解析为 `ResolvedContextProfileV1`：`{schema_version, profile_id, revision, model_deployment_ref, task_class, tokenizer_ref, tokenizer_fingerprint, working_input_limit, model_input_limit, model_total_window, output_reserve, safety_margin, max_tool_result_tokens, retrieval:{mode, version, embedding_ref?, rerank_version}, role_visibility_version}`。各 token 值为正整数，安全余量可为 0；实际模型部署资料来源与有效限制一起记录，不能由 Worker 提供。

装配 port：`resolve_context_profile(ref, runtime_profile) -> ResolvedContextProfileV1`，再把实际 tokenizer、ContextPolicy、可用 embedding 注入既有 `AgentRuntimePorts`。`ContextPolicy.max_input_tokens=min(working_input_limit,model_input_limit)`，max_total_tokens/model_total_window、output_reserve、安全余量按原公式配置；最终实际请求包括 system、工具 schemas、消息协议和当前输入全部计数。render_slack 不在公式外给正裕量；要使用则并入安全余量。必需合同放不下时零 Provider 调用，不能剪掉原 criterion/硬约束。

角色可见性为版本化矩阵：Explorer 候选/反例带真实等级与 source 状态；Exploiter 正式可用材料与明确前提；Connector 可复用 scope/lineage；Critic/Verifier 独立证据及实际失败，不只 Worker 摘要。原文一律受控数据表示，不能因 role 改成 SYSTEM 权威。相关性排序和预算截取只作用于可选材料，排除项记录原因。

上层 `ContextBindingV1` 冻结 config hash、角色版本、选材 IDs/revisions 和原 intent 输入 hash；下层用现有 run/turn/request/selection receipt 的实际 request_hash、selected_seqs、dropped_ranges、tokenizer fingerprint 对齐。两个 hash 不互相替代。恢复一个已冻结 Provider 请求保持原 bytes/selection/计费身份，不重新召回；新请求才可按冻结配置作新 selection。

### 9.8 历史回读准入、分页与重复 preview

当前 N1 暴露的两个具体边界必须纳入 A06/A07：`context.port._preview_of` 给出 dotted `session_history.read`，但实际工具名为 **`session_history_read`**；该工具虽已注册，编排角色 `AgentConfig.tool_names` 尚无准入。其现有 `page_size` 只限制记录数，一条大记录仍可能再次 preview，形成不能推进的回读链。不能靠增大 max_result 或打印全文掩盖。

后继 Context/tool schema 设计：

- 新 preview 输出 `journal_read_back={tool:"session_history_read", arguments:{from_seq:seq,to_seq:seq,page_size:1}, record_hash}`。实际 descriptor、AgentConfig.tool_names 与 ProviderToolSpec 同时准入；此为已绑定 Agent 自身历史的只读 port，不接受调用者指定 agent_id/tenant/path，不向模型扩展跨 run 能力。旧 journal bytes/receipt 不改写，旧 dotted ref 可由受控渲染适配为新实际工具名并记录版本。
- 复用同名工具，后继 schema 为原 `from_seq/to_seq/page_size` 增可选 `content_offset`、`max_content_chars`、`expected_record_hash`。`content_offset` 为 **0 基 Unicode code points**，不冒充 byte/line；单记录内容模式要求 `from_seq==to_seq`、page_size=1。非零 offset 必须提供 hash。v1 max_content_chars 默认 4096、上限 16384，仍受实际工具/Context token 预算再次缩小；数值是有界请求参数，不是模型最佳窗口宣称。
- 内容模式返回 `record_seq, kind, turn_id, record_hash, content_encoding:"canonical-message-json", content_chunk, content_offset, next_content_offset|null, total_chars`。hash 对原 message 的 canonical UTF-8 JSON bytes；页内 metadata 保留原 provenance/trust/authority 与来源关联，不在续页凭空恢复已撤销 source。记录列表模式保留原 `records/next_seq` 兼容，但巨大单记录必须给可推进的原记录内容 cursor。
- 生成页时以实际 tokenizer 计算 **整个 ToolResult 包装后的大小**，不只 chunk；它必须可放入 max_tool_result_tokens 和当前请求工作预算。必要时缩小页；metadata 单独都放不下则明确 `history_page_budget_exhausted`，不无限重试。SDK 内部标识已验证有界页的 provenance，不能信任模型 JSON 自报 `already_paged`。
- 分页响应不能再次指向“分页响应自己的 Journal seq”而丢原 seq；preview 与 continuation 始终指原 record/hash/offset。每次成功读非末页必须 `next_offset > offset`。同一 cursor 的幂等重放可返回同页，但要标为重放、受原工具调用预算与有限重复控制；没有新读取内容不计搜索进展。
- 主已新增的 `workspace_read_file` 分页是当前源码事实：Unicode offset、expected_sha256、next_offset、行定位、信任与实际 bytes hash。它解决 workspace 大文件直接阅读，不等于历史回读已准入。两种 cursor 的 units 分开标识，共同用真实 Provider 请求证明后文可读且未再截断；不修改正在冻结的 N1 接线。

### 9.9 独立验收 oracle 清单

下列 selector 名为待创建提案。每项先以真实 Orchestrator/SDK/Provider 调用边界写红测试；不得写手工 PASS/receipt、直接造 UI 状态或用生产 eligibility 函数自身作为唯一 oracle。共享 helper 只能负责建立输入，结论由独立 bytes、事件、费用、实际请求与公开结果检查。

| 建议 selector（`tests/orchestrator/p34/`） | 必须独立看到的判据 | 原 AC |
|---|---|---|
| `test_fragment_scope.py::test_projection_preserves_origin_criterion_identity_and_conditions` | 实际失败 A 的原 revision/1基 IDs、完整前提保持；改变 ID/revision/子串/目标拒绝；文件存在但内容命题错误不能变 VERIFIED | A03/A05 |
| `test_fragment_scope.py::test_actual_partial_verification_reuses_only_its_scope` | 原 A 整体 FAIL，新验证 Task 实际工具/测试/Critic 完成，真实 B 读入并引用新 scope；原失败/费用、未选 criterion 与 Claim 保持，缺输入/source revoke 反例拒绝 | A03/A05 |
| `test_selection_transaction.py::test_ready_is_not_done_pass_or_publication` | A 真实验证结束，Result RUNNING/None、Attempt VERIFYING；多调度 tick 不重复验证，B 到齐后新 C 真执行且未提前 accept/Action | A04/A08 |
| `test_selection_transaction.py::test_compare_owner_and_early_return_are_fenced` | 直接 accept、owner=None/空串、lease 过期/接管、伪 DONE/PASS、跨 result/decision 均拒绝；并发 final 仅一个接受 receipt/Knowledge/交付 | A04 |
| `test_selection_transaction.py::test_ready_currentness_and_reopen` | ready 后真实 source revoke/合同相关修订/验证行变更使资格失效；无关图修订不重跑成果；重开不改 deadline、不新增同请求或费用 | A02/A03/A04 |
| `test_selection_budget.py::test_tail_reservation_leaves_room_for_real_synthesis` | A/B 占候选槽后 C 仍在原总 attempts/预算内执行；hold→实际 subject 转移守恒；预算/次数不足拒绝展开；UNKNOWN 不释放、不按 0 结算 | A04/A08 |
| `test_selection_budget.py::test_bounded_probe_and_duplicate_fragments` | 反复改角色/重述/重复 source 不重置 deadline/no_progress/次数；到期按冻结规则选择完整候选或明确停止 | A03/A08 |
| `test_role_context_runtime.py::test_profiles_control_actual_wire_requests` | 不同角色实际收到不同受控材料；两 tokenizer/profile 所有多轮 wire 请求及 schemas 均在预算内，必需超限零出站；冷恢复原 request bytes/hash 不变 | A01/A06/A07 |
| `test_history_readback.py::test_actual_role_can_page_original_record_without_preview_recursion` | 实际 role toolnames/Provider 工具调用读到初始 preview 之后的表格，所有 cursor 回指原 seq/hash且推进；大单记录、中文/emoji、多页及紧预算不再预览递归 | A06/A07 |
| `test_history_readback.py::test_readback_cannot_cross_agent_or_upgrade_authority` | 跨 Agent/坏 hash/无绑定拒绝；历史注入被作为数据，不能调用未授权工具/升格审批；读取消后不新 handoff，真实已知 usage 可结算 | A06/A07 |

旧 FIRST_VERIFIED 的实际首 PASS 即完成 selector 与 code met/unmet 仲裁点击、P3.3 source/Claim/assessment/citation/human/UNKNOWN 原回归继续存在；这些 oracle 与 §5 的八 AC 一一补充，不以新增单测数量替代 §7 真实模型＋源码 UI 闭环。整个闭环仍须实际出现一次局部改图、一次跨分支复用、一次新综合再验收。

### 9.10 后续 patch 分界与冻结前待定项

| Vertical slice | 可准备但当前不应用的 patch 范围 | 冻结前裁决 |
|---|---|---|
| A：Context/角色/回读 | model_router profile、assembly 端口、context_builder 可见性、BaseAgent history schema/preview、实际 request receipt、Host 只读详情与相应 oracle | 精确 flash tokenizer/部署窗口资料；constraints projection 对现有 context 字段的显式白名单；后继 schema/prompt 注册 ID |
| B：局部修复/复用 | 现有 graph/Commit 内部原子组合、scope projection、真实验证输入物化、新旧 criterion 映射、fragment 只读投影与 oracle | 先选实际能证明闭包的 code/doc 正例；对不可确定 scope 的拒绝理由及显式新合同路径独立挑战 |
| C：比较/综合/接受 | candidate_selection、round/ready 索引和 replay、policy 后继解析、reserve 转移、scheduler 等待计数、final 单门、真实 Host DTO/UI | reserve 拆分至 Attempt/Verifier/Critic 的准确账户归属；ready lease 接管与 reconcile 逐窗口测试；新事件 migration/replay 方案 |

上述待定点是明确接口评审输入，不授权缩减 AC 或默认关闭已完成能力。实施前确定稳定 schema/version 与 oracle，不能用“先实现再看”消除 owner/source/合同/预算门。源码冻结期间只保留本文设计；不生成或应用 SDK/Host 生产 patch，不安装、不启动、不构建、不运行 pytest。
