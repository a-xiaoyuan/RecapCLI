# Archer

第一步：最小 ReAct 循环。只有一个 `read_file` 工具；一次任务的消息保存在内存中。

```text
用户问题 → 模型请求工具 → Python 读取文件 → 工具结果加入历史 → 再次请求模型 → 回答
```

## 运行

使用 Python 3.12+ 和 uv。在 PowerShell 中执行：

```powershell
cd C:\Users\26779\Desktop\Archer
uv sync
```

在 Archer 项目目录的 `.env` 中填写配置（该文件已被 Git 忽略）：

```dotenv
OPENAI_API_KEY=你的Key
OPENAI_MODEL=你的模型ID
OPENAI_BASE_URL=https://你的服务地址/v1
```

然后运行：

```powershell
uv run python main.py "读取 README.md，说明这个项目如何运行"
```

也可以继续通过 PowerShell 环境变量配置；环境变量优先于 `.env`：

```powershell
$env:OPENAI_API_KEY = "你的 Key"
$env:OPENAI_MODEL = "你的模型 ID"
# 使用第三方兼容服务时设置其 API 根地址，通常以 /v1 结尾：
$env:OPENAI_BASE_URL = "https://你的服务地址/v1"
uv run python main.py "读取 README.md，说明这个项目如何运行"
```

使用 OpenAI 官方地址时不设置 `OPENAI_BASE_URL`。程序只读取 Archer 自身目录的 `.env`，不读取目标工作区的配置。`read_file` 工具禁止读取 Archer 的这份凭据配置。
模型与端点必须支持 **Chat Completions 的 function tools / tool_calls**；仅支持 Responses 的端点不适用于本版。
运行上面的提问会真实请求配置的模型服务，可能产生费用。Key 保存在本机 `.env` 或环境变量中，不要提交到 Git。

指定其他工作区：

```powershell
uv run python main.py "读取 README.md 并概括项目" --workspace C:\你的项目 --max-steps 8
```

省略问题时会提示输入。本版每次运行处理一个问题，不提供跨任务会话。
终端显示模型请求次数、工具名称和成功或失败状态，最后显示回答。
提问使用薄荷绿、回答标签使用淡紫色，工具状态为青色，成功为绿色、错误为粉红色。
回答由 Rich 渲染 Markdown：标题、粗体、行内代码和代码块都有对应样式，代码支持
语法高亮。关闭颜色或重定向输出时保留原始 Markdown 文本。

启动时显示软萌小幽灵：薄荷白身体、豆豆眼、粉色腮红、挥动的小手和小星星。
可预览启动画面及示例对话，无需配置模型，示例不会读取文件或请求模型：

```powershell
uv run python main.py --preview
# 手动开启整个界面的配色：
uv run python main.py --preview --color always
```

面板保留青色边框和薄荷白小幽灵，最多 96 列。按窗口的**宽度与高度**选择左右或上下
布局，空间不足时等比缩小角色，并为后续输入留出空间；极窄、极矮窗口使用简洁文本。
中文按显示列宽对齐，长工作区路径保留首尾，方便辨认当前项目。

- **Windows Terminal / PowerShell**：自动检测终端，传统 Windows 控制台会临时启用
  ANSI 输出，打印结束后恢复原有模式；不支持时回退为纯文本。
- **PyCharm**：识别 `PYCHARM_HOSTED=1` 的 Run 控制台。需要终端模拟时，在运行配置
  中启用 **Emulate terminal in output console**；具体选项见
  [PyCharm 官方运行配置说明](https://www.jetbrains.com/help/pycharm/run-debug-configuration-python.html)。
- 重定向到文件、设置 `NO_COLOR` 或 `TERM=dumb` 时自动使用纯文本；旧编码不能表示
  像素字符时也会回退，避免装饰面板阻断任务。

也可以手动指定显示方式，以下命令均不请求模型：

```powershell
uv run python main.py --preview --color always  # 整个界面始终开启配色，可覆盖 NO_COLOR
uv run python main.py --preview --color never   # 整个界面使用纯文本
uv run python main.py --preview --banner off    # 关闭启动面板，仍显示示例对话
```

`--banner` 同样适用于正常提问，默认 `auto`。终端提供真实尺寸时自动适配；PyCharm
等环境无法提供尺寸时默认按 80 列、24 行布局，也可通过 `COLUMNS`、`LINES` 指定。
`--color auto|always|never` 同时控制对话配色和默认启动面板；显式指定 `--banner`
可单独覆盖面板显示。配色预览标明演示内容，不代表真实模型或工具调用结果。

## 从哪里读代码

按 `main → run_agent → execute_tool → read_file → run_agent` 的顺序读 `main.py`。
`run_agent` 保存 assistant 工具请求，再用相同的调用 ID 添加 tool 结果，进入下一轮模型请求。
有多个工具请求时逐一处理；工具失败也会作为结构化结果返回模型。

学习 Agent 时优先看 `main.py` 的这条调用链。`terminal_art.py` 只负责启动画面的
像素和终端布局，`terminal_ui.py` 负责提问、状态与回答配色，可以暂时跳过；
先知道 `print_banner()` 显示面板、`TerminalUI` 显示对话即可。

## 本地验证

```powershell
uv run python -m unittest discover -s tests -v
```

测试用 `httpx2.MockTransport` 模拟模型接口，并在临时工作区实际读取文件；不联网，不产生模型费用。
它验证请求与结果配对、连续工具轮次、错误回传和次数上限，不能代替真实服务接入验证。
面板测试另外覆盖不同窗口宽高、中文和长路径、PyCharm 输出流、重定向与旧编码回退，
以及 `--preview` 不读取模型配置、不创建模型客户端。
对话显示测试覆盖 Markdown/代码高亮、状态配色、中文窄窗口折行、纯文本回退、
错误输出通道以及 CLI 实际接入显示层。

## 当前边界

只读取工作区中的 UTF-8 文本文件，每个文件最多 64 KiB；越界路径、额外参数和未知工具返回错误。
路径检查会解析符号链接和 Windows junction，但属于应用层检查，不是 OS 沙箱，也不保证抵御并发替换路径。
每次模型请求超时 30 秒，自动重试关闭；最多请求 8 次，可通过参数调整。Ctrl+C 中断当前任务；已发送请求的远端状态和用量仍以服务端记录为准。
本版没有文件编辑、命令执行、持久化、LangChain/LangGraph 或飞书接入。

工具调用流程参考：[OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)。
