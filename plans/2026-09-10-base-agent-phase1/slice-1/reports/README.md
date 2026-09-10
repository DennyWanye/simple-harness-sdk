# Slice 1 真实 provider 报告归档（key 已脱敏；mock 报告见 journal §4.2）

| 文件 | 端点/模型 | 结果 | 备注 |
|---|---|---|---|
| `real-provider-run.txt` | DeepSeek `deepseek-v4-pro` | failed | 修 wire **前**：主 Agent 第二次请求被端点拒绝（assistant 无 tool_calls） |
| `real-provider-run2.txt` | 同上 | failed | 修 wire 后第 1 次：`provider_response_not_durable`（既有 dispatch 校验，F-BA-1） |
| `real-provider-run3.txt` | 同上 | failed | 修 wire 后第 2 次：同上 |
| `real-provider-run4.txt` | 同上 | **passed** 12.79 s | `main_state committed`、`nonce_in_final_answer true`、provider 3 次 |
| `real-provider-run5.txt` | 同上 | **passed** 10.21 s | review 修复后复跑 |
| `demo-run2.txt` | 同上（`examples/base_agent_delegation.py`） | exit 0 | 最终回答含子 Agent 验证码 |

修 wire 后真实端点成功率 2/4（run2/run3 失败均为主 Agent 最后一次综合调用的 `provider_response_not_durable`，子 Agent 结论与委派结算正常，Agent 存活）。svtun `gpt-5.6-luna` 直连探针 90 s 超时，未用于本片。
