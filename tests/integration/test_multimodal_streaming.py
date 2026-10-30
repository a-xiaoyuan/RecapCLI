"""真实 SDK + 本地 SSE 模拟服务：附件、工具调用、流式正文与持久化。"""

from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
from threading import Event, Lock
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI
from PIL import Image

from recapcli.cli import run_turn
from recapcli.config import Config, ConfigError, load_config
from recapcli.core.agent import run_agent
from recapcli.core.budget import AgentCancelled
from recapcli.core.compaction import prepare_compaction
from recapcli.images import image_data_url, prepare_user_message
from recapcli.models import ModelClient
from recapcli.storage.session import SessionStore
from recapcli.storage.tasks import TaskStore
from recapcli.streaming import IncompleteStreamError
from recapcli.ui.terminal_ui import TerminalUI


def chunk(content=None, calls=None, finish=None, usage=None):
    delta = {"role": "assistant"}
    if content is not None:
        delta["content"] = content
    if calls is not None:
        delta["tool_calls"] = calls
    return {"id": "chat-stream", "object": "chat.completion.chunk", "created": 1, "model": "offline",
            "choices": [] if usage is not None else [{"index": 0, "delta": delta, "finish_reason": finish}],
            **({"usage": usage} if usage is not None else {})}


def text_events(text="你好"):
    return [chunk(text[:1]), chunk(text[1:]), chunk(finish="stop"),
            chunk(usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})]


def sse(events):
    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                          content="".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events).encode("utf-8") + b"data: [DONE]\n\n")


def plain(text):
    return httpx2.Response(200, json={"id": "plain", "object": "chat.completion", "created": 1,
        "model": "offline", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": text}}]})


class MultimodalStreamingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name).resolve()
        self.image = self.workspace / "error screen.png"
        Image.new("RGB", (32, 24), "red").save(self.image)
        self.requests = []
        self.request_lock = Lock()

    @contextmanager
    def sdk(self, replies, *, images=True, streaming=True, protocol="chat_completions"):
        def handler(request):
            with self.request_lock:
                self.requests.append(json.loads(request.content))
                return replies[len(self.requests) - 1]

        def factory(**options):
            return OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **options)

        config = Config("offline-key", "offline", "https://offline.invalid/v1", images=images,
                        streaming=streaming, protocol=protocol)
        with ModelClient(config, client_factory=factory) as client:
            yield client

    def test_image_stream_and_restore_keep_snapshot_without_base64_in_history(self):
        output = io.StringIO()
        ui = TerminalUI(output, io.StringIO(), color="never")
        store = SessionStore(self.workspace)
        session = store.new_session("offline")
        with self.sdk([sse(text_events("这是红色图片")), sse(text_events("仍是红色"))]) as client:
            answer = run_turn(client, "offline", self.workspace, store, ui, session,
                              '@image:<error screen.png> 这张图是什么颜色？')
            ui.answer(answer)
            store.save(session)
            raw = store.path_for(session["id"]).read_text(encoding="utf-8")
            self.assertNotIn("base64", raw)
            self.assertEqual(output.getvalue().count("这是红色图片"), 1)
            self.assertEqual(len(session["messages"][0]["attachments"]), 1)
            self.image.unlink()
            restored = store.load(session["id"])
            second = run_turn(client, "offline", self.workspace, store, ui, restored, "再确认一下颜色")
            ui.answer(second)
        for request in self.requests:
            self.assertTrue(request["stream"])
            images = [part for message in request["messages"] if isinstance(message["content"], list)
                      for part in message["content"] if part["type"] == "image_url"]
            self.assertTrue(images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        tasks = TaskStore(self.workspace)
        for task in tasks.list_tasks():
            data = tasks.get(task["id"])
            self.assertEqual(data["usage"]["total_tokens"], 7)
            self.assertNotIn("base64", json.dumps(data))

    def test_image_responses_conversion_and_encrypted_state_survive_stream(self):
        final = {"id": "resp-one", "object": "response", "created_at": 1, "model": "offline", "status": "completed",
                 "output": [{"type": "reasoning", "id": "rs", "summary": [], "encrypted_content": "opaque"},
                            {"type": "message", "id": "msg", "status": "completed", "role": "assistant",
                             "content": [{"type": "output_text", "text": "红色", "annotations": []}]}],
                 "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}}
        events = [{"type": "response.output_text.delta", "item_id": "msg", "output_index": 1,
                   "content_index": 0, "delta": "红", "sequence_number": 1, "logprobs": []},
                  {"type": "response.output_text.delta", "item_id": "msg", "output_index": 1,
                   "content_index": 0, "delta": "色", "sequence_number": 2, "logprobs": []},
                  {"type": "response.completed", "sequence_number": 3, "response": final}]
        updates = []
        with self.sdk([sse(events)], protocol="responses") as client:
            result = run_agent(client, "offline", '@image:"error screen.png" 描述图片', self.workspace,
                               on_event=lambda text: None, on_stream=lambda kind, text: updates.append((kind, text)))
        self.assertEqual(result.answer, "红色")
        self.assertEqual([text for kind, text in updates if kind == "delta"], ["红", "色"])
        request = self.requests[0]
        self.assertFalse(request["store"])
        parts = request["input"][-1]["content"]
        self.assertEqual([part["type"] for part in parts], ["input_text", "input_image"])
        self.assertEqual(result.messages[-1]["provider_state"]["items"][0]["encrypted_content"], "opaque")

    def test_streamed_tool_arguments_are_complete_before_execution(self):
        (self.workspace / "README.md").write_text("hello", encoding="utf-8")
        fragments = [chunk(calls=[{"index": 0, "id": "call-one", "type": "function",
                                  "function": {"name": "read_file", "arguments": '{"pa'}}]),
                     chunk(calls=[{"index": 0, "function": {"arguments": 'th":"README.md"}'}}]),
                     chunk(finish="tool_calls")]
        updates = []
        with self.sdk([sse(fragments), sse(text_events("读取完成"))]) as client:
            result = run_agent(client, "offline", "读 README", self.workspace, on_event=lambda text: None,
                               on_stream=lambda kind, text: updates.append((kind, text)))
        self.assertEqual(result.answer, "读取完成")
        self.assertEqual(self.requests[1]["messages"][-1]["tool_call_id"], "call-one")
        self.assertIn("hello", self.requests[1]["messages"][-1]["content"])
        self.assertEqual("".join(text for kind, text in updates if kind == "delta"), "读取完成")

    def test_interrupted_stream_does_not_save_partial_answer_or_execute_tools(self):
        output, errors = io.StringIO(), io.StringIO()
        store = SessionStore(self.workspace)
        session = store.new_session("offline")
        events = [chunk("还没完成"), chunk(calls=[{"index": 0, "id": "edit", "type": "function",
                   "function": {"name": "edit_file", "arguments": '{"path":"new.txt",'}}])]
        with self.sdk([sse(events)]) as client:
            with self.assertRaises(IncompleteStreamError):
                run_turn(client, "offline", self.workspace, store, TerminalUI(output, errors, color="never"),
                         session, "创建文件", capabilities=("read", "write"))
        self.assertEqual(session["messages"], [])
        self.assertFalse((self.workspace / "new.txt").exists())
        self.assertIn("回答未完成", output.getvalue())
        task = TaskStore(self.workspace).list_tasks()[0]
        self.assertEqual(task["status"], "failed")

    def test_cancellation_during_delta_stops_before_another_request(self):
        cancelled = Event()
        def update(kind, text):
            if kind == "delta":
                cancelled.set()
        with self.sdk([sse(text_events("长回答"))]) as client:
            with self.assertRaises(AgentCancelled):
                run_agent(client, "offline", "回答", self.workspace, on_event=lambda text: None,
                          on_stream=update, cancel_event=cancelled)
        self.assertEqual(len(self.requests), 1)

    def test_image_capability_and_invalid_paths_fail_without_a_model_request(self):
        with self.sdk([], images=False) as client:
            with self.assertRaisesRegex(ValueError, "images=false"):
                run_agent(client, "offline", '@image:<error screen.png> 描述', self.workspace)
        self.assertEqual(self.requests, [])
        for prompt in ("@image:", "@image:../outside.png", "@image:https://example.com/image.png"):
            with self.subTest(prompt=prompt), self.assertRaises((ValueError, OSError)):
                prepare_user_message(prompt, self.workspace)
        (self.workspace / "bad.png").write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            prepare_user_message("@image:bad.png", self.workspace)

    def test_modified_attachment_is_rejected_and_compaction_uses_only_text(self):
        user = prepare_user_message('@image:<error screen.png> 解释图片', self.workspace)
        history = [user, {"role": "assistant", "content": "画面是红色"},
                   {"role": "user", "content": "二"}, {"role": "assistant", "content": "二答"},
                   {"role": "user", "content": "三"}, {"role": "assistant", "content": "三答"}]
        compact, end = prepare_compaction(history, None, 32768)
        self.assertEqual(end, 2)
        self.assertNotIn("base64", json.dumps(compact))
        self.assertIn("摘要未重新观察图片", compact[-1]["content"])
        attachment = user["attachments"][0]
        path = self.workspace / ".recapcli" / "attachments" / attachment["sha256"]
        path.write_bytes(b"x" * attachment["size"])
        with self.assertRaisesRegex(ValueError, "校验失败"):
            image_data_url(attachment, self.workspace)

    def test_first_delta_arrives_before_the_remaining_http_stream_is_consumed(self):
        updates = []
        observed = []
        class ObservedStream(httpx2.SyncByteStream):
            def __iter__(self):
                yield ("data: " + json.dumps(chunk("第一段")) + "\n\n").encode()
                observed.append(any(kind == "delta" and text == "第一段" for kind, text in updates))
                for event in [chunk("第二段"), chunk(finish="stop")]:
                    yield ("data: " + json.dumps(event) + "\n\n").encode()
                yield b"data: [DONE]\n\n"
        response = httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=ObservedStream())
        with self.sdk([response]) as client:
            result = run_agent(client, "offline", "分段回答", self.workspace, on_event=lambda text: None,
                               on_stream=lambda kind, text: updates.append((kind, text)))
        self.assertEqual(observed, [True])
        self.assertEqual(result.answer, "第一段第二段")

    def test_plan_streams_only_summary_and_multi_streams_only_parent(self):
        updates = []
        replies = [plain('{"steps":["分析图片"]}'), plain("已观察图片"),
                   plain('{"status":"completed","reason":"已有观察结果","steps":[]}'), sse(text_events("图片总结"))]
        with self.sdk(replies) as client:
            result = run_agent(client, "offline", '@image:<error screen.png> 分析', self.workspace,
                               mode="plan", max_steps=4, on_event=lambda text: None,
                               on_stream=lambda kind, text: updates.append((kind, text)))
        self.assertIn("图片总结", result.answer)
        self.assertEqual("".join(text for kind, text in updates if kind == "delta"), "图片总结")
        self.assertEqual([request.get("stream", False) for request in self.requests], [False, False, False, True])
        self.requests.clear()
        updates.clear()
        with self.sdk([plain("分析意见"), plain("审查意见"), sse(text_events("父任务总结"))]) as client:
            result = run_agent(client, "offline", '@image:<error screen.png> 分析', self.workspace,
                               mode="multi", max_steps=3, on_event=lambda text: None,
                               on_stream=lambda kind, text: updates.append((kind, text)))
        self.assertEqual("".join(text for kind, text in updates if kind == "delta"), result.answer)
        self.assertEqual(len(result.messages[0]["attachments"]), 1)
        self.assertEqual(sum(request.get("stream", False) for request in self.requests), 1)

    def test_non_streaming_image_profile_and_config_defaults(self):
        with self.sdk([plain("整段图片回答")], streaming=False) as client:
            result = run_agent(client, "offline", '@image:<error screen.png> 分析', self.workspace,
                               on_event=lambda text: None, on_stream=lambda *args: self.fail("已关闭流式"))
        self.assertEqual(result.answer, "整段图片回答")
        self.assertNotIn("stream", self.requests[0])
        with patch("recapcli.config.dotenv_values", return_value={"OPENAI_API_KEY": "offline", "OPENAI_MODEL": "offline"}), \
                patch("recapcli.config.MODELS_FILE", self.workspace / "absent.toml"), \
                patch.dict("os.environ", {}, clear=True):
            self.assertTrue(load_config().streaming)
            self.assertFalse(load_config().images)
            with patch.dict("os.environ", {"OPENAI_IMAGES": "true", "OPENAI_STREAMING": "false"}):
                self.assertTrue(load_config().images)
                self.assertFalse(load_config().streaming)
            with patch.dict("os.environ", {"OPENAI_IMAGES": "maybe"}):
                with self.assertRaises(ConfigError):
                    load_config()


if __name__ == "__main__":
    unittest.main()
