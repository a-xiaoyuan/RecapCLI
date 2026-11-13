# RecapCLI 评测

在固定的目标仓库上跑真实任务，比较不同版本的引擎。

- **引擎**是当前检出的代码。
- **目标**是 `tasks.json` 里固定的提交。工作区从这个提交导出，里面没有 git 历史，也没有 `evals/`，模型看不到验收脚本。

这样换引擎版本时题目不变。如果让 Agent 改当前代码，已经修过的题就没有意义了；`prepare` 发现任务在模型开始前就已通过时，会直接报错。

## 运行

```sh
python -X utf8 evals/runner.py prepare              # 准备工作区并预检，不调用模型
python -X utf8 evals/runner.py run --real-model     # 调用真实模型，会产生费用
python -X utf8 evals/runner.py run --real-model --case bugfix-1
python -X utf8 evals/runner.py report               # 汇总并和基线对比，不调用模型
python -X utf8 evals/runner.py recheck              # 用新版验收离线重评已有产物，另存结果
```

工作区、模型回答和报告都写到 `evals/runs/`，不入库。

### 解释器

评测进程和模型跑测试必须用**没有安装 recapcli 本体**的解释器。目标仓库的 `project_version()` 会优先读 `importlib.metadata`。如果解释器里装着引擎的 recapcli（例如 0.7.0），而目标工作区是更早的版本（例如 0.6.0），`tests.integration.test_package` 会在未改动的代码上失败，模型可能因此撤回一次有效修改。

不改引擎里写给模型的 `sys.executable`：启动评测的解释器就是模型看到的解释器。依赖照常安装，只是不把本仓库装进该环境；引擎源码靠 `PYTHONPATH` 和 `evals/runner.py` 开头的路径插入来导入。

```sh
uv sync --no-install-project
PYTHONPATH="$PWD" .venv/bin/python -X utf8 evals/runner.py prepare
```

`prepare` 发现当前解释器已经安装了 recapcli、而目标工作区也是这个项目时，会直接拒绝。它还会用同一解释器在单独的 `precheck-selftest` 里跑一遍目标仓库自带测试，把修改前就失败的用例记进 `precheck.json` 的 `_workspace_tests`。报告会注明这些失败不是本次模型改动引入的。验收预检仍用原来的 `precheck-workspace`，两份互不覆盖。

### 扩展套件

扩展套件保留原来三题，再加入四道来自仓库历史需求的工程题。七道题各跑两次，共十四例。目标提交和单例预算与原套件一致。

| 新题 | 独立验收的场景 |
| --- | --- |
| `outage-after-tool` | 编辑后遇到网关故障或断连，以未完成收尾，保留修改且不重复执行 |
| `verification-gate` | 验证命令失败后仍约束结束，修复后可正常返回，只读回合跳过验证 |
| `skill-loading` | 提示词只列目录，正文通过工具按需返回，同名优先级及损坏文件处理 |
| `mcp-streamable` | 本机 HTTP 服务验证会话头、JSON 和 SSE、远程写审批及故障不重发 |

```sh
python -X utf8 evals/runner.py prepare --suite expanded
python -X utf8 evals/runner.py run --real-model --run-dir RUN_DIR --case outage-after-tool-1
python -X utf8 evals/runner.py report --run-dir RUN_DIR
python -X utf8 evals/runner.py recheck --run-dir RUN_DIR
```

`RUN_DIR` 使用准备时输出的路径。扩展套件写入 `evals/runs/expanded/`，报告和重评读取该目录下的运行清单并选择对应的验收。新题的验收用模拟模型和本机服务检查产物，真实模型只在显式 `run --real-model` 时调用。扩展评分同时记录新验收和沿用的 v3 验收指纹。

扩展报告单独记录，历史六例继续按原口径比较。汇报扩展结果时同时列出七道不同任务及每题两次运行的结果，并区分 `task_ok`、`engine_ok` 和无效运行。
正式对照从干净的引擎提交准备，两次评测使用相同的目标、模型、预算和验收版本。

首次使用扩展套件时，先逐个运行四道新题的 `-1` 用例。每题仍以 24 次模型请求和 300 秒为限。检查独立验收、耗时、已知用量和停止原因，判断题目难度是否合适，再决定是否跑全套。难度需要调整时，先固定新版题目和预算，再重新准备对照。

### 提示词对照

`prompt_comparison.py` 固定 `c4f2680` 的引擎源码、依赖、目标和 v3 评分。同一引擎交替运行 `control-1`、`candidate-1`、`control-2`、`candidate-2`。两组只差「改完要运行真实场景并看输出内容」这一行。先提交实验脚本并从干净工作区准备，再用一条小请求探测模型端点。通过后，从准备时的同一提交启动付费对照。

```sh
python -X utf8 evals/prompt_comparison.py probe
python -X utf8 evals/prompt_comparison.py prepare
python -X utf8 evals/prompt_comparison.py run --real-model --run-dir RUN_DIR --probe PROBE_JSON
python -X utf8 evals/prompt_comparison.py report --run-dir RUN_DIR
```

`probe` 输出证据文件路径，`prepare` 输出对照运行目录。四例约用 160 万至 190 万 tokens。开工前服务故障判无效时停止本轮。两组各有两次有效结果后，候选组的 `task_ok` 次数更多且 `engine_ok` 次数不少于对照组，才支持保留。`comparison.json` 保存计数与结论。

### 对比运行时需求验收

从干净提交用同一引擎、目标、用例、模型和预算准备两组。实验组在 `prepare` 加 `--agent-accept 路径`，保存验收配置的指纹；对照组用普通 `prepare`。公开验收用 CLI 任务详情检查两种工作区，一种有多个深层子目录提供有效规则，另一种只有超预算的子目录规则。v3 保留原有场景和断言，在目标工作区之外独立评分。

```sh
python -X utf8 evals/checks/bugfix_rule_summary.py --write-config evals/runs/bugfix-agent-accept.json
python -X utf8 evals/runner.py prepare
python -X utf8 evals/runner.py prepare --agent-accept evals/runs/bugfix-agent-accept.json
python -X utf8 evals/runner.py run --real-model --run-dir CONTROL_DIR --case bugfix-1
python -X utf8 evals/runner.py run --real-model --run-dir EXPERIMENT_DIR --case bugfix-1
python -X utf8 evals/runner.py run --real-model --run-dir CONTROL_DIR --case bugfix-2
python -X utf8 evals/runner.py run --real-model --run-dir EXPERIMENT_DIR --case bugfix-2
python -X utf8 evals/runner.py report --run-dir EXPERIMENT_DIR --compare CONTROL_DIR
```

配置文件在运行前重新校验指纹。报告分别列出运行时需求验收、独立评测得分和请求数。用 v3 得分观察 `bugfix` 在这组反馈下有没有改善。公开检查和评分覆盖同一产品需求，证据限于本任务和这次配置。

`recheck --run-dir 路径` 可以指定原运行目录；默认使用最近一次运行。它核对保存的改动与工作区一致后，只重新运行外部验收，结果另存到该运行目录的 `rechecks/`，不会覆盖原报告或调用模型，也不会改变原运行的引擎终态。报告记录新旧验收脚本指纹，验收变更后的分数不能直接与旧分数比较。

## 怎么计分

每一例分开记两项：

| 项目 | 含义 |
| --- | --- |
| `task_ok` | 任务做对了没有，看外部验收或人工核对 |
| `engine_ok` | 引擎是否正常结束，终态为 `returned` 且没有异常 |

分开是因为两者含义不同：基线里缺陷修复两次都 6/6 通过验收，却因为预算用完被判失败。收尾逻辑变好只会提高 `engine_ok`，不能算成解题能力的提升。

项目理解任务靠源码核对。把结论写到运行目录的 `source-reviews.json`，格式是 `{"understanding-1": {"verdict": "passed"}}`。AI 完成的核对加上 `"review_method": "ai_source_review"`，报告会标明来源。没有结论时显示「待核对」。[已有 AI 复核记录](baselines/2026-11-06-source-reviews-ai.json)

如果模型服务以 HTTP 5xx 或连接错误（包括 TLS 握手失败）结束，且记录显示模型还没返回内容、调用工具或产生改动，该次运行显示为“无效”。`valid_runs` 是纳入计分的次数，`invalid_runs` 单列。无效运行不做验收，两项分数都留空，也不算待人工核对。报告仍记录实际请求、耗时和已知用量。开工后的故障按原规则计分。

用量从任务的逐请求事件累计。有请求缺少用量时，`total_tokens` 为 `null`，报告显示已知下界和缺失请求数，例如 `≥335258（1 次请求用量未知）`。这不是精确总量，失败任务也不会因此显示成零。`report` 和 `recheck` 都会从保存的 `task.json` 恢复已有记录中的已知用量。

## 基线

`baselines/2026-11-03.json` 是当时结果的摘要，已去掉本机路径和接口地址，分数按 v1 验收，不再改动。`baselines/2026-11-03-v3.json` 是同一批产物用当前验收离线重评的结果，`report` 默认和它比较，发现目标内容或验收脚本不同时会在报告里提示。

重评基线的命令如下，会先核对工作区和当时保存的 diff 一致：

```sh
python -X utf8 evals/runner.py rescore-baseline --run-dir 基线运行目录
```

验收改过两次，都是看到结果之后的裁判修正，不代表引擎能力提升：

| 版本 | 怎么判规则摘要 | 问题 |
| --- | --- | --- |
| v1（`baselines/acceptance-v1.py`） | 不能出现“未找到”“未加载”等字样 | 准确说明根规则缺失会被判错；“AGENTS.md · 已加载”这种指错文件的写法却能过 |
| v2（`baselines/acceptance-v2.py`） | 要求内部 `status` 为 `loaded` | 绑定了具体实现，也只认“已加载 N 字节”一种写法 |
| v3（当前） | 只看用户读到的摘要 | 根规则没加载时不能说它已加载，还要写出子目录规则或文件名，或者明确说根规则没加载；字节数要对 |

验收每次用全新的测试数据，`prepare` 的预检也在单独的 `precheck-workspace` 里运行，不碰真实用例的工作区。

和基线比较时要注意：

- **目标内容相同：** 指纹统一换行后再算。基线当时有 25 个文件换行符不同，内容一致，现在固定用 LF 导出。
- **限额是近似换算：** 基线上下文限额是 128 KiB，现在按项目约定的 3 字节/token 换算成 43690 token。当时 128 KiB 的工具结果上限已经不存在。
- **模型不完全可控：** 温度和种子用默认值，后端模型版本也没有验证。

## 这些数字能说明什么

历史 M2 是 3 个任务各跑 2 次。扩展套件覆盖 7 个任务，但两次重复执行仍属于同一道题。两套结果都只说明各自固定任务上的表现。
