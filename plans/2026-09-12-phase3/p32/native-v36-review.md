# 原生发布与人工复核 v36

最后更新：2026-09-14 CST。源码 Host `2eee204e` / SDK `c19bbd0`，不可变 `source-snapshot-v36`。本项通过真实原生 UI 操作与 DeepSeek Flash 调用验收；不代表整个 P3.2 或 Phase3 关闭。

| 用例 | 结果 | 实际证据 |
|---|---|---|
| 模型生成候选、批准后本地发布 | PASS | `mission-49ca089834176fbd`：Planner 正确区分 WEEKLY.md 内容文件和 reports/native-v35.md 目标；审批前目录为空，UI 审批后正式交付。4 调用、12180 tokens、0 预留。 |
| 带理由拒绝并按理由修订 | PASS | `mission-2be836772fc244fe`：空理由时拒绝按钮禁用；提交理由后第二 Attempt 冻结输入包含该理由，无另发评论。实际 UI 打开 REVIEW.md，从“初稿”变为“已按复核意见修改”，再人工通过。10 调用、28212 tokens、0 预留。 |
| 同源码冷启动重读 | PASS | 两任务仍正式交付，分别打开实际文件。选定任务/意图/结果/审批/动作/事件/预算/Provider/journal/context 表哈希全部一致，发布文件未变，14 调用、0 rehandoff，零新增调用或动作交付。 |

发布文件 `published/reports/native-v35.39fd43852c8b.v1.md`，37B；CAS、动作绑定、发布文件及回执内容哈希一致：`3545657c6ad662a000d059f3f9ec65b13c5354f0c4ea1debf1b7cd616f9b2f10`。修订 REVIEW.md 为24B，SHA256 `3dc10d053dc0b7a42926202624162410880a8f83410dd1201a248aeffbb2a8d4`。首版 REJECTED 保留。

原生进程组26770正常退出、无残留，生命周期662.777秒；冷启动30300正常退出、无残留，49.152秒。以上包含人工操作/等待，不是模型推理时长。总真实用量40392 tokens。

原始证据仅在 Host ignored `.local-test-evidence/2026-09-13/p33-g/source-ui-p32-publish-v36/`：01–10截图、before/after-approval、before/after-cold、case-summary.json。summary SHA256 `fd79da9084a915512708f4530f7162de7efa766e357f29e5a323c43d84535b6e`；冷前 `8a525a0d9bc76ce061d696d6c885b17c26bc4053a8872adfad4b56683f05c58d`，冷后 `fca7157f06042105ad1ecc5b2d8f5f560a4e7061f7ea49dd82aa7179d006459a`。后两文件记录时间不同，内部选定事实相同。

保留 v35 原始 FAIL。当前只关闭正常审批发布与复核理由回传/冷读切片；不把它替代改内容重批、丢回执/UNKNOWN、沙箱等其余原始 AC。普通产物卡长哈希的视觉溢出仍是已观察限制。LC2 旧库共存原生、P34 严格真实对照及最终累计仍待；打包、安装器、发布推送均未执行。
