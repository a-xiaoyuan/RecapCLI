# RecapCLI

在项目目录里运行的终端 AI 编程助手。用自然语言读代码、定位问题、规划修改，并在确认后编辑文件、执行命令。

[![CI](https://github.com/a-xiaoyuan/RecapCLI/actions/workflows/ci.yml/badge.svg)](https://github.com/a-xiaoyuan/RecapCLI/actions/workflows/ci.yml)

[安装](#安装) · [模型配置](#模型配置) · [使用](#使用) · [源码运行与开发](#源码运行与开发) · [反馈问题](https://github.com/a-xiaoyuan/RecapCLI/issues)

## 能做什么

- **阅读项目**：浏览目录、读取文件，按文件名、正则、整词和上下文搜索代码；支持 Python AST 符号查找和 SQLite FTS5 关键词索引。
- **完成修改**：支持单文件与批量编辑、差异查看、按编辑组撤销，以及命令执行；默认只读，编辑与命令需显式开启。
- **选择执行方式**：`react` 直接处理任务，`plan` 先规划再执行，`multi` 并发分析与审查（仅本地只读）。
- **保留工作上下文**：保存和恢复会话、压缩历史、检索长期记忆，并读取工作区根目录的 `AGENTS.md` 项目规则。
- **查看执行过程**：终端界面展示模型回答、工具进度和审批；任务记录保留工具结果、命令退出码与用量。

支持 Chat Completions 与 Responses 协议、流式回答；模型支持视觉输入时可使用图片附件。网页搜索与读取需单独开启。

## 安装

需要 **Python 3.12+**、**Git** 和 **[uv](https://docs.astral.sh/uv/getting-started/installation/)**。当前从 GitHub 源码安装，以下命令包含终端界面依赖：

```sh
uv tool install "git+https://github.com/a-xiaoyuan/RecapCLI.git" --with "textual>=8,<9"
```

进入希望处理的项目，而不是 RecapCLI 的安装目录：

```sh
cd "你的项目目录"
recapcli
```

首次使用按 **F3** 或输入 `/config` 配置模型，保存后按 **F2** 或输入 `/model` 选择配置。RecapCLI 不附带模型服务，需要提供自己的接口与 API Key。

还没配置模型时，可以用 `recapcli --demo` 查看离线界面演示，不请求模型。

找不到 `recapcli` 命令时，执行 `uv tool update-shell`，再重新打开终端。

更新与卸载：

```sh
uv tool upgrade recapcli
uv tool uninstall recapcli
```

## 模型配置

设置界面中填写接口实际接受的**模型 ID**、**API 地址**和 **API Key**，并选择接口支持的协议：`chat_completions` 或 `responses`。模型需要支持相应协议的工具调用；图片输入还需要模型支持视觉能力。

安装后的配置保存在用户目录，与正在处理的项目分开：

- Windows：`%APPDATA%\recapcli`
- macOS / Linux：`~/.config/recapcli`；设置了 `XDG_CONFIG_HOME` 时使用该目录下的 `recapcli`
- 自定义位置：通过环境变量 `RECAPCLI_CONFIG_DIR` 指定

<details>
<summary>手动配置与多个模型</summary>

在配置目录创建 `.env`，可参考仓库的 [.env.example](.env.example)：

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-id
# 第三方兼容接口按服务商说明填写 API 根地址
# OPENAI_BASE_URL=https://your-provider.example/v1
# OPENAI_PROTOCOL=chat_completions
```

多个模型可参考 [models.example.toml](models.example.toml)，在配置目录创建 `models.toml`。密钥通过 `api_key_env` 引用 `.env` 或环境变量，不直接写进 TOML。

```sh
recapcli --models
recapcli --profile backup
```

环境变量优先于 `.env`。`.env` 中的基础模型对应 `default` 配置；`models.toml` 的 `default` 字段决定默认使用哪个配置，`--profile` 可在启动时明确指定。

从源码运行时，默认读取 RecapCLI 仓库根目录的 `.env` 与 `models.toml`；`RECAPCLI_CONFIG_DIR` 同样可覆盖该位置。不会读取目标项目的同名配置。

</details>

## 使用

直接提问，适合阅读代码和排查问题：

```sh
recapcli "读取 README.md，说明项目如何运行"
```

进入交互界面，先规划，再在审批后修改文件和运行命令：

```sh
recapcli --mode plan --allow-write --allow-exec
```

恢复最近会话，或进行只读的并发分析：

```sh
recapcli --continue
recapcli --mode multi "分析会话保存逻辑"
```

终端里常用的命令：

- `/model`、`/config`：选择与配置模型。
- `/new`、`/open ID`：新建或恢复会话。
- `/diff [任务ID]`：查看编辑差异与命令结果。
- `/undo [组ID]`：确认后撤销编辑组，需要 `--allow-write`。
- `/tasks`、`/task ID`：查看任务记录。
- `/copy`、`/copy all`：复制最近的完整回答或整段对话（TUI）。
- `/help`：查看分类帮助与快捷键；`/exit` 保存退出。

**Enter** 发送，**Shift+Enter / Ctrl+J** 换行，**Esc** 停止本轮。**Ctrl+Q** 保存退出；忙时会等待在途请求或操作返回。全部启动参数见 `recapcli --help`。

**Ctrl+A** 全选输入；输入框为空时全选对话，随后按 **Ctrl+C** 复制。IDE 截获快捷键时，可直接输入 `/copy` 或 `/copy all`。

需要联网时使用 `--allow-web`。网页读取不需要搜索密钥；网页搜索需要在 RecapCLI 的 `.env` 或环境变量中配置 `BRAVE_API_KEY` 或 `TAVILY_API_KEY`。

## MCP 服务

项目自带一个 stdio MCP 服务，只暴露仓库读取工具，不提供写入、编辑或命令执行：

```powershell
.\.venv\Scripts\python.exe -m recapcli.mcp.repo_server --workspace "你的项目目录"
```

其他 MCP 客户端可以把上面的命令作为 stdio server 启动；可用工具包括 `read_file`、`search_text`、`find_files`、`list_directory` 等读取类工具。服务会拒绝工作区外路径和未知/写入工具。

RecapCLI 自己也可以连接这个服务，行式 CLI 和 TUI 都支持：

```powershell
recapcli --mcp-repo "只使用 mcp_repo_read_file 阅读 README.md 的前 10 行"
```

模型会看到带 `mcp_repo_` 前缀的只读工具；写入和命令能力不会通过 MCP 暴露。

外部服务需要用 `--mcp-config 路径` 显式启动；仅使用 `--mcp-repo` 不会执行工作区 `mcp.json` 中的命令。例如接入官方 [Fetch MCP 服务](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)，让模型读取公开技术文档，无需额外 API Key。

已安装 uv 时，可将以下配置保存为 `mcp-fetch.json`。首次启动会下载服务；`--no-readability-js` 使用 Python 提取网页正文，不另外启动 Node.js：

```json
{
  "servers": {
    "fetch": {
      "command": ["uv", "tool", "run", "--from", "mcp-server-fetch==2026.10.28", "mcp-server-fetch", "--no-readability-js"],
      "timeout": 30,
      "max_result_bytes": 49152,
      "tool_permissions": {
        "fetch": "read"
      }
    }
  }
}
```

```powershell
recapcli --mcp-config .\mcp-fetch.json "用 mcp_fetch_fetch 读取 https://docs.astral.sh/uv/guides/tools/，解释 uv tool install 和 uvx 的区别"
```

如果服务已装在独立虚拟环境中，也可将 `command` 改为该环境的 Python 绝对路径，加上 `-m mcp_server_fetch --no-readability-js`，避免启动时下载依赖。

每个服务都可以设置 `timeout`（秒）和 `max_result_bytes`（UTF-8 字节）；工具名会带上 `mcp_<服务名>_` 前缀，避免冲突。返回超过限制时会被截断或明确拒绝。

`tool_permissions` 是你对可信工具的本地授权，可设为 `read`、`write` 或 `exec`；未声明的外部工具默认需要 `--allow-exec` 和审批。`write` 工具需要 `--allow-write` 和审批，不根据服务端自报的只读标签放行。

外部服务以当前用户权限运行，没有操作系统沙箱；请先核对配置中的启动命令和工具用途。子进程不自动继承宿主凭据，需要的服务密钥在该服务的 `env` 中显式配置；含密钥的配置请保存在用户目录，不提交仓库。

```powershell
recapcli --mcp-repo --mcp-config .\mcp-fetch.json
```

## 权限与数据

- 默认只读。`--allow-write` 与 `--allow-exec` 分别开放编辑和命令，默认逐次确认；`--yes` 会跳过逐次确认。
- 文件工具按路径规则拦截 `.env`、私钥文件和常见凭据目录。获准执行的命令以当前用户权限运行，**没有操作系统沙箱隔离**，不能把文件工具的限制视作命令的权限边界。
- 工具读取的项目内容可能发送到你配置的模型服务。会话、记忆、任务和编辑备份保存在目标项目的 `.recapcli/`；已有的 `.revocli/` 或 `.archer/` 数据目录会沿用。
- 工作区根目录的 `AGENTS.md` 可说明开发约定和验证命令，每轮重新读取；其中的文字不能扩大工具权限或代替审批。

符号查找目前针对 Python，FTS5 是关键词检索。命令执行成功与任务验收是两回事，可通过任务记录核对实际结果。

## 源码运行与开发

```sh
git clone https://github.com/a-xiaoyuan/RecapCLI.git
cd RecapCLI
uv sync --locked --extra tui
uv run python -m recapcli --workspace "你的项目目录"
```

没有交互式终端或希望使用行式界面时，可运行 `uv run python -m recapcli.cli`。

主要目录：

```text
recapcli/
  application/   任务流程、会话命令与修改报告
  core/          Agent 循环、规划、预算、压缩与记忆
  llm/           模型客户端、协议、流式与图片输入
  tools/         文件、检索、编辑、命令、审批与网页工具
  storage/       会话、任务与运行数据
  tui/           Textual 交互界面、模型设置与执行驱动
  ui/            行式终端输出
tests/           单元与集成测试
```

本地测试与打包：

```sh
uv run python -X utf8 -m unittest discover -s tests
uv build
```

[CI](.github/workflows/ci.yml) 在推送和 Pull Request 时运行检查：测试矩阵覆盖 Windows / Ubuntu、Python 3.12 / 3.13；打包检查在源码目录外安装 wheel，验证安装后的命令入口与配置保存。

使用中遇到问题，可在 [Issues](https://github.com/a-xiaoyuan/RecapCLI/issues) 提供系统、Python 版本、复现步骤和错误信息。日志与截图请隐去 API Key 等敏感信息。
