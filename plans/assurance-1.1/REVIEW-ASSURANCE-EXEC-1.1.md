# ASSURANCE-EXEC-1.1 再评审：可进入主体开发，附三项必修约束

日期：2026-09-22（Asia/Shanghai）  
评审对象：`simpleharness-assurance-exec-1.1-2026-09-22.zip`。  
ZIP SHA-256：`1be99510db3a71bf9735a938683f5a4f7ffb7f89710b0c08024145c1afd45ed4`。

## 1. 结论

**GO_WITH_FIXES：可以开始主体开发，实施基线为 1.1 完整包＋本评审 R01–R03。无需再让 planAgent 整包返工。**

与 1.0 不同，1.1 已就主要架构选择给出可落实的方案：复用六类正式审阅链、多 invocation 与单 round 的关系、typed checks、实际证据曝光、唯一完成 writer、事件摄取与处理分阶段、恢复隔离和当前重新授权、持久 lane 判别、完成后默认 ON，以及主体先接线后统一验收。

本轮仍发现三项局部问题，其中两类有附带 SQL 的直接反例，一类是到期唤醒与队列协议之间尚缺一个明确步骤。它们**不需要新增架构选择或用户业务授权**，可以由开发者在首批代码中按下文确定修法一并处理。不能把附件 SQL 原样复制进产品后称已符合规格，也不能把本次 GO 当作 SDK/Host/模型验收通过。

本轮用户只要求再次评估；附件中的“授权实施”文字不作为本轮开发指令。本次未改业务代码、未安装候选、未启动模型或原生 UI，未运行 SDK 或参考包的批量测试。

## 2. 实际核查结果

| 核查 | 本轮结果 | 边界 |
|---|---|---|
| 交付 `verify_delivery.py` | PASS | 文件清单/hash 一致 |
| 资产 `check_plan.py` | PASS | 新字段指针、展开 hash、Ref/SQL 列映射等在工具覆盖范围内一致 |
| 原直接 INSERT FINALIZED | REJECTED | 上轮缺口已修 |
| 原无 Review 直接 INSERT BOUND pin | REJECTED | 上轮缺口已修 |
| 原 FINALIZED DELETE 后重建 | DELETE REJECTED | 上轮缺口已修 |
| 使用另一主键撞其他 UNIQUE，替换 policy | 被接受 | R01；原不可变 policy 被换掉，FK 检查仍为空 |
| 使用另一主键撞其他 UNIQUE，替换 BOUND pin | 被接受 | R01；原 BOUND/v2 被 PREPARING/v1 取代 |
| 同 work/同 target seq、异 fingerprint 入队 | 静默忽略，rowcount=0 | R02；正文要求拒绝冲突 |
| DONE 的 VALIDITY work，仅时间过去并复用旧 target 入队 | 未重开，claim=0 | R03；需要新的持久到期目标，不代表即时使用 guard 已放过过期证书 |
| 18 份源码摘录 hash 与当前文件对比 | 13 相同 / 5 已变化 | AS-0 重核；不是要求回退旧源码 |
| 当前候选 HEAD | `102ad3dfa2db38d575ea929d39ec5ed1561a71da` | 保留全部 dirty，HEAD 相同不代表源码相同 |

七个局部探针仅使用附件 fixture 和内存 SQLite，未接触产品数据库。作者声称的 168 reference/19 mutations 本轮未重跑，也未借用为本地 SDK 证据。上述 SQL 反例说明附件本身仍有缺口，不宣称当前生产 Store 已暴露相同漏洞。

当前 `ARCHITECTURE/index.md` 顶部仍记录 V1.4 主体未完成、H4 完整动作生产链正在补齐，TaskGraph 未获 ready。因此可以开始 Assurance 的已明确工作，但要按 AS-0 与当前实现协调共同文件；不能把一次计划通过解释为 HTN 或 TaskGraph 依赖门关闭。

## 3. 上轮 F01–F15 的逐项结果

“规格关闭”仅指足以交给开发者，真实行为仍待开发后的验收；不要求新增目标函数先存在。

| 上轮项 | 本轮判断 | 依据/剩余工作 |
|---|---|---|
| F01 现有链复用与来源映射 | 规格关闭 | 明确保留 scoped/root/composition/OCC 与单一集成人；AS-0 刷新当前 hash/调用边 |
| F02 Ref 来源不完整 | 规格关闭 | 专用 commit/reservation/turn/check/disclosure 内部 kind、创建顺序及 resolver 已给出 |
| F03 evidence_ids 精确映射 | 规格关闭 | 完整 ref+review_key 标签、持久曝光和实际 Provider 输入绑定；不能用 catalogue 代替披露 |
| F04 SEMANTIC/CHECKED 三值 | 规格关闭 | NOT_APPLICABLE、typed CheckResult、OR-of-AND、实际本地 recorder 均已明确 |
| F05 六类 Review/预算/重试 | 规格关闭 | 单 package/round，多序号 invocation；原 service-intent 同库事务，执行库后导入 |
| F06 最终完成/通知 writer | 规格关闭 | judge 新 profile 转 closeout；提取唯一终态写段；通知明确为本地状态消息 |
| F07 旧备份后的当前授权 | 规格关闭 | 选择新 root 隔离＋当前精确 read reauthorization，不再假装旧备份知道后续撤权 |
| F08 失效/读集/时间 | 主体方案成立，补 R03 | 两级 epoch、读集、有界计算已清楚；到期唤醒需与 event-seq target 接齐 |
| F09 事件 cursor 与处理事务 | 主体方案成立，补 R02 | ingress→pending 与 prepare→commit/ACK 已明确；同 target 异 fingerprint 不能静默忽略 |
| F10 legacy/new lane 与默认 ON | 规格关闭 | 独立持久 classification；新 factory 完整验收后同交付默认开启 |
| F11 SQL 状态保护 | 原三例修复，补 R01 | 主键 REPLACE 防线有了，其他唯一键冲突仍可绕过 |
| F12 生成资产漂移 | 上轮缺口关闭 | 字段表改 pointer+resolved hash；本轮一致性检查 PASS，仍不是全部业务语义证明 |
| F13 Host 与原生载入 | 规格关闭 | 下划线 verb、版本化 DTO、隔离 wheel/venv/userdata、实际点击验收已明确 |
| F14 编码顺序/架构回写 | 规格关闭 | BODY_WIRED 后才集中验收；ARCHITECTURE 与 PROJECT_STATUS 同交付更新 |
| F15 继承与模型 oracle | 规格关闭 | C08 离线计费分离；12 个预登记 trial、固定输入/预算/oracle、保留失败均已明确 |

无需让 planAgent 为这 12 项重新写一遍或先交真实 SDK PASS。开发中发现新事实与既定语义不兼容时，才需要提出有具体反例的新裁定问题。

## 4. 三项必修开工约束

### R01 — 不可变表的 REPLACE 保护必须覆盖全部唯一键

优先级：P1。关联 F11。定位：`sql/assurance_additive.sql` 尾部 `*_no_replace` triggers。

当前多数保护仅检查新行的主键是否已存在。例如：

```text
已有 policy_id=p1，UNIQUE(mission_id, requirements_revision, scope_hash)=K
INSERT OR REPLACE policy_id=p2，仍使用 K，但换 policy_hash/body
```

在 `recursive_triggers=OFF` 下，旧行因其他 UNIQUE 冲突被隐式删除；主键 `p2` 不存在，BEFORE INSERT 的主键检查不触发。包内 SQLite 实测原 policy 被替换，FK 仍合法。同样可以把 `(mission,review_key,blob_hash)` 不变的 BOUND pin 换成另一个 pin_id 的 PREPARING/v1。当前 SDK Store 明确设置 foreign_keys，但所查初始化没有设置 recursive_triggers；不能以未声明的连接设置替代完整约束。

**确定修法**：保持不可变同体幂等由 Store 预读原 receipt/行后直接返回；异体冲突拒绝。不可变表的 BEFORE INSERT 冲突检查覆盖**所有可能命中现存行的 PK/UNIQUE**，而不只主键；禁止依靠 `OR REPLACE` 更新历史。可额外固定连接 recursive_triggers 并校验，但不要只补一个 pragma 而保留其他连接/恢复路径的缺口。

一次检查以下所有非冗余冲突键，不只修本轮两个反例：

| 表 | 除主键外需覆盖的唯一身份 |
|---|---|
| assurance_criterion_policies | mission_id, requirements_revision, scope_hash |
| assurance_blob_pins | mission_id, review_key, blob_hash |
| assurance_check_bindings | mission_id, execution_ref_hash, check_spec_hash, subject_hash, assertion_key |
| assurance_review_bindings | package_id；mission_id, request_command_id, round_no |
| assurance_review_invocations | dispatch_intent_id |
| assurance_review_record_bindings | mission_id, review_key, source_turn_ref_hash |

其他派生索引的合法重建仍按其既定语义；不要把 pending queue 的合法 UPSERT 一并禁止。

**验收反例**：递归 trigger ON/OFF 两种连接设置；不同主键撞每种自然唯一键；原 body/hash/状态/版本保持；同体重放无写；异体明确冲突。先用一个定点检查确认 SQL guard，再在主体完成后跑完整矩阵。

### R02 — 同一个工作目标的 fingerprint 冲突必须显式拒绝

优先级：P1。关联 F09。定位：`sql/queries.sql` 的 pending-work INSERT…ON CONFLICT…WHERE。

正文 §9 明确“相同 seq 异 fingerprint 是冲突”。当前 UPSERT 只更新 `excluded.target_epoch > current.target_epoch`，所以 target 相等但 fingerprint 不同会 rowcount=0；随后 cursor 仍可能推进，将语义冲突误当正常去重。

**确定修法**：同一短 Store 写事务内，读取并分类已有 work：

1. 不存在：按原合同新增 PENDING/v1。
2. 新 target 大于旧 target：按原 CAS 合并，更换目标，保留累计 tries/预算上限，使旧 claim 失效。
3. target 相同、fingerprint 相同：真正幂等，可以推进 cursor。
4. target 相同、fingerprint 不同：返回具名 `WORK_TARGET_CONFLICT`，拒绝该事务；不得悄悄 ACK/cursor 前进。持久冲突诊断走原错误路径，不触发模型重问。
5. target 小于旧 target：已被更新目标覆盖的旧事件可按既定幂等策略推进 cursor，不降低目标。

可在 SQL trigger/Store 校验中实现，但校验、合并/拒绝和 cursor 都必须处于原同一连接事务；不能先读另一连接再盲写。target fingerprint 应绑定确定的工作输入，不把每次计算时间混进去制造假冲突。

**验收反例**：相同 target/相同体零写；相同 target/异体整事务回滚且 cursor 不前进；较新目标使旧 ACK 失败；已覆盖的旧事件不重开工作。

### R03 — 到期重评要产生新的持久目标，不能仅重投旧 seq

优先级：P1（活性与状态正确性）。关联 F08/F09。定位：正文 §8.4、§9、附录 C.5、`event-consumer-map.json`、pending-work queries。

正文要求无新业务事件时也能到期重评；同时 pending target 必须是持久 Event.seq，DONE/REJECTED 只有更高 target 才能重新 PENDING。当前 SQL 实测：一个已 DONE 的 VALIDITY work 即使到期，使用原事件重新 enqueue 仍 0 行更新，claim 也为 0。因此“tick upsert earliest expiry wake”还需要明确如何形成新目标。

这**不证明过期证书能越过即时 guard**：计划已要求使用时检查 now。缺口在持久唤醒、投影更新与后续工作推进，不能用拒绝过期证书替代自动重评能力。

**确定修法**：沿现有原 Event 机制新增内部到期事件，例如 `AssuranceUseExpiryDue`；这不是新 scheduler。

- 原 tick/startup 扫描已持久、尚未处理的 earliest expiry；同一个 certificate/consumer/use/root/expiry 边界生成稳定幂等事件 key。
- 将到期事件持久化后，用它的真实 Event.seq 合并目标，走现有 VALIDITY/CLOSEOUT 事件分类与 pending 协议。不伪造更大的 seq，不把 validity epoch 当 target epoch。
- 到期事件只表达时间边界到达；source/权限当前性仍由原 CompleteRead＋即时 guard 核对，不自行修改证据真值。
- 补充事件类别、producer、work key、消费者和 dedup receipt。崩溃发生在“事件已写、未入队”时由 cursor 重放恢复；入队已完成则同 target 幂等。
- 只对有未来有效期、确需调度的可用证书登记下一边界；已经过期的诊断证书、已处理边界不无限生成事件。新证书采用新的确定边界身份，累计重算/预算限制不重置。

**验收反例**：没有其他业务事件，仅时间前进；同边界两次 tick；tick 后强退/重启；旧证书被新证书替代；clock rollback；WAITING/MANUAL_REQUIRED 与到期事件交错；旧 worker 不能 ACK 新目标。断言实际发生一次有效重评或具名等待，不能只断言旧证书被拒。

## 5. 开发者可直接处理，无需再通过用户询问 planAgent

- 依 R01–R03 更新实际 Schema/SQL/Store、事件 adapter 与相应测试；同步参考资产及 hash/manifest，原 1.1 输入保留，不篡改作者交付包。
- 刷新 AS-0 的 5 个变动文件及其调用边；当前有 H4 主体编码，不覆盖别人的 dirty，也不把旧摘录当冻结基线。强耦合公共文件由一个实际 integration owner 接管。
- 1.1 的新增函数/表都是实施目标，其尚不存在不是再次阻止开工的理由。普通等价函数定位和下一迁移编号由开发者决定。
- 原始参考 helper 不是完整安全实现。例如恢复披露 helper 只演示 root/time/exact-ref 规则；实际 guard 仍必须核 caller/tenant/policy/receipt 和恢复根，不直接拿一个 `current_authenticated=True` 当授权来源。
- 格式修复第二次 invocation 的曝光必须来自第二次实际输入/相应 Agent；全 review_key 的 batch 链延续，不能重复写同一个 batch 0 或自动继承另一 Agent 的私有材料。这属于 F03/F05 已定语义的实现核对项。
- 按 BODY_WIRED 先完成主体与跨层接线，期间仅做具体阻塞的最小检查；随后统一 SDK/继承/变异/状态化/原生/模型/独立代码审查。不要把本轮窄探针当成提前启动批量测试的理由。
- 验收完成后默认 ON 与 ARCHITECTURE 回写仍是交付要求；这次评审通过不触发提前启用半成品，也不改变 H1/TaskGraph 门禁。

## 6. 给后续开发任务的简短说明

> 以 ASSURANCE-EXEC-1.1 完整包和 REVIEW-ASSURANCE-EXEC-1.1.md 为实施基线。计划可开工，先完成 AS-0 当前源码核对，并把 R01 全唯一键不可变保护、R02 工作目标冲突拒绝、R03 持久到期事件唤醒纳入主体实现。保留当前 dirty，统一公共文件 owner，不另建 Review/预算/效果主链。主体跨层接线完成前不跑批量测试；之后按完整门禁验收、默认 ON 并更新 ARCHITECTURE。本说明是下一任务可用的交接，不代表本轮已经执行开发。

## 7. 本轮可复现证据

Host 本地 ignored 根：`.local-test-evidence/2026-09-22/assurance-plan-review-v11/`。

- `review_v11_probes.py`：七个局部反例检查，标准库、内存库，不导入 SDK。
- `review-v11-probes.json`：本轮输出，明确 SPEC_REVIEW_ONLY。
- `current-source-comparison.json`：18 个指定文件的附件 hash/当前 hash 对比。
- `evidence-index.json`：以上文件的 SHA-256 索引。

复现（先解压本次交接包，脚本与原 1.1 目录同层）：

```bash
"$PY" -B review_v11_probes.py
"$PY" -B simpleharness-assurance-exec-1.1-2026-09-22/tools/verify_delivery.py \
  --root simpleharness-assurance-exec-1.1-2026-09-22
"$PY" -B simpleharness-assurance-exec-1.1-2026-09-22/tools/check_plan.py \
  --root simpleharness-assurance-exec-1.1-2026-09-22
```

不要把本报告解读为整个 V1.4、Assurance SDK 或当前安装制品已完成。
