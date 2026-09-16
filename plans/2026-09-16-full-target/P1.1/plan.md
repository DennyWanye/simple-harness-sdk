# P1.1：HTN 契约类型与四值谓词求值器（纯内存）

范围（v1.4 §23）：`contracts/htn.py`、`contracts/obligations.py`、`contracts/evidence_state.py`、`contracts/resolution.py`、`knowledge/predicates.py`、`planning/htn/applicability.py`。
红测试：`tests/orchestrator/full_target/test_predicate_truth_table.py`、`test_htn_recursion_fuel.py`、`test_obligation_conservation.py`、`test_semantic_binding_codec.py`、`test_aer_contract_codecs.py`。
门槛：零 store/commit import；diff 不碰 orchestrator/、scheduling/、artifacts/；ruff 与 mypy 通过；编排范围回归零变化。
执行：Opus 子代理单代理实施；主 session 回归、独立审阅、提交。
