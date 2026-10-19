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

## 从哪里读代码

按 `main → run_agent → execute_tool → read_file → run_agent` 的顺序读 `main.py`。
`run_agent` 保存 assistant 工具请求，再用相同的调用 ID 添加 tool 结果，进入下一轮模型请求。
有多个工具请求时逐一处理；工具失败也会作为结构化结果返回模型。

## 本地验证

```powershell
uv run python -m unittest discover -s tests -v
```

测试用 `httpx2.MockTransport` 模拟模型接口，并在临时工作区实际读取文件；不联网，不产生模型费用。
它验证请求与结果配对、连续工具轮次、错误回传和次数上限，不能代替真实服务接入验证。

## 当前边界

只读取工作区中的 UTF-8 文本文件，每个文件最多 64 KiB；越界路径、额外参数和未知工具返回错误。
路径检查会解析符号链接和 Windows junction，但属于应用层检查，不是 OS 沙箱，也不保证抵御并发替换路径。
每次模型请求超时 30 秒，自动重试关闭；最多请求 8 次，可通过参数调整。Ctrl+C 中断当前任务；已发送请求的远端状态和用量仍以服务端记录为准。
本版没有文件编辑、命令执行、持久化、LangChain/LangGraph 或飞书接入。

工具调用流程参考：[OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)。
