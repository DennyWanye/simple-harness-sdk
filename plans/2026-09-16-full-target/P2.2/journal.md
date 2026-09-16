# P2.2 实施记录：PANDA / HDDL 后端适配

日期：2026-09-16。基线：main HEAD 873fd4a。实际耗时约 70 分钟（含一手资料调研 7 分钟）。

## 1. 交付清单

新增文件（未改动任何既有文件）：

| 文件 | 说明 |
|---|---|
| `src/agent_orchestrator/planning/htn/__init__.py` | 仅 SPDX 头 + docstring，无导出（P1.1 子代理可覆盖） |
| `src/agent_orchestrator/planning/htn/backends/__init__.py` | 后端包说明 |
| `src/agent_orchestrator/planning/htn/backends/panda.py` | 适配主体，约 1080 行 |
| `tests/orchestrator/full_target/test_panda_backend.py` | 43 条测试 |
| `tests/orchestrator/full_target/fixtures/panda/*` | 5 份 HDDL 样本、2 份计划样本、11 个 stub 可执行脚本、1 个不可执行文件 |
| `plans/2026-09-16-full-target/P2.2/journal.md` | 本文件 |

`panda.py` 自带全部结果类型，不 import P1.1 的 contracts，也不 import store/commit/compiler。无新增第三方依赖。

实现要点：

- `PandaToolchain`：路径来自显式参数（`from_paths`）或环境变量 `SH_PANDA_PARSER` / `SH_PANDA_GROUNDER` / `SH_PANDA_ENGINE`（`from_env`）。含路径分隔符的值必须直接指向可执行文件；裸程序名走 `shutil.which` 查 PATH。除此之外不探测任何目录。
- `availability()`：返回 `available` / `missing` / `versions` / `digests` / `paths`。版本字符串是 best-effort 的 `--version` 探测（上游用 gengetopt，是否提供 `--version` 未文档化），**身份以二进制 sha256 为准**，对应 §21.1「固定求解器版本」。
- `SupportedFragment` / `check_fragment`：声明支持的子集是「总序与部分序方法 + typed objects + STRIPS 前提/效果」。检测 = 去注释后的词法扫描，两条通道：(a) `:requirements` 里不在白名单的 flag；(b) 固定的语法探针表（`(:functions`、`(increase`、`(:durative-action`、`:duration`、`(at start`、`(when`、`(forall`、`(exists`、`(:derived`、`(preference`、`(probabilistic`、`(total-cost` 等）。
- `verify_plan()` / `solve()`：状态集合按计划书要求；`UNSOLVABLE_PROVEN` 只在引擎自己打印证明行时返回。
- `DecompositionWitness`：`primitives` 按执行序保留**每一个 occurrence**，`occurrences_of()` 返回同一 grounded 动作的多个出现，`parent_of()` / `occurrence_parents()` 给出 occurrence→方法应用的映射。解析器不合并、不修复、不排序（附件 TG §12、正文 §8.3 末段）。
- 子进程纪律：显式 argv、`shell=False`、`cwd` 为 `tempfile.TemporaryDirectory`、输入写临时文件、逐步超时、最小环境（只保留 `PATH` / `LC_ALL=C` / `HOME`）。每次调用记 `ProcessTrace`：argv、退出码、是否超时、耗时、stdout/stderr 的 sha256 与前 4096 字节摘录。异常不吞：`OSError` / `SubprocessError` 只在版本探测里被吸收（那是 best-effort），主路径的非零退出码进入 `TOOL_ERROR` 的 `detail`。

## 2. PANDA 用法来源

由独立子代理只读官方仓库/PDF 获得（未安装、未克隆）。**README 只记载了翻译与流水线；验证标记、引擎状态行、退出码语义全部来自上游 C++ 源码，属于「源码推出、官方未文档化」**，因此适配器一律「先认 stdout 标记、退出码只做兜底」。

| 事实 | 来源 URL | 文档化状态 |
|---|---|---|
| `pandaPIparser domain.hddl problem.hddl out.htn` | https://github.com/panda-planner-dev/pandaPIparser/blob/master/README.md | README 明文 |
| 验证三参数顺序 domain、problem、plan | https://github.com/panda-planner-dev/pandaPIparser/blob/master/src/options.ggo | `sectiondesc` 明文（README 无） |
| `Plan verification result: true/false`，退出码 0/1 | https://github.com/panda-planner-dev/pandaPIparser/blob/master/src/main.cpp （L284–295） | 仅源码 |
| 未加 `-C` 时 verdict 被 ANSI 色码包裹 | https://github.com/panda-planner-dev/pandaPIparser/blob/master/src/util.cpp | 仅源码 |
| 单横线 `-verify` 会被当成 `-v erify` 而失败 | 同 `options.ggo` + getopt_long 语义 | 推断，未实测 |
| `pandaPIgrounder input.htn output.sas`，不报告不可解 | https://github.com/panda-planner-dev/pandaPIgrounder/blob/master/README.md 、 .../src/main.cpp | README + 源码负面证据 |
| 三步流水线与「引擎计划对原模型不合法，需 `-c` 回译」 | https://github.com/panda-planner-dev/pandaPIengine/blob/master/README.md | README 明文 |
| `- Status: Solved` / `- Status: Timeout` / `- Status: Proven unsolvable` 三者互斥 | https://github.com/panda-planner-dev/pandaPIengine/blob/master/src/search/PriorityQueueSearch.h | 仅源码 |
| 引擎退出码恒 0，结果不体现在退出码 | https://github.com/panda-planner-dev/pandaPIengine/blob/master/src/SearchEngine.cpp | 仅源码 |
| SAT/BDD 分支不打印 `- Status:` 行 | https://github.com/panda-planner-dev/pandaPIengine/blob/master/src/sat/sat_planner.cpp | 仅源码 |
| `-t/--timelimit` 秒，默认 1800；无节点限额选项 | https://github.com/panda-planner-dev/pandaPIengine/blob/master/src/options.ggo | 源码规格 |
| IPC 计划文件语法（`==>` / 动作行 / `root` / 分解行 / 可选 `<==`） | http://ipc2020.hierarchical-task.net/data/format.pdf （由 https://ipc2020.hierarchical-task.net/benchmarks/output-format 链接） | 官方 PDF 明文 |
| 真实样例计划（无 `<==`、分解行不按 id 排序） | https://github.com/panda-planner-dev/pandaPIparser/blob/master/tests/plan-for-transport-pfile01.txt | 仓库测试文件 |

以上全部写进了 `panda.py` 的模块 docstring，并逐条标注了「README / 源码 / 推断」。

## 3. 是否构建了 PANDA

**没有，走 stub 路线。** 本机 `cmake` 与 `gengetopt` 均不存在（`which` 返回 not found），而 pandaPIparser / pandaPIgrounder / pandaPIengine 三者都以 CMake 为构建入口、CLI 由 gengetopt 从 `options.ggo` 生成，缺这两个前置无法构建；按分派要求不做 brew/sudo 安装。因此本机不具备真实二进制，`test_real_pandapiparser_verifies_the_modelled_sample` 处于 SKIP。

按 §7.4 的门槛地位，这条的正确记法是 **UNSUPPORTED，不是 PASS**：适配器在无二进制时返回显式 `SOLVER_UNAVAILABLE`，不抛异常、不回退到内部搜索、不产生 `VERIFIED`。只要 CI 或他机配好 `SH_PANDA_PARSER`，该用例自动转为真实校验，无需改代码。

## 4. 关键设计裁决

1. **`solve()` 必须回译才敢报 SOLVED。** grounder 会改写模型，引擎输出的计划对原始 HDDL **不合法**（引擎 README 原话）。所以流水线是四步：parser 翻译 → grounder → engine → `parser -c` 回译；见证一律从回译后的计划构建，回译失败即 `TOOL_ERROR`。若只解析引擎原始输出，就是在拿 grounded 模型的轨迹冒充原任务网络的计划，正是 §8.3 末段禁止的那类偷换。
2. **`search_limit` 的语义被如实收窄。** pandaPIengine 只有秒级预算（`-t/--timelimit`），没有节点限额。适配器不发明一个假的节点限额，而是把 `search_limit` 记为秒数转成 `--timelimit=N`，并在 docstring 里写明。由此两种「用尽」被分开：引擎自己报 `- Status: Timeout` → `SEARCH_LIMIT_REACHED`；适配器按 `timeout_s` 杀进程 → `TIMEOUT`。两者都永远不会变成 `UNSOLVABLE_PROVEN`。
3. **分类优先级固定为 Solved → 限额 → 不可解 → 退出码。** 限额标记排在不可解标记之前（源码里两者互斥，但对抗性输出必须有确定行为），有 `test_solve_never_turns_an_exhausted_budget_into_a_proof` 兜底。
4. **不认识的输出一律 `TOOL_ERROR`。** 例如 SAT 后端只打印计划块、没有 `- Status:` 行，适配器拒绝据此宣布成功；宁可报工具错误也不猜。
5. **`-C` 与 ANSI 剥离双保险。** `-C` 是源码推出的、未文档化的行为，所以除了传 `-C`，分类前还会用正则剥掉 ANSI 序列；摘录与 hash 仍保存原始字节。

## 5. 边界与偏差（明确记下，不藏）

- **片段检测是词法的，不是解析。** 因此：(a) 谓词/任务/方法若真叫 `when`、`forall`、`exists`，会被误报为不支持——这是保守拒绝，不是放行；(b) 既不声明 flag、又不使用上述 token 的特性检测不到；(c) 比较运算符 `(<`、`(>=` 故意**不**作为数值特性的探针，因为 `(< t1 t2)` 同时是 HDDL 的子任务序约束语法，有 `test_subtask_ordering_operator_is_not_mistaken_for_a_numeric_comparison` 锁住。外部 parser 始终是权威，本检查只是准入门。
- **计划解析接受 `;` 注释行与空行**，这是对已发布语法的超集（为了能给 fixture 加 SPDX 头），docstring 里写明了。
- **HDDL fixture 未经真实 parser 校验**（本机无二进制），它们只用于驱动适配器与片段检测，不作为「已建模样本通过 PANDA」的证据。
- **`--version` 探测可能返回 usage 文本**（上游是否提供该选项未文档化）。版本字符串因此只是辅助，身份以 sha256 为准。
- **超时后的孙进程不做进程组清理**：`subprocess.run` 只杀直接子进程。当前三个工具都不派生子进程，暂不引入 `start_new_session` 复杂度，若将来接入包装脚本需要补。
- **仅支持默认 progression 搜索**，`-s/-b/-2` 等后端不在本片范围。
- `Availability` / 结果类型里的 `Mapping` 字段使得 frozen dataclass 不可 hash（调用 `hash()` 才会报错），当前无消费者需要 hash，留作已知项。

## 6. 测试与门槛结果

命令：`uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target/test_panda_backend.py -q`

结果：**42 passed, 1 skipped**（skip 即真实 pandaPIparser 用例）。

覆盖分组：二进制缺席与路径解析 5 条；工具链身份（sha256/版本/PATH 语义）2 条；验证路径 9 条（true / ANSI 着色 true / false+退出码 1 / 超时 / 崩溃退出码 3 / 无法识别输出 / 参数顺序 / 临时目录已清理 / 不支持特性短路）；求解路径 12 条（SOLVED+回译+见证、UNSOLVABLE_PROVEN、SEARCH_LIMIT_REACHED、限额与不可解同时出现仍判限额、`--timelimit` 透传、非法 limit、引擎超时、grounder 崩溃、SAT 式输出拒绝分类、回译失败、不支持特性短路、输出超界时摘录 4096 而 hash 覆盖全量）；片段检测 6 条；见证解析 7 条（重复 occurrence 保两条、occurrence→方法映射、roots/`__top`/plan hash、无 `<==` 的 IPC 样例、畸形计划、重复 id、空 body）。

**变异测试（临时改源码验证测试确实咬住，已还原）**：5 个变异全部被捕获——
1. 不可解判断排到限额之前 → 2 条失败；
2. 二进制缺席时仍走验证 → 1 条失败；
3. 见证按签名去重 → 2 条失败；
4. 验证参数顺序改成 plan 在前 → 2 条失败；
5. 跳过回译失败检查 → 1 条失败。
还原后 `diff` 与备份一致。

lint / 类型：

- `ruff check src/agent_orchestrator/planning/htn tests/orchestrator/full_target` → All checks passed
- `ruff format --check` 同目录 → 17 files already formatted
- `uv run --frozen --group dev mypy src/agent_orchestrator` → `planning/htn` 下 **0 error**。仓库残留错误与本片无关：进入时基线 17 个（缺可选依赖的 import-not-found + `agentdojo_runner.py` 一处 misc），期间 P1.1 子代理新增的 `contracts/evidence_state.py` 两条 `__setattr__` 报错不属于本片，未触碰。

`git status` 确认：本片只新增文件，既有文件零改动；未提交、未推送、未跑全量回归。

## 7. 与相邻片的接口

- 本片**不做 HDDL 导出**，只吃文本。P2.1 的 compiler 产出 domain/problem 文本后直接调 `verify_plan` / `solve` 即可。
- 复用/occurrence 的约束在本片体现为「见证保留全部 occurrence」；「导出时必须用显式可复用方法表达复用」是 P2.1 compiler 的义务，本片通过保留 occurrence 映射提供可核对的一侧。
- 结果类型自带，将来若 P1.1 的 `contracts/htn.py` 要统一，替换点集中在文件末尾的 `_verification` / `_solve` 两个构造函数。

## 8. 审阅修复（2026-09-16，独立审阅「需修后合并」）

按阻断 → 高 → 中 → 低顺序修复，逐条对应如下。

### F1（阻断）分类与计划抽取只读 4096 字节摘录

原实现把 `ProcessTrace.stdout_excerpt` 同时当证据和分类输入，于是 400 条动作的计划被截成 200 条、`roots` 丢失、末条动作名被切断，而真实引擎的启发式统计常把 `- Status:` 行挤出前 4 KiB，导致 Solved 被误判 TOOL_ERROR。

修法：`_run` 改为返回内部的 `_RunOutcome{trace, stdout: bytes, stderr: bytes}`。**完整流只在模块内部流动**，`_haystack` / `_verify_verdict` / `_engine_outcome` / `_extract_plan` 全部改吃完整 stdout+stderr；`ProcessTrace` 的摘录退回纯诊断用途（docstring 已写明），hash 仍覆盖全量。

同时按审阅要求收紧 root 语义：`parse_plan(plan_text, *, require_root: bool = True)`。默认必须有 root 行（已发布语法本就要求），solve 路径显式传 `require_root=True`；只有 `verify_plan` 这个「文本不是我们产出」的显式模式传 `require_root=False`。

新增红测试 `test_solve_classifies_and_extracts_from_the_whole_stream_not_the_excerpt`（stub 先打 200 行噪声再打状态行与 400 条动作，断言摘录恰为 4096 且不含状态行、见证有 400 条 primitive、`roots == ("400",)`、末条名为 `act399` 未被截断）。

### F2（高）`_resolve_binary` 不绝对化

相对路径通过校验后，`_run` 切到临时 cwd 就会抛 `FileNotFoundError`，绕过类型化返回。改为一律 `Path(text).expanduser().resolve()`（PATH 命中的结果同样 resolve），可执行校验放在绝对化之后，仍在 `availability()` 阶段完成。新增 `test_relative_binary_paths_are_absolutised_before_the_cwd_changes`（`monkeypatch.chdir` 到 fixtures 后用 `./stub_parser_true.py`）。

### 补测试：审阅在隔离副本存活的 5 个变异

| 变异 | 新测试 | 现在结果 |
|---|---|---|
| (a) 删掉 root 合法性校验 | `test_parse_plan_refuses_a_root_that_names_no_task_in_the_plan`（新 fixture `plan_bad_root.plan`，所有子任务 id 都存在，只有 root 校验能触发）+ `test_parse_plan_requires_a_root_line_by_default`（新 fixture `plan_no_root.plan`）+ `test_verification_is_the_only_mode_that_accepts_a_rootless_plan` | 咬住 |
| (b) `_stream_hashes` 取首次而非末次 | `test_solve_reports_the_last_stage_streams`（`stub_parser_true.py` 的 `-c` 模式现在打印独有行，四个阶段 stdout 互不相同，断言 `result.stdout_sha256` 恰等于回译阶段输出的 sha） | 咬住 |
| (c) verify 里 true 优先于 false | `test_verify_plan_lets_the_rejection_win_when_both_verdicts_are_printed`（新 stub `stub_parser_true_and_false.py` 同时打两行） | 咬住 |
| (d) 各阶段各自独占 `timeout_s` | `test_every_stage_shares_one_budget_instead_of_getting_its_own`（新 stub 睡 2.0s 的 parser + 睡 3.5s 的 grounder，`timeout_s=4.5`：grounder 单独给 4.5s 能过，接在 parser 后面只剩约 2.5s 必超时） | 咬住 |
| (e) 无空格形式的探针 | `test_probes_catch_the_no_space_spelling`，参数化覆盖 `(when(` / `(forall(` / `(exists(` | 咬住 |

真实二进制用例改为只接受 `VERIFIED`（原先同时接受 REJECTED，等于把 fixture 建模错误也算过）。

### F3（中）`_run` 只捕 `TimeoutExpired`

`Popen` 的 `OSError`（ENOEXEC / ETXTBSY / 权限）会逃出适配器。改为捕获并转成带 `launch_error` 的 `ProcessTrace`，`verify_plan` 与 `solve` 的每个阶段都先查 `launch_error` 再查超时，返回 `TOOL_ERROR`。新增 fixture `stub_bad_format.bin`（可执行但非可执行格式）与 `test_a_file_that_cannot_be_executed_is_a_tool_error_not_an_exception`。

### F5（中）`solve` docstring 的 "node limit"

已改为秒，并明写 pandaPIengine 没有节点限额、适配器不发明一个。

### F4（中）进程组

`subprocess.run` 换成 `Popen(..., start_new_session=True)`，超时走 `_kill_process_group()`：先 `os.killpg(os.getpgid(pid), SIGKILL)`，失败再退回 `process.kill()`，容忍 `ProcessLookupError`/`PermissionError`/`OSError`，与 `runtime/sandbox.py` 的约定一致。同时给每次调用加 `TMPDIR` 指向该次的临时工作目录（工具的临时文件不外泄）。

新增 `test_a_timeout_reaps_the_whole_process_group`：stub 先派生一个 3 秒后落地标记文件的孙进程再挂起，适配器 1 秒超时；测试等 4 秒断言标记文件不存在。只杀直接子进程的变异会让该用例失败（已验证）。

注意一个实现坑：stub 的 marker 路径**不能**用 `tempfile.gettempdir()`——适配器现在给子进程自己的 `TMPDIR`，与测试进程的 tempdir 不同，会让断言恒真。两边改用同一个字面量路径，并在 `finally` 清理。

`_probe_version` 也对齐主路径：最小 env、临时 cwd、`stdin=DEVNULL`、超时、`start_new_session`。

### F6（低）`search_limit <= 0`

不再在 `_engine_argv` 抛 `ValueError`，改为 `solve()` 入口的前置校验返回 `TOOL_ERROR`（`traces == ()`）。对应测试由 `pytest.raises` 改成断言状态与 detail。

### F7（低）文档

上游源码引用的行号无法钉 commit sha，按要求全部去掉行号，改成「读取于 master 分支、2026-09-16」的描述，并注明「用真实二进制的验收轮次应重新钉死 commit」。另在 `parse_plan` 的 docstring 说明 `PandaPlanFormatError` 为何仍是普通 `ValueError` 而不并入 `ContractError` 家族：它报告的是第三方进程吐出的畸形字节，不是内部契约被违反，且本模块刻意不 import contracts，以便在 HTN 契约落地前可用。

### 修复后结果

- `uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target/test_panda_backend.py -q` → **55 passed, 1 skipped**（原 42 passed，新增 13 条）
- `ruff check` / `ruff format --check`（`planning/htn` + `tests/orchestrator/full_target`）→ 全过，28 files formatted
- `uv run --frozen --group dev mypy src/agent_orchestrator` → `planning/htn` 下 **0 error**；全仓 17 errors / 4 files，全部是缺可选依赖的 import-not-found 与 `agentdojo_runner.py` 一处既有 misc，与本片无关
- **变异复核 11 项全部被捕获**：F1 分类回退到摘录、F1b 抽取回退到摘录、F2 去掉 `resolve()`、F3 不捕 `OSError`、F4 只杀直接子进程、F6 去掉 `search_limit` 前置校验，以及审阅点名的 (a)~(e) 五项。每次变异后均 `diff` 校验源码已还原。
- 新增 fixture：`plan_bad_root.plan`、`plan_no_root.plan`、`stub_bad_format.bin`、`stub_parser_true_and_false.py`、`stub_sleepy_parser.py`、`stub_sleepy_grounder.py`、`stub_engine_big_plan.py`、`stub_spawns_grandchild.py`；`stub_parser_true.py` 的 `-c` 模式增加一行独有输出。
- `git status` 确认仍只新增文件，本片零改动既有文件（工作区里 `contracts/__init__.py` 的改动属并行的 P1.1 子代理）；未提交、未推送、未跑全量回归。
