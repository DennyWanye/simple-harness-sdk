# N1 同机共享模型容量接纳

最后更新：2026-09-15 05:40 CST。实现与定向测试已完成；新完整回归、真实Qwen多进程及源码UI联调待验。N1–N8整体仍OPEN，Flash0。

同一个规范化local profile文件的相邻capacity-v1.sqlite3为共同账本，物理endpoint作为池身份（模型别名不另开容量），上限2槽/393216在途tokens。配置冲突拒绝，重复同配置绑定幂等，冲突嵌套绑定在出站前拒绝。主对话保守预留整个262144服务窗口，编排/实验使用已绑定tokenizer的最终wire输入加输出上限。两个执行器内部2槽不再等于两份独立总容量。

SDK ProviderInvocationCoordinator在记录handoff之前等待共享容量；BaseAgent恢复tool_calls之后计数。CapacityProvider和MeteredProvider的同task/同请求嵌套共用一次grant，评测等待取消计为0物理调用，既有Task/Mission预算身份及冻结旧请求不改。排队被标为非计费slot wait，保留原有stall与取消纪律。

SQLite FIFO/短事务保存WAITING、RESERVED、HANDED_OFF、UNKNOWN和终态；随机owner epoch防陈旧回调。进程终止或PID复用只释放前置状态，出站状态转UNKNOWN继续占槽与tokens，绝不靠TTL放行。PID+OS进程创建时间核验依赖SDK新local-capacity可选extra psutil（本机7.2.2，uv.lock已锁定）；身份不可读时保持保守，旧缺创建时间条目也不猜测释放。

可信恢复根据同一SDK数据库namespace、invocation和handoff ordinal核对：存在完整terminal usage、明确CONFIRMED_NOT_STARTED，或容量标记后SDK handoff未提交的CLAIMED记录才可清理；对账proof只存opaque identity/version。复制库到另一目录不能释放原库容量。该对账接口不是模型工具。

范围：只约束采用同一profile/账本的同机客户端；直接HTTP、另一个profile文件或其他电脑不受它约束。当前不改DGX服务配置、不声称跨机器全局限流。源码UI仍由完整launcher管理唯一Vite/backend，不打包。

|定向检查|结果|实测时间|
|---|---|---|
|SDK原语/出站/Orchestrator/计量/恢复/历史结果合同|40PASS；含进程杀死、PID复用、取消、缺usage未知保持、同DB可信对账、错DB拒绝、实际runtime嵌套一次占用|2.59秒|
|Host本地profile/主对话与编排共池/绑定幂等及原LAN请求|15PASS，配置不符拒绝、原provider身份保持|以host-review-v4.log原始时间为准|
|静态检查|5个SDK源mypy通过；Ruff检查收尾中|工程未独立计时|

保留初始失败：父级把Agent.run_id(str)当RunId.value导致真实runtime用例失败，已修正；Host测试夹具空messages先于输出cap校验失败，已补真实消息。独立审查发现重复绑定自锁和PID复用陈旧队列，已新增回归与修复，未通过放宽断言隐藏。

真实协议预声明：Qwen256K；三个160K输入进程（最多2并行），两个210K输入进程（加权后串行），每请求输出上限4096、超时600秒；先查服务idle，未知用量停止后继接纳。具体任务文本、源hash和实际server usage将在运行前后存本机ignored证据，不把预声明当已执行。

## 实机发现的校时问题

最后更新：2026-09-15 06:01 CST。N1真实三进程v1为FAIL：两次物理调用321104tokens、0新增抢占，第三路前置排队被错误释放（已知0出站）。系统校时影响psutil.create_time造成身份误判，旧源3反例FAIL；改为psutil稳定process hash（>=7.2.2），新25PASS/1.85秒，完整及实机后继待验。N1–N8仍OPEN，Flash0。

稳定身份使用psutil公开Process hash；macOS/Linux由内核单调创建身份组成，Windows使用创建身份。unsupported平台/旧缺identity记录保守保持，只在PID确定不存在时按原状态恢复；新列process_identity与旧process_started分开，禁止跨算法比较。散列碰撞只会保守保留，不会误释放。真实v1原始结果不改写；第三路无usage是出站前拒绝，不是物理调用未知，修正审计另存parent-audit.json。
