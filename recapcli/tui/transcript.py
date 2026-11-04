"""对话区：追加消息、工具事件、流式回答和跟随滚动。"""


from ..ui.terminal_ui import clean_output
from .status import TaskStatus
from .widgets import ConversationLine, ConversationView, GlyphButton, TodoPanel, ToolProgress





class TranscriptMixin:
    def _static(self, content, kind):
        return ConversationLine(content, kind)

    def _follow_latest(self, *, force=False):
        conversation = self.query_one("#conversation", ConversationView)
        if force:
            conversation.follow_latest()
            self._has_new_content = False
        elif conversation.following:
            conversation.scroll_end(animate=False)
        else:
            self._has_new_content = True
        self._refresh_navigation()

    def _refresh_navigation(self):
        if self.query("#jump-latest"):
            following = self.query_one("#conversation", ConversationView).following
            if following:
                self._has_new_content = False
            button = self.query_one("#jump-latest", GlyphButton)
            button.display = not following
            button.update("↓ 新内容" if self._has_new_content else "↓ 回到底部")
            self.query_one("#status-row").display = bool(self._status_message) or not following

    def on_conversation_view_following_changed(self):
        self._refresh_navigation()

    async def _append(self, content, kind="user"):
        await self.query_one("#transcript").mount(self._static(content, kind))
        self._follow_latest(force=True)

    def append_line(self, content, kind):
        self.query_one("#transcript").mount(self._static(content, kind))
        self._follow_latest(force=kind == "user")

    def event_update(self, message):
        message = clean_output(message)
        prefix, separator, _ = message.partition("] ")
        name = prefix.removeprefix("[")
        role = name if separator and (name in {"analysis", "review", "aggregate", "planner", "reviewer", "team_summary"}
                                     or name.startswith("worker:")) else ""
        event = message.removeprefix(f"[{role}] ") if role else message
        if event.startswith("[待办]\n"):
            if self._todo_widget is None:
                self._todo_widget = TodoPanel()
                self.query_one("#transcript").mount(self._todo_widget)
            self._todo_widget.show(event.removeprefix("[待办]\n"))
            self._follow_latest()
            return
        if event.startswith("[模型]"):
            self.set_activity("请求模型 · " + event.removeprefix("[模型]").strip())
            model = self._display_model or self._selected_profile().get("model") or "当前模型"
            message = (f"[{role}] " if role else "") + "[模型] 请求模型 · " + clean_output(model)
        if event.startswith("[工具]"):
            self.set_activity("工具 · " + event.removeprefix("[工具]").strip())
        if event.startswith(("[模型]", "[工具]")) and self._progress_widget is None:
            self._progress_widget = ToolProgress()
            self.query_one("#transcript").mount(self._progress_widget)
        if self._progress_widget is not None and self._progress_widget.observe(message, role):
            self._follow_latest()
        else:
            self.append_line(message, "event")

    def stream_update(self, kind, text):
        if kind == "start":
            self._response_widget = None
            self._response_text = ""
        elif kind == "delta":
            self._response_text += text
            if self._response_widget is None:
                self._response_widget = self._static(self._response_text, "assistant")
                self._turn_answer_widget = self._response_widget
                self._response_widget.set_task(self._task_status)
                self.query_one("#transcript").mount(self._response_widget)
            else:
                self._response_widget.set_content(self._response_text)
            self._follow_latest()
        elif kind == "end":
            if self._progress_widget is not None:
                self._progress_widget.complete_request()
        elif kind == "abort":
            if self._response_widget is not None:
                self._response_widget.set_content("[回答未完成]\n" + self._response_text, "error")
            self._response_widget = None
            self._response_text = ""

    def finish_answer(self, answer):
        if self._progress_widget is not None:
            for role in {row["role"] for row in self._progress_widget.rows}:
                self._progress_widget.complete_request(role)
        if self._response_widget is not None:
            self._response_widget.set_content(answer)
            self._turn_answer_widget = self._response_widget
        else:
            self._turn_answer_widget = self._static(answer, "assistant")
            self.query_one("#transcript").mount(self._turn_answer_widget)
        self._turn_answer_widget.set_task(self._task_status)
        self._follow_latest()
        self._response_widget = None
        self._response_text = ""

    async def clear_transcript_sync(self):
        self._response_widget = None
        self._turn_answer_widget = None
        self._response_text = ""
        self._progress_widget = None
        self._todo_widget = None
        await self.query_one("#transcript").remove_children()
        self._follow_latest(force=True)

    async def replace_transcript_sync(self, rows):
        self._task_status = TaskStatus()
        self._refresh_status()
        self._response_widget = None
        self._turn_answer_widget = None
        self._response_text = ""
        self._progress_widget = None
        self._todo_widget = None
        transcript = self.query_one("#transcript")
        await transcript.remove_children()
        for kind, text in rows:
            await transcript.mount(self._static(text, kind))
        self._follow_latest(force=True)
