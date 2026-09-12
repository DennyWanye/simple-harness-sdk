# P3.3 E 执行细化

最后更新2026-09-12；18:53开始。基线fc43d6f已推送0/0；D源码d3d3fd8完整编排1119 passed /8 skipped /446.64秒，提交后542 passed /8.72秒，无残留。E只读准备与D回归重叠，不重复计时。

## 范围、定义与独立挑战

原plan v3、46条AC、同一整体run不改；覆盖15/16/23/24/25/33/34/35a/35b/44及41撤销冲突边界，36每片回归。F/G与Host结论区留后续。已读Host theory04 Provenance与10 Conflict/Single Writer原定义。

Kepler与Ohm独立挑战后锁定：

1. 直接citation的新接受：真实冻结receipt通过后、assessment/Claim/Knowledge写入前，同一事务复核实际引用来源仍有效且CAS可读。supersede/revoke→stale_source，阻止本次接受并进入普通重新验证；未使用冻结来源变化不拒。错误读取是ERROR，不伪装stale，不算D missing-limitations重试。Resolver仍只按冻结map解析，原rule receipt不重解释；DONE/PASS重复接受先走历史幂等。
2. 仅used_knowledge来源过期：KnowledgeIndex.stale独立派生，check函数体一个字不改；retrieval排除并标明原因，rule_check与accept的KnowledgeIndex.check不得因stale而FAIL。历史claim/knowledge/assessment一个字不改。新的派生知识继承全部依赖，不能一跳洗白。
3. KnowledgeRecord.source_versions新增可选Mapping[path,tuple[hash,...]]，同path多版本全保留、排序去重；缺字段None旧编码省略。新projection合并自身已验证refs与接受前已存在的依赖。旧记录从真实verifier evidence_refs+dependencies递归只读派生；空字段不能洗掉已有refs。循环与diamond区分；缺失/跨Mission/未知来源不能返回部分map冒充clean，unknown_source_provenance需传递；非空issues保存在既有verifier JSON。读取故障保持ERROR。
4. 冲突范围只加注：source_versions、完整checked_scope与来源条件保存在sides；无scope=全域，不引入交集代数。相同key反stance仍进入争议；原code分支不变。第三方加入已有冲突同步完整sides/claim_ids及版本，不能只追加ID。
5. 文档ConflictTask首次通过必要非人工检查到human_review时，直接创建唯一arbitration等待，不先创建普通review，不等预算耗尽。actual FAIL/ERROR不能由人覆盖；写侧accept_result也拒绝普通review绕过doc冲突裁决。
6. 复用approvals/decisions/HumanOverride/ConflictResolvedByHuman，doc选项keep:<side>、contextual、unresolved；code选项不变。contextual只表示人工认定双方在所列条件下分别成立，要求非空basis。绑定同Mission/Conflict/Task/result、产物/规则receipt、最终真实Claim版本与全部成员证据范围，裁决事务重查。原assessment revision独立保留，不被最终Claimversion覆盖。
7. Doc仲裁请求ID可含快照hash，subject保持原Conflict ID。成员增加使旧pending请求失效，可申请新请求；重复同决定幂等，跨Mission/错方/过时绑定拒绝。单纯revoke不重写历史sides，不取消或解决已开的冲突。
8. 裁决保留双方Claim等级、disputed标记和证据，不创建VERIFIED Knowledge、不做source有效性恢复。既有机制取消ConflictTask并解除synthesis阻塞；上下文显示人工条件性裁决与范围。无新事件/信封字段/DB schema。

## 接口与分工

所有测试由主线程串行运行；子代理不pytest/commit/push。先写测试，再通知主线程记录red，获得实现启动消息后写生产。

- Ohm：memory/verified_knowledge.py、context/retrieval.py、新memory/source_dependencies.py；test_p33_source_dependencies.py。
- Kepler：orchestrator/commit_service.py、orchestrator/human_commits.py；test_p33_source_commits.py、test_p33_doc_arbitration.py。当前引用接受检查、projection并集调用、sides、专用人工挂起/裁决由其负责。
- 主线程：event_handler接线、memory/summaries.py只读context失效过滤、实际runtime测试、架构/计划/证据索引；不触碰两位ownedfiles。

锁定接口：

```python
source_dependencies_for(store, *, mission_id, evidence_refs, used_knowledge)
# -> (dict[path,tuple[hash,...]], list[issue]); 只读真实历史provenance，供projection继承
merge_source_versions(...) # pure canonical并集
source_current_issues(store, mission_id, citations, artifact_store) -> list[dict]
# 仅actualcitations；直接stale与ERROR分类，不混入继承issues
KnowledgeIndex.stale(ids=None) -> dict[knowledge_id,list[issue]]
rank_knowledge(..., stale=...) # 可选，不改变旧code无stale路径
```

human suspend专用接口由Kepler复用suspend_verification内部doc分流，保持event_handler已有入口；runtime仅补必要恢复/仲裁上下文接线，不重复写状态。

## 先行oracle

| ID | 操作与预期 |
|---|---|
| E01 | actual resolver+rule PASS后，经facade真实审批supersede/revoke再accept→stale_source，无新assessment/VERIFIED；未引用来源变化正对照仍接受 |
| E02 | 先接受再更新来源，历史claim/knowledge/assessment字节不变，重复accept不写；读取历史证据仍能回读原版本 |
| E03 | stale知识检索排除且带原因；同used_knowledge的rule与accept TOCTOU不因stale失败，原check AST不变 |
| E04 | 多跳综合继承同path A/B版本并集；撤销任一后下游仍stale，diamond有效，cycle/未知不能伪clean |
| E05 | 同key反stance，范围无交集/缺scope仍争议；完整双方sources/conditions保留；第三方加入更新目录 |
| E06 | 实际docConflictTask非人工层通过即唯一arbitration等待；普通review、伪PASS、实际FAIL/ERROR不能绕；code原路径无contextual |
| E07 | 实际人工keep/contextual/unresolved、关库重开、重复nonce；contextual后等级/Knowledge数量不变，ConflictResolvedByHuman回放一致 |
| E08 | 错方/跨Mission/过时binding拒，新增成员旧请求失效且可新申请，单纯revoke不消解冲突 |
| E09 | 实际synthesis用doc模板无code_test完成，继承来源并集、关键结论来自success_criteria；未知/stale不通过summary/candidate上下文暗中洗白 |

完成门：定向红绿、实际SDK运行/重开、独立累计审查、mypy/Ruff/legacy AST、干净HEAD完整编排、架构与交接回写、提交推送。不是P3.3整体完成。

接口最终锁名：issue.code=stale_source|unknown_source_provenance|ERROR；verifier非空issues字段source_provenance_issues。ERROR传到context时为RetrievalUnavailable。Ohm独立认可摘要只读过滤：自身产出或真实used依赖命中stale才替换历史摘要，不影响无关联Task/无stale旧hash。

E 干净源码 `cf40b8ec86a2f307d0f8b8f89cf7f0166e5de121` 完整编排 **1199 passed /8 skipped /0 failed**（481.36秒），watchdog481.63秒，PG25036无残留。8项skip为未启用真实Provider。A–E完成SDK源码验证；F/G、wheel、Host与真实flash仍未完成。
