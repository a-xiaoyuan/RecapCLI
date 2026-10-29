"""模型配置与 CLI 的离线集成验收；真实 SDK 仅连接 MockTransport。"""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
from threading import Lock
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

from recapcli.cli import main, run_turn
from recapcli.config import ConfigError, list_model_profiles, load_config
from recapcli.core.agent import AgentLimits
from recapcli.core.compaction import history_hash, validate_summary
from recapcli.models import ModelClient
from recapcli.storage.session import SessionStore
from recapcli.storage.tasks import TaskStore
from recapcli.ui.terminal_ui import TerminalUI


PROFILES = '''
[models.fast]
model = "fast-model"
base_url = "https://fast.invalid/v1"
api_key_env = "FAST_KEY"

[models.reasoning]
model = "reasoning-model"
protocol = "responses"
base_url = "https://responses.invalid/v1"
api_key_env = "REASONING_KEY"
context_bytes = 32768
images = true
streaming = false

[models.textonly]
model = "text-model"
base_url = "https://text.invalid/v1"
api_key_env = "FAST_KEY"
tools = false

[models.missingkey]
model = "unavailable-model"
api_key_env = "MISSING_KEY"
'''


def chat_reply(text="离线回答"):
    return {"id": "chat-test", "object": "chat.completion", "created": 1, "model": "offline",
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


def message(text="离线回答", identifier="msg-test"):
    return {"type": "message", "id": identifier, "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def reasoning(identifier="rs-test", opaque="opaque-test-state"):
    return {"type": "reasoning", "id": identifier, "summary": [], "encrypted_content": opaque}


def response_reply(*items):
    return {"id": "resp-test", "object": "response", "created_at": 1, "model": "reasoning-model",
            "status": "completed", "output": list(items),
            "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 1}}}


class ModelTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "target"
        self.workspace.mkdir()
        self.env_file = self.root / ".env"
        self.models_file = self.root / "models.toml"
        self.env_file.write_text("OPENAI_API_KEY=file-offline-key\nOPENAI_MODEL=legacy-model\n"
                                 "OPENAI_BASE_URL=https://legacy.invalid/v1\n"
                                 "FAST_KEY=fast-offline-key\nREASONING_KEY=reasoning-offline-key\n",
                                 encoding="utf-8")
        self.models_file.write_text(PROFILES, encoding="utf-8")
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        stack.enter_context(patch("recapcli.config.ENV_FILE", self.env_file))
        stack.enter_context(patch("recapcli.config.MODELS_FILE", self.models_file))
        self.requests = []
        self.replies = []
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.ui = TerminalUI(self.output, self.errors, color="never")

    def sdk_factory(self, **options):
        def respond(request):
            body = json.loads(request.content)
            self.requests.append({"path": request.url.path, "host": request.url.host,
                                  "body": body, "authorization": request.headers.get("authorization")})
            if self.replies:
                reply = self.replies.pop(0)
                if isinstance(reply, BaseException):
                    raise reply
                if isinstance(reply, httpx2.Response):
                    return reply
            else:
                text = "回答来自 " + body["model"]
                reply = response_reply(message(text)) if request.url.path.endswith("/responses") else chat_reply(text)
            return httpx2.Response(200, json=reply)
        return OpenAI(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))

    def run_cli(self, *arguments, prompts=None):
        args = [*arguments, "--workspace", str(self.workspace), "--banner", "off", "--color", "never"]
        with patch("recapcli.cli.OpenAI", side_effect=self.sdk_factory), \
                redirect_stdout(self.output), redirect_stderr(self.errors):
            if prompts is None:
                return main(args)
            with patch("builtins.input", side_effect=prompts):
                return main(args)

    def runtime(self, profile="reasoning"):
        config = load_config(profile)
        return ModelClient(config, client_factory=self.sdk_factory)

    def new_session(self):
        self.store = SessionStore(self.workspace)
        self.session = self.store.new_session("reasoning-model")
        return self.session

    def run_turn(self, client, prompt="分析项目", **options):
        return run_turn(client, client.model, self.workspace, self.store, self.ui, self.session,
                        prompt, memory_enabled=False, auto_compact=False, **options)

    def task(self):
        tasks = TaskStore(self.workspace, create=False)
        return tasks.get(tasks.list_tasks()[0]["id"])


class ModelConfigTests(ModelTestCase):
    def test_legacy_env_and_environment_priority_ignore_target_configuration(self):
        self.models_file.unlink()
        (self.workspace / ".env").write_text("OPENAI_API_KEY=wrong-key\nOPENAI_MODEL=wrong-model", encoding="utf-8")
        (self.workspace / "models.toml").write_text('default = "wrong"', encoding="utf-8")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-offline-key"}):
            config = load_config()
            self.assertEqual((config.api_key, config.model, config.profile, config.protocol),
                             ("environment-offline-key", "legacy-model", "default", "chat_completions"))
            self.assertEqual(self.run_cli("你好"), 0)
        self.assertEqual(self.requests[0]["host"], "legacy.invalid")
        self.assertEqual(self.requests[0]["authorization"], "Bearer environment-offline-key")
        self.assertEqual(self.requests[0]["body"]["model"], "legacy-model")

    def test_profiles_use_app_path_and_expose_capabilities_without_keys(self):
        (self.workspace / "models.toml").write_text('default = "wrong"', encoding="utf-8")
        self.models_file.write_text('default = "reasoning"\n' + PROFILES, encoding="utf-8")
        config = load_config()
        self.assertEqual((config.profile, config.model, config.protocol),
                         ("reasoning", "reasoning-model", "responses"))
        self.assertEqual(config.context_bytes, 32768)
        self.assertTrue(config.images)
        self.assertFalse(config.streaming)
        profiles = {item["name"]: item for item in list_model_profiles()}
        self.assertTrue(profiles["reasoning"]["default"])
        self.assertFalse(profiles["missingkey"]["configured"])
        self.assertFalse(profiles["textonly"]["tools"])
        self.assertNotIn("offline-key", json.dumps(profiles))

    def test_invalid_profile_documents_fail_explicitly(self):
        invalid = ["[models", 'unknown = 1', 'default = "missing"',
                   '[models.bad]\nmodel = "x"\napi_key = "not-allowed"',
                   '[models.bad]\nmodel = "x"\nprotocol = "unknown"',
                   '[models.bad]\nmodel = "x"\nprotocol = []',
                   '[models.bad]\nmodel = "x"\nprotocol = {}',
                   '[models.bad]\nmodel = "x"\ntools = "true"',
                   '[models.bad]\nmodel = "x"\ncontext_bytes = true',
                   '[models.bad]\nmodel = "x"\ncontext_bytes = 0',
                   '[models.bad]\nmodel = "x"\nbase_url = "https://user:pass@host.invalid"',
                   '[models.bad]\nmodel = "x"\nbase_url = "https://host.invalid/?key=hidden"',
                   '[models.bad]\nmodel = "x"\napi_key_env = "NOT A NAME"']
        for document in invalid:
            with self.subTest(document=document):
                self.models_file.write_text(document, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config()

    def test_missing_key_and_unknown_profile_make_no_sdk_client(self):
        for profile in ("missingkey", "unknown"):
            with self.subTest(profile=profile), patch("recapcli.cli.OpenAI") as factory, \
                    redirect_stderr(self.errors), self.assertRaises(SystemExit) as error:
                main(["你好", "--profile", profile, "--workspace", str(self.workspace), "--banner", "off"])
            self.assertEqual(error.exception.code, 2)
            factory.assert_not_called()

    def test_models_listing_needs_no_keys_and_creates_no_client_or_state(self):
        self.env_file.write_text("", encoding="utf-8")
        with patch("recapcli.cli.OpenAI") as factory, redirect_stdout(self.output):
            self.assertEqual(main(["--models", "--workspace", str(self.workspace), "--color", "never"]), 0)
        factory.assert_not_called()
        self.assertIn("reasoning", self.output.getvalue())
        self.assertIn("responses", self.output.getvalue())
        self.assertFalse((self.workspace / ".recapcli").exists())


class ModelCLITests(ModelTestCase):
    def test_profile_is_saved_restored_and_explicitly_overridden(self):
        self.assertEqual(self.run_cli("第一问", "--profile", "reasoning"), 0)
        saved = SessionStore(self.workspace).latest()
        self.assertEqual(saved["model_profile"], "reasoning")
        self.assertEqual(self.run_cli("第二问", "--continue"), 0)
        self.assertEqual(self.requests[-1]["path"], "/v1/responses")
        self.assertEqual(self.requests[-1]["body"]["model"], "reasoning-model")
        self.assertEqual(self.run_cli("第三问", "--continue", "--profile", "fast"), 0)
        self.assertEqual(self.requests[-1]["path"], "/v1/chat/completions")
        self.assertNotIn("provider_state", json.dumps(self.requests[-1]["body"]))
        restored = SessionStore(self.workspace).latest()
        self.assertEqual((restored["id"], restored["model_profile"]), (saved["id"], "fast"))
        self.assertEqual(len(restored["messages"]), 6)

    def test_interactive_switch_only_changes_next_question_without_probe(self):
        self.assertEqual(self.run_cli("--profile", "fast", prompts=[
            "第一问", "/model reasoning", "/model", "/models", "第二问", "/exit"]), 0)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([item["body"]["model"] for item in self.requests], ["fast-model", "reasoning-model"])
        self.assertEqual([item["path"] for item in self.requests], ["/v1/chat/completions", "/v1/responses"])
        self.assertIn("尚未请求接口", self.output.getvalue())
        self.assertEqual(SessionStore(self.workspace).latest()["model_profile"], "reasoning")

    def test_failed_interactive_switch_keeps_current_profile_and_session(self):
        self.assertEqual(self.run_cli("--profile", "fast", prompts=[
            "第一问", "/model missingkey", "/model unknown", "第二问", "/exit"]), 0)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(item["body"]["model"] == "fast-model" for item in self.requests))
        saved = SessionStore(self.workspace).latest()
        self.assertEqual((saved["model_profile"], saved["model"], len(saved["messages"])),
                         ("fast", "fast-model", 4))
        self.assertIn("MISSING_KEY", self.errors.getvalue())

    def test_switch_client_creation_failure_preserves_old_runtime(self):
        with self.runtime("fast") as client:
            before = client.config
            with patch.object(client, "_factory", side_effect=RuntimeError("offline construction failure")):
                with self.assertRaisesRegex(RuntimeError, "construction"):
                    client.switch("reasoning")
            self.assertIs(client.config, before)
            self.new_session()
            self.run_turn(client)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["body"]["model"], "fast-model")

    def test_tools_disabled_and_small_context_stop_before_http(self):
        self.new_session()
        with self.runtime("textonly") as client, self.assertRaisesRegex(ValueError, "tools=false"):
            self.run_turn(client)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.task()["status"], "failed")
        with self.runtime("reasoning") as client, self.assertRaisesRegex(RuntimeError, "上下文"):
            self.run_turn(client, "x" * 40000, limits=AgentLimits(max_context_bytes=100000))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.task()["metadata"]["limits"]["max_context_bytes"], 32768)

    def test_switch_reuses_clients_and_model_mismatch_never_sends_state(self):
        clients = []

        def factory(**options):
            sdk = self.sdk_factory(**options)
            clients.append(sdk)
            return sdk

        with ModelClient(load_config("fast"), client_factory=factory) as client:
            client.switch("reasoning")
            client.switch("fast")
            client.switch("reasoning")
            self.assertEqual(len(clients), 2)
            with self.assertRaisesRegex(ValueError, "不一致"):
                client.complete(model="unexpected-model", messages=[{"role": "user", "content": "test"}])
            self.assertEqual(self.requests, [])
        self.assertTrue(all(sdk.is_closed() for sdk in clients))


class ResponsesRuntimeTests(ModelTestCase):
    def test_tool_roundtrip_persistence_resume_and_usage_trace(self):
        self.new_session()
        (self.workspace / "app.py").write_text("print('checked')\n", encoding="utf-8")
        call = {"type": "function_call", "id": "fc-read", "call_id": "call-read", "name": "read_file",
                "arguments": '{"path":"app.py"}', "status": "completed"}
        self.replies = [response_reply(reasoning(), call), response_reply(message("已读取 app.py:1")),
                        response_reply(message("继续回答", "msg-second"))]
        with self.runtime() as client:
            self.assertIn("app.py", self.run_turn(client))
            expected_origin = client.origin
        self.assertEqual(len(self.requests), 2)
        second = self.requests[1]["body"]["input"]
        self.assertTrue(any(item.get("encrypted_content") == "opaque-test-state" for item in second))
        result = next(item for item in second if item.get("type") == "function_call_output")
        self.assertEqual(result["call_id"], "call-read")
        self.assertIn("checked", result["output"])
        self.store.save(self.session)
        self.session = self.store.load(self.session["id"])
        self.assertEqual(self.session["messages"][1]["provider_state"]["origin"], expected_origin)
        task = self.task()
        self.assertEqual((task["status"], task["model_requests"], task["usage"]["total_tokens"]),
                         ("returned", 2, 28))
        self.assertEqual(task["metadata"]["model_config"]["protocol"], "responses")
        self.assertEqual(task["verification_status"], "unverified")
        self.assertEqual(len([event for event in task["events"] if event["kind"] == "tool_result"]), 1)
        self.assertNotIn("reasoning-offline-key", json.dumps(task))
        with self.runtime() as client:
            self.run_turn(client, "继续")
        self.assertEqual(len(self.requests), 3)
        self.assertIn("opaque-test-state", json.dumps(self.requests[2]["body"]))

    def test_changed_credentials_do_not_replay_other_origin_state(self):
        self.new_session()
        self.replies = [response_reply(reasoning(), message("第一答")), response_reply(message("第二答"))]
        with self.runtime() as client:
            self.run_turn(client, "第一问")
        original = deepcopy(self.session["messages"])
        with patch.dict(os.environ, {"REASONING_KEY": "rotated-offline-key"}), self.runtime() as client:
            self.run_turn(client, "第二问")
        self.assertNotIn("opaque-test-state", json.dumps(self.requests[1]["body"]))
        self.assertIn("第一答", json.dumps(self.requests[1]["body"], ensure_ascii=False))
        self.assertEqual(self.session["messages"][:2], original)

    def test_compaction_excludes_private_state_without_changing_source_hash(self):
        self.new_session()
        with self.runtime() as client:
            for index in range(4):
                text = f"回答{index}:" + "证据" * 150
                self.replies.append(response_reply(reasoning(f"rs-{index}", f"opaque-only-{index}"),
                                                   message(text, f"msg-{index}")))
                self.run_turn(client, f"问题{index}:" + "背景" * 150)
            original = deepcopy(self.session["messages"])
            content = {"goal": "梳理代码", "constraints": "保留原文", "decisions": "只读",
                       "actions": "已分析", "verification": "未验收", "pending": "继续"}
            self.replies.append(response_reply(message(json.dumps(content, ensure_ascii=False), "msg-summary")))
            self.run_turn(client, "/compact", compact_only=True)
        self.assertEqual(self.session["messages"], original)
        payload = json.dumps(self.requests[-1]["body"], ensure_ascii=False)
        self.assertNotIn("opaque-only", payload)
        self.assertNotIn("provider_state", payload)
        self.assertIn("回答0", payload)
        summary = self.session["summary"]
        self.assertEqual(summary["source_hash"], history_hash(original[:summary["covered_messages"]]))
        validate_summary(summary, self.session["messages"])
        self.store.save(self.session)
        validate_summary(self.store.load(self.session["id"])["summary"], original)

    def test_incomplete_response_preserves_session_and_records_failure_without_retry(self):
        self.new_session()
        original = deepcopy(self.session)
        incomplete = response_reply(message("partial"))
        incomplete["status"] = "incomplete"
        self.replies = [incomplete]
        with self.runtime() as client, self.assertRaisesRegex(RuntimeError, "未完整结束"):
            self.run_turn(client)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.session, original)
        self.assertEqual(self.task()["status"], "failed")
        self.assertEqual(self.task()["usage"]["total_tokens"], 14)

    def test_plan_responses_keeps_stage_permissions_and_valid_history(self):
        self.new_session()
        texts = [json.dumps({"steps": ["解释入口"]}), "入口已说明",
                 json.dumps({"status": "completed", "reason": "模型核对", "steps": []}), "最终说明"]
        self.replies = [response_reply(message(text, f"msg-plan-{index}")) for index, text in enumerate(texts)]
        with self.runtime() as client:
            answer = self.run_turn(client, mode="plan", max_steps=5)
        self.assertIn("最终说明", answer)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual([item["body"]["tool_choice"] for item in self.requests], ["none", "auto", "none", "none"])
        self.store.save(self.session)
        self.assertEqual(len(self.store.load(self.session["id"])["messages"]), 5)
        self.assertEqual(self.task()["usage"]["total_tokens"], 56)

    def test_multi_responses_keeps_each_role_state_isolated_and_shares_budget(self):
        self.new_session()
        (self.workspace / "app.py").write_text("print('multi')\n", encoding="utf-8")
        lock = Lock()
        counts = {}

        def respond(request):
            body = json.loads(request.content)
            system = body["input"][0]["content"]
            role = next(name for name in ("analysis", "review", "aggregate")
                        if f"当前角色：{name}" in system)
            with lock:
                counts[role] = counts.get(role, 0) + 1
                number = counts[role]
                self.requests.append({"role": role, "number": number, "body": body})
            if role == "aggregate":
                reply = response_reply(message("已汇总两个角色", "msg-aggregate"))
            elif number == 1:
                call = {"type": "function_call", "id": f"fc-{role}", "call_id": f"call-{role}",
                        "name": "read_file", "arguments": '{"path":"app.py"}', "status": "completed"}
                reply = response_reply(reasoning(f"rs-{role}", f"opaque-{role}-private"), call)
            else:
                reply = response_reply(message(f"{role} 已读取 app.py:1", f"msg-{role}"))
            return httpx2.Response(200, json=reply)

        def factory(**options):
            return OpenAI(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))

        with ModelClient(load_config("reasoning"), client_factory=factory) as client:
            answer = self.run_turn(client, mode="multi", max_steps=5)
        self.assertIn("已汇总两个角色", answer)
        self.assertEqual(counts, {"analysis": 2, "review": 2, "aggregate": 1})
        self.assertEqual(len(self.requests), 5)
        for role, other in (("analysis", "review"), ("review", "analysis")):
            request = next(item["body"] for item in self.requests if item["role"] == role and item["number"] == 2)
            encoded = json.dumps(request)
            self.assertIn(f"opaque-{role}-private", encoded)
            self.assertNotIn(f"opaque-{other}-private", encoded)
            outputs = [item for item in request["input"] if item.get("type") == "function_call_output"]
            self.assertEqual([item["call_id"] for item in outputs], [f"call-{role}"])
            self.assertIn("multi", outputs[0]["output"])
        aggregate = next(item["body"] for item in self.requests if item["role"] == "aggregate")
        self.assertEqual(aggregate["tool_choice"], "none")
        self.assertNotIn("tools", aggregate)
        self.assertNotIn("opaque-", json.dumps(aggregate))
        self.assertEqual([item["role"] for item in self.session["messages"]], ["user", "assistant"])
        self.assertNotIn("provider_state", json.dumps(self.session["messages"]))
        self.store.save(self.session)
        task = self.task()
        self.assertEqual((task["model_requests"], task["usage"]["total_tokens"]), (5, 70))
        self.assertEqual(len([event for event in task["events"] if event["kind"] == "tool_result"]), 2)
        budget = next(event["data"] for event in task["events"] if event["kind"] == "subagent_budget")
        self.assertEqual((budget["model_requests"], budget["tool_calls"]), (5, 2))


if __name__ == "__main__":
    unittest.main()
