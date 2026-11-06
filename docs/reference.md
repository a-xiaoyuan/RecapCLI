# RecapCLI 进阶参考

[返回 README](../README.md)

## 自动验证与 Hooks

想让 Agent 改完自己验证，用 `--verify-cmd` 指定命令：

```sh
recapcli --allow-write --verify-cmd "python -m unittest"
```

本轮有实际编辑时，模型结束前会自动运行这个命令；失败结果交回模型继续修，最多 3 次，仍失败时任务标记为未完成。失败后模型直接回答也必须复验，通过后才能正常结束；时间或请求预算不足时以未完成结束。它只在 `react` 模式生效，不经过 shell，所以不能用管道或 `&&`，命令也不需要逐次审批。

想在工具执行前后或任务结束时运行自己的检查，用 `--hooks 路径` 加载 JSON 配置：

```json
{
  "pre_tool": [{"match": "run_command", "command": ["python", "check_command.py"]}],
  "post_tool": [{"match": "edit_.*", "command": ["python", "format_file.py"], "timeout": 60}],
  "stop": [{"command": ["python", "check_done.py"]}]
}
```

Hook 从标准输入读取事件 JSON，退出码 `0` 放行，`2` 阻止并把输出交回模型，其他情况只提示。`pre_tool` 阻止后工具不执行，`stop` 阻止后模型继续处理，最多 3 次。配置必须显式指定，不会自动读取项目文件；里面的命令以当前用户权限运行，不走逐次审批。`stop` 只在 `react` 模式生效，`--hooks` 暂不支持 `multi`。

## Skills

技能是一份写给 Agent 的操作指引，格式兼容 `SKILL.md`：一个目录放一个技能，头部写名称和描述，正文写步骤。

```markdown
---
name: release-notes
description: 用户要写发布说明时使用
---

1. 用 git log 列出上次发布以来的提交。
2. 按功能、修复分组，写成中文。
```

RecapCLI 按下面的顺序扫描，同名时靠前的生效：

1. `项目/.agents/skills/技能名/SKILL.md`
2. `项目/.claude/skills/技能名/SKILL.md`
3. 配置目录下的 `skills/技能名/SKILL.md`

系统提示词只列出名称和描述，模型判断用得上时才调用 `load_skill` 读全文，所以技能多了也不会每轮占满上下文。列表最多占 8 KiB，放不下的技能不会列出；单个文件最大 64 KiB。

输入 `/skills` 查看已发现的技能和被跳过的原因。每个任务都会记录当轮技能的名称和 sha256，可在任务记录的 `skill_versions` 里核对。

技能只是资料：不能授权写入或执行，也不替代审批，里面的脚本不会自动运行，要走 `run_command`。项目里的技能不能通过符号链接指向项目之外。目前只在 `react` 模式的主代理上生效，`plan`、`multi` 和子代理不加载；头部只解析常见写法，`allowed-tools` 等其他字段会被忽略。

## 网页工具

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

TUI 启动后输入 `/mcp` 可以查看当前连接的服务和工具名。

只想查看可用工具清单、不请求模型时：

```powershell
recapcli --mcp-tools --mcp-repo
recapcli --mcp-tools --mcp-config .\mcp-fetch.json
```

会连接服务并打印每个工具的完整名称和说明（说明中带本地权限），随后直接退出；不需要模型配置、不创建会话，交互终端也不会打开界面。

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

## 计划、协作与混合检索

`--mode plan` 支持带步骤 ID、类型及依赖的计划；独立只读步骤可以同批处理，修改和验证按依赖执行。每步保留模型核对依据，模型核对不等于测试或人工验收。

计划默认最多 12 步、2 次重规划，可用 `--plan-steps`、`--plan-replans` 调整。验证步骤必须有实际成功结束的命令，后台运行中不能登记为验证通过。

### 预算与收尾

| 项目 | 默认值 | 调整 |
| --- | --- | --- |
| 上下文窗口 | 128000 token | 模型配置 `context_tokens`，或 `--context-tokens` |
| 模型请求 | ReAct 50，Plan/Multi 60 | `--max-steps` |
| 工具调用 | 100 | `--max-tool-calls` |
| 整轮时间 | 1800 秒（最后约 60 秒留给收尾） | `--max-seconds` |
| 单次请求超时 | 120 秒 | `--request-timeout` |

上下文按 token 估算，并用服务端返回的 `prompt_tokens` 校准；工具定义也计入窗口。当前轮接近窗口的 75% 时，较早的工具结果会换成一行占位（完整内容仍在任务记录里，模型需要时重新读取），最近几条结果保留原文。单条结果放不下时只保留收据并提示缩小范围。

任何一项预算用完都不会让任务直接失败：Agent 停止调用工具，用最后一次请求汇总已完成的工作和未完成项，状态记为 `incomplete`；连这次请求也发不出去时，用本轮事实生成结束说明。

工具已有执行结果后，模型请求超时、服务端上下文超限或输出截断也会进入这条收尾路径，不重放工具。失败请求和重试都计入请求预算；鉴权和其他参数错误仍按失败处理。

涉及多个文件或多个入口的任务，模型会用 `todo_write` 维护待办清单（界面里显示为“待办”）；结束前清单还有未完成项时会被提醒一次。

`--mode multi` 默认仍是只读分析与审查。显式加上 `--allow-write` 或 `--allow-exec` 后，使用规划者、执行 Worker、独立审查和汇总流程；只读 Worker 可并发，修改和验证串行，沿用原有审批、共享预算与任务记录。审查可提出一轮剩余修正。执行团队与 CLI/TUI 复用同一运行层。

同一轮中的独立文件读取、文本搜索与网页读取可并行，结果按调用顺序交给模型，完成事实及时落盘。编辑、命令、索引更新和外部 MCP 调用不自动并行。

命令默认允许运行 300 秒，单条最多 86400 秒，前台同时受本轮剩余时间限制。模型可以用 `run_command(background=true)` 启动长构建或服务，用 `command_status` 查询终态和增量输出；`interactive=true` 使用 Windows ConPTY / POSIX PTY，保留后续输入能力。继续输入与停止操作沿用执行权限及审批。输出按上限截取，截取不会中止正常构建；每个命令最多保留最近 1 MiB 输出。后台命令在本次 RecapCLI 运行期间有效，退出时会回收，重启不会按旧 PID 重新连接或重放。

每轮自动选取与问题相关的部分仓库地图，提供真实声明位置及构建配置中的验证命令候选。模型也可调用 `repository_map` 调整范围、`find_references` 查询调用或名称使用位置。名称关联属于语法候选，未做跨模块类型、重载或动态调用解析；需要结合源码、编译或类型检查核实。缓存命中仍校验选中文件内容。

`search_code_index` 默认融合 FTS5 关键词与文件/符号名称；`mode=keyword` 只查精确短语。语义召回需要在 RecapCLI 的用户配置 `.env` 中显式设置以下参数，不会自动使用聊天模型或聊天密钥：

```dotenv
RECAPCLI_EMBEDDING_BASE_URL=https://your-provider.example/v1
RECAPCLI_EMBEDDING_MODEL=你的Embedding模型名
RECAPCLI_EMBEDDING_API_KEY=你的Embedding服务密钥
```

接口需兼容 OpenAI `/embeddings`，本地服务不需要密钥时可省略最后一项。启用后，会向该服务发送工作区中可索引的代码分块及检索问题。向量按服务、模型和文件内容缓存；每次最多补齐 256 个分块，超出时明确报告尚未建立向量的数量。分块最多 40 行、8 KiB，过大分块不做语义索引。

结果中的 `semantic_status` 区分未配置、可用、部分索引和接口不可用；接口失败回退本地检索。命中仍核验文件内容和行号；没有语义命中或索引不完整，不代表项目没有相关实现。

## 语言支持

文件读写、搜索和编辑适用于各种文本代码。语法符号定位覆盖 Python、Java、JavaScript/TypeScript（含 JSX/TSX）、Go、Rust、C/C++、C#、Kotlin、Ruby、PHP；解析器随依赖安装，运行时不下载语法。符号位置不等于完整语言服务器的类型或调用关系分析。命令执行成功与任务验收是两回事，可通过任务记录核对实际结果。
