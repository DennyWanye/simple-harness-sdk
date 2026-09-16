# 计划包规范文件的仓库内副本

本目录是 FULL-TARGET-1.4 计划包规范文件的**逐字节副本**，供 P1.1 的
`test_semantic_binding_codec.py` 与 `test_aer_contract_codecs.py` 使用。
复制而不是原地引用的理由：CI 只 checkout SDK 仓库，计划包在另一个仓库；
若测试依赖仓库外路径，21 条 fixture 校验在 CI 里会静默变成空转。

## 来源

上游根目录（本机）：
`/Users/taiwan/PROJECTS/SimplaHarness/simple_harness/plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4`

计划版本 FULL-TARGET-1.4；复制日期 2026-09-16；SDK 基线 `873fd4a`。

## 清单（SHA-256）

| 本目录文件 | 上游相对路径 | SHA-256 |
|---|---|---|
| `aer/index.json` | `annex/aer-1.0/fixtures/index.json` | `e47af11bb516fb52ad77190c4460857cbd04c2c1373200066fb6868afe0f359b` |
| `aer/operation-envelope.invalid.json` | `annex/aer-1.0/fixtures/operation-envelope.invalid.json` | `24a9cad5f12a7ecc3413943a03619d8c0e297e7c8313ef2435ed80ff1c0456fc` |
| `aer/operation-envelope.schema.json` | `annex/aer-1.0/schemas/operation-envelope.schema.json` | `6fbb366e37d21c61d4543ac787cd6ab6c0f58c47a6749075d11f1ec476c87148` |
| `aer/operation-envelope.valid.json` | `annex/aer-1.0/fixtures/operation-envelope.valid.json` | `ce4a636928ee8ca11bdea5d2640c4272e74957889b6af357a0a6a9d5bb35f22f` |
| `aer/reconciliation-result.invalid.json` | `annex/aer-1.0/fixtures/reconciliation-result.invalid.json` | `9184fd268fa7248c58ab7a0f115e05739526ba566b3ed32eb391f329e069f7de` |
| `aer/reconciliation-result.schema.json` | `annex/aer-1.0/schemas/reconciliation-result.schema.json` | `9eae09e88442d0d5b89a4e1430c995746146a5d686b8d09185e6c84a44f7ecd5` |
| `aer/reconciliation-result.valid.json` | `annex/aer-1.0/fixtures/reconciliation-result.valid.json` | `9026751237ab44ad51bbd11ff722e6c47cf555fd9e37d21820567102f45174f3` |
| `aer/review-record.invalid.json` | `annex/aer-1.0/fixtures/review-record.invalid.json` | `55a02c922715e742d67777cb3f743ff9ef811ce03f01fff126611ca73e42da43` |
| `aer/review-record.schema.json` | `annex/aer-1.0/schemas/review-record.schema.json` | `ea3c1d06ed74dbb7594ea2efb17b14eddd7a7da6d2a8a50d3bca9005f00f9d06` |
| `aer/review-record.valid.json` | `annex/aer-1.0/fixtures/review-record.valid.json` | `54684bf0ab0303e021da17b43291eb888fab454459bc133608e791751e549c07` |
| `aer/validity-witness.invalid.json` | `annex/aer-1.0/fixtures/validity-witness.invalid.json` | `6f277535db216e753bc1b7df6ff042e5184bd0de275f31d3385796d621d8c5b1` |
| `aer/validity-witness.schema.json` | `annex/aer-1.0/schemas/validity-witness.schema.json` | `97dc1ff5df80d0975da6da55c34392febb6d13eb06283f1eb6cf3caf0f796d71` |
| `aer/validity-witness.valid.json` | `annex/aer-1.0/fixtures/validity-witness.valid.json` | `aaf463b47c01374a71ebc528e78c83da98b1267501ce8b1fc5e7447221494869` |
| `plan_pack/execution-feedback-v1.schema.json` | `schemas/execution-feedback-v1.schema.json` | `1a16bb15e18edf9583f8f5d646531fdfa498a93b100232ab436b1ebca5f1a34e` |
| `plan_pack/fixtures.json` | `schema-fixtures/fixtures.json` | `5707e0e195c420533157262be3762af1f081cd2a6ac7295063d62817e20be356` |
| `plan_pack/goal-resolution-v1.schema.json` | `schemas/goal-resolution-v1.schema.json` | `f53736d9e4c3bb931866089803a237e736ba5a77dd0b98d36a04f567b64a984f` |
| `plan_pack/method-contract-v1.schema.json` | `schemas/method-contract-v1.schema.json` | `68e55314c4ef278a42221b7651be0eb707a08982f86b2a474aa8caab3e7306e1` |
| `plan_pack/plan-revision-proposal-v1.schema.json` | `schemas/plan-revision-proposal-v1.schema.json` | `4e1c7cff95d6d7a44a83d39e8349f0aedc5f8b4a55227c7aad8d51f320cb1daa` |

## 漂移检查

`full_target_world.py` 默认读本目录的副本。当本机存在上游计划包时
（或设置了 `SIMPLEHARNESS_PLAN_PACK`），`test_repo_copies_match_the_upstream_plan_pack`
会逐文件比对 SHA-256 并在漂移时失败；计划包不在时该检查跳过，**主测试不跳过**。

上游规范修订后：重新复制这些文件并更新上表的 SHA-256，再让红测试说明改动的后果。
