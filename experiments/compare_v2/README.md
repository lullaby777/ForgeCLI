# 第四题 v2：只提高单次输出上限

execution 是执行器的停止/控制状态，不是验收成绩。原版 round_limit 表示达到 30 轮，
已有补丁仍可能通过最终检查。原版 returned_empty 表示空回复结束；改进版 passed 是控制器确认验收通过，
内部执行仍可能因调用上限停止。应始终同时看 functional_passed 和明细中的 Agent 状态。

## 本轮变化

新目录 `/tmp/corecoder-compare-02`，仅有 task04-original、task04-improved 两组。
不复制上一轮已执行的 workspace，而从 v1 原始题目快照重新开始；runtime 直接复制 v1 冻结版本，不使用当前项目的新代码。
任务文本、验收、依赖、Python 和原有失败豁免不变。

唯一有意改变的配置是 model.max_tokens 从 4096 到 8192，两组同步变化。
模型 deepseek-flash、接口、温度 0、上下文 128000、单请求超时 60 秒、总模型入口调用 30 次、
执行进程上限 900 秒、中途验收 60 秒及最终回归 120 秒等保持原设置。
原版单次 Agent.chat，改进版最多 3 次尝试共享这 30 次调用，连续 2 次无进展停止。
目录和 Git 提交标识不同属于新副本元数据，不是额外算法改动。测量代码仅适配新根目录并移除第二题选项。
每次输出上限提高意味着潜在 token 消耗可能增加：不能称 v1/v2 为相同 token 预算，也不保证第四题一定修好。
本轮不修改截断恢复提示，不增加任务记忆，避免同时改变多个变量。

## 手动启动（以下会调用真实模型，准备阶段未执行）

在终端配置有效密钥，不要截图密钥：

```sh
export CORECODER_API_KEY='你自己的有效密钥'
```

使用新一轮的统一环境和启动器，逐条执行，完成一组再运行另一组。顺序采用改进版先运行，记录这一顺序；不能据此消除服务波动。

```sh
/tmp/corecoder-compare-02/venv/bin/python /tmp/corecoder-compare-02/runner/run.py task04 improved --live
/tmp/corecoder-compare-02/venv/bin/python /tmp/corecoder-compare-02/runner/run.py task04 original --live
```

首次运行不追加人工提示，不人工修改文件。缺密钥不会占用案例；启动后每组只运行一次，拒绝覆盖旧 run。
模型只修改 workspace，执行器、配置和外部验收保持冻结。目录隔离不是安全沙箱。
所有模型参数由 case.json 读取，不接受环境变量逐组覆盖。30 次入口数不包含内核内部 HTTP 重试，不是美元硬预算。

## 查看汇总（不调用模型）

```sh
/tmp/corecoder-compare-02/venv/bin/python /tmp/corecoder-compare-02/runner/summary.py
```

结果在每组 run/summary.json，包含功能验收、执行状态、调用、reported token、耗时和日志位置。
完整记录会话、模型事件、验收/回归、改进版中途验证以及补丁。保留 workspace，因为 Git 补丁不含新建的未跟踪测试。
分析时先比较 v2 内原版/改进版，再描述 v1/v2 差异；两轮都仅单次，不能宣称稳定提升。
重点看第四题是否开始实际修改、是否还触发 length、是否通过验收，以及新增输出消耗是否换来进展。

## 离线检查

正式两组在准备阶段均未运行。验收应都为 14 项中 9 个失败；原有测试仍应 352 通过、1 个已知 README 行数失败。
合成临时工作区检查两套启动/评分路径和规范化首个请求一致性，不修复正式题目、不构造真实模型客户端。
原五个基线、v1 四组副本与原始结果在准备前后按文件哈希核对，不修改或覆盖。
