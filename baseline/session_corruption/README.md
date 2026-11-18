# 会话损坏：第一道基线任务

本目录只有复现、外部验收和运行辅助材料，不修改业务代码。
验收直接加载目标仓库的真实 session.py，不需要模型或第三方依赖；
它不覆盖 CLI 集成，因此修复后还需要运行项目原有测试。

## 1. 查看原始缺陷

在项目根目录执行：

```sh
python3 baseline/session_corruption/reproduce.py
python3 -m unittest discover -s baseline/session_corruption -v
```

复现脚本会打印加载时的 TypeError 和列表时的 AttributeError。
复现脚本捕获异常，退出码为 0 不代表已修复；以验收测试退出码为准。
验收只读写自动清理的临时目录，绝不使用真实用户会话目录。
原版验收应失败；修复后要求全部通过。

验收契约：顶层对象，非空字符串 model，列表 messages；每条消息是对象，
role 是非空字符串，content 如果存在则为字符串或 null。
不要求完整校验所有模型协议字段；空历史和工具调用历史必须保留兼容。
坏文件应被忽略，不应删除或改写。测试中的 subTest 会使错误数量大于测试方法数量。

## 2. 准备独立副本与环境

不要让 Agent 直接修改你日常开发的工作区。下面复制当前完整源码（包括未提交修改），
但不复制密钥、Git 元数据、个人配置和本目录的外部验收材料。
路径存在时换一个新目录；每次实验都从未修复的原始快照重新复制。

```sh
cd /Users/qiaoya/Desktop/CoreCoder
mkdir /tmp/corecoder-session-baseline-01
rsync -a --exclude='.git' --exclude='.env*' --exclude='.venv' --exclude='venv' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.codex' --exclude='.agents' --exclude='.aws' --exclude='.vscode' --exclude='baseline' ./ /tmp/corecoder-session-baseline-01/candidate/
python3 -m venv /tmp/corecoder-session-baseline-01/venv
/tmp/corecoder-session-baseline-01/venv/bin/python -m pip install -e '/tmp/corecoder-session-baseline-01/candidate[dev]'
```

在候选副本中建立起始快照（只操作这份副本）：

```sh
cd /tmp/corecoder-session-baseline-01/candidate
git init
git add .
git -c user.name='Baseline' -c user.email='baseline@example.invalid' commit -m 'Baseline starting snapshot'
git rev-parse HEAD > /tmp/corecoder-session-baseline-01/start-commit.txt
/tmp/corecoder-session-baseline-01/venv/bin/python -m pip freeze > /tmp/corecoder-session-baseline-01/dependencies.txt
/tmp/corecoder-session-baseline-01/venv/bin/python -m pytest tests/ -q > /tmp/corecoder-session-baseline-01/regression-before.txt 2>&1
echo $?
```

先检查原有测试的退出码和输出。如果未通过，先解决环境或明确已有失败，不要把它归为 Agent 修复失败。
把外部验收在候选副本上也运行一次，保存修复前结果（此处失败是预期）：

```sh
CORECODER_TARGET=/tmp/corecoder-session-baseline-01/candidate python3 -m unittest discover -s /Users/qiaoya/Desktop/CoreCoder/baseline/session_corruption -v > /tmp/corecoder-session-baseline-01/acceptance-before.txt 2>&1
echo $?
```

## 3. 运行原版 Agent

在终端中配置你自己的模型名称、API 地址和 API key，使用项目原本支持的环境变量。
密钥不要写进任务、报告或命令日志。副本不含原项目的 .env，因此需要环境变量提供配置。

```sh
/tmp/corecoder-session-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/session_corruption/run_baseline.py /tmp/corecoder-session-baseline-01/candidate /tmp/corecoder-session-baseline-01/result
```

这个辅助入口使用候选副本未经修改的 Agent/LLM，调用一次 Agent.chat，保留原有内部工具循环、
压缩与模型请求重试，不添加外部验收反馈或额外修复轮次。使用默认 50 轮上限。
它自动允许工具操作，工作目录是候选副本，但独立目录不是安全沙箱。
为减少个人配置影响，不加载 CLI 的个人 hooks/MCP；这是本次基线的固定配置。
模型可自行运行原有或新写的开发测试。外部验收留在副本之外，任务要求它不得读取或修改。
这是流程约束，不是对恶意 Agent 的强隔离。

输出包括原始任务、最终回答、消息历史、耗时、模型配置、token 与费用估算。
消息历史可能被原有压缩机制改写，不是完整逐事件 trace。
token 仅代表现有模型层报告并累计的用量，可能不包含失败请求/缺失 usage；费用未知时为 null。
执行返回不等于修复成功；脚本不会自动验收。它也不承诺严格的总时间或美元硬预算。
不要手动提示它继续修复；追加提示应作为另一次实验。

## 4. 外部验收和回归

```sh
CORECODER_TARGET=/tmp/corecoder-session-baseline-01/candidate python3 -m unittest discover -s /Users/qiaoya/Desktop/CoreCoder/baseline/session_corruption -v > /tmp/corecoder-session-baseline-01/acceptance-after.txt 2>&1
echo $?
cd /tmp/corecoder-session-baseline-01/candidate
/tmp/corecoder-session-baseline-01/venv/bin/python -m pytest tests/ -q > /tmp/corecoder-session-baseline-01/regression-after.txt 2>&1
echo $?
git diff --binary "$(cat /tmp/corecoder-session-baseline-01/start-commit.txt)" > /tmp/corecoder-session-baseline-01/change.patch
git status --short > /tmp/corecoder-session-baseline-01/changed-files.txt
git ls-files --others --exclude-standard > /tmp/corecoder-session-baseline-01/new-files.txt
```

git diff 不包含未跟踪文件；Agent 新增的开发测试应连同整个候选目录保留。
检查它没有修改/删除起始快照中的已有测试；即使验收全绿，违反此约束仍不能记为通过。
补丁始终相对 start-commit.txt 中的起始提交生成，因此也包含 Agent 自行提交的改动。

成功标准：外部 10 个测试方法全部通过、原有回归不新增失败、已有测试未改、人工检查补丁合理。
报错未执行完、环境未准备好、超时中断，应分别记录，不能混成“修复不通过”。

## 5. 基线记录

复制填写 result-template.md。保留整个实验目录，每次重跑用新目录、同一起始快照和相同配置。
本题已经参与开发和讨论，只能作为开发基线，不作为未见题泛化成绩。
