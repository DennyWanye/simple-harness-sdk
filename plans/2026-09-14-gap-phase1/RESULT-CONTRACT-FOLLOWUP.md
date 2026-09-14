# N3 结果封套与困难消费后继

最后更新：2026-09-15 02:25 CST。完整N1–N8仍OPEN；本文件分开记录自然失败、受控恢复和新源码验证。

## 已结束的真实本地运行

N2 v4官方AppWorld/F任务530b157_1仍FAIL：28次物理调用、912645tokens/未知0，1557.359秒（受管1561.140秒），40工具效果。Planner分配1.2M/1.5M/1.3M；第一Task因自身预算终止，仅1Attempt，后两Task未执行。此处是既有SDK的Task预算准入拒绝，不是新evaluation meter适配触发；meter admission_denials为空。29个SDK invocation记录=28succeeded+1claimed且handoff_attempt=0、handed_off_at=null，不能计成29次模型调用或物理未知；既有p35恢复合同允许这种零出站claim表示。0已接受Task/知识/复用，官方10检查仅1PASS/9FAIL。保留原失败，不扩大预算，不能宣称新AppWorld知识刷新已获真实验证。

N3均使用Qwen3.8 256K配置、1物理槽、max_inflight393216；实际输入远小于256K，不能冒充近窗压力。三种初始用例后追加一个按操作日期、适用范围和例外优先级判断的场景，其三份资料都是VERIFIED，不能仅靠失效目录过滤回答。

|用例|最终交付|调用|已知tokens|模型/编排实测耗时|
|---|---|---:|---:|---:|
|bilingual-exact-id|COMPLETED / PASS|5|18808|53.731秒|
|conflict-withdrawn|COMPLETED / PASS|4|14985|56.736秒|
|paged-tail-condition|FAILED / FAIL|9|63238|118.588秒|
|temporal-scope-exception|FAILED / FAIL|8|93127|163.716秒|

四个场景的实际原文读取/分页校验均通过；两个失败场景在工作区留下了正确候选答案，但没有VERIFIED产物，不能记成功。分页场景最终envelope非法JSON；日期/范围/例外场景最终为`<result_envelope>{"json":"placeholder"}</result_envelope>`，被严格拒绝。前两个只是基础控制/失效目录过滤，不夸称完整语义冲突或embedding能力。撤回fixture使用SUPERSEDED而非源码不存在的KnowledgeRecord.REJECTED，并不代表真实来源撤回转移已测。

截至上述已结束批次，两轮后续累计226次本地物理调用、5156159已知tokens下限、1个早先未知；不含正在运行的受控恢复。Flash0。

## 结果封套提示修复

实际任务包的output_contract给出`<result_envelope>{json}</result_envelope>`，存在把json当字段名的歧义；真实返回与此一致，但仅凭两例不能证明唯一因果。新增code profile v4的冻结completion_rules能力candidate-json-v1，在八类结果角色包中提供合法、扁平JSON示例，使用真实Task/Attempt ID，artifact/evidence路径来自当前Task.outputs。明确JSON文件与最终envelope不同、不要json包装/placeholder、按实际工作填summary/claims等字段，rule_check仍要求可核验claim。样例只是候选，不授予成功或verified状态。

v1–v3保留原文字，新v3历史常量完整canonical hash 53f88dc8102523114527b5e500d4ffc3d96cdcf39115ae95ae728150180329f7 与a30639e一致。新能力默认用于新code Mission；旧持久化profile/request不改。没有放宽解析器、预算、claim/evidence或产物验证；AppWorld/document/ARE输出合同不在本次范围。

定向组合90PASS/11.13秒，含真实Provider dispatch、8角色身份、JSON引号转义、缺文件拒绝、空claims拒绝以及旧profile/角色/Planner快照。旧a30639e源码下8个新契约反例JSON解析失败；此前测试夹具漏补系统赋予的result id导致10FAIL，以及正向夹具没提交claim导致1FAIL均保留，修复夹具后没有放松生产验收。Ruff/两源码mypy通过；最新完整回归和真实模型/原生后继仍待。本次工程没有独立计时，测试时间不当总工时。

## 独立受控恢复（旧a30639e源码）

同分页场景保持3.6M/40调用/1200秒，显式给2次Attempt。在第一个实际模型envelope后注入一次坏JSON，保存原响应及变更前后hash、原实际用量；必须观察envelope_invalid、链接到首Attempt的第二Attempt和最终VERIFIED答案才算恢复通过。不能把注入错误计成自然模型失败率，也不能改写此前单Attempt自然失败。

该运行器先用合成Provider验证：14次合成调用/0网络、2Attempt及真实文件/验证/收尾PASS（3.712秒）。初始注入器直接dataclasses.replace(Message)把只读metadata带入构造而抛错，产生一个合成SDK未知；已显式复制metadata字典，停止并保留旧fixture，未影响真实DGX或累计用量。真实受控恢复目前运行中。

原N3 helper父审补齐LAN地址、完整连续分页/hash核验、失败退出、未知用量停止和累计/峰值输入区分；3正10负评分控制和3实际core阴性已通过，另外15次合成调用验证三组真实tool→VERIFIED正路径（4.373秒）。这些都不是模型成绩。

|证据相对Host .local-test-evidence/2026-09-15/gap-two-wave/|SHA-256|
|---|---|
|result-contract-combined-v2.log|97cf67c42ec8d6ed3e15556f8d70c7e9a7d59f9c4c4fe4f67c230d321551f7c8|
|result-contract-old-v2.log|c58e3b67344da6e24a1b92cef9b930150964bbfcbb82f876b9a287b658dd8de8|
|core-regression-v8-source-audit.json|7b272b325660cf117b8b205b5ee8276bd9ea8bec045fab5e6ebb9235cd7dab09|
|n3-hard-protocol-v1.json|46ce5fdd1e1e98765127111b6aa14c1dc5f79ea2d1e831661f4bcfb31cb5c7c5|
|n3-hard-protocol-v2.json|5faa0e36f916a3664fa56932ed77c1f012d71d87c7536a31d7d9c794c76a87fb|
|n3-temporal-protocol-v1.json|160b1e29629c4c87519098a347377bdd9484c3af4a3eee40de08eb7dea2dd1e5|
|n3-recovery-protocol-v1.json|68753f254d4a42502042795ab2829269d8891e1dc7865acfbfd3c12742030a67|

AppWorld v4父审：Host .local-test-evidence/2026-09-14/gap-two-wave/appworld-n2-local-v4/parent-audit.json，SHA256 1bd5943e53a5fb2db0a8078ec67a9e3e3fb82234699de5d9c4d62915351a5742。
