# 第三题：默认 Agent 实例隔离

独立副本：`/tmp/corecoder-agent-isolation-baseline-01/candidate`

独立环境：`/tmp/corecoder-agent-isolation-baseline-01/venv`

源码与第一题修复前起点完全相同，不带入会话、换行或空响应修复。
本题只检查默认 Agent 构造路径；自定义工具显式共享不在范围内。

## 问题和验收

A、B 是两个不同 Agent，却会拿到相同的有状态默认工具：
A 的待办出现在 B，B 更新清单覆盖 A；B 还会覆盖 A 的子 Agent 工具里的父 Agent 关联。
因此 A 派生的 worker 可能拿到 B 的模型、权限和 hooks。

外部验收 12 个方法：清单初始为空、双向更新/清空隔离、提示词无泄漏、
父 Agent 关联正确、子 Agent 配置继承、父子清单独立、禁止递归派生、消息隔离、
工具 schema 兼容、空工具列表、自定义对象兼容。

验收用假模型（调用 chat 会直接报错），不发模型请求；不运行工具命令、不读写真实用户会话。
工具注册表状态在每个测试后恢复，防止一个测试的污染影响下一项。
原有回归测试中真实模型测试明确排除，本机 HTTP 服务测试仍保留。

## 查看复现（不调用模型）

```sh
CORECODER_TARGET=/tmp/corecoder-agent-isolation-baseline-01/candidate /tmp/corecoder-agent-isolation-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/agent_isolation/reproduce.py
```

它会展示 B 偷看到的清单，以及 A 派生子 Agent 时模型/权限归属不正确的结果。
脚本退出 0 仅表示执行完成，不能代替验收结果。

## 手动让原版 CoreCoder 修复（会调用模型）

```sh
cd /tmp/corecoder-agent-isolation-baseline-01/candidate
source /tmp/corecoder-agent-isolation-baseline-01/venv/bin/activate
```

在终端配置和前两题相同的模型、服务商接口和有效密钥。不要把密钥写进任务或截图。
副本不复制 .env；启动终端记录前先配置好环境变量。

```sh
script -q /tmp/corecoder-agent-isolation-baseline-01/manual-terminal.txt env HOME=/tmp/corecoder-agent-isolation-baseline-01/agent-home /tmp/corecoder-agent-isolation-baseline-01/venv/bin/python -m corecoder
```

另开终端复制题目，回到 CoreCoder 粘贴提交：

```sh
pbcopy < /Users/qiaoya/Desktop/CoreCoder/baseline/agent_isolation/task.md
```

允许你检查过的工具请求。先不追加指导、不人工改代码、不读取外部验收。
完成后输入 `/tokens`、`/save`、`quit`，保留终端过程与会话。
如果又发生空回复，先保存并退出，将这次记为失败，不在本次运行中混入空回复修复。

## 修复后独立验收（不调用模型）

```sh
/tmp/corecoder-agent-isolation-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/agent_isolation/check.py after
tail -8 /tmp/corecoder-agent-isolation-baseline-01/acceptance-after.txt
tail -8 /tmp/corecoder-agent-isolation-baseline-01/regression-after.txt
```

目标：12 个方法全部通过、原有测试不新增失败、原有测试未修改或删除。
已有 README 总行数失败保留，不应改测试放宽标准；源码变化使具体文档引用失效时可以同步。
程序使用隔离测试用户目录，结果保存在实验目录。

检查脚本拒绝覆盖旧日志。如果以后做人工续跑，另存新标签：

```sh
/tmp/corecoder-agent-isolation-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/agent_isolation/check.py after --label continuation-01
```

这会生成 acceptance-continuation-01.txt 等文件，保留首次结果，不要把续跑记成首次成功。
准备阶段结果归档在 observed/；不得让候选 Agent 读取或修改这些材料。
