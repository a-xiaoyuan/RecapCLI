# RevoCLI

一个 Python 终端代码助手：读取和检索项目、修改文件、运行命令，并保存会话与任务记录。

## 核心能力

- **代码操作**：查找文件、搜索文本、按行读取、编辑与备份恢复。
- **持久代码索引**：Python AST 符号定位与 SQLite FTS5 关键词短语检索，返回可按行读取的引用。
- **三种模式**：ReAct 直接处理任务，Plan and Execute 先计划再分步执行，Multi 并发只读分析与审查。
- **持续对话**：保存和恢复会话，支持历史摘要与自动压缩。
- **长期记忆**：保存偏好与项目事实，按问题检索，审阅并接纳任务经验。
- **执行追溯**：查看模型请求、工具结果、用量与验收记录。
- **多模型配置**：Chat Completions / Responses 协议，启动选择或会话内切换。
- **联网工具**：显式开启网页搜索与 HTTP(S) 页面读取，返回来源和截断标记。
- **人工审批**：编辑和命令在能力开关之外逐次确认；拒绝、过期、取消和文件变化都不会执行。

## 快速开始

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/)，以及支持 Chat Completions 或 Responses 工具调用的模型服务。

在项目根目录安装依赖并复制配置（PowerShell）：

```powershell
uv sync --locked
Copy-Item .env.example .env
```

macOS / Linux 使用 `cp .env.example .env` 复制配置。

填写 `.env`：

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-id
# 使用兼容服务时填写其 API 根地址
# OPENAI_BASE_URL=https://your-provider.example/v1
```

启动交互对话：

```shell
uv run python -m revocli
```

配置读取 RevoCLI 根目录的 `.env`，环境变量优先。模型请求会产生所配置服务的用量。

多模型可将根目录 `models.example.toml` 复制为 `models.toml`，填写模型 ID、协议、服务根地址与密钥环境变量名；密钥仍放在 `.env` 或环境变量中。原有 `OPENAI_*` 配置继续作为 `default` 使用；可用 `OPENAI_PROTOCOL=responses` 改变其协议。

```shell
uv run python -m revocli --models
uv run python -m revocli --profile backup
```

交互时用 `/models` 列举、`/model backup` 切换下一轮、`/model` 查看当前配置。`--continue` 沿用会话最后保存的配置名，显式 `--profile` 可覆盖。切换不探测、不重试或自动降级。工具、图片、流式能力为配置声明，不按模型名推断；本版只执行文本及工具调用，图片与流式尚未实现。`context_bytes` 限制上下文 UTF-8 JSON 字节，并与 CLI 上限取较小值，不等同于模型 token 窗口。

历史问答与工具事实继续保留；Responses 使用 `store=false`，协议私有密文仅回送原配置，不进入摘要与长期记忆。两种协议均有离线模拟测试，具体服务是否可用仍需真实端点验收。

## 常用用法

```shell
# 单次提问
uv run python -m revocli "读取 README.md，说明项目功能"

# 指定要处理的项目
uv run python -m revocli --workspace ./your-project

# 继续最近的会话
uv run python -m revocli --continue

# 使用计划模式，并开放编辑与命令执行
uv run python -m revocli --mode plan --allow-write --allow-exec

# 并发只读分析与审查，再由父任务汇总
uv run python -m revocli --mode multi "分析并审查这个项目的会话保存逻辑"
```

默认只读。编辑与执行权限分别由 `--allow-write`、`--allow-exec` 开启；命令以当前用户权限运行，可以修改文件，并非系统沙箱。

### 网页搜索和读取

联网默认关闭，`react` 与 `plan` 支持下面两种入口，`multi` 仅支持只读的 `--allow-web`。搜索密钥放在 RevoCLI 根目录 `.env` 或环境变量中，环境变量优先，不读取目标工作区配置；读取 URL 不需要搜索密钥。

| 启动选项 | 搜索配置 | URL 读取 |
| --- | --- | --- |
| `--allow-network` | `TAVILY_API_KEY`，使用 Tavily basic 搜索 | 公网静态 HTML、文本、Markdown、JSON；正文最多 32 KiB，逐次校验重定向和目标 IP |
| `--allow-web` | `BRAVE_API_KEY` 或 `TAVILY_API_KEY`；`REVO_WEB_PROVIDER=brave` / `tavily` 可指定服务 | HTTP(S) HTML 与纯文本，读取及正文最多 64 KiB |

`--allow-network` 提供 `web_search` 和 `read_url`；`--allow-web` 提供 `web_search` 和 `fetch_url`，未指定服务时优先选 Brave，仅配置 Tavily 密钥时选 Tavily。两个开关同时启用时，搜索优先走 `network` 的 Tavily 配置。联网工具不逐次请求审批；查询会发送给对应服务并产生其账户用量。

```shell
uv run python -m revocli --allow-network "搜索 Python 官方 asyncio 文档，读取相关页面并附来源链接"
uv run python -m revocli --allow-web "读取 https://docs.python.org/3/library/asyncio.html，说明主要用途"
```

搜索返回标题、URL 和摘要；URL 返回正文、最终地址与截断标记。不运行网页 JavaScript，不支持登录页面与 PDF/图片等二进制内容。搜索摘要或截断片段不等于全文；外部内容仅作资料，其中的指令不能授权写入、执行或发送密钥。`--allow-network` 另限制公网访问、重定向、响应大小与期限，失败不自动重试；同步在途请求的取消需等待返回或超时。

### 人工审批

`--allow-write` 和 `--allow-exec` 打开能力后，`edit_file`、`restore_file` 和 `run_command` 仍逐次显示参数、文件指纹与差异提示，等待 `approve`、`reject` 或 `cancel`（或 `批准`、`拒绝`、`取消`）。其他文字不会被当成批准。等待默认 120 秒，可用 `--approval-timeout` 调整，且受任务剩余时间限制。

单次提问和非交互运行不会等待输入，结果为 `needs_approval`，不写文件、不启动进程。脚本化执行需要显式加上 `--yes`。批准仍绑定这次的最终参数和文件内容指纹；执行前如果参数或指纹变了，批准作废且不会执行。

拒绝、过期、取消和失效都不会写备份，也不会启动进程。

### 只读子代理（W4 切片）

`--mode multi` 同时启动 `analysis`（分析）与 `review`（独立审查）两个子代理。两者各自维护上下文、复用现有 ReAct 与工具执行链，彼此不接收对方的中间消息；结束后由父任务汇总报告、分歧和未完成项。默认 `react` 保持直接执行，`plan` 保持计划执行。

子代理仅获得父任务能力与 `read`、`web` 的交集，即使加上 `--allow-write --allow-exec` 也不会开放编辑、恢复或命令执行。汇总阶段禁止工具；代码索引缓存与会话、任务记录仍写入运行数据目录。分析和审查属于模型意见，不能代替运行测试或人工验收。

`--max-steps` 是包括历史压缩、两个子代理和父任务汇总的**请求总上限**，默认 20，最少 3。压缩后先保留一次汇总请求，剩余请求均分给两个角色（余数给分析），未用份额不转移；因此最小预算只够直接报告，需要检索时应增加预算。`--max-tool-calls`、`--max-tool-bytes` 和 `--max-seconds` 在整个任务内共享，不按子代理重置；上下文大小按每次模型请求限制。这些是请求数、工具结果字节和时间限制，不是 token 或费用硬上限，usage 按服务实际返回记录。

Ctrl+C 会传播取消并停止后续模型与工具调度。Python API 可使用 `run_agent(..., mode="multi", cancel_event=threading.Event())`，调用方通过该 Event 的 `set()` 取消。取消是协作式的：已在途的同步 HTTP 请求等待返回或 SDK 超时（单次 SDK 超时设为 30 秒与剩余任务时间的较小值），已执行的只读工具事实保留，不自动重试。

`/task ID` 或 `--task ID --json` 可查看带 `agent` 角色和独立动作 ID 的模型请求、真实工具结果、完整子代理报告、共享预算及用量。会话只追加用户问题与最终汇总，子代理失败会标明未完成；汇总失败时保留两个报告，不重复请求。此模式不包含并发写入或流式输出，不代表整个 W4 已验收。

### 代码索引

Agent 可通过 `search_symbols` 按精确名称和种类（`class`、`function`、`async_function`）查找 Python 符号，或通过 `search_code_index` 查找 FTS5 关键词短语。结果包含 `path`、`start_line`、`end_line` 和 `reference`；关键词结果的 `snippet` 是对应的一行，可用 `read_file` 核对。FTS5 使用默认分词，按短语匹配，不是子串或语义搜索。

索引保存在工作区 `.revocli/retrieval/index.sqlite3`（旧数据目录沿用 `.archer`），每次查询按文件内容哈希增量更新，并在返回前复核结果文件；查询期间发生变化的结果会被丢弃并标记 `stale_files`，请重新查询。引用是检查时的快照，文件之后仍可能变化，使用前应重新读取。索引仅遍历工作区，跳过链接、内部目录、`.env`、`.env.*` 和常见密钥文件，未实现通用敏感信息识别或 `.gitignore` 规则。仅索引 UTF-8 文本；超过 1 MiB 的文件、二进制文件、超过 `read_file` 64 KiB 限制的单行或完整符号会跳过；语法错误的 Python 文件仍可做关键词搜索但无符号索引。扫描预算或结果上限可能导致结果不完整，查看 `truncated`、`limit_reason` 和 `skipped_files`。该功能需要带 FTS5 的 SQLite；索引内容只是资料，不构成授权。

## 常用交互命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看全部交互命令 |
| `/exit` | 保存并退出 |
| `/new` | 开始新会话 |
| `/compact`、`/summary` | 压缩旧历史、查看摘要 |
| `/remember preference 文本` | 保存常用偏好 |
| `/remember fact 文本` | 保存项目事实 |
| `/memory`、`/memory-search 问题` | 查看或检索长期记忆 |
| `/tasks`、`/task ID` | 查看任务记录 |

启动参数可通过 `uv run python -m revocli --help` 查看。

## 测试

```shell
uv run python -m unittest discover -s tests
```

模型接口测试使用模拟响应，不请求真实模型服务。
