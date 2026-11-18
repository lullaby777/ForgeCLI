# 第四题 v3：通用反馈 vs 阶段化恢复反馈

本轮准备不调用真实模型，不修改旧基线、v1/v2 副本或结果。
上一轮已在当前项目修改控制器：新增阶段判断与执行事实，不是简单增加提示长度。

## 这次只比较什么

新目录 `/tmp/corecoder-compare-03`：

| 组别 | 停止后如何反馈 |
|---|---|
| task04-generic | 使用 v2 原来的“原任务 + 通用验收失败 + 输出尾部”模板 |
| task04-staged | 截断且无修改时要求最小下一动作；已修改但失败时提供短错误摘要与执行事实 |

两组都有 ISSUE-001 防护、独立验收、事实记录、有限续跑和同样的无进展规则。
generic 不是最早的原版 CoreCoder，也不是复制 v2 最后修过的工作区。
两份 runtime 字节完全相同，统一包含已验证的阶段化控制器。在各自独立进程中，generic 只替换反馈构造函数为旧模板。
因此 v3 组间处理差异是恢复反馈内容，读取事实与记录结果的开销对两组相同。
相对于 v2，公共记录层增加了执行事实；v3 不用 v2 的历史单次运行直接冒充当前对照组。

统一条件：相同第四题修复前 workspace、相同 task.md 和验收；deepseek-flash、温度 0、输出 8192、上下文 128000、请求超时 60 秒。
每轮最多 30 次模型入口调用，总计也最多 30 次；最多 3 次尝试共用此预算。
执行进程 900 秒，中途验收 60 秒，连续 2 次无进展停止，最后回归 120 秒；SDK 自动重试关闭。
30 次入口数不含内核内部 HTTP 重试，不是美元硬预算。一次模型响应上限 8192 不保证模型一定会产生工具动作。

## 手动运行（以下会调用真实模型，准备阶段未执行）

在终端设置有效密钥，不要把密钥截图或写入任务：

```sh
export CORECODER_API_KEY='你自己的有效密钥'
```

逐条运行，每组完成后再启动下一组；本轮先通用反馈组、后阶段化组，记录这个次序，不声称消除服务波动。

```sh
/tmp/corecoder-compare-03/venv/bin/python /tmp/corecoder-compare-03/runner/run.py task04 generic --live
/tmp/corecoder-compare-03/venv/bin/python /tmp/corecoder-compare-03/runner/run.py task04 staged --live
```

只运行一次，不追加人工提示、不人工修改代码，不进入旧实验 CLI 续跑。
启动器自动限额、记录、独立评分；没有 --live 不启动模型，缺密钥不占用案例，已有 run 目录拒绝覆盖。
工具自动授权沿用前两轮设置，案例工作区不是安全沙箱，请仅运行你授权的本地修复任务。

## 汇总（不调用模型）

```sh
/tmp/corecoder-compare-03/venv/bin/python /tmp/corecoder-compare-03/runner/summary.py
```

run/summary.json 保存最终功能通过与执行状态；worker.json 和 controller/result.json 保存每次尝试、facts、反馈种类与字符数、是否提交给下一轮 Agent。
同时记录模型调用、reported token、耗时、日志、原有测试保护、最终回归、补丁和 Git 状态。
完整 workspace 需保留，Git diff 不包含新建未跟踪测试。
两组的 execution 都表示同类控制器终态，底层 Agent 停止原因另看 attempts[].agent_status。

重点观察：截断后能否开始实际修改、是否仍连续 length、是否通过验收，以及额外调用/token/时间是否值得。
首个请求去除案例路径后应一致；反馈后的请求应体现策略差异。只做两组各一次，不能宣称稳定成功率提升。

## 离线检查

正式两组均从修复前开始，预期 14 项验收仍有 9 个失败；共同起点原有测试为 352 通过、1 个已知 README 行数失败。
合成临时工作区使用脚本响应模拟重复 length，两边都必须按无进展停止、不能自动修改文件。
检查通用组严格使用旧模板，阶段组进入 truncated_without_changes，首个请求相同、恢复请求不同，事实两组都记录。
这些离线检查不消耗正式案例，也不证明真实模型会遵循新的提示。
