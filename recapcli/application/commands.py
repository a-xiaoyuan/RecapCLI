"""不依赖终端框架的会话、任务和记忆命令。"""

import json

from ..config import list_model_profiles
from ..core.memory import active
from ..storage.tasks import TaskStore
from .runtime import save_session
from .help import help_text, config_help


def show_history(ui, session):
    messages = session["messages"]
    if not messages:
        ui.info("当前会话还没有消息。")
        return
    ui.info(f"会话 {session['id']}，共 {len(messages)} 条消息，最近 10 条：")
    for message in messages[-10:]:
        role = message.get("role")
        if role == "tool":
            preview = "（工具结果）"
        else:
            preview = " ".join(str(message.get("content") or "").split())[:60]
        ui.info(f"  {role}: {preview}")


def show_memory(ui, store, *, query=None, candidates=False):
    items = store.search_memory(query) if query is not None else store.memory_items()
    if candidates:
        items = [item for item in items if item.get("status") == "candidate"]
    if not items:
        ui.info("暂无长期记忆。用 /remember 文本 添加。")
        return
    ui.info(f"长期记忆（{len(items)} 条）：")
    for item in items:
        state = item.get("status", "active")
        if state == "active" and not active(item):
            state = "expired"
        ui.info(f"  {item['id']} · {item.get('kind', 'fact')} · {state} · {item['text']}")
        ui.info("    来源：" + json.dumps(item.get("source", {"type": "legacy"}), ensure_ascii=False)
                + f" · 更新：{item.get('updated_at', '未知')} · 失效时间：{item.get('expires_at') or '无'}")


def show_tasks(ui, tasks, task_id=None, *, as_json=False):
    """查看本地事实不请求模型，也不修改任务。"""
    data = tasks.get(task_id) if task_id else tasks.list_tasks()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if task_id is None:
        if not data:
            ui.info("暂无任务记录。")
        for task in data:
            prompt = " ".join(task["prompt"].split())[:60]
            ui.info(f"{task['id']} · {task['status']} · 验收 {task['verification_status']} · {task['mode']} · {prompt}")
        return
    task = data
    verification = {"unverified": "未验证", "passed": "人工验收通过", "failed": "人工验收失败"}[task["verification_status"]]
    ui.info(f"任务 {task['id']}\n目标：{task['prompt']}\n执行终态：{task['status']}\n"
            f"任务验收：{verification}（范围以验收说明为准，模型核对另行记录）\n"
            f"停止原因：{task['stop_reason'] or '尚未结束'}\n会话：{task['session_id']}\n"
            f"工作区：{task['workspace']}\n模式：{task['mode']} · 模型：{task['model']}\n"
            f"开始：{task['started_at']} · 结束：{task['ended_at'] or '未知'}\n"
            f"模型请求：{task['model_requests']} · 用量：{json.dumps(task['usage'], ensure_ascii=False)}")
    starts = {}
    finished = set()
    for event in task["events"]:
        kind, entry = event["kind"], event["data"]
        if kind == "project_version":
            ui.info("项目版本：" + json.dumps(entry, ensure_ascii=False))
        elif kind == "tool_start":
            starts[entry["action_id"]] = entry
            ui.info(f"动作 {entry['call_id']} · {entry['name']} · 参数：{entry['arguments']}")
        elif kind == "approval":
            ui.info(f"审批 {entry.get('tool')} · {entry.get('outcome')} · {entry.get('reason')}")
            if entry.get("fingerprints"):
                ui.info("  指纹：" + json.dumps(entry["fingerprints"], ensure_ascii=False))
        elif kind == "tool_result":
            finished.add(entry["action_id"])
            result = entry["result"]
            ui.info(f"结果 {entry['call_id']}：" + json.dumps(result, ensure_ascii=False))
        elif kind == "plan":
            ui.info("模型计划/核对（不等于实际验收）：" + json.dumps(entry, ensure_ascii=False))
        elif kind == "artifact":
            folder = "backups" if entry["record_type"] == "backup" else "commands"
            root_name = tasks.path.parent.parent.name
            ui.info(f"操作记录（不代表已完成）：{root_name}/{folder}/{entry['record_id']}.json")
        elif kind in ("error", "recovery", "tool_interrupted", "verification"):
            ui.info(kind + "：" + json.dumps(entry, ensure_ascii=False))
        elif kind in ("compaction", "compaction_failed", "compaction_skipped", "summary_omitted"):
            ui.info(kind + "：" + json.dumps(entry, ensure_ascii=False))
        elif kind.startswith("subagent_"):
            ui.info("子代理记录（模型意见不等于验收）：" + json.dumps(entry, ensure_ascii=False))
    for action_id in starts.keys() - finished:
        ui.info(f"动作 {action_id}：结果未知，需核对备份/命令记录；不会自动重试。")
    if task["answer"]:
        ui.info("已返回回答：\n" + task["answer"])


def handle_command(command, ui, store, session, model):
    """处理会话命令；返回新的 session 或退出标记。"""
    parts = command.split(maxsplit=1)
    name = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    if name in {"/exit", "/quit"}:
        if not save_session(store, session, ui):
            ui.info("当前消息仍保留在内存，输入 /save 重试。")
            return session
        ui.info("再见。")
        return None
    if name == "/help":
        ui.info(help_text(argument or "common"))
    elif name == "/config":
        ui.info(config_help())
    elif name == "/save":
        if save_session(store, session, ui, allow_empty=True):
            ui.info("会话已保存。")
    elif name == "/new":
        if not save_session(store, session, ui):
            ui.info("当前会话未切换，输入 /save 重试。")
            return session
        new_session = store.new_session(model)
        ui.info(f"已开始新会话：{new_session['id']}")
        return new_session
    elif name == "/session":
        ui.info(f"会话 {session['id']} · {len(session['messages'])} 条消息 · {store.path_for(session['id'])}")
    elif name == "/sessions":
        try:
            sessions = store.list_sessions()
            if not sessions:
                ui.info("暂无已保存的会话。")
            for saved in sessions:
                ui.info(f"  {saved['id']} · {saved['messages']} 条消息 · {saved['model']} · {saved['updated_at']}")
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
    elif name == "/history":
        show_history(ui, session)
    elif name == "/summary":
        summary = session.get("summary")
        ui.info(json.dumps(summary, ensure_ascii=False, indent=2) if summary else "当前会话尚无摘要。")
    elif name in {"/tasks", "/task"}:
        try:
            if name == "/task" and not argument:
                raise ValueError("用法：/task ID")
            show_tasks(ui, TaskStore(store.workspace, create=False), argument if name == "/task" else None)
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
    elif name == "/task-verify":
        try:
            arguments = argument.split(maxsplit=3)
            if len(arguments) != 4:
                raise ValueError("用法：/task-verify 任务ID passed|failed 命令ID 验收范围与依据")
            tasks = TaskStore(store.workspace, create=False)
            tasks.verify(*arguments)
            ui.info("人工验收已记录，执行终态保持原值；此结论仅覆盖填写的验收范围。")
            try:
                candidate = store.propose_task_memory(tasks.get(arguments[0]))
                ui.info(f"已生成经验候选 {candidate['id']}；/memory-candidates 查看，/memory-accept ID 接纳。")
            except (OSError, ValueError) as error:
                ui.error(f"人工验收已保存，经验候选未生成：{error}")
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
    elif name in {"/memory", "/memory-search", "/memory-candidates"}:
        try:
            if name == "/memory-search" and not argument:
                raise ValueError("用法：/memory-search 问题")
            show_memory(ui, store, query=argument if name == "/memory-search" else None,
                        candidates=name == "/memory-candidates")
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
    elif name == "/remember":
        try:
            kind, separator, text = argument.partition(" ")
            if kind in {"preference", "fact"} and separator:
                item = store.add_memory(text, kind=kind)
            else:
                item = store.add_memory(argument)
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
        else:
            ui.info(f"已记住（{item['id']}）：{item['text']}")
    elif name in {"/memory-expire", "/memory-activate", "/memory-accept", "/memory-reject"}:
        try:
            status = {"/memory-expire": "expired", "/memory-activate": "active",
                      "/memory-accept": "active", "/memory-reject": "rejected"}[name]
            if name == "/memory-accept":
                candidate = next((item for item in store.memory_items() if item["id"] == argument), None)
                if candidate is None or candidate.get("status") != "candidate":
                    raise ValueError("只能接纳存在的经验候选")
            item = store.set_memory_status(argument, status)
            ui.info(f"记忆 {item['id']} 状态：{item['status']}；来源与验收范围保留。")
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
    elif name == "/memory-propose":
        try:
            task_id, _, text = argument.partition(" ")
            task = TaskStore(store.workspace, create=False).get(task_id)
            item = store.propose_task_memory(task, text or None)
            ui.info(f"经验候选 {item['id']} 已保存，尚未进入模型上下文。")
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
    elif name == "/memory-update":
        try:
            key, separator, text = argument.partition(" ")
            if not separator:
                raise ValueError("用法：/memory-update ID 文本")
            item = store.update_memory(key, text)
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
        else:
            ui.info(f"已更新记忆（{item['id']}）：{item['text']}")
    elif name == "/forget":
        try:
            removed = store.remove_memory(argument)
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
        else:
            ui.info(f"已删除 {removed} 条记忆。")
    else:
        ui.error(f"未知命令：{name}，输入 /help 查看命令。")
    return session


def show_models(ui, current=None, pattern="*"):
    profiles = list_model_profiles()
    default = next(spec["name"] for spec in profiles if spec["default"])
    ui.info(f"默认配置：{default}。--profile NAME 可覆盖；配置名 default 不保证是当前默认。")
    selected = [spec for spec in profiles if pattern == "*" or pattern.casefold() in
                " ".join((spec["name"], spec["model"], spec["protocol"])).casefold()]
    if not selected:
        ui.info("没有匹配的模型配置。")
    for spec in selected:
        marker = "当前" if spec["name"] == current else ("默认" if spec["default"] else "可选")
        ready = "已配置" if spec["configured"] else "缺少模型或密钥配置"
        capabilities = "/".join(name for name in ("tools", "images", "streaming") if spec[name]) or "纯文本"
        ui.info(f"{marker} · {spec['name']} · {spec['protocol']} · {spec['model'] or '未设置模型'} · {ready} · {capabilities}")
