# RecapCLI 进阶参考

[返回 README](../README.md)

## 自动验证与 Hooks

想让 Agent 改完自己验证，用 `--verify-cmd` 指定命令：

```sh
recapcli --allow-write --verify-cmd "python -m unittest"
```

本轮有实际编辑或执行过可能改动产物的命令时，在结束 Hook 放行后运行验证。命令尚在运行时会先要求核对，预算用完则标记未完成。验证失败会交回模型继续修，最多 3 次，通过后才能正常结束。`--verify-cmd` 只支持 `react` 模式，直接执行参数数组，不经过 shell。用户显式指定的命令无需逐次审批。

想在工具执行前后或任务结束时运行自己的检查，用 `--hooks 路径` 加载 JSON 配置：

```json
{
  "pre_tool": [{"match": "run_command", "command": ["python", "check_command.py"]}],
  "post_tool": [{"match": "edit_.*", "command": ["python", "format_file.py"], "timeout": 60}],
  "stop": [{"command": ["python", "check_done.py"]}]
}
```

Hook 从标准输入读取事件 JSON，退出码 `0` 放行，`2` 阻止并把输出交回模型，其他情况只提示。`pre_tool` 阻止后工具不执行，`stop` 阻止后模型继续处理，最多 3 次。配置必须显式指定，不会自动读取项目文件；里面的命令以当前用户权限运行，不走逐次审批。`stop` 只在 `react` 模式生效，`--hooks` 暂不支持 `multi`。

## 需求验收

用 `--accept 路径` 提供本轮要满足的具体条件。行式 CLI、交互 CLI 和 TUI 都支持 `react` 模式。下面的 JSON 逐项检查实际入口的退出码和输出。

```json
{
  "checks": [
    {
      "id": "cli-help",
      "description": "命令行帮助显示需求验收选项",
      "argv": ["python", "-m", "recapcli.cli", "--help"],
      "exit_code": 0,
      "output_contains": ["--accept"]
    }
  ]
}
```

```sh
recapcli --accept /path/to/acceptance.json "补齐命令行入口和帮助"
```

`checks` 最多 12 项，`id` 必须唯一，`exit_code` 默认 `0`。可选的 `output_contains` 和 `output_excludes` 对合并后的标准输出和错误输出做区分大小写的字面匹配。输出截断时，依赖输出的断言记为失败。

配置在启动时读取。检查在工作区以当前用户权限执行，使用显式指定的 `argv`，无需逐次审批。请使用可信的验收配置和脚本。让脚本位于 Agent 无权修改的位置，并让检查保持待验产物内容稳定。

待办、在途命令、结束 Hook 和自动验证放行后，执行全部验收项。失败项和输出摘要交回模型，最多三轮，仍有失败或未运行项时任务以未完成结束。未配置验收时沿用原有任务终态。用 `/task ID` 或 `recapcli --task ID --json` 查看逐项结果、命令 ID 和实际输出。自动验证的命令 ID 可以用于 `/task-verify` 登记人工验收。

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

通过 `--mcp-config 路径` 显式配置外部服务。`--mcp-repo` 只启动内置仓库服务。下面用 stdio 接入 Fetch MCP，让模型读取公开技术文档，无需额外 API Key。[Fetch MCP 服务](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)

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

本地 MCP 子进程以当前用户权限运行，请先核对配置中的启动命令和工具用途。子进程需要的服务密钥在该服务的 `env` 中显式配置。含密钥的配置请保存在用户目录。

```powershell
recapcli --mcp-repo --mcp-config .\mcp-fetch.json
```

### 连接远程 MCP 服务

用 Streamable HTTP 连接独立运行的 MCP 服务。每个服务在本地启动命令 `command` 和远程地址 `url` 中选填一项，同一配置文件可以登记两类服务。`cwd` 和 `env` 用于本地命令。

将以下配置保存为 `mcp-remote.json`，把 `url` 和工具名替换成实际值。

```json
{
  "servers": {
    "catalog": {
      "url": "https://mcp.example.com/mcp",
      "bearer_token_env": "CATALOG_MCP_TOKEN",
      "timeout": 15,
      "max_result_bytes": 65536,
      "tool_permissions": {"search": "read"}
    }
  }
}
```

在环境中设置 `CATALOG_MCP_TOKEN` 后，用以下命令查看工具。`bearer_token_env` 填环境变量名。公开服务可以省略此字段。

```powershell
recapcli --mcp-tools --mcp-config .\mcp-remote.json
```

远程地址使用 HTTPS，本机回环地址可用 HTTP。工具仍遵守本地授权和审批。发送后遇到断线或超时会提示执行结果未知，需先核对服务端状态再决定是否重试。会话过期后会在下一次工具调用前重新连接。

当前支持 `2025-06-18` 协议下同一端点的 JSON 响应和请求内 SSE 流。旧版 HTTP+SSE 的双端点兼容是后续项。[MCP 传输规范](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)

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

工具已有执行结果后，模型请求超时、连接中断、HTTP 429 限流、服务端 5xx、上下文超限或输出截断也会进入这条收尾路径，不重放工具。失败请求和重试都计入请求预算；鉴权和其他参数错误仍按失败处理。

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
