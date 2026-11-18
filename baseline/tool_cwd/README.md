# 第五题：命令工具和文件工具的当前目录一致性

副本：`/tmp/corecoder-tool-cwd-baseline-01/candidate`

环境：`/tmp/corecoder-tool-cwd-baseline-01/venv`

源码起点与第一题修复前一致，不带入前四题或空回复修复。

## 一个例子

```text
project/
├── note.txt          内容 ROOT VALUE
└── backend/
    └── note.txt      内容 SUB VALUE
```

命令工具成功 cd backend 后，cat note.txt 读取子目录文件，read_file("note.txt") 却读根目录文件。
因为 bash 使用自己记录的会话目录，而文件工具默认按 Python 进程目录解析相对路径。
同样的分歧发生在编辑、写入、搜索和查找文件时，可能造成改错同名文件。

第四题研究“目录有没有记对”，本题研究“记对了以后其他工具用不用它”。
本题只用成功 cd，避免依赖第四题修复。

## 外部验收

15 个方法，检查 read/edit/write/grep/glob 的相对路径、默认搜索目录、显式相对目录、
绝对路径、~、无会话目录时的回退、批次继承和合并、线程与子 Agent 隔离。
文件均在临时目录中；模型要么不调用，要么由本地固定函数替代，不发模型请求。
测试 fixture 会暂时设置测试进程目录并恢复，修复实现不得用 os.chdir 解决工具一致性。
验证文件内容和实际落盘位置，不依赖 Agent 自述。

## 手动运行（提交任务后会调用模型）

```sh
cd /tmp/corecoder-tool-cwd-baseline-01/candidate
source /tmp/corecoder-tool-cwd-baseline-01/venv/bin/activate
```

通过终端环境变量配置与前四题相同的模型、服务商接口和有效密钥；副本不带 .env。
不要把密钥写进题目或截图；启动终端记录前先配置好。

```sh
script -q /tmp/corecoder-tool-cwd-baseline-01/manual-terminal.txt env HOME=/tmp/corecoder-tool-cwd-baseline-01/agent-home /tmp/corecoder-tool-cwd-baseline-01/venv/bin/python -m corecoder
```

另开终端复制固定题目，再回到 CoreCoder 粘贴提交：

```sh
pbcopy < /Users/qiaoya/Desktop/CoreCoder/baseline/tool_cwd/task.md
```

检查并允许工具请求；首次运行不追加指导，不人工改代码。
结束后输入 `/tokens`、`/save`、`quit`，保留过程。
如出现空回复或达到轮次上限，同样先保存，外部验收后记录结果，不直接追问覆盖首次基线。

## 修复后验收（不调用模型）

```sh
/tmp/corecoder-tool-cwd-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/tool_cwd/check.py after
tail -8 /tmp/corecoder-tool-cwd-baseline-01/acceptance-after.txt
tail -8 /tmp/corecoder-tool-cwd-baseline-01/regression-after.txt
```

目标：15 项全部通过、原有测试不新增失败、已有测试未修改/删除。
已有 README 总行数失败单独记录；具体源码行数文档引用可以同步，不修改测试降低标准。
原有测试包含本机 HTTP 服务，真实模型测试明确排除。
日志生成在实验目录。以后如人工续跑，使用 `check.py after --label continuation-01`，不覆盖旧结果。

## 单独看复现（不调用模型）

```sh
CORECODER_TARGET=/tmp/corecoder-tool-cwd-baseline-01/candidate /tmp/corecoder-tool-cwd-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/tool_cwd/reproduce.py
```

修复前记录在 observed/，不得让候选 Agent 读取或修改外部验收材料。
