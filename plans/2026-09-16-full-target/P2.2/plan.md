# P2.2：PANDA / HDDL 后端适配

范围（v1.4 §23、§7.4）：`planning/htn/backends/panda.py`。已建模样本的分解见证由独立验证器校验；环境不可用返回显式 SOLVER_UNAVAILABLE，记 UNSUPPORTED 不记 PASS；不支持的 HDDL 特性显式拒绝；超时返回 SEARCH_LIMIT_REACHED 不冒充 UNSOLVABLE。
红测试：`tests/orchestrator/full_target/test_panda_backend.py`（二进制缺席路径、stub 可执行文件模拟输出解析、若本机能构建 pandaPIparser 则真实校验一例）。
门槛：不依赖 P1.1 类型（自带结果类型）；不接 compiler；ruff/mypy 通过。
