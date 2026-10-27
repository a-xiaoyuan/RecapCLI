import io
import json
from pathlib import Path
import tempfile
import unittest

from revocli.cli import handle_command
from revocli.storage.session import SessionStore
from revocli.storage.tasks import TaskStore
from revocli.ui.terminal_ui import TerminalUI


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.store = SessionStore(self.workspace)
        self.session = self.store.new_session("test-model")
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.ui = TerminalUI(self.output, self.errors, color="never")

    def command(self, text):
        return handle_command(text, self.ui, self.store, self.session, "test-model")

    def task(self, *, verified=False):
        tasks = TaskStore(self.workspace)
        task_id = tasks.start(self.session["id"], "修复 Kafka 重复消费", "test-model", "react", {})
        tasks.append(task_id, "project_version", {"head": "abc", "dirty": True, "fingerprint": "hash", "complete": True})
        command_id = "a" * 32
        tasks.append(task_id, "tool_result", {"action_id": "one", "call_id": "one", "name": "run_command",
                     "result": {"ok": True, "returncode": 0, "record_id": command_id, "output": "checked"}})
        tasks.finish(task_id, "returned", "模型返回，尚未人工验收")
        if verified:
            tasks.verify(task_id, "passed", command_id, "仅验证 Kafka 消费幂等的本地断言")
        return tasks.get(task_id), command_id

    def test_query_selects_relevant_facts_and_persistent_preferences(self):
        kafka = self.store.add_memory("Kafka 消费通过业务唯一键实现幂等")
        unrelated = self.store.add_memory("向量检索使用 RRF 融合召回")
        preference = self.store.add_memory("回答使用中文", kind="preference")
        context = self.store.memory_context(query="Kafka 重复消费怎么处理")
        self.assertIn(kafka["id"], context.selected_ids)
        self.assertIn(preference["id"], context.selected_ids)
        self.assertNotIn(unrelated["id"], context.selected_ids)
        self.assertEqual(context.omitted_count, 1)
        self.assertIn("user", context.text)
        self.assertEqual(context.sources[0]["id"], preference["id"])

    def test_chinese_and_identifier_queries_match_without_embeddings(self):
        item = self.store.add_memory("上传任务使用 vectorize_with_usage 完成分块向量化")
        for query in ("上传任务怎样向量化", "vectorize_with_usage", "vectorize"):
            with self.subTest(query=query):
                self.assertEqual(self.store.search_memory(query)[0]["id"], item["id"])
        self.assertEqual(self.store.search_memory("完全无关的西瓜"), [])

    def test_expire_update_activate_delete_survive_reload(self):
        item = self.store.add_memory("使用 uv 运行测试")
        self.command(f"/memory-expire {item['id']}")
        restored = SessionStore(self.workspace)
        self.assertEqual(restored.memory_context(query="uv 测试").selected_ids, ())
        restored.update_memory(item["id"], "使用 uv 运行单元测试")
        self.assertEqual(restored.memory_context(query="uv 测试").selected_ids, ())
        self.command(f"/memory-activate {item['id']}")
        self.assertIn(item["id"], SessionStore(self.workspace).memory_context(query="uv 测试").selected_ids)
        self.command(f"/forget {item['id']}")
        self.assertEqual(SessionStore(self.workspace).memory_items(), [])

    def test_time_expired_records_are_not_injected(self):
        self.store.add_memory("uv 测试", expires_at="2000-01-01T00:00:00+00:00")
        self.assertEqual(self.store.memory_context(query="uv").text, "")
        with self.assertRaises(ValueError):
            self.store.add_memory("无时区", expires_at="2030-01-01T00:00:00")

    def test_unverified_task_is_only_a_candidate_until_explicit_acceptance(self):
        task, _ = self.task()
        item = self.store.propose_task_memory(task)
        self.assertEqual(item["source"]["verification"], "unverified")
        self.assertEqual(self.store.memory_context(query="Kafka").text, "")
        self.command(f"/memory-accept {item['id']}")
        context = self.store.memory_context(query="Kafka")
        self.assertIn(item["id"], context.selected_ids)
        self.assertIn(task["id"], context.text)
        self.assertIn("unverified", context.text)
        self.assertEqual(SessionStore(self.workspace).memory_items()[0]["source"]["project_version"]["head"], "abc")

    def test_human_verification_generates_candidate_without_model_or_auto_activation(self):
        task, command_id = self.task()
        self.command(f"/task-verify {task['id']} passed {command_id} 仅核对 Kafka 本地幂等断言")
        item = self.store.memory_items()[0]
        self.assertEqual(item["status"], "candidate")
        self.assertEqual(item["source"]["verification"], "passed")
        self.assertEqual(item["source"]["command_id"], command_id)
        self.assertEqual(self.store.memory_context(query="Kafka").text, "")
        self.command("/memory-candidates")
        self.assertIn(item["id"], self.output.getvalue())
        self.command(f"/memory-reject {item['id']}")
        self.assertEqual(self.store.memory_context(query="Kafka").text, "")

    def test_task_proposal_can_be_edited_without_changing_source_evidence(self):
        task, _ = self.task(verified=True)
        self.command(f"/memory-propose {task['id']} Kafka 消费需要业务幂等键")
        item = self.store.memory_items()[0]
        self.store.update_memory(item["id"], "Kafka 消费以业务主键幂等")
        edited = self.store.memory_items()[0]
        self.assertEqual(edited["source"]["task_id"], task["id"])
        self.assertEqual(edited["source"]["edited_by"], "user")
        self.assertEqual(edited["status"], "candidate")

    def test_running_or_other_workspace_task_cannot_be_proposed(self):
        task, _ = self.task()
        for changed in ({**task, "status": "running"}, {**task, "workspace": str(self.workspace / "other")}):
            with self.assertRaises(ValueError):
                self.store.propose_task_memory(changed)

    def test_sensitive_text_is_rejected_before_write(self):
        for text in ("API_KEY=abcdefghijklmnop", "密码：abcdefghijklmnop", "sk-12345678901234567890"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "凭据"):
                self.store.add_memory(text)
        self.assertFalse(self.store.memory_path.exists())

    def test_legacy_records_still_load_and_retrieve_without_rewriting(self):
        self.store.memory_path.write_text(json.dumps({"items": [{"id": "old", "text": "Kafka 消费者"}]}), encoding="utf-8")
        before = self.store.memory_path.read_bytes()
        context = self.store.memory_context(query="Kafka")
        self.assertEqual(context.selected_ids, ("old",))
        self.assertIn("legacy", context.text)
        self.assertEqual(self.store.memory_path.read_bytes(), before)

    def test_budget_and_status_corruption_do_not_silently_drop_data(self):
        self.store.add_memory("Kafka " * 250)
        short = self.store.add_memory("Kafka 幂等")
        context = self.store.memory_context(query="Kafka", limit=100)
        self.assertEqual(context.selected_ids, (short["id"],))
        self.assertLessEqual(len(context.text), 100)
        data = json.loads(self.store.memory_path.read_text(encoding="utf-8"))
        data["items"][0]["status"] = []
        self.store.memory_path.write_text(json.dumps(data), encoding="utf-8")
        before = self.store.memory_path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.add_memory("新的事实")
        self.assertEqual(self.store.memory_path.read_bytes(), before)

    def test_explicit_preference_and_search_commands(self):
        self.command("/remember preference 使用中文解释")
        self.command("/remember fact 使用 uv 运行测试")
        self.assertEqual(self.store.memory_items()[0]["kind"], "preference")
        self.command("/memory-search uv")
        self.assertIn("来源", self.output.getvalue())
        self.command("/memory-accept missing")
        self.assertIn("只能接纳", self.errors.getvalue())


if __name__ == "__main__":
    unittest.main()
