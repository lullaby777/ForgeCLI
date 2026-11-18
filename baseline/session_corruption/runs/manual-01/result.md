# 手动基线 01：会话损坏容错

结论：本题开发基线通过。外部验收通过；原有测试没有新增失败；未修改已有测试。

## 已核对证据

- 起始快照：736c677a0a7b68be1adcd9ea0b6ccaa2f4d6e1a1。
- 运行方式：用户交互运行原版 CoreCoder；不是 run_baseline.py 自动记录。
- 修复前原有测试：352 passed，1 failed；真实模型测试排除。
- 修复后独立复核：376 passed，1 failed（15.35 秒为复核测试耗时，不是 Agent 耗时）；真实模型测试排除。
- 唯一失败仍是 test_readme_line_counts_are_current；没有新增失败。
- 外部验收：10 个方法全部通过（用户保存的 acceptance-after.txt）。
- 已有测试未改；新增 tests/test_session_tolerance.py，包含 24 个参数化后测试项。
- 修改 corecoder/session.py：统一结构校验，坏文件跳过，保留有效历史。
- 同步 article/06-session-and-cli.md 中 session.py 行数 97 → 155；不是修改测试降低标准。
- 输入 token 182694、输出 token 7236、合计 189930：来自用户截图中的 /tokens，并非独立计费核对。

## 未记录项

模型/服务商、完整会话、Agent 耗时、真实费用、是否追加提示暂未提供，不能补猜。
如需严格无干预标签，需由用户确认过程中未追加引导、未人工改代码。

## 保存材料

change.patch 是对起始快照的已跟踪文件补丁；新增测试单独保存在 test_session_tolerance.py.snapshot。
修复前后日志和依赖版本同时保存。原实验目录仍在 /tmp/corecoder-session-baseline-01。

本题已用于开发和讨论，只是单题单次成功，不能推断一般修复成功率，也不证明需要或不需要新 harness。
