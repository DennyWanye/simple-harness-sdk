# plan-test gate report

RUN: run-20260816-140241
STATE: SHIPPABLE
TESTED HEAD: 70a2b9fe20016772e4a34035fb96520f2f17da1e
GATE RECEIPT: 2ff5ed0b4cd3285230d6ed3a3f32421ac4bcc00eea8cc41d48b3567ab38c6eb5

## 身份说明（tested vs delivery，读 receipt 前必看）
- TESTED HEAD 是**测试时**的代码提交；把本 run-dir 的账本/截图/receipt 提交进仓库
  的后续提交（evidence-only descendant）**不改变被测内容指纹**，receipt 依然有效。
- 所以「receipt 的 head 早于仓库最终 HEAD」可以是完全合法的状态——判定依据是
  内容指纹（排除下方声明范围），不是提交号。若 tested HEAD 之后还改了任何非 run-dir
  文件，validator 会以 TESTED_RUNTIME_MISMATCH / RETEST_REQUIRED_AFTER_CHANGE 拦截。

## 适用性判定（判「不适用」等于放弃对应条件门，理由须可追责）
- input_sensitive: 适用（user 判定）理由：原始验收要求自然语言主Agent与三个Workflow的语义选择和有效结果
- llm_payload_driven: 适用（user 判定）理由：Provider工具调用和Workflow控制载荷会直接驱动durable状态机
- stateful_init: 不适用（user 判定）理由：本slice只实现显式依赖注入SDK且不修改产品首次登录初始化

## 指纹排除范围（init 时冻结的显式声明；事后往仓库塞文件不改变它）
- 声明范围：plans/2026-08-16-sdk-v0.1.1-slice-a/verification/2026-08-16-r3

## 本次命中排除的文件
- plans/2026-08-16-sdk-v0.1.1-slice-a/verification/2026-08-16-r3/baseline-conclusion.md（declared-scope:plans/2026-08-16-sdk-v0.1.1-slice-a/verification/2026-08-16-r3）
- plans/2026-08-16-sdk-v0.1.1-slice-a/verification/2026-08-16-r3/findings-plan-iteration-001-round-1.json（declared-scope:plans/2026-08-16-sdk-v0.1.1-slice-a/verification/2026-08-16-r3）
- plans/2026-08-16-sdk-v0.1.1-slice-a/verification/2026-08-16-r3/plan-test-run.json（declared-scope:plans/2026-08-16-sdk-v0.1.1-slice-a/verification/2026-08-16-r3）

## 收尾期改动（re-attest 记录）
- 2026-08-16T15:48:49+0800｜behavioral｜变更 80 个文件｜理由：实现与测试收口并提交到本地 HEAD 70a2b9f，正式场景将针对该 committed HEAD 重跑

## 审计与账本完整性
- 审计：verdict=PASS engine=architecture-challenger-poincare（产物 auditor-output.json）
- 账本链：自洽（59 条写入，链首 init）

## 场景状态（由 validator 重算）
- SDK-A1 [required]: PASS
- SDK-A2 [required]: PASS
- SDK-A3 [required]: PASS
- SDK-A4 [required]: PASS
- SDK-A5 [required]: PASS
- SDK-A6 [required]: PASS
- SDK-A7 [required]: PASS

## 耗时分解（measured=CLI 单调时钟实测；declared=申报值，低信任）
- automated_test: measured 0.0 min / declared 2.2 min / retry 0 / abort 0 / tests 2355
- implementation: measured 0.0 min / declared 104.0 min / retry 0 / abort 0 / tests 0
- checkpoints: 1
## 被顶替的证据（attach-evidence --replace 留痕）
- receipts/sdk-a6.md（旧 sha256 d6d6b75b8239…，2026-08-16T15:58:55+0800）

