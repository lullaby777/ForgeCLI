# ISSUE-001：空响应与安全恢复

当前项目实现此修复，五个基线副本和 baseline 下原始证据均不更新。
本次不实现自动测试验收闭环，不合入五题的业务修复。

## 行为

- 没有正文且没有工具调用的响应（包括仅有推理文字）视为异常空响应。
- 默认最多重试一次，且受 max_rounds 的总请求轮数约束；可在 Agent(max_empty_retries=0) 中禁用。
- 重试不把空消息加入历史，不重放先前执行过的工具；重复失败后明确返回 empty_response。
- length 与 content_filter 分别停止为 output_limit 和 filtered，不执行可能截断的工具调用。
- 合法的 content=null + tool_calls 消息照常执行、保存和恢复。
- load_session 只在内存清除旧历史中无正文且无工具调用的 assistant 消息，不改写原始文件。
- save_session 与 Agent 直接接收历史也做同一项窄范围清理；不做完整会话协议修复，不随意删除 tool 结果。

## 执行状态

chat 仍返回字符串，保持现有调用接口；agent.last_run 暴露状态、请求轮数、空响应重试次数、结束原因和异常类型。

| status | 含义 |
|---|---|
| idle | 尚未运行或已 reset |
| running | 正在执行 |
| responded | 模型正常返回正文，任务尚未独立验收 |
| empty_response | 异常空响应，有限重试未恢复 |
| output_limit | 服务商报告输出长度限制，正文可能不完整 |
| filtered | 服务商报告内容过滤，响应不能当作正常完成 |
| round_limit | 达到工具循环轮次上限，已有产物仍可能可用 |
| error | 调用或执行异常，保留异常类型 |
| interrupted | 用户中断，未完成工具结果按原有逻辑回填 |

上述状态都不是“补丁通过测试”的结论。后续独立验证器负责给出通过/未通过。
无法获得 finish_reason 时记录 null，不猜测服务商的停止原因。

## CLI 与日志

CLI 在流式文字或推理结束后仍显示执行状态，上限/空回复不再被已有流式输出遮住。
一次性模式异常结束返回退出码 1，中断返回 130。
每次 CLI 启动将 JSONL 事件追加到 ~/.corecoder/runs/<随机标识>.jsonl，屏幕显示路径。
库调用可选 Agent(event_log_path=...)；默认只发 Python logging 元数据。

事件包括运行起止、每轮响应结束原因与 reported token 用量、空响应重试、历史清理、工具起止。
不写入提示词、回复/推理正文、工具参数和输出或异常原文，避免记录密钥及敏感内容。
日志写入失败会警告，不阻断任务。此为诊断事件而非完整可重放 trace，也不保证失败请求计费统计完整。

## 离线回归

tests/test_empty_response.py 使用固定响应、模拟流和本地临时文件，覆盖空响应、有限重试、
工具副作用只执行一次、截断、正常回复、旧历史恢复、CLI 提示、异常/中断和日志失败。
运行完整测试时明确排除 tests/test_live_smoke.py，并使用隔离用户目录，不配置真实模型密钥。
