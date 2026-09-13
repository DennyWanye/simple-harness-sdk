**最后更新：2026-09-13 13:46 CST — 合法败选验证器收尾。** 新屏障用真实SDK双候选和实际code_test固定胜选先正式接受、败选后返回：旧代码3FAIL2.84秒，分别暴露终态续租拒绝及错误清理丢失胜选完成进展。现仅在recorder续租被拒且持久记录证明合法同Task胜选已接受、败选因sibling_accepted被SUPERSEDED、原Result为superseded时，结束该过期验证器；其他验证异常不吞掉。完成任务表成为唯一消费点，移除重复错误callback，仅移除正在抛出的错误，保留成功兄弟进展。新3项与原隔离2项共5PASS4.50秒；恢复/多调度/多Mission/Critic邻接31PASS42.91秒（runner43.24），g-late-verifier-recovery-integration-v3；mypy117/ruff PASS。正常屏障最终Mission完成、胜选产物独立判定通过、9次SDK请求/1350结算/0预留，再次run不增加调用、事件或预算；RuntimeError和无关CommitRejected仍可见。原full-v8具体交错未被记录，不把新屏障证据追认为原日志事实。下一全套回归待执行；P33/P34/P35累计仍OPEN，不打包/P36/推送。

**最后更新：2026-09-13 13:40 CST — 崩溃取证副本恢复。** full-v8 SIGKILL现场为rollback journal模式，存在有效hot journal；读取需要SQLite先恢复，原mode=ro会失败，普通WAL只读查询也会改写原SHM。取证helper现仅在所属子进程已退出后复制主DB与journal/WAL，在临时副本正常恢复查询，原件完整保留给实际cold owner。新DELETE/WAL实际进程SIGKILL双反例旧helper2FAIL0.35秒；修复后连同原两种SDK结果/UNKNOWN冷恢复4PASS9.79秒（runner10.03），g-recovery-observation-green-v2。新测试逐文件hash证明取证不改原DB/sidecar、临时副本清理，并让真正冷owner最后恢复原件。这是测试取证修复，未声称原生产冷恢复失败，也未预先恢复原件削弱边界。

4ffad9e真实固定pairv5：FIRST293.774秒/547472 tokens，F/B/C完成，S已消费61673但下一请求30986超过保留Critic后的可用额度；COMPARE125.624秒/174535 tokens，B后续候选Critic下限40960而仅余28333，两臂均budget_exhausted，runner420.05秒。SDK `.local-test-evidence/2026-09-13/p34-real-search-value-9d74269f9f484f87b9fd75c08bd287b5/`，不声明节省、成功或优势。原实验预算未改，下一轮预算策略待用户偏好；并发收尾屏障复现与全量审计归因继续，P33/P34/P35累计仍OPEN。不打包/P36/推送。

**最后更新：2026-09-13 13:27 CST — 角色按合同执行与及时提交。** 新code默认worker-v3/synthesizer-v3按当前Task合同和实际暴露工具执行，复用仍完整可见且未改变的文件，相关代码/数据/配置/环境不变才复用本Attempt已通过测试，完成outputs及必要验证后提交envelope。旧v2、五个variant-v1及doc派生保持字节；66个历史模板/9个domain canonical核对通过，既有doc6聚合显式选择旧worker/synth版本，原hash不变。新实际SDK请求/部署工具交集/综合错误产物仍被独立code_test拒绝4PASS3.84秒；兼容及policy回归83PASS1可选tokenizer未配置SKIP/12.91秒，g-prompt-efficiency-compat-v2；mypy117/ruff PASS。本次不改预算、工具权限、独立Critic或验收oracle，未做receipt精简。真实pairv4已证B无权限run_tests及4次同字节重读，S代码未改时重复测试；两者已写文件却未提交候选，按原预算正确拒绝。新指令是否改善真实行为尚待原固定FIRST/COMPARE下一组，不预先声明节省或P34完成。P33/P35全量中两项运行失败根因仍在核对；不打包/P36/推送。

**最后更新：2026-09-13 13:23 CST — Critic冻结版本反馈修复与完整回归实际结果。** SDK33b25b5完整编排回归1814PASS/3FAIL/9真实Provider默认SKIP，pytest623.29秒/runner623.81秒；g-current-orchestrator-full-v8，源码干净。失败：P35 SIGKILL读实时WAL时readonly错误、step06双候选结束仍ACTIVE、step09禁止以当前默认prompt_version决策的结构检查。第三项已修：schema反馈能力绑定显式冻结critic-v3，旧/未知版本无反馈，不随将来默认变动；critic/policy/provenance15PASS8.39秒、ruff PASS，g-critic-frozen-policy-v1。前两项无audit2PASS6.14秒、带audit2PASS6.04秒，保留full失败并继续根因诊断，不凭重跑判定无缺陷。全量O4原始OPEN：1259DB/1394Mission/8090观察，206finding/938store errors，负向fixture及覆盖诊断正在逐项归因，不能称rawPASS。另独立原生全扫33DB/45Mission/78观察PASS零差异/零额外调用，0.781秒，Hostreplay-all-native-v4.json SHA2565d17f7df6d9b4903c4908d0e2b508bb3b9b1ccc6b07c09a8b0a05d9d37ce7b33。P33/P34/P35累计仍OPEN；不打包/P36/推送。

**最后更新：2026-09-13 12:59 CST — 系统Worker原额度增长与Critic结构反馈。** 真实FIRST smoke（4ba53f4）两次Manager/片段F/独立B/修复C均完成，最终S因名义60K Worker预留无法动用原120K系统hold剩余额度而失败（330.401秒/452199tokens）；原失败保留。新逐请求增长仅从同Task原hold按差额原子转入，保留各活跃候选的冻结Critic token/费用最低额；初始分派和背压不变，UNKNOWN不增长/返还，实际已知结算仅一次返还未用额度。初版全量转移Worker room被独审指出并发/背压风险后撤回，不交付。当前预算65PASS/5.49s，含实际SDK请求、费用、兄弟候选、UNKNOWN和返还控制；独立Sol审查无确定P1/P2。Critic默认升v3，v2及文档历史字节保留；mission_criteria仅允许原Mission条件，结构错误仅向下一独立v3 service给出白名单反馈，严格解析/两次费用不变。真实COMPARE此前一次错误混入Task条件导致额外22205tokens复核；新冷热/历史模板81PASS/3.77s，Astra独审无P1/P2。测试初稿空claims触发rule_check而非Critic、超时2FAIL，以及旧canonical聚合误引用新默认模板的1FAIL均保留；修复夹具与显式旧critic-v2映射，原hash未改。Mypy117/ruff通过；新已提交态真实对照与最终全量待，整体未完成，不打包/P36/推送。

**最后更新：2026-09-13 12:48 CST — 完整原生验证背压。** snapshot-v23（SDK4ba53f4/Host5a939d6b）真实UI提交7个短来源Mission，2个实际Verifier有界等待。持久采样观察2RUNNING+2PENDING达到总上限4，BackpressureRaised seq140；首个验证转待人后再派发的Worker预留减为10000，随后总数降至低水位2，BackpressureCleared seq192，恢复20000，间隔20.222秒。UI读取Raised/Cleared历史、逐一打开83字节review.md并复核通过，7Mission全部COMPLETED/各900结算/0预留；42Provider记录/0rehandoff/105journal在人审前后不变。峰值未及时截屏，峰值与减速由同一次真实运行的持久事件/采样/预算证明；不是手动释放，两个Verifier均20秒自动解阻后运行真实Critic。进程组正常退出无残留，生命周期512.800秒。Host `.local-test-evidence/2026-09-13/p33-g/source-ui-pressure-v24/case-summary.json` SHA256 `616bb3deed0ca3c76a9212ec9c4fb67582ca120a4311c459a1d78c5743fdb5bd`。关闭P35-A03原生阈值/减速/排空缺口，保留最终回归与新发现的system Worker尾部增长问题；整体未完成，不打包/P36/推送。

**最后更新：2026-09-13 12:36 CST — 长Context原生交付与冷读通过。** snapshot-v23（SDK4ba53f4/Host5a939d6b）通过原生文件选择器导入原97,200字节来源，Mission `mission-cb2cd1cb76fc88ee` 正式交付，2550结算/0预留。12个8,100字节页面全部保留在真实SDK journal，8个Worker请求出现持久Context轮转，原instructions/user_input及完整工具组保留，实际Provider观察hash与selection逐一相符。UI打开380字节REPORT.md（SHA256 `c4f9c9eb9c57dca52dd45cf720f66092a0f401f335899a6ad69a263184b3c9d9`），首段/原约束/末段引用完整读取；冷启动读同报告和约束引用，51事件/17Provider记录/0rehandoff/37journal/17selections完全不变。原生/冷读生命周期299.810/271.067秒，两进程组正常退出无残留。受控Provider机制证据，不代表真实模型记忆质量。Host `.local-test-evidence/2026-09-13/p33-g/source-ui-context-v23/case-summary.json` SHA256 `7a0b1f773879e3f07b99f5f16d2d85a7f815e7427fd4a8c0df87e09a78fa1e0b`。关闭P35-A07当前原生轮转/冷读缺口，整体仍待完整压力与最新回归，不打包/P36/推送。

**最后更新：2026-09-13 12:25 CST — 已接受片段的 Manager 续接。** Manager 的已完成片段 Task 是证据，调用权限仍由活跃 Mission 与独立 service intent 决定。仅当冻结 validated_fragment 与实际 accepted Result（PASS/DONE）、原 Attempt 和正式 projection receipt 一致时允许 COMPLETED Task；取消、租约、身份与预算检查保留。真实 DeepSeek v3 暴露后，完整跨分支逐请求准入先红，修复后53项通过/21.63s；P34/P35/step05扩大回归278PASS3SKIP/101.28s，mypy117/ruff通过。独立Astra静态审查无P1/P2，直接非法绑定8项测试通过/3.17s（合法出站及7种零出站/零grant/预算不变拒绝），续接切片完成。真实对照仍失败，未重测；P33/P34/P35未关闭。证据与局限见 plans/2026-09-12-phase3/p33/journal.md 12:20 条目；不打包/P36/推送。

**Last updated: 2026-09-13 11:48 CST — Manager admission and late accounting.** Real DeepSeek fixed pairv2 exposed Manager authority rejection: referenced failed Attempt is historical evidence, while its service intent owns current authority. Guard now validates exact evidence Task/Mission identity without requiring that old Attempt live; own authority/lease/pool/fingerprint/budget/cancel checks remain. Initial28PASS1.73s. Related270-case sweep268PASS2FAIL2SKIP89.51s exposed a second defect: real Manager task_id was missing from older late-accounting fixture; with it present, recovery wrongly expected Task-funded account though Manager is Mission-funded. Recovery now preserves evidence Task validation but checks original Mission account for Manager. Actual hot/cold receipt import35PASS3.05s, terminal business and original invocation unchanged, no Task money charged. Parent intermediate fixture NameError and both original failures retained. Mypy117/ruffPASS. Real search test keeps all materials/root2M/A-B240K budgets and aligns output ceiling to Host8K (old32K required65536 while only60000available);8pure configPASS. Corrected paid pair and final current full suite pending. See journal; no packaging/P36/push.

**Last updated: 2026-09-13 11:35 CST.** Current source cumulative non-network1762PASS plus16journal/contextPASS, approvedCOMPARE native andcoldPASS. Corrected UI wording77PASS/nativepending. Real fixedDeepSeekpairv2 found Manager authority_rejected before outbound; botharms retainedbudgetFAIL, guarded27/12calls,210702/65800tokens. Astrahigh child fixingidentity, no additional paid reroll until deterministic verification. NativeContext/fullthresholdpressure waiting usable UI afternoWindowsAvailable; no Missionclaimed. See p33journal11:35 entry for paths/hashes/timing. Phase3 remainsOPEN; no packaging/P36/push.

**当前补充 — 2026-09-13 11:05 CST：** P34原固定FIRST/COMPARE真实deepseek-flash两臂分别283.621s/79.057s、550469/108311 tokens，均budget_exhausted；Mission总额未耗尽，子Task额度不足。账本与Provider用量一致，无重复计费证据。测试原配置没有provider token grants，不能代表Host已接入的逐请求准入。现已保持原任务/材料/2M总预算与A/B限额不变，接入同一固定官方tokenizer的Context与Provider estimator，明确记录ZERO_GRANTS/UNKNOWN/EXERCISED；纯配置7PASS/.22s、ruff通过，未重跑付费组。原两次失败完整保留：SDK .local-test-evidence/2026-09-13/p34-real-search-value-8dc3876aadb546e0baa9148a0095122e/。P34价值门仍OPEN。

**最后更新：2026-09-13 10:51 CST — 源码原生负载、独立恢复与搜索链验收。** snapshot-v17（SDK0a1a050/Hostc61744d6）：三Mission/两物理槽中第三任务真实UI取消，释放前后无实际Provider调用；官方backup/restore到独立userdata后，成功/取消/待人三状态及96事件、14 Provider记录（13succeeded/1claimed）、34journal完全相同，恢复副本真实UI复核后正式交付，调用不增、rehandoff0。B2 summary SHA256 `0d4e3c76fbbf35ddc7ccda3eb5241e71147c92e234f9814ab01db25460591efb`。原生Verifier压力v19：UI显示第三Result PENDING，UI时点持久事件对应2RUNNING+1PENDING，20秒自动释放后3任务均交付；手动marker未观察，不声称pending上限4饱和。summary SHA256 `c67edef26d8360b5feb53f8c068d05904f1a2f54e2545326976a1b911611f6c7`。FIRST原生v20：保留A三次失败，F仅核选中片段，Manager改C依赖为F+B，C实际测试通过，S读取已验证C并实际测试通过；UI打开final.md，冷启动同hash `bc04ba9b12d5ab4e0729599c2cce15ca42d715152ea84e81484f0f78ac1c73c3`、26调用/0rehandoff/62journal/192全局事件不变。summary SHA256 `7ebfacc0d9cc21f1d3989598f13b320fdc97d423fc81095f37a581e7463a3479`。均为受控Provider机制验收，不冒称真实模型质量。Host证据根 `.local-test-evidence/2026-09-13/p33-g/`，对应source-ui-b2-restored-v18b/source-ui-pressure-v19/source-ui-search-v20。只读回放replay-all-native-v2.json：27数据库/35Mission/62观察，PASS零差异/错误，.730s，无调用/效果变化，SHA256 `a2cc5a33ba08f4dfe1c1c6cb3452148d37367b15dc2f003dcf829ef23f57382b`。当前SDK826c0e1五项回归修复56PASS/20.03s，新全量待；P33累计关联/P34/P35仍OPEN。真实FIRST/COMPARE原固定pair两臂预算失败已保留，测试漏接原生精确tokenizer/逐请求准入的配置正在修正，尚未重跑。长Context原生rotation与批准COMPARE UI待。不打包/P36/推送。

**最后更新：2026-09-13 10:33 CST — 全量回归发现的恢复与预算分类修复。** 全量 g-doc9-orchestrator-full-v5 为1746 PASS/5 FAIL/12 SKIP，623.89s，未通过。缺失冻结runtime pool时，恢复跳过绑定并保留原SUBMITTED turn；必需Critic冷却时进入有界等待，Worker可路由不再重置Critic等待起点；保护尾部的Attempt额度耗尽改用BudgetExhausted(attempts)，让冲突任务按原合同转人工，避免误报runtime_unavailable。策略结构断言区分搜索角色读取与Mission绑定的模板选择。五个受影响文件组56 PASS/20.03s（runner20.32s），包含原5失败、健康Critic拒绝对照和预算四表不变检查；mypy117源文件与ruff通过。源码测试已通过，新全量与该修复的原生UI仍待；P33/P34/P35整体OPEN，不打包/P36/推送。证据在SDK .local-test-evidence/2026-09-12/p33-g/g-full-regression-five-fixes-v1.*。

**N1 original-source acceptance — 2026-09-13 10:09 CST: PASS.** Source snapshot v16 (SDK aada164 / Host ba6be341), doc9, same two original files and original 400000/12 goal/budget: Mission mission-0b12722003e0b883 COMPLETED/verification_passed in291.398s, one Worker Attempt,320365 settled/0 reserved. Actual assistant journal submitted ordinal refs; canonical Claims:11 VERIFIED source attributions,3 SUPPORTED analyses,1 UNDER_REVIEW structural statement. REPORT SHA256 `812114f5b9f1252a56d43e4ff6815961a031aeec01e62da3f751103c0c9c3e52`. Parent opened report and both-source citations in native UI, including complete HA-12 row and 99-character conditional unit; full CAS report matched displayed hash. Parent and independent Terra medium review PASS: build/startup failure records acknowledged; historical verification is not current installation evidence. Same-source cold UI reread preserved artifact/citations/status and14 Provider records (11 succeeded/3 failed),0 rehandoff,60 events and SDK journal counts. Native/cold carrier lifecycles651.361/104.222s include manual inspection, both exit0/no residual. Host evidence `.local-test-evidence/2026-09-13/p33-g/source-ui-n1-v16/case-summary.json` SHA256 `195b17df5a66ee13937410abbbe59b4e75c255a49fa6766ee75808afc1972bde`. Historical failed N1 runs retained. This closes current N1 content/native/cold gate, not P33 cumulative audit or overall P34/P35. No packaging/P36/push.

**Updated 2026-09-13 10:02 CST — queued cancellation and resumed stall timing.** Legacy calls now acquire physical slots before durable SDK handoff and recheck Mission/intent under the Orchestrator Store transaction; cancelled queued Planners cannot issue a new request. Existing usage and UNKNOWN semantics remain. Liveness reads effective admission; a persisted blocked-to-unblocked transition starts one new stall window without inventing SDK progress or renewing it on ordinary heartbeats. Actual two-slot/three-Mission cancel, accepted response, UNKNOWN, six-stall-window waiting, completion/cancel and true post-queue stall: 8 PASS/7.71s. Affected budget/lease/replay:44 PASS/32.97s; mypy117 and ruff PASS. Sol and Terra high independent limited reviews ACCEPT. Raw logs under SDK `.local-test-evidence/2026-09-12/p33-g/`: g-local-queue-liveness-v4.log SHA256 `4f05f5ebb5de92615dd68b964f93b9027ad2303e153ed47f1c6c8b30c2519b6b`; g-queue-admission-affected-v5.log SHA256 `22b45b6d736420ebc05d647b5bdd6c2a2bf61bd73523ab18aea166f871e736c0`. Earlier configuration and actual queue-exit failures remain. Native load validation is pending. Doc9 current/legacy profile follow-up37 PASS/.43s. P34 only seven material-mechanics checks/1.99s so far, not real-model value. P33/P34/P35 remain OPEN; no packaging/P36/push.

**最后更新：2026-09-13 09:54 CST — doc9 冻结序号输入。** 新文档任务默认doc9；Result提交允许criterion_refs/mission_criterion_refs按冻结Task/原Mission目录严格展开，正式Claim仍保存完整ID，原始SDK journal不改；互斥、越界、错身份、旧doc8/code、坏完整hash拒绝，alias不增加证据或放宽验证。旧doc8 canonical SHA保持。当前parser/历史profile53 PASS/1.01s（g-doc9-parser-v4，runner1.36s），先前受影响prompt/域/queue组合95 PASS/3.89s，ruff通过，doc9独立静态复审ACCEPT。原始资料N1v15(doc8)仍FAILED：首提交第18条Claim完整hash抄漏字符，重试Task预算不足；284857已结算/0预留，未接受报告仍有过度概括缺口。新增提示词要求核对开头/历史反例，但尚不证明质量修复；doc9同资料/400000/12真实UI待验。当前另行排队取消/队列退出stall修复未随本切片验收。P33/P34/P35整体OPEN，不打包/P36/推送。

**原生边界与恢复检查点 — 2026-09-13 05:40 CST：** 新冻结 SDK c8e2541 / Host b7dc4c64 综合1127 PASS/75.26s。N1v9真模型正式交付及同源冷恢复已核对；新Host显示修复在受控原生来源指令用例验证。N4来源指令归属、错误逐字引用、矛盾证据三例原生UI符合预期，独立原始证据保存在Host `.local-test-evidence/2026-09-13/p33-g/source-ui-n4-*-v10/`。实际OS SIGKILL后两库冷恢复2 PASS/9.59s：成功结果零重复Worker、独立Critic读产物；UNKNOWN保持原token/cost占用。仅覆盖该两边界，不覆盖完整Mission或P32逃逸进程恢复。FIRST新保护虽18PASS/0.91s，独立审查仍有系统hold丢cap和priced分别取整2项P1，修复中。P33剩余N6/active管理/O4、P34综合价值场景及P35其余门槛保持OPEN，不打包/P36/推送。

**源码与原生 UI 检查点 — 2026-09-13 05:25 CST：** N1v9 原始两文档、400000/12 原目标在 SDK c9a1f183 / Host 45c09756 源码环境完成：220.968s，正式 REPORT f6b192a3…f905、6 条 VERIFIED 逐字引用（两来源、完整表格行、完整限定单元），242431 tokens 已结算/预留0，13 次 Provider handoff。真实 UI 读报告、引用并冷启动重读，调用仍13/无重复；文档区“尚未判定”投影缺陷已修复，后端13 PASS/0.06s、前端25 PASS/0.912s及typecheck通过，新 UI 待验。动态新增已完成依赖的 Task 回放修复42 PASS/36.16s，原 v14 #14 历史43事件全覆盖/无差异；Python3.12空AST字段兼容35 PASS/0.29s，保持原生产基线。总体P33/P34/P35仍OPEN；进程kill测试仍在修复，FIRST请求保护仅helper7 PASS未集成；不打包/P36/推送。

**当前源码检查点 — 2026-09-13 04:49 CST：** P35 离线备份 20 PASS/17.29s；租约丢失恢复及取消 10 PASS/23.14s，受影响取消/恢复/租约回归 44 PASS/6.68s。N1v8 真模型仍失败：18 次实际调用、378113 tokens 已结算、当前预留 0；已定位 RUNNING 时终态 ordinal_to 为空造成 180s 错误超时。改读 SDK 持久进度的定向检查 8 PASS/8.98s，保持真正停滞超时控制；Host 六类文档场景及启动器 28 PASS/12.74s。上述为以 SDK e4da042 / Host 985e403 为基线的未提交修复证据；新原生 UI 待验，P33N1/P34/P35 整体 OPEN，不打包、不执行 P36、不推送。

**Source and native checkpoint — 2026-09-13 04:13 CST:** SDK protocol-error response parsing preserves independently valid Provider usage while still rejecting malformed tools (26 PASS/1.36s); missing/invalid usage stays unknown. Late-accounting automatic original-subject import/settle11 PASS/2.90s and receipt boundaries5 PASS/0.47s, independently reviewed. Citation repair retains failing claim/index/source/line identity without source-body reinlining3 PASS/0.46s. Broader integration v14 is still running/stalled in legacy recovery, not PASS. N1v7 was UI-cancelled after malformed-tool response without usage,170532 settled/108083 unknown held,13 physical handoffs, no successful value acceptance; original proof retained. Controlled source UI N2 delivered two28-Claim Missions (750 tokens each/zero reserve), actual long block290080 characters reached END_OF_LONG_TABLE, in-flight citation switching/CAS error and restored retry observed. N3 source supersede/revoke-reject/revoke-approve and historical read observed; cold verification in progress. N4/N6 boundary software27 PASS/10.67s including launcher identity, native cases not yet run. Whole Phase3 gates remain OPEN; no packaging/P3.6/push.

**Latest source checkpoint — 2026-09-13 03:49 CST:** priced system runtime oracle now passes (1 / 0.94s), after repairing its actual synthesis knowledge fixture; no price-gate relaxation. Three Missions/two physical slots control passes (1 / 0.57s): queued cancellation has zero Provider handoffs/charge, slow verifier and human waiting release model slots, surviving tasks complete with zero reserved usage. Independent review identified an OPEN P1: late accounting effective SDK facts for terminal/collected subjects lack automatic orchestration import/settle; prior explicit-import controls do not prove recovery wiring. Native N1v7 is running with SDK6866/Host7b5; boundary fixture software first run6 FAIL1 PASS/159.83s is being repaired. No P33/P34/P35 completion or push.

**Source checkpoint — 2026-09-13 03:35 CST:** broad P33/P34/P35 source integration1082 PASS/1 priced-test setup FAIL/54.91s; after fixture repair priced system protected-tail real synthesis remains FAIL (0.54s), so priced-system gate stays OPEN. Fragment scope/reuse/actual Worker-Critic/replay25 PASS/1.97s; context/criterion repair5 PASS/0.45s; accounting core10 and other system controls passed in prior batches. Native N1 historical replay:6 orchestration DBs,5 Missions, no mismatch/unknown event, sibling6 SDK execution DBs explicitly inventoried separately; this is history integrity, not successful delivery. FIRST6000 protects configured quota only, not a guaranteed complete initial Critic request under an8192 output profile; effective bounded allowance still pending. Next source snapshot is for unpriced N1 and controlled UI. No overall completion or push.

**Latest native/source checkpoint — 2026-09-13 03:26 CST:** N1v6 produced a rejected REPORT and9 proposed Claims; no formal acceptance. Rule failure from generated free-text quality criteria, then11 zero-call context-overflow retries.128156 tokens settled/currentreserved0, UI observed and exited cleanly. Repair5 software controls passed; broad1051 PASS2 regression FAIL3 optional skips. P34 fragment runtime2 failures; P35 priced rounding reserve review P1 open. N1–N6/O4 and overall P34/P35 remain OPEN. Details/current commands/evidence in Phase3 journals; no packaging/P3.6.

**Source checkpoint, 2026-09-13 03:12 CST:** integrated source checks:1037 PASS/2 legacy schema FAIL (48.27s); pre-schema15 reserved-attempt read compatibility fixed, targeted9 PASS/0.36s. Includes15 candidate controls, Mission system pool7, FIRST6, priced cold1 and missing-usage boundary2. COMPARE decisions and full immutable payloads now replay; frozen candidate deadline cannot dispatch new pending candidates. Controlled Host document fixture software2 PASS/6.21s proves28 formal citations and >256KiB paging; frontend search51 PASS/1.04s and typecheck pass. Fragment branch remains5 output-conflict failures (16 other controls passed); Mission-system runtime hooks, SUCCEEDED-missing-usage settlement, N1–N6/O4 and real P34/P35 gates remain OPEN. No release packaging/P3.6. This is an incomplete development checkpoint.

**Runtime checkpoint, 2026-09-13 02:50 CST:** default FIRST Critic tail reserve/consume/release is connected to actual production dispatch and passed6 controls/0.58s. Typed denial collection3/0.36s; true two-SQLite priced cold reopen1/0.40s. Scope excludes OS-kill, future Mission-level system pools and SUCCEEDED-without-usage late accounting. P34 joint run has3 FAIL/4 PASS/5 setupERROR; source inheritance defect identified and being repaired. N1 and remaining native/full audit gates remain open. See current journals; no packaging/P3.6.

**Source budget checkpoint, 2026-09-13 02:35 CST:** public Mission snapshot now carries current ledger usage in the same read transaction (39 SDK controls/5.12s); Host projection uses settled/current reserved values instead of historical Attempt totals (22 controls/8.42s; frontend49/0.941s). Provider tail/price primitives and durable typed denial:19 controls/1.00s. FIRST tail runtime wiring and native verification remain open; no release or completion claim. First-run collection/fixture failures retained in journals.

**Latest native checkpoint, 2026-09-13 02:25 CST:** N1 v5b (Host133aaa62 / SDKdfc9b7c) FAILED: sole Task90000/4 exhausted its attempts despite Mission400000/12. Both original sources were read in4 pages; no REPORT. Eight physical calls all settled, Mission86732 tokens/current reserved0. UI44494 reservation display is a confirmed projection bug; correction and typed denial stopping are in progress. N1–N6/O4 and P3.4/P3.5 remain open; no packaging or P3.6. See current Phase3 journal for immutable evidence.

最后更新：2026-09-13。

**Current source state, 2026-09-13 02:08 CST: P3.3 G / P3.4 / P3.5 remain in progress.** Integration run g-source-integration-v8: 989 passed, 2 outdated profile-fixture assertions failed, 40.34s (wrapper40.80s). Only the fixture was corrected: g-profile-compat-v9 passed all20 controls in0.02s (wrapper0.23s), preserving exact historical v3/v4/v5 and rejecting unknown v7. The integration includes all18 role-context and all18 provider-admission/recovery controls; the earlier cold-owner failure is closed (focused3 PASS/0.44s and integration). Changed Python Ruff and104-source-file mypy pass. Explicit unpriced profiles have shared token/slot admission, exact owner/epoch recovery, held UNKNOWN cost and actual late usage; priced admission is explicitly refused until monetary accounting is implemented. Future Critic/synthesis tail reservation, full P3.4 selection/fragment reuse, P3.5 load/backup and N1 native acceptance remain open. N1 v4b remains a real failed run; original sources, goal, criteria and400k cap are unchanged. Packaging, release and P3.6 remain paused.

[生产链路与边界](ORCHESTRATOR.md)；[精确命令、首跑失败和本机证据](../plans/2026-09-12-phase3/p33/journal.md#planning-role-local-20260913)。

## 历史版本验证记录

### 9月13日前序局部验证（历史）

大页/context组合 `g-doc6-large-pages-v1` 97 passed / 0 skipped，5.88秒（wrapper6.15）；官方本地tokenizer/provider wire组合另26 passed / 0 skipped，2.51秒（wrapper2.73）。前者覆盖新8192字符/32KiB及实际tokenizer双重上限、匹配ContextPolicy和冻结恢复，后者没有真实模型调用；均不证明累计预算guard或N1业务通过。大页core后续已获Ohm限定ACCEPT。[原命令与证据](../plans/2026-09-12-phase3/p33/journal.md#大页与匹配context配置局部验证2026-09-13)。

00:40阶段源码兼容874项通过/35.70秒，94文件mypy与改动Ruff通过；更早 `g-reading-lifecycle-v2` 为39 passed /0 skipped（旧分页34＋lease5），1.15秒（wrapper1.41）。这些是对应工作树和旧2000B分页阶段的历史证据，不覆盖后续改动或N1真实重验。


G SDK源码验证里程碑（21:18 CST）：干净提交a5c8fca659be8b491d4d0f3f3f5536a5e711ce48完整编排1302 passed /8 skipped /0 failed，487.75秒（runner488.09秒），PG50040已查无残留。8项真实Provider未启用；G整体未完成。0.11.1可复现候选wheel49137655…、306包文件与709个sdist源码输入逐字匹配；Host安装组合/原生flash继续验收。

G进行中（21:09 CST）：SDK默认文档画像v4、原子创建、历史引用全文分页、Mission判定树恢复和每次发布前来源复查已实现；两个SDK范围独立审查均限定ACCEPT。串行定向744 passed /17.44秒，非完整回归。Host后端/UI已实现但尚未安装新wheel验收；前端86 passed、typecheck通过。0.11.1只是候选版本，完整编排、制品、原生deepseek-flash及46项最终审计仍待做。

F 验证完成（保留既有红集）：干净源码 `5bcca08fe666b8e20524206b76ce2afbba63db4d` 整仓3241 passed /60 failed /18 errors /13 skipped（547.16秒）；与同依赖旧源码a4aae8c的78项红集按kind+nodeid完全相同，新增0。0.11.0安装验证1402 passed /11 skipped /1既有迁移失败（506.99秒）；304包文件逐字匹配，258实际加载模块均来自安装包且哈希一致。F不是整仓全绿或新正式发布；G、Host与真实flash仍未完成。

E 干净源码 `cf40b8ec86a2f307d0f8b8f89cf7f0166e5de121` 完整编排 **1199 passed /8 skipped /0 failed**（481.36秒），watchdog481.63秒，PG25036无残留。8项skip为未启用真实Provider。A–E完成SDK源码验证；F/G、wheel、Host与真实flash仍未完成。

## Agent 编排 Phase3 历史阶段状态

D 干净源码 `d3d3fd8650acc8b837dc4c0e093ab95068d054ff` 完整编排 **1119 passed / 8 skipped / 0 failed**（446.64秒），watchdog446.86秒，PG18131无残留。8项skip为未启用真实Provider。D为SDK源码里程碑，E–G、Host/wheel/真实flash仍未完成。
DOC_PROFILE v3、契约schema3；旧历史不迁改。当前生产链路新增有限接受、人工恢复、预算前限额与Mission固定分母确定性判定。见P33 journal §2.4。

P3.1/P3.2 已交付；P3.3 切片 A、B、C 已完成 SDK 源码验证；C 干净源码 `963b090` 编排全量 **979 passed / 8 skipped / 0 failed**（475.47 秒）；独立审查闭环；D 已完成源码验证，E–G、P3.4/P3.5 未完成。
切片 A 源码 `1eaa91f` 的编排全量 **651 passed / 8 skipped / 0 failed**；8 个真实 Provider 用例未启用。
切片 B 干净源码 `fb58bf1`：编排全量 **867 passed / 8 skipped / 0 failed**（488.39 s），定向 300 passed；独立审查无剩余 P1/P2。8 个真实 Provider 用例未启用。
本次未换 Host wheel、未做新的原生或真实模型验收；Host `04350956` 仍钉 SDK 0.10.0。
生产链路与边界见 [ORCHESTRATOR.md](ORCHESTRATOR.md)，接续与证据见
[Phase3 HANDOFF](../plans/2026-09-12-phase3/HANDOFF.md)。本机未新增 worktree。

## 以下为此前 SDK 能力与验证记录

最后更新：2026-09-07。0.7.10 nullable源031fdc6+Host2d64e6e5/fad81ebb：仅明确原类型/null pair，保留required/enum/const/非null约束与原raw hash；Host两字段无值不请求复用，非适用hash拒绝。新增4唯一控制通过，Host首批夹具缺真实evidence入口红已保留，仅重红1。PG76045 exit0/remaining[]，旧H079不改；主统一一次wheel/installed组合，尚非真实模型或main质量通过。[限定结果](../plans/2026-09-07-nullable-tool-schema/RESULTS.md)。

## Mandatory context repair source — 2026-09-06

Last updated 2026-09-06. Separate successor source from H078: typed pending-action
refusal is handled after actual response checkpoint, with at most two durable
same-Run repairs and original budgets. Every repair-bearing terminal (including
routed) still checks real ACK/current pending; fresh typed grants/physical guard
remain. Existing context.no_recall/context.apply audits bind repair identity.
SDK11 + Host3 new controls passed in separate batches; Dirac fixed-source/results
limited ACCEPT. Main owns H079 packaging/installed/r17 with M618; no source tests
repeated, no old Run or frozen wheel changes. LastPG21416 exit0/remaining[].
[Results and exact boundaries](../plans/2026-09-06-mandatory-context-action/RESULTS.md).

## Native Host ordinary Run verified — 2026-09-06

H078/Hostb3680732 real native r13 completed a fresh ordinary turn and the default
audit consumer enumerated45 public DTO rows. Public SDK metadata reports
verified_current_intervals, coverage_gaps=[], history_coverage=recorded; no tool
or effect path was exercised. Old r12 unverified history is not recertified.
Host Run f4370cbe-1a87-537c-8d3b-8e0abbf8bd16; native PG99878 exited normally,
remaining[]. [Host evidence and exact scope](/Users/denny/projects/simple_harness-primary-candidate/plans/2026-09-06-typed-use-primary/NATIVE-R13.md).

## Native driver audit successor — 2026-09-06

SDK-owned immutable start-mode selection exposes the actual driver to kernel
recording. Four new actual SQLite Runtime controls passed; opaque/subclass and
custom Host-control drivers remain unverified. Source13abfe8, no schema change;
H078 one offline artifact and installed3 checks plus Host4 checks passed and received scoped independent ACCEPT; native remains pending. [Evidence and scope](../plans/2026-09-06-native-driver-audit/RESULTS.md).

<!-- Updated 2026-09-06 -->

## Authorization expiry terminal proof successor — 2026-09-06

H076 authorization expiry wrote a failed Run without a run.failed event. This
successor atomically binds root React tool-authorization terminal decisions and
provides explicit public eligibility/recovery plus exact public terminal metadata.
21 unique new source controls passed in bounded batches (not24). Actual r6
SQLite/WAL-consistent COPY passed public eligibility -> recovery -> terminal ->
reopen exact replay;33 original events retained and original DB/WAL bytes unchanged
at this gate. Original userdata has NOT been recovered. Fixed077 sourcec29af669/wheel60f7fb16
has one offline build, small-target public consumer PASS and Dirac scoped artifact
ACCEPT; Host/native remain separate gates. Unknown/multicycle/child recovery shapes refuse. H075/H076
artifacts unchanged. [Contract](../plans/2026-09-06-decision-terminal-recovery/CONTRACT.md),
[results](../plans/2026-09-06-decision-terminal-recovery/RESULTS.md),
[artifact](../plans/2026-09-06-decision-terminal-recovery/ARTIFACT.md).

<!-- Updated 2026-09-06 -->

## Exact short Context identity successor (source scope)

H074 requires a positive ContextFragmentV2 source_revision even though the public
short selected item correctly hasNone. Isolated H075 successor now requires only
None for SHORT_HORIZON, preserves strict positive nonshort and existing hash domain.
Explicit execution9 descriptor/backup migration isolates old binaries before durable
business reads. Wire8PASS and separate migration7PASS; actual Host11groups short
normal physical-guard allow and independent-source deny2PASS in source overlay.
Old artifacts/user data unchanged. Frozen075 sourceabbb0fd has identical double
offline wheel7969a2e5; small target installed actualshort two controls plus previous
Host factory refusal failure3PASS11.40s, no remaining process. Dirac scoped source
ACCEPT; new-artifact/installed review ACCEPT. Full Host typed-use/native acceptance remains open. [Contract and remaining bounds](../plans/2026-09-06-short-context-revision/CONTRACT.md),
[source evidence](../plans/2026-09-06-short-context-revision/RESULTS.md).

<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# ARCHITECTURE 目录

## Receipt-bound Provider reservation source — 2026-09-06

Isolated successor from frozen H073 `0282fa98`: schema2 typed Context intents,
original request/time checkpoint, actual Memory authority port and atomic
receipt/Provider claim association, existing handoff CAS and new-grant retry,
payload-free public view, explicit execution7→8 WAL-aware migration are implemented
with Dirac scoped source ACCEPT at69db778. New bounded source batch32PASS,
separate migration3PASS and adjacent33PASS; retained initial failures are documented.
Fixed0.7.4 source9229269 has identical double offline wheels (168 source files)
and6 target-installed public consumer tests passing; Dirac independent artifact read-only review
is scoped ACCEPT (exact168 package bytes/119 origins/manifest chain). No Host default/native/formal401 PASS. See the
[artifact handoff](../plans/2026-09-06-recall-use-reservation/ARTIFACT-HANDOFF.md).
Generic no-Memory behavior is retained;
missing/legacy typed carrier is not an empty attestation. See
[contract](../plans/2026-09-06-recall-use-reservation/CONTRACT.md) and
[results](../plans/2026-09-06-recall-use-reservation/RESULTS.md).


记录 Simple Harness SDK 的架构生产事实。本叶后继 source candidate 为 `0.7.4`（冻结0.7.3不变）。Human Memory S1
已经把自动 pre-Provider recall 改为显式的同 Run route seam：每个新的 Provider turn 只能消费 Host 经
`RunContextAuthorityPort` 返回并由 SDK 校验、冻结的 Context snapshot；同批 route-required effect 在
route receipt 尚未可见时会在 ledger/handoff 前拒绝。fresh execution schema v7 持久绑定
`TaskExecutionEnvelope`。`WorkspaceBindingAuthorityPort` 现定义独立的 Manual challenge/decision 与 Host
Run-mode snapshot 验证链；只有 Host durable lookup 后返回的 grant 才能进入 append transaction，随后
`WorkspaceBindingSetReceipt` 携带 sorted unique root identity hashes：genesis 固定 canonical empty-set
parent，后续只能验证为 exact parent set 加 grant 的一个新 root，并固定 base→new revision。schema v2/v3
route receipt 和每个 project effect envelope 都交叉绑定该 binding-set receipt id/hash；v1 decoder 只
兼容无 authority standalone，project v1 fail-closed。generic Tool authorization receipt 或
`RunContextSnapshot.metadata` 不具备此 authority。0.7.1 的 route receipt v3 区分 context-tool 与 Host-initial
provenance；ordinary start snapshot v7 将完整 Host initial route/hash 纳入 durable start identity，ReAct
checkpoint schema v6 只在 checkpoint 不存在时原子初始化，并在恢复时以 version-zero 初始锚拒绝启动 route/TaskScope/binding 冲突，保留合法演进的当前 route。
它继续跨轮保留 snapshot revision 与 ID→payload hash，
Provider durable response 只接受 public allowlist，隐藏推理和私有 metadata 不进入 ledger、checkpoint 或
Context。旧 `AgentMemoryPort.record_committed_turn` terminal outbox 仍保留；生产 kernel 不再自动调用
`recall_for_turn`/`release_recall`。Memory SDK 的新 evidence/认知状态和 Host TaskScope 产品实现不属于本仓
当前能力，仍由后续 release unit 完成。

S1 `a2-003` 现已冻结 schema-v2 cognitive wire：EvidenceSpan 由 admitted evidence authority 精确验证 UTF-8
byte range，typed observation 绑定 exact evidence/admission/item；四类长期记忆使用独立 payload/lifecycle、
revision target、canonical DAG 与 strict-atomic authority receipt。RecallPlan 必须绑定未过期 RecallContext，
保留 Host mandatory selector 并只允许缩窄；unknown/external/untrusted disclosure 默认不能产生 RECALL。
RecallContext 还把 Host 当前 Procedure applicability fingerprint 集合纳入 canonical hash，模型计划没有
对应可写字段，不能扩大或伪造当前适用性。RecallDecision 已单独升级为 strict schema v4：每个 selected item
明确区分 cognitive-memory
和 Short-Horizon source，前者绑定 memory type/exact revision，后者绑定 exact chunk ref 且禁止伪造
memory type。NEEDS_USER_CONFIRMATION 使用有序、完整的 atomic group/member，不接受部分冲突组。
typed result/page 与 ContextFragment v2 继续绑定 decision/result/item/use；Context assembly 按 fragment
`(id, hash)` 组装。公开 parser 只接受 v4，v3 与 naked source ref fail closed。
分类 enum 的唯一事实源是无依赖 `information_classification_protocol`；EvidenceItemAuthority 使用公开
`EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION=3`
由 Host 强制附带 privacy floor、canonical attributes 和 classification authority ref。span verification
只接受 exact Host authority type，一次 resolve 后返回同一 verified item authority 供后续 join 复用。
typed observation 仅允许 Tool/Trusted Tool 或 External/External Source 两组 exact provenance 且必须解析
typed receipt；Mutation DTO 同时冻结 epistemic/evidence matrix，Memory repository 后续仍复验 authority。
conversation causal metadata 是 raw evidence 入库后的独立 Host registration，非法 metadata 不删除原始证据，
只失去后续 Short-Horizon 资格。该 registration 现使用独立
`CONVERSATION_EVIDENCE_SCHEMA_VERSION=3`：可召回 item 必须 all-or-none 绑定 Host 已验证
`EvidenceItemAuthority` 派生的 RFC 6901 `public_text` pointer、UTF-8 SHA-256、effective privacy、canonical
information attributes、classification authority ref 与 item-authority id/hash；没有该绑定的 evidence 仍永久保存，
但不得进入索引。v2 conversation metadata/receipt/registration fail closed。

S1 `a2-006` 已新增 Host-owned `MemoryActionAuthority`：该授权约束最初随 mutation schema v4 引入，
当前继续由 schema v5 承载；
REVISE/SUPERSEDE/SUPPRESS 只能引用 `MemoryActionAuthorityRef`，Memory 必须经 Host durable authority port
单次解析并校验 exact subject/action/existing target revision/evidence/run/turn/plan/operation/expiry/nonce/issuer/hash。
action schema v2 还绑定 authority-free whole-plan `plan_intent_hash` 与 canonical operation index；其他 operation
被插入或修改时旧授权必然失效。plan/operation intent hash 都明确排除 authority ref，避免 plan/authority hash
循环，而最终 `plan_hash` 仍承诺 ref；Memory repository 仍必须在同一
mutation transaction 唯一消费 `replay_identity`。缺 authority 使用 typed
`MemoryMutationApplyResult.NEEDS_USER_CONFIRMATION`，不伪装成异常或 Recall outcome；COMMITTED result 与可信
apply receipt 都会复验全部 protected existing operation 已携带 ref。CREATE 不需要 action authority；CONTEST 不得携带 action ref、必须是
CONTESTED、禁止 destructive terminal lifecycle，也不因此取得覆盖、删除或任意降级无关记忆的权限。Memory
consumer 仍必须把 CONTEST payload/lifecycle 与可信 target state 做 exact unchanged 比较，只允许 conflict flag 变化。

S3 Procedure/Prospective 的 Host authority seam 也已补齐，但还不是 Memory repository 实现。
`ProcedureObservationAuthority` 以 ref-only wire 绑定 exact subject/scope/memory revision、TaskScope、admitted
evidence span、terminal receipt/outcome、版本化 applicability fingerprint、risk/hazard、预期 lifecycle transition
及 Run/operation；`ProspectiveSignalAuthority` 绑定 exact typed trigger/hash、scheduler registration revision、
clock/event/ack receipt、outbox（仅 ack）、occurrence 和 lifecycle transition。两者完整 authority 只能由 Host
resolver 返回，校验窗口统一为 `issued_at <= now < expires_at`，并携带 nonce/replay identity；Memory 后续仍须
复验当前 head/scope/receipt，并把 replay fence、decision、CAS 和 outbox 放在同一事务。Procedure applicability
fingerprint v2 使用 exact fields + version 的 canonical domain hash，避免字段分隔符碰撞；Memory pure kernel
必须复用同一算法，避免 exact-wheel 漂移。

Main-model analysis 的 provider delivery 由独立的 `MemoryAnalysisResultEnvelope` 承载：其中 Host
durable `MemoryAnalysisDeliveryReceipt` 必须经 injected authority lookup 验证；Memory 后续产生的
`MemoryAnalysisReceipt` 仍只负责 validator/apply，两者不可互相替代。

2026-09-01 的 relation 增量把 mutation wire 升级为 strict schema v5：Semantic payload 显式区分
`claim | relation`，V1 relation 只允许 `applies_to`，same-plan endpoint 必须经显式 dependency 引用 CREATE，
并限制为 Semantic claim → Procedure/Prospective；普通 mutation target 的同类型规则不变。package-root 公开
validation diagnostic 只输出稳定 bounded reason，不回显不可信输入。跨仓 Memory v7 candidate 已通过原子关系持久化、
公开 committed receipt view 与数字孪生图投影的 exact-wheel 验收；Host durable pre-admission audit 仍未完成。

2026-08-25 Tool/Capability：SDK 0.6.2 起已提供三类 capability record、bounded search/describe、
typed activation receipt、Run-local exposure port 与 ReAct ready-attempt 动态投影；Provider reserved 仍精确
重放原 request。fresh schema v6 分离 legacy Provider specs fingerprint 与完整 envelope digest，exact v5
只能显式 backup-first 迁移。目录可见性不拥有授权、确认、scope 或 effect authority。simple_harness Host
当前工作树将 0.6.2 的 morphology-safe discovery 与 privacy-safe handler diagnostics 固化为本地 candidate
wheel（source `67f5769ca5501f17e37193477d87a149203b6887`，SHA-256
`ffb7c0619851f3c936fcc1d0cf527d07f49e87770291b85e57fe87032ac02c2e`）；这只是本地 candidate
consumption，不是 tag/release 或 production promotion。Host 已修复 SDK authority/legacy
ToolRegistry 的 split scope Store，并把物理 policy fingerprint 冻结进 RunStart，严格 stale 校验仍保留。
真实 macOS UI CAP-1～CAP-5 已覆盖 filesystem、browser、Skill、external-origin policy 与完全重启后的独立
根 Run；重启 Run 从 13 个基线工具重新激活到 16 个，并在 stale nonce 被拒绝后重新 describe/activate 自愈。
Host 随后从 exact wheel 同步，packaged macOS app 在无 `PYTHONPATH` 条件下再次完成 13→14 与真实 README
读取。source `67f5769…` 的最终 reproducible wheel 与完整真测 wheel 的 `simple_harness/` 运行时包逐文件
相同；Host 重锁、重装后又完成一次无 `PYTHONPATH` 冷启动与可操作 UI 冒烟。正式发布稳定线仍保持原版本；
tag、release 上传、download-back 与 consumer promotion 继续分别验收。

- [ARCHITECTURE.md](./ARCHITECTURE.md) — Agent Memory/Context contracts、identity binding、context
  staging、release retry、resource ownership、production builder、installed-wheel Linux ARM64 core gate，以及 Provider/预算、
  结构化消息、工具 catalog 与 projection outbox 权威边界。
- [PROJECT_STATUS.md](./PROJECT_STATUS.md) — 当前 SDK candidate 模块完成度、最近里程碑与跨仓开放门禁。

Human Memory Program 当前只完成 Harness SDK 的 S1 source candidate 边界；不得把后续 Memory SDK、Host
TaskScope、动态 Context、单主对话 UI 或数字孪生体目标误当成已有产品能力。

<!-- last-updated: 2026-09-05 -->
