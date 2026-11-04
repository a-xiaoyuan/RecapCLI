# RecapCLI 开发约定

终端 Coding Agent（Python 3.12+，Textual TUI + 行式 CLI）。改代码前先读这里。

## 目录

- `recapcli/core/agent.py`：ReAct 主循环、上下文计量与清理、预算收尾、todo_write。改 Agent 行为基本都在这里。
- `recapcli/core/budget.py`：请求/工具/时间预算；用完抛 `BudgetExhausted`，由 agent 转入收尾，不当作失败。
- `recapcli/core/context.py`：token 估算（`TokenMeter` 用服务端 prompt_tokens 校准）和历史选择。
- `recapcli/core/planning.py`、`subagents.py`、`team.py`：plan / multi 模式。
- `recapcli/tools/`：工具实现，`registry.py` 是工具定义与分发入口。
- `recapcli/application/`：一轮任务的落盘与恢复（`runtime.py`）、斜杠命令（`commands.py`）、帮助文本（`help.py`）。
- `recapcli/cli.py`：行式 CLI 与全部启动参数；`recapcli/tui/`：Textual 界面，样式在 `recap.tcss`，配色在 `theme.py`。
- `recapcli/storage/`：会话 JSON、任务 SQLite（`.recapcli/` 目录）。

## 验证

用项目自己的解释器（Windows 上是 `.venv\Scripts\python.exe`），总带 `-X utf8`。先跑和改动相关的最小范围，不要一上来跑全量：

| 改动位置 | 命令 |
| --- | --- |
| core/ | `python -X utf8 -m unittest discover -s tests/core -t tests` |
| tools/ | `python -X utf8 -m unittest discover -s tests/tools -t tests` |
| storage/、application/ | `python -X utf8 -m unittest discover -s tests/storage -t tests` 和 `tests/integration` |
| cli.py、config.py、llm/ | `python -X utf8 -m unittest discover -s tests/integration -t tests` |
| tui/、ui/ | `python -X utf8 -m unittest discover -s tests/ui -t tests`（较慢，Windows 上几分钟） |
| 收尾前 | `python -X utf8 tests/fast.py`（除 UI 外的全部，约半分钟） |

单个用例：`python -X utf8 -m unittest discover -s tests -t tests -k 用例名片段`。

## 约定

- 面向用户的文字（界面、错误、帮助、提示词）用中文；代码标识符用英文。
- 新功能如果有多个入口（存储、CLI 参数、斜杠命令、帮助文本、测试），每个入口都要改到，并在 `application/help.py` 里同步帮助。
- 改 CLI 参数时同时检查 `cli.py` 的 `build_parser` 和 `tui/launch.py` 的 `shell_options`。
- 不新增第三方依赖；确有必要时先说明理由。
- 界面配色只用 `theme.py` 的 `PALETTE` 和 `recap.tcss` 里的 ANSI 颜色名，唯一的固定色是强调色。
- 测试用 `unittest` 和 `httpx2.MockTransport` 模拟模型，不发真实网络请求。
- 不要修改 `.recapcli/`、`.env`、`models.toml`。
