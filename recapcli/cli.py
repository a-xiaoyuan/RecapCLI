"""命令行入口、交互循环与用户命令。"""

import argparse
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import sys
import time

from openai import APIError, OpenAI

from .config import ConfigError, list_model_profiles, load_config
from .models import ModelClient
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
  /models         列出本地模型配置（不请求接口）
  /model [名称]   查看当前模型，或切换下一轮使用的配置
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
    model_config = client.metadata() if isinstance(client, ModelClient) else None
    if model_config is not None:
        limits = replace(limits, max_context_bytes=min(limits.max_context_bytes, model_config["context_bytes"]))
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
        "model_config": model_config,
    })
    started = time.monotonic()

    def trace(kind, data):
        try:
            tasks.append(task_id, kind, data)
        except (OSError, ValueError) as error:
            raise RuntimeError(f"任务 {task_id} 的记录失败，停止后续执行：{error}") from error

    def publish_task():
        receiver = getattr(ui, "task_update", None)
        if receiver is not None:
            try:
                task = tasks.get(task_id)
            except (OSError, ValueError):
                # 状态栏读取失败不改变已持久化的任务事实或重复执行操作。
                return
            receiver(task)

    try:
        publish_task()
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
                           cancel_event=cancel_event, approver=approver,
                           on_stream=getattr(ui, "stream_update", None) if isinstance(client, ModelClient) else None)
    except BaseException as error:
        if getattr(ui, "stream_update", None) is not None:
            ui.stream_update("abort", "")
        status = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
        reason = f"模型请求失败（{type(error).__name__}）" if isinstance(error, APIError) else f"{type(error).__name__}: {error}"
        try:
            trace("error", {"error_type": type(error).__name__, "reason": reason})
            tasks.finish(task_id, status, reason, duration=time.monotonic() - started)
        except (OSError, ValueError, RuntimeError) as record_error:
            ui.error(f"任务 {task_id} 的终态未保存：{record_error}；请核对现有操作记录，勿重复执行。")
        publish_task()
        raise
    try:
        tasks.finish(task_id, result.status, result.stop_reason, answer=result.answer,
                     duration=time.monotonic() - started)
    except (OSError, ValueError) as error:
        ui.error(f"任务 {task_id} 的终态未保存：{error}；回答仍保留，请勿重复执行。")
    publish_task()
    session["messages"].extend(result.messages)
    session["model"] = model
    if model_config is not None:
        session["model_profile"] = model_config["profile"]
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


def show_models(ui, current=None):
    for spec in list_model_profiles():
        marker = "当前" if spec["name"] == current else ("默认" if spec["default"] else "可选")
        ready = "已配置" if spec["configured"] else "缺少模型或密钥配置"
        ui.info(f"{marker} · {spec['name']} · {spec['protocol']} · {spec['model'] or '未设置模型'} · {ready}")
        ui.info(f"  能力声明：工具={spec['tools']}、图片={spec['images']}、流式={spec['streaming']}；上下文 JSON 字节上限={spec['context_bytes']}")


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
        if prompt.split()[0].lower() in {"/models", "/model"}:
            parts = prompt.split()
            try:
                if not isinstance(client, ModelClient):
                    raise ValueError("当前 Python 客户端未启用配置切换；请从 CLI 启动")
                if parts[0].lower() == "/models":
                    if len(parts) != 1:
                        raise ValueError("用法：/models")
                    show_models(ui, getattr(client.config, "profile", "default"))
                elif len(parts) == 1:
                    info = client.metadata()
                    ui.info(f"当前模型：{info['profile']} · {info['protocol']} · {info['model']}")
                elif len(parts) == 2:
                    client.switch(parts[1])
                    model = client.model
                    session["model"] = model
                    session["model_profile"] = getattr(client.config, "profile", "default")
                    ui.info(f"已切换：{parts[1]} · {getattr(client.config, 'protocol', 'chat_completions')} · {model}；尚未请求接口。")
                    ui.info("历史问答与工具事实继续保留；协议私有状态仅回送原配置，切换不代表新端点已经验证可用。")
                    save_session(store, session, ui)
                else:
                    raise ValueError("用法：/model [配置名称]")
            except (OSError, ValueError, RuntimeError) as error:
                ui.error(str(error))
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
    parser = argparse.ArgumentParser(
        description="RecapCLI：支持 ReAct、Plan and Execute 与仅本地只读子代理的终端代码 Agent。交互终端默认打开 Textual 壳。",
        epilog="终端壳需要可选依赖 textual（uv sync --extra tui）。--demo 只演示界面，不请求模型。无 textual 时使用 python -m recapcli.cli。",
    )
    parser.add_argument("prompt", nargs="?", help="本次问题；省略则进入多轮对话")
    parser.add_argument("--profile", metavar="名称", help="选择 models.toml 中的模型配置；恢复会话时默认沿用上次配置")
    parser.add_argument("--models", action="store_true", help="列出本地模型配置，不请求模型、不要求密钥齐全")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="允许读取的工作区")
    parser.add_argument("--mode", choices=["react", "plan", "multi"], default="react",
                        help="执行模式：react 直接执行；plan 先计划再执行；multi 仅本地只读的并发分析与审查")
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
    parser.add_argument("--allow-web", action="store_true",
                        help="开放网页搜索与公网读取（web_search、read_url）。--allow-network 是兼容别名")
    parser.add_argument("--allow-network", action="store_true", dest="allow_web", help=argparse.SUPPRESS)
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
    requested = {"read"}
    if args.allow_web:
        requested.add("web")
    if args.allow_write:
        requested.add("write")
    if args.allow_exec:
        requested.add("exec")
    # Multi 实际只有本地读取；网页、编辑和命令开关不授予子代理。
    capabilities = {"read"} if args.mode == "multi" else requested
    ui = TerminalUI(color=args.color)
    banner_mode = args.banner
    if banner_mode == "auto" and args.color != "auto":
        banner_mode = "color" if args.color == "always" else "plain"
    if args.preview:
        print_banner(workspace=args.workspace, preview=True, mode=banner_mode)
        ui.preview()
        return 0
    if args.models:
        if args.prompt is not None or args.session or args.continue_ or args.compact or args.task or args.tasks or args.json:
            parser.error("--models 不能与提问、会话或任务查看参数同时使用")
        try:
            show_models(ui, args.profile)
        except ConfigError as error:
            parser.error(str(error))
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
        parser.error("Multi 仅本地只读，至少需要 3 次模型请求预算")
    try:
        limits = AgentLimits(max_context_bytes=args.context_bytes, max_tool_calls=args.max_tool_calls,
                             max_tool_bytes=args.max_tool_bytes, max_seconds=args.max_seconds)
    except ValueError as error:
        parser.error(str(error))
    if not args.workspace.is_dir():
        parser.error("--workspace 必须是存在的目录")
    config = None
    if not args.continue_ and not args.session:
        try:
            config = load_config(args.profile) if args.profile is not None else load_config()
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
            session = None
    except (OSError, ValueError) as error:
        parser.error(str(error))

    try:
        profile = args.profile if args.profile is not None else (session or {}).get("model_profile")
        if config is None:
            config = load_config(profile) if profile is not None else load_config()
        if session is None:
            session = store.new_session(config.model)
    except ConfigError as error:
        parser.error(str(error))

    try:
        print_banner(workspace=args.workspace, mode=banner_mode)
        labels = {"react": "ReAct", "plan": "Plan and Execute", "multi": "Multi（仅本地只读）"}
        ui.info("执行模式：" + labels[args.mode])
        granted = "、".join(name for key, name in [("read", "读取"), ("write", "编辑"), ("exec", "命令执行"), ("web", "网页")] if key in capabilities)
        if args.mode == "multi":
            ui.info("工具能力：" + granted + "。不联网、不修改文件、不执行命令。")
        else:
            ui.info("工具能力：" + granted)
        ui.info(f"模型配置：{getattr(config, 'profile', 'default')} · {getattr(config, 'protocol', 'chat_completions')} · {config.model}")
        interactive_session = args.prompt is None and not args.compact and sys.stdin.isatty()
        approver = make_approver(ui, yes=args.yes, interactive=interactive_session, timeout=args.approval_timeout)
        if "write" in capabilities or "exec" in capabilities:
            if args.yes:
                ui.info("审批：--yes 会批准本进程内的写入和命令，执行前仍复核文件指纹。")
            elif interactive_session:
                ui.info("审批：每次写入或命令前输入 approve、reject 或 cancel。其他文字不会批准。")
            else:
                ui.info("审批：当前不会等待输入。未加 --yes 时，写入和命令不会执行。")
        with ModelClient(config, client_factory=OpenAI) as client:
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


if __name__ == "__main__":
    sys.exit(main())
