# RevoCLI

一个 Python 终端代码助手：读取和检索项目、修改文件、运行命令，并保存会话与任务记录。

## 核心能力

- **代码操作**：查找文件、搜索文本、按行读取、编辑与备份恢复。
- **持久代码索引**：Python AST 符号定位与 SQLite FTS5 关键词短语检索，返回可按行读取的引用。
- **三种模式**：ReAct 直接处理任务，Plan and Execute 先计划再分步执行，Multi 并发只读分析与审查。
- **持续对话**：保存和恢复会话，支持历史摘要与自动压缩。
- **长期记忆**：保存偏好与项目事实，按问题检索，审阅并接纳任务经验。
- **执行追溯**：查看模型请求、工具结果、用量与验收记录。

## 快速开始

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/)，以及支持 Chat Completions 工具调用的模型服务。

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

### 只读子代理（W4 切片）

`--mode multi` 同时启动 `analysis`（分析）与 `review`（独立审查）两个子代理。两者各自维护上下文、复用现有 ReAct 与工具执行链，彼此不接收对方的中间消息；结束后由父任务汇总报告、分歧和未完成项。默认 `react` 保持直接执行，`plan` 保持计划执行。

子代理仅获得父任务能力与 `read` 的交集，即使加上 `--allow-write --allow-exec` 也不会开放编辑、恢复或命令执行。汇总阶段禁止工具；代码索引缓存与会话、任务记录仍写入运行数据目录。分析和审查属于模型意见，不能代替运行测试或人工验收。

`--max-steps` 是包括历史压缩、两个子代理和父任务汇总的**请求总上限**，默认 20，最少 3。压缩后先保留一次汇总请求，剩余请求均分给两个角色（余数给分析），未用份额不转移；因此最小预算只够直接报告，需要检索时应增加预算。`--max-tool-calls`、`--max-tool-bytes` 和 `--max-seconds` 在整个任务内共享，不按子代理重置；上下文大小按每次模型请求限制。这些是请求数、工具结果字节和时间限制，不是 token 或费用硬上限，usage 按服务实际返回记录。

Ctrl+C 会传播取消并停止后续模型与工具调度。Python API 可使用 `run_agent(..., mode="multi", cancel_event=threading.Event())`，调用方通过该 Event 的 `set()` 取消。取消是协作式的：已在途的同步 HTTP 请求等待返回或 SDK 超时（单次 SDK 超时设为 30 秒与剩余任务时间的较小值），已执行的只读工具事实保留，不自动重试。

`/task ID` 或 `--task ID --json` 可查看带 `agent` 角色和独立动作 ID 的模型请求、真实工具结果、完整子代理报告、共享预算及用量。会话只追加用户问题与最终汇总，子代理失败会标明未完成；汇总失败时保留两个报告，不重复请求。此切片不包含并发写入、流式输出或第二模型协议适配，不代表整个 W4 已验收。

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
