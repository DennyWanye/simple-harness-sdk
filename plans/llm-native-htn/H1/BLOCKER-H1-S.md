# H1-S 阻塞：Host 请求体选择新协议不在本片 allowlist 内

日期：2026-09-19。SDK：`/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk-h1s`，
基准 `0d89307`，受检 HEAD `a57ccc7`（本文件随后提交）。
状态：**部分阻塞**——阻塞「Host 经请求体显式选择新协议」这一条接线；不阻塞本片已交付的
「契约字段 + 持久绑定 + 只读恢复函数」，也不阻塞 H1-F/H1-G/H1-H 各片。

## 1. 事实

权威补遗 §8.3 要求「由 Host 为**新建**任务显式选择新协议」，但没有指定**哪一层**接线。
SDK 内唯一把 Host 请求体转成契约的入口是：

- `src/agent_orchestrator/api/missions.py::spec_from_request`（第 56 行起），它构造 `MissionSpec` 时
  **没有** `planning_protocol_version` 参数，因此请求体里的同名键被静默丢弃；
- 上层调用点 `orchestrator/event_handler.py::create_mission`（1593 行起）与
  `create_mission_with_sources`（1613 行起）都只转发请求体，不补该键。

实测（独立核验报告 §1 P1-3 与变异 M11 同口径）：把该键加回 `spec_from_request` 后，
`python - <<` 构造请求即可得到新协议契约；不加回，则请求体里的键无论取值为何都被丢弃，
未知名字也不会被拒。

## 2. 与本片 allowlist 的直接冲突

本片白名单 `任务书-H1-2026-09-19/h1s-allow.txt` 为：

```text
src/agent_orchestrator/orchestrator/commit_service.py
src/agent_orchestrator/orchestrator/planning_protocol_binding.py
tests/orchestrator/full_target/test_planning_protocol_switch.py
plans/llm-native-htn/H1/journal-S.md
plans/llm-native-htn/H1/BLOCKER-*.md
```

`src/agent_orchestrator/api/missions.py` **不在其中**。上一轮的处置正是在闸门上实测到这一点：
`gate-1.json` 的 `allowlist` 项报 `files outside allowlist: src/agent_orchestrator/api/missions.py`，
整个切片因此判红。也就是说：**在现行 allowlist 下，这条接线无法在不弄红闸门的前提下完成。**

核验员给出的两条出路（报告 §5 第 3 条）中，「把 `api/missions.py` 与
`event_handler.py::create_mission` 纳入 allowlist 并按行为接线」属于**改任务书/白名单**，超出实施者
权限；实施者只能选另一条，即本文件。

## 3. 本片实际交付的边界（收窄后的措辞）

- **可达**（有本片内的行为测试）：任何**自行构造契约**的调用方——`__main__` 的 CLI、
  `evaluation/` 各 runner、Host 自己的记录读取路径、以及任何持有 `MissionSpec` 的宿主代码——
  可以直接写 `MissionSpec(..., planning_protocol_version=PLANNING_DECISION_V1)`，
  契约字段生效、绑定写入、恢复可读、未知名字在构造与入口两处被拒。
- **不可达**（本片未交付）：Host 通过 **HTTP/请求体**里的 `planning_protocol_version` 键选择新协议。
  该路径今天仍然静默丢弃该键，且这是**被测试钉住的现状**，不是遗漏。

`journal-S.md` 中「reachable」的措辞据此限定为前者，避免指挥者高估交付面。

## 4. 建议裁定（给后续片）

需要该路径的切片（预计 H1-F/H1-H 的 Host 接线一带）在任务书里**显式把下列文件纳入 allowlist**，
并把本片那两条源码字符串断言替换为行为断言：

1. `src/agent_orchestrator/api/missions.py` —— `spec_from_request` 映射该键，
   合法名字 → 契约带上新协议；未知名字 → `MissionRequestError`；缺省 → 规格字节逐字节不变。
2. `src/agent_orchestrator/orchestrator/event_handler.py` —— 仅在 `create_mission` /
   `create_mission_with_sources` 的既有 door 校验内转发，不改 door 语义。

同时建议明确：**缺省规格字节/哈希不变**这条约束在该片继续成立（本片已用黄金哈希
`d0903d35…d161` 钉住），且未知名字必须在写任何一行之前被拒（本片已在 `create_mission` 入口复核，
见 `commit_service.py` 的 `checked_planning_protocol` 调用点，可被该片复用）。

## 5. 本片不因此降低任何验收要求

- 专项 19 条全绿（含 P1-1/P1-2 修补后新增的两条）；
- 闸门 8 项全绿、工作树干净；
- 缺省规格字节/哈希与基准逐字节相同（独立核验 §2.2 已实测）；
- 本片**不含**任何运行期行为差异（派发分支属 H1-F/H1-H）。
