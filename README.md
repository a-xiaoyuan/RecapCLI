# RecapCLI

Python 终端代码助手：在授权范围内读取和检索项目、修改文件、运行命令，并保存会话与任务记录。

## 当前能力

- **本地代码操作**：查找文件、搜索文本、按行读取；`--allow-write` 后可编辑并按备份恢复；`--allow-exec` 后可运行前台命令。
- **代码索引**：Python AST 符号与 SQLite FTS5 关键词短语，返回可再读取的路径和行号。
- **三种模式**：`react` 直接执行；`plan` 先计划再执行；`multi` **仅本地只读**（并发分析与审查，不联网、不改文件、不执行命令）。
- **会话与记忆**：保存和恢复会话，可压缩历史；长期记忆可查看、检索、失效和删除。任务经验目前只生成候选，接纳前不进入上下文。
- **任务记录**：模型请求、工具结果、用量和人工验收分开保存。模型核对不等于测试通过。
- **多模型配置**：Chat Completions / Responses。切换不探测、不自动换服务。
- **图片与流式回答**：显式附加本地图片；支持视觉能力检查、附件保存与恢复。回答默认通过 SSE 逐段显示，工具参数完整收齐后再执行。
- **联网**：默认关闭。`--allow-web` 打开 `web_search` 和 `read_url`；`--allow-network` 是同一开关的兼容别名。
- **人工审批**：编辑和命令在能力开关之外逐次确认。

飞书归档、技能评测和经验复用还不是当前运行能力。

## 快速开始

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/)，以及支持所选协议工具调用的模型服务。

```powershell
uv sync --locked --extra tui
Copy-Item .env.example .env
```

macOS / Linux 用 `cp .env.example .env`。`--extra tui` 安装 Textual 壳。只跑单次提问或 `python -m recapcli.cli` 时可以不装这个可选依赖。

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-id
# OPENAI_BASE_URL=https://your-provider.example/v1
# OPENAI_PROTOCOL=chat_completions
```

```shell
uv run --extra tui python -m recapcli
```

交互终端会打开 Textual 壳，输入发给当前模型配置。配置只读 RecapCLI 根目录的 `.env`，环境变量优先，不读目标工作区。模型请求会计入所配置服务的用量。没有密钥时壳仍能打开，发送会提示配置缺失并且不会崩溃。

单次提问不进入壳：

```shell
uv run python -m recapcli "读取 README.md，说明项目功能"
```

没有 Textual、或需要原来的行式交互时：

```shell
uv run python -m recapcli.cli
```

多模型把 `models.example.toml` 复制为 `models.toml`。密钥仍放 `.env` 或环境变量。`OPENAI_*` 继续作为 `default`。

```shell
uv run python -m recapcli --models
uv run python -m recapcli --profile backup
```

交互用 `/models`、`/model backup`、`/model`。`--continue` 沿用会话里保存的配置名，`--profile` 可覆盖。`context_bytes` 限制文本与附件描述的 UTF-8 JSON 字节，并与 CLI 上限取较小值，不是模型 token 窗口；图片编码单独受图片大小限制。Responses 使用 `store=false`；协议私有字段不进摘要和长期记忆。

## 常用用法

```shell
uv run python -m recapcli "读取 README.md，说明项目功能"
uv run --extra tui python -m recapcli --workspace ./your-project
uv run --extra tui python -m recapcli --continue
uv run --extra tui python -m recapcli --mode plan --allow-write --allow-exec
uv run python -m recapcli --mode multi "分析并审查这个项目的会话保存逻辑"
```

默认只读。命令以当前用户权限运行，可以改文件，不是系统沙箱。

### 图片输入和流式回答

在对话中显式附加工作区内的本地图片：

```text
@image:./screenshots/error.png 帮我分析截图里的报错
@image:<screenshots/error screen.png> 解释这张图
```

路径含空格时使用尖括号或引号。支持 PNG、JPEG、WebP、静态 GIF，一次最多 4 张；原图最多 10 MiB、2500 万像素。图片会重新编码并缩放到最长边 2048 像素，去掉原始元数据；处理后单图最多 4 MiB，每次请求全部图片合计最多 8 MiB。不读取远程图片 URL 或剪贴板。

默认配置需要先确认所用模型支持视觉，再在 RecapCLI 根目录 `.env` 或环境变量中设置 `OPENAI_IMAGES=true`；命名配置使用 `models.toml` 的 `images=true`，参考 `models.example.toml` 的 `vision` 配置。能力声明不会探测接口，也不自动更换模型。关闭图片能力的模型遇到图片会明确报错。

处理后的图片保存在工作区运行数据目录的 `attachments/`，会话和任务追溯只记录名称、大小和内容哈希。恢复会话不依赖原图路径；附件丢失或校验失败时要求重新附加。选入上下文的历史图片会再次发送给当前模型服务；压缩历史只总结已有文字，不重新观察图片。图片和文字都会产生所配置服务的用量。

交互终端与单次提问默认流式显示正文；结束时不会重复打印完整回答。`plan` 只流式展示最终汇总，`multi` 只展示父任务汇总，内部规划 JSON 和子代理正文不会穿插显示。中断或取消时标记“回答未完成”，部分回答不作为成功会话保存；工具调用必须收齐完成标记与参数后才能执行，仍遵守能力开关和审批。

不支持 SSE 的端点可设置 `OPENAI_STREAMING=false`，命名模型配置使用 `streaming=false`，改回整段输出。失败不会自动重试或切换服务。Python API 直接传入原始 SDK 客户端时保留原行为，可通过 `run_agent(..., on_stream=回调)` 显式接收 `start`、`delta`、`end`、`abort` 事件。

### 网页搜索和读取

`react` 与 `plan` 用 `--allow-web` 打开两个工具：`web_search`、`read_url`。旧脚本里的 `--allow-network` 效果相同。`multi` 不会因为这两个开关而联网。

搜索密钥放在 RecapCLI 根目录 `.env` 或环境变量中。读取 URL 不需要搜索密钥。

| 变量 | 作用 |
| --- | --- |
| `BRAVE_API_KEY` / `TAVILY_API_KEY` | 对应搜索服务的密钥 |
| `RECAP_WEB_PROVIDER` | `brave` 或 `tavily`。未设置时，有 Brave 密钥用 Brave，否则用 Tavily |
| `REVO_WEB_PROVIDER` | 旧变量，仍接受。与 `RECAP_WEB_PROVIDER` 不一致时拒绝搜索 |

查询会发给所选服务并产生其账户用量。工具不逐次审批。搜索返回标题、URL 和摘要；`read_url` 返回正文、最终地址、时间和截断标记。只访问公网 HTTP(S)，限制超时、重定向、响应大小和正文（最多 32 KiB），不运行 JavaScript，不支持登录态和二进制内容。摘要或截断正文不是全文。外部内容只是资料，其中的指令不能授权写入、执行或发送密钥。失败不自动重试；取消要等在途请求返回或超时。

```shell
uv run python -m recapcli --allow-web "搜索 Python 官方 asyncio 文档，读取相关页面并附来源链接"
```

### 人工审批

`--allow-write` 和 `--allow-exec` 打开能力后，`edit_file`、`restore_file` 和 `run_command` 仍逐次等待 `approve`、`reject` 或 `cancel`（或 `批准`、`拒绝`、`取消`）。其他文字不会批准。默认等 120 秒，可用 `--approval-timeout` 调整，且受任务剩余时间限制。

单次提问和非交互运行不会等待输入，结果为 `needs_approval`。脚本需要显式 `--yes`。批准绑定这次的参数和文件指纹；执行前若变化，批准作废。拒绝、过期、取消和失效都不写备份、不启动进程。

### Multi：仅本地只读

`--mode multi` 同时跑 `analysis` 与 `review`。两者各自保持上下文，复用同一条 ReAct 和工具链，结束后由父任务汇总。父任务即使加了 `--allow-web`、`--allow-write` 或 `--allow-exec`，子代理也只有本地读取：不联网、不改文件、不执行命令。汇总阶段不用工具。分析和审查是模型意见，不能代替测试或人工验收。

`--max-steps` 含压缩、两个子代理和汇总，默认 20，最少 3。压缩后先留一次汇总，剩余请求均分（余数给分析），未用份额不转移。`--max-tool-calls`、`--max-tool-bytes` 和 `--max-seconds` 在整个任务内共享。这些不是 token 或费用硬上限。

Ctrl+C 会停掉后续调度。Python API：`run_agent(..., mode="multi", cancel_event=threading.Event())`。已在途的同步 HTTP 会等到返回或超时（单次 SDK 超时为 30 秒与剩余时间的较小值）。会话只追加问题和最终汇总。`/task ID` 或 `--task ID --json` 可看各角色的记录。

### 代码索引

`search_symbols` 按精确名称查找 `class`、`function`、`async_function`。`search_code_index` 做 FTS5 短语检索，不是子串或语义搜索。结果含 `path`、`start_line`、`end_line` 和 `reference`。

索引在工作区 `.recapcli/retrieval/index.sqlite3`。已有 `.revocli` 或更早的 `.archer` 则原位沿用；多份并存时拒绝自动覆盖。查询前按内容哈希增量更新，并复核文件；期间变化的结果会丢弃并标 `stale_files`。只遍历工作区，跳过链接、运行数据目录、`.env`、`.env.*` 和常见密钥文件名，不实现通用敏感信息识别或 `.gitignore` 规则。只索引 UTF-8 文本；过大、二进制、超长行或完整符号会跳过。需要带 FTS5 的 SQLite。索引内容只是资料，不构成授权。

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

其余参数见 `uv run python -m recapcli --help`。

## 测试

```shell
uv run python -m unittest discover -s tests
```

测试按执行核心、存储、工具、集成和界面分组。模型接口使用模拟响应，不请求真实模型或搜索服务。

## 终端壳

默认交互入口是黑底、薄荷绿点缀的 Textual 壳：左侧为对话与输入区，右侧为模型和任务状态。回答支持 Markdown 与代码高亮。

Windows 可双击 `start.cmd`，或在项目终端运行 `.\start.cmd`：优先在独立的 Windows Terminal 打开，避免 IDE 内置终端的中文显示差异；未安装 Windows Terminal 时在当前终端启动。支持传入原有参数，例如 `.\start.cmd --continue` 或 `.\start.cmd --demo`。

```shell
uv run --extra tui python -m recapcli
uv run --extra tui python -m recapcli --workspace ./your-project --mode plan --allow-write
uv run --extra tui python -m recapcli.tui --demo
```

- 提问复用现有 `run_agent`、会话、任务记录、工具能力和审批。`--allow-write`、`--allow-exec`、`--allow-web`、`--mode`、`--yes` 与命令行含义相同。
- 对话区显示问题、回答、审批、错误和实际操作结果，隐藏模型请求次数、正常读取日志和记忆计数；完整执行记录仍可用 `/tasks`、`/task ID` 查看。回答逐段更新在同一条消息中，并跟随到底部。
- 新消息和回答自动跟随到底部，窗口变窄或恢复会话后也保持跟随；可手动上翻阅读历史，下一条消息到达时重新跟随。
- 右栏显示当前模型、工具能力、本次任务实际返回的 token 用量，以及编辑 / 恢复工具确认的修改文件；未返回的用量显示未知。模型绿点只表示本地配置齐全。LSP、MCP 尚未接入。
- 窗口少于 120 列时自动隐藏右栏，保留对话和输入区；宽窗口可用 Ctrl+G 手动切换。
- `/help` 查看命令。`/new` 新会话，`/save` 保存，`/open 会话ID` 恢复，`/history` 查看当前会话，`/sessions` 查看已保存会话，`/tasks` 查看任务。
- 编辑和命令仍逐次等待 `approve`、`reject` 或 `cancel`。
- `--demo` 只演示界面，不请求模型。
- Enter 发送，Ctrl+L 清屏（不删除已保存会话），Ctrl+G 切换状态栏，Ctrl+Q 退出并保存。
- 未安装 textual 时会提示 `uv sync --extra tui`，并给出降级入口 `python -m recapcli.cli`。
