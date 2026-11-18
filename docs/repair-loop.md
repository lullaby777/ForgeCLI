# 独立验收闭环

阅读范围：本文介绍一次性 `RepairController` 和共用的检查能力。当前持久化任务入口是 `RepairTask`；先读[项目主线与代码导览](项目主线与代码导览.md)，恢复操作见[长任务恢复](task-recovery.md)。两者已共用独立的 VerificationRunner 检查器；预算与调度逻辑仍待后续收敛。

实现位置为当前项目，五个基线副本和原始结果保持不变。本轮不调用真实模型。

## 通俗流程

Agent 像负责改代码的人，控制器负责安排轮次，验收命令像考官。
改代码的人说“好了”之后，考官实际运行本地检查。配置回归门禁后，要过两关：目标问题修好了，以及没有弄坏原来能过的检查。
先在改代码前冻结一份回归起点；每次目标验收通过后，再和这份起点比较。新增失败会反馈给同一个 Agent 继续修，全部合格才记为整体完成。
没有进展、达到上限、环境异常或用户中断时，保存结果并明确停止，不无限重试。
Agent 达到轮次上限也会检查已经落盘的代码：代码可能已经合格，只是没有最后总结。

## 使用接口

`corecoder.repair` 提供 RepairController、VerificationSpec、RegressionSpec、RepairLimits。
控制器接收现有 Agent，不另写一套 Agent 内循环；run(task) 返回 RepairResult。
Agent.chat 的原返回接口不变，只增加可选的执行边界检查。退出时恢复模型对象、轮次设置、日志路径、会话目录和原检查回调。

验证命令 argv 由调用方预先设置，shell=False，在指定 workspace 中执行；不采纳模型给出的验收命令。
验收进程设置 CORECODER_TARGET 和优先指向工作目录的 PYTHONPATH，移除已知模型密钥和 live-smoke 开关；调用方仍需确保其测试确实加载目标代码。
result_dir 必须位于工作目录之外且尚不存在，防止覆盖旧证据或把输出文件纳入代码指纹。
不使用全局 os.chdir；现有文件工具尚未合入第五题修复，因此提示 Agent 对文件工具使用绝对路径。

## 上限与停止

默认最多 3 次执行尝试，每次最多 10 个 Agent 轮次，总模型入口调用最多 30 次。
该计数包含压缩、空响应重试、子 Agent 的 chat 入口调用，不包含模型客户端内部的 HTTP 重试；不能当作严格美元预算。
默认总时间 300 秒，验收单次 30 秒，连续 2 次代码未变且失败退出码相同则停止。

总时间在调用边界协作检查：过期不启动新的模型/工具/验收。已经执行中的 API 或工具无法保证立即中断，权限交互也不能靠线程强制终止。
验收进程有实际超时；POSIX 下终止其进程组，Windows 回退终止直接进程。
这些不是容器隔离或任意代码安全边界，也没有强杀整个 Agent 的承诺。

回归起点检查、每次目标验收与回归检查共用总时间预算，每条检查命令仍受 verifier_seconds 限制；回归不额外增加模型调用预算。

主要最终状态：passed、attempt_limit、model_call_limit、time_limit、no_progress、interrupted、agent_error、
verification_timeout、verification_error、protected_inputs_changed、workspace_changed_during_verification、controller_error。
配置回归后还可能有 regression_baseline_error/timeout、regression_error/timeout；这些环境或报告异常会停止，不作为代码失败无限续跑。
执行状态和验收状态分别保存；即使执行异常，现有代码仍可能通过验收，记录中保留异常信息而不伪称执行正常。

## 验收可信度

- 固定命令退出码 0 表示通过；默认 1 表示测试失败，其他代码视为验收/环境错误，停止而不继续烧模型预算。
- exit 1 也可能包含环境问题，任意命令的语义无法自动推断，调用方可配置 failure_codes。
- 调用方负责保证命令确实执行所需测试；例如 pytest 没收集到测试的 exit 5 不会被当成通过。
- 默认冻结 workspace/tests 整个目录（包括新增文件）；开发测试应放在未冻结位置，或把 protected_paths 配成具体原有测试文件。
- 外部验收脚本应明确加入 protected_paths，绝对路径受支持。模型会被告知受保护路径，不能修改验收标准。
- 验证前后核对保护文件哈希；验收前后核对工作目录指纹，拒绝把另一份代码的结果算到当前版本。
- 仅排除 Git、虚拟环境和常见缓存目录；若验收会在源码目录生成报告文件，应把报告改放外部结果目录，避免指纹变化。
- 哈希检测是误改防护，不是对恶意 Agent 的隔离；不保证发现“修改后再恢复”等行为。
- 验收诊断保留退出码和末尾最多 4000 字节输出，模型收到的恢复反馈按下述阶段分流，不传参考补丁。固定命令和完整输出保存在结果目录；输出可能包含测试数据，应按本地项目资料管理。

### 回归门禁

`RepairController(..., regression=RegressionSpec(argv=(python, "-m", "pytest", "tests", "-q", "--ignore=tests/test_live_smoke.py")))`
启用第二道检查。命令由可信调用方固定，必须支持 pytest 的 `--junitxml` 参数；控制器自动把报告放在结果目录，不接收模型提供的测试命令或失败豁免清单。需要本地环境安装 pytest。

开始调用模型前先运行一次回归，冻结各测试的标识与状态。原有失败按 JUnit 的 classname/name 识别，后续不再扩大清单。
目标验收未过时先修目标，不重复跑全量回归；目标验收通过后才跑回归。已有失败仍存在可以通过门禁；新增失败、原有测试消失或从执行变成跳过均不能通过。
新增失败反馈包含用例名和简短错误证据，回归完整输出与 XML 另行保存。修复后重新跑目标验收，避免修旧功能时把目标修复撤回。

报告缺失、格式错误、重复测试标识、没有实际执行测试、退出码与报告不一致等视为检查异常；启动失败、收集错误或超时也不会算成通过。
默认保护回归 tests 目录，并自动保护根目录 pyproject.toml、pytest.ini、conftest.py、setup.cfg、tox.ini（包括原先不存在的文件），防止改收集配置掩盖失败。外部测试文件也应明确列入 protected_paths。
如需允许新增开发测试，可将回归 protected_paths 配成具体原有文件；调用方仍负责固定测试范围和所有影响验收的输入。

为避免继承终端里的筛选条件或第三方插件，检查进程清空 PYTEST_ADDOPTS 并关闭插件自动加载；确需插件时由可信 argv 显式启用。
这不是对任意测试的正确性证明：既有失败按用例标识识别，不能自动判断同一失败用例的内部失败原因是否改变。

结果分别保存 acceptance_status、regression_status、completion_scope、completed。只有已配置回归且两关通过、保护检查合格时 completed=true。
为了兼容旧调用，不配 regression 的配置仍可返回 status=passed，但 scope=acceptance_only、regression_status=not_configured、completed=false，明确只通过了目标验收。
后续对照实验必须配置回归并按 completed 判断整体完成，不能仅依赖旧 status 字段。

### 阶段化恢复反馈

当前项目已增加恢复分流，v1/v2 冻结实验不更新：

- 输出截断且文件内容/路径未变化：明确告知 output_limit、没有修改落盘，要求基于已有读取证据做一个最小工具动作，不重复完整分析；不重复塞入旧断言堆栈。
- 文件有变化但验收失败：列出有变化的文件、退出码，以及最多 600 字符的关键错误摘要，要求针对当前补丁做小范围修正。
- 目标验收通过但回归失败：进入 regression_failed，明确保留目标修复并处理新增失败，不得更改保护测试或扩大原有失败清单。
- 无修改但不是截断：明确指出尚未产生文件修改，给少量失败证据并要求实际推进。
- 验收启动错误/超时：记录 verification_unavailable，保持停止，不当作代码错误提交给模型继续。

完整输出与末尾 4000 字节诊断仍存 result.json/verification-N.txt；发给下一轮模型的是分流后的短反馈，通常不再是这段原始尾部。
原任务完整保留，不为追求短消息而删掉用户约束。反馈不包含参考补丁或模型内部推理。

每次尝试的 facts 保存执行状态、结束原因、当前与验收输入指纹、变化文件数量和最多 8 个文件名、
当前会话历史可确认的最多 8 个成功 read_file 请求。失败/被拒绝/没有配对结果的读取不计入。
读取记录只说明过去返回过读取结果，不代表当前版本已验证；相对路径请求明确标注且不臆断实际位置，已知的绝对工作区外路径不汇总。
这些 facts 是当前执行中的最小事实记录，不是完整文件系统快照；一次性 RepairController 不提供重启恢复，持久化任务恢复由 tasks.py 的 RepairTask 实现。
facts/recovery 会写入 result.json；feedback_submitted_to_agent 表示提示已交给下一次 Agent.chat，不保证已发生上游 API 请求。

无进展判定仍使用原来的工作区指纹、失败状态和退出码；读取更多文件或生成恢复提示不会重置该计数。
同样失败连续两次且无变化仍然停止；模型调用、尝试次数和时间上限保持原设置。
新增回归见 tests/test_recovery_feedback.py，使用固定响应、本地文件和本地验收命令，不调用真实模型。

## 结果文件

- result.json：配置、最终状态、总调用数、每轮 Agent 状态和代码指纹、验收状态、退出码、反馈与日志位置。
- regression-baseline.txt/xml：改代码前的完整回归输出及冻结的起点报告；结构化失败清单同时保存在 result.json。
- regression-N.txt/xml：第 N 次目标验收通过后的完整回归结果；每轮记录新增失败、仍存在的原有失败和消失/被跳过的测试。
- verification-N.txt：完整合并 stdout/stderr，超大输出不放进模型上下文。
- agent-events.jsonl：沿用 ISSUE-001 元数据事件；子 Agent 事件用不同 run_id 区分。
- 一次性 RepairController 在每轮失败后与结束时原子写入结果，仅有结果文件不足以恢复任务；启用 RepairTask 检查点后，恢复行为与边界见长任务恢复说明。

## 完全离线演示

```sh
python examples/repair_demo.py /tmp/corecoder-repair-demo-01
```

目标目录必须不存在。脚本模型第一轮写 wrong，验收失败；收到反馈后第二轮写 fixed，验收通过。
请使用安装了当前项目的环境；本次验证环境为 `/tmp/corecoder-issue001/venv/bin/python`，五个基线环境仍安装旧副本，不应拿来验证新入口。
真实执行 WriteFileTool 和独立本地 Python 验收，不创建模型客户端，不读写真实用户撤销历史。
脚本响应是固定的，只证明编排机制工作，不证明真实模型修复质量提升。

带回归门禁的离线演示：

```sh
python examples/regression_repair_demo.py /tmp/corecoder-regression-demo-01
```

固定响应先修好 answer.txt 却弄坏 legacy.txt；目标验收通过、回归失败后继续执行，恢复 legacy.txt，两关同时通过。
它使用真实本地工具、独立验收与 pytest 子进程，不创建真实模型客户端；撤销记录也隔离在演示目录。

## 后续手动使用 CLI（会调用真实模型，本轮未执行）

创建位于工作目录外的 JSON，例如：

```json
{
  "workspace": "/tmp/my-repair-workspace",
  "result_dir": "/tmp/my-repair-result-01",
  "verification": {
    "argv": ["/absolute/path/to/python", "/tmp/acceptance/check.py"],
    "protected_paths": ["tests", "/tmp/acceptance/check.py"]
  },
  "regression": {
    "argv": ["/absolute/path/to/python", "-m", "pytest", "tests", "-q", "--ignore=tests/test_live_smoke.py"],
    "protected_paths": ["tests"]
  },
  "limits": {"max_attempts": 3, "max_model_calls": 30, "rounds_per_attempt": 10}
}
```

```sh
corecoder --repair-config /tmp/repair.json -p '修复指定问题' --yes
```

workspace/result_dir 相对路径以配置文件所在目录为准，验收命令的相对文件参数以 workspace 为准。
配置文件自动加入保护路径。--yes 沿用原有自动授权语义，必须使用自己准备的工作副本；独立目录不是安全沙箱。
CLI 打印最终验收状态和结果位置，通过返回 0，其他状态返回 1。
为保持原始基线，后续真实模型实验应新建改进版实验副本，不复用或覆盖五个原始副本。

## 独立检查器

`corecoder.verification.VerificationRunner` 提供 `verify(label, timeout)`、`regress(label, timeout, baseline=None)`。它只执行固定本地检查并返回结果，报告文件名和结果字段沿用原格式。
RepairController 保留原 `_verify/_regress/_run_check` 兼容适配层；RepairTask 直接调用公开检查器。目标或回归失败后是否重试、何时保存基线和检查点，仍由调度器决定。
