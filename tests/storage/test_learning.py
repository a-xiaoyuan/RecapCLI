"""/memory learn：从结束的任务提炼经验候选，只请求一次模型，结果接纳前不生效。"""

import io
import json
from pathlib import Path
import tempfile
import unittest

import httpx2
from openai import OpenAI

from recapcli.application.commands import handle_command
from recapcli.application.help import help_text
from recapcli.core.learning import MAX_DIGEST_CHARS, failure_digest, parse_lesson
from recapcli.storage.session import SessionStore, content_version
from recapcli.storage.tasks import TaskStore
from recapcli.ui.terminal_ui import TerminalUI

LESSON = {"trigger": "修复规则摘要显示且根规则缺失、子目录规则有效时",
          "lesson": "摘要要写出实际加载的子规则文件名，不要沿用根文件名 AGENTS.md",
          "evidence": "验收项 missing root with loaded nested rule 失败；回答写成 AGENTS.md · 已加载"}


def completion(content):
    return {"id": "x", "object": "chat.completion", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


class LearnCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.store = SessionStore(self.workspace)
        self.session = self.store.new_session("test-model")
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.ui = TerminalUI(self.output, self.errors, color="never")
        self.requests = []
        self.reply = json.dumps(LESSON, ensure_ascii=False)

    def client(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx2.Response(200, json=completion(self.reply))

        client = OpenAI(api_key="offline-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        self.addCleanup(client.close)
        return client

    def failed_task(self, status="incomplete"):
        tasks = TaskStore(self.workspace)
        task_id = tasks.start(self.session["id"], "修复规则摘要显示", "test-model", "react", {})
        tasks.append(task_id, "tool_start", {"call_id": "e", "name": "edit_file",
                                            "arguments": json.dumps({"path": "pkg/status.py"})})
        tasks.append(task_id, "tool_result", {"call_id": "e", "name": "edit_file",
                                             "result": {"ok": True, "path": "pkg/status.py"}})
        tasks.append(task_id, "tool_start", {"call_id": "c", "name": "run_command",
                                            "arguments": json.dumps({"argv": ["python", "-m", "unittest"]})})
        tasks.append(task_id, "tool_result", {"call_id": "c", "name": "run_command",
                                             "result": {"ok": True, "returncode": 1, "output": "FAILED (errors=2)"}})
        tasks.append(task_id, "acceptance_check", {"attempt": 1, "id": "nested", "status": "failed",
                                                  "reason": "摘要没有说明实际加载的规则"})
        if status != "running":
            tasks.finish(task_id, status, "模型请求次数用完", answer="已修复规则摘要。")
        return tasks.get(task_id)

    def learn(self, argument, *, client="default"):
        client = self.client() if client == "default" else client
        return handle_command(f"/memory learn {argument}", self.ui, self.store, self.session, "test-model",
                              client=client)

    def test_digest_keeps_failures_edits_and_note_within_limit(self):
        digest = failure_digest(self.failed_task(), "外部验收：missing root 用例失败")
        for expected in ("修复规则摘要显示", "incomplete", "pkg/status.py", "退出码 1", "FAILED (errors=2)",
                         "需求验收项 nested 未通过", "外部验收：missing root 用例失败", "已修复规则摘要。"):
            self.assertIn(expected, digest)
        huge = {**self.failed_task(), "prompt": "长" * 50000, "answer": "答" * 50000}
        self.assertLessEqual(len(failure_digest(huge, "注" * 50000)), MAX_DIGEST_CHARS)

    def test_learn_saves_an_inactive_candidate_with_its_evidence(self):
        task = self.failed_task()
        self.learn(f"{task['id']} 外部验收：missing root 用例失败")
        self.assertEqual(len(self.requests), 1)
        body = self.requests[0]
        self.assertFalse(body.get("tools"))
        self.assertIn("外部验收：missing root 用例失败", body["messages"][-1]["content"])
        [item] = self.store.memory_items()
        self.assertEqual((item["kind"], item["status"]), ("experience", "candidate"))
        self.assertTrue(item["text"].startswith("适用：修复规则摘要显示"))
        self.assertIn("做法：摘要要写出实际加载的子规则文件名", item["text"])
        source = item["source"]
        self.assertEqual((source["task_id"], source["method"]), (task["id"], "model_distilled"))
        self.assertEqual(source["distilled"], {**LESSON, "model": "test-model"})
        self.assertEqual(self.store.memory_context(query="规则摘要").text, "")
        self.assertIn(f"/memory accept {item['id']}", self.output.getvalue())

    def test_insufficient_or_malformed_replies_do_not_create_candidates(self):
        task = self.failed_task()
        for reply in (json.dumps({**LESSON, "lesson": ""}, ensure_ascii=False), "我觉得应该更仔细。",
                      json.dumps({"trigger": "x"}), "{坏的 JSON}"):
            with self.subTest(reply=reply):
                self.reply = reply
                self.learn(task["id"])
                self.assertEqual(self.store.memory_items(), [])
        self.assertIn("未生成", self.errors.getvalue())

    def test_learn_needs_a_model_and_a_finished_task(self):
        task = self.failed_task()
        self.learn(task["id"], client=None)
        self.assertIn("需要先配置模型", self.errors.getvalue())
        running = self.failed_task(status="running")
        self.learn(running["id"])
        self.learn("")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.memory_items(), [])
        self.assertIn("结束后再提炼", self.errors.getvalue())
        self.assertIn("用法：/memory learn 任务ID", self.errors.getvalue())

    def test_parse_lesson_accepts_fenced_json_and_trims_fields(self):
        lesson = parse_lesson("```json\n" + json.dumps({**LESSON, "lesson": "做" * 900}, ensure_ascii=False) + "\n```")
        self.assertEqual(lesson["trigger"], LESSON["trigger"])
        self.assertLessEqual(len(lesson["lesson"]), 500)

    def test_help_lists_memory_learn(self):
        self.assertIn("/memory learn", help_text("memory"))

    def test_evaluation_is_bound_to_the_experience_text_version(self):
        item = self.store.add_memory("改规则摘要时写出实际加载的文件", kind="experience", status="candidate")
        version = content_version(item)
        accepted = self.store.set_memory_status(item["id"], "active", force=True)
        self.assertEqual(content_version(accepted), version)
        evaluation = {"verdict": "passed", "content_version": version}
        stored = self.store.record_memory_evaluation(item["id"], evaluation)
        self.assertEqual(stored["evaluation"], evaluation)
        self.assertEqual(content_version(stored), version)
        self.store.update_memory(item["id"], "改过的正文")
        with self.assertRaisesRegex(ValueError, "被修改过"):
            self.store.record_memory_evaluation(item["id"], evaluation)
        fact = self.store.add_memory("普通事实")
        with self.assertRaisesRegex(ValueError, "只有经验"):
            self.store.record_memory_evaluation(fact["id"], {"content_version": content_version(fact)})


class ExperienceLifecycleTests(unittest.TestCase):
    """启用要有通过的对照；启用后被注入并记下版本；停用就不再注入；改正文要重新对照。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.store = SessionStore(self.workspace)
        self.session = self.store.new_session("test-model")
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.ui = TerminalUI(self.output, self.errors, color="never")
        self.item = self.store.add_memory("修复规则摘要时写出实际加载的子规则文件名", kind="experience",
                                          status="candidate")

    def command(self, text):
        return handle_command(text, self.ui, self.store, self.session, "test-model")

    def evaluate(self, verdict):
        return self.store.record_memory_evaluation(self.item["id"], {
            "verdict": verdict, "content_version": content_version(self.item), "reasons": ["样本不够"],
            "run_dir": "evals/runs/example"})

    def selected(self):
        return self.store.memory_context(query="修复规则摘要").selected_ids

    def test_accept_requires_a_passed_evaluation_of_the_current_text(self):
        self.command(f"/memory accept {self.item['id']}")
        self.evaluate("failed")
        self.command(f"/memory accept {self.item['id']}")
        self.assertEqual(self.errors.getvalue().count("还没有通过对照"), 2)
        self.assertEqual(self.selected(), ())
        self.command("/memory")
        self.assertIn("对照：不通过：样本不够", self.output.getvalue())
        self.evaluate("passed")
        self.command(f"/memory accept {self.item['id']}")
        [stored] = self.store.memory_items()
        self.assertEqual(stored["status"], "active")
        self.assertNotIn("activated_without_evaluation", stored)
        self.assertEqual(self.selected(), (self.item["id"],))

    def test_active_experience_is_recorded_with_its_version_and_expire_rolls_back(self):
        self.evaluate("passed")
        self.command(f"/memory accept {self.item['id']}")
        [source] = self.store.memory_context(query="修复规则摘要").sources
        self.assertEqual((source["content_version"], source["evaluation"]),
                         (content_version(self.item), "passed"))
        self.command(f"/memory expire {self.item['id']}")
        self.assertIn("不再进入模型上下文", self.output.getvalue())
        self.assertEqual(self.selected(), ())
        self.command(f"/memory activate {self.item['id']}")
        self.assertEqual(self.selected(), (self.item["id"],))

    def test_force_marks_the_skip_and_editing_requires_a_new_evaluation(self):
        self.command(f"/memory reject {self.item['id']} --force")
        self.assertIn("用法：/memory reject ID", self.errors.getvalue())
        self.command(f"/memory accept {self.item['id']} --force")
        [stored] = self.store.memory_items()
        self.assertTrue(stored["activated_without_evaluation"])
        self.assertIn("跳过对照", self.output.getvalue())
        [source] = self.store.memory_context(query="修复规则摘要").sources
        self.assertEqual(source["evaluation"], "skipped")
        edited = self.store.update_memory(self.item["id"], "修复规则摘要时写出实际加载的规则文件和字节数")
        self.assertEqual(edited["status"], "candidate")
        self.assertEqual(self.selected(), ())

    def test_editing_an_evaluated_experience_voids_the_old_result(self):
        self.evaluate("passed")
        self.command(f"/memory accept {self.item['id']}")
        self.store.update_memory(self.item["id"], "修复规则摘要时说明根规则没有加载")
        self.command("/memory")
        self.assertIn("正文已改，旧结论作废", self.output.getvalue())
        self.command(f"/memory accept {self.item['id']}")
        self.assertIn("还没有通过对照", self.errors.getvalue())

    def test_task_detail_lists_the_experience_versions_used(self):
        self.evaluate("passed")
        self.command(f"/memory accept {self.item['id']}")
        tasks = TaskStore(self.workspace)
        task_id = tasks.start(self.session["id"], "修复规则摘要", "test-model", "react", {})
        context = self.store.memory_context(query="修复规则摘要")
        tasks.append(task_id, "context", {"memory_ids": list(context.selected_ids), "memory_text": context.text,
                                          "memory_sources": list(context.sources),
                                          "experience_versions": [{"id": self.item["id"],
                                                                   "content_version": content_version(self.item),
                                                                   "evaluation": "passed"}]})
        tasks.finish(task_id, "returned", "模型返回")
        self.command(f"/task {task_id}")
        self.assertIn(f"经验 {self.item['id']} · 版本 {content_version(self.item)} · 已通过对照",
                      self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
