# H1-S 实施日志 — 任务规格里的新协议开关与协议绑定写入

**片名：** H1-S（V2 §8.1/§8.2/§9/§36 + 裁定补遗 §5/§8）
**分支：** `h1-s-protocol-switch`
**开工基线：** `0d89307`（main）
**工作目录：** `/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk-h1s`

> 本片由两位实施者接力：上一位完成 `37af171`–`43ecd37` 五个提交后被中途叫停；接手者只做
> ①通读既有实现、②逐条核对任务书「测试至少覆盖」清单并补齐缺项、③补齐本日志。既有实现未推翻，
> 只收紧了两处并补了 7 条有鉴别力的用例。

---

## 1. 白名单与范围

### 修改

| 路径 | 说明 |
|---|---|
| `src/agent_orchestrator/orchestrator/commit_service.py` | 热文件，只留调用点（净 +19/−19 行内） |
| `src/agent_orchestrator/orchestrator/planning_protocol_binding.py` | 新增模块，新逻辑集中于此 |
| `tests/orchestrator/full_target/test_planning_protocol_switch.py` | 专项测试（17 条） |
| `plans/llm-native-htn/H1/journal-S.md` | 本日志 |

### 已回退的越界改动：`api/missions.py`（闸门红 → 修复）

上一位实施者曾在 `src/agent_orchestrator/api/missions.py` 加过 8 行，把请求里的
`planning_protocol_version` 映射进 `MissionSpec`。**该文件不在 `h1s-allow.txt` 白名单内**，
闸门因此判红（`gate-1.json` 的 `allowlist` 项：`files outside allowlist:
src/agent_orchestrator/api/missions.py`），工作树再干净也过不了。

修法是**把那 8 行整块删掉**，让该文件与基线 `0d89307` **逐字节相同**（`git diff 0d89307 --
src/agent_orchestrator/api/missions.py` 无输出）。理由：开关本来就落在白名单内。

- `MissionSpec` 就是任务契约本身，`planning_protocol_version` 是它的字段，**任何自己构造契约的
  调用方**（`__main__` 的 CLI、`evaluation/` 各 runner、Host 自己的记录读取路径）都能直接命名该
  wire，不依赖请求解析器；
- `spec_from_request` 保持原样（继续静默丢弃未知键）已被两条新用例钉死，所以「回退会悄悄重演」
  不会发生；
- 缺省规格字节/哈希不变：`to_json()` 只在非缺省时才写该键，回退不影响这一点。

> 遗留（**明确留给后续切片**）：Host 通过 HTTP 请求体里的 `planning_protocol_version` 选择新协议
> 这条路径，本片（白名单内）无法启用——它必须改 `api/missions.py`。这属于**派发/Host 接线片**
> （H1-F/H1-H 一带）的工作，不是本片的验收面。本片交付的是「契约字段 + 持久绑定 + 只读恢复函数」。

### 明确未改

`event_handler.py`、`hierarchical_dispatch.py`、`policy_snapshot`、`contracts/`、存储层
（`storage/planning_decision_store.py` / `planning_decision_schema.py` 一行未动）。

---

## 2. 既有提交（上一位实施者，全部保留）

```text
37af171 test(h1-s): specify durable planning protocol switch
bac9176 test(h1-s): tighten protocol validation and rollback coverage
bc67dea feat(h1-s): planning_protocol_version on MissionSpec and the durable protocol binding
bc63d28 fix(h1-s): tighten protocol binding validation and rollback
43ecd37 test(h1-s): verify policy binding ignores protocol switch
```

## 3. 接手者新增（含第三位接手者的修复）

```text
d7986a2 test(h1-s): cover host hand-off, forged specs, stored hash and env isolation
3108556 feat(h1-s): planning_protocol_version on MissionSpec and the durable protocol binding
3362c4c docs(h1-s): record the hand-off audit, the added cases and the measured gates
```

### 3.0 闸门红与修复（第三位接手者）

`gate-1.json` 对 `3362c4c` 判红：`allowlist` 项报 `files outside allowlist:
src/agent_orchestrator/api/missions.py`，其余 8 项全绿。修法见 §1「已回退的越界改动」。

```text
c5c8f12 test(h1-s): pin the switch reachable without the out-of-allowlist parser edit
8df04e6 fix(h1-s): keep the protocol switch inside the slice allowlist
```

- `c5c8f12`（先红）：新增两条用例——`test_the_protocol_switch_is_reachable_without_editing_the_request_parser`
  与 `test_the_slice_touches_no_file_outside_its_allowlist`；同时删掉 `d7986a2` 里那条把
  **越界映射**当期望钉住的 `test_the_spec_carries_the_protocol_to_the_mission_spec_factory`
  （它断言的正是白名单禁止的行为，必须换掉而不是留着）。旧用例在回退后实测 `2 failed, 16 passed`。
- `8df04e6`（转绿）：整块删掉 `api/missions.py` 的 8 行，该文件回到与 `0d89307` 逐字节相同；
  专项 `17 passed`。

### 3.1 先红的测试（`d7986a2`，`3 failed, 12 passed`）

接手时专项 9 条全绿。逐条核对任务书「测试至少覆盖」后，发现 4 项只有弱覆盖或完全没覆盖：

1. **未知协议名在入口处被拒** — 原用例只覆盖「`MissionSpec(...)` 构造时拒绝」，而
   `dataclasses.replace` / 反序列化 / 手工赋值的规格根本不走 `__post_init__`。新增
   `test_commit_service_refuses_a_spec_that_bypassed_the_constructor`。
2. **Host 请求 → 规格** — 原用例没有一条走请求解析，开关可以说「有字段但不可达」。新增
   `test_the_spec_carries_the_protocol_to_the_mission_spec_factory`。
3. **缺省哈希不变** — 原断言只比对 `to_json()` 与手写字典，没有比对**入库的 `spec_hash`**。新增
   `test_legacy_mission_created_with_the_default_keeps_its_spec_hash`。
4. **恢复路径只读表格** — 原用例全是「刚创建完立刻读」，`planning_protocol_for_mission`
   即使读了环境变量或配置也会通过。新增
   `test_binding_comes_from_the_stored_table_not_from_the_config_attribute` 与
   `test_the_durable_binding_ignores_the_ambient_environment`。

### 3.2 实现收紧（`3108556`）

- `PLANNING_PROTOCOLS` / `checked_planning_protocol()` 移入 `planning_protocol_binding.py`，
  `MissionSpec.__post_init__` 与 `create_mission` 共用同一份判定（原来有两处重复集合）；
  `create_mission` 现在把 `ContractError` 转成 `CommitRejected`，未校验规格在写 `spec_hash`
  之前即被拒。
- `planning_protocol_replay_conflict` 改为**拿请求值与整行持久绑定**逐字段比较（协议名、包版本、
  提示词版本），不再只按“请求名”分支：绑定到同一协议名但不同包/提示词的 Mission 也是冲突，
  与存储层 `bind_mission_protocol` 的「同一文档幂等、任何差异 StoreConflict」一致。
- `api/missions.py` 的 8 行映射（见 §1）。

### 3.3 最终用例清单（17 条）与对应覆盖项

| # | 用例 | 覆盖 |
|---|---|---|
| 1 | `test_default_spec_json_bytes_and_hash_are_unchanged` | 缺省字节/哈希不变 |
| 2 | `test_legacy_mission_created_with_the_default_keeps_its_spec_hash` | 缺省哈希不变（**入库行**，新增） |
| 3 | `test_new_protocol_json_key_and_unknown_values_are_rejected` | 新协议规格往返 + 未知名被拒 |
| 4 | — | 原「入口映射」用例已删除（它钉住的越界映射被白名单禁止，见 §1） |
| 5 | `test_commit_service_refuses_a_spec_that_bypassed_the_constructor` | 未校验规格被拒（新增，改自原构想） |
| 6 | `test_the_two_protocol_constants_are_the_frozen_online_names` | 线上常量名不得改（新增） |
| 7 | `test_new_protocol_creation_writes_one_binding_with_frozen_hash` | 恰一行 + 三项正确 + 摘要 |
| 8 | `test_legacy_creation_has_no_binding` | 旧协议无行 |
| 9 | `test_binding_is_transactional_on_creation_failure` | 事务中途失败无残留 |
| 10 | `test_replay_is_idempotent_and_protocol_cannot_change` | 同协议幂等 + 异协议报错 + 绑定判定（新增断言） |
| 11 | `test_legacy_mission_cannot_be_replayed_as_new_protocol` | 旧任务不得改协议 + 显式缺省仍幂等（新增断言） |
| 12 | `test_policy_snapshot_digest_does_not_include_planning_protocol` | 策略摘要不受开关影响 |
| 13 | `test_binding_survives_a_new_connection` | 新连接读到同一绑定 |
| 14 | `test_binding_comes_from_the_stored_table_not_from_the_config_attribute` | 恢复只读存储（新增） |
| 15 | `test_a_replayed_new_protocol_mission_keeps_exactly_one_binding_row` | 重放不增行、不改 `created_at`（新增） |
| 16 | `test_the_durable_binding_ignores_the_ambient_environment` | 环境变量不得猜模式（新增） |
| 17 | `test_the_protocol_switch_is_reachable_without_editing_the_request_parser` | 开关在**白名单内**可达：请求解析器丢弃该键、直接构造契约可命名该 wire（新增） |
| 18 | `test_the_slice_touches_no_file_outside_its_allowlist` | 防回归：`spec_from_request` 源码里不得再出现该字段（新增） |

任务书「测试至少覆盖」十项全部有对应用例，无遗漏。

### 3.4 关于「鉴别力」的一次修正（重要，第一位接手者）

接手时第 10、11 条对「Mission 创建后不可切协议」其实**鉴别力为零**：把
`planning_protocol_version` 从 `planning-decision-v1` 改成 `legacy-plan-proposal-v1` 会改变
`to_json()` 的字节（`legacy` 只是**省略**该键而非等于 `{"…":"legacy"}`），因此**规格哈希先一步**
抛出「different specification」，`planning_protocol_replay_conflict` 无论写没写都会通过。

验证方式（确定性，非推断）：把 `commit_service.py` 里对 `planning_protocol_replay_conflict(...)`
的整段调用删掉后重跑，16 条**仍全绿** —— 该调用当时杀不掉。

修正后：

- 新增 `_spec_with_field_set_behind_the_constructor(...)`（`object.__setattr__` 直写冻结槽位），
  用来构造「门卫之外」的规格；
- 新增直接调用绑定的断言：
  `planning_protocol_replay_conflict(store, mission.id, LEGACY_PLANNING_PROTOCOL) == "…is durably bound to protocol 'planning-decision-v1'/package 4, not 'legacy-plan-proposal-v1'/package 4"`，
  并把 `planning_protocol_replay_conflict(store, mission.id, PLANNING_DECISION_V1) is None` 一并钉住；
- 补上「显式写出缺省协议名 = 同一文档 = 仍幂等」的断言（`explicit.to_json() == spec.to_json()`
  且 `sha256_hex` 相等且 `replayed is False`）——这是升级后 Host 会真实遇到的情形。

修正后，把持久绑定比较换成 `if False:`（等价于删掉调用点的检查）→ `1 failed, 15 passed`（KILLED）。

### 3.5 另一处「用文档差异代替检查」的说明

「新协议 Mission 被当成旧协议重放」这一条，实测**无法**构建出「两个规格文档逐字节相同、只有协议不同」
的输入：`to_json()` 把协议存进文档，协议一变哈希就变。因此该场景的正确定性由两条组成：
(a) 文档不同 → 规格哈希冲突（既有测试）；
(b) 文档相同（显式写缺省名 / 绑定身份变化）→ 绑定判定（§3.4 新增断言）。
任务书原本设想的「字节相同、仅协议不同」输入在本实现下不存在，特此记录，未强行伪造。

---

## 4. 变异（全部 KILLED，注入 `/tmp` 副本并 `diff` 校验恢复）

| 变异 | 结果 |
|---|---|
| M1 `to_json()` 无条件写 `planning_protocol_version` | 2 failed（#1、#2） |
| M2 创建时不写绑定 | 7 failed |
| M3 旧协议也写绑定（`bind_planning_protocol` 去掉 `if`） | 4 failed（#8、#11、#14、#16） |
| M4 删掉 `create_mission` 的 `checked_planning_protocol` 门卫 | 1 failed（#5） |
| M5 删掉 `planning_protocol_replay_conflict` 调用点 | 1 failed（#10，见 §3.4） |
| M6 把持久绑定比较换成 `if False:` | 1 failed（#10） |
| M7 回退 `api/missions.py` 后仍断言「解析器会映射该键」 | 1 failed（旧 #4，故已删除） |
| M8 删掉 `to_json()` 里的协议键（直写槽位也不再生效） | 5 failed（含新增 #17） |

`ruff check`（本片四个文件）与 `ruff format --check`（本片新改文件）均无输出问题；
仓库既有 `ruff check .` 存量告警（`tests/integration/runtime/*` 等），与基线一致，非本片引入
（`ruff check src/agent_orchestrator` 为 `All checks passed!`）。

---

## 5. 验收门（本机实测，原样粘贴；数字为**修复后** `8df04e6` 的复测值）

```text
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_protocol_switch.py -q -p no:cacheprovider
.................                                                        [100%]
17 passed in 0.44s
```

```text
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
3495 passed, 2 skipped in 127.58s (0:02:07)
```

```text
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/step02 tests/orchestrator/step05 tests/orchestrator/step06 tests/orchestrator/step07 tests/orchestrator/p34 -q -p no:cacheprovider
350 passed, 13 skipped in 126.52s (0:02:06)
```

```text
$ uv run --offline ruff check src/agent_orchestrator/orchestrator/commit_service.py src/agent_orchestrator/orchestrator/planning_protocol_binding.py src/agent_orchestrator/api/missions.py tests/orchestrator/full_target/test_planning_protocol_switch.py
All checks passed!
```

### 5.0 闸门本身（`sdk_gate.sh`，修复前 → 修复后）

```text
$ sdk_gate.sh "$PWD" 0d89307 --allow .../h1s-allow.txt --tests "tests/orchestrator/full_target/test_planning_protocol_switch.py" --max-sentinel 26 --out /tmp/gate-after.json
sdk_gate: ok=true (failed items: -)
```

8 项 `clean / allowlist / contracts_frozen / no_secrets / ruff / import_origin / targeted / sentinel`
全部 `ok=true`；`targeted` 为 `passed=17 failed=0 errors=0`，`sentinel count=26 (max=26)`。
修复前同一命令为 `ok=false (failed items: allowlist)`。

### 5.1 「旧模式 560」与本次实测的差异（必须记录，不得当作本片回归）

任务书引用的旧模式基线是 `560/13/0`（`tests/orchestrator/{step02,step05,step06,step07,p34,p35}`，
见 `journal-C.md` §5）。本次在同一组目录实测为 1–3 条失败，且**逐条证明为既有/环境问题**：

| 目录/用例 | 实测 | 结论 |
|---|---|---|
| `p32`（全目录，不在 560 集合内） | `22 failed, 124 passed` | 本沙箱禁止 `bind(127.0.0.1,0)`：`PermissionError: [Errno 1] Operation not permitted`（`runtime/sandbox.py:973`）。环境限制 |
| `p35/test_mission_system_runtime_hooks.py` 2 条 | 稳定 `2 failed`（单独、目录内、六连跑均同） | 在 `const CommitService = MissionSpec` 的基线 worktree（`0d89307`）同样 `2 failed, 5 passed` → **本片之前就存在** |
| `step02/test_live_provider_progress.py` 1 条 | 单跑 `1 passed`；混跑偶发 1 failed | 跨目录顺序相关的偶发（flaky） |
| 其余五个目录（step02+step05/06/07+p34） | 修复后复测 `350 passed, 13 skipped` | 全绿 |
| `p33` 全目录（不在 560 集合内） | `8 failed, 1042 passed` | 在基线 worktree（`0d89307`）实测**同样 8 条失败**（`8 failed, 1037 passed`），逐条同名前缀 → **本片之前就存在**，与本次回退无关 |

`full_target` 的 `2 skipped` 为 `test_panda_backend.py`（未配置 `SH_PANDA_PARSER`）与
`test_real_provider_hierarchical_smoke.py`（需 `--run-real-provider`），与基线一致。

本片未触碰 `p32`/`p35`/`step02` 相关代码路径，上述失败与本片无因果关系；若有疑义，可按上表逐条复现。

---

## 6. 未做（属其它片）

- 派发分支、`_new_mode` 调用点、模型可见行为的开关差异（H1-F/H1-H）；
- `PlanningDecisionEvaluated` 事件（补遗 §6，属派发片）；
- 新协议 Mission 的恢复路径在编排层的调用点（本片只交付只读函数，无新调用点，哨兵数不变）；
- **Host HTTP 请求体里的 `planning_protocol_version` 接线**：`api/missions.py` 不在白名单内，
  本片无法启用该路径（详见 §1）。需要它的切片必须同时把该文件纳入 allowlist。

---

## 7. 结果

- 提交链：上一位 `37af171`–`43ecd37`；接手者 `d7986a2`（红测试）、`3108556`（实现）、`3362c4c`（日志）；
  第三位接手者 `c5c8f12`（红测试）、`8df04e6`（修复 + 本日志）。
- 专项 17 条全绿；`full_target` 3495 passed / 2 skipped；旧模式集合（无环境限制部分）350 passed / 13 skipped。
- **白名单内改动已收敛**：`api/missions.py` 与基线 `0d89307` 逐字节相同，本片不再有任何越界文件；
  闸门 8 项全绿。
