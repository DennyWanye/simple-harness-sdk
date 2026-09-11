# P3.1 遗留修复 · 记录

## 0. handoff（每次提交时更新）

- 2026-09-12：
  - 已写好 `program.md`、本切片的 plan 和 acceptance（第 1 版），计划交独立评审中。
  - 在评审给出结论之前，只写测试草稿，不改源码。这是第 7 步的教训。
  - 测试草稿已写好：`drafts/test_p31_fixes.py`，覆盖 FX-1..FX-5，接口按 plan §2 设计，包括 `TaskBudgetFloor`、`validate_graph(task_floor=)`、`validate_change(task_floor=)`、`OrchestratorConfig.min_task_tokens`，以及 Planner 输入里的两个下限字段。评审结论出来后，移到 `tests/orchestrator/host_support/`，先确认它们是红的，再动手实现。
  - P3.2 相关代码的梳理已派给只读的 Explore 子代理，结果回来后写进 `p32/` 的计划。
  - 起点：SDK main `29daa9c`（0.9.10 / 0.9.3），Host main `e1f9e6cb`。
- 接手须知：
  - 真实模型只用 deepseek-flash；
  - 全量回归要带看门狗（scratchpad 里的 `watchdog.py`，思路是用 subprocess 起 pytest，超时就杀掉整个进程组）；
  - 同一时间只跑一个 pytest；
  - 后台全量回归期间，不要往 `tests/` 里放依赖尚未实现代码的测试。

## 1. 计划评审处置

（待填）

## 2. 实现与测试

（待填）

## 3. 回归与 wheel

（待填）

## 4. 代码评审处置

（待填）

## 5. 遗留

- 预算超支没有单独发可观测事件，放到 P3.5 处理。
- 角色模板没有写入下限说明，P3.4 考虑是否升提示词版本。
