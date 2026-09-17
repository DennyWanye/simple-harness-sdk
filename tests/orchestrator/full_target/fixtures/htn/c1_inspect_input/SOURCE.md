# c1_inspect_input 夹具来源（P2.3k / N1）

- 真实局：Grok 验收第 2 批 H 臂 `H-L3-C1-r1`（SDK c7cfedd，Host 23a3d6eb，grok-4.6 medium）。
- `method.json`：合成轮准入（`MethodSynthesisRoundRecorded{TRIAL_ADMITTED}`）后写进 `method_contracts` 表的方法定义原文 `code.fix-by-reproduce-patch-verify-explain@1`。六步 read-facts → reproduce → apply-patch → {verify, inspect} → summarize；**inspect 步 `arguments: {}`**（`code.inspect-changeset@1` 不声明任何输入端口），summarize 只接 findings。同形方法也出现在 C2-r0 / C2-r1 / C4-r0 / C4-r1。
- 所有 content_hash 与本仓库 code 域目录一致（种子哈希由 `(id, version)` 派生）；未含任何 mission / agent 标识。
- 用途：钉住缺陷形状——按原定义提交，inspect 叶的 `attempt_inputs` 为空；改绑到 `code.inspect-changeset@2` 的 `patch` 端口后，inspect 叶在 apply-patch 验收前处于 WAITING_DATA，验收后收到补丁产物。证据目录只读，未写入任何文件。
