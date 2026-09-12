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

# Simple Harness SDK 项目状态

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


本页只记录当前 SDK source candidate 的生产事实和仍开放的跨仓门禁。完整协议边界见
[ARCHITECTURE.md](./ARCHITECTURE.md)，目录入口见 [index.md](./index.md)。


## Core audit producer source checkpoint — 2026-09-05

Real lease-bound core intervals, canonical child/workflow/control sources and
continuous recording verification are implemented in the isolated source tree.
Independent missing-whole-preflight findings now have first/later/same-epoch
corruption regressions; causal parent/child and canonical event bindings are checked.
196 directly affected tests plus two workflow relation tests pass; exact installed
old0.7.2 resume executes once and is correctly unverified by the new reader.
**C5 command source93ce163 independently scoped ACCEPT**: explicit audit
schema1, same-transaction version facts and stable pre-Run command pages.
Descriptor-column P2 now returns typed incompatibility before SELECT; schema8 PASS. Adjacent
236 PASS; exact installed072 legacy/middle-writer gaps remain unverified.
**Whole source leaf remains incomplete**: Delivery physical intervals and
independent complete producer review are still required. No version/schema/wheel
freeze or native claim. See [core progress](../plans/2026-09-05-run-operation-audit/CORE-PROGRESS.md).

## Stable Run audit page source successor — 2026-09-05

Metadata correction 6a8b0e4 independently scoped ACCEPT after the original canary
counterexample. Public open/page seams now materialize safe immutable audit snapshots
from one source read transaction, with bounded page reads, original source hashes,
opaque cursor/refs, version checks and restart continuity. Public async audit facades
offload to dedicated mode=ro connections; the runtime writer transaction remains separate. 85 focused/adjacent tests
pass; fixed pagination correction review pending. The6b5d inode-only dataset boundary
was independently BLOCKED; format2 verifies actual immutable Run/start/owner identity
and the captured append-only event cut on every page. The real kernel >256-record and waiting→terminal
prefix oracles pass. Disk/time limits yield unavailable, not partial completeness.
Source only: no frozen version/schema/wheel or installed consumer changes. Full producer
coverage/history gaps remain. See ../plans/2026-09-05-run-operation-audit/PAGINATION.md.

## 2026-09-05 isolated Run operation audit V1 source candidate

From2b842, an isolated follow-up adds public `read_run_operation_audit` (UoW/RunClient)
with safe names, source/call/attempt hashes and links, recorded timestamps/duration and
usage provenance. Pre-effect denial/wait/failure facts and source CAS transitions reuse
SDK-owned run_events; no schema or package-version change. **74 focused tests passed
(1.57s)** plus selected lint. The original 0eb1 metadata projection was independently
BLOCKED for arbitrary error-code leakage; the correction uses closed SDK codes,
registered-tool provenance and opaque join refs, with fixed-source re-review pending.
This is a bounded atomic first slice: current-source
completeness is separate from partial history coverage. Stable pagination and complete
producer enumeration are pending MUSTs; no full-Run/all-SDK audit or native completion.
Frozen main/wheel unchanged; independent fixed-source review and consumer integration
pending. See [contract](../plans/2026-09-05-run-operation-audit/CONTRACT.md),
[coverage](../plans/2026-09-05-run-operation-audit/COVERAGE.md) and
[results](../plans/2026-09-05-run-operation-audit/RESULTS.md).


## 2026-09-05 route 恢复限定修复

- 用户已批准 S5b 的必要 Harness 解冻例外；candidate 0.7.2 修复合法路由演进后授权恢复误拒绝。
- version-zero immutable anchor 与 current route 分别验证，既有 wire/导出不变；新内部 port 要求见架构。
- 针对性 checkpoint 13 passed，现有 runtime 148 passed，执行层/契约 991 passed / 2 fixture skips。
- 完整授权恢复新增 2 passed（文件 14 passed），独立审查无 P0/P1；
  exact wheel、Host 两 root 真实生产验收仍未完成，S5b 仍 BLOCKED。

## Human Memory Program

| 能力 | 当前状态 | 证据与边界 |
|---|---|---|
| S4 Host initial TaskScope route | Harness candidate 已完成 | route receipt v3、ordinary start snapshot v7、ReAct checkpoint v6 已冻结 Host authority、exact run/TaskScope/binding 并对恢复冲突 fail closed；Host execution composition 仍是跨仓门禁。 |
| S1 cognitive/evidence strict wire | Harness candidate 已完成 | schema-v2 cognitive wire、EvidenceItemAuthority v3、RecallDecision v4；Memory SDK 与 Host 产品能力仍需各自验收。 |
| S3 typed recall/use/assembly wire | Harness candidate 已完成 | v4 source-aware decision、Host-only Procedure applicability fingerprint authority、atomic confirmation-group page carrier、单一 budget identity、typed result/page、ContextFragment/assembly v2 与 use authorization/receipt 已冻结；Memory executor/provider adapter 仍是跨仓门禁。 |
| S3 Short-Horizon recall-text authority | Harness candidate 已完成 | Conversation evidence schema v3 将唯一 public_text pointer/hash 与 effective classification、item authority exact 绑定；无授权绑定只保留原始 evidence，不可索引。 |
| S1 Host action authority | Harness candidate 已完成 | `MemoryActionAuthority` schema v2 的 whole-plan commitment 继续由当前 mutation schema v5 承载；它防跨 operation 重放，COMMITTED result/receipt fail-closed 检查 protected refs，CONTEST 不能表达 destructive terminal lifecycle。 |
| S1 Semantic relation mutation wire | SDK 跨仓闭合 | mutation schema v5 冻结 claim/relation discriminator 与唯一 `applies_to`；relation endpoint 支持 existing/created exact ref，created ref 必须显式 dependency 指向 CREATE，且只允许 Semantic claim → Procedure/Prospective。Memory v7 持久化与 graph projection exact-wheel 已通过；Host durable diagnostic audit 仍是后续门禁。 |
| S3 Procedure/Prospective Host authority | Harness candidate 已完成 | 两套 schema v1 ref-only authority 已冻结：Procedure terminal/applicability 与 Prospective scheduler/event/ack signal 都必须 Host resolve，exact scope/revision/receipt/transition binding，半开有效期和 replay identity。Memory consumer 尚待跨仓实现。 |
| Memory repository action-authority consumer | 未完成，跨仓门禁 | Memory 必须单次 resolve 并复验 exact binding，在 mutation transaction 内原子唯一消费 replay identity；同 receipt 的幂等重放是唯一例外。 |
| TaskScope、动态 Context、数字孪生体 | 未完成 | 不属于当前 Harness SDK candidate 的已交付能力。 |

## 最近里程碑

- **2026-09-01 — Harness 0.7.1 Host-initial route candidate**：新增 origin-specific route
  provenance、ordinary start snapshot v7 与 ReAct checkpoint v6；fresh start、restart、tamper、legacy
  compatibility 和 existing-checkpoint conflict 均有聚焦测试。
- **2026-09-01 — Harness Semantic relation schema-v5 candidate**：Semantic claim 显式携带
  `semantic_kind=claim` 并保留 qualifiers；一等 relation payload 只开放 `applies_to`，冻结 exact endpoint、
  dependency DAG、类型矩阵、自环/relation endpoint 拒绝和 strict v5 fail-closed。新增 package-root validation
  diagnostic 仅给稳定 bounded reason，不回显 credential-bearing wire；跨仓 Memory transaction/graph 已通过
  exact-wheel public value 与 40-case integrity matrix，Host durable audit 尚未完成。focused conformance、全量
  1728 tests、ruff 与 mypy 均通过。
- **2026-08-31 — Procedure recall applicability authority**：`RecallContext` 现在绑定 Host 当前可用的
  canonical applicability fingerprint 集合并纳入 context hash；`RecallPlan` 没有对应可写字段，因此主模型只能
  继承这项 authority，Memory 可对 Procedure revision 做 exact current-applicability gate。
- **2026-08-31 — Harness typed Recall v4 candidate**：公开 recall wire 升级为 source-aware v4，
  Short-Horizon-only 与 mixed source 都可精确表达；confirmation 以完整有序组处理。typed result/page、
  use authorization/receipt、recalled ContextFragment v2 和 `(fragment_id, fragment_hash)` assembly 形成
  连续 hash/authority 链，预算上限与 canonical domain 严格验证。本里程碑不包含 Memory
  持久化、Host provider adapter 或真实召回质量验收。
- **2026-08-31 — Harness conversation recall-text authority candidate**：Conversation metadata/receipt/registration
  升级为 strict schema v3；Host 从 verified item authority 一次派生 pointer/hash/privacy/attributes/classification，
  registration exact 复算。未授权 item 保留原始 evidence 但不可进入 Short-Horizon index，旧 v2 wire fail closed。
- **2026-08-31 — Harness S3 lifecycle-authority candidate**：新增 Procedure observation 与 Prospective signal
  两套 Host-owned strict protocol；阻断调用方自证 terminal success、clock due 或 event occurrence，冻结 exact
  scope/revision/trigger/receipt/transition/Run-operation commitment、strict current wire、半开有效期和 replay identity。
  Memory repository 的原子 consumer、scheduler outbox 与状态机仍是下一跨仓门禁。
- **2026-08-31 — Harness S1 action-authority candidate**：新增 Host-owned exact
  action authority、无循环 whole-plan/operation-intent commitment、canonical operation index、ref-only mutation wire、严格旧 wire
  拒绝，以及 `COMMITTED` / `NEEDS_USER_CONFIRMATION` / `REJECTED` typed result。CONTEST
  不获得覆盖或删除权；Memory consumer 的原子 replay fence 与 CONTEST target-state exact-unchanged
  验证仍是下一跨仓验收门禁。

<!-- last-updated: 2026-09-05 -->

## Delivery audit source leaf — 2026-09-05

SDK-owned delivery now records original outbox versions and actual dispatcher
handoff/settlement facts in the authority transaction. Missing settlement remains
unknown; claim/expiry do not prove send or non-send. Immutable CAS receipts prevent
post-commit competing-owner substitution.95 adjacent PASS, real old072 middle
writer remains a history gap; fixedeea3c19 independently scoped ACCEPT.
Delivery-only public parent-ref normalization now joins captured heads (11 PASS). Whole producer
source and successor artifact are not complete. See
[Delivery contract](../plans/2026-09-05-run-operation-audit/DELIVERY.md).

## Canonical audit source associations — 2026-09-05

Terminal projection preparation, Provider projection receipt, wait blocker and
continuation-consumed context staging are included via canonical Run relations.
Five decisive source omissions red→green;136 adjacent PASS. Existing bytes/receipts
are reused, with no extra Provider accounting. SDK-owned committed-turn/release
port-call history and complete producer review still prevent all-operation/artifact
completion; Memory SDK internals remain outside this leaf. See core progress.

## Harness committed-turn port audit — 2026-09-05

SDK MemoryDispatcher records actual handoff/receipt with original owner/claim epoch;
logical APPLIED and receipt REJECTED_ERASED remain distinct. Same-transaction facts
survive legitimate outbox cleanup via final-hash tombstone. Transaction-local CAS
results prevent competing-owner substitution.31 directly related PASS plus6 leaf
PASS; exact072 middle call remains missing-epoch gap. This is source-only, fixed
review pending. No-Run Context prepare/release staging still requires the explicit
schema/association contract in
[Memory port contract](../plans/2026-09-05-run-operation-audit/MEMORY-PORT.md);
whole operation coverage and successor artifact are not yet complete.

## Committed-turn independent P1 correction — 2026-09-05

ba1d6ec was BLOCKED for caller-payload substitution and whole-family loss after
cleanup. New correction checks the actual immutable outbox row in begin/settlement
and derives a committed-turn hash in the canonical terminal receipt, independent
of audit-family retention. Consumed legacy cursor proof is reused; absence stays
unverified. Original terminal receipts are not restamped.35 direct/67 terminal
adjacent/1 cleanup replay PASS; fixed-source independent re-review required.
No schema2 WIP or artifact completion claim is included. See MEMORY-PORT contract.

## noRun audit schema2 source candidate — 2026-09-05

Implemented default stage-domain Context/release recording, explicit audit1→2,
actual consumed Run association and stable pagination. Direct schema/kernel/start/
page tests79 PASS4.90s; overlapping stage positive5 PASS0.28s. Real exact072 structural
claim/complete/cleanup remains recorded with missing-call/result gaps. No paid Provider,
native, full suite or wheel build. Independent fixed review pending; new terminal
payload's real Host installed exact comparison remains mandatory. This is not whole
Agent-operation completion or Memory-internal coverage. See MEMORY-PORT.md/RESULTS.md
under plans/2026-09-05-run-operation-audit; original counterexamples retained locally.

2026-09-06 fixed review follow-up: noRun9756 has two P1s; current successor adds actual
Run stage-cut revalidation and structural generation anchors. Late old release keeps
its real outcome without settling a recreated queue. Narrow45 PASS; overlapping11
PASS after exact generation-result proof. Independent revalidation still pending,
artifact freeze prohibited until closed. See MEMORY-PORT.md original red indexes.

2026-09-06 stage pagination cost correction: saved cut/binding validation now uses
only exact stage-event PK lookups and, for continuations, one actual continuation PK
owner lookup. Public SQL trace/query-plan oracle and old-binding rejection pass;
23 directly adjacent PASS overlaps the 2 indexed root/cut cases, plus 1 continuation
case. Fixed independent review pending; no new ledger/wheel or weakened Host compare.

2026-09-06 noRun stage source fixed d6c1951 independently scoped ACCEPT; original
9756 P1s preserved, no remaining leaf-blocking P0/P1. 23/2/1 evidence reviewed with
SQLtrace/EXPLAIN and wrong-Run/cut negatives, no repeated suites or additive count.
Source leaf may be frozen; successor wheel/installed publicconsumer/real Host terminal
exact compare and broader producer scope are not certified by this review.

2026-09-06 public exact terminal field implemented for root/ordinary Host audit
consumption; source29 PASS (typed final1 overlaps), independent source review pending.
It separates stored payload SHA from whole-row SHA; no weakening Host compare, no
new ledger. Artifact remains pending this last direct consumer primitive review.

2026-09-06 terminal source continuation correction:36 adjacent PASS; saved exact
terminal row is point-validated even when later events exist. Original red retained.
Fixed-source review pending before one successor candidate build.

2026-09-06 terminal projection originalfd/c313 not accepted: final same-TX state,
unique event and saved metadata closure implemented with explicit audit2 read index.
Direct25 PASS plus overlapping1, actual072 index-write positive with legacy gaps.
No new wheel yet; waiting this necessary Host-consumption source fixed review.
