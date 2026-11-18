# 第四题：组合命令的工作目录跟踪

副本：`/tmp/corecoder-bash-cwd-baseline-01/candidate`

环境：`/tmp/corecoder-bash-cwd-baseline-01/venv`

起点与第一题修复前的源码清单一致，不带入前三题或空回复修复。

## 问题是什么

BashTool 每次启动新的命令进程；为方便连续操作，它另外记录目录，使下一次调用从上次目录开始。
当前靠整条命令返回码和对命令文本的扫描更新记录，因此有两种相反的错误：

- `cd sub && false`：cd 已执行，只是后面失败，却忘记目录变化。
- `false && cd sub; true`：cd 没执行，整条最终返回成功，却误记目录已变化。

验收运行真实本地 shell 和真实 BashTool，用临时目录检查记录目录与下一次 pwd。
不是让模型判断输出是否正确。本题不处理第五题的 read/edit 等工具相对路径一致性。

## 验收范围

14 项：&&/分号失败尾命令、跳过 cd、失败 cd、多级 cd、成功链、子 shell、引号文本、
空格目录、stdout/stderr/退出码保留、线程隔离、Agent 并行批次的目录继承与合并。
只新增 macOS/POSIX 下正常结束的命令要求；超时和显式 exit 时的目录恢复不在新增范围。
已有安全检查、超时、输出截断和 Windows 路由不能被删除。

测试只在临时目录执行无破坏性本地命令，不调用模型、不改变进程全局目录。

## 手动运行（从提交任务起会调用模型）

```sh
cd /tmp/corecoder-bash-cwd-baseline-01/candidate
source /tmp/corecoder-bash-cwd-baseline-01/venv/bin/activate
```

使用与前三题相同的模型、服务商和有效密钥，通过终端环境变量配置；源码副本不带 .env。
配置后再启动记录，避免把密钥录进终端日志，不要截图密钥。

```sh
script -q /tmp/corecoder-bash-cwd-baseline-01/manual-terminal.txt env HOME=/tmp/corecoder-bash-cwd-baseline-01/agent-home /tmp/corecoder-bash-cwd-baseline-01/venv/bin/python -m corecoder
```

另开终端复制任务，回到 CoreCoder 粘贴提交：

```sh
pbcopy < /Users/qiaoya/Desktop/CoreCoder/baseline/bash_cwd/task.md
```

检查并允许工具请求；第一次运行不追加指导、不人工改代码。
结束后输入 `/tokens`、`/save`、`quit`，保留会话和终端日志。
空回复或触及轮次上限时也先保存，不在基线中混入运行时修复；完成与否由外部验收检查。

## 修复后外部检查（不调用模型）

```sh
/tmp/corecoder-bash-cwd-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/bash_cwd/check.py after
tail -8 /tmp/corecoder-bash-cwd-baseline-01/acceptance-after.txt
tail -8 /tmp/corecoder-bash-cwd-baseline-01/regression-after.txt
```

目标：14 项全部通过，原有测试没有新增失败，已有测试未修改/删除。
已有 README 行数失败单独记录。测试需允许本机 HTTP 服务，真实模型测试明确排除。
脚本只记录检查，不反馈给 Agent，也不自动继续修复。
如以后人工续跑，使用 `check.py after --label continuation-01` 单独保存，不能覆盖第一次结果。

## 单独复现

```sh
CORECODER_TARGET=/tmp/corecoder-bash-cwd-baseline-01/candidate /tmp/corecoder-bash-cwd-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/bash_cwd/reproduce.py
```

复现脚本退出 0 只表示脚本正常执行，验收结果才表示是否修好。
修复前日志在实验目录，并归档在 observed/。
