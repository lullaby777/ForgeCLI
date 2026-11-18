# 第二、第四题：原版与整套改进的受控对照

已实现的改进包括两部分：ISSUE-001 的空回复/恢复防护，以及独立验收、失败反馈和有限续跑。
本次只准备实验与离线检查，未调用真实模型。原五份副本和原始结果不改动。

## 四组新案例

实验根目录：`/tmp/corecoder-compare-01`。

| 案例目录 | 任务 | 运行时 |
|---|---|---|
| task02-original | 换行保留 | 原版 Agent，单轮执行，不接收验收反馈 |
| task02-improved | 换行保留 | ISSUE-001 + 独立验收闭环 |
| task04-original | 组合命令目录跟踪 | 原版 Agent，单轮执行，不接收验收反馈 |
| task04-improved | 组合命令目录跟踪 | ISSUE-001 + 独立验收闭环 |

每组含 `workspace`（模型修改的题目）、`runtime`（冻结的执行器）、task.md、case.json 和 seal.json。
四份 workspace 完全相同，来自第一题修复前的源码快照；没有带入任何题目的修复答案。
runtime 与 workspace 分开，避免把执行器的改进误当成题目修复，或模型自改执行器。
两个 runtime 的 edit.py/bash.py 均保留题目原始缺陷，已检查没有预装答案。
模型只能修改 workspace；runtime、外部验收和实验设置受指纹检查保护。这不是 OS 安全沙箱。

## 固定条件与公平性

- 模型 deepseek-flash；地址 https://api.deepseek.com；温度 0。
- 每次输出上限 4096，最大上下文 128000，请求超时 60 秒。
- 统一依赖与 Python；SDK 自动重试设为 0，原内核的请求重试仍保留，两边一致。
- 总计最多 30 次模型 chat 入口调用（包括压缩、子 Agent、空回复重试），每轮最多 30 次。
- 原版只运行一次 Agent.chat；改进版最多 3 次尝试，但不是每次再给 30 次：共享总计 30 次。
- 改进版默认空回复重试一次、连续无进展两次停止；原版保持原本行为。这些正是实验处理差异。
- 两边执行进程均由外部看门程序限制为 900 秒，POSIX 下超时终止整个进程组。
- 中间验收时间计入改进版执行预算；两边结束后均进行一次相同的最终评分，另限验收 60 秒、回归 120 秒。
- 30 次是模型入口数，不含内核内部 HTTP 重试，不是美元硬预算；usage 缺失或进程被强杀时保留未知。
- 初始任务内容相同；首个请求去除案例路径差异后记哈希。系统提示词/工具与模型参数一致，不加载个人 MCP/hooks。
- 工具预授权、使用独立用户目录，模型配置只从冻结 case.json 读取；环境中的模型名/接口变量不会覆盖它。
- 仅 CORECODER_API_KEY 来自用户终端，不落盘、不打印。pytest 环境附加选项清空并禁用第三方自动插件。

这是“原版 + 通用测量/预算约束”与“整套改进 + 同样约束”的比较，不等同于之前的无测量交互运行。
它不能分离空回复重试、历史修复、失败反馈各自的贡献；若有改善，再做消融实验。
零温度也不保证服务商完全确定；两题各一次只能作为探索，不能宣称稳定提升。

## 如何手动启动

不要进入旧基线，不要手动开启四份副本里的 CLI。使用冻结的统一启动器，让它自动控制预算并记录结果。
先在终端设置有效密钥（不要截图或发到聊天，不要把密钥写进 task/config）。

```sh
export CORECODER_API_KEY='你自己的有效密钥'
```

以下每条命令都会调用真实模型，准备阶段未执行。建议一条完成后再执行下一条；不要中途追加提示或人工修改。
顺序采用第二题原版先、第四题改进版先，减少总是同一组先运行的影响，不能据此消除时间波动。

```sh
/tmp/corecoder-compare-01/venv/bin/python /tmp/corecoder-compare-01/runner/run.py task02 original --live
/tmp/corecoder-compare-01/venv/bin/python /tmp/corecoder-compare-01/runner/run.py task02 improved --live
/tmp/corecoder-compare-01/venv/bin/python /tmp/corecoder-compare-01/runner/run.py task04 improved --live
/tmp/corecoder-compare-01/venv/bin/python /tmp/corecoder-compare-01/runner/run.py task04 original --live
```

没有 --live 不会启动模型，缺密钥不消耗案例。每组只允许执行一次，拒绝覆盖 run 目录；不要删除结果重跑。
若想换模型、预算或重复运行，需要新建整套实验，不能只调整其中一组。
工具调用默认允许，工作目录不是安全沙箱，请仅运行自己授权的本地任务。

## 查看结果（不调用模型）

```sh
/tmp/corecoder-compare-01/venv/bin/python /tmp/corecoder-compare-01/runner/summary.py
```

每组的 `run/summary.json` 是最终评分入口；未运行会显示 not_run。
同时保留 worker.json、messages.json、model-events.jsonl、terminal.txt、最终验收与回归日志、JUnit XML、补丁和 Git 状态。
改进版另有 controller/result.json 和中间验收/事件记录。
Git 补丁不含未跟踪的新测试，必须保留完整 workspace，不能只留 change.patch。

功能通过要求：最终验收全过、没有新增回归失败、已有测试/配置未改、运行时/验收/测量程序未改。
原有 README 行数检查失败作为已知失败单列；新增文件行数引用检查失败仍计入新增失败。
最终评分输出不再反馈给原版；改进版只接收预算内的中间验收反馈。
执行停止原因与功能结果分开：超时/轮次上限时可能已有合格补丁；API 错误不能直接解释成补丁能力不足。
token 记录采用 reported usage，不根据提示词字符数猜计费。没有数据就填未知，不写零。
如果执行进程被强杀，worker.json 可能缺失，外部记录仍保留看门状态和最终验收，未知用量不能补猜。

## 已做离线验证

四份 workspace 哈希相同，第二题均应有 5 个验收失败，第四题均应有 9 个验收失败。
共享起点的原有测试应为 352 通过、1 个已知失败，真实 API 冒烟明确排除。
smoke.py 在一次性合成工作区验证旧版遇空回复结束、改进版有限重试与反馈、最终评分、请求哈希一致性。
它不修改四份待运行 workspace、不提供题目答案，也不证明真实模型效果。
实验运行时、题目、验收和测量脚本均冻结到实验根目录；当前项目之后修改不会悄悄改变这批实验。
