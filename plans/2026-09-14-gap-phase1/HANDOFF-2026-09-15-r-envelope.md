# SDK 交接：AppWorld R 选择计量（Flash 信封小复验）

最后更新：2026-09-15 23:27 CST。

**完整叙事和原始测试文档目录在 Host，不在本仓。**  
Host 接手入口：`/Users/denny/projects/simple_harness/plans/taskSys2/HANDOFF-2026-09-15-r-envelope.md`  
公开镜像路径：`plans/taskSys2/HANDOFF-2026-09-15-r-envelope.md`（simple_harness）。

本仓只记录代码身份。凭据、用量明细、ignored `result.json` **不要**写进 SDK 公开仓。

## 代码

| 项 | 值 |
|---|---|
| HEAD | `f7432dc` `fix(appworld): reserve tool schemas before R self-selection` |
| 相对 v1 矩阵冻结源 | v1 96 跑在 `69d679c` 快照上；不要用本提交重放 v1 目录 |
| 文件 | `src/simple_harness/agents/context/tokenizer.py`（`estimate_provider_request`） |
|  | `src/agent_orchestrator/evaluation/appworld_arms.py`（候选全空则跳过选择轮） |
| 测试 | `tests/orchestrator/gap_phase1/test_appworld_arms.py` 25 PASS |

## 为什么改

Host Flash 身份 `a96-flash256k-v1` 的 R 24/24 报 `self-selection has no output`。根因是计量预估漏掉 tool schema，第一轮 billed input 超过 reserve，meter 关闭，选择轮从未发出。这不是「模型不会写 JSON」。

## 对应 Host 原始测试文档（只读指针）

- v1 96 收条：`plans/taskSys2/testPhase1-a96-flash256k-2026-09-15.md`
- v1 只读拆解：`plans/taskSys2/testPhase1-a96-flash256k-dissection-2026-09-15.md`
- R 信封小复验：`plans/taskSys2/testPhase1-a96-flash256k-r-envelope-v2-2026-09-15.md`
- 更早四臂/R 协议原文：`plans/taskSys2/testPhase1-results-2026-09-14.md`、`testPhase1-followup-2026-09-14.md`；本仓 `RESULTS.md` / `FOLLOWUP.md`
- 上一份 A 轮交接：`plans/taskSys2/HANDOFF-2026-09-15-a-round.md`

新身份 `a96-flash256k-r-envelope-v2` 在 Host 侧 2/2 选择闭环。不重跑 96，不补 Qwen，不开 512K，不把 2 例写进 v1 的 19/96。

## 约束

- 不用 plan-test，不打包。
- 后续真实测试目前只许 `deepseek-flash`。
- 失败与截断样本保留。
