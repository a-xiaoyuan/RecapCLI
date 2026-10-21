"""命令行入口、交互循环与用户命令。"""

import argparse
from pathlib import Path

from openai import APIError, OpenAI

from .config import ConfigError, load_config
from .core.agent import AgentLimits, run_agent
from .storage.session import SessionStore
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
  /memory         查看长期记忆
  /remember 文本   添加一条长期记忆
  /memory-update ID 文本  更新记忆，保留原 ID
  /forget ID|all  删除一条或全部长期记忆"""


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


def show_memory(ui, store):
    items = store.memory_items()
    if not items:
        ui.info("暂无长期记忆。用 /remember 文本 添加。")
        return
    ui.info(f"长期记忆（{len(items)} 条）：")
    for item in items:
        ui.info(f"  {item['id']}  {item['text']}")


def run_turn(client, model, workspace, store, ui, session, prompt, *,
             max_steps=8, memory_enabled=True, limits=None):
    """CLI 编排存储与展示；核心循环只接收消息及事件回调。"""
    memory_text = ""
    if memory_enabled:
        memory = store.memory_context()
        memory_text = memory.text
        if memory.selected_ids or memory.omitted_count:
            ui.info(f"长期记忆：本轮选用 {len(memory.selected_ids)} 条，省略 {memory.omitted_count} 条。")
    result = run_agent(client, model, prompt, workspace, history=session["messages"],
                       memory_text=memory_text, max_steps=max_steps, limits=limits, on_event=ui.event)
    session["messages"].extend(result.messages)
    session["model"] = model
    return result.answer


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
    elif name == "/memory":
        try:
            show_memory(ui, store)
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
    elif name == "/remember":
        try:
            item = store.add_memory(argument)
        except (OSError, ValueError) as error:
            ui.error(f"命令执行失败：{error}")
        else:
            ui.info(f"已记住（{item['id']}）：{item['text']}")
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


def interactive_loop(client, model, workspace, store, ui, session, max_steps, memory_enabled, limits=None):
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
        if prompt.startswith("/"):
            session = handle_command(prompt, ui, store, session, model)
            if session is None:
                return 0
            continue
        try:
            answer = run_turn(client, model, workspace, store, ui, session,
                              prompt, max_steps=max_steps, memory_enabled=memory_enabled, limits=limits)
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
    parser = argparse.ArgumentParser(description="Archer：支持多轮对话与记忆的只读代码 Agent")
    parser.add_argument("prompt", nargs="?", help="本次问题；省略则进入多轮对话")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="允许读取的工作区")
    parser.add_argument("--max-steps", type=int, default=8, help="每轮模型请求次数上限，默认 8")
    parser.add_argument("--max-tool-calls", type=int, default=16, help="每轮工具调用数量上限")
    parser.add_argument("--max-tool-bytes", type=int, default=128 * 1024, help="每轮工具结果正文的 UTF-8 字节上限")
    parser.add_argument("--context-bytes", type=int, default=256 * 1024, help="模型消息的 UTF-8 JSON 大小上限")
    parser.add_argument("--max-seconds", type=float, default=120.0, help="每轮执行时间预算，秒")
    sessions = parser.add_mutually_exclusive_group()
    sessions.add_argument("--session", metavar="ID", help="加载指定会话 ID 后继续")
    sessions.add_argument("--continue", dest="continue_", action="store_true", help="恢复最近一个有内容的会话")
    parser.add_argument("--no-memory", dest="memory", action="store_false", help="本次不注入长期记忆")
    parser.add_argument("--preview", action="store_true", help="预览小幽灵与示例对话配色，不请求模型")
    parser.add_argument("--banner", choices=["auto", "color", "plain", "off"], default="auto",
                        help="启动面板显示：自动检测、彩色、纯文本或关闭（默认 auto）")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                        help="对话配色：自动、始终开启或关闭（默认 auto）")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui = TerminalUI(color=args.color)
    banner_mode = args.banner
    if banner_mode == "auto" and args.color != "auto":
        banner_mode = "color" if args.color == "always" else "plain"
    if args.preview:
        print_banner(workspace=args.workspace, preview=True, mode=banner_mode)
        ui.preview()
        return 0
    if args.max_steps < 1:
        parser.error("--max-steps 必须大于 0")
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
    except OSError as error:
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
        # 首版只接 Chat Completions 工具调用协议，不自动重试收费请求。
        with OpenAI(api_key=config.api_key, base_url=config.base_url,
                    timeout=30.0, max_retries=0) as client:
            if args.prompt is not None:
                prompt = args.prompt.strip()
                if not prompt:
                    parser.error("问题不能为空")
                ui.user(prompt)
                answer = run_turn(client, config.model, args.workspace, store, ui, session,
                                  prompt, max_steps=args.max_steps, memory_enabled=args.memory, limits=limits)
                ui.answer(answer)
                return 0 if save_session(store, session, ui) else 1
            return interactive_loop(client, config.model, args.workspace, store, ui, session,
                                    args.max_steps, args.memory, limits)
    except (KeyboardInterrupt, EOFError):
        ui.error("\n已取消。")
        return 130
    except APIError as error:
        ui.error(f"模型请求失败（{type(error).__name__}），请检查网络、协议和模型配置。")
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        ui.error(f"任务停止：{error}")
        return 1
