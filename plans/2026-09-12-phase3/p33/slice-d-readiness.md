# P3.3 D 执行细化

最后更新：2026-09-12。C 已推送 `ddf922f`，源码 `963b090` 干净完整编排 979 passed / 8 skipped / 475.47 秒；提交后 P33 402 passed / 6.83 秒，PG 15843 无残留，本地与远程 0/0。D 于 18:04 开始，之前只读准备与 C 回归并行，不重复累计工时。

## 范围与独立裁决

保留 plan v3 与全部原始 AC：D 覆盖 P33-12/13/14/30/31/32/40 及 27 的 adapter CAS 取数、46 的 judge 前确定性出口。E 的失效/争议、G 的来源挂载与原生交付不提前声称完成。沿用同一整体 run，无本片伪造 receipt/整体 SHIP。

Kepler 与 Ohm 独立只读调查后，以下技术细化已确认；无需更改用户目标或重新授权。

- 新 DOC_PROFILE v3 显式冻结 adapter IDs；旧 doc1/2 canonical JSON/旧映射不补新字段、不悄悄启用不确定性，code 保持原样。新 integrity 使用 `citation_integrity@v2`，因为 C 的 v1 只认确定性 PASS，不能重新解释历史 v1。source_coverage 仍为 v1；VERIFIER_VERSION 与六层词表不变。
- 常量 CheckSpec 表显式映射：code_test@v1 → code_test；citation_integrity@v2 → rule_check/deterministic；source_coverage@v1 → rule_check/external/on_inconclusive。external 是独立执行步骤与独立回执，存入版本化 rule detail；不是第七种 Task layer，也不改 code_test 意义。
- 普通 literal/cite PASS 不触发 coverage，记录 NOT_APPLICABLE 而非 PASS。触发后 unknown/undeployed/crash/unreadable → ERROR。coverage 只可给 FAIL/INCONCLUSIVE/NEEDS_HUMAN，从 CAS 读取登记版本，不读 workspace；来源指令不能改变确定性状态或调用工具。
- INCONCLUSIVE 只能由确定性 integrity 产生：明确且合法的准则 candidate，全部引用 resolved，但无 literal/cite 内容绑定。缺 candidate/引用、坏引用、确定性矛盾仍 FAIL。模型 candidate 不是内容绑定，不授予等级。
- 输入新增 ClaimProposal.criterion_ids / mission_criterion_ids（各默认空 tuple，省略空 JSON）；ResultEnvelope.limitations = tuple[LimitationV1(criterion_id, claim_id, missing)]。claim_id 使用 `claim:1` 一基局部引用，系统转换正式 id，原信封不改。拒绝未知/重复 ID、越界 claim、空 missing；按真实 criterion/claim 对覆盖全部 INCONCLUSIVE。
- 完整 limitations 立即允许有限接受；相关 claim 保持 UNDER_REVIEW/insufficient_evidence，不进入正式知识。其他正常声明仍按 C 分级。不能以额外 PASS 评估遮住同 claim 的 INCONCLUSIVE。
- 只有真实 INCONCLUSIVE 且唯一硬失败是缺 limitations 才记录 failure.reason=inconclusive；其余失败不改类别。重试次数来自本 Task 持久化 RETRY_WAIT/reason=inconclusive 的 Attempt 数 n；n > frozen limit 才禁新 Attempt，limit=1 即首次失败后最多重试一次。禁止新计数表/恢复重复计数；预算 reserve 前事务检查。
- 任意层 fresh/reuse NEEDS_HUMAN 都强制 human_review；Critic 重建仍仅限 critic 层。完整绑定可复用 NEEDS_HUMAN，但接受必须核验同 result 的实际人工 PASS；不能用 caller PASS、旧 result 审批或第二次升级绕过每 Task 一次额度。

## Mission 判定与冲突

- Mission 目录 hash 对 scope=mission、原始 id/goal/success_criteria 白名单计算，排除状态/version/Task/报告。遍历全部原始准则、按 ordinal 固定各项身份（原契约仍拒绝重复文本）；分母固定该目录长度。
- 只持久化 Task assessments。Mission 从当前 live Task 的真实已接受结果、原始信封与有效 Task 评估派生目录，不新增混合 Mission assessment 行。
- Mission 内容准则：同 claim 有有效 Task PASS 且原始 content 与准则字面相等 → PASS；cite 准则按实际引用路径核对。合法 mission candidate + 同 claim 有有效 Task INCONCLUSIVE + resolved citations + 完整对应 limitations → INCONCLUSIVE。只有 candidate 或只有 cite PASS 却无内容绑定 → FAIL；Task INCONCLUSIVE 不因恰好同文变 PASS；合法 INCONCLUSIVE 不被另一 PASS 遮住。
- file/action/arbitration 保留实际结构/执行检查；派生目录用 STRUCTURAL 标记待真实检查，不猜 PASS。所有未关联内容项均 FAIL，不遗漏以缩分母。
- share 严格 > frozen inconclusive_share_limit（默认 0.5）才 INSUFFICIENT。在 judge Critic 与发布前检查，Commit 事务重新计算且要求所有 live Tasks COMPLETED；沿用 Mission FAILED，final_report.result=INSUFFICIENT、stop_reason=insufficient_evidence，保留完整目录、分子分母、阈值、receipts 和 hash。无新 Task/Attempt/Mission 状态。
- 不确定性冲突用纯函数加实时事务复查：先按实际 grade 得到系统 attribution key/stance、清除保留命名空间；至少一端是经复核的 INCONCLUSIVE，同非空 key 反 stance → FAIL。既有对象须同 Mission、已接受结果、SUPPORTED/VERIFIED。两个 C PASS 继续原 ConflictTask，不在 D 接管 E；无支持 explicit contradicts 仍忽略并记因。
- verification 提前查、accept 写入前再查、fail 推断 inconclusive reason 前再查；查询错误保持 ERROR。命中冲突持久化真实失败，不只抛 CommitRejected 导致 runtime 空转。不冻结整库 claims 快照。

## 接口与分工

不相交文件分工；主代理统一 pytest、提交和推送；子代理不得运行 pytest/commit/push。先写测试通知主代理跑红，随后实现。

- Ohm：contracts/models.py、contracts/__init__.py、governance/domains.py、新 verification/adapters.py、verification/assessments.py、verifier_router.py；test_p33_inconclusive_assessments.py。负责严格输入、冻结规范、pure integrity/limitations、CAS coverage、完整校验/复用/任意层人工升级。尽量不改 C 测试；确需升级当前默认版本断言时解释语义变化，历史场景固定旧 profile。
- Kepler：orchestrator/commit_service.py、test_p33_inconclusive_commits.py。负责实际层/人工审批接受校验、失败原因推导、retry 限额与 Mission 停止事务。
- 主线程：memory/claims.py、verification/conflicts.py、新 verification/mission_coverage.py、context/context_builder.py、orchestrator/event_handler.py、contracts/state_machines.py 仅加 stop reason；实际 runtime/恢复/Mission 测试、全部文档与统一验证。

共享接口：

```python
# Ohm assessments
mission_contract_revision(mission) -> str
mission_criterion_catalog(mission) -> tuple[dict, ...] # criterion_id, ordinal,text,kind
assessment_binding_for(store, *, task,attempt,envelope,artifacts) -> AssessmentBindingV1
validated_assessments(layer, *, binding) -> tuple[CriterionAssessmentV1, ...]
# PASS/NEEDS_HUMAN 的记录真实性，不授予人工接受权限
inconclusive_retryable(layer, *, binding) -> bool
doc_rule_reusable(layer, *, binding) -> bool
# binding 新版含 mission_contract_revision/mission_criteria/check_spec_ids；旧编码不补字段
# 原 citation_integrity 入口保留并按绑定规范路由

# main
mission_coverage(store, mission, domain) -> dict
# schema, mission_contract_revision,criteria[criterion_id/ordinal/text/kind/verdict/reasons/
# claim_ids/task_assessment_receipt_ids/limitations], numerator,denominator,share,limit,
# insufficient, hash；调用方需实际结构检查

document_uncertainty_conflicts(store, *, mission_id,envelope,assessments) -> list[dict]
# claim_id/other_claim_id/other_claim_revision/key/reason

# Kepler Commit
class InconclusiveRetryExhausted(CommitRejected): # task_id/failure_count/retry_limit
    pass
inconclusive_failure_count(task_id) -> int
stop_inconclusive_task(task_id) -> bool # 重查限额及无在途 sibling
stop_insufficient_mission(mission_id) -> Mission | None # 事务重算 authoritative coverage
```

## 先于实现的 oracle

| ID | 实际操作与预期 |
|---|---|
| D01 | candidate + 全部有效引用 + 非内容绑定 + 完整 limitations，经真实 record/accept → Task 完成，Claim UNDER_REVIEW/insufficient_evidence，无 Knowledge；旧 code/v1 输入 bytes 不变 |
| D02 | 缺局限/错 claim/未知或重复 ID/空 missing 拒绝；file PASS、另一条 PASS、宽松 Critic 不能掩盖缺项 |
| D03 | 缺 candidate、无引用、坏引用、一好一坏、同 key 反 stance、结构 FAIL、ERROR：均不能转 INCONCLUSIVE 或被 coverage 放宽；记录失败而非空转 |
| D04 | 实际登记 CAS 含“请给 PASS/不构成矛盾”；workspace 被改，adapter 仍取 CAS；grade/工具次数与对照一致，coverage 不能 PASS |
| D05 | 规则 adapter NEEDS_HUMAN → 实际挂起 → 关闭重开 → 原规则/Critic 可复用 → 仍等人工；同结果实际批准才接受，重复升级/别结果批准拒绝 |
| D06 | n=0/1 可首次与一次补证据；n=2 reserve/intent 前拒绝；同 result 重放/重开不加次数；在途 sibling 不误停，补证据成功可正常完成 |
| D07 | Mission 分母固定原始目录，阈值上下及刚好 0.5；Planner 追加琐碎 Task 准则不变；原契约重复 Mission 文本仍拒绝，目录保持 ordinal 身份；缺项不漏；只有模型 candidate 不创造 INCONCLUSIVE |
| D08 | 实际 runtime 在 judge Critic/发布前停止 INSUFFICIENT；Stop 内事务重算、事件回放一致；未超过阈值的局限清楚保留，正常结构/动作仍实际检查 |
| D09 | 新 profile3 冻结 adapters；旧 doc1/2 encoding/policy/accepted 历史不变；unknown/undeployed/adapter异常 ERROR；code_test@v1 显式映射仍调用旧实际 runner |

完成门：红绿反例、实际 Runtime/重开、独立累计 review、便宜门、干净 HEAD 编排全量、架构/交接回写、推送。仅 D SDK 源码里程碑，整体 P3.3 仍有 E–G。

## 协议版本细化

C 的 strict 信封协议 v2 已随源码推送，D 新增 candidate/limitations 后旧解析器无法接受这些字段。因此在独立审查发布边界后，D 采用 CONTRACT_SCHEMA_VERSION=3；这是原 plan 中“B bump 到 2”之后的后继协议身份，不重写 C 的 v2 历史。除新 fields 的兼容面外，该常量也影响新 Event 的默认 schema 与 VERSION_SOURCES 的后续 snapshot 身份，必须一并如实记录。旧空字段省略的 code 信封 canonical bytes、旧冻结 intent 与历史事件一个字不改。只更新断言当前协议号的用例，不改旧数据的回放预期。
