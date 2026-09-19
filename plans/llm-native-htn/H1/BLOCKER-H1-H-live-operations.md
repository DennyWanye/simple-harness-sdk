# H1-H 阻塞：两种已声明操作缺少 HTN 执行路径

日期：2026-09-19。SDK：`0d89307d4d9034291eff49d5cef5f0ffb617a5af`。
状态：**BLOCKED，仅阻塞依赖这两种操作的主链接线及 H1 整体验收**。H1-F 独立核验、H1-S 协议开关可以继续。

## 原计划与事实冲突

权威 V2 §7、§12、§26、§27、§44–45 要求 H1 真执行 `BIND_EXISTING_GOAL` 与 `REPAIR/PROPOSE_SUCCESSOR`，并称其可映射到现有 `bind_shared_goal` / `propose_successor`，复用现有 ground/compiler/commit。H1-G 任务书同样要求适配为现有 PlanProposal，不换编译器。

源码实际只有两种 operation 的类型和解析：

- `contracts/htn.py:2512`：`BindSharedGoalOperation`。
- `contracts/htn.py:2529`：`ProposeSuccessorOperation`。
- `contracts/htn.py:2571`、`:2585`：解析分支。
- `orchestrator/hierarchical_dispatch.py:3760` 的 `compile_proposal` 在 `:3787–3799` 强制恰好一个 refine、最多一个 retire；独立 bind/successor 无条件拒绝。

类型存在、旧文本能解析、适配前后 canonical JSON 相等，都不能证明这两种操作可执行。不能据此关闭 H1。

## 独立复现与主代理复核

Luna 独立检查后，主代理阅读脚本及实际源码，并亲自重跑。最小 spike 使用既有 `test_root_review_repair_library._seeded` 装配真实 HierarchicalDispatch/Store/PlanningWorld，调用实际 `compile_proposal`；并非真实模型或端到端验收。

```bash
PYTHONPATH=src uv run --offline python \
  .local-test-evidence/2026-09-19/h1-live-seam/spike.py
```

两个 operation 均返回 `ContractError`，共同错误正文：

```text
carries 1 operation(s) of which 0 refine and 0 retire; this slice assembles exactly one refinement per round
```

该拒绝发生在具体对象、参数与状态校验之前，因此证明的是**操作分派入口缺失**；不以 spike 中的占位对象证明任何业务语义正确。

本地证据：`.local-test-evidence/2026-09-19/h1-live-seam/{spike.py,stdout.txt}`，不提交原始证据。

- `spike.py` SHA-256：`b9797a73397baf470afebe3c7f3b4dd9452368a2cb75912ecc453108b915183e`。
- `stdout.txt` SHA-256：`9a8bc95522f807b8d298f344a4ead8547d9c2c6e7839685de18d2cb3fee7a65d`。

## 可复用边界与缺失规格

1. **绑定已有目标**：`planning/htn/compiler.py::compile_refinement_bundle` 和 `HierarchicalDispatch` 已支持 refine 内部的 sharing/reuse。但不能直接处理已有 consumer instance 的独立槽位重新绑定。须明确原槽位工作、旧 demand、DATA/ORDER、结果有效性和在途工作如何处理，并确定生成何种 ProposedPlanDelta。
2. **提出后继**：`obligation_commits.py::_inherit_obligation_on_replacement` 是旧 DAG `GraphChange` replacement 提交后的责任继承钩子，调用点在 `commit_service.py:1643–1655`。它不是 HTN successor 编译器，不能直接替代 occurrence、method instance、plan revision、DATA/ORDER 与 acceptance 的一致性更新。
3. 独立核查最初称 successor 可复用现有 replacement commit。主代理要求复核后已纠正为上面的 DAG/HTN 边界，未按初始建议实施。

## 建议裁定

保留 H1 全部目标及旧协议不变，先补充这两条操作的 HTN 执行规格，再对照现有 delta/commit 能力做小型 spike 与独立挑战，确定最小实现范围后继续 H1-H。复用原验证与提交体系，不新建第二套编译器或状态机。

至少须在补充规格中钉死：适用对象状态、槽位替换语义、责任与预算继承、运行中工作收敛、DATA/ORDER 与 acceptance 影响、幂等身份和恢复断点、旧协议兼容边界。

这不是给两个解析类型各加一行路由的问题。V2 §0 / §61 及 handoff §5.9 要求遇到未覆盖的源码/规格冲突先记录 blocker，不自行发明状态语义。因此当前未修改两条执行路径，也未降低 H1 启用矩阵或验收要求。

已向用户提交以上范围内修订建议；答复前仅继续独立的准入核验和协议开关工作。
