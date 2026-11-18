# SWE-bench 评测与故障注入

本目录用 [SWE-bench Lite](https://www.swebench.com) 在真实 GitHub issue 上评估三件事：

1. **改进版是否比原版更会修 bug**（通过率、成本、失败模式）；
2. **修复验证闭环的判断是否可信**（内部验证结果 vs 官方评测结果）；
3. **长任务 Checkpoint 能否在进程被强杀后正确恢复**（故障注入）。

所有数字都来自 `results/` 下的原始报告，可用本文末尾的命令复现。

## 评测方法

### 选题：固定规则 + gold patch 校验

- `select_instances.py`：取 SWE-bench Lite 测试集中 `pytest-dev/pytest`、`psf/requests`、`pallets/flask` 三个仓库的**全部** 26 道题。规则固定，不挑题。
- `gold_check.sh` + `filter_gold.py`：先用官方标准答案（gold patch）跑一遍官方评测。标准答案都过不了的题说明是环境问题（例如 requests 的测试要访问 httpbin.org），予以剔除。最终保留 **20 道**（pytest 16、flask 3、requests 1），名单见 `results/selected_ids.txt` 与 `results/dropped_ids.json`。

### Harness：Agent 在宿主机运行，命令在题目容器里执行

`run_instance.py` 对每道题、每个版本：

1. 从官方镜像中复制出该题的 `/testbed`，再以**同一路径**挂载回容器，保证 Agent 看到的绝对路径在宿主机和容器内一致；
2. 启动容器时使用 `--network none`（原因见下文「评测污染」）；
3. 把 Agent 的 `bash` 工具替换为 `docker exec`，进入题目自带的 conda 环境执行；文件读写工具直接作用于挂载目录；
4. 禁用 `fetch_url`（防止上网查答案）和子 Agent 工具（让对比更干净）；
5. 所有版本使用相同的模型（`deepseek-flash`，temperature 0）、每题最多 40 次模型调用、25 分钟；
6. 每次模型请求/响应和每条命令都写入 `trace.jsonl`，用于审计和复盘；
7. 结束后用 `git diff` 导出补丁，交给**官方评测**（`swebench==4.1.0`）判分。Agent 全程看不到隐藏测试。

`run_batch.py` 负责并行运行和打分，`report.py` 生成对比表。

### 对比的版本（`arms/`，冻结的源码快照）

| 版本 | 内容 |
| --- | --- |
| `original` | 上游提交 `3a7d484`，单次 `agent.chat` 直到模型停止 |
| `improved` | 先写复现测试并确认它在原代码上失败，然后冻结；再交给 `RepairController`，以「复现测试 + 回归测试」双门禁验证，失败时给出结构化反馈继续修 |
| `improved_v2` | 在 `improved` 基础上，Agent 第一次改动受保护文件时**原样恢复并提醒**，而不是直接终止（`RepairLimits.max_protected_violations`） |
| `improved_v3` | 在 `v2` 基础上，`RepairTask` 支持在调用方保证沙箱已终止时恢复「工具执行中被中断」的任务 |

复现测试与回归测试清单都放在 `swe_repro/` 下，导出补丁时排除；验证命令由 Harness 构造，模型无法修改。

## 结果

### 评测污染：原版一度 100% 通过

第一轮（`r1`）容器可以联网，原版 20/20 全部通过。审计 trace 后发现，原版在 **12/20** 道题里执行了 `pip download pytest==6.2.5` 之类的命令，下载**修复之后的上游版本**再照抄（示例：`results/trace_examples/r1_original_pytest-7432_pip_download_leak.jsonl`）。加上 `--network none` 后，原版回到真实水平。r1 的结果只作为污染证据保留，不参与比较。

### 通过率（断网后，`r2`、`r3` 两轮）

| 版本 | r2 | r3 | 平均 tokens / 题（r3） | 平均耗时 / 题（r3） |
| --- | --- | --- | --- | --- |
| original | 12/20 | 10/20 | 400,511 | 80 s |
| improved | 11/20 | 11/20 | 659,118 | 184 s |
| improved_v2 | — | 12/20 | 450,583 | 123 s |

**结论：在这个样本量下通过率没有可测的差异。** 原版两轮之间就相差 2 题；20 道题里 7 道所有版本每次都能做对、5 道都做不对，只有 8 道存在差异空间。`v2` 相比 `improved` 在同一轮里 token 少 32%、耗时少 33%、空补丁从 5 个降到 2 个。

### 修复验证闭环的判断是否可信

用官方结果检验内部验证器（r2 + r3 中三组带验证器的运行，共 60 次）：

| 内部验证结果 | 次数 | 官方实际通过 | 比例 |
| --- | --- | --- | --- |
| 通过（复现 + 回归） | 36 | 26 | 72% |
| 未通过或未走完 | 24 | 8 | 33% |

验证器能把「大概率正确」和「不可靠」的补丁分开，可以作为提交前的置信度信号。主要误判来源：复现测试由同一个模型编写，测试太弱时错误补丁也能通过。

### 故障注入：Checkpoint 能否扛住 `kill -9`

`fault_inject.py` 对每道题做两次独立运行：

- **nofault**：正常跑完，作为对照，也用于估算「没有 Checkpoint、只能重跑」的成本；
- **kill**：进入修复阶段后，在随机时刻（第 1–6 次模型响应之后，再随机等 0–8 秒；种子固定）对整个进程组发送 `SIGKILL`，然后在**新进程**中恢复。恢复前重启容器，确保被杀进程留下的命令不再运行。

`fault_report.py` 统计中断时 Checkpoint 记录的状态、恢复结果、恢复后携带的历史消息数和 token 消耗。

**f1（`improved_v2`，15 道题，11 次命中修复阶段）**

| 中断时正在做什么 | 次数 | 恢复结果 |
| --- | --- | --- |
| 等待模型响应 | 7 | 全部恢复并跑完 |
| 两步之间 | 1 | 恢复并跑完 |
| 执行工具 | 3 | 按设计拒绝恢复（`tools_outcome_unknown`） |

可恢复的 8 次全部续跑完成，恢复后的首次模型请求携带 32–54 条历史消息。中断 + 恢复共消耗 4.63M tokens，「已花费 + 从头重跑」估计为 6.46M，**节省 28%**。

**f2（`improved_v3`，8 道题，专门在工具执行时强杀）**

| 结果 | 次数 |
| --- | --- |
| 恢复并跑完 | 5 |
| 拒绝恢复：Agent 在中断前已篡改受保护文件（复现测试 / 回归清单） | 2 |
| 进程在强杀点前已结束 | 1 |

工具执行中断的可恢复率从 f1 的 0/3 变为 5/5；被拒绝的 2 次是 Agent 自己违规，即使不中断，下一次验证也会被拦下。这一轮中断发生得早（修复阶段第 1–5 次调用），能节省的 token 很少（1.8%）：任务进行得越久，恢复越划算。

## 局限

- 只有 20 道题、每版本 1–2 轮，且只用一个模型；通过率差异需要更大样本才能下结论。
- 官方镜像是 x86 的，在 Apple 芯片上通过模拟运行。
- 「从头重跑」的成本用同题的独立正常运行估算，不是同一次运行的反事实。
- 故障注入覆盖了模型调用、步骤间和工具执行三种中断位置，没有覆盖验证执行中的中断（目前按设计拒绝恢复）。

## 复现

```bash
conda create -n swe python=3.11 -y && conda activate swe
pip install -r requirements.txt
export DEEPSEEK_API_KEY=...

python select_instances.py && bash gold_check.sh && python filter_gold.py   # 选题 + 环境校验
caffeinate -i python run_batch.py --run-id r2 --workers 3                   # 原版 vs 改进版
python run_batch.py --run-id r3 --arms original improved improved_v2 --workers 3
python report.py --run-id r3

caffeinate -i python fault_inject.py --run-id f1                            # 随机时刻强杀
python fault_report.py --run-id f1
caffeinate -i python fault_inject.py --run-id f2 --arm improved_v3 --target tools --modes kill \
  --ids pallets__flask-4992 pytest-dev__pytest-7432 pytest-dev__pytest-9359 pytest-dev__pytest-7490 \
        pytest-dev__pytest-8906 pytest-dev__pytest-7373 pytest-dev__pytest-5692 pytest-dev__pytest-5103
python fault_report.py --run-id f2 --arm improved_v3 --control f1
```

Docker Desktop 建议分配 8GB 以上内存，否则构建环境镜像时 conda 会被系统杀掉（退出码 137）。运行产物写入 `runs/` 和 `logs/`（已在 `.gitignore` 中排除），汇总结果复制到 `results/`。
