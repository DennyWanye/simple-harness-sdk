# SimpleHarness Assurance 完整实施计划 1.1

**编号 ASSURANCE-EXEC-1.1｜2026-09-22｜替代 1.0 的正文、活动 Schema/SQL 与执行安排。**

## 0. 使用规则、范围与证据身份

这是 F01–F15 的一次性修订，不是生产补丁。`RESPONSE-TO-REVIEW.md`逐项给出裁定、改动资产、反例、关闭判据。旧 AER/OCC/OPS 原件仅在 inputs 中作为追踪依据；冲突以本正文为准，OCC 完成范围、Operation 身份与 D3 原 raw 恢复语义不变。HTN H1–H8 范围不缩减，NanoJev/Shadow/PR-7 不纳入。

本轮来源是收到的 handoff 和 `sources/LOCAL-SOURCE-EXCERPTS.md`：18 个文件的定点摘录及完整文件 SHA，不是全仓审阅或运行证明。SDK 候选是 `/Users/denny/projects/simple-harness-sdk-h1h-impl`、`codex/h1h-impl`、`102ad3d…` 加 dirty 内容；Host `/Users/denny/projects/simple_harness`。不得 reset/clean/rebase 或以远端旧代码覆盖；开发前 AS-0 重核涉及文件。迁移清单已报告到 24，实际登记取本地下一空闲号并冻结描述符，不能占用 TaskGraph 旧快照的 23。

已实现但未核验，与尚未实现分开记录。当前已有 scoped_content/scoped_composition/completion_status/completion_inputs/completion_support/root_review/composition_review/operation_proposal_review；不另建平行主链。来源表列 NEW 是授权实现目标，不要求先有运行 PASS 才准开发。一般名称、等价函数和编号由 WorkAgent 判断并留证，无需用户转问。

**施工纪律：主体和跨层接线完成前不批量跑单元、全量、回归或模型。**只允许针对明确阻塞的静态检查、单个反例/极小 micro-test；主体完成后统一验收。包内参考测试验证规格，不是 WorkAgent 编码时反复攒 PASS 的步骤。

**交付默认 ON：**完整实现和规定验收通过后，新 Mission 的 Assurance 默认开启，包含在同一交付，不额外等待操作人再批准一次。既有活动 Mission 不静默换协议；未完成依赖和 TaskGraph 自身门禁不被解除。应用模型从当前已批准 profile 读取，不改用户的 DeepSeek 应用配置；实施/核验子代理遵循 GPT-5.6 约束。真实模型未实跑不填 PASS。

## 1. 核心不变量与唯一 owner

1. 不修改旧 RequirementsRevision、ReviewPackage、Acceptance、GoalResolution、TypedRef 的 canonical V1；新增内部 side binding 使用本包版本。
2. 六个 ReviewPurpose 不增加；独立审阅用原 AgentBridge/dispatch/预算。新 coordinator 只是现有协调器的公共实现，不创建第二个 scheduler。
3. 内容/准备/效果沿 OCC Spec/Scope/Contribution；效果来源只经原 OperationCompletionReader，不重写 T0–T3 或 action 账本。
4. 所有正式 Review/接受、终态 writer 和所有源事实 mutation 都必须被接管。不能只改 UI、readiness 或某个 accept_result。
5. JSON 合法、源码符号存在、参考测试通过均不证明真实来源合法。缺数据具名失败，不补 true/空结果；合法空读有完整读取证书。
6. 旧 raw、费用、历史结论不因拒绝采用而丢失；三值 CHECKED 判定与 model grade 均不能把 UNKNOWN 升成 PASS。
7. 本机已提交的撤回立即使旧使用证书无效；未被本机观察的远端变化不在保证范围。
8. 备份不能证明自己包含备份之后的撤权。恢复必须新隔离根和新授权，绝不自动恢复旧访问许可或自动重发操作。

### 1.1 共同文件的单一集成人

WorkAgent 为本交付指定一个 integration owner，独占 `commit_service.py / resolution_commits.py / event_handler.py / schema.py / Host service.py` 的最终合并。OCC/HTN 先保留实际主链，Assurance 在同一位置接 guard；TaskGraph 尚未就绪的接缝保持独立门，不拉入本轮。其他 Agent 可给独立小模块/测试补丁，但不得并行各加一个最终 writer。

## 2. 当前源码的复用/修改/新增分区（F01）

详见 `implementation/integration-map.json` 和 `implementation/seams.json` 的 S01–S26。摘录中可确认的入口如下；未见正文的函数由 AS-0 定位，不把新名字宣称成存在。

| purpose | 当前生产起点/保留 builder | 本 profile 需要改动 | 正式消费 owner |
|---|---|---|---|
| TASK_CONTENT | scoped_content_review.read_task_content_projection；leaf_acceptance；原 VerifierRouter/Critic | 拆出投影结构读取与“已有 verdict”校验，允许正式独立审阅发生前构造 package；现有受信 Critic 可通过绑定适配，不重问同一审阅 | ResolutionCommits.accept_review + OCC contribution |
| METHOD_PLAN | 原 MethodProposal 准入/HTN Planner 与 Registry | 对需要独立审阅的草案建立原 ReviewPackage；结构检查不伪装语义 Review | 原 method admission writer，仅准入方法，不完成 Task |
| COMPOSITION | scoped_composition_review + composition_review | 保留子贡献/Scope 收集，接统一 transport、check policy 与 official guard | 原 commit_goal_resolution，先组合后接受 |
| ACTION_PROPOSAL | operation_proposal_review + 原 T0 Review | 接实际 payload/spec/owner；原正式 Review 决定是否可进入 T1 | 原 consume_action_proposal_review/materialize，不代用户审批 |
| OPERATION_OUTCOME | 原 operation_outcomes/OCC outcome binding | 先读完整真实效果链，再审指定 milestone | 原 accept_operation_outcome→accept_review；唯一效果 writer |
| MISSION_FINAL | RootReviewCoordinator.cut/record_review；_ask_root_reviewer/_collect_root_review | 替换本 profile 内 transport/collector 分支，保留旧 profile 原路径；每 round 独立 package | 原 root Resolution→本计划 closeout→原 Mission writer |

`RootReviewCoordinator.cut` 原来按历史材料生成 package 的做法在 profile 内委托统一 builder；不能同时原 cut 发一次、ensure 又发一次。`record_review` 的公开内部调用在 profile 下必须要求已验证 OfficialReviewSource；raw verdict 或 agent 字符串不构成来源。legacy 仍原行为。

### 2.1 S01–S26 的职责保持

S01 caller/factory；S02 Requirements；S03 Task/Obligation/Scope；S04 selected plan；S05 artifact/CAS；S06 pins/GC；S07 CheckSpec/policy；S08 checks；S09 review preparation；S10 dispatch/reservation；S11 catalogue/exposure；S12 official collector；S13 source lifecycle writers；S14 Observation；S15 rules/support；S16 complete snapshot；S17 validity compute；S18 atomic barriers；S19 scoped acceptance；S20 OCC effects；S21 root/terminal；S22 actual running work；S23 accounting；S24 cursor/pending consumers；S25 Host/native；S26 restore/recovery。

source-map 从附件 18 文件新 hash 初始化，旧 seed hash 移入历史，不用“仅 1 项相同”推导另外 5 项已审。来源不足只影响该 edge 的实现/验收，不阻止无关编码。

## 3. 内部 Ref 与精确 resolver（F02）

### 3.1 通用形状，不修改公开 TypedRef

内部 `AssuranceRef = {kind, pin:{id, revision, content_hash}}`；通用种类唯一源 `contracts/common.schema.json`。字段只允许 `implementation/ref-resolution-map.json` 规定的子集，禁止反射式 getattr resolver。

每个 adapter 必须返回 `ResolvedRef(body, pin, tenant, mission, issuer, state_witness)` 或 `SourceUnavailable(code, locator)`。以真实查询得到归属，不相信调用参数。校验 id、revision、hash、要求的用途和当前访问；过期拒绝用于新动作，但历史 raw/成本可导入。未知 kind、缺失、同 ID 异内容、跨 tenant/Mission、错误 collector 都有不同拒绝。

| 内部 kind | 持久源、唯一生产者与精确身份 |
|---|---|
| commit_receipt | 原 commit_receipts(commit_id)，body=原不可变回执完整 canonical body；revision=0 明确指不可变 body，不是 latest；真实 Commit writer |
| reservation_fact | 原 events(event_id) 的 `AssuranceReservationLinked`；正文含实际 ledger reserve 身份、账户/责任、subject、金额上限、原 reserve receipt 引用；create_service_intent 成功的同事务记录。它是桥，不是余额 |
| agent_turn_receipt | 原 events(event_id) 的 `AssuranceReviewTurnImported`；原 execution collector 经 profile+intent+agent+turn+input/output hash 核对后导入。正文含 original immutable runtime receipt exact ref；不复制可变 turn 行充当原回执 |
| execution_receipt | 原 events(event_id) 的 `AssuranceExecutionImported`，受信 execution importer 以 provider/call identity、实际结果/费用来源核对；review/read 不直接跨库做原子写 |
| local_check_receipt | 原 events(event_id) 的 `AssuranceLocalCheckFinished`；§5 的同步 recorder 在实际本地 checker 执行之后导入真实结果，含源码/环境/目标和 assertion；不可从 API 提交 bool |
| check_binding | assurance_check_bindings(check_binding_id)，正文 binding_json、binding_hash；revision=0；writer 只接上述两种真实检查来源 |
| check_spec | 已注册 checker 的不可变 `AssuranceCheckSpecRegistered` 事件 body；id=event_id，revision=0。声明并非检查结果，registry 安装时由固定系统 owner 写入 |
| check_policy | assurance_criterion_policies(policy_id)，批准映射 body+hash，revision=0；引用精确 requirements revision 与 scope |
| disclosure_receipt | `AssuranceEvidenceDisclosed` 原事件；由实际 reviewer Provider 输入/消息 manifest importer 写入，必须证明已包含相应消息且先于结论输出 |
| review_package | 原 review_packages(package_id) 的完整不可变ReviewPackage body；原六purpose builder产生，所属Mission/round/purpose精确匹配，revision=0不表示latest |
| result | 原 ResultEnvelope 的不可变正文，非 verification_state 可变包装；ResultID+body hash，revision=0 |
| task / requirements / method / method_instance | 原精确合同/版本/草案 body；Task不使用心跳 row version。MethodInstance 读取明确 instance 与 plan pin，不查最后一个 |
| artifact / input_manifest | 原正式 metadata+CAS/manifest 完整字节，读 nofollow/hash；不把 URI 当权限 |
| source / observation / review / acceptance / resolution | 原正式 source/observations/review_records/acceptances/goal_resolutions 的精确 body；另查当前可用性，不改历史 body |
| completion_scope / completion_spec | 原 OCC Store 的精确记录；归属、requirement hash、plan occurrence 使用原 OCC reader |
| operation / tool_receipt | 原 OperationEnvelope/action-link 或原执行回执；Assurance 只读取和核对，不签新 operation id |
| policy / authority / capability | 原部署冻结政策/真实授权 receipt/adapter profile；静态声明不作为结果证据；当前生效另核查 |

事件类 Ref 的 hash 覆盖**原 Event 的 immutable body**（原 Event.to_json 对应固定存储序列化，包含实际 event_id/type/mission/payload；不得剔除不喜欢的字段）。seq 若属于原 Event canonical 合同就保持，不重新定义 SDK hash。确切函数由 AS-0 登记。事件导入时同原来源同 body 重送幂等；同来源异 body 拒绝。

### 3.2 无循环的创建顺序

注册 CheckSpec 和批准 policy → 为本 round 分配稳定 review_key/package_id → 取得 CAS pins → 外层 Store transaction 中创建原 ReviewPackage、真实 reservation 与 service intent → 记录 ReservationLinked 事件 → invocation binding → ensure receipt。Binding 自身不引用自身 hash 或未来 official record。

收包时先持久实际 runtime receipt/raw/费用，再生成 TurnImported → official ReviewRecord → record side binding。已完成调用的 reservation 可以已 SETTLED；导入历史 review/费用不要求它还 ACTIVE。**只有创建新 invocation**才需要新的真实预留，不得拿已结算旧 reserve 复用。新 invocation 并不新建预算账户，仍累计同 owner/Obligation。

## 4. 证据标签、曝光与冷恢复（F03）

唯一标签算法：

```text
label = "ev-" + sha256(canonical({kind:"assurance-evidence-label-v1", review_key, ref}))
```

使用完整 64 hex，不截断；同 ID 不同 kind/revision/hash 得到不同标签。build_catalogue 在碰到同 label 异 ref 时拒绝。初始 catalogue 按 label 字典序，hash 覆盖完整 label→exact ref 数组，保存在 review binding；重复 exact ref 可规范去重，标签不靠数组位置。

**目录中有，不等于该 reviewer 已看到。**

- 初始曝光：invocation 的实际原 Provider input_hash 与 frozen catalogue_hash 对齐；原 collector 取得确定请求 receipt 后，生成 disclosure batch 0。
- 追加证据：只读工具 `assurance_read_evidence` / `assurance_find_evidence` 通过原 ToolGateway，先按当前权限读/取证，再返回带 label/ref 的内容。写 append-only exposure batch；可用于结论的证明须来自**后续模型请求实际包含该 tool result/message**的原输入 manifest。仅 Tool 函数曾返回但未进入模型输入不算曝光。
- 同 turn 内工具消息由真实 Agent Journal/message ID 和 Provider request receipt 关联；若原 runtime 无该信息，在 runtime 的原请求冻结 hook 增加 manifest import（不改旧 input bytes）；缺证明保持 UNEXPOSED，不用“很可能看过”代替。
- 每个 batch 固定 previous_hash、review_key、reviewer_agent、实际 turn receipt/input hash、message IDs 和 receipt。批次同号同体重放不新增；异体冲突。初始与追加同 exact ref 用同 label，不重新编号。
- 格式修复 invocation 可使用相同已曝光材料，必须在其新实际 input 中包含 catalogue/材料或带合法精确回读工具；旧 Agent 私有上下文不可自动继承给新 Agent。
- official collector 只接受**产生该结论的 Agent/turn**实收曝光集合中的 evidence_ids。Replay 从原 raw+binding+disclosure chain 恢复；不重新检索 latest，不改原 Provider input_id/hash。

追加 fetch 仍受真实工具预算、scope、只读边界。根 reviewer 目前 tool_names=()，本 profile 新模板必须显式装配上述两工具（非万能浏览/写工具）；旧 profile 保持空集合。

## 5. 批准的 check-policy 与三值 oracle（F04）

### 5.1 唯一批准 writer

`CommitService.approve_assurance_check_policy(requirements_ref, completion_scope, candidate_mapping, caller, command_id)` 在原 Requirements 细化/批准事务中写 assurance_criterion_policies。政策域绑定 Requirements hash+scope hash；一份批准不能被后来的 builder 改 mode。

无损旧适配：required_check_ids 非空→CHECKED，单个 AND group 包含全部精确 CheckSpec refs；有批准的替代组才可 OR。没有 required_check_ids **不自动** SEMANTIC：只有原明确 evaluation_kind=语义审阅且 policy 授权这一用途时才能 SEMANTIC。未知 evaluation_kind、找不到 CheckSpec、checker 不部署→CHECK_POLICY_UNRESOLVED，不删除要求。对 TASK_CONTENT 使用现有 scoped projection 的 criteria 和证据政策，不能因为旧 LayerResult 没有通用 execution_ref 就永久拒绝。

### 5.2 本地检查真实 recorder

`VerifierRouter.verify(..., recorder=...)` 的原本地 format/rule/citation/code_test 分支保留算法，外包 `record_local_check_run`：运行前固定 checker digest、subject/input/env、run identity；事务外真正执行函数/受控 subprocess；结束记录实际 state 和逐 assertion 输出 hash；原 Commit 导入 `AssuranceLocalCheckFinished`。函数抛错也记 ERROR，不签 PASS。单一CheckSpec固定一个assertion_key；normalize按exact CheckSpec+subject选择该断言，不按任意成功条目。ERROR/CANCELLED允许实际assertions为空；SUCCEEDED缺少指定assertion为UNKNOWN，重复同key拒绝；不为满足Schema捏造测试输出。

format只证明格式；citation只证明指定来源段落；code_test 的 receipt 必须含实际 nodeid/平台/环境/目标命题，exit0 不单独代表内容正确。若 code_test 原本已通过 executor，则使用实际 executor receipt；不重复本地执行。断电前未持久 recorder 结果仍 UNKNOWN，可对无副作用检查按原政策重新运行，不能根据 cache 猜回 PASS。

### 5.3 精确真值表

执行记录 SUCCEEDED 且 assertion PASS→PASS；SUCCEEDED/FAIL→FAIL；SUCCEEDED/UNKNOWN→UNKNOWN；ERROR/CANCELLED/NOT_RUN/RUNNING、receipt缺失/过期/错范围→UNKNOWN。畸形的 receipt 与错身份直接拒绝导入，而不是改成 PASS。

CHECKED 的每组是 AND，组间 OR；SEMANTIC 的 check gate 是 **NOT_APPLICABLE**，既不是假 execution PASS，也不需要伪造 check receipt。

| model grade | check gate PASS | check gate FAIL | check gate UNKNOWN | SEMANTIC/不适用 |
|---|---|---|---|---|
| PASS | PASS | FAIL | UNKNOWN | PASS |
| FAIL | FAIL | FAIL | FAIL | FAIL |
| UNKNOWN | UNKNOWN | FAIL | UNKNOWN | UNKNOWN |

准则 BLOCKER 强制该准则 FAIL；全局安全 finding 必须映射 mandatory，无法映射则拒绝整条回复，不能藏在未选分支。所有有效 grades 计算后，再求成功公式；ALL任一FAIL则FAIL，全PASS则PASS；ANY任一PASS则PASS，全FAIL则FAIL；其余UNKNOWN。mandatory单独ALL。公式所选成功 witness 按冻结公式顺序确定，不用模型打分。

`decide_review` 返回 acceptable、effective_grades、success_witness、实际 consumed receipts、reasons，代码在 reference/protocol_v11.py。模型最终 verdict 不是 ACCEPT 时，不因公式通过而自动批准。失败组可不阻塞已成功替代组，但其反证仍在完整候选集合/有效性中核对。控制权限/hash/Scope 独立硬门永远不受 ANY 绕过。

## 6. 六类 Review 真实切换、预算和调用身份（F05）

### 6.1 选择有父子关系的多 intent adapter

**唯一默认：一个 review_key/round → 一个 ReviewPackage → 至多两个 transport invocations。**格式修复不是新语义 round；不同独立二审必须显式新 round/package。旧 review_records 每包唯一 official 约束保留。

```text
subject_id = mission_id + ':assurance:' + review_key + ':' + ordinal
creation_key = subject_id
input_id = 'assurance-input:' + review_key + ':' + ordinal
kind = 'plan'（沿当前 service dispatch 类型）
config.role = 原对应 reviewer role；新增 assurance_protocol/package/review_key/ordinal
```

通过原 `CommitService.create_service_intent` 创建，owner依原 account_for_purpose：task/mission_planning/parent_compound_task/operation_task/mission。其 reserve、intent、Package、side binding、event、ensure receipt 必须使用同一 orchestrator.db Store connection 外层事务；若原方法自开事务，提取 `_create_service_intent_locked` 保留旧wrapper，内层不commit。不是跨 execution.db 原子。真实 Agent 创建/提交由原 dispatch consumer 执行，call账本后导入。

| 类型 | trigger → coordinator/builder → transport → collector → next |
|---|---|
| TASK_CONTENT | 原 result可审→scoped_content projection/原Critic准备→统一 invocation→actual critic collector→accept_review；原已完成且精确冻结的 Critic 可注册为 import adapter，不额外重复模型 |
| METHOD_PLAN | 原 MethodProposal需审→registry/planning review builder→invocation→official method review collector→原方法准入 |
| COMPOSITION | 原 children可组合→scoped_composition/composition coordinator→invocation→official组合collector→原 parent resolution |
| ACTION_PROPOSAL | T0原 intent→operation_proposal_review→invocation→official操作候选collector→原T1 |
| OPERATION_OUTCOME | 真实 outcome已导入→原OCC outcome builder→invocation→official效果collector→原accept_operation_outcome |
| MISSION_FINAL | root readiness→RootReviewCoordinator.cut委托builder→_ask_root_reviewer新profile adapter→_collect_root_review新collector→root resolution/closeout |

现有 APIs 对新 profile 不得接裸 verdict 写 official；所有 accept/record 路径验证 `assurance_review_record_bindings` 和 invocation+TurnImported。legacy 根据持久 lane 分派，不改旧 prompt/hash。

### 6.2 轮次状态表

| 事件 | review_key/package | intent/reserve | 结果 |
|---|---|---|---|
| 相同通知/command重送 | 同一 | 同一 | 回原receipt，不重预留 |
| 首次合法请求 | 新round | ordinal1 | 真预留后派发 |
| 可解析 REWORK/INCONCLUSIVE | 当前round已official | 不自动ordinal2 | 返工/有界处理，不当格式错 |
| 不可解析且原调用已确定结束 | 同一package | ordinal2+新真实reserve | 携带固定格式错误，旧费用仍算 |
| 原 Provider UNKNOWN | 同一 | 原intent由runtime核对 | 不能靠ordinal2重复请求 |
| 获准独立二审或内容返工 | 新round/package | ordinal1 | 同责任累计预算与round上限 |
| 另一invocation晚到 | 原raw/费用保存 | 不另占official | 原包已有official则冲突/历史，不覆盖 |

格式修复上限1次；round上限用原冻结责任政策，不新建无限循环。`assurance_review_bindings`不再含唯一dispatch字段；新增`assurance_review_invocations`保存ordinal与实际intent/reserve。collector找到实际 invocation，不能用task ID随意关联。

## 7. 接受与全部终态 writer（F06）

### 7.1 保留并包住已有主链

| writer/reader | 新profile行为 |
|---|---|
| `accept_result` | 原结果、费用、artifact保存照常；内容接受经原 scoped/leaf 路径＋assurance guard，不建第二份 Review；不直接签效果 |
| `ResolutionCommits.accept_review` | 原OCC checker仍执行；加official transport、typed checks、UseCertificate复核，Acceptance和Contribution同事务 |
| `commit_goal_resolution` | 原OCC Scope/effects闭包仍唯一；要求完整Assurance bundle，直调也不能绕过 |
| `completion_status/inputs/support`、`accepted_outputs` | CONTENT/PREPARATION按用途可读；MIXED未满足效果不被任何CURRENT接受代替 |
| RootReviewCoordinator.record_review / 原其它record writer | 新profile必须由统一来源collector调用；裸dict/自行official拒绝 |
| DeliveryReceipt writer | 继续原OCC T3精确绑定；不能凭非空OperationId完成 |
| `CommitService.judge_mission` | 新profile只读取root有效性并请求closeout，不直接COMPLETED、不提前调用release_terminal_pools；legacy保持 |
| 其它 cancel/fail/timeout/terminal cleanup | 所有终止仍收敛原执行并保留UNKNOWN/hold；不得通过“失败”丢掉未决真实责任 |
| `Store.update_task/update_mission`底层 | 只有合法Commit caller；AS-0枚举所有terminal写入点，测试证明无新profile旁路，不按某个文件名当完备 |

**最后唯一 writer**：从当前 `judge_mission` 的成功末段提取 `_finalize_assured_mission_locked(validated_closeout, caller)`，在原 connection 做 `next_mission→update_mission→安全release已知terminal pools→MissionCompleted`。此方法仅接受同事务新生成的 validated closeout，不公开为模型/API工具，旧judge不得构造假的许可调用。

### 7.2 Root 满足与 closeout 中的状态

root GoalResolution/对应Task和Obligation可记录历史“业务要求已满足”；Mission 仍 ACTIVE，`assurance_closeouts.state=DRAINING/BLOCKED_UNKNOWN`。调度将其视为 CLOSEOUT_PENDING，不开新Worker、不用no-progress将它误判FAILED。原operational责任、generation、lease、UNKNOWN必须真实收敛，不以Task终态判断。

默认成功策略：危险/必需效果UNKNOWN→BLOCKED_UNKNOWN；实际写者未收敛→DRAINING；当前依据失效→NOT_READY；费用未结算→DRAINING。只有原批准政策明确允许“结果已知、费用待导入且hold继续存在”时，使用KEEP_HOLD_PENDING，release函数必须跳过这些hold；不自行新增免费/零费用行为。

closeout只能INSERT NOT_READY/version1；重评转READY；最终事务重读当前epoch/权限/effect/运行集合才READY→FINALIZED。Task/Mission历史不回退。新要求走原后继机制，不能DELETE FINALIZED再创建。

### 7.3 通知选定实现，不假设通用 outbox

本轮使用**原持久 Event + 本计划 pending_work 的 NOTIFY 行**作为本地状态通知意图，不声称已有万能outbox。最后事务同写 MissionCompleted、`AssuranceStatusNotificationRequested`、稳定NOTIFY工作和原receipt。原Orchestrator tick→Host当前事件推送链发 `{mission_id,event_id,state_version}`，不夹敏感报告正文；Host按event_id去重、重连从seq补读。

发送前当前读权/恢复门仍检查。网络重复发送可能发生，用户已读无回执就UNKNOWN；SQLite与UI/外部邮件不是exactly-once。用户明确要求外部交付仍走原Operation，不能通过这个本地通知完成外部效果要求。

## 8. 证据屏障、读集、时效与活性（F08）

### 8.1 最小正确性策略：Mission聚合 epoch＋全局已导入访问/政策 epoch

现有 `validity_epochs(mission_id,scope_id)`保留，新增mission聚合分区由factory真实创建；无行不当0。`assurance_environment_state`只保存全局epoch、时钟代次/高水位，不保存授权内容。

| 实际 mutation 入口 | 来源库 | 同事务屏障 / 影响范围 |
|---|---|---|
| Store.put_source 及source替代/撤回/删除metadata/tombstone | orchestrator | 变更归属mission聚合epoch；归属集合不能完整证明或跨Mission共享时global epoch，宁可全局重核 |
| HtnStore.insert_observation | orchestrator | 幂等首次插入才 bump 本Mission；正反证同等 |
| insert_justification_set / support member / rule admission与撤回 | orchestrator | 原规则/成员与聚合epoch同事务；不能只在reader装监听 |
| Requirements、OCC Spec/Scope正式变化 | orchestrator | 本Mission；来源/要求hash作为旧证书依赖 |
| Method/Task adopted input或contract变更 | orchestrator | 本Mission，与原PlanCommit同事务 |
| ACL/当前policy/permission mutation | 原权限源；若本机orchestrator则同事务global epoch | 外部权威则其真实import事务bump；交接/披露若要求远端现值须再查当前authority，不能假装跨库同步 |
| operation更正/反向证据、execution结果import | execution先存；orchestrator后导入 | 以真实import receipt和屏障同事务为本机线性化点，不能预见尚未导入事实 |
| artifact删除/访问状态、更正Check/Review有效性 | 原metadata与lifecycle writer | 在正式metadata mutation事务 bump；CAS unlink只能在所有live根和pin检查后，不能绕gate |
| scope/grant撤回 | 原orchestrator grant writer | 本Mission+原local epoch；全局权限变动global |

下层 Store/HtnStore mutation 处调用 `_mark_assurance_change_locked(change_kind, mission_set, source_event)`，不在最后某个orchestrator回调才加；所有直接SQL writer经AS-0扫描列入owner表。严禁无审阅的生产SQL直写。对reader源码存在不予“writer覆盖完整”认证。

### 8.2 CompleteRead 和查询键

`CompleteRead`必须包含分页结束/SQL全结果、原schema/version、精确scope、count、canonical集合摘要、捕获的两级epoch；缺表/失败/没读完不能COMPLETE。完整空集count0且有epoch和摘要。

命题键不拼接模糊文本：`canonical({predicate:{id,version,hash},typed_args,namespace,scope})`；args 类型检查后编码；不将 1/true、缺失/null 混同。read_set item的 `(channel,key)` 唯一；相同key即使同fingerprint重复也拒绝，避免各层对去重规则不同。

- OBJECT key=canonical `{kind,id,revision}`，fingerprint=原body hash。
- QUERY_SET key=canonical `{reader_version,mission,scope,query_kind,proposition_keys,polarity:"BOTH"}`；值为完整候选ID/rev/hash及准入/撤回排序摘要。包括未被模型引用的新反证与不采用分支。
- ACCESS key=canonical `{principal,tenant,scope,use}`，fingerprint=真实授权/ACL/恢复grant绑定；非本机新授权不得从旧cache宣称当前。
- POLICY key=canonical `{policy_id,version,use}`，fingerprint=完整冻结政策。

按channel/key排序，hash原canonical数组，拒绝重复和未知通道；模型 reason_refs不是安全读集。

### 8.3 计算与最终写锁

使用原正负支持最小不动点及clean closure；不能从已VERIFIED缓存开始。默认限10,000 literals、20,000 rules、1,000,000 premise visits、250ms CPU预算，任一先到→EVIDENCE_EVALUATION_INCOMPLETE；严禁截断后COMPLETE。真实实现使用worklist降低重复扫描，不需自研新planner。

读取候选元数据的一致读快照→事务外读CAS/计算→短写事务比较mission epoch/global epoch/clock generation/受影响精确对象+实际caller+expiry。默认**不在写锁内扫描20,000条候选再求closure**。epoch未变才使用已计算fingerprint；变了返回RECHECK_REQUIRED到pending工作，重新完整读取。正确性由所有writer屏障保障；索引只是优化。

证书最大256KiB，read_set至多20,000只是独立上限，哪个限制先到就以具名limit拒绝；不承诺两上限可同时用满。证书含root incarnation，不能跨恢复根复用。

### 8.4 expiry 与时钟

`[issued_at_ms,not_after_ms)`；certificate expiry=所有必需source/policy/current authority的最早界。需MAINTAIN时必须有实际持续监测/锁/fence和coverage interval，点状采样不冒充连续；缺能力阻断MAINTAIN用途，不阻断合法历史审阅。

唯一计时owner是原Orchestrator tick的VALIDITY pending worker：发证/缓存同事务 upsert该consumer最早expiry wake；启动扫描所有未过期证书/dirty对象重建due，无需等新业务事件。证书即时使用总是检查now，UI即便timer延迟也不可放行。

Store服务clock wrapper维护持久wall_high_ms；now<high且clock_state=STABLE→clock_state改为ROLLBACK、clock_generation增加并记录一次TimeDiscontinuity；ROLLBACK期间不重复增代次，新用证书全部重核，绝不把deadline延后。高水位只增；now重新达到高水位且当前时间检查通过时，clock_state恢复STABLE，保留递增代次。受信修时命令也不得降低旧高水位或延长已冻结期限。直到本机时钟恢复可信，时间敏感START/MAINTAIN/ACCEPT/DISCLOSE拒绝，raw/费用和不披露诊断继续。超lease只回收协调owner，不证明外部动作停止。

连续RECHECK_REQUIRED不占模型格式重试；3次即时重算后wait 500/1000/2000ms，最多32次/300s将pending行保持WAITING并置wait_reason=MANUAL_REQUIRED（仍不放行，due查询排除此原因，须真实新条件或授权恢复命令才能唤醒）。新源事件唤醒同logical work，不新建无限job；原累计预算/截止保持。

## 9. 事件摄取、准备、提交和ACK（F09）

唯一选择：**持久 pending inbox＋原Orchestrator tick**，不再提供“或 startup 随便扫描”的二义实现。新增 assurance_pending_work 不执行Agent，实际派发仍原dispatch_intents。

`pending_work.target_epoch` 是**工作目标版本**：取触发该工作的已持久 Event.seq（激活扫描取activation seq），不是validity_epochs的授权/真假代次。后续相关事件seq更大即可合并唤醒；相同seq异fingerprint是冲突。提交仍独立复核真正mission/global validity epoch。这样即便仅预算/运行状态变化也不会因真假epoch未变漏唤醒。

四consumer REVIEW/VALIDITY/CLOSEOUT/NOTIFY各自cursor。cursor只表示“事件已可靠分类入队”，不表示昂贵处理已完成。

1. 读一页原事件，不推进cursor。
2. 短事务核cursor CAS；无关/自己状态通知只推进；相关事件稳定work_key插入或合并到pending（REVIEW按review slot，VALIDITY按consumer/use，CLOSEOUT按mission/root要求，NOTIFY按最终event）。同事务推进cursor。禁止先ack再内存future。
3. tick以row-version/lease claim due work，事务外准备CAS/check/closure。原authority来源变更事件仍继续摄取，预算堵住一个review不会卡后续撤权/取消。
4. 提交事务重读work target epoch/fingerprint/caller/source；有效则原效果writer+receipt+DONE同事务。已存在command receipt时仍执行幂等工作ACK，不early-return让cursor死循环。
5. BUDGET_WAIT/CHECK_PENDING/RECHECK_REQUIRED保存WAITING+next_due/reason；永久非法保存REJECTED和receipt；profile未绑定/restore隔离保存等待来源，不触发模型格式重试。
6. 准备后source变化→原事务无业务变更，row若已被更高target epoch合并则旧worker不能ACK；由同work下一轮处理。不用新event ID创建重复预算。

新lane激活同事务cursor=activation_event.seq，同时枚举当前已存在可审Result、Review回流、dirty证据、未收尾root入pending，形成reconciliation manifest/hash；因此不会漏激活前任务，也不用再次跑所有历史事件。cursor丢失：恢复为0重新ingest，依据稳定workkey/原receipts去重；已完成同target保持完成，不重新预留。源记录也缺则SOURCE_UNAVAILABLE，不“从最后seq继续”漏活跃工作。

self events（WorkWaiting/CursorAdvanced/NotificationAck）在分类表为IGNORE，不生成新的同类事件。有限page每tick≤128、每consumer≤8个work，防止一个Mission饿死其他Mission；按原全局调度预算约束，不独立线程。

## 10. 旧备份恢复与当前授权（F07）

### 10.1 采用隔离恢复＋当前重新授权；不建立全局撤回平台

`storage/offline_backup.restore_offline`保持不外查、不运行Agent、还原到新目录。完成备份字节校验后，在暴露target之前生成**新的 root_incarnation_id**与 `restore-quarantine.json`；不从备份拷贝可用的根信任。旧backup内grant/证书只是历史。

新root启动第一步是 `open_assurance_root_gate`，早于API全文/搜索/summary/Context/通知/下载/调度。marker缺失、不匹配、部分库、校验失败→QUARANTINED。新空root由当前已认证create-root初始化；非空未知root不能当“新空库”。这只承诺应用管理的restore与startup边界；持有本机任意文件写权限的对抗者不在anti-rollback保证中，不声称可检测所有恶意磁盘回滚。

### 10.2 谁能重新授权

`MissionControlV1.reauthorize_restored_read`→当前Host固定Principal与tenant的认证入口（不能使用备份中的session/token）→`CommitService.reauthorize_restored_read`。命令绑定新root id、restore manifest hash、精确允许对象/范围、用途、TTL和当前policy。它是新授权事实，不是声称知道t1曾发生何种撤回。

来源仍受外部所有者ACL控制的资料，必须有当前权威确认；当前用户无权重新授权其来源时保持阻断。用户明确拥有的本地文档可通过当前明确确认获得新的READ_ONLY_REAUTHORIZED许可；不存在当前认证权威时只有非披露诊断，不能用测试注入最新watermark冒充真实producer。

新授权先在恢复库提交真实receipt，再将不含秘密的root grant封装原子写到本安装的受保护根状态文件；读门要求文件与库receipt/newroot完全一致。两者之间崩溃保持QUARANTINED，按同命令幂等补文件。文件与DB不是跨媒介ACID，fail-closed处理窗口。备份导出排除这个可用根grant；只记录历史恢复来源。

授权默认24h或当前政策更短，精确读取允许集合；到期重获当前许可。只授权披露，不自动允许续跑旧模型、写工具、旧approval或 UNKNOWN Operation。执行恢复必须原H1/OPS当前授权+真实核对；不因读门打开自动重发。

### 10.3 允许的非披露诊断

QUARANTINED可返回schema版本、库完整性状态、记录数量、匿名阻塞码、需要当前认证的信息；不返回原任务标题、报告、摘要、token、来源路径或通知正文。t0备份→t1撤权→t2恢复，无外部新授权时无正文披露；原库丢失同样。只恢复某一库且manifest不完整保持隔离，不补latest数据。

## 11. Lane、首次启用和默认 ON（F10）

新增 `assurance_creation_contracts` 是不可变创建协议判别，不是授权表。新factory在原Mission创建事务固定 lane；新lane需要真实系统activation receipt和assurance binding，一项缺失就错误，不fallback。

| 持久创建lane | 规则 |
|---|---|
| LEGACY | 只在已认证原历史创建记录/升级分类证明旧协议时，保持原行为 |
| COMPLETION_V1 | 既有 planning-decision-v1/OCC但未启用Assurance；保留现有OCC链，不偷偷增加新profile |
| ASSURANCE_1_1 | factory新交付默认；同事务真实activation/binding/epoch/cursors；缺binding→ASSURANCE_PROFILE_UNBOUND |
| 缺classification/来源矛盾 | CREATION_CONTRACT_UNRESOLVED，不能以assurance表缺行当legacy |

升级只按迁移前已有mission_planning_protocols+原创建事实分类历史任务，保存migration-classification receipt，**不授新权限**。新版本创建的行在factory同事务分类，不能落入历史推断。明确从旧Mission创建后继才用新profile，历史hash保持。

默认设置集中于原Orchestrator Mission factory的 `default_assurance_profile_for_new_mission()`；完整交付验证后返回注册assurance-exec-v1.1，不是环境变量控制的永远OFF。主体阶段在隔离候选测试指定profile，最终交付同批将新factory默认ON并验证；不为生产再加一次用户确认。风险动作原用户审批与TaskGraph独立开关继续有效。

## 12. SQL、codec、引用资产一致性（F11/F12）

`sql/assurance_additive.sql`是**完整1.1增量目标**，替代尚未正式部署的1.0附件。若本机已实际登记1.0，保留旧checksum并写显式后继迁移；不可覆盖旧条目。AS-0验证真实完整schema后选择下一空闲号。

九个原side tables保留职责；新增六个窄side tables：creation_contracts、criterion_policies、review_invocations、disclosure_batches、pending_work、environment_state。没有另一套任务、真假、action结果、余额。预算/本地check/execution桥复用原事件。SQL主键/FK/immutable/状态保护和queries全附。

数据库直接负责：closeout初始NOT_READY/v1；只能READY→FINALIZED；所有closeout禁止DELETE/REPLACE；pin初始PREPARING/v1，BOUND必须同Mission真实review binding；pin不可变对象/来源、RELEASED不可重开；不可变side记录禁止UPDATE/DELETE/REPLACE；队列/游标CAS单调。

Store负责不能仅靠SQL做到的：canonical_json/hash重算、原CommitReceipt属于真实issuer/Mission、pin blob真实存在、ref revision/body、checks真实性、pending目标证书、当前授权。不得称作数据库已经保证了这些。

正常备份restore按物理SQLite backup还原含终态行，不逐条绕trigger插入终态。新empty schema重建投影必须用原历史初始事件和合法转换重放；如历史源不足标INCOMPLETE，不能临时DROP trigger。不得手写DELETE再重建FINALIZED。

### 12.1 一致性唯一源

共同Ref/pin/read_item/目录entry在`common.schema.json`。字段表不复制nested_schema，改用 `schema_file + JSON_pointer + resolved_subtree_hash + producer_rule`。`tools/check_plan.py`递归解析本地$ref，并检查全部properties/required/field-map/ref-resolver、生成fixture和SQL-column map；人工加删nested kind/字段不更新映射会失败。DDL列映射由实际DDL的PRAGMA列清单生成并由逐表writer规则绑定；不靠9/26/48这种数量断言代替内容。

严格JSON先UTF-8/bytes≤256KiB，重复key/NaN/Infinity/溢出/孤立surrogate/bool整型/unknown字段拒绝；JSON容器深度64（根0），formula逻辑节点深度16（根0）、512节点、256准则。formula数组/object包装深度不与逻辑深度混算。set字段查重后canonical排序，formula分支保序。长raw由原CAS保存，拒绝解释后不丢真实费用。

`CheckPolicy CHECKED` 非空OR组且每组非空；SEMANTIC组为空；authors默认必须非空，METHOD_PLAN无可证明作者时回SOURCE_UNAVAILABLE而不是绕独立性；findings.reason和assessment.reason同限2000。`reference`可运行这些规则，但真实resolver/权威事实仍由SDK接线提供。

## 13. Host wire、隔离载入、原生验收（F13）

### 13.1 三个读verb（下划线，不用点号）

| verb | handlers.py _ACTIONS | service.py → SDK |
|---|---|---|
| mission_assurance_snapshot | 新薄委托 | `.assurance_snapshot(body)`→固定 `_control` 新同名read委托→AssuranceApi.snapshot |
| mission_assurance_review | 新薄委托 | `.assurance_review(body)`→AssuranceApi.review |
| mission_assurance_use_check | 新薄委托 | `.assurance_use_check(body)`→只读diagnostic，不签可执行证书 |

DTO完整见`contracts/host-*-v1.schema.json`。请求无tenant/principal；沿 service.py:303–305 构造的MissionControlV1，body无法变更caller。snapshot/review最大100项；CURRENT返回同一seq切面的历史事实和当前use状态，HISTORY需明确at_event_seq但披露始终按**当前**权限/restore gate。返回hash/body字段不带secret。

cursor=base64url(canonical `{version,mission_id,view,at_seq,filter_hash,last_sort_key}`)，不是授权；每页重新caller guard，sort=(kind,id)稳定。current epoch/seq变化不混拼页面，返回SNAPSHOT_CHANGED从第一页读；history页仍固定seq。超限给next_cursor/truncated=true，不把页当完整支持集合。

`projection.py`只映射正式状态，不重推一次完成。`MissionsView.tsx`接现有store/传输层，显示criteria/review/pending_effects/closeout/current-vs-history；非法DTO保留上次有效画面并显示协议错误，不填0/空列表装成功。当前关联store文件由AS-0定位真实import边，新增adapter不另开WebSocket。

### 13.2 候选原生装载的唯一默认

采用**隔离 Host 源码副本＋独立 venv＋独立 userdata/端口＋固定候选 wheel**，不安装到共享Host。包自带 `tools/prepare_native_manifest.py`校验已准备好的隔离目录、端口、wheel与指纹配置并生成manifest（只读核对，不安装、不启动）；必须冻结 SDK dirty源→原构建命令生成wheel→记录wheel hash→在隔离Host的 `backend/deskpet/sdk_adapters/sdk_candidate.py` 和 pyproject vendor路径绑定同一wheel。不只改版本号。

实际工程source装载由原固定SDK adapter验证；隔离venv用原项目uv锁解析，不自动变更共享依赖。需要本地wheel锁更新仅在隔离副本执行，记录lock差异；安装/构建本来就是AS-4获准步骤，不因共享Host禁改而永远不装候选。

原生启动只执行隔离 tauri-app/package.json 中当前 `tauri dev` 的实际script；Tauri自管backend/vite，不手动再启动第二份。隔离副本配置 beforeDevCommand/devUrl/backend参数，使用Manifest固定端口如 17841 backend/15173 vite（冲突则退出选新完整manifest，不悄悄连接旧服务）。userdata仅指本run新目录，不使用生产DB/凭据自动复制。

启动必须记录 `simple_harness.__file__`、distribution version、wheel+模块sha、PID/backend地址、userdata、Host和SDK指纹；网络确认该UI连接本backend。图上版本相同但字节不同不可混用证据。

### 13.3 原生场景

实际点击：新Mission→review详情→待效果/closeout→断线重连→历史与当前切换→scope撤回→冷恢复根隔离。截图/日志与event/request ID和指纹关联；接口pytest不能替代点击。多用户/跨tenant负例SDK/Host接口均验证，UI不因是否找到ID泄露对象存在。截图/log留ignored，Git只相对索引hash。

## 14. 完整施工顺序与 BODY_WIRED（F14）

### AS-0 合同/来源固定（非SDK验收前置）

核18文件当前hash、六purpose实际入口、terminal/事实writers、迁移链、原reserve/dispatch事务、Host import与构建命令。integration map里的新方法按目标实现；普通改名不用请示。正文中的政策/状态/归属不留实现二选一。

### AS-BODY 主体集中实现

1. 内部codec/ref-map/check-policy/labels/SQL和持久创建lane。
2. AS-2的必要子接口先写：complete snapshot、typed checks、epoch barrier、restore gate和certificate checker；不得用True占位让AS-1先通过。
3. 复用六purpose builders、统一多intent transport、可信localcheck/实际execution import、官方collector/曝光、scoped接受与全部终态guard。
4. cursor→pending阶段协议、expiry/恢复、closeout最后writer、本地通知。
5. Host三个读verb/UI、隔离装载入口、factory默认设置路径、architecture/status回写。

此期间仅对具体编译/SQL/接口阻塞作最小定点检查，不运行成组SDK、全量、回归、mutation或模型来累计通过数。已知设计中的新Module可以直接实现，不能因还没有测试nodeid就称规格阻塞。

BODY_WIRED清单见`implementation/BODY-WIRED.md`：按生产调用边确认而非文件数。主体写完≠验收完成；独立review也不得仅以AST存在签PASS。

### AS-VERIFY 一次集中验收

按顺序：本profile deterministic SDK场景与继承MUST→SQL迁移/竞争/强退→16+新增定点mutation→stateful→legacy/fullH1已批准回归→候选原生Host→真实模型→独立代码审查→默认ON/factory最终回归→同交付文档。

参考测试本PlanAgent可运行用于检查规格资产；不把它计入SDK。SDK实际测试缺dependencies是环境结果，不自动装新runtime依赖，更不改断言跳过。

## 15. 真实模型 oracle、预算、继承与完成标准（F15）

原48组保留ID，C08只做离线late accounting；4×3真实模型单列`implementation/model-scenarios.json`与runner协议，不挂一个允许stub的pytest来冒充。原66和OCC12每条保留完整given/when/then，增加负责writer、触发入口、实际断言、验收门及target_nodeid；actual_nodeid未实施前PENDING合理，不能只挂组ID。

四场景固定：M01精确事实报告；M02已知错误候选被独立审阅拒绝再允许有界返工；M03执行中源撤回/新增反证导致旧接受不可复用；M04准备并送达，其中受控真实connector测试服务首个回执丢失，原操作核对一次最终交付。真实模型传输不可stub，connector用真实运行的受控本地服务非生产邮件；M04不强迫模型随机制造网络失败，测试控制器在指定handoff切点触发。

每场景3个预登记trial/seed/输入hash，不补抽好结果。每trial总input+output预算300000 tokens、model calls≤16、tools≤32、wall≤900s，实际项目更紧上限取小；格式修复最多1、内容返工最多2，受原责任预算。profile读取用户当前批准实际配置并保存hash；模型不同失败不切换替样本。模型中断/环境故障记录INVALID_ENV，无PASS；排除这类trial也使整套暂不闭合，修复后同trial_id新的attempt留历史，不偷偷减分母。

通过门：12/12无false completion、无越权/错误复用/重复现实操作；12/12满足各自确定性oracle；其中M02期望拒绝坏候选不是Mission失败，合法返工结尾须正确；M03必须拒绝旧证书后以v2重新处理并完成（total=232）；真实来源不足只能阻断且本trial不PASS，不能把任意失败算成功；M04要求实际服务确认送达且调用效果计数=1。任一超预算/跑不完保留FAIL，整体不得发布；质量/成本同时给每局值和median，不能只给完成总数。

固定输入、逐步允许轨迹与oracle具体字段见JSON；本轮不发起模型，不编造当前profile名。

### 15.1 文档与最终门

同交付更新Host `ARCHITECTURE/AGENT_ORCHESTRATION.md`、关联Assurance事实源（可新增`ARCHITECTURE/ASSURANCE.md`）、`ARCHITECTURE/index.md` 和 `PROJECT_STATUS.md`。记录真实入口、版本、日期、未完成和relative evidence index；正文不贴raw receipts/log/db/screenshots。源码WIP与验收完成分开。

`SPEC_RESOLVED`表示15条已有确定方案且资产一致；不是100%无未知缺陷。`BODY_WIRED`是生产边完成；`SDK_VERIFIED/HOST_NATIVE_VERIFIED/MODEL_VERIFIED/INDEPENDENT_CODE_REVIEW`必须真实独立证据。最终默认ON属于当前用户已给定同交付规则，不额外要求operator enable。当前本文不声称这些运行门通过，也不开放未完成TaskGraph。

四视角挑战是作者分视角自审；接收方此次评审单独记；生产最终审阅须由实际可调用的独立审阅者提供。没有工具时标NOT_RUN，不把同一作者换角色当独立Agent。

## 16. 实际执行命令与交付

所有本地命令使用候选 `.venv/bin/python -B` 或该项目 `uv run --frozen --no-sync`。不使用系统Python替产品验收，不写.env/keychain。证据目录必须既有ignored规则确认，且新run目录不覆盖。

```bash
SDK=/Users/denny/projects/simple-harness-sdk-h1h-impl
HOST=/Users/denny/projects/simple_harness
PY="$SDK/.venv/bin/python"
KIT="$(pwd -P)"
test -x "$PY" || exit 2
RUN="$(TZ=Asia/Shanghai date +%Y%m%dT%H%M%S)-$$"
EVID="$HOST/.local-test-evidence/2026-09-22/assurance-1.1/$RUN"
git -C "$HOST" check-ignore -q "$EVID" || exit 2
mkdir -p "$EVID"
# 交付一致性；不是SDK验收。
"$PY" -B "$KIT/tools/verify_delivery.py" --root "$KIT"
"$PY" -B "$KIT/tools/check_plan.py" --root "$KIT"
# 实际源码fingerprint采集；不自动宣称语义已审。
"$PY" -B "$KIT/tools/capture_identity.py" --sdk "$SDK" --host "$HOST" \
 --map "$KIT/implementation/source-map.seed.json" --out "$EVID/source-map.local.json"
```

BODY_WIRED完成后，才执行目标存在的SDK测试：

```bash
cd "$SDK"
"$PY" -B -m pytest -q tests/orchestrator/full_target/assurance_exec \
 -o cache_dir="$EVID/cache" --junitxml="$EVID/sdk.junit.xml" > "$EVID/sdk.log" 2>&1
```

遗留nodeid/mutation/stateful/完整H1具体argv从本地实际任务书登记`gate-commands.local.json`，不造不存在CLI；新增目标nodeid在附件全部给出。真实模型runner由WorkAgent按JSON的`tools/run_assurance_model_scenarios.py`目标接口实现，只有在body完后运行；本包不附能绕预算的直接API脚本。原生启动同理使用当前锁定Tauri script并记录实际argv。

**本次无需先取得全部SDK PASS才允许编码。**WorkAgent按确定合同一体接线；只有发现现有源码语义确实无法同时满足要求时，列具体反例与冲突字段，其他工作继续。不要再次用“需要producer”替代本包已经给出的来源、writer、reader与事务。

## 附录 A：资产与新接口落点

- `implementation/integration-map.json`：六purpose、S01–26、terminal/writer/consumer/Host/backup的单owner映射。
- `implementation/ref-resolution-map.json`：逐kind与字段的严格resolver，来源/键/body/版本/issuer/失败。
- `implementation/field-producers.json`：完整Schema JSON pointer及resolved hash，无重复nested_schema。
- `implementation/sql-column-producers.json`：全部新增SQL字段与writer/reader，实际库适配仍由AS-0核对。
- `contracts/*.schema.json`：活动1.1精确结构；修改layout的binding用v2文件，不改公开TypedRef/ReviewPurpose。
- `sql/assurance_additive.sql`与`queries.sql`：完整DDL/查询，不是完整SDK父schema。
- `reference/protocol_v11.py`：三值check、证据labels、读集、lane/恢复门；`semantics.py`原clean closure复用。
- `tests/test_review_findings.py`：F01–F15新增决定性反例；已有reference测试同步新合同。
- `implementation/model-scenarios.json`：四场景输入/oracle/预算，SDK runner由实施者接真实runtime。
- `implementation/inherited-coverage.json`：66项逐条；`occ-coverage.json`：12项逐条。
- `review/CHALLENGE-REPORT.md`：真实自审发现与修订；`VALIDATION.md`只记本包实跑，不记产品PASS。

## 附录 B：外部技术依据（不替代本地事实）

源码事实只来自本次18文件摘录和handoff。外部仅用于接口边界核对：
Python sqlite3（complete_statement/事务/executescript）：https://docs.python.org/3/library/sqlite3.html
pytest Function.originalname/collection：https://docs.pytest.org/en/stable/reference/reference.html
Tauri dev自管devUrl/beforeDevCommand：https://v2.tauri.app/develop/
不升级用户现有库，不拿当前官网版本推断本地锁文件。

## 附录 C：生产接口、初始化与循环引用消除（本正文组成部分）

### C.1 固定新增内部接口与实际调用者

以下名称是明确实现目标，允许本地登记等价名字，不允许变更输入的事实来源。所有 `*_locked` 必须使用原Store事务/connection；`read/prepare/compute` 不写业务表。

| 接口 | 精确输入 → 输出 | 唯一实际 caller / 边界 |
|---|---|---|
| `bind_assurance_profile_locked` | 已存在Mission＋creation协议＋冻结部署profile＋真实factory命令 → binding/activation receipt | 原Mission factory，同事务，不是模型工具 |
| `approve_assurance_check_policy` | exact requirements/scope＋原批准的check映射＋authenticated caller/command → CheckPolicy ref | 原Requirements批准/派生准则准入，不由package builder批准 |
| `ensure_assurance_review` | 已注册purpose builder产出的冻结subject/criteria/policy/catalogue/authors＋显式round＋command → 原Package+review_key+首次invocation | 六行切换表的现有coordinator，去重不按通知event |
| `ensure_format_repair_invocation` | review_key＋原确定结束的malformed turn receipt＋command → ordinal2 intent+真实reserve | 原collector；可解析REWORK/UNKNOWN不进入此函数 |
| `record_local_check_run` | registered CheckSpec＋exact subject/input/environment＋受信函数/executor → 真实raw检查产物 | VerifierRouter原分支，实际运行在事务外 |
| `import_assurance_check_locked` | 实际local/executor来源＋CheckSpec＋subject＋assertion → immutable CheckBinding | 原受信checker/importer；API布尔值不能调用 |
| `import_reviewer_disclosure_locked` | 实际Provider input/message receipt＋review invocation＋exact catalogue增量 → event+batch | 原runtime输入回执导入hook；工具曾返回不够 |
| `import_official_assurance_review` | 原collector源receipt+raw hash＋invocation＋真实曝光/check records → 原official ReviewRecord+side binding | 六purpose现有collector的新profile分支 |
| `read_complete_evidence_snapshot` | Mission/Scope/use/consumer＋原一致读上下文 → CompleteRead候选、rules、epochs | ValidityService原事实reader的组合，无writer side effect |
| `compute_assurance_use` | CompleteRead＋当前caller access witness＋now/policy → CandidateUseCertificate | 事务外有界计算；不直接签可用权限 |
| `commit_assurance_use_locked` | candidate＋当前epoch/global/clock/access/expiry → committed certificate或RECHECK_REQUIRED | 真正accept/disclose/handoff的guard；不从缓存补来源 |
| `accept_assured_review_locked` | official record+certificate+OCC scope → 原Acceptance/Contribution | `ResolutionCommits.accept_review`内接线，不第二份接受writer |
| `try_finalize_assured_mission` | root exact Resolution＋完整runtime/effect/hold读集 → closeout候选/最终receipt | 原judge_mission新profile分支/持久CLOSEOUT worker |
| `_finalize_assured_mission_locked` | 同事务刚复核的READY closeout → 原Mission写入/安全释放/最终事件+NOTIFY | 原judge的提取末段；无公开裸调用入口 |
| `ingest_assurance_events_locked` | consumer cursor expected version＋实际事件页 → pending分类+cursor | 原Orchestrator tick，不另开scheduler |
| `commit_assurance_work_locked` | claim row/version+target＋准备结果/原command → effects+receipt+ACK | 同一tick；已存在receipt也ACK匹配旧work |
| `reauthorize_restored_read` | 当前认证caller＋new root/manifest＋exact refs/用途＋当前ACL证据 → 新read grant | 现有MissionControl认证入口；只读，不恢复旧execution |

四种缓存/历史读者都不得绕开共同 `read_use`：Input/Context、原生下载/summary、Acceptance/GoalResolution、工具handoff。`api/assurance.py` use_check仅诊断，不返回可被调用方当执行许可使用的签名对象。

### C.2 第一次启动和批准政策的先后

新Mission原创建事务先写Mission与Requirements事实；随后写creation discriminator、真实activation事件/receipt、assurance binding、Mission epoch与四cursor。已批准的check policy随后在同一事务或显式原批准命令中写入；**policy读取者不再要求一个尚未创建的Review/Reservation。**新空环境的global epoch/clock row由真实安装初始化receipt产生，不由query默认填0。

成功的factory回执不可递归包含自身`content_hash`。先冻结command body、生成原event/receipt；后续Ref用实际存储body计算。epoch/cursor插入引用receipt ID可使用同事务deferred FK，不需要先提交半成品。

历史非空root首次安装此版本，也必须由当前已认证安装入口登记真实root身份；不能仅靠旧库有profile就视为可信恢复根。这是来源初始化，不是额外一次Assurance功能启用批准。

### C.3 原始raw、受信import与无hash循环规则

- reserve事件正文含原账户/保留ID/金额上限/owner/intent，不包含将来invocation的hash；invocation引用已经持久的reserve事件。
- disclosure事件正文含原请求输入receipt、review_key、batch_no、previous_batch_hash、**label-ref delta hash**，不含本batch最终hash。然后batch引用事件ref并计算完整batch_hash。避免event↔batch互相包含hash。
- TurnImported引用实际原Provider请求/输出，不包含未来official record binding；RecordBinding随后引用其原event与已保存ReviewRecord。
- pin acquire事务先有稳定command/receipt ID。receipt返回pin ID/blob hash/动作，不序列化含该receipt hash的完整pin对象；commit后reader形成source_receipt_ref。bind/release同样，避免循环。
- Usage导入始终使用原调用的冻结计价与身份，不因为review拒绝/源码更换/subject失效改写过去费用。

### C.4 Pin/GC 实际实现范围

`artifacts/assurance_pins.py`新增 acquire/bind/release，底层唯一 writer仍AssuranceStore。acquire记录PREPARING后才读CAS供审阅；若GC先删除、读失败则release并SOURCE_UNAVAILABLE，绝不继续使用空材料。GC最终删除门必须在原CAS删除协调锁/原Store事务中检查原全部正式引用根＋PREPARING/BOUND pins。缺某库引用覆盖时保守不删除，不能因为索引空认定无引用。应用层没有GC时不新建定时GC；保留数据，直到未来实现同一final-delete guard。与生产接受有别，SQL只保证pin身份/状态，不保证磁盘字节存在。

### C.5 事件适配、时间与队列状态补充

`implementation/event-consumer-map.json`定义内部事件类别与原writer/receiver对应。AS-0把当前真实event enum登记到类别adapter；不是靠字符串前缀猜所有Assurance事件。一条event可喂不同consumer，各cursor独立。MANUAL_REQUIRED采用WAITING+reason，不增加公开状态；due query排除它，当前真实新条件可以以更高事件seq合并重开，用户重试不能重置累计上限。

RUNNING lease过期只允许重新领取本地协调/计算；其原service intent仍幂等复用。新src事件把相同work升级target，旧claim的row version失效，不能ACK。通知交付也必须先查是否已有同logical notification的回执；传输可能重复，用户已读另有回执才判断。

### C.6 Host DTO 字段语义

snapshot的每个item：history_state取固定seq的原Criterion/Review/Effect/Closeout/Contribution状态；current_use是在当前权限/恢复门/Validity下计算的使用结论，不用历史状态替代。review响应使用独立 `host-review-response-v1`，包含官方状态、逐准则model_grade/effective_grade/check_gate和披露标签；证据正文通过原获准artifact读取，不从UI拼任意CAS URI。use_check响应为 `host-use-response-v1`、diagnostic_only=true、certificate_ref=null，不把诊断变成权限token。

history读取按受支持的原事件/不可变记录重建。没有完整历史投影覆盖时返回`HISTORY_UNAVAILABLE`，不得从当前状态伪造旧快照。current翻页过程中epoch变化返回`SNAPSHOT_CHANGED`，不混合两次状态。返回可见列表的分页不作证据集合COMPLETE声明。
