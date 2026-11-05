"""不依赖终端框架的会话、任务和记忆命令。"""

import json

from ..config import list_model_profiles
from ..core.memory import active
from ..core.project_rules import project_rules_description
from ..core.skills import PROJECT_ROOTS, discover_skills, user_skills_dir
from ..storage.tasks import TaskStore
from .runtime import run_undo, save_session
from .changes import show_changes
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


def _show_project_version(ui, kind, entry, tasks, starts, finished):
    ui.info("项目版本：" + json.dumps(entry, ensure_ascii=False))


def _show_project_rules(ui, kind, entry, tasks, starts, finished):
    if entry.get("agent"):
        return
    ui.info("项目规则：" + project_rules_description(entry))


def _show_tool_start(ui, kind, entry, tasks, starts, finished):
    starts[entry["action_id"]] = entry
    ui.info(f"动作 {entry['call_id']} · {entry['name']} · 参数：{entry['arguments']}")


def _show_approval(ui, kind, entry, tasks, starts, finished):
    ui.info(f"审批 {entry.get('tool')} · {entry.get('outcome')} · {entry.get('reason')}")
    if entry.get("fingerprints"):
        ui.info("  指纹：" + json.dumps(entry["fingerprints"], ensure_ascii=False))


def _show_tool_result(ui, kind, entry, tasks, starts, finished):
    finished.add(entry["action_id"])
    result = entry["result"]
    ui.info(f"结果 {entry['call_id']}：" + json.dumps(result, ensure_ascii=False))


def _show_plan(ui, kind, entry, tasks, starts, finished):
    ui.info("模型计划/核对（不等于实际验收）：" + json.dumps(entry, ensure_ascii=False))


def _show_artifact(ui, kind, entry, tasks, starts, finished):
    folder = "backups" if entry["record_type"] == "backup" else "commands"
    root_name = tasks.path.parent.parent.name
    ui.info(f"操作记录（不代表已完成）：{root_name}/{folder}/{entry['record_id']}.json")


def _show_labeled_event(ui, kind, entry, tasks, starts, finished):
    ui.info(kind + "：" + json.dumps(entry, ensure_ascii=False))


def _show_subagent_event(ui, kind, entry, tasks, starts, finished):
    ui.info("子代理记录（模型意见不等于验收）：" + json.dumps(entry, ensure_ascii=False))


def _show_auto_verify(ui, kind, entry, tasks, starts, finished):
    result = "通过" if entry["success"] else f"失败（{entry['status']}，退出码 {entry['returncode']}）"
    ui.info(f"自动验证 {' '.join(entry['command'])}：{result}")


def _show_hook(ui, kind, entry, tasks, starts, finished):
    target = f" {entry['tool']}" if entry.get("tool") else ""
    result = "已阻止" if entry["blocked"] else f"{entry['status']}，退出码 {entry['returncode']}"
    ui.info(f"Hook {entry['event']}{target}：{' '.join(entry['command'])} {result}")


_TASK_EVENT_PRINTERS = {
    "project_version": _show_project_version,
    "project_rules": _show_project_rules,
    "tool_start": _show_tool_start,
    "approval": _show_approval,
    "tool_result": _show_tool_result,
    "plan": _show_plan,
    "artifact": _show_artifact,
    "error": _show_labeled_event,
    "recovery": _show_labeled_event,
    "tool_interrupted": _show_labeled_event,
    "verification": _show_labeled_event,
    "auto_verify": _show_auto_verify,
    "hook": _show_hook,
    "compaction": _show_labeled_event,
    "compaction_failed": _show_labeled_event,
    "compaction_skipped": _show_labeled_event,
    "summary_omitted": _show_labeled_event,
    "subagent_": _show_subagent_event,
}


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
        lookup = "subagent_" if kind.startswith("subagent_") else kind
        printer = _TASK_EVENT_PRINTERS.get(lookup)
        if printer is None:
            continue
        printer(ui, kind, entry, tasks, starts, finished)
    for action_id in starts.keys() - finished:
        ui.info(f"动作 {action_id}：结果未知，需核对备份/命令记录；不会自动重试。")
    if task["answer"]:
        ui.info("已返回回答：\n" + task["answer"])


def _cmd_add_dir(name, argument, ui, store, session, model, *, capabilities, approver):
    if argument:
        try:
            path = store.access.add(argument.strip("\"'"))
            ui.info(f"本次运行已授权目录：{path}；写入仍需开放能力并审批。")
        except (OSError, ValueError) as error:
            ui.error(str(error))
    else:
        ui.info("当前文件访问目录：\n" + "\n".join(str(path) for path in (store.workspace, *store.access.directories)))
    return session


def _cmd_skills(name, argument, ui, store, session, model, *, capabilities, approver):
    catalog = discover_skills(store.workspace)
    if not catalog.skills and not catalog.skipped:
        places = "、".join(f"{root}/技能名/" for root in PROJECT_ROOTS)
        ui.info(f"未发现技能。把 SKILL.md 放到 {places} 或 {user_skills_dir()}。")
        return session
    ui.info(f"技能（{len(catalog.skills)} 个，只在 react 模式提供给模型）：")
    for skill in catalog.skills:
        ui.info(f"  {skill.name} · {skill.source}")
        ui.info(f"    {skill.description}")
    for source, reason in catalog.skipped:
        ui.info(f"  已跳过 {source}：{reason}")
    return session


def _cmd_exit(name, argument, ui, store, session, model, *, capabilities, approver):
    if not save_session(store, session, ui):
        ui.info("当前消息仍保留在内存，输入 /save 重试。")
        return session
    ui.info("再见。")
    return None


def _cmd_help(name, argument, ui, store, session, model, *, capabilities, approver):
    ui.info(help_text(argument or "common"))
    return session


def _cmd_jobs(name, argument, ui, store, session, model, *, capabilities, approver):
    from ..tools.commands import list_commands
    from ..tools import execute_tool
    try:
        if name == "/jobs":
            items = list_commands(store.workspace)
            ui.info("\n".join(f"{item['id']} · {item['status']} · {' '.join(item['argv'])}" for item in items)
                    or "本次运行暂无命令。")
        else:
            arguments = argument.split(maxsplit=1)
            if not arguments or (name == "/send" and len(arguments) != 2):
                raise ValueError("用法：/job ID、/send ID 文本、/stop ID")
            tool = {"/job": "command_status", "/send": "command_input", "/stop": "stop_command"}[name]
            params = {"record_id": arguments[0]}
            if name == "/send":
                params["text"] = arguments[1] + "\n"
            result = execute_tool(tool, params, store.workspace, capabilities=capabilities,
                                  access_policy=store.access, approver=approver)
            (ui.info if result["ok"] else ui.error)(result.get("error") or result.get("summary", result.get("status", "")))
            if result.get("output"):
                ui.info(result["output"])
    except (OSError, ValueError) as error:
        ui.error(str(error))
    return session


def _cmd_config(name, argument, ui, store, session, model, *, capabilities, approver):
    ui.info(config_help())
    return session


def _cmd_changes(name, argument, ui, store, session, model, *, capabilities, approver):
    try:
        if name == "/diff":
            show_changes(ui, TaskStore(store.workspace, create=False), argument or None)
        else:
            result = run_undo(store, ui, session, model, group_id=argument or None,
                              capabilities=capabilities, approver=approver)
            (ui.info if result["ok"] else ui.error)(result.get("error") or result.get("summary", "撤销未完成"))
    except (OSError, ValueError, RuntimeError) as error:
        ui.error(f"命令执行失败：{error}")
    return session


def _cmd_save(name, argument, ui, store, session, model, *, capabilities, approver):
    if save_session(store, session, ui, allow_empty=True):
        ui.info("会话已保存。")
    return session


def _cmd_new(name, argument, ui, store, session, model, *, capabilities, approver):
    if not save_session(store, session, ui):
        ui.info("当前会话未切换，输入 /save 重试。")
        return session
    new_session = store.new_session(model)
    ui.info(f"已开始新会话：{new_session['id']}")
    return new_session


def _cmd_session(name, argument, ui, store, session, model, *, capabilities, approver):
    ui.info(f"会话 {session['id']} · {len(session['messages'])} 条消息 · {store.path_for(session['id'])}")
    return session


def _cmd_sessions(name, argument, ui, store, session, model, *, capabilities, approver):
    try:
        sessions = store.list_sessions()
        if not sessions:
            ui.info("暂无已保存的会话。")
        for saved in sessions:
            ui.info(f"  {saved['id']} · {saved['messages']} 条消息 · {saved['model']} · {saved['updated_at']}")
    except (OSError, ValueError) as error:
        ui.error(f"命令执行失败：{error}")
    return session


def _cmd_history(name, argument, ui, store, session, model, *, capabilities, approver):
    show_history(ui, session)
    return session


def _cmd_summary(name, argument, ui, store, session, model, *, capabilities, approver):
    summary = session.get("summary")
    ui.info(json.dumps(summary, ensure_ascii=False, indent=2) if summary else "当前会话尚无摘要。")
    return session


def _cmd_tasks(name, argument, ui, store, session, model, *, capabilities, approver):
    try:
        if name == "/task" and not argument:
            raise ValueError("用法：/task ID")
        show_tasks(ui, TaskStore(store.workspace, create=False), argument if name == "/task" else None)
    except (OSError, ValueError) as error:
        ui.error(f"命令执行失败：{error}")
    return session


def _cmd_task_verify(name, argument, ui, store, session, model, *, capabilities, approver):
    try:
        arguments = argument.split(maxsplit=3)
        if len(arguments) != 4:
            raise ValueError("用法：/task-verify 任务ID passed|failed 命令ID 验收范围与依据")
        tasks = TaskStore(store.workspace, create=False)
        tasks.verify(*arguments)
        ui.info("人工验收已记录，执行终态保持原值；此结论仅覆盖填写的验收范围。")
        try:
            candidate = store.propose_task_memory(tasks.get(arguments[0]))
            ui.info(f"已生成经验候选 {candidate['id']}；/memory candidates 查看，/memory accept ID 接纳。")
        except (OSError, ValueError) as error:
            ui.error(f"人工验收已保存，经验候选未生成：{error}")
    except (OSError, ValueError) as error:
        ui.error(f"命令执行失败：{error}")
    return session


def _cmd_memory(name, argument, ui, store, session, model, *, capabilities, approver):
    try:
        if name == "/memory-search" and not argument:
            raise ValueError("用法：/memory search 问题")
        show_memory(ui, store, query=argument if name == "/memory-search" else None,
                    candidates=name == "/memory-candidates")
    except (OSError, ValueError) as error:
        ui.error(f"命令执行失败：{error}")
    return session


def _cmd_remember(name, argument, ui, store, session, model, *, capabilities, approver):
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
    return session


def _cmd_memory_status(name, argument, ui, store, session, model, *, capabilities, approver):
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
    return session


def _cmd_memory_propose(name, argument, ui, store, session, model, *, capabilities, approver):
    try:
        task_id, _, text = argument.partition(" ")
        task = TaskStore(store.workspace, create=False).get(task_id)
        item = store.propose_task_memory(task, text or None)
        ui.info(f"经验候选 {item['id']} 已保存，尚未进入模型上下文。")
    except (OSError, ValueError) as error:
        ui.error(f"命令执行失败：{error}")
    return session


def _cmd_memory_update(name, argument, ui, store, session, model, *, capabilities, approver):
    try:
        key, separator, text = argument.partition(" ")
        if not separator:
            raise ValueError("用法：/memory update ID 文本")
        item = store.update_memory(key, text)
    except (OSError, ValueError) as error:
        ui.error(f"命令执行失败：{error}")
    else:
        ui.info(f"已更新记忆（{item['id']}）：{item['text']}")
    return session


def _cmd_forget(name, argument, ui, store, session, model, *, capabilities, approver):
    try:
        removed = store.remove_memory(argument)
    except (OSError, ValueError) as error:
        ui.error(f"命令执行失败：{error}")
    else:
        ui.info(f"已删除 {removed} 条记忆。")
    return session


_COMMANDS: dict[str, callable] = {
    "/add-dir": _cmd_add_dir,
    "/skills": _cmd_skills,
    "/exit": _cmd_exit,
    "/quit": _cmd_exit,
    "/help": _cmd_help,
    "/jobs": _cmd_jobs,
    "/job": _cmd_jobs,
    "/send": _cmd_jobs,
    "/stop": _cmd_jobs,
    "/config": _cmd_config,
    "/diff": _cmd_changes,
    "/undo": _cmd_changes,
    "/save": _cmd_save,
    "/new": _cmd_new,
    "/session": _cmd_session,
    "/sessions": _cmd_sessions,
    "/history": _cmd_history,
    "/summary": _cmd_summary,
    "/tasks": _cmd_tasks,
    "/task": _cmd_tasks,
    "/task-verify": _cmd_task_verify,
    "/memory": _cmd_memory,
    "/memory-search": _cmd_memory,
    "/memory-candidates": _cmd_memory,
    "/remember": _cmd_remember,
    "/memory-expire": _cmd_memory_status,
    "/memory-activate": _cmd_memory_status,
    "/memory-accept": _cmd_memory_status,
    "/memory-reject": _cmd_memory_status,
    "/memory-propose": _cmd_memory_propose,
    "/memory-update": _cmd_memory_update,
    "/forget": _cmd_forget,
}


# /memory 的子命令；原来的 /memory-xxx 写法继续可用，只是不再列入帮助。
MEMORY_ACTIONS = {"search", "update", "expire", "activate", "candidates", "propose", "accept", "reject"}


def handle_command(command, ui, store, session, model, *, capabilities=("read",), approver=None):
    """处理会话命令；返回新的 session 或退出标记。"""
    parts = command.split(maxsplit=1)
    name = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    if name == "/memory" and argument:
        action, _, rest = argument.partition(" ")
        if action.lower() not in MEMORY_ACTIONS:
            ui.error("用法：/memory [search|update|expire|activate|candidates|propose|accept|reject] ...")
            return session
        name, argument = f"/memory-{action.lower()}", rest.strip()
    handler = _COMMANDS.get(name)
    if handler is None:
        ui.error(f"未知命令：{name}，输入 /help 查看命令。")
        return session
    return handler(name, argument, ui, store, session, model, capabilities=capabilities, approver=approver)


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
