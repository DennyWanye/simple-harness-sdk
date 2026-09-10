# T2 第 0 步：基线复核与锚点核对

- 时间：2026-09-10（T2 开工前）
- 固定回归命令（见 `../baseline.md`）在 `fd12e7dd` 上：`60 failed / 15 errors / 1843 passed / 2 skipped`，红集与 `../baseline-known-failures.txt` `diff` 为空（3.12 与 3.14 一致）。
- 锚点核对：closure 复核已在 `fd12e7dd` 工作树上逐行核对 plan v2 的锚点；实测偏差按附 E7 订正：`tests/unit/contracts/test_public_api.py` 硬编码版本在 `:17/:42/:52/:62`；`react.py __all__` 在 `:453`；`react_loop.py` 的 `call_ordinal` 循环在 `:735`；`execution/effects.py` 的 `EffectRecord.arguments` 在 `:277`。其余锚点 OK。
- v10 预计牵动的既有红夹具（schema/迁移类）：`tests/integration/execution/test_schema_v1.py`、`tests/integration/execution/test_open_close.py`、`tests/integration/runtime/test_short_context_migration.py`、`tests/integration/runtime/test_context_use_migration.py`、`tests/integration/test_context_authority_storage_v015.py`、`tests/execution/test_command_ingress.py`、`tests/execution/test_execution_schema_v3.py`、`tests/execution/test_audit_schema.py`、`tests/execution/test_stage_audit_schema.py`。
- T2 收尾实测：v10 之后红集与基线**逐条相同**（60 failed / 15 errors），新增 8 条测试全绿——这些夹具原本就因缺旧版本环境或旧断言而红，v10 没有改变它们的失败方式，也没有新增红。
