# RecapCLI

**在你的项目目录里运行的终端编程 Agent：默认只读，改动先审批，做过什么都有记录，改错了能撤销。**

[![CI](https://github.com/a-xiaoyuan/RecapCLI/actions/workflows/ci.yml/badge.svg)](https://github.com/a-xiaoyuan/RecapCLI/actions/workflows/ci.yml)

[快速开始](#快速开始) · [常用方式](#常用方式) · [安全与数据](#安全与数据) · [进阶参考](docs/reference.md) · [开发](#开发) · [反馈问题](https://github.com/a-xiaoyuan/RecapCLI/issues)

RecapCLI 用你自己的模型接口工作，支持 OpenAI 兼容的 Chat Completions 和 Responses 协议。它能读代码、定位问题、规划修改、编辑文件和运行验证，并把每一步写入本地任务记录。

> 项目处于 Alpha 阶段，主要面向个人开发者。功能和配置仍可能调整，真实任务的完成率还在持续对照和改进。

## 为什么用它

| 你关心的事 | RecapCLI 的做法 |
| --- | --- |
| Agent 会不会乱改 | 默认只读；写入和命令要显式开启，并绑定参数和文件指纹审批，批准后内容变了就作废 |
| 它说改好了，真的好了吗 | `--verify-cmd` 在结束前自动跑验证，失败就交回模型继续修 |
| 它到底做了什么 | 工具结果、命令退出码、审批和用量都写入任务记录，用 `/task ID` 查看 |
| 改错了怎么办 | 编辑自动备份，`/undo` 按编辑组撤销，文件有后续变化时不会覆盖 |
| 任务太长跑不完 | 上下文和预算用完时转入收尾，说明已完成和未完成的部分 |
| 怎么接入团队规则 | 读取 `AGENTS.md`，并支持 Skills、Hooks、MCP 和长期记忆 |

## 快速开始

需要 **Python 3.12+**、**Git** 和 **[uv](https://docs.astral.sh/uv/getting-started/installation/)**。

**1. 安装**

```sh
uv tool install "git+https://github.com/a-xiaoyuan/RecapCLI.git" --with "textual>=8,<9"
```

找不到 `recapcli` 命令时，执行 `uv tool update-shell`，再重新打开终端。

**2. 进入你的项目并启动**

```sh
cd "你的项目目录"
recapcli
```

**3. 配置模型**

按 **F3** 填写模型 ID、API 地址和 API Key，保存后按 **F2** 选择。RecapCLI 不附带模型服务。

没有模型也能先看界面：

```sh
recapcli --demo
```

建议使用 Windows Terminal、iTerm2 这类完整终端。部分 IDE 内置终端的中文字宽不准，字距会明显变大。

## 常用方式

**只读提问**

```sh
recapcli "读取 README.md，说明项目如何运行"
```

**允许修改和运行命令**（每次操作前会询问）

```sh
recapcli --allow-write --allow-exec
```

**改完自动验证**

```sh
recapcli --allow-write --verify-cmd "python -m unittest"
```

**按需求逐项验收**

```sh
recapcli --allow-write --accept ./acceptance.json "修正项目规则摘要"
```

在 `react` 模式下，`--accept` 读取用户显式指定的 JSON，逐项检查真实命令的退出码和输出。失败项及输出节选交回模型，最多复验三轮。仍有失败或未运行项时，任务以未完成结束。每项的结论、命令 ID 和输出证据都可用 `/task ID` 查看。配置示例见[需求验收](docs/reference.md#需求验收)。

**先规划再执行**

```sh
recapcli --mode plan --allow-write --allow-exec
```

**继续上次会话**

```sh
recapcli --continue
```

全部参数见 `recapcli --help`。

### 执行模式

| 模式 | 适合 | 说明 |
| --- | --- | --- |
| `react`（默认） | 日常任务 | 模型决定下一步，工具结果回填后继续 |
| `plan` | 有依赖的多步任务 | 先出步骤，按依赖执行并核对，必要时重规划 |
| `multi` | 多角度分析或分工 | 默认只读协作；开放写入或命令后启用执行团队 |

### 常用命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看命令和快捷键 |
| `/model`、`/config` | 选择和配置模型 |
| `/new`、`/open ID` | 新建或恢复会话 |
| `/diff [任务ID]` | 查看改动和命令结果 |
| `/undo [组ID]` | 撤销一组编辑（需要 `--allow-write`） |
| `/tasks`、`/task ID` | 查看任务记录 |
| `/jobs`、`/job ID`、`/stop ID` | 管理后台命令 |
| `/memory`、`/remember` | 查看和添加长期记忆 |
| `/copy`、`/copy all` | 复制回答或整段对话 |

### 快捷键

| 按键 | 作用 |
| --- | --- |
| Enter / Shift+Enter 或 Ctrl+J | 发送 / 换行 |
| ↑ ↓ | 翻阅已发送内容，审批时选择选项 |
| @ | 补全项目文件路径 |
| Esc | 停止本轮 |
| Ctrl+G | 查看任务详情和用量 |
| F2 / F3 | 选择模型 / 模型设置 |
| Ctrl+Q | 保存退出 |

## 模型配置

设置界面需要填写接口实际接受的**模型 ID**、**API 地址**和 **API Key**，并选择 `chat_completions` 或 `responses` 协议。模型需要支持工具调用；图片输入还需要视觉能力。

配置保存在用户目录，和你的项目分开：

- Windows：`%APPDATA%\recapcli`
- macOS / Linux：`~/.config/recapcli`；设置了 `XDG_CONFIG_HOME` 时用其下的 `recapcli`
- 自定义：环境变量 `RECAPCLI_CONFIG_DIR`

<details>
<summary>手动配置与多个模型</summary>

在配置目录创建 `.env`，可参考 [.env.example](.env.example)：

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-id
# 第三方兼容接口按服务商说明填写 API 根地址
# OPENAI_BASE_URL=https://your-provider.example/v1
# OPENAI_PROTOCOL=chat_completions
# 模型上下文窗口（token），默认 128000
# OPENAI_CONTEXT_TOKENS=200000
```

多个模型可参考 [models.example.toml](models.example.toml)，在配置目录创建 `models.toml`。密钥通过 `api_key_env` 引用，不直接写进 TOML。

```sh
recapcli --models
recapcli --profile backup
```

环境变量优先于 `.env`。`models.toml` 的 `default` 决定默认配置，`--profile` 可在启动时指定。

从源码运行时，默认读取 RecapCLI 仓库根目录的 `.env` 和 `models.toml`，`RECAPCLI_CONFIG_DIR` 同样可覆盖。不会读取目标项目的同名配置。

</details>

## 失败案例与验证

在 M2 的 `bugfix-2` 中，任务要求正确显示实际加载的 `pkg/AGENTS.md`。Agent 新增的测试全部通过，但断言只检查了「已加载」等关键词。任务详情仍把根目录的 `AGENTS.md` 显示为已加载，独立 v3 验收只有 **3/6**。依据是 `evals/runs/20261106-050050/results/bugfix-2/agent.log` 和同一运行目录下的 `rechecks/20261106-231245-472435/report.md`。

逐项验收可以直接检查用户看到的 CLI 输出。在同一历史目标上做脚本化重放，关闭验收时独立评分为 **3/6**。开启后，错误摘要触发反馈，预设修复随后使评分达到 **6/6**。这是 `MockTransport` 流程演示，数据来自 `_testtmp/recapcli-acceptance-replay-6f14521d/report.md`。真实对照将用不同的公开检查场景和 v3 评分场景，观察失败反馈对这项任务的影响。评测分别记录产物是否做对 `task_ok` 和引擎是否正常结束 `engine_ok`。[评测方法](evals/README.md)

项目理解两例均通过 AI 源码复核，第一例的引擎终态仍为 `incomplete`。复核依据是目标提交上的引用和实际读取轨迹，结论附有勘误。[复核记录](evals/baselines/2026-11-06-source-reviews-ai.json)

| 真实运行 | `task_ok` | `engine_ok` | 证据 |
| --- | --- | --- | --- |
| 历史 M2，`515abe4`，按 v3 离线重评 | 3/6，含两例 AI 源码复核 | 1/6 | `evals/runs/20261106-050050/rechecks/20261106-231245-472435/report-ai-reviewed.md` |
| 最终引擎的 M2 六例 | 待测 | 待测 | 运行完成后记录目录 |

## 安全与数据

- **默认只读。** `--allow-write` 和 `--allow-exec` 分别开放编辑和命令，默认逐次确认；`--yes` 会跳过确认，但仍复核参数和文件指纹。
- **工作区是默认范围。** 访问外部目录要确认一次，也可以用 `--add-dir` 或 `/add-dir` 授权，重启后不保留。
- **文件工具会拦截凭据。** `.env`、私钥文件和常见凭据目录默认不可访问。
- **没有操作系统沙箱。** 获准执行的命令以当前用户权限运行，不要把文件工具的限制当成命令的权限边界。
- **内容可能发送给模型服务。** 工具读到的项目内容会发送到你配置的接口。
- **数据保存在本地。** 会话、记忆、任务和编辑备份都在目标项目的 `.recapcli/`。
- **`AGENTS.md` 只是开发约定。** 其中的文字不能扩大工具权限，也不能代替审批。

命令执行成功不等于任务验收通过，可以在任务记录里核对实际结果。

## 进阶

下面这些内容放在 [进阶参考](docs/reference.md)：

- [自动验证与 Hooks](docs/reference.md#自动验证与-hooks)
- [Skills](docs/reference.md#skills)
- [MCP 服务](docs/reference.md#mcp-服务)
- [计划、预算与收尾](docs/reference.md#计划协作与混合检索)
- [语义检索配置](docs/reference.md#计划协作与混合检索)

## 开发

```sh
git clone https://github.com/a-xiaoyuan/RecapCLI.git
cd RecapCLI
uv sync --locked --extra tui
uv run python -m recapcli --workspace "你的项目目录"
```

依赖只从上层指向下层，由 `tests/core/test_layers.py` 检查：

```text
recapcli/
  tui/ cli.py    Textual 界面和行式入口
  ui/            行式终端输出
  application/   一轮任务、斜杠命令、启动校验
  storage/       会话、记忆和任务记录
  core/ mcp/     Agent 主循环、规划、预算、Skills、Hooks、MCP
  tools/         文件、检索、编辑、命令和审批
  llm/           模型客户端、协议和流式输出
  base/          路径、文件写入和 token 估算
tests/           单元与集成测试
evals/           固定任务上的真实模型评测，见 evals/README.md
```

```sh
uv run python -X utf8 tests/fast.py                     # 除 UI 外的测试
uv run python -X utf8 -m unittest discover -s tests     # 全量测试
uv build
```

开发约定见 [AGENTS.md](AGENTS.md)。[CI](.github/workflows/ci.yml) 覆盖 Windows / Ubuntu、Python 3.12 / 3.13，并在源码目录外验证 wheel 安装。

## 更新与卸载

```sh
uv tool upgrade recapcli
uv tool uninstall recapcli
```

遇到问题可以在 [Issues](https://github.com/a-xiaoyuan/RecapCLI/issues) 提供系统、Python 版本、复现步骤和错误信息。日志和截图请隐去 API Key。
