# 第二题：编辑后换行格式改变

独立副本：`/tmp/corecoder-newline-baseline-01/candidate`

独立环境：`/tmp/corecoder-newline-baseline-01/venv`

本题从第一题修复前的相同源码快照开始，不包含会话容错修复。
准备阶段只添加外部材料，没有修改候选源码，也没有调用模型。

## 题目范围

CRLF 是 Windows 常见换行，LF 是另一种常见换行。当前 edit_file 把 CRLF 文件读成普通文本再写回，
导致只替换一个单词也把全部换行改成 LF。验收以原始字节比较，不以看起来一样作为通过。

第一版仅验收统一 CRLF/LF 的 UTF-8 文件，不涉及混合换行、纯 CR 换行或其他编码。
CRLF 文件允许 old_string 使用 LF 或 CRLF 多行文本，替换内容沿用文件换行风格。
未编辑内容、空行和末尾换行状态必须保留。
兼容性检查包括 LF、多行、中文、唯一匹配、非 UTF-8 拒绝、重启后撤销和差异输出。

## 已准备的文件

- reproduce.py：在临时目录展示修改前后字节，不碰个人文件或真实撤销记录。
- test_acceptance.py：12 个外部验收测试方法，有些包含多个子用例。
- task.md：交给 Agent 的固定任务说明，不提供实现方案。
- check.py：运行复现、外部验收和原有测试，不调用 Agent。

## 手动运行（此步会调用模型）

开一个终端：

```sh
cd /tmp/corecoder-newline-baseline-01/candidate
source /tmp/corecoder-newline-baseline-01/venv/bin/activate
```

沿用第一题的模型和服务商，通过终端环境变量设置 CORECODER_MODEL、CORECODER_API_KEY、
CORECODER_BASE_URL。使用默认 OpenAI 地址时不需要自定义 BASE_URL；别把密钥写进任务或日志。
源码副本未复制 .env。先设置好模型变量，再启动终端记录：

```sh
script -q /tmp/corecoder-newline-baseline-01/manual-terminal.txt env HOME=/tmp/corecoder-newline-baseline-01/agent-home /tmp/corecoder-newline-baseline-01/venv/bin/python -m corecoder
```

上面的命令适用于 macOS：仍然是原版 CLI，但把个人 hooks、MCP、权限和撤销等用户目录隔离，
并记录终端过程。独立目录不是安全沙箱；检查其工具请求后允许。

另开一个终端，把固定题目复制到剪贴板：

```sh
pbcopy < /Users/qiaoya/Desktop/CoreCoder/baseline/newline_preservation/task.md
```

回到 CoreCoder 粘贴并提交，先不追加指导、不人工修改代码。
Agent 完成后依次输入 `/tokens`、`/save`、`quit`，终端记录中会保留消耗；
记录模型名、开始结束时间，未知费用不要填成零。/save 会话在隔离 agent-home 中。

## 修复后外部验收（不调用模型）

退出 CoreCoder 后执行：

```sh
/tmp/corecoder-newline-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/newline_preservation/check.py after
tail -8 /tmp/corecoder-newline-baseline-01/acceptance-after.txt
tail -8 /tmp/corecoder-newline-baseline-01/regression-after.txt
```

程序将生成 reproduction-after.txt、acceptance-after.txt、regression-after.txt 及 checks-after.json。
它拒绝覆盖已有日志。验收需要全部通过，原有测试不得新增失败，已有 README 行数失败单独记录。
本机 HTTP 测试需要本机端口权限；真实模型测试始终明确排除，测试用户目录隔离。
如果修复改变文件行数，允许同步对应文档引用，但不允许改测试降低标准。
保留整个 candidate（包含新测试），修复完成后再检查补丁和记录结果，不把本题修改带到第三题。

## 单独查看复现

```sh
CORECODER_TARGET=/tmp/corecoder-newline-baseline-01/candidate /tmp/corecoder-newline-baseline-01/venv/bin/python /Users/qiaoya/Desktop/CoreCoder/baseline/newline_preservation/reproduce.py
```

复现脚本退出码 0 只表示执行完成；看字节是否相等或外部验收结果才知道修好没有。
修复前检查输出和起始源码清单位于实验目录；准备结果另行归档到本目录 observed 下。
