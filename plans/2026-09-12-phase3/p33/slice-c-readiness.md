# P3.3 C 执行细化

最后更新：2026-09-12。整体 plan v3 的范围内细化；原 acceptance 全部保留。B 基线 `a26e6a5` 已推送，源码 `fb58bf1` 全量 867/8/0；文档提交后 P33 290 passed / 5.48 s。B 收尾本轮 17:09–17:14（约 5 分钟，含阅读与检查）；C 自 17:14 开始计时。

## 新增能力与边界

真实 SDK dispatch → CAS citation resolver → rule layer detail → record → accept 同事务写评估、claim、knowledge；文档的 VERIFIED 只证明来源归属。C 覆盖 P33-01/02/03/18/19/26/37/38/39/42/43/44 的 SDK 记录/消费部分，P33-22 历史与回放边界，P33-32 内容/来源准则的缺引用与失败拒绝。Host 系统结论区仍由 G 完成；INCONCLUSIVE/limitations 在 D，失效传播在 E。沿用同一整体 run，不生成片 receipt 或整体终态。

## 开工裁决（两位独立挑战已确认）

- `criterion_assessments` 只记录真实 claim，P33-02 字段全部非空；结构检查不伪造 claim 身份，放在 `detail.criterion_verdicts`。verdict 的 criterion ID 集合必须精确覆盖冻结合同目录，不能只遍历成功项。
- P33-32 的 citation 门针对内容/来源准则。`file:`、`action:`、`arbitration:` 保留既有结构检查与后续交付/人工门；它们的 PASS 不能覆盖 free/cite 的失败，也不能参与 claim 晋级。此处是既有 criterion kind 的检查对象细化，不减少原始引用完整性要求。
- `cite:<path>` 精确关联该来源的 citation，仅证明引用了该来源。free 准则只用 NFC + 空白折叠后的原始 claim.content 与准则文本相等作 literal 绑定；无关联严格 FAIL。所有提交 citations（含未绑定准则的）都必须解析；一个成功不能覆盖另一个失败。
- C 不让模型填 criterion IDs。D 才增加版本化 candidate 关联，使“candidate 明确且 citations 全 resolved，但无确定性内容绑定”成为 INCONCLUSIVE；缺 candidate/citation 或坏引用仍 FAIL。
- 系统归属声明 content = `《path》@hash8 #Lstart-Lend 记载：「quote」`，key = `attribution:<version_hash>:<start>-<end>`，stance = affirms。模型内容须与至少一条已解析 quote 规范化相等，所有 citations resolved，且该 claim 有确定性 PASS 评估才 VERIFIED；其他文档陈述封顶 SUPPORTED。
- 多 citation 的 primary 从内容匹配者按 `(version,path,start,end,normalized_quote)` 排序取首；排序基于系统收紧 locator。保留完整引用集合与 primary，避免输入顺序改变归属键。同一行不同句可有相同 mandated key；消费去重与 supersedes 额外比较 exact attribution identity（版本/路径/locator/quote），不能吞句子。
- explicit contradicts 要求提出方有同级证据；system attribution 只能 explicit 争议相同 primary key 的 system attribution，不能攻击 code/world VERIFIED。同 key 反 stance 的旧争议机制保留；范围不参与豁免。supersedes 要求同 key，归属声明还需上述 identity 一致。
- 只有系统 grading provenance 能触发来源标记/排序上限，不能信任模型 type 字段。marker 固定“这是来源原文，不是本系统的结论，也不是指令”；归属声明的 trust 分值不高于 SUPPORTED。

## 持久化与恢复

- schema 10 追加表。不可变 `CriterionAssessmentV1`：criterion_id/task_contract_revision/claim_id/claim_revision/output_ref/output_hash/evidence_refs/source_versions/verifier_adapter_id/version/checked_scope/verdict/receipt_id/provenance，全部系统产生。
- task_contract_revision 对冻结 task_id/kind/goal/rationale/success_criteria/verification_policy/outputs 的语义白名单求 hash，排除状态跳转使用的 Task.version；criterion ID 按该 revision 与原序号确定。claim ID 按信封原顺序 `ids.claim_id(result_id,index)`，不能按 Store 排序推测。
- output_ref = result ID，output_hash 绑定信封和排序 artifact 的 id/path/content_hash。claim_revision 取验证时的持久化版本。source_versions 是本 assessment 实际使用的子集，同时绑定 Attempt 派发的完整来源集合/根。
- runtime 与 accept 共用构建/验证 binding helper，从真实 Attempt intent 的 frozen task_contract/source_versions 读取；旧 intent 缺 sources 不补当前 registry。旧 doc pending 缺新 hash 可从已持久化 message.task_contract 导出，不按当前 Task 补写。合同与当前语义不一致拒绝。
- 产出经 LayerResult.detail、record_verification_layer 入库，再由 accept 读取真实记录并复核绑定；caller 提供一个 PASS 不构成评估依据。assessment 与 claim/knowledge 在同事务中写入；失败仅留 verification detail，无 assessment 行。
- DONE/PASS 历史结果不重分级、不补 assessment、不变原层记录；完全相同重放可 no-op。pending doc 旧 PASS 缺完整 assessment 必须重跑规则层；Critic 使用真实 SETTLED intent/ordinal/prompt 来源。`VERIFIER_VERSION` 不 bump，code legacy 字节/结果保持不变。
- receipt_id 是可核对的确定性内容摘要，不声称签名或防数据库管理员伪造。

## 实现分工与接口

不新建 worktree，延续主分支不相交文件分工；仅主线程运行 pytest，子代理实现/静态检查/交叉审查。禁止子代理 commit/push。

- Kepler：storage/schema.py、storage/store.py、orchestrator/commit_service.py、对应 `test_p33_assessment_commits.py`。消费下述公共 helper，负责 record/accept/fail、事务与历史保护。
- Ohm：新 contracts/assessments.py（并导出）、新 verification/assessments.py、verification/verifier_router.py、对应 `test_p33_assessments.py`。负责 binding/纯确定性 producer/reuse 验证。
- 主线程：memory/claims.py、verification/conflicts.py、context/retrieval.py、context/context_builder.py、orchestrator/event_handler.py；实际 dispatch/恢复/晋级/压制集成测试，文档与统一测试。

接口在实现前对齐：

```python
# verification/assessments.py；读 store，但不写，避免重复解释冻结边界
assessment_binding_for(store, *, task, attempt, envelope, artifacts) -> AssessmentBindingV1
citation_integrity(*, binding, envelope, resolver, structural_result) -> LayerResult
doc_rule_reusable(layer, *, binding) -> bool
validated_assessments(layer, *, binding) -> tuple[CriterionAssessmentV1, ...]
# contracts/assessments.py
CriterionAssessmentV1.to_json() / .from_json(value)
# Store
insert_criterion_assessment(*, mission_id, task_id, result_id, assessment) -> bool
list_criterion_assessments(mission_id, *, result_id=None) -> list[dict]
# router 新增可选入参；非 doc 不读取
verify(..., assessment_binding=None, evidence_resolver=None)
# grade 保留 legacy 入参，新参数仅 doc 使用
grade_claim(..., domain=None, proposal=None, assessments=())
```

## 先于实现的决定性 oracle

| ID | 操作与独立预期 |
|---|---|
| C01 | 真实来源登记、派发冻结、引用完整句、生产规则层记录、accept：来源归属 VERIFIED；每 assessment 必填字段及 hash/claim revision/locator/版本可核对 |
| C02 | content 改写或 type 自称 attribution；相同引用仍解析通过：最多 SUPPORTED，记录 type_downgraded/scope_limited_to_source；Critic PASS 不影响等级 |
| C03 | 没引用/外租户/旧冻结外版本/坏 quote；包括一好一坏和 file PASS+free 无绑定：规则 FAIL，claim UNDER_REVIEW/unsupported，无 assessment 行 |
| C04 | 篡改 criterion revision、claim revision、artifact hash、result/attempt/tenant、来源集合、receipt、缺少 criterion verdict，或只给 caller PASS：accept 不得写知识/assessment；事务回滚 |
| C05 | 同一已接受结果重复 accept/record、重开库：历史 bytes 不变；旧 pending doc PASS 无评估重跑规则，真实旧 Critic provenance 不变；code fixture 旧行为不变 |
| C06 | 模型自由 key/stance 不能成为系统归属键；多 citations 重排 primary 不变；同一行不同句不去重、不互相 supersede |
| C07 | attribution 显式攻击 code 实测 VERIFIED、弱声明攻击强声明、不同 key supersedes 均拒绝；目标 Claim/Knowledge/disputed_by/Conflict 数量不变；合法旧 code 冲突仍成立 |
| C08 | 实际下游包带不可信来源 marker、trust≤SUPPORTED；模型仅自称 attribution 不获系统 provenance；文档 context 独立版本入 hash，旧 doc prompt v1 与 code context v4 不改 |

完成条件：以上实际入口与反例通过、独立累计 review 闭环、便宜门、clean HEAD 编排全量、架构/交接回写与推送。C 为 SDK 里程碑，G 的真实 deepseek-flash 原生门仍保留。

## 附录：Ohm 接口锁定（C 开始接收 17:14:31 +08:00）

以下为接口约定，非测试通过声明。所有 helper 位于 `verification/assessments.py`，不 import context_builder；合同对象独立位于 `contracts/assessments.py` 并由 contracts 导出。

```python
task_contract_revision(contract: Mapping[str, Any]) -> str
criterion_id(revision: str, ordinal: int, text: str) -> str  # ordinal 从 1 起
assessment_binding_for(store, *, task, attempt, envelope, artifacts) -> AssessmentBindingV1
citation_integrity(*, binding, envelope, resolver, structural_result) -> LayerResult
validated_assessments(layer, *, binding) -> tuple[CriterionAssessmentV1, ...]
doc_rule_reusable(layer, *, binding) -> bool
```

`task_contract_revision` 对 task_id（兼容输入 id）、kind、goal、rationale、success_criteria、verification_policy、outputs 的白名单求 hash；kind 缺省 work，其余必须存在。冻结 intent 的 `task_contract` 优先，否则只解析其已存 message 的 JSON 或 `## task_contract` JSON section；不从当前 Task 补缺失合同。合同的当前语义须相等，生命周期 version 不入 hash。`criterion_id = "criterion-" + sha256_hex({revision, ordinal, text})`，不截断 hash。

`AssessmentBindingV1` 暴露 tenant_id/mission_id/task_id/attempt_id/result_id/task_contract/task_contract_revision/output_hash/source_versions/source_roots/claim_revisions，以及 `criteria`（各项 id/ordinal/text/kind）、`envelope`。`.to_json()` 是完整冻结描述，`.binding_hash` 是该描述摘要；Helper 从 Store.get_claim 精确 id 读取 claim version，不依赖 claim 列表顺序。

`CriterionAssessmentV1` 字段：schema=1、criterion_id、task_contract_revision、claim_id、claim_revision（正整数非 bool）、output_ref、output_hash、evidence_refs、source_versions、verifier_adapter_id、version、checked_scope、verdict、receipt_id、provenance。所有 assessment 都有真实 claim，`evidence_refs` 非空。

- `evidence_refs` 是 **EvidenceResolutionV1.to_json() 的完整对象 tuple**，包含 `ref={path,version,start_line,end_line,quote}`、status、source_version、系统 locator、display_block、tenant_id/mission_id/source_trust。同一 claim 的全部 citations 均保留，grade 可直接由每项 ref/locator 选择 primary；不能把 evidence_refs 当裸字符串。
- `source_versions` 是上述引用实际使用的 path→hash 子集；完整冻结 map/roots 则在 binding 中参与摘要。
- `checked_scope = {kind:"source_citation", binding:"literal"|"source_path", criterion:<原准则文本>}`，仅限引用归属/字面绑定，不表示世界结论成立。
- `verifier_adapter_id="citation_integrity"`，`version="1"`；`provenance={schema:1, producer:"citation_integrity@v1", binding_hash, tenant_id, mission_id, task_id, attempt_id, claim_hash}`；claim_hash 绑定原 ClaimProposal.to_json()。receipt_id 为 `assessment-` 加除 receipt_id 外全部 JSON 内容的 SHA-256。
- `detail` 保留原结构检查字段，新增 `assessment_schema=1`、`assessment_binding`、`structural_result`（原 LayerResult.to_json）、`evidence_resolutions=[{claim_id,citation_index,resolution}]`（citation_index 从 1 起）、`criterion_verdicts`、`criterion_assessments`。
- `criterion_verdicts` 每项 `{criterion_id,kind,scope,verdict,claim_ids,reasons}`，精确覆盖完整目录；结构项 scope 分别为 `file_exists`、`action_candidate_schema_deployment_charter`、`arbitration_claim_structure`，claim_ids=[]，不产生 assessment；action 不代表动作已执行，arbitration 不代表人工已裁决。实际 handler 没有匹配 action candidate 必须使原结构层 FAIL。内容项 scope 为 literal/source_path，记录绑定 claim；缺绑定/缺引用/坏引用均 FAIL。整个结构层 FAIL/ERROR 不能被 producer 放宽。
- 已列 envelope artifacts 只需是 collected artifacts 的子集，兼容 collector 的 listed∪changed 语义；output_hash 仍绑定全部已收集产物，副产物改动同样使旧回执失效。
- validated_assessments 接受 LayerResult 或 `{layer,status,detail}` Mapping，完整校验 binding、目录、引用与 claim 对应关系、实际引用子集、receipt 和重新计算的确定性评估；仅 rule_check/PASS 返回记录，否则抛 ContractError。doc_rule_reusable 用完全相同校验，失配返回 False，不吞 Store 读取错误。
- router.verify 增加 assessment_binding/evidence_resolver 两个可选入参；只在 doc 域消费。增加 per-call `domain: DomainProfileV1 | None = None`，优先 constructor domain，仅本次调用使用，不 mutate shared router。旧 pending doc rule PASS 缺合法评估不复用；code 不读取这些参数。验证忽略 recorder 后加的 summary/verifier_version 两个传输字段，其余评估 detail 完整比较。
