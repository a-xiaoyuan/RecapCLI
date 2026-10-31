"""把终端壳接到现有 Agent、会话、任务和审批。不另写循环。"""

from dataclasses import dataclass
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from openai import APIError

from ..application.runtime import run_turn, save_session
from ..application.commands import handle_command, show_models, show_tasks as show_task_report
from ..application.help import help_text
from ..config import ConfigError, load_config
from ..core.agent import AgentLimits, DEFAULT_STEPS
from ..llm.client import ModelClient, api_error_message
from ..storage.tasks import TaskStore
from ..tools.approval import InteractiveApprover, approve_all
from .state import _sessions

_EOF = object()

@dataclass
class ShellOptions:
    workspace: Path
    mode: str = "react"
    capabilities: tuple = ("read",)
    max_steps: int = DEFAULT_STEPS["react"]
    limits: AgentLimits | None = None
    profile: str | None = None
    session_id: str | None = None
    continue_session: bool = False
    memory: bool = True
    auto_compact: bool = True
    yes: bool = False
    approval_timeout: float = 120.0


def describe_runtime(options):
    labels = {"react": "ReAct", "plan": "Plan and Execute", "multi": "Multi（仅本地只读）"}
    names = (("read", "读取"), ("write", "编辑"), ("exec", "命令"), ("web", "网页"))
    granted = "、".join(name for key, name in names if key in options.capabilities)
    approval = ""
    if options.mode != "multi" and ({"write", "exec"} & set(options.capabilities)):
        approval = (
            "本进程内的写入和命令按 --yes 批准，执行前仍复核指纹。"
            if options.yes else "写入和命令会逐次审批。"
        )
    return f"{labels[options.mode]} · {granted}。{approval}/help 查看命令，Ctrl+Q 退出。"


class Presenter:
    """把工作线程里的界面调用送回 Textual。"""

    def __init__(self, app):
        self.app = app

    def _call(self, method, *args):
        if self.app.ui_thread is not None and threading.get_ident() == self.app.ui_thread:
            return method(*args)
        return self.app.call_from_thread(method, *args)

    def info(self, message):
        message = str(message)
        if message.startswith(("任务 ID：", "长期记忆：本轮选用")):
            return
        self._call(self.app.append_line, message, "info")

    def error(self, message):
        self._call(self.app.append_line, str(message), "error")

    def event(self, message):
        message = str(message)
        event = message
        for role in ("analysis", "review", "aggregate"):
            event = event.removeprefix(f"[{role}] ")
        if event.startswith(("[模型]", "[工具]")):
            activity = ("请求模型 · " + event.removeprefix("[模型]").strip() if event.startswith("[模型]")
                        else "工具 · " + event.removeprefix("[工具]").strip())
            self._call(self.app.set_activity, activity)
            if event.startswith("[模型]"):
                return
        self._call(self.app.append_line, message, "event")

    def answer(self, message):
        self._call(self.app.finish_answer, str(message))

    def stream_update(self, kind, text):
        self._call(self.app.stream_update, kind, str(text))

    def task_update(self, task):
        self._call(self.app.apply_task, task)

    def show_approval(self, message):
        self._call(self.app.begin_approval, str(message))

    def read_approval(self, timeout):
        if self.app.driver is not None and self.app.driver.quitting:
            raise EOFError
        try:
            item = self.app.approval_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _EOF:
            raise EOFError
        return item

    def clear_transcript(self):
        self._call(self.app.clear_transcript_sync)

    def replace_transcript(self, rows):
        self._call(self.app.replace_transcript_sync, tuple(rows))

    def model_changed(self, model, ready):
        self._call(self.app.apply_model, str(model), bool(ready))

    def disarm_approval(self):
        try:
            self._call(self.app.end_approval)
        except RuntimeError:
            return

    def push_cancel(self):
        self.app.approval_queue.put(_EOF)


class ShellDriver:
    def __init__(self, presenter, options, *, client_factory, ready=False):
        self.presenter = presenter
        self.options = options
        self.client_factory = client_factory
        self.ready = bool(ready)
        self.store = None
        self.session = None
        self.client = None
        self.model = None
        self.quitting = False
        self.cancel_event = threading.Event()

    def close(self):
        client = self.client
        self.client = None
        if client is not None:
            client.__exit__(None, None, None)

    def request_stop(self):
        self.quitting = True
        self.cancel_turn()

    def cancel_turn(self):
        self.cancel_event.set()
        push = getattr(self.presenter, "push_cancel", None)
        if push is not None:
            push()

    def prepare(self):
        """启动时的只读检查。只有明确要求恢复会话时才打开存储。"""
        notes = []
        if self.options.continue_session or self.options.session_id:
            try:
                self._ensure_store()
                if self.options.continue_session:
                    self.session = self.store.latest()
                    if self.session is None:
                        notes.append("没有可恢复的会话。")
                else:
                    self.session = self.store.load(self.options.session_id)
                if self.session is not None:
                    self.model = self.session.get("model") or self.model
                    notes.append(f"已恢复会话 {self.session['id']}，共 {len(self.session['messages'])} 条消息。")
            except (OSError, ValueError) as error:
                notes.append(f"会话恢复失败：{error}")
        try:
            recovered = TaskStore(self.options.workspace, create=False).recover_interrupted()
            if recovered:
                notes.append(f"发现 {len(recovered)} 个所属进程已退出的任务，已标为 unknown；/tasks 查看，不自动重试。")
        except (OSError, ValueError) as error:
            notes.append(f"任务恢复检查失败：{error}")
        return notes

    def transcript_rows(self):
        rows = []
        if not self.session:
            return rows
        for message in self.session["messages"]:
            role = message.get("role")
            if role == "user":
                rows.append(("user", str(message.get("content") or "")))
            elif role == "assistant":
                content = message.get("content")
                if isinstance(content, str) and content:
                    rows.append(("assistant", content))
        return rows

    def show_help(self, topic="common"):
        self.presenter.info(help_text(topic))

    def show_sessions(self):
        try:
            sessions = self.store.list_sessions() if self.store is not None else _sessions(self.options.workspace)
        except (OSError, ValueError) as error:
            self.presenter.error(f"会话列表读取失败：{error}")
            return
        if not sessions:
            body = "暂无已保存的会话。"
        else:
            body = "\n".join(f"{item['id']}  ·  {item.get('messages', 0)} 条消息" for item in sessions)
        self.presenter.info("已保存会话\n" + body + "\n恢复：/open 会话ID，或启动时 --continue / --session ID。")

    def show_tasks(self):
        try:
            show_task_report(self.presenter, TaskStore(self.options.workspace, create=False))
        except (OSError, ValueError) as error:
            self.presenter.error(f"命令执行失败：{error}")

    def save_if_needed(self):
        if self.store is None or self.session is None:
            return True
        return save_session(self.store, self.session, self.presenter)

    def handle_line(self, line, *, cancel_event=None):
        prompt = line or ""
        if not prompt.strip() or self.quitting:
            return None
        if prompt.lstrip().startswith("/"):
            prompt = prompt.strip()
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        try:
            return self._dispatch(prompt)
        except KeyboardInterrupt:
            self.presenter.error("已取消本轮。")
            self._save_quiet()
            return None
        finally:
            self.presenter.disarm_approval()

    def _dispatch(self, prompt):
        head = prompt.split(maxsplit=1)[0].lower()
        if head in {"/exit", "/quit"}:
            return self._exit()
        if head == "/help":
            self.show_help(prompt.partition(" ")[2].strip() or "common")
            return None
        if head == "/open":
            argument = prompt.split(maxsplit=1)[1] if len(prompt.split(maxsplit=1)) == 2 else ""
            self._open(argument)
            return None
        if head in {"/models", "/model"}:
            self._models(prompt)
            return None
        if head == "/compact":
            if prompt.lower() != "/compact":
                self.presenter.error("用法：/compact（不需要参数）")
                return None
            self._turn(prompt, compact_only=True)
            return None
        if prompt.startswith("/"):
            return self._slash(prompt)
        self._turn(prompt, compact_only=False)
        return None

    def _exit(self):
        if self.session and self.session.get("messages"):
            if not self.save_if_needed():
                self.presenter.info("当前消息仍保留在内存，输入 /save 重试。")
                return None
        self.presenter.info("再见。")
        return "exit"

    def _slash(self, prompt):
        head = prompt.split(maxsplit=1)[0].lower()
        if head == "/sessions":
            self.show_sessions()
            return None
        if head in {"/tasks", "/task", "/diff", "/undo"}:
            store = self.store if self.store is not None else SimpleNamespace(workspace=self.options.workspace)
            session = self.session if self.session is not None else {"id": "", "messages": [], "summary": None}
            handle_command(prompt, self.presenter, store, session, self.model or "",
                           capabilities=self.options.capabilities, approver=self._approver() if head == "/undo" else None)
            return None
        if head == "/new":
            self._new_session()
            return None
        if head in {"/history", "/summary", "/session", "/save"} and self.session is None:
            self.presenter.info("当前还没有会话。")
            return None
        try:
            self._ensure_store()
        except (OSError, ValueError) as error:
            self.presenter.error(f"命令执行失败：{error}")
            return None
        session = self.session if self.session is not None else {"id": "", "messages": [], "summary": None}
        updated = handle_command(prompt, self.presenter, self.store, session, self.model or "")
        if updated is None:
            return "exit"
        if self.session is not None and isinstance(updated, dict):
            self.session = updated
        return None

    def _new_session(self):
        if not self._require_client(sending=False):
            return
        before = self.session["id"]
        updated = handle_command("/new", self.presenter, self.store, self.session, self.model)
        if not isinstance(updated, dict):
            return
        self.session = updated
        if updated.get("id") != before:
            self.presenter.replace_transcript(())
            self.presenter.info(f"已开始新会话：{updated['id']}")

    def _open(self, session_id):
        session_id = session_id.strip()
        if not session_id or any(char.isspace() for char in session_id):
            self.presenter.error("用法：/open 会话ID")
            return
        try:
            self._ensure_store()
            if self.session and self.session.get("messages"):
                if not save_session(self.store, self.session, self.presenter):
                    self.presenter.info("当前会话未切换。")
                    return
            loaded = self.store.load(session_id)
        except (OSError, ValueError) as error:
            self.presenter.error(f"未能恢复会话：{error}")
            return
        self.session = loaded
        if loaded.get("model"):
            self.model = loaded["model"]
            self.presenter.model_changed(loaded["model"], self.ready)
        self.presenter.replace_transcript(self.transcript_rows())
        self.presenter.info(f"已恢复会话 {loaded['id']}，共 {len(loaded['messages'])} 条消息。")

    def _models(self, prompt):
        parts = prompt.split()
        head = parts[0].lower()
        try:
            if head == "/models":
                if len(parts) > 2:
                    raise ValueError("用法：/models [筛选词]")
                current = self.options.profile
                if isinstance(self.client, ModelClient):
                    current = getattr(self.client.config, "profile", "default")
                show_models(self.presenter, current, parts[1] if len(parts) == 2 else "*")
                return
            if len(parts) == 1:
                if not self._require_client(sending=False):
                    return
                info = self.client.metadata()
                self.presenter.info(f"当前模型：{info['profile']} · {info['protocol']} · {info['model']}")
            elif len(parts) == 2:
                if self.client is None:
                    if not self._require_client(sending=False, profile=parts[1]):
                        return
                elif isinstance(self.client, ModelClient):
                    self.client.switch(parts[1])
                else:
                    raise ValueError("当前 Python 客户端未启用配置切换；请从命令行启动")
                self.model = self.client.model
                self.options.profile = parts[1]
                self.session["model"] = self.model
                self.session["model_profile"] = getattr(self.client.config, "profile", "default")
                self.ready = True
                self.presenter.model_changed(self.model, True)
                self.presenter.info(
                    f"已切换：{parts[1]} · {getattr(self.client.config, 'protocol', 'chat_completions')} · {self.model}；尚未请求接口。"
                )
                save_session(self.store, self.session, self.presenter)
            else:
                raise ValueError("用法：/model [配置名称]")
        except (OSError, ValueError, RuntimeError) as error:
            self.presenter.error(str(error))

    def _turn(self, prompt, *, compact_only):
        if self.quitting:
            return
        if not self._require_client(sending=True):
            return
        approver = self._approver()
        try:
            answer = run_turn(
                self.client, self.model, self.options.workspace, self.store, self.presenter, self.session,
                prompt, max_steps=self.options.max_steps, memory_enabled=self.options.memory,
                limits=self.options.limits, capabilities=self.options.capabilities, mode=self.options.mode,
                auto_compact=self.options.auto_compact, compact_only=compact_only,
                cancel_event=self.cancel_event, approver=approver,
            )
        except KeyboardInterrupt:
            self.presenter.info("已停止本轮，可以继续提问。")
            self._save_quiet()
            return
        except APIError as error:
            self.presenter.error(api_error_message(error, self.model))
            return
        except (OSError, ValueError, RuntimeError) as error:
            self.presenter.error(f"任务停止：{error}")
            return
        self.presenter.answer(answer)
        if not save_session(self.store, self.session, self.presenter):
            self.presenter.info("当前消息仍保留在内存，输入 /save 重试，无需重新提问。")

    def _approver(self):
        return approve_all if self.options.yes else InteractiveApprover(
            self.presenter, timeout=self.options.approval_timeout, reader=self.presenter.read_approval)

    def _require_client(self, *, sending, profile=None):
        if self.client is not None:
            return True
        profile = profile or self.options.profile
        if profile is None and self.session is not None:
            profile = self.session.get("model_profile")
        try:
            config = load_config(profile) if profile is not None else load_config()
        except ConfigError as error:
            suffix = "。输入未发送。" if sending else "。"
            self.presenter.error(f"{error}{suffix}")
            self.ready = False
            return False
        try:
            client = ModelClient(config, client_factory=self.client_factory)
            client.__enter__()
        except Exception as error:
            suffix = "。输入未发送。" if sending else "。"
            self.presenter.error(f"模型客户端未能启动（{type(error).__name__}）{suffix}")
            return False
        self.client = client
        self.model = config.model
        try:
            self._ensure_store()
        except (OSError, ValueError) as error:
            self.presenter.error(f"会话存储不可用：{error}")
            return False
        if self.session is None:
            self.session = self.store.new_session(config.model)
            self.session["model_profile"] = getattr(config, "profile", "default")
        self.ready = True
        self.presenter.model_changed(self.model, True)
        return True

    def _ensure_store(self):
        if self.store is None:
            from ..storage.session import SessionStore
            self.store = SessionStore(self.options.workspace)
        return self.store

    def _save_quiet(self):
        if self.store is None or self.session is None:
            return
        save_session(self.store, self.session, self.presenter)
