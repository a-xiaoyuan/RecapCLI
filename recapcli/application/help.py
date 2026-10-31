"""命令目录共用于行式帮助与终端输入补全。"""

from ..config import ENV_FILE, MODELS_FILE


# 名称、参数提示、说明、分类；方括号包围整个参数时可省略。
COMMANDS = (
    ("/model", "[名称]", "选择或切换模型", "models"),
    ("/config", "", "配置模型、接口与密钥", "models"),
    ("/new", "", "保存并开始新会话", "sessions"),
    ("/open", "ID", "恢复已保存的会话（TUI）", "sessions"),
    ("/tasks", "", "查看最近任务", "tasks"),
    ("/diff", "[任务ID]", "查看本轮差异、原有改动与命令结果", "tasks"),
    ("/help", "[分类]", "查看命令与快捷键", "common"),
    ("/exit", "", "保存并退出", "common"),
    ("/models", "[筛选词]", "筛选本地模型配置", "models"),
    ("/sessions", "", "列出已保存会话", "sessions"),
    ("/session", "", "查看当前会话与保存位置", "sessions"),
    ("/save", "", "保存当前会话", "sessions"),
    ("/history", "", "查看最近消息", "sessions"),
    ("/compact", "", "请求模型压缩旧历史，原文保留", "sessions"),
    ("/summary", "", "查看历史摘要", "sessions"),
    ("/task", "ID", "查看工具事实与命令结果", "tasks"),
    ("/undo", "[组ID]", "审批后撤销编辑组，需要 --allow-write", "tasks"),
    ("/task-verify", "ID passed|failed 命令ID 说明", "登记人工验收", "tasks"),
    ("/memory", "", "查看长期记忆", "memory"),
    ("/remember", "[preference|fact] 文本", "添加偏好或项目事实", "memory"),
    ("/memory-search", "问题", "检索有效记忆", "memory"),
    ("/memory-update", "ID 文本", "更新记忆", "memory"),
    ("/memory-expire", "ID", "标记记忆失效", "memory"),
    ("/memory-activate", "ID", "恢复失效记忆", "memory"),
    ("/forget", "ID|all", "删除记忆", "memory"),
    ("/memory-candidates", "", "查看经验候选", "memory"),
    ("/memory-propose", "任务ID [文本]", "从任务提炼候选", "memory"),
    ("/memory-accept", "ID", "接纳经验候选", "memory"),
    ("/memory-reject", "ID", "拒绝经验候选", "memory"),
)
COMMON = {"/model", "/config", "/new", "/open", "/tasks", "/diff", "/help", "/exit"}
GROUPS = {"common": "常用", "models": "模型", "sessions": "会话", "tasks": "任务与审批", "memory": "记忆", "keys": "快捷键"}
KEYS = """Enter 发送 · Shift+Enter / Ctrl+J 换行
↑↓ 选择命令 · Tab 补全 · Esc 收起列表
Esc 停止本轮 · Ctrl+Q 保存退出（忙时等待在途操作返回）
Ctrl+G 任务详情与用量 · Ctrl+L 清屏
Ctrl+End 回到底部 · Alt+PageUp / PageDown 翻阅历史
F2 选择模型 · F3 模型设置"""


def command_items(query="", topic="all"):
    items = [item for item in COMMANDS if topic == "all" or
             (item[0] in COMMON if topic == "common" else item[3] == topic)]
    query = query.casefold().lstrip("/")
    return [item for item in items if not query or item[0][1:].startswith(query) or query in item[2]]


def help_text(topic="common"):
    if topic == "keys":
        return KEYS
    if topic not in GROUPS and topic != "all":
        return f"没有帮助分类 {topic}。分类：" + " / ".join(GROUPS)
    lines = [GROUPS.get(topic, "全部命令")]
    lines.extend(f"  {name + (' ' + args if args else ''):<29} {description}"
                 for name, args, description, _ in command_items(topic=topic))
    lines.append("\n/help models · /help sessions · /help tasks · /help memory · /help keys · /help all")
    return "\n".join(lines)


def config_help():
    return (f"模型设置\nTUI 输入 /config 打开设置，/model 选择模型。\n"
            f"配置列表：{MODELS_FILE}\n密钥：{ENV_FILE}\n"
            "环境变量优先于 .env；models.toml 的 default 决定默认配置。\n"
            "基础配置也可用 OPENAI_API_KEY / OPENAI_MODEL / OPENAI_BASE_URL。\n"
            "查看：/models；切换：/model 配置名；启动指定：--profile 配置名。")
