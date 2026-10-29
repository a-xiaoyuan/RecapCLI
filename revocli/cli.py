"""命令行入口、交互循环与用户命令。"""

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
import time

from openai import APIError, OpenAI

from .config import ConfigError, load_config
from .core.agent import AgentLimits, run_agent
from .core.memory import active
from .storage.session import SessionStore
from .storage.tasks import TaskStore, project_version
from .tools.approval import InteractiveApprover, approve_all, fail_closed
from .ui.terminal_art import print_banner
from .ui.terminal_ui import TerminalUI

HELP_TEXT = """命令：
  /exit, /quit    退出并保存会话
  /help           显示本帮助
  /save           立即保存当前会话
  /new            保存当前会话并开始新会话
  /session        显示当前会话 ID、消息数与保存路径
  /sessions       列出已保存的会话
  /history        查看最近消息
  /compact        压缩较早的完整轮次（请求模型，原文保留）
  /summary        查看摘要、覆盖范围和来源任务
  /tasks          列出最近任务（含失败与取消）
  /task ID        查看任务事实、命令结果与模型核对
  /task-verify ID passed|failed 命令ID 说明  登记有命令证据的人工验收
  /memory         查看长期记忆
  /remember [preference|fact] 文本  添加偏好或项目事实，默认 fact
  /memory-update ID 文本  更新记忆，保留原 ID
  /memory-search 问题  检索当前有效记忆及来源
  /memory-expire ID  将记忆标为失效
  /memory-activate ID  恢复失效记忆
  /memory-candidates  查看任务经验候选
  /memory-propose 任务ID [文本]  从已结束任务提炼候选
  /memory-accept ID  接纳经验候选
  /memory-reject ID  拒绝候选
  /forget ID|all  删除一条或全部长期记忆
审批询问时只认 approve、reject、cancel（或 批准、拒绝、取消）；其他文字不会批准"""


def save_session(store, session, ui, *, allow_empty=False):
    if not allow_empty and not session["messages"]:
        return True
    try:
        store.save(session)
    except (OSError, ValueError) as error:
        ui.error(f"会话未保存：{error}")
        return False
    except KeyboardInterrupt:
        ui.error("会话保存已取消，消息尚未保存。")
        return False
    return True


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


def run_turn(client, model, workspace, store, ui, session, prompt, *,
             max_steps=None, memory_enabled=True, limits=None, capabilities=("read",), mode="react",
             auto_compact=True, compact_only=False, cancel_event=None, approver=None):
    """先落任务，再执行；失败轨迹独立于成功会话，核心不依赖存储。"""
    limits = limits or AgentLimits()
    capabilities = tuple(sorted(capabilities))
    tasks = TaskStore(workspace)
    if compact_only:
        max_steps = 1
    task_id = tasks.start(session["id"], prompt, model, "compact" if compact_only else mode, {
        "history_messages": len(session["messages"]), "capabilities": capabilities,
        "limits": asdict(limits), "max_steps": max_steps if max_steps is not None else (20 if mode in ("plan", "multi") else 8),
        "provider_host": str(getattr(getattr(client, "base_url", None), "host", "unknown")),
        "experience_versions": [], "skill_versions": [],
        "auto_compact": auto_compact, "summary_covered_messages": (session.get("summary") or {}).get("covered_messages", 0),
    })
    started = time.monotonic()

    def trace(kind, data):
        try:
            tasks.append(task_id, kind, data)
        except (OSError, ValueError) as error:
            raise RuntimeError(f"任务 {task_id} 的记录失败，停止后续执行：{error}") from error

    try:
        ui.info(f"任务 ID：{task_id}（/task ID 查看事实）")
        trace("project_version", project_version(Path(workspace).resolve()))
        memory_text = ""
        memory_ids = []
        memory_sources = []
        if memory_enabled and not compact_only:
            memory = store.memory_context(limit=min(3000, limits.max_context_bytes // 12), query=prompt)
            memory_text = memory.text
            memory_ids = list(memory.selected_ids)
            memory_sources = list(memory.sources)
            if memory.selected_ids or memory.omitted_count:
                ui.info(f"长期记忆：本轮选用 {len(memory.selected_ids)} 条，省略 {memory.omitted_count} 条。")
        trace("context", {"memory_ids": memory_ids, "memory_text": memory_text, "memory_sources": memory_sources})
        result = run_agent(client, model, prompt, workspace, history=session["messages"],
                           memory_text=memory_text, max_steps=max_steps, limits=limits, on_event=ui.event,
                           capabilities=capabilities, mode=mode, on_trace=trace,
                           summary=session.get("summary"), auto_compact=auto_compact, compact_only=compact_only,
                           cancel_event=cancel_event, approver=approver)
    except BaseException as error:
        status = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
        reason = f"模型请求失败（{type(error).__name__}）" if isinstance(error, APIError) else f"{type(error).__name__}: {error}"
        try:
            trace("error", {"error_type": type(error).__name__, "reason": reason})
            tasks.finish(task_id, status, reason, duration=time.monotonic() - started)
        except (OSError, ValueError, RuntimeError) as record_error:
            ui.error(f"任务 {task_id} 的终态未保存：{record_error}；请核对现有操作记录，勿重复执行。")
        raise
    try:
        tasks.finish(task_id, result.status, result.stop_reason, answer=result.answer,
                     duration=time.monotonic() - started)
    except (OSError, ValueError) as error:
        ui.error(f"任务 {task_id} 的终态未保存：{error}；回答仍保留，请勿重复执行。")
    session["messages"].extend(result.messages)
    session["model"] = model
    if result.summary is not None:
        session["summary"] = {**result.summary, "task_id": task_id}
    return result.answer


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
        ui.info(HELP_TEXT)
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


def make_approver(ui, *, yes, interactive, timeout):
    """--yes 跳过提问；非交互没有审批人时不执行写入和命令。"""
    if yes:
        return approve_all
    if not interactive:
        return fail_closed
    return InteractiveApprover(ui, timeout=timeout)


def interactive_loop(client, model, workspace, store, ui, session, max_steps, memory_enabled, limits=None,
                     capabilities=("read",), mode="react", auto_compact=True, approver=None):
    ui.info("多轮对话模式：直接输入问题；/help 查看命令，/exit 退出。")
    while True:
        try:
            line = ui.read_prompt()
        except (EOFError, KeyboardInterrupt):
            ui.info("")
            return 0 if save_session(store, session, ui) else 1
        prompt = (line or "").strip()
        if not prompt:
            continue
        compact_only = prompt.split()[0].lower() == "/compact"
        if compact_only and prompt.lower() != "/compact":
            ui.error("用法：/compact（不需要参数）")
            continue
        if prompt.startswith("/") and not compact_only:
            session = handle_command(prompt, ui, store, session, model)
            if session is None:
                return 0
            continue
        try:
            answer = run_turn(client, model, workspace, store, ui, session,
                              prompt, max_steps=max_steps, memory_enabled=memory_enabled, limits=limits,
                              capabilities=capabilities, mode=mode,
                              auto_compact=auto_compact, compact_only=compact_only, approver=approver)
        except KeyboardInterrupt:
            ui.error("已取消本轮。")
            continue
        except APIError as error:
            ui.error(f"模型请求失败（{type(error).__name__}），请检查网络、协议和模型配置。")
            continue
        except (OSError, ValueError, RuntimeError) as error:
            ui.error(f"任务停止：{error}")
            continue
        ui.answer(answer)
        if not save_session(store, session, ui):
            ui.info("当前消息仍保留在内存，输入 /save 重试，无需重新提问。")


def build_parser():
    parser = argparse.ArgumentParser(description="RevoCLI：支持 ReAct、Plan and Execute 与只读子代理的终端代码 Agent")
    parser.add_argument("prompt", nargs="?", help="本次问题；省略则进入多轮对话")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="允许读取的工作区")
    parser.add_argument("--mode", choices=["react", "plan", "multi"], default="react",
                        help="执行模式：react 直接执行；plan 先计划再执行；multi 并发只读分析与审查")
    parser.add_argument("--max-steps", type=int, help="每轮模型请求总上限，react 默认 8，plan/multi 默认 20")
    parser.add_argument("--max-tool-calls", type=int, default=16, help="每轮工具调用数量上限")
    parser.add_argument("--max-tool-bytes", type=int, default=128 * 1024, help="每轮工具结果正文的 UTF-8 字节上限")
    parser.add_argument("--context-bytes", type=int, default=256 * 1024, help="模型消息的 UTF-8 JSON 大小上限")
    parser.add_argument("--max-seconds", type=float, default=120.0, help="每轮执行时间预算，秒")
    sessions = parser.add_mutually_exclusive_group()
    sessions.add_argument("--session", metavar="ID", help="加载指定会话 ID 后继续")
    sessions.add_argument("--continue", dest="continue_", action="store_true", help="恢复最近一个有内容的会话")
    tasks = parser.add_mutually_exclusive_group()
    tasks.add_argument("--tasks", action="store_true", help="查看最近任务，不需要模型配置")
    tasks.add_argument("--task", metavar="ID", help="查看指定任务的事实和结果，不请求模型")
    parser.add_argument("--json", action="store_true", help="配合 --tasks/--task 输出完整 JSON 事实")
    parser.add_argument("--no-memory", dest="memory", action="store_false", help="本次不注入长期记忆")
    parser.add_argument("--no-auto-compact", dest="auto_compact", action="store_false", help="关闭自动历史压缩，仍可使用 /compact")
    parser.add_argument("--compact", action="store_true", help="压缩所恢复会话的旧轮次后退出；需要 --continue 或 --session")
    parser.add_argument("--allow-web", action="store_true", help="开放网页搜索和读取（只读联网）")
    parser.add_argument("--allow-write", action="store_true", help="开放文件编辑与按备份恢复")
    parser.add_argument("--allow-exec", action="store_true", help="开放前台命令执行（以当前用户权限运行）")
    parser.add_argument("--yes", action="store_true",
                        help="跳过逐次提问并批准本进程内的写入和命令；执行前仍复核参数与文件指纹，变化则作废")
    parser.add_argument("--approval-timeout", type=float, default=120,
                        help="交互审批等待秒数，默认 120，最大 3600")
    parser.add_argument("--preview", action="store_true", help="预览小幽灵与示例对话配色，不请求模型")
    parser.add_argument("--banner", choices=["auto", "color", "plain", "off"], default="auto",
                        help="启动面板显示：自动检测、彩色、纯文本或关闭（默认 auto）")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                        help="对话配色：自动、始终开启或关闭（默认 auto）")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (isinstance(args.approval_timeout, bool) or not isinstance(args.approval_timeout, (int, float))
            or not math.isfinite(args.approval_timeout) or not 0 < args.approval_timeout <= 3600):
        parser.error("--approval-timeout 必须是大于 0 且不超过 3600 的秒数")
    capabilities = {"read"}
    if args.allow_web:
        capabilities.add("web")
    if args.allow_write:
        capabilities.add("write")
    if args.allow_exec:
        capabilities.add("exec")
    ui = TerminalUI(color=args.color)
    banner_mode = args.banner
    if banner_mode == "auto" and args.color != "auto":
        banner_mode = "color" if args.color == "always" else "plain"
    if args.preview:
        print_banner(workspace=args.workspace, preview=True, mode=banner_mode)
        ui.preview()
        return 0
    if args.tasks or args.task:
        if args.prompt is not None or args.session or args.continue_ or args.compact:
            parser.error("任务查看参数不能与提问或会话恢复同时使用")
        if not args.workspace.is_dir():
            parser.error("--workspace 必须是存在的目录")
        try:
            task_store = TaskStore(args.workspace, create=False)
            task_store.recover_interrupted()
            show_tasks(ui, task_store, args.task, as_json=args.json)
            return 0
        except (OSError, ValueError) as error:
            parser.error(str(error))
    if args.json:
        parser.error("--json 需要配合 --tasks 或 --task ID")
    if args.compact and (args.prompt is not None or not (args.session or args.continue_)):
        parser.error("--compact 需要 --continue 或 --session，且不能同时提问")
    if args.max_steps is None:
        args.max_steps = 20 if args.mode in ("plan", "multi") else 8
    if args.max_steps < 1:
        parser.error("--max-steps 必须大于 0")
    if args.mode == "plan" and args.max_steps < 4 and not args.compact:
        parser.error("计划模式至少需要 4 次模型请求预算")
    if args.mode == "multi" and args.max_steps < 3 and not args.compact:
        parser.error("多代理模式至少需要 3 次模型请求预算")
    try:
        limits = AgentLimits(max_context_bytes=args.context_bytes, max_tool_calls=args.max_tool_calls,
                             max_tool_bytes=args.max_tool_bytes, max_seconds=args.max_seconds)
    except ValueError as error:
        parser.error(str(error))
    if not args.workspace.is_dir():
        parser.error("--workspace 必须是存在的目录")
    try:
        config = load_config()
    except ConfigError as error:
        parser.error(str(error))

    try:
        store = SessionStore(args.workspace)
        task_store = TaskStore(args.workspace)
        recovered = task_store.recover_interrupted()
        if recovered:
            ui.info(f"发现 {len(recovered)} 个所属进程已退出的任务，已标为 unknown；/tasks 查看，不自动重试。")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    try:
        if args.continue_:
            session = store.latest()
            if session is None:
                parser.error("没有可恢复的会话")
        elif args.session:
            session = store.load(args.session)
        else:
            session = store.new_session(config.model)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    try:
        print_banner(workspace=args.workspace, mode=banner_mode)
        ui.info("执行模式：" + {"react": "ReAct", "plan": "Plan and Execute", "multi": "只读子代理（分析 + 审查）"}[args.mode])
        effective = capabilities & {"read"} if args.mode == "multi" else capabilities
        ui.info(f"工具能力：{'、'.join(name for key, name in [('read', '读取'), ('write', '编辑'), ('exec', '命令执行')] if key in effective)}")
        interactive_session = args.prompt is None and not args.compact and sys.stdin.isatty()
        approver = make_approver(ui, yes=args.yes, interactive=interactive_session, timeout=args.approval_timeout)
        if "write" in effective or "exec" in effective:
            if args.yes:
                ui.info("审批：--yes 会批准本进程内的写入和命令，执行前仍复核文件指纹。")
            elif interactive_session:
                ui.info("审批：每次写入或命令前输入 approve、reject 或 cancel。其他文字不会批准。")
            else:
                ui.info("审批：当前不会等待输入。未加 --yes 时，写入和命令不会执行。")
        # 首版只接 Chat Completions 工具调用协议，不自动重试收费请求。
        with OpenAI(api_key=config.api_key, base_url=config.base_url,
                    timeout=30.0, max_retries=0) as client:
            if args.prompt is not None or args.compact:
                prompt = "/compact" if args.compact else args.prompt.strip()
                if not prompt:
                    parser.error("问题不能为空")
                ui.user(prompt)
                answer = run_turn(client, config.model, args.workspace, store, ui, session,
                                  prompt, max_steps=args.max_steps, memory_enabled=args.memory, limits=limits,
                                  capabilities=capabilities, mode=args.mode,
                                  auto_compact=args.auto_compact, compact_only=args.compact, approver=approver)
                ui.answer(answer)
                return 0 if save_session(store, session, ui) else 1
            return interactive_loop(client, config.model, args.workspace, store, ui, session,
                                    args.max_steps, args.memory, limits, capabilities, args.mode,
                                    args.auto_compact, approver)
    except (KeyboardInterrupt, EOFError):
        ui.error("\n已取消。")
        return 130
    except APIError as error:
        ui.error(f"模型请求失败（{type(error).__name__}），请检查网络、协议和模型配置。")
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        ui.error(f"任务停止：{error}")
        return 1
