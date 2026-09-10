# 绿色基线（执行前快照）

- 日期：2026-09-10 13:55
- 仓库：simple-harness-sdk，main = `fd12e7dd7122786865ba61c19c48855a8eadcd8c`（0.7.10），工作树仅 `.gitignore` 有未提交改动（加 `.env` 忽略）
- 环境：`uv sync --group dev` → `.venv`（Python 3.14.7）；`UV_PROJECT_ENVIRONMENT=.venv312 uv sync --python 3.12 --group dev` → `.venv312`（Python 3.12，CI 矩阵为 3.11–3.13）

## 命令与结果

| 检查 | 命令 | 结果 |
|---|---|---|
| pytest 全量 | `.venv/bin/python -m pytest -q -p no:cacheprovider tests` | 收集即中断：3 个模块 import 不存在的 `simple_harness_memory`（`tests/integration/runtime/test_context_use_{admission,durable,public_memory}.py`） |
| pytest 全量（忽略上述 3 文件） | 同上 + `--continue-on-collection-errors --ignore=<3 文件>` | 3.14：**60 failed / 15 errors / 1843 passed / 2 skipped**，44–46 s |
| 同上 Python 3.12 | `.venv312/bin/python -m pytest …` | 与 3.14 红集**逐条相同**（75 条，见 `baseline-known-failures.txt`），说明与解释器无关 |
| mypy | `.venv/bin/mypy`（pyproject 限定 35 个文件） | 通过，0 issues |
| ruff | `.venv/bin/ruff check src tests` | 156 条既有告警（E501 130、I001 17、E702 4、E402 3、E701 1、F401 1）；CI 未跑 ruff 门 |

## 既有红的分类（全部为本次改动前已存在）

| 类别 | 条数 | 代表 | 原因 |
|---|---:|---|---|
| 需要旧版本安装目标/解释器环境变量 | 25 | `H077_LEGACY_075_TARGET`、`H074_PYTHON`、`H073_PYTHON` | 迁移/回退测试要求本机预装历史 wheel，本机未配置 |
| 迁移与 schema 夹具漂移 | ~20 | `test_short_context_migration.py`、`test_context_use_migration.py`、`test_schema_v1.py`（仍断言 v7）、`test_context_authority_storage_v015.py` | 夹具停留在旧 schema 版本 |
| 原子性/恢复夹具 | ~25 | `test_atomic_decision.py`、`test_atomic_root_start.py`、`test_decision_terminal_recovery.py`（15 errors）、`test_workflow_launch_admission_h16.py`、`test_open_close.py` | `UNIQUE constraint failed: run_events` / 计数断言 `assert 2 == 1` 等，夹具与当前内核不一致 |
| 快照/版本断言过期 | 3 | `test_public_api_matches_frozen_snapshot`、`'0.7.10' == '0.7.8'`、`__version__ = "0.7.5"` | 冻结快照未随 0.7.10 更新 |
| 其它 | 2 | `test_logging_observability[tools/executor.py-execute]`、`BUILD_INFO identity differs` | 既有 |

## 回归门口径

- 后续每次回归用同一条命令（3.12 或 3.14 皆可）；新红 = 不在 `baseline-known-failures.txt` 里的 FAILED/ERROR，**一条即阻断**。
- 既有红不在本切片修复范围；若本切片改动触碰到既有红的夹具（如 schema v9→v10 让 `test_schema_v1`/迁移夹具再变），需在 journal 单独说明差异。
