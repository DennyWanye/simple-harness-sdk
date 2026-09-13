# P3.4 新子预算真实对照

最后更新：2026-09-13。用户在上一轮明确列出 B240K→480K、最终S120K→240K、总2M不变的建议后要求“请你继续”，本轮按该方案继续。新实验标识 `docs480-s240-v3`；原 `original-v2` 及其失败记录保留。

- 当前最短路径：父代理配置与运行一组固定FIRST→COMPARE；一个Terra/medium子代理只读检查配置传递、旧合同和oracle。没有三个独立工作面，不为凑并行开启额外代理。
- 原绿色基线：SDK f25a4de 完整1826PASS/9真实默认SKIP/0FAIL，626.58秒；当前41c4481仅文档后继。生产代码不变，无需重跑未变化的全量/UI矩阵。
- 两臂均沿用原每Mission 2,000,000 tokens/24 attempts，不降低真实验证层、reserve保护或严格oracle；不表示两臂合计只允许2M。A240K/4、C400K/4不变；B480K/3、最终S240K/2；同Task候选综合reserve仍120K。每臂最长1800秒。
- 原始材料、固定测试、成功条件、顺序、角色和生产FIRST默认不变。两臂只在显式搜索绑定上不同。两臂各一次，即使FIRST失败也保留并运行预先声明的COMPARE，不挑选幸运重跑。
- 预期：两臂都须实际完成C和S；COMPARE还须真实Manager改图、失败片段独立验证/复用、同Task候选综合。A的真实基线失败及分析成功都必须保留。不接受改seed/test、绕过独立验证或未知用量。
- 先验检查：原合同hash必须仍为 `b482cc0452e1855e62854025d92dc99bf9b5228072735ea956654dd7d48738e3`；新导出与原导出只允许B/S额度、B目标内额度及实验标识/hash不同；未知profile须在凭据/调用前拒绝。
- 新纯配置及原profile检查 `g-p34-budget-variant-v1`：11PASS/0.24秒（runner0.74），官方固定tokenizer参与。实际Provider对照尚未执行，不将此配置检查称为真实交付。
- Provider固定deepseek-flash / api.deepseek.com；原安全read_key注入DEEPSEEKER_APIKEY，无凭据写入或输出。原始证据留ignored目录，Git只存文字结论/hash。
- 对照收益只能依据这组真实产物及记录；一组不能证明普遍优势。如果没有收益，保留简单FIRST默认。

JOURNAL_VERDICT: IN_PROGRESS — 配置检查通过，独立复审及真实固定对照待完成。

Independent review: Schrodinger GPT-5.6 Terra/medium, actual runtime verified, read-only noP0/P1/P2, no rework. Ruff/diff check PASS.

**最后更新：2026-09-13 15:36 CST — 真实对照暴露后继缺陷，P35-A04重新打开。** 新profile docs480-s240-v3在干净3164954真实deepseek-flash/API准入双臂执行：FIRST276.552秒/563256tokens/63调用，COMPARE174.378秒/281851tokens/34调用；均budget_exhausted，runner451.71秒。所有97实际响应model=deepseek-flash，用量已知，97准入，未见物理Provider错误；不声称交付/收益。证据SDK .local-test-evidence/2026-09-13/p34-real-search-value-7c36782f54774c54983a571549c85148/pair-summary.json SHA25694626876cfc1f679fec87bf290b208f375b0506ec36c068bad82e8a17708613b。FIRST的S原hold尚有122683，Critic1已用24614、原预留40960、下一请求22999需增长6653，却因system增长路径排除Critic而回退普通账户余额0拒绝；public_output=null，无已观察schema错误。不可重试admission被包装为ContractError，又运行Critic2及新Worker，后者额外65399tokens仍失败。Astra独立诊断确认hold增长与错误分类两处缺口，待先红后绿修复。COMPARE则B完成、A原240K扣120K综合/40960首Critic后，Worker58811+下一请求23710超可用79040，仅差3481；为合法原额度拒绝。另登记audit320-docs480-s240-v4，每Mission总2M不变；A320K/B480K/S240K/C400K，旧两合同及全部oracle/material不变，12纯配置PASS0.21秒/runner0.76，新真实组未运行。P34继续OPEN，P35-A04因新缺陷重新打开，不用上一全量绿覆盖新发现。

Current v7 findings and unchanged-profile v8 correction: [Manager/selection fix](manager-selection-fragment-fix.md). v7 failed both arms and is retained; v8 not yet run.
