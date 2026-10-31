# RecapCLI

> 终端里的 AI 编程 Agent：读代码、做计划、改文件、跑命令，写入与命令先审批，任务过程可回查。

![CI](https://github.com/a-xiaoyuan/RecapCLI/actions/workflows/ci.yml/badge.svg)

## 特性

- **三种模式**：ReAct 直接执行、Plan 先规划再执行、Multi 仅本地只读的并发分析与审查。
- **本地工具**：文件查找、正则/整词/上下文检索、AST 符号、FTS5 索引、目录浏览、批量编辑、按组撤销、命令执行。
- **可控执行**：默认只读；写入和命令逐次审批，批准绑定参数与文件指纹；凭据路径隔离，命令环境白名单。
- **任务追溯**：记录模型请求、工具结果、审批、用量和人工验收；失败或中断后可恢复事实，不自动重放操作。
- **终端体验**：Textual TUI，多行输入、命令补全、F1 帮助、F2 选模型、F3 配置、Git 差异与撤销。
- **上下文与规则**：历史压缩、长期记忆、`AGENTS.md` 项目规则、跨会话恢复。
- **模型与输入**：Chat Completions / Responses 双协议、SSE 流式、图片附件；可选网页搜索与读取。

## 快速开始

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/)，以及支持工具调用的模型端点。

```powershell
git clone https://github.com/a-xiaoyuan/RecapCLI.git
cd RecapCLI
uv sync --locked --extra tui
Copy-Item .env.example .env
uv run --extra tui python -m recapcli
```

macOS / Linux 使用 `cp .env.example .env`。

没有密钥也可以打开界面，按 **F3** 填写模型 ID、接口地址和 API Key；按 **F2** 切换已有配置。

单次提问：

```powershell
uv run python -m recapcli "读取 README.md，说明项目如何运行"
```

## 安装为命令

```powershell
uv build
uv tool install dist/recapcli-0.6.0-py3-none-any.whl --with "textual>=8,<9"
recapcli
```

安装后配置保存在用户目录（Windows `%APPDATA%\recapcli`，macOS/Linux `~/.config/recapcli`），可用 `RECAPCLI_CONFIG_DIR` 覆盖。源码运行时仍读取项目根目录的 `.env` 与 `models.toml`。

## 配置

`.env` 中至少设置：

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-id
# OPENAI_BASE_URL=https://your-provider.example/v1
# OPENAI_PROTOCOL=chat_completions
```

多模型配置复制 `models.example.toml` 为 `models.toml`，密钥仍放在 `.env` 或环境变量：

```powershell
uv run python -m recapcli --models
uv run python -m recapcli --profile backup
```

需要网页搜索时，在 `.env` 中配置 `BRAVE_API_KEY` 或 `TAVILY_API_KEY`，再用 `--allow-web` 开启。

## 常用用法

```powershell
# 计划模式，开放编辑与命令
uv run python -m recapcli --mode plan --allow-write --allow-exec

# 恢复最近会话
uv run python -m recapcli --continue

# 并发分析与审查（仅本地只读）
uv run python -m recapcli --mode multi "分析会话保存逻辑"
```

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看命令与快捷键 |
| `/model`、`/config` | 选择模型 / 配置模型 |
| `/new`、`/open ID` | 新会话 / 恢复会话 |
| `/compact`、`/summary` | 压缩历史 / 查看摘要 |
| `/tasks`、`/task ID` | 查看任务事实 |
| `/diff [任务ID]` | 查看编辑差异与命令结果 |
| `/undo [组ID]` | 审批后撤销编辑组（需要 `--allow-write`） |
| `/exit` | 保存并退出 |

## 项目规则

在工作区根目录放置 `AGENTS.md`，记录项目结构、开发约定和验证命令。每轮任务会重新读取；规则不能扩大工具权限或替代审批。

## 修改与验证

任务开始时记录 Git 状态；编辑工具产生的差异可用 `/diff` 查看，`/undo` 按操作组恢复。命令记录包含实际参数与退出码；命令退出成功不等于任务验收通过。

## 安全边界

- 默认只读，写入和命令需要显式开关并逐次审批。
- 命令以当前用户权限运行，**不是操作系统沙箱**；不要在不可信仓库中运行。
- 工作区内的 `.env`、密钥文件和凭据目录不会被读取或编辑。
- 尚未接入 MCP、Skills 与外部知识库。

## 开发

```powershell
uv run python -X utf8 -m unittest discover -s tests
```

模型请求使用本地 mock，不访问真实模型服务。CI 在 Windows / Ubuntu 与 Python 3.12 / 3.13 上运行编译、测试和 CLI 冒烟。

## 目录结构

```text
recapcli/
  application/   任务流程、命令、帮助、修改报告
  core/          Agent 循环、预算、规划、压缩、记忆、恢复
  llm/           模型客户端、协议、流式、图片
  tools/         文件、检索、编辑、命令、审批、网页
  storage/       会话、任务与运行数据
  tui/           Textual 界面、设置与执行驱动
  ui/            行式输出与终端字标
tests/
  core/ storage/ tools/ integration/ ui/
```
