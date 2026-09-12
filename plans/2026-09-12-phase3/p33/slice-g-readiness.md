# P3.3 G 就绪记录

最后更新：2026-09-12。正式启动20:25 CST，前置只读准备与F重叠，不重复计时。
状态：READY_FOR_IMPLEMENTATION；非验收完成。原plan v3与46项AC不变。
主线：原生创建含版本资料的文档Mission，形成可展开原版本依据的系统结论；只调用DeepSeeker deepseek-flash。

## 交付与任务

沿用G整体交付，内部顺序为G1 SDK原子入口/来源读取/新判定、G2 Host接线与UI、G3制品与原生验收。
三个风险子系统：SDK判定、Host产品接线、冻结制品。新增兑现P33-20/45/46及A08最终判定缺口；原46项全部保留在最终审计。
执行模式：主代理处理共用版本分界与Mission判定；Kepler处理SDK原子创建和来源读取；Ohm处理Host后端；前端独立代理处理Missions界面。
沿用现有共享main及严格互斥文件所有权，不创建新worktree；代理不得提交或跑pytest，主代理串行跑测试。
各任务先写测试与预期，累计diff独立交叉审查，便宜检查/核心价值smoke先于全量、制品封存与完整原生矩阵。

## 先行契约与oracle

1. DOC_PROFILE_V3保持canonical原值；新默认DOC_PROFILE v4立即ON。supports_document_assessments仅v3/v4共享D能力；requires_mission_source_binding仅v4使用G规则。schema/contract不变，旧v3 context、intent、费用和code行为不改。独立Ohm20:14已挑战同意此边界，撤回旧intent迁移方案。
2. facade create_with_sources严格外形{mission,sources}。来源项path/content/kind。规范Mission spec和按path排序的来源hash/kind形成批次身份。Mission/预算/domain/sources/事件/批次receipt同事务，任一失败数据库全回滚，CAS可留无引用blob。同key同内容重排幂等，不同内容冲突；普通create同key拒绝补登记；完成后Host才wake。覆盖第二来源失败、事件注入失败、并发幂等、NFC/case别名、来源根与发布目标交叉。
3. citation_read(mission_id, result_id, receipt_id, citation_index, offset=0, limit=65536)：index为指定receipt.evidence_refs中0基索引。只由真实accepted assessment绑定推导路径/版本/块；caller不能传路径/范围。read_view中验证租户、绑定、实际记录和CAS。返回该绑定完整block的字符分页(text,offset,next_offset,total_chars)，保持原换行/Unicode，固定版本与block身份不随页改变；页限制1..65536。source_state独立含revoked/superseded_by/active_version_hash/revision，历史verdict不改。trust与scope marker明确。无对应/越权/错误receipt统一not_found；CAS损坏integrity_error，无active fallback。失败结果只显示真实diagnostic，不伪造assessment。覆盖超过预览及256KB原文、多页、CRLF/CR、外租户、旧版变更、无历史写入。
4. DOC4 Mission判定树从实际accepted receipts和原used_knowledge递归血缘取得原版本并集，从CAS挂载；同path多版本不同物理路径，catalog明确logical/version/mounted与hash，空catalog也冻结。校验seed/artifact冲突。intent创建前先查existing；新intent冻结actual view_id、树指纹、catalog和untrusted roots，恢复在bridge.recover唤醒前按原config复验/重建缺失树，存在但错误树ERROR不覆盖。坏v4缺metadata/hash/trust不补造intent，不额外收费。v4 root arbitration必须独立MissionCritic；普通文档内容确定性判断不新增Critic。v3旧路径保持。
5. DOC4 ACTIVE最终coverage按真实assessment refs及原used_knowledge检查currentness。stale/unknown排除受影响Claim贡献并记录excluded_claim_ids/source_provenance_issues；有独立有效替代可满足。没有有效依据则相关criterion FAIL；CAS/存储ERROR为VERIFIER_UNAVAILABLE；stale优先于多数INCONCLUSIVE。缓存、重启和计算到提交间撤销都复查，不产生CommitRejected循环。P33-25的Task rule/accept反向约束不改；terminal历史不重判。覆盖真实revoke、继承-only、无关source、替代依据、多版本、三处race、CAS错误、终态不变、v3费用与context不变。
6. Host沿现有control IPC→handlers→Service→SDK facade，fixed principal，source变更沿现有审批，expected version不丢。模型侧没有Mission/source ToolSpec，不引origin/is_ui自报鉴权或新权限系统。批次成功后wake，source注册/替代/撤销和citation_read有显式命令。doc投影完整formal claims/assessment/criteria/source各版本，无20行截断。code投影保持。覆盖实际dispatcher禁止模型source通道、原子失败不wake、回放/冷恢复/审批。
7. UI领域默认code；doc允许导入或粘贴并编辑来源path，创建原子批次；系统结论由正式Claim/assessment渲染，Worker自由文案仅“分析 / 非结论”。显示徽标、信任、范围、limitations、真实review、来源生命周期。引用展开只走绑定read，转义文字，分页无截断，缺失/错误/加载可见。请求绑定request_id+Mission+引用身份，迟到响应不能覆盖新选择。source_change/action/review/arbitration分支显式，未知无按钮。仲裁选项与实际keep/contextual/unresolved一致。INSUFFICIENT来自final_report.result，不改Task enum。覆盖21+claims、长引用、跨Mission/同Mission迟到、冷重连、注入原文、code旧行为。
8. G新源码构建新的后继candidate（拟0.11.1），不复用F0.11.0身份。Host版本/hash/manifest/lock一致；打包spec使用candidate helpers，serviceSDK不升级，平台资源路径实际验证。先原生价值spike（真实仓库文档含表格、冒号、否定条件），再完整新UI路径矩阵。最终installed app去掉BACKEND_DIR/PYTHON/PYTHONPATH覆盖，证明bundled backend与wheel身份；Tauri自己管理backend/Vite。凭据仅主Host ignored .env DEEPSEEKER_APIKEY进入进程env，无key复制/日志/Keychain探测。

## 完成门与当前未做

先逐段定向测试、独立review与价值smoke，然后G完整编排回归、安装包/Host验证、原生真实flash与46AC审计、架构同步、commit/push。F已有红集保留，不能声称整仓全绿。
21:09更新：业务实现和定向测试已有结果，见journal §2.7；完整回归、真实调用、制品/原生仍NOT_RUN。耗时由journal逐段记录，G估时16–32小时为粗估。
