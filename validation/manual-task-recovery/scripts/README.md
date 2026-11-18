# 长任务暂停恢复：一次手动验证

目录 `/tmp/corecoder-task-manual-01`。新工作区是第四题原始未修复代码，不含以前模型生成的修复。
运行时冻结当前主项目的检查点实现；会话和历史使用本进程独立 HOME，不影响以前的会话或实验。
这是一次暂停/恢复机制验证，不是新的模型性能对照。

## 准备阶段已做什么

独立 Python 环境使用与既有实验一致的依赖固定版本，离线复制安装。
任务配置采用 deepseek-flash、温度 0、输出 8192、上下文配置 128000、请求超时 60 秒、SDK 自动重试关闭。
任务最多 30 次模型入口调用、3 次尝试、每次 10 轮、总执行 900 秒、检查单次 60 秒，连续两次无进展停止。
原始候选验收应为 14 项中 9 项失败，回归 352 通过、1 个已知 README 行数失败。允许新增开发测试，已有测试及验收被冻结。
正式任务目录尚未创建。离线模拟只在临时副本中进行，没有调用真实模型。

## 操作一：启动并开始任务

终端中准备有效 CORECODER_API_KEY，不把密钥写到任务配置或截图。运行（从这里起可能调用真实模型）：

```sh
/tmp/corecoder-task-manual-01/venv/bin/python /tmp/corecoder-task-manual-01/scripts/launch.py
```

进入 `You >` 后粘贴：

```text
/repair /tmp/corecoder-task-manual-01/task.json
```

它先打印关联会话 ID、运行原始回归，再进入模型执行；初始检查可能安静运行十几秒。
记下打印的 `corecoder -r session_...`，也可以稍后由查看脚本自动找到同一个会话。
这次验证沿用 --yes 自动授权工具，仅操作指定独立工作区。

## 操作二：在明确的安全点 Ctrl+C

第一次模型响应及工具结果都保存完后，屏幕会出现：

> 【安全暂停窗口】响应和工具结果已经保存，现在没有在途操作。
> 请现在按一次 Ctrl+C；回到 You > 后输入 /task 查看，再输入 quit 退出。

此时按一次 Ctrl+C，不要回车，不要连续按两次。这个窗口专门用于手动验证，等待不发送请求或执行工具，但等待时间计入任务运行时间，请及时按键。
回到 `You >` 输入 `/task`，应看到 paused、已用调用不为零。再输入 `quit`，退出程序。
不要在工具执行中随意抢按，否则可能按设计进入“工具结果不明”而阻止自动恢复。

退出后在终端记录暂停快照（不调用模型）：

```sh
/tmp/corecoder-task-manual-01/venv/bin/python /tmp/corecoder-task-manual-01/scripts/check.py --snapshot paused
```

脚本核对原始失败只有 README，总预算未超限，当前没有进行中的操作；把任务 ID、账本、基线摘要和基线文件 SHA256 保存为 records/paused.json。

## 操作三：恢复原会话，先核对预算尚未变化

```sh
/tmp/corecoder-task-manual-01/venv/bin/python /tmp/corecoder-task-manual-01/scripts/launch.py --resume-last
```

这里不是任意选择“最新聊天”：脚本查找与本任务目录绑定的唯一会话，找不到或不唯一就拒绝。
也可显式选择前面记下的 ID：`launch.py --resume session_...`，继续使用同一个 Python 路径。
进入 `You >` 后先不要输入“继续任务”，可以先输入 `/task` 查看；打开会话本身不发起模型请求。

保持这个窗口开着，在第二个终端执行：

```sh
/tmp/corecoder-task-manual-01/venv/bin/python /tmp/corecoder-task-manual-01/scripts/check.py --snapshot restored
```

应显示 comparison_to_paused=passed：任务 ID、整个账本、原始失败清单、基线文件内容哈希都与暂停时完全一致。
如果核对失败，先保留现场，不手工改检查点、预算或基线。

## 操作四：输入“继续任务”

回到 `You >`，输入：

```text
继续任务
```

程序读取原始任务检查点，先验证已落盘代码，再使用剩余预算继续。本次不会再次打开安全暂停窗口。
它不重新创建任务、不把失败现场当成新的原始基线。两道检查通过才 completed=true；也可能因预算、无进展或环境问题正常停止，均记录原因。
回到 `You >` 后输入 `/task` 查看，再输入 quit。

## 操作五：记录结束结果

```sh
/tmp/corecoder-task-manual-01/venv/bin/python /tmp/corecoder-task-manual-01/scripts/check.py --snapshot finished
```

检查脚本核对任务 ID 和基线不变，已用调用/尝试/时间没有减少且不超预算。同一次尝试的轮数不能倒退；进入下一次尝试时轮数可以按设计重新从该次的零开始。
它显示最终 completed、验收、回归、新增失败和停止原因。原有 README 失败仍可存在，不算新增失败。

三个 snapshot 名称只写一次，不覆盖旧证据。随时只查看、不保存用 `check.py` 不带参数。
记录位置：task/state.json、task/baseline.json、records/paused.json、records/restored.json、records/finished.json，以及 home/.corecoder/sessions/ 原会话链接。
模型 API 密钥由环境提供，查看脚本不需要密钥。

## 辅助窗口的实现边界

launch.py 仅在这个新启动器中包裹 RepairTask._hook：先执行正式保存，再在首次安全回调等待 Ctrl+C。
没有改动主项目或旧运行时源代码，没有在在途模型/工具上截断，没有给模型添加暂停提示。
窗口只触发一次；若首个请求在返回前报错或任务提前停止，窗口可能不出现，请保留现有记录，不重置预算追跑。
这验证安全节点的恢复。工具执行中断或硬退出的未知结果保护另由自动测试覆盖，不能用本次安全窗口证明所有崩溃都能自动恢复。
